"""工具版本规则与回归闭环 测试

规则单一源: ``youmi/core/versioning.py``

覆盖:
1. 版本号命名规则 — 主版本号(不兼容变更)/次版本号(兼容功能)/修订号(仅修复)
2. 来源与初始版本 — 外部接入 1.0.0 / 系统自生成 0.0.1 (候选库)
3. ToolStore — 初始版本落库 / change_kind 提升 / promote_to_stable /
   mark_external / head 保护 / 统计
4. ToolVault — add_tool / add_tools_from_provider 按来源定版本并落库
5. SkillIngestor — 外部技能默认 1.0.0 且联动携带工具转正式版
6. PostTaskPipeline — 连续成功达阈值晋升 / 失败清零 / 修复提交修订号
7. ToolGuardian — 修复有效提交修订号 / 多次无效重试后转人工
"""

from __future__ import annotations

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.core.versioning import (
    AUTO_FIX_MAX_ATTEMPTS,
    GENERATED_INITIAL_VERSION,
    PROMOTION_MIN_SUCCESSES,
    STABLE_INITIAL_VERSION,
    ChangeKind,
    VersionStage,
    bump_for,
    compare_versions,
    initial_version,
    is_promotion_eligible,
    resolve_initial_version,
    stage_of,
)
from youmi.coordinator.post_task import PostTaskPipeline, ToolExperience
from youmi.coordinator.tool_guardian import ToolGuardianAgent
from youmi.mcp.approval import ApprovalDecision, ApprovalManager
from youmi.mcp.models import ToolContextTier, ToolEntry
from youmi.mcp.protocol import ToolIssueReport, ToolIssueType
from youmi.mcp.provider import LocalFunctionProvider
from youmi.mcp.server import MCPServer
from youmi.mcp.skill_ingest import SkillIngestor
from youmi.mcp.skill_store import SkillStore
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.vault import ToolVault


# ===================================================================
# 辅助
# ===================================================================

_SKILL_DOC = """---
skill: 邮件汇报流程
describe: 每日汇总数据并发送邮件
tools: send_email
tags: [report, email]
---

1. 拉取当日数据
2. 渲染模板并调用邮件工具
"""


def _defn(
    name: str,
    description: str = "",
    version: str = GENERATED_INITIAL_VERSION,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"工具 {name} 的功能描述",
        parameters=[ToolParameter(name="input", type="string", description="输入参数")],
        version=version,
    )


def _entry(
    name: str,
    version: str = GENERATED_INITIAL_VERSION,
    origin: str = "generated",
    description: str = "",
) -> ToolEntry:
    return ToolEntry(
        tool_name=name,
        definition=_defn(name, description, version),
        summary=description[:80] if description else f"工具 {name}",
        tier=ToolContextTier.COLD,
        version=version,
        origin=origin,
    )


def get_weather(city: str) -> str:
    """获取指定城市的天气"""
    return f"{city} 25°C"


@pytest.fixture
async def store():
    s = ToolStore(db_path=":memory:", embedding_dim=8)
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def skill_store():
    s = SkillStore(db_path=":memory:")
    await s.initialize()
    yield s
    await s.close()


# ===================================================================
# 1. 版本号命名规则 (纯函数)
# ===================================================================

