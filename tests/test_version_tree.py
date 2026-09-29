"""Git 版本树测试 — P2 (branch / merge / rollback / diff_patch / tree)

覆盖:
1. create_branch — 分叉快照、main 保留名、重复分支、指定分叉点
2. 分支 create_version — 非 main tool_id 格式、分支内唯一 head
3. 三向合并 — 无冲突 (双方改不同字段)、字段级冲突抛 VersionConflictError
4. rollback — revert 语义 (不删历史、内容回滚、changelog 自动)
5. diff_patch — 结构化 diff 三段 (added/removed/changed)、分叉空 diff
6. get_version_tree / get_version_chain — 分组视图与 main 线性视图
7. delete_tool — 分支 tool_id 覆盖
"""

from __future__ import annotations

import json

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.models import ToolEntry, ToolContextTier
from youmi.mcp.tool_store import ToolStore, VersionConflictError, _dict_diff


# ===================================================================
# 辅助
# ===================================================================

async def _make_store() -> ToolStore:
    store = ToolStore(db_path=":memory:", embedding_client=None)
    await store.initialize()
    return store


def _defn(name: str, description: str, **kw) -> ToolDefinition:
    return ToolDefinition(
        name=name, description=description,
        parameters=[ToolParameter(name="input", type="string")],
        **kw,
    )


async def _seed(store: ToolStore) -> None:
    """mailer: main 1.0.0 → 1.0.1 (描述演进)"""
    await store.upsert_tool(ToolEntry(
        tool_name="mailer", definition=_defn("mailer", "邮件工具基础版"),
        summary="邮件工具基础版", tier=ToolContextTier.COLD, version="1.0.0",
    ))
    await store.create_version(
        "mailer", _defn("mailer", "邮件工具: 基础版+定时发送"),
        changelog="加定时",
    )


# ===================================================================
# 1. _dict_diff 纯函数
# ===================================================================

class TestDictDiff:

    def test_added_removed_changed(self):
        d = _dict_diff(
            {"a": 1, "b": {"x": 1, "y": 2}, "c": 3},
            {"a": 2, "b": {"x": 1, "y": 9}, "d": 4},
        )
        assert d["added"] == {"d": 4}
        assert d["removed"] == {"c": 3}
        assert d["changed"] == {"a": [1, 2], "b.y": [2, 9]}

    def test_identical_dicts_empty(self):
        d = _dict_diff({"a": 1}, {"a": 1})
        assert d == {"added": {}, "removed": {}, "changed": {}}

    def test_nested_path_prefix(self):
        d = _dict_diff(
            {"cfg": {"deep": {"k": 1}}},
            {"cfg": {"deep": {"k": 2}}},
        )
        assert d["changed"] == {"cfg.deep.k": [1, 2]}

    def test_list_value_changed_wholesale(self):
        """list 值按字段级整体比较 (不递归元素)"""
        d = _dict_diff({"params": [1, 2]}, {"params": [1, 3]})
        assert d["changed"] == {"params": [[1, 2], [1, 3]]}


# ===================================================================
# 2. create_branch
# ===================================================================

