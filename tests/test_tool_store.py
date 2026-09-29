"""
ToolStore 工具持久化存储层 测试

测试覆盖:
1. 生命周期 — initialize, close
2. 核心 CRUD — upsert_tool, get_tool, list_tools, delete_tool
3. 版本管理 — create_version, get_version_chain, bump_version
4. 变更日志 — add_changelog
5. 向量搜索 — search (向量模式 + 关键词 fallback)
6. 别名与标签 — add_alias, resolve_alias, add_tag, search_by_tags
7. 依赖关系 — add_dependency
8. 诊断统计 — stats
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter, ToolVersion, bump_version
from youmi.mcp.tool_store import ToolStore, _cosine_similarity
from youmi._vec_utils import cosine_similarity_python


# ===================================================================
# 辅助工具
# ===================================================================

def _make_tool(
    name: str,
    description: str = "",
    risk_level: str = "low",
    required_permissions: list[str] | None = None,
) -> ToolDefinition:
    """创建测试用 ToolDefinition (含锥形检索元数据)"""
    return ToolDefinition(
        name=name,
        description=description or f"工具 {name} 的功能描述",
        parameters=[
            ToolParameter(name="input", type="string", description="输入参数"),
        ],
        risk_level=risk_level,
        required_permissions=required_permissions or [],
    )


def _make_entry(
    name: str,
    description: str = "",
    essential: bool = False,
    embedding: list[float] | None = None,
    version: str = "0.0.1",
    risk_level: str = "low",
    required_permissions: list[str] | None = None,
):
    """创建测试用 ToolEntry"""
    from youmi.mcp.vault import ToolEntry, ToolContextTier
    return ToolEntry(
        tool_name=name,
        definition=_make_tool(name, description, risk_level, required_permissions),
        essential=essential,
        summary=description[:80] if description else f"工具 {name}",
        tier=ToolContextTier.COLD,
        embedding=embedding or [],
        version=version,
    )


class MockEmbeddingClient:
    """Mock EmbeddingClient: 根据文本内容生成确定性向量"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._text_to_vec(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._text_to_vec(text)

    def _text_to_vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, c in enumerate(text):
            vec[i % self.dim] += ord(c) / 100.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


@pytest.fixture
async def store():
    """创建内存模式的 ToolStore"""
    s = ToolStore(db_path=":memory:", embedding_dim=8)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def store_with_embedding():
    """创建带 MockEmbeddingClient 的 ToolStore (sqlite-vec vec0 模式)"""
    s = ToolStore(
        db_path=":memory:",
        embedding_client=MockEmbeddingClient(),
        embedding_dim=8,
    )
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def store_fallback():
    """创建强制降级模式的 ToolStore (无 sqlite-vec)"""
    s = ToolStore(
        db_path=":memory:",
        embedding_client=MockEmbeddingClient(),
        embedding_dim=8,
    )
    await s.initialize()
    # 强制关闭 vec0 模式
    s._vec_available = False
    yield s
    await s.close()


# ===================================================================
# 1. 余弦相似度工具函数测试
# ===================================================================