class TestVersionNumbering:

    def test_initial_version_by_origin(self):
        """外部接入 1.0.0 (正式版首版) / 系统自生成 0.0.1 (候选库)"""
        assert initial_version("external") == STABLE_INITIAL_VERSION == "1.0.0"
        assert initial_version("generated") == GENERATED_INITIAL_VERSION == "0.0.1"
        assert initial_version(None) == "0.0.1"

    def test_resolve_external_placeholder_becomes_stable(self):
        """外部工具未声明版本 (空 / 占位 0.0.1) → 1.0.0"""
        assert resolve_initial_version("", "external") == "1.0.0"
        assert resolve_initial_version(None, "external") == "1.0.0"
        assert resolve_initial_version("0.0.1", "external") == "1.0.0"

    def test_resolve_declared_version_kept(self):
        """显式声明的合法版本以声明为准"""
        assert resolve_initial_version("2.3.4", "external") == "2.3.4"
        assert resolve_initial_version("0.3.0", "generated") == "0.3.0"
        # 非法声明回落到来源初始版本
        assert resolve_initial_version("v1", "generated") == "0.0.1"
        assert resolve_initial_version("v1", "external") == "1.0.0"

    def test_stage_boundary(self):
        """0.x.y = 候选库, >=1.0.0 = 正式库"""
        assert stage_of("0.0.1") == VersionStage.CANDIDATE.value
        assert stage_of("0.9.9") == VersionStage.CANDIDATE.value
        assert stage_of("1.0.0") == VersionStage.STABLE.value
        assert stage_of("2.0.0") == VersionStage.STABLE.value
        assert stage_of("invalid") == VersionStage.CANDIDATE.value

    def test_bump_rules_for_stable(self):
        """正式版: 修订号=仅修复 / 次版本号=兼容功能 / 主版本号=不兼容"""
        assert bump_for("1.2.3", ChangeKind.FIX.value) == "1.2.4"
        assert bump_for("1.2.3", ChangeKind.FEATURE.value) == "1.3.0"
        assert bump_for("1.2.3", ChangeKind.BREAKING.value) == "2.0.0"

    def test_bump_breaking_in_candidate_stays_candidate(self):
        """候选库 (0.x) 的破坏性变更只提升次版本号, 不越过 1.0.0"""
        assert bump_for("0.0.1", ChangeKind.BREAKING.value) == "0.1.0"
        assert bump_for("0.3.1", ChangeKind.FEATURE.value) == "0.4.0"
        assert bump_for("0.3.1", ChangeKind.FIX.value) == "0.3.2"

    def test_promotion_eligibility(self):
        """晋升要求连续成功达阈值且零错误"""
        assert is_promotion_eligible(PROMOTION_MIN_SUCCESSES, 0) is True
        assert is_promotion_eligible(PROMOTION_MIN_SUCCESSES - 1, 0) is False
        assert is_promotion_eligible(PROMOTION_MIN_SUCCESSES, 1) is False

    def test_compare_versions(self):
        assert compare_versions("1.0.0", "0.9.9") == 1
        assert compare_versions("0.0.1", "0.0.1") == 0
        assert compare_versions("0.1.0", "1.0.0") == -1


# ===================================================================
# 2. ToolStore — 来源 / 阶段 / 晋升
# ===================================================================