class TestCreateBranch:

    @pytest.mark.asyncio
    async def test_branch_from_main_head(self):
        """默认从 main head 分叉, tool_id = {name}@{branch}/{version}"""
        store = await _make_store()
        await _seed(store)

        branch_id = await store.create_branch("mailer", "dev")
        assert branch_id == "mailer@dev/1.0.1"

        tree = await store.get_version_tree("mailer")
        assert "dev" in tree
        dev_head = tree["dev"][-1]
        assert dev_head.version == "1.0.1"
        assert dev_head.is_head
        # 快照内容 = 分叉点内容
        assert "定时发送" in dev_head.definition_json
        await store.close()

    @pytest.mark.asyncio
    async def test_branch_from_explicit_version(self):
        store = await _make_store()
        await _seed(store)

        branch_id = await store.create_branch("mailer", "stable", from_version="1.0.0")
        assert branch_id == "mailer@stable/1.0.0"

        tree = await store.get_version_tree("mailer")
        assert "基础版" in tree["stable"][-1].definition_json
        await store.close()

    @pytest.mark.asyncio
    async def test_duplicate_branch_raises(self):
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")

        with pytest.raises(ValueError, match="already exists"):
            await store.create_branch("mailer", "dev")
        await store.close()

    @pytest.mark.asyncio
    async def test_main_branch_name_reserved(self):
        store = await _make_store()
        await _seed(store)

        with pytest.raises(ValueError, match="reserved"):
            await store.create_branch("mailer", "main")
        await store.close()

    @pytest.mark.asyncio
    async def test_invalid_branch_name_rejected(self):
        store = await _make_store()
        await _seed(store)

        with pytest.raises(ValueError, match="invalid branch name"):
            await store.create_branch("mailer", "a/b")
        with pytest.raises(ValueError, match="invalid branch name"):
            await store.create_branch("mailer", "a@b")
        await store.close()

    @pytest.mark.asyncio
    async def test_branch_fork_point_not_found(self):
        store = await _make_store()
        await _seed(store)

        with pytest.raises(ValueError, match="not found"):
            await store.create_branch("mailer", "dev", from_version="9.9.9")
        with pytest.raises(ValueError, match="not found"):
            await store.create_branch("ghost", "dev")
        await store.close()

    @pytest.mark.asyncio
    async def test_branch_snapshot_empty_diff(self):
        """分叉快照与分叉点内容一致 → 空 diff"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")

        tree = await store.get_version_tree("mailer")
        snapshot = tree["dev"][0]
        assert json.loads(snapshot.diff_patch) == {
            "added": {}, "removed": {}, "changed": {},
        }
        await store.close()


# ===================================================================
# 3. 分支上的版本演进
# ===================================================================

class TestBranchVersions:

    @pytest.mark.asyncio
    async def test_branch_version_tool_id_format(self):
        """非 main 分支 tool_id = {name}@{branch}/{version}; main 保持旧格式"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")

        vid = await store.create_version(
            "mailer", _defn("mailer", "邮件工具: 分支实验版"),
            changelog="实验", branch="dev",
        )
        assert vid == "mailer@dev/1.0.2"

        # main 分支格式不受影响
        mid = await store.create_version(
            "mailer", _defn("mailer", "邮件工具: main 演进"),
            changelog="main 演进",
        )
        assert mid == "mailer@1.0.2"
        await store.close()

    @pytest.mark.asyncio
    async def test_branch_unique_head(self):
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        await store.create_version(
            "mailer", _defn("mailer", "分支 v2"), branch="dev",
        )
        await store.create_version(
            "mailer", _defn("mailer", "分支 v3"), branch="dev",
        )

        tree = await store.get_version_tree("mailer")
        dev = tree["dev"]
        assert [v.version for v in dev] == ["1.0.1", "1.0.2", "1.0.3"]
        heads = [v for v in dev if v.is_head]
        assert len(heads) == 1 and heads[0].version == "1.0.3"
        # main 的 head 不受分支影响
        assert tree["main"][-1].is_head
        await store.close()

    @pytest.mark.asyncio
    async def test_main_branch_versioning_unchanged(self):
        """存量兼容: main 分支 create_version 行为与旧版一致"""
        store = await _make_store()
        await _seed(store)

        vid = await store.create_version(
            "mailer", _defn("mailer", "新版本"), changelog="说明",
        )
        assert vid == "mailer@1.0.2"

        entry = await store.get_tool("mailer", version="1.0.2")
        assert entry is not None
        # changelog 挂在新版本行
        chain = await store.get_version_chain("mailer")
        assert chain[0].version == "1.0.2"
        assert chain[0].parent_version_id == "mailer@1.0.1"
        assert "说明" in chain[0].changelog
        await store.close()


# ===================================================================
# 4. 三向合并
# ===================================================================