class TestCosineSimilarity:
    """_cosine_similarity 纯函数测试"""

    def test_identical(self):
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal(self):
        assert abs(_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-6

    def test_opposite(self):
        assert abs(_cosine_similarity([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-6

    def test_empty(self):
        assert _cosine_similarity([], [1.0]) == 0.0
        assert _cosine_similarity([1.0], []) == 0.0

    def test_mismatched_length(self):
        assert _cosine_similarity([1.0, 2.0], [1.0]) == 0.0


# ===================================================================
# 2. bump_version 测试
# ===================================================================

class TestBumpVersion:
    """语义化版本号自增测试"""

    def test_patch(self):
        assert bump_version("1.2.3", "patch") == "1.2.4"

    def test_minor(self):
        assert bump_version("1.2.3", "minor") == "1.3.0"

    def test_major(self):
        assert bump_version("1.2.3", "major") == "2.0.0"

    def test_default_is_patch(self):
        assert bump_version("0.0.1") == "0.0.2"

    def test_invalid_format(self):
        assert bump_version("invalid") == "0.0.1"

    def test_two_parts(self):
        assert bump_version("1.2") == "0.0.1"


# ===================================================================
# 3. ToolStore 生命周期测试
# ===================================================================

class TestToolStoreLifecycle:
    """initialize / close 测试"""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self):
        s = ToolStore(db_path=":memory:")
        await s.initialize()
        # 再次 initialize 应该幂等
        await s.initialize()
        stats = await s.stats()
        assert stats["tools"] == 0
        await s.close()

    @pytest.mark.asyncio
    async def test_close_and_reinitialize(self):
        s = ToolStore(db_path=":memory:")
        await s.initialize()
        await s.close()
        # close 后操作应抛异常
        with pytest.raises(RuntimeError):
            await s.list_tools()

    @pytest.mark.asyncio
    async def test_ensure_conn_raises(self):
        s = ToolStore(db_path=":memory:")
        with pytest.raises(RuntimeError, match="not initialized"):
            await s.list_tools()


# ===================================================================
# 4. 核心 CRUD 测试
# ===================================================================

class TestToolStoreCRUD:
    """upsert_tool, get_tool, list_tools, delete_tool 测试"""

    @pytest.mark.asyncio
    async def test_upsert_and_get(self, store):
        entry = _make_entry("test_tool", "测试工具描述")
        tool_id = await store.upsert_tool(entry)
        assert tool_id == "test_tool@0.0.1"

        # 获取最新版本
        result = await store.get_tool("test_tool")
        assert result is not None
        assert result.tool_name == "test_tool"
        assert result.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, store):
        result = await store.get_tool("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_specific_version(self, store):
        entry = _make_entry("tool_a", "描述A")
        await store.upsert_tool(entry)

        result = await store.get_tool("tool_a", version="0.0.1")
        assert result is not None
        assert result.tool_name == "tool_a"

        result2 = await store.get_tool("tool_a", version="9.9.9")
        assert result2 is None

    @pytest.mark.asyncio
    async def test_list_tools(self, store):
        await store.upsert_tool(_make_entry("tool_a", "描述A"))
        await store.upsert_tool(_make_entry("tool_b", "描述B"))
        await store.upsert_tool(_make_entry("tool_c", "描述C"))

        tools = await store.list_tools()
        assert len(tools) == 3
        names = {t.tool_name for t in tools}
        assert names == {"tool_a", "tool_b", "tool_c"}

    @pytest.mark.asyncio
    async def test_delete_tool(self, store):
        await store.upsert_tool(_make_entry("tool_x", "描述X"))
        assert await store.get_tool("tool_x") is not None

        result = await store.delete_tool("tool_x")
        assert result is True
        assert await store.get_tool("tool_x") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store):
        result = await store.delete_tool("nonexistent")
        assert result is True  # 删除不存在的不报错

    @pytest.mark.asyncio
    async def test_upsert_with_embedding(self, store_with_embedding):
        """vec0 模式: 向量归一化后存储，读回为归一化向量"""
        store = store_with_embedding
        if not store._vec_available:
            pytest.skip("sqlite-vec 不可用，跳过 vec0 归一化测试")

        import math
        raw = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        entry = _make_entry("tool_emb", "带向量的工具", embedding=raw)
        await store.upsert_tool(entry)

        result = await store.get_tool("tool_emb")
        assert result is not None
        assert len(result.embedding) == 8
        # 归一化后模长应为 ~1.0
        norm = math.sqrt(sum(x * x for x in result.embedding))
        assert abs(norm - 1.0) < 1e-5

    @pytest.mark.asyncio
    async def test_upsert_with_embedding_fallback(self, store_fallback):
        """降级模式: 向量原样存储在 JSON 列"""
        raw = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        entry = _make_entry("tool_emb", "带向量的工具", embedding=raw)
        await store_fallback.upsert_tool(entry)

        result = await store_fallback.get_tool("tool_emb")
        assert result is not None
        assert result.embedding == raw

    @pytest.mark.asyncio
    async def test_get_latest_version(self, store):
        entry = _make_entry("tool_v", "初始版本")
        await store.upsert_tool(entry)

        latest = await store.get_latest_version("tool_v")
        assert latest is not None
        assert latest.version == "0.0.1"


# ===================================================================
# 5. 版本管理测试
# ===================================================================

class TestToolStoreVersioning:
    """create_version, get_version_chain 测试"""

    @pytest.mark.asyncio
    async def test_create_version_patch(self, store):
        entry = _make_entry("versioned_tool", "初始版本")
        await store.upsert_tool(entry)

        new_def = _make_tool("versioned_tool", "修复了 bug")
        new_id = await store.create_version(
            "versioned_tool", new_def, changelog="修复了一个 bug", bump="patch"
        )
        assert new_id == "versioned_tool@0.0.2"

    @pytest.mark.asyncio
    async def test_create_version_minor(self, store):
        entry = _make_entry("tool_minor", "初始")
        await store.upsert_tool(entry)

        new_def = _make_tool("tool_minor", "新增功能")
        new_id = await store.create_version("tool_minor", new_def, bump="minor")
        assert new_id == "tool_minor@0.1.0"

    @pytest.mark.asyncio
    async def test_create_version_major(self, store):
        entry = _make_entry("tool_major", "初始")
        await store.upsert_tool(entry)

        new_def = _make_tool("tool_major", "破坏性变更")
        new_id = await store.create_version("tool_major", new_def, bump="major")
        assert new_id == "tool_major@1.0.0"

    @pytest.mark.asyncio
    async def test_version_chain(self, store):
        entry = _make_entry("chain_tool", "v1")
        await store.upsert_tool(entry)

        # 创建 v2
        v2_def = _make_tool("chain_tool", "v2 改进")
        await store.create_version("chain_tool", v2_def, changelog="v2 变更", bump="patch")

        # 创建 v3
        v3_def = _make_tool("chain_tool", "v3 大改")
        await store.create_version("chain_tool", v3_def, changelog="v3 变更", bump="minor")

        chain = await store.get_version_chain("chain_tool")
        assert len(chain) == 3
        # 按时间倒序: 最新在前
        assert chain[0].version == "0.1.0"
        assert chain[1].version == "0.0.2"
        assert chain[2].version == "0.0.1"

    @pytest.mark.asyncio
    async def test_create_version_nonexistent(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.create_version("no_such_tool", _make_tool("no_such_tool"))

    @pytest.mark.asyncio
    async def test_get_version(self, store):
        entry = _make_entry("ver_tool", "初始")
        await store.upsert_tool(entry)

        v2_def = _make_tool("ver_tool", "v2")
        await store.create_version("ver_tool", v2_def, bump="patch")

        v1 = await store.get_version("ver_tool", "0.0.1")
        assert v1 is not None
        assert v1.version == "0.0.1"

        v2 = await store.get_version("ver_tool", "0.0.2")
        assert v2 is not None
        assert v2.version == "0.0.2"


# ===================================================================
# 6. 变更日志测试
# ===================================================================

class TestToolStoreChangelog:
    """add_changelog 测试"""

    @pytest.mark.asyncio
    async def test_add_changelog(self, store):
        entry = _make_entry("log_tool", "有日志的工具")
        await store.upsert_tool(entry)

        await store.add_changelog("log_tool", "bugfix", "修复了空指针", source="agent-001")

        chain = await store.get_version_chain("log_tool")
        assert len(chain) == 1
        assert "修复了空指针" in chain[0].changelog

    @pytest.mark.asyncio
    async def test_multiple_changelogs(self, store):
        entry = _make_entry("multi_log", "多日志工具")
        await store.upsert_tool(entry)

        await store.add_changelog("multi_log", "bugfix", "修复 A")
        await store.add_changelog("multi_log", "description_update", "更新描述")

        chain = await store.get_version_chain("multi_log")
        assert len(chain) == 1
        assert "修复 A" in chain[0].changelog
        assert "更新描述" in chain[0].changelog

    @pytest.mark.asyncio
    async def test_changelog_nonexistent_tool(self, store):
        with pytest.raises(ValueError, match="not found"):
            await store.add_changelog("no_tool", "bugfix", "修复")


# ===================================================================
# 7. 向量搜索测试
# ===================================================================

class TestToolStoreSearch:
    """search 向量搜索和关键词搜索测试"""

    @pytest.mark.asyncio
    async def test_vector_search(self, store_with_embedding):
        store = store_with_embedding
        await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await store.upsert_tool(_make_entry("calc_math", "执行数学计算"))
        await store.upsert_tool(_make_entry("search_web", "搜索互联网内容"))

        # 更新所有 embedding
        await store.update_embedding("send_email")
        await store.update_embedding("calc_math")
        await store.update_embedding("search_web")

        results = await store.search("发送邮件", top_k=3, min_score=0.0)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_vector_search_with_exclude(self, store_with_embedding):
        store = store_with_embedding
        await store.upsert_tool(_make_entry("tool_a", "工具 A 描述"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B 描述"))

        await store.update_embedding("tool_a")
        await store.update_embedding("tool_b")

        results = await store.search("工具", top_k=5, min_score=0.0, exclude={"tool_a"})
        names = {r.tool_name for r in results}
        assert "tool_a" not in names

    @pytest.mark.asyncio
    async def test_keyword_search_fallback(self, store):
        """无 embedding_client 时使用关键词搜索"""
        await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await store.upsert_tool(_make_entry("calc_math", "执行数学计算表达式"))

        results = await store.search("发送邮件", top_k=3)
        # 关键词匹配应该能找到 send_email
        if results:
            assert any(r.tool_name == "send_email" for r in results)

    @pytest.mark.asyncio
    async def test_keyword_search_with_exclude(self, store):
        await store.upsert_tool(_make_entry("send_email", "发送电子邮件"))
        await store.upsert_tool(_make_entry("send_sms", "发送短信消息"))

        results = await store.search("发送", top_k=5, exclude={"send_email"})
        names = {r.tool_name for r in results}
        assert "send_email" not in names

    @pytest.mark.asyncio
    async def test_search_empty_store(self, store):
        results = await store.search("任何查询")
        assert results == []

    @pytest.mark.asyncio
    async def test_update_embedding_no_client(self, store):
        """无 embedding_client 时 update_embedding 应静默返回"""
        await store.upsert_tool(_make_entry("tool_x", "描述"))
        await store.update_embedding("tool_x")  # 不应报错

    @pytest.mark.asyncio
    async def test_vec0_search(self, store_with_embedding):
        """sqlite-vec vec0 KNN 搜索"""
        store = store_with_embedding
        if not store._vec_available:
            pytest.skip("sqlite-vec 不可用，跳过 vec0 搜索测试")

        await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await store.upsert_tool(_make_entry("calc_math", "执行数学计算"))
        await store.upsert_tool(_make_entry("search_web", "搜索互联网内容"))

        await store.update_embedding("send_email")
        await store.update_embedding("calc_math")
        await store.update_embedding("search_web")

        results = await store.search("发送邮件", top_k=3, min_score=0.0)
        assert len(results) > 0
        # 分数应为余弦相似度 (0-1 范围)
        for r in results:
            assert -1.0 <= r.score <= 1.0

    @pytest.mark.asyncio
    async def test_fallback_vector_search(self, store_fallback):
        """降级模式: JSON 向量表 + Python 余弦相似度"""
        store = store_fallback
        assert not store._vec_available, "测试需要降级模式"

        await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址"))
        await store.upsert_tool(_make_entry("calc_math", "执行数学计算"))

        await store.update_embedding("send_email")
        await store.update_embedding("calc_math")

        results = await store.search("发送邮件", top_k=3, min_score=0.0)
        assert len(results) > 0
        for r in results:
            assert -1.0 <= r.score <= 1.0

    @pytest.mark.asyncio
    async def test_vec0_stats(self, store_with_embedding):
        """vec0 模式下 stats 正确统计向量数"""
        store = store_with_embedding
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))
        await store.update_embedding("tool_a")
        await store.update_embedding("tool_b")

        stats = await store.stats()
        assert stats["vectors"] == 2


# ===================================================================
# 8. 别名与标签测试
# ===================================================================

class TestToolStoreAliasAndTags:
    """别名和标签功能测试"""

    @pytest.mark.asyncio
    async def test_add_and_resolve_alias(self, store):
        entry = _make_entry("send_email", "发送邮件")
        await store.upsert_tool(entry)

        await store.add_alias("legacy_email", "send_email", "0.0.1")

        resolved = await store.resolve_alias("legacy_email")
        assert resolved is not None
        assert resolved.tool_name == "send_email"
        assert resolved.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_alias(self, store):
        result = await store.resolve_alias("no_such_alias")
        assert result is None

    @pytest.mark.asyncio
    async def test_add_tag(self, store):
        entry = _make_entry("tagged_tool", "有标签的工具")
        await store.upsert_tool(entry)

        await store.add_tag("tagged_tool", "communication")
        await store.add_tag("tagged_tool", "email")

        results = await store.search_by_tags(["communication"])
        assert len(results) >= 1
        assert any(r.tool_name == "tagged_tool" for r in results)

    @pytest.mark.asyncio
    async def test_search_by_multiple_tags(self, store):
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))

        await store.add_tag("tool_a", "web")
        await store.add_tag("tool_b", "email")
        await store.add_tag("tool_a", "search")

        results = await store.search_by_tags(["web", "email"])
        names = {r.tool_name for r in results}
        assert "tool_a" in names
        assert "tool_b" in names

    @pytest.mark.asyncio
    async def test_search_by_nonexistent_tag(self, store):
        results = await store.search_by_tags(["nonexistent_tag"])
        assert results == []


# ===================================================================
# 9. 依赖关系测试
# ===================================================================

class TestToolStoreDependencies:
    """add_dependency 测试"""

    @pytest.mark.asyncio
    async def test_add_dependency(self, store):
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))

        # 不应报错
        await store.add_dependency("tool_a", "tool_b", dep_type="required")

    @pytest.mark.asyncio
    async def test_add_dependency_nonexistent(self, store):
        # 不存在的工具不应报错（静默忽略）
        await store.add_dependency("no_tool", "also_no_tool")