class TestToolStoreVersionPolicy:

    @pytest.mark.asyncio
    async def test_generated_tool_starts_candidate(self, store: ToolStore):
        """自生成工具默认 0.0.1 → 候选库"""
        tool_id = await store.upsert_tool(_entry("gen_tool"))
        assert tool_id == "gen_tool@0.0.1"
        entry = await store.get_tool("gen_tool")
        assert entry is not None
        assert entry.version == "0.0.1"
        assert entry.origin == "generated"
        assert entry.stage == VersionStage.CANDIDATE.value

    @pytest.mark.asyncio
    async def test_external_tool_starts_stable(self, store: ToolStore):
        """外部接入工具默认 1.0.0 → 正式库"""
        tool_id = await store.upsert_tool(_entry("ext_tool", origin="external"))
        assert tool_id == "ext_tool@1.0.0"
        entry = await store.get_tool("ext_tool")
        assert entry is not None
        assert entry.version == "1.0.0"
        assert entry.origin == "external"
        assert entry.stage == VersionStage.STABLE.value

    @pytest.mark.asyncio
    async def test_create_version_respects_change_kind(self, store: ToolStore):
        """修订号/次版本号/主版本号按变更类型提升"""
        await store.upsert_tool(_entry("policy_tool"))

        v_fix = await store.create_version(
            "policy_tool", _defn("policy_tool"), change_kind=ChangeKind.FIX.value,
        )
        assert v_fix == "policy_tool@0.0.2"

        v_feature = await store.create_version(
            "policy_tool", _defn("policy_tool"), change_kind=ChangeKind.FEATURE.value,
        )
        assert v_feature == "policy_tool@0.1.0"

        # 候选阶段破坏性变更只进次版本号
        v_breaking = await store.create_version(
            "policy_tool", _defn("policy_tool"), change_kind=ChangeKind.BREAKING.value,
        )
        assert v_breaking == "policy_tool@0.2.0"

    @pytest.mark.asyncio
    async def test_create_version_breaking_on_stable(self, store: ToolStore):
        await store.upsert_tool(_entry("stable_tool", origin="external"))
        v_breaking = await store.create_version(
            "stable_tool", _defn("stable_tool"), change_kind=ChangeKind.BREAKING.value,
        )
        assert v_breaking == "stable_tool@2.0.0"

    @pytest.mark.asyncio
    async def test_promote_to_stable(self, store: ToolStore):
        """候选 head 晋升 1.0.0, 历史保留, 幂等"""
        await store.upsert_tool(_entry("promo_tool"))
        promoted = await store.promote_to_stable("promo_tool", evidence="测试晋升")
        assert promoted == "promo_tool@1.0.0"

        head = await store.get_head_version("promo_tool")
        assert head is not None
        assert head.version == "1.0.0"
        assert head.stage == VersionStage.STABLE.value

        # 历史保留: 1.0.0 的父版本指向原候选 head
        chain = await store.get_version_chain("promo_tool")
        stable = next(v for v in chain if v.version == "1.0.0")
        assert stable.parent_version_id == "promo_tool@0.0.1"
        assert "测试晋升" in (stable.changelog or "")

        # 幂等: 已是正式版不再晋升
        assert await store.promote_to_stable("promo_tool") is None

    @pytest.mark.asyncio
    async def test_mark_external_promotes_bound_tool(self, store: ToolStore):
        """外部技能携带的工具 → 来源 external + 1.0.0 正式版"""
        await store.upsert_tool(_entry("skill_tool"))
        await store.mark_external("skill_tool")

        head = await store.get_head_version("skill_tool")
        assert head is not None
        assert head.version == "1.0.0"
        assert head.origin == "external"
        assert head.stage == VersionStage.STABLE.value

        # 版本级属性: 历史行同样标记外部来源
        root = await store.get_tool("skill_tool", version="0.0.1")
        assert root is not None
        assert root.origin == "external"

    @pytest.mark.asyncio
    async def test_low_version_upsert_does_not_steal_head(self, store: ToolStore):
        """已晋升 1.0.0 后, 低版本 upsert 不篡夺 head"""
        await store.upsert_tool(_entry("keep_tool"))
        await store.promote_to_stable("keep_tool")

        # 重复 seed 旧版本 (例如 Vault 重启时按 0.0.1 重新导入)
        await store.upsert_tool(_entry("keep_tool"))

        head = await store.get_head_version("keep_tool")
        assert head is not None
        assert head.version == "1.0.0"

    @pytest.mark.asyncio
    async def test_list_tools_filters_and_stats(self, store: ToolStore):
        await store.upsert_tool(_entry("gen_a"))
        await store.upsert_tool(_entry("ext_b", origin="external"))

        candidates = await store.list_tools(stage=VersionStage.CANDIDATE.value)
        assert [e.tool_name for e in candidates] == ["gen_a"]

        externals = await store.list_tools(origin="external")
        assert [e.tool_name for e in externals] == ["ext_b"]

        stats = await store.stats()
        assert stats["by_origin"] == {"generated": 1, "external": 1}
        assert stats["by_stage"] == {"candidate": 1, "stable": 1}


# ===================================================================
# 3. ToolVault — 按来源定初始版本并落库
# ===================================================================