class TestMergeBranch:

    @pytest.mark.asyncio
    async def test_merge_no_conflict(self):
        """双方修改不同字段 → 各自保留, minor bump 提交到 target"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        # dev: 改 description
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: 分支实验版(模板变量)"),
            changelog="dev 实验", branch="dev",
        )
        # main: 只改 risk_level (description 不动)
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: 基础版+定时发送",
                            risk_level="medium"),
            changelog="main 提风险", branch="main",
        )

        merged_id = await store.merge_branch("mailer", "dev")
        assert merged_id == "mailer@1.1.0"

        merged = await store.get_tool("mailer", version="1.1.0")
        # 取 source 的 description 修改 + 保留 target 的 risk 修改
        assert "模板变量" in merged.definition.description
        assert merged.definition.risk_level == "medium"
        await store.close()

    @pytest.mark.asyncio
    async def test_merge_conflict_raises(self):
        """双方修改同一字段且不同 → VersionConflictError 列出冲突路径"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: dev 独占描述"), branch="dev",
        )
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: main 独占描述"), branch="main",
        )

        with pytest.raises(VersionConflictError) as exc_info:
            await store.merge_branch("mailer", "dev")
        assert "description" in exc_info.value.conflicts
        assert exc_info.value.tool_name == "mailer"
        # 冲突时不产生新版本
        tree = await store.get_version_tree("mailer")
        assert tree["main"][-1].version == "1.0.2"
        await store.close()

    @pytest.mark.asyncio
    async def test_merge_both_sides_same_change(self):
        """双方做相同修改 → 无冲突, 结果一致"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        same_desc = "邮件工具: 双方一致的修改"
        await store.create_version(
            "mailer", _defn("mailer", same_desc), branch="dev",
        )
        await store.create_version(
            "mailer", _defn("mailer", same_desc), branch="main",
        )

        merged_id = await store.merge_branch("mailer", "dev")
        merged = await store.get_tool("mailer", version=merged_id.split("@")[1])
        assert merged.definition.description == same_desc
        await store.close()

    @pytest.mark.asyncio
    async def test_merge_changelog_auto_generated(self):
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: 分支实验版"), branch="dev",
        )

        await store.merge_branch("mailer", "dev")
        chain = await store.get_version_chain("mailer")
        assert "merge branch 'dev' into 'main'" in chain[0].changelog
        await store.close()

    @pytest.mark.asyncio
    async def test_merge_missing_branch_raises(self):
        store = await _make_store()
        await _seed(store)

        with pytest.raises(ValueError, match="not found"):
            await store.merge_branch("mailer", "no_such_branch")
        await store.close()

    @pytest.mark.asyncio
    async def test_merge_nested_dict_no_conflict(self):
        """嵌套 dict 字段双方改不同子键 → 深入递归无冲突"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        base = ToolDefinition.model_validate_json(
            json.dumps({
                "name": "mailer", "description": "邮件工具: 基础版+定时发送",
                "parameters": [{"name": "input", "type": "string"}],
            }),
        )
        # 用额外字段构造嵌套差异: dev 放宽 risk, main 调整 required
        await store.create_version(
            "mailer",
            _defn("mailer", "邮件工具: 基础版+定时发送", risk_level="low"),
            branch="dev",
        )
        await store.create_version(
            "mailer",
            _defn("mailer", "邮件工具: 基础版+定时发送",
                  required_permissions=["fs:read"]),
            branch="main",
        )
        merged_id = await store.merge_branch("mailer", "dev")
        merged = await store.get_tool("mailer", version=merged_id.split("@")[1])
        assert merged.definition.required_permissions == ["fs:read"]
        await store.close()


# ===================================================================
# 5. rollback
# ===================================================================

class TestRollback:

    @pytest.mark.asyncio
    async def test_rollback_revert_semantics(self):
        """回滚 = 新版本 (内容=目标, parent=当前 head), 历史保留"""
        store = await _make_store()
        await _seed(store)
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: 基础版+定时发送+抄送"),
            changelog="加抄送",
        )

        rollback_id = await store.rollback("mailer", "1.0.0")
        assert rollback_id == "mailer@1.0.3"

        entry = await store.get_tool("mailer", version="1.0.3")
        assert entry.definition.description == "邮件工具基础版"

        # 历史不删除 (1.0.0/1.0.1/1.0.2 + 回滚版 1.0.3); parent = 当前 head
        chain = await store.get_version_chain("mailer")
        assert len(chain) == 4
        assert chain[0].version == "1.0.3"
        assert chain[0].parent_version_id == "mailer@1.0.2"
        assert "rollback to 1.0.0" in chain[0].changelog
        await store.close()

    @pytest.mark.asyncio
    async def test_rollback_diff_records_revert(self):
        """diff_patch 记录回滚差异 (head → 目标)"""
        store = await _make_store()
        await _seed(store)
        await store.create_version(
            "mailer", _defn("mailer", "邮件工具: 完全不同的新描述"),
            changelog="大改",
        )
        await store.rollback("mailer", "1.0.0")

        chain = await store.get_version_chain("mailer")
        diff = json.loads(chain[0].diff_patch)
        assert diff["changed"]["description"] == [
            "邮件工具: 完全不同的新描述", "邮件工具基础版",
        ]
        await store.close()

    @pytest.mark.asyncio
    async def test_rollback_target_not_found(self):
        store = await _make_store()
        await _seed(store)

        with pytest.raises(ValueError, match="not found"):
            await store.rollback("mailer", "9.9.9")
        with pytest.raises(ValueError, match="not found"):
            await store.rollback("ghost", "1.0.0")
        await store.close()