# ===================================================================
# 10. 诊断统计测试
# ===================================================================

class TestToolStoreStats:
    """stats 统计信息测试"""

    @pytest.mark.asyncio
    async def test_empty_stats(self, store):
        stats = await store.stats()
        assert stats["tools"] == 0
        assert stats["versions"] == 0
        assert stats["vectors"] == 0
        assert stats["changelogs"] == 0
        assert stats["aliases"] == 0
        assert stats["tags"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_data(self, store):
        await store.upsert_tool(_make_entry("tool_a", "工具 A"))
        await store.upsert_tool(_make_entry("tool_b", "工具 B"))
        await store.add_tag("tool_a", "tag1")
        await store.add_changelog("tool_a", "bugfix", "修复")

        stats = await store.stats()
        assert stats["tools"] == 2
        assert stats["versions"] == 2
        assert stats["changelogs"] == 1
        assert stats["tags"] == 1

    @pytest.mark.asyncio
    async def test_repr(self, store):
        r = repr(store)
        assert "ToolStore" in r
        assert ":memory:" in r


# ===================================================================
# 11. 锥形检索元数据 (风险位/权限位/lineage) 测试
# ===================================================================

class TestConeMetadata:
    """risk_level / required_permissions / lineage_id 读写贯通测试"""

    @pytest.mark.asyncio
    async def test_risk_permissions_roundtrip(self, store):
        """upsert 后 get_tool 读回 definition 携带的风险位/权限位"""
        await store.upsert_tool(_make_entry(
            "danger_tool", "危险工具",
            risk_level="critical", required_permissions=["fs:delete", "shell:exec"],
        ))

        got = await store.get_tool("danger_tool")
        assert got is not None
        assert got.risk_level == "critical"
        assert got.required_permissions == ["fs:delete", "shell:exec"]
        assert got.lineage_id == "danger_tool"
        # definition 同步携带
        assert got.definition.risk_level == "critical"

    @pytest.mark.asyncio
    async def test_default_risk_is_low(self, store):
        """未指定风险位的工具默认 low / 无权限门槛"""
        await store.upsert_tool(_make_entry("plain_tool", "普通工具"))
        got = await store.get_tool("plain_tool")
        assert got.risk_level == "low"
        assert got.required_permissions == []
        assert got.lineage_id == "plain_tool"

    @pytest.mark.asyncio
    async def test_version_inherits_lineage(self, store):
        """create_version 后新版本继承同一 lineage_id 且风险位随新 definition"""
        await store.upsert_tool(_make_entry("vtool", "v1", risk_level="low"))
        v2 = _make_tool("vtool", "v2 更危险", risk_level="high")
        await store.create_version("vtool", v2, bump="patch")

        got = await store.get_tool("vtool")  # 最新 = v2
        assert got.version == "0.0.2"
        assert got.risk_level == "high"
        assert got.lineage_id == "vtool"  # 与 v1 同一版本链

    @pytest.mark.asyncio
    async def test_corrupted_permissions_json_fallback(self, store):
        """required_permissions 列损坏时回退空列表"""
        await store.upsert_tool(_make_entry("bad_perm", "工具"))
        conn = store._ensure_conn()
        conn.execute(
            "UPDATE tools SET required_permissions = 'NOT_JSON' WHERE tool_name = 'bad_perm'"
        )
        conn.commit()

        got = await store.get_tool("bad_perm")
        assert got is not None
        assert got.required_permissions == []


# ===================================================================
# 12. Schema 迁移测试
# ===================================================================

_LEGACY_SCHEMA_SQL = """
CREATE TABLE tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id TEXT UNIQUE NOT NULL,
    tool_name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '0.0.1',
    parent_version_id TEXT,
    provider_id TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    definition_json TEXT NOT NULL,
    handler_module TEXT DEFAULT '',
    language TEXT DEFAULT 'python',
    runtime TEXT DEFAULT 'python',
    essential INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class TestSchemaMigration:
    """旧库 → 锥形检索列的幂等迁移测试"""

    def test_migrate_adds_columns_and_backfills_lineage(self):
        """旧 schema 补齐三列 + lineage_id 回填为 tool_name"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(_LEGACY_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tools (tool_id, tool_name, definition_json, created_at, updated_at) "
            "VALUES ('legacy@0.0.1', 'legacy', '{}', '2024-01-01', '2024-01-01')"
        )
        conn.commit()

        ToolStore._migrate_schema(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(tools)")}
        assert {"risk_level", "required_permissions", "lineage_id"} <= cols

        row = conn.execute(
            "SELECT risk_level, required_permissions, lineage_id FROM tools WHERE tool_name = 'legacy'"
        ).fetchone()
        assert row == ("low", "[]", "legacy")  # 默认值 + lineage 回填
        conn.close()

    def test_migrate_idempotent(self):
        """重复迁移不报错不重复加列"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(_LEGACY_SCHEMA_SQL)
        conn.commit()

        ToolStore._migrate_schema(conn)
        ToolStore._migrate_schema(conn)
        ToolStore._migrate_schema(conn)

        cols = [row[1] for row in conn.execute("PRAGMA table_info(tools)")]
        # 每列只出现一次
        assert cols.count("risk_level") == 1
        assert cols.count("lineage_id") == 1
        conn.close()

    @pytest.mark.asyncio
    async def test_initialize_upgrades_legacy_db(self, tmp_path):
        """带旧 schema 的存量库文件经 initialize() 自动升级"""
        import sqlite3
        db_file = str(tmp_path / "legacy_tools.db")
        conn = sqlite3.connect(db_file)
        conn.executescript(_LEGACY_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tools (tool_id, tool_name, definition_json, created_at, updated_at) "
            "VALUES ('old@0.0.1', 'old', '{}', '2024-01-01', '2024-01-01')"
        )
        conn.commit()
        conn.close()

        s = ToolStore(db_path=db_file)
        await s.initialize()
        got = await s.get_tool("old")
        # definition_json = '{}' 无法解析 → None; 直接查列验证
        if got is None:
            row = s._ensure_conn().execute(
                "SELECT lineage_id, risk_level FROM tools WHERE tool_name = 'old'"
            ).fetchone()
            assert row == ("old", "low")
        else:
            assert got.lineage_id == "old"
        await s.close()


# ===================================================================
# 13. 锥形联合查询 (search_cone) 测试
# ===================================================================

async def _seed_cone_store(store):
    """填充锥形测试数据: 三个不同风险级别的工具 + tag"""
    await store.upsert_tool(_make_entry("send_email", "发送电子邮件到指定地址", risk_level="low"))
    await store.upsert_tool(_make_entry(
        "write_file", "写入文件内容", risk_level="medium", required_permissions=["fs:write"],
    ))
    await store.upsert_tool(_make_entry(
        "rm_file", "删除文件的shell工具", risk_level="critical", required_permissions=["fs:delete"],
    ))
    if store._embedding_client:
        await store.update_embedding("send_email")
        await store.update_embedding("write_file")
        await store.update_embedding("rm_file")
    await store.add_tag("send_email", "communication")
    await store.add_tag("write_file", "fs")


class TestConeSearch:
    """search_cone — 语义方向 ∩ tag 边界 ∩ 风险边界 联合查询"""

    @pytest.mark.asyncio
    async def test_cone_risk_ceiling_vec0(self, store_with_embedding):
        """vec0 路径: 风险上限在 SQL 内裁剪 (critical 不召回)"""
        store = store_with_embedding
        if not store._vec_available:
            pytest.skip("sqlite-vec 不可用")
        await _seed_cone_store(store)

        results = await store.search_cone(
            "工具", top_k=10, min_score=0.0, risk_ceiling="medium",
        )
        names = {r.tool_name for r in results}
        assert "rm_file" not in names  # critical > medium 被裁
        assert "send_email" in names and "write_file" in names

    @pytest.mark.asyncio
    async def test_cone_risk_ceiling_fallback(self, store_fallback):
        """降级路径: 同一风险边界语义"""
        store = store_fallback
        await _seed_cone_store(store)

        results = await store.search_cone(
            "工具", top_k=10, min_score=0.0, risk_ceiling="medium",
        )
        names = {r.tool_name for r in results}
        assert "rm_file" not in names
        assert "send_email" in names and "write_file" in names

    @pytest.mark.asyncio
    async def test_cone_tags_boundary(self, store_with_embedding):
        """tag 边界: 仅命中指定 tag 的工具可召回"""
        store = store_with_embedding
        await _seed_cone_store(store)

        results = await store.search_cone(
            "工具", top_k=10, min_score=0.0, tags=["communication"],
        )
        assert {r.tool_name for r in results} == {"send_email"}

    @pytest.mark.asyncio
    async def test_cone_tags_multiple(self, store_with_embedding):
        """tag 边界: 多 tag 任意命中即召回"""
        store = store_with_embedding
        await _seed_cone_store(store)

        results = await store.search_cone(
            "工具", top_k=10, min_score=0.0, tags=["communication", "fs"],
        )
        assert {r.tool_name for r in results} == {"send_email", "write_file"}

    @pytest.mark.asyncio
    async def test_cone_exclude_lineages(self, store_with_embedding):
        """lineage 排除: 同版本链去重 (GitLineageGuard 依赖)"""
        store = store_with_embedding
        await _seed_cone_store(store)

        results = await store.search_cone(
            "工具", top_k=10, min_score=0.0, exclude_lineages={"write_file"},
        )
        names = {r.tool_name for r in results}
        assert "write_file" not in names
        assert "send_email" in names

    @pytest.mark.asyncio
    async def test_cone_combined_boundaries(self, store_with_embedding):
        """组合锥形: 风险 ∩ tag ∩ 排除 联合裁剪"""
        store = store_with_embedding
        await _seed_cone_store(store)

        results = await store.search_cone(
            "工具", top_k=10, min_score=0.0,
            tags=["communication", "fs"], risk_ceiling="low",
            exclude={"nonexistent"},
        )
        # write_file (medium) 被风险边界裁掉; 只剩 send_email
        assert {r.tool_name for r in results} == {"send_email"}

    @pytest.mark.asyncio
    async def test_cone_result_carries_metadata(self, store_with_embedding):
        """结果携带风险位/权限位/lineage (P1 AuditGate 的前提)"""
        store = store_with_embedding
        await _seed_cone_store(store)

        results = await store.search_cone("删除", top_k=5, min_score=0.0)
        rm = next((r for r in results if r.tool_name == "rm_file"), None)
        assert rm is not None
        assert rm.risk_level == "critical"
        assert rm.required_permissions == ["fs:delete"]
        assert rm.lineage_id == "rm_file"

    @pytest.mark.asyncio
    async def test_cone_keyword_fallback(self, store):
        """无 embedding client: 关键词路径同样接受锥形约束"""
        await _seed_cone_store(store)

        # 关键词"文件": 字符级匹配命中 write_file ("写入文件内容") 与
        # send_email ("电子邮件"含"文"); rm_file ("删除文件...") 无 communication/fs
        # tag 被 SQL 锥形裁掉; 风险上限 medium 不影响 low/medium 工具
        results = await store.search_cone(
            "文件", top_k=10, risk_ceiling="medium", tags=["communication", "fs"],
        )
        names = {r.tool_name for r in results}
        assert "rm_file" not in names
        assert names == {"send_email", "write_file"}

    @pytest.mark.asyncio
    async def test_search_backward_compat(self, store_with_embedding):
        """旧 search() 无锥形参数时行为不变 (critical 也能召回)"""
        store = store_with_embedding
        await _seed_cone_store(store)

        results = await store.search("工具", top_k=10, min_score=0.0)
        names = {r.tool_name for r in results}
        assert names == {"send_email", "write_file", "rm_file"}


# ===================================================================
# 10. 三级摘要索引 — L1/L2 双层向量 + index_level + rebuild_summaries (P2)
# ===================================================================

async def _seed_summary_store(store: ToolStore) -> None:
    """seed 两个工具并生成双层向量"""
    await store.upsert_tool(_make_entry(
        "send_email", "发送电子邮件到指定地址并支持附件",
    ))
    await store.upsert_tool(_make_entry(
        "write_file", "写入文件内容到指定路径",
    ))
    await store.update_embedding("send_email")
    await store.update_embedding("write_file")


class TestThreeLevelSummaryIndex:
    """三级摘要索引: L1 摘要向量 + L2 摘要的摘要薄层初筛"""

    @pytest.mark.asyncio
    async def test_dual_level_vectors_written(self, store_with_embedding):
        """update_embedding 后 L1/L2 双层向量各自落库"""
        store = store_with_embedding
        await _seed_summary_store(store)

        l1_cnt = store._conn.execute(
            "SELECT COUNT(*) FROM vec_tools_idx",
        ).fetchone()[0]
        l2_cnt = store._conn.execute(
            "SELECT COUNT(*) FROM vec_tools_l2_idx",
        ).fetchone()[0]
        assert l1_cnt == 2
        assert l2_cnt == 2
        await store.close()

    @pytest.mark.asyncio
    async def test_l1_index_level_default(self, store_with_embedding):
        """默认 index_level='l1': 与历史行为一致"""
        store = store_with_embedding
        await _seed_summary_store(store)

        results = await store.search_cone("邮件", top_k=3, min_score=0.0)
        assert "send_email" in {r.tool_name for r in results}

        # 显式 l1 与默认等价
        explicit = await store.search_cone(
            "邮件", top_k=3, min_score=0.0, index_level="l1",
        )
        assert {r.tool_name for r in explicit} == {r.tool_name for r in results}
        await store.close()

    @pytest.mark.asyncio
    async def test_l2_index_level_recall_with_l1_summary(self, store_with_embedding):
        """L2 薄层初筛命中, 结果仍携带 L1 摘要文本"""
        store = store_with_embedding
        await _seed_summary_store(store)

        results = await store.search_cone(
            "邮件", top_k=3, min_score=0.0, index_level="l2",
        )
        assert "send_email" in {r.tool_name for r in results}
        hit = next(r for r in results if r.tool_name == "send_email")
        assert "发送电子邮件" in hit.summary  # L1 摘要文本
        await store.close()

    @pytest.mark.asyncio
    async def test_l2_falls_back_to_l1_when_empty(self, store_with_embedding):
        """存量行无 L2 向量 → L2 检索自动回退 L1"""
        store = store_with_embedding
        await _seed_summary_store(store)
        store._conn.execute("DELETE FROM vec_tools_l2_idx")
        store._conn.commit()

        results = await store.search_cone(
            "邮件", top_k=3, min_score=0.0, index_level="l2",
        )
        assert "send_email" in {r.tool_name for r in results}
        await store.close()

    @pytest.mark.asyncio
    async def test_l2_fallback_json_path(self, store_fallback):
        """降级路径 (无 sqlite-vec): L2 无命中同样回退 L1"""
        store = store_fallback
        await _seed_summary_store(store)
        store._conn.execute(
            "DELETE FROM vec_tools WHERE level = 'l2'",
        )
        store._conn.commit()

        results = await store.search_cone(
            "邮件", top_k=3, min_score=0.0, index_level="l2",
        )
        assert "send_email" in {r.tool_name for r in results}
        await store.close()

    @pytest.mark.asyncio
    async def test_summary_l2_read_back(self, store_with_embedding):
        """读取贯通: summary_l2 落库后可从 get_tool/list_tools 读回"""
        store = store_with_embedding
        await _seed_summary_store(store)

        entry = await store.get_tool("send_email")
        assert entry.summary == "发送电子邮件到指定地址并支持附件"
        assert entry.summary_l2 == entry.summary[:30]

        listed = await store.list_tools()
        se = next(e for e in listed if e.tool_name == "send_email")
        assert se.summary_l2 == entry.summary[:30]
        await store.close()

    @pytest.mark.asyncio
    async def test_rebuild_summaries_heuristic(self, store_with_embedding):
        """rebuild_summaries: 全量重算 L1/L2 并重建双层向量"""
        store = store_with_embedding
        await _seed_summary_store(store)
        await store.create_version(
            "send_email", _make_tool("send_email", "新版本描述完全不同"),
            changelog="大改",
        )

        n = await store.rebuild_summaries()
        assert n == 3  # send_email 2 版 + write_file 1 版

        v1 = await store.get_tool("send_email", version="0.0.1")
        v2 = await store.get_tool("send_email", version="0.0.2")
        assert v1.summary == "发送电子邮件到指定地址并支持附件"
        assert v2.summary == "新版本描述完全不同"
        assert v2.summary_l2 == "新版本描述完全不同"[:30]

        # 双层向量同步重建
        l2_cnt = store._conn.execute(
            "SELECT COUNT(*) FROM vec_tools_l2_idx",
        ).fetchone()[0]
        assert l2_cnt == 3
        await store.close()

    @pytest.mark.asyncio
    async def test_rebuild_summaries_empty_store(self, store):
        assert await store.rebuild_summaries() == 0
        await store.close()

    @pytest.mark.asyncio
    async def test_rebuild_summaries_with_llm_generator(self, store_with_embedding):
        """注入 LLM 生成器的 rebuild: 生成式摘要覆盖存量行"""

        class FakeLLM:
            async def chat(self, messages):
                return "LLM 摘要"

        from youmi.mcp.summary import SummaryGenerator
        store = store_with_embedding
        await _seed_summary_store(store)
        store._summary_generator = SummaryGenerator(FakeLLM())

        await store.rebuild_summaries()
        entry = await store.get_tool("send_email")
        assert entry.summary == "LLM 摘要"
        assert entry.summary_l2 == "LLM 摘要"
        await store.close()


# ===================================================================
# 11. SummaryGenerator 三级摘要生成器 (P2)
# ===================================================================

class TestSummaryGenerator:
    """youmi/mcp/summary.py — 默认启发式 / LLM 生成式 / 失败回退"""

    @pytest.mark.asyncio
    async def test_heuristic_l1_truncation(self):
        from youmi.mcp.summary import L1_MAX_CHARS, SummaryGenerator
        gen = SummaryGenerator()

        long_desc = "描述" * 100
        l1 = await gen.generate_l1("tool", long_desc)
        assert l1 == long_desc[:L1_MAX_CHARS]

    @pytest.mark.asyncio
    async def test_heuristic_l2_truncation(self):
        from youmi.mcp.summary import L2_MAX_CHARS, SummaryGenerator
        gen = SummaryGenerator()

        l2 = await gen.generate_l2("tool", "L1 摘要" * 20)
        assert l2 == ("L1 摘要" * 20)[:L2_MAX_CHARS]

    @pytest.mark.asyncio
    async def test_empty_description_returns_empty(self):
        from youmi.mcp.summary import SummaryGenerator
        gen = SummaryGenerator()

        assert await gen.generate_l1("tool", "") == ""
        assert await gen.generate_l2("tool", "") == ""

    @pytest.mark.asyncio
    async def test_llm_generative_summary(self):
        from youmi.mcp.summary import SummaryGenerator

        class FakeLLM:
            async def chat(self, messages):
                assert messages[0]["content"]  # 提示词非空
                return "生成式摘要"

        gen = SummaryGenerator(FakeLLM())
        assert await gen.generate_l1("tool", "描述") == "生成式摘要"
        assert await gen.generate_l2("tool", "L1") == "生成式摘要"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_heuristic(self):
        from youmi.mcp.summary import SummaryGenerator

        class BrokenLLM:
            async def chat(self, messages):
                raise RuntimeError("llm down")

        gen = SummaryGenerator(BrokenLLM())
        assert await gen.generate_l1("tool", "启发式描述") == "启发式描述"
        assert await gen.generate_l2("tool", "启发式") == "启发式"

    @pytest.mark.asyncio
    async def test_llm_empty_reply_falls_back(self):
        from youmi.mcp.summary import SummaryGenerator

        class EmptyLLM:
            async def chat(self, messages):
                return ""

        gen = SummaryGenerator(EmptyLLM())
        assert await gen.generate_l1("tool", "描述") == "描述"

    def test_repr(self):
        from youmi.mcp.summary import SummaryGenerator

        assert "heuristic" in repr(SummaryGenerator())
        assert "llm" in repr(SummaryGenerator(object()))