class TestVaultInitialVersion:

    @pytest.mark.asyncio
    async def test_add_tool_generated_defaults_candidate(self, store: ToolStore):
        vault = ToolVault(store=store)
        await vault.add_tool(_entry("vault_gen"))

        entry = vault.get_entry("vault_gen")
        assert entry is not None
        assert entry.version == "0.0.1"
        assert entry.stage == VersionStage.CANDIDATE.value

        persisted = await store.get_tool("vault_gen")
        assert persisted is not None
        assert persisted.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_add_tool_external_defaults_stable(self, store: ToolStore):
        vault = ToolVault(store=store)
        await vault.add_tool(_entry("vault_ext", origin="external"))

        entry = vault.get_entry("vault_ext")
        assert entry is not None
        assert entry.version == "1.0.0"
        assert entry.origin == "external"
        assert entry.stage == VersionStage.STABLE.value

        persisted = await store.get_tool("vault_ext")
        assert persisted is not None
        assert persisted.version == "1.0.0"
        assert persisted.stage == VersionStage.STABLE.value

    @pytest.mark.asyncio
    async def test_import_provider_external(self, store: ToolStore):
        """外部 Provider 导入 → 携带工具 1.0.0 并逐工具落库"""
        provider = LocalFunctionProvider(provider_id="ext", origin="external")
        provider.register_function(get_weather)

        vault = ToolVault(store=store)
        await vault.add_tools_from_provider(provider, essential_names=set())

        entry = vault.get_entry("get_weather")
        assert entry is not None
        assert entry.version == "1.0.0"
        assert entry.origin == "external"

        persisted = await store.get_tool("get_weather")
        assert persisted is not None
        assert persisted.version == "1.0.0"

    @pytest.mark.asyncio
    async def test_import_provider_generated(self, store: ToolStore):
        """自生成 Provider 导入 → 0.0.1 候选"""
        provider = LocalFunctionProvider(provider_id="local")
        provider.register_function(get_weather)

        vault = ToolVault(store=store)
        await vault.add_tools_from_provider(provider, essential_names=set())

        entry = vault.get_entry("get_weather")
        assert entry is not None
        assert entry.version == "0.0.1"
        assert entry.origin == "generated"

        persisted = await store.get_tool("get_weather")
        assert persisted is not None
        assert persisted.stage == VersionStage.CANDIDATE.value

    @pytest.mark.asyncio
    async def test_explicit_origin_overrides_provider(self, store: ToolStore):
        """显式 origin 优先于 provider.origin"""
        provider = LocalFunctionProvider(provider_id="local")
        provider.register_function(get_weather)

        vault = ToolVault(store=store)
        await vault.add_tools_from_provider(
            provider, essential_names=set(), origin="external",
        )
        entry = vault.get_entry("get_weather")
        assert entry is not None
        assert entry.version == "1.0.0"


# ===================================================================
# 4. SkillIngestor — 外部技能 1.0.0 并联动携带工具
# ===================================================================

class TestSkillVersionPolicy:

    @pytest.mark.asyncio
    async def test_external_skill_defaults_stable(
        self, skill_store: SkillStore, store: ToolStore,
    ):
        """外部技能默认 1.0.0, 携带工具联动转正式版"""
        await store.upsert_tool(_entry("send_email"))
        ingestor = SkillIngestor(skill_store, tool_store=store)

        entry = await ingestor.ingest(_SKILL_DOC)
        assert entry.version == STABLE_INITIAL_VERSION

        stored = await skill_store.get_skill("邮件汇报流程")
        assert stored is not None
        assert stored.version == "1.0.0"

        tool_head = await store.get_head_version("send_email")
        assert tool_head is not None
        assert tool_head.version == "1.0.0"
        assert tool_head.origin == "external"

    @pytest.mark.asyncio
    async def test_generated_skill_starts_candidate(
        self, skill_store: SkillStore, store: ToolStore,
    ):
        """自生成技能 0.0.1, 且不改变绑定工具来源/版本"""
        await store.upsert_tool(_entry("send_email"))
        ingestor = SkillIngestor(skill_store, tool_store=store)

        entry = await ingestor.ingest(_SKILL_DOC, origin="generated")
        assert entry.version == GENERATED_INITIAL_VERSION

        tool_head = await store.get_head_version("send_email")
        assert tool_head is not None
        assert tool_head.version == "0.0.1"
        assert tool_head.origin == "generated"

    @pytest.mark.asyncio
    async def test_declared_skill_version_kept(self, skill_store: SkillStore):
        """技能文档显式声明版本时以声明为准"""
        ingestor = SkillIngestor(skill_store)
        doc = "---\nskill: 数据清理\ndescribe: 清理脏数据\nversion: 1.2.0\n---\n步骤"
        entry = await ingestor.ingest(doc)
        assert entry.version == "1.2.0"