# ===================================================================
# 6. 版本树视图
# ===================================================================

class TestVersionTreeViews:

    @pytest.mark.asyncio
    async def test_get_version_tree_grouped_by_branch(self):
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        await store.create_version(
            "mailer", _defn("mailer", "dev v2"), branch="dev",
        )
        await store.create_version(
            "mailer", _defn("mailer", "main v2"),
        )

        tree = await store.get_version_tree("mailer")
        assert set(tree.keys()) == {"main", "dev"}
        # 每分支时间正序
        assert [v.version for v in tree["main"]] == ["1.0.0", "1.0.1", "1.0.2"]
        assert [v.version for v in tree["dev"]] == ["1.0.1", "1.0.2"]
        # 每分支唯一 head
        assert sum(1 for v in tree["main"] if v.is_head) == 1
        assert sum(1 for v in tree["dev"] if v.is_head) == 1
        # ToolVersion 携带分支元数据
        assert all(v.branch == "main" for v in tree["main"])
        assert all(v.branch == "dev" for v in tree["dev"])
        await store.close()

    @pytest.mark.asyncio
    async def test_version_chain_main_only_linear(self):
        """get_version_chain 是 main 线性视图 (不混入分支行)"""
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        await store.create_version(
            "mailer", _defn("mailer", "dev 独占"), branch="dev",
        )

        chain = await store.get_version_chain("mailer")
        assert [v.version for v in chain] == ["1.0.1", "1.0.0"]
        assert all("dev 独占" not in v.definition_json for v in chain)
        await store.close()

    @pytest.mark.asyncio
    async def test_version_tree_unknown_lineage_empty(self):
        store = await _make_store()
        assert await store.get_version_tree("ghost") == {}
        await store.close()

    @pytest.mark.asyncio
    async def test_create_version_diff_patch_segments(self):
        """create_version 的 diff_patch 三段正确"""
        store = await _make_store()
        await store.upsert_tool(ToolEntry(
            tool_name="cfg_tool", definition=_defn("cfg_tool", "原始描述"),
            summary="原始描述", tier=ToolContextTier.COLD, version="1.0.0",
        ))
        await store.create_version(
            "cfg_tool",
            _defn("cfg_tool", "新描述", risk_level="high"),
            changelog="改描述+提风险",
        )

        chain = await store.get_version_chain("cfg_tool")
        diff = json.loads(chain[0].diff_patch)
        assert diff["changed"]["description"] == ["原始描述", "新描述"]
        assert diff["changed"]["risk_level"] == ["low", "high"]
        await store.close()


# ===================================================================
# 7. delete_tool 分支覆盖
# ===================================================================

class TestDeleteBranchRows:

    @pytest.mark.asyncio
    async def test_delete_by_version_covers_branch_rows(self):
        """按版本删除同时命中 main 行与分支行 (version 列精确匹配)"""
        store = await _make_store()
        await _seed(store)  # main: 1.0.0, 1.0.1
        await store.create_branch("mailer", "dev")  # dev 快照: dev/1.0.1
        await store.create_version(
            "mailer", _defn("mailer", "dev 1.0.2"), branch="dev",
        )

        assert await store.delete_tool("mailer", "1.0.1") is True
        tree = await store.get_version_tree("mailer")
        assert "1.0.1" not in [v.version for v in tree["main"]]
        assert "1.0.1" not in [v.version for v in tree.get("dev", [])]
        # main 1.0.0 与 dev 1.0.2 仍在
        assert "1.0.0" in [v.version for v in tree["main"]]
        assert "1.0.2" in [v.version for v in tree.get("dev", [])]
        await store.close()

    @pytest.mark.asyncio
    async def test_delete_missing_version_returns_false(self):
        store = await _make_store()
        await _seed(store)
        assert await store.delete_tool("mailer", "9.9.9") is False
        await store.close()

    @pytest.mark.asyncio
    async def test_delete_all_versions_includes_branches(self):
        store = await _make_store()
        await _seed(store)
        await store.create_branch("mailer", "dev")
        await store.create_version(
            "mailer", _defn("mailer", "dev v2"), branch="dev",
        )

        assert await store.delete_tool("mailer") is True
        assert await store.get_version_tree("mailer") == {}
        assert await store.get_tool("mailer") is None
        await store.close()