# ===================================================================
# 5. PostTaskPipeline — 成功晋升 / 失败清零 / 修复修订号
# ===================================================================

class TestPostTaskVersionPolicy:

    @pytest.mark.asyncio
    async def test_promotes_after_consecutive_successes(self, store: ToolStore):
        """连续成功达阈值且无错误 → 自动晋升 1.0.0"""
        await store.upsert_tool(_entry("promo_by_task"))
        pipeline = PostTaskPipeline(tool_store=store)
        exp = ToolExperience(tool_name="promo_by_task", usage_count=1, success_rate=1.0)

        for _ in range(PROMOTION_MIN_SUCCESSES - 1):
            assert await pipeline.update_tool_versions([exp]) == {}

        promoted = await pipeline.update_tool_versions([exp])
        assert promoted == {"promo_by_task": "promo_by_task@1.0.0"}

        head = await store.get_head_version("promo_by_task")
        assert head is not None
        assert head.version == "1.0.0"
        assert head.stage == VersionStage.STABLE.value

    @pytest.mark.asyncio
    async def test_failure_resets_success_window(self, store: ToolStore):
        """窗口内出现失败 → 计数清零, 需重新累计"""
        await store.upsert_tool(_entry("flaky_tool"))
        pipeline = PostTaskPipeline(tool_store=store)
        ok = ToolExperience(tool_name="flaky_tool", usage_count=1, success_rate=1.0)
        bad = ToolExperience(tool_name="flaky_tool", usage_count=2, success_rate=0.5)

        await pipeline.update_tool_versions([ok, ok])
        await pipeline.update_tool_versions([bad])

        # 失败后重新累计 2 次仍不够
        assert await pipeline.update_tool_versions([ok, ok]) == {}
        head = await store.get_head_version("flaky_tool")
        assert head is not None
        assert head.version == "0.0.1"

        # 第 3 次成功 → 晋升
        assert await pipeline.update_tool_versions([ok]) == {
            "flaky_tool": "flaky_tool@1.0.0",
        }

    @pytest.mark.asyncio
    async def test_auto_verification_blocks_promotion(self, store: ToolStore):
        """自动测试未通过 → 不晋升正式库"""
        await store.upsert_tool(_entry("verify_tool"))
        pipeline = PostTaskPipeline(
            tool_store=store,
            tool_verifier=lambda name: False,
        )
        exp = ToolExperience(
            tool_name="verify_tool",
            usage_count=PROMOTION_MIN_SUCCESSES,
            success_rate=1.0,
        )

        assert await pipeline.update_tool_versions([exp]) == {}
        head = await store.get_head_version("verify_tool")
        assert head is not None
        assert head.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_auto_verification_passes(self, store: ToolStore):
        """注入的自动测试通过 → 晋升"""
        await store.upsert_tool(_entry("verify_tool"))

        async def _verify(name: str) -> bool:
            return name == "verify_tool"

        pipeline = PostTaskPipeline(tool_store=store, tool_verifier=_verify)
        exp = ToolExperience(
            tool_name="verify_tool",
            usage_count=PROMOTION_MIN_SUCCESSES,
            success_rate=1.0,
        )
        assert await pipeline.update_tool_versions([exp]) == {
            "verify_tool": "verify_tool@1.0.0",
        }

    @pytest.mark.asyncio
    async def test_builtin_verification_requires_description(self, store: ToolStore):
        """未注入测试器时, 内置校验要求定义完整 (空描述不晋升)"""
        await store.upsert_tool(_entry("no_desc", description="   "))
        pipeline = PostTaskPipeline(tool_store=store)
        exp = ToolExperience(
            tool_name="no_desc",
            usage_count=PROMOTION_MIN_SUCCESSES,
            success_rate=1.0,
        )

        assert await pipeline.update_tool_versions([exp]) == {}
        head = await store.get_head_version("no_desc")
        assert head is not None
        assert head.version == "0.0.1"

    @pytest.mark.asyncio
    async def test_trigger_fix_submits_patch_version(self, store: ToolStore):
        """回归修复只提交修订号 (change_kind=fix)"""
        await store.upsert_tool(_entry("fix_by_task", version="1.2.3"))
        pipeline = PostTaskPipeline(tool_store=store)

        tool_id = await pipeline.trigger_tool_version_update(
            "fix_by_task", fix_description="修复参数边界",
        )
        assert tool_id == "fix_by_task@1.2.4"

        head = await store.get_head_version("fix_by_task")
        assert head is not None
        assert head.version == "1.2.4"


# ===================================================================
# 6. ToolGuardian — 回归修复闭环 (修订版本 / 重试 / 转人工)
# ===================================================================

async def _make_guardian(
    store: ToolStore,
    approval_manager: ApprovalManager | None = None,
    repo_root: str = ".",
) -> ToolGuardianAgent:
    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    await server.register_provider(provider)

    guardian = ToolGuardianAgent(
        mcp_server=server,
        tool_store=store,
        approval_manager=approval_manager,
        repo_root=repo_root,
    )
    await guardian.initialize()
    return guardian


def _report(
    issue_type: ToolIssueType = ToolIssueType.PARAMETER_BOUNDARY,
    error_message: str = "城市 '天津' 不在支持列表中",
) -> ToolIssueReport:
    return ToolIssueReport(
        reporter_agent_id="worker-001",
        tool_name="get_weather",
        issue_type=issue_type,
        error_message=error_message,
        call_arguments={"city": "天津"},
    )


class TestGuardianRegressionLoop:

    @pytest.mark.asyncio
    async def test_effective_fix_commits_patch_version(
        self, store: ToolStore, tmp_path,
    ):
        """修复产生实质变更 → 收集现场信息 + 提交修订版本"""
        await store.upsert_tool(_entry("get_weather", description="获取指定城市的天气"))
        guardian = await _make_guardian(store, repo_root=str(tmp_path))

        await guardian.receive_report(_report())
        results = await guardian.process_reports()

        assert len(results) == 1
        result = results[0]
        assert result["description_updated"] is True
        # 现场信息已收集 (版本链)
        assert result["evidence"]["current_version"] == "0.0.1"
        assert result["evidence"]["version_history"] >= 1
        # 仅修复 bug → 修订号
        assert result["fix_version"] == "get_weather@0.0.2"

        head = await store.get_head_version("get_weather")
        assert head is not None
        assert head.version == "0.0.2"
        assert head.stage == VersionStage.CANDIDATE.value

        # 历史提交写入 changelog 而非描述
        chain = await store.get_version_chain("get_weather")
        assert any("基线 v0.0.1" in (v.changelog or "") for v in chain)
        assert "基线 v0.0.1" not in (head.definition.description or "")

    @pytest.mark.asyncio
    async def test_no_change_retries_then_escalates(
        self, store: ToolStore, tmp_path,
    ):
        """自动修复无实质变更 → 重试, 达上限后转人工"""
        await store.upsert_tool(_entry("get_weather", description="获取指定城市的天气"))
        approver = ApprovalManager()
        guardian = await _make_guardian(
            store, approval_manager=approver, repo_root=str(tmp_path),
        )

        # OTHER 类型规则路径无描述修正 → 无实质变更
        for attempt in range(1, AUTO_FIX_MAX_ATTEMPTS):
            await guardian.receive_report(_report(ToolIssueType.OTHER, "未知问题"))
            results = await guardian.process_reports()
            assert results[0]["retry_scheduled"] is True
            assert results[0]["fix_attempts"] == attempt
            assert results[0]["escalated"] is False

        # 第 N 次仍无变更 → 转人工审批
        await guardian.receive_report(_report(ToolIssueType.OTHER, "未知问题"))
        results = await guardian.process_reports()
        assert results[0]["escalated"] is True
        assert results[0]["fix_attempts"] == AUTO_FIX_MAX_ATTEMPTS
        assert results[0]["retry_scheduled"] is False

        record = approver.get_record(results[0]["approval_record_id"])
        assert record is not None
        assert record.tool_name == "get_weather"
        assert record.decision == ApprovalDecision.PENDING
