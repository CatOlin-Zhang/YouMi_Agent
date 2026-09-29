"""AuditGate 召回审计闸门测试 — P1

覆盖:
1. 三态判定: PASS / BLOCK(权限不满足) / MANUAL(风险超阈值)
2. MANUAL 入 ApprovalManager 待审队列
3. 审计事件: 三态均记录 tool.recall_audit (含 BLOCK reason), JSONL 落盘
4. Bridge 集成: _load_discovered_tool 加载 schema 前拦截 (BLOCK/MANUAL/PASS)
5. Bridge 集成: search_and_confirm 确认后 BLOCK 继续下一候选 / MANUAL 不加载
"""

from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import RiskLevel, ToolDefinition, ToolParameter
from youmi.mcp.approval import ApprovalDecision, ApprovalManager
from youmi.mcp.audit_gate import (
    AUDIT_EVENT_TYPE,
    AuditDecision,
    AuditGate,
    AuditResult,
)
from youmi.mcp.bridge import ToolBridge
from youmi.mcp.client import MCPClient
from youmi.mcp.context import AgentToolContext
from youmi.mcp.models import ToolEntry, ToolSearchResult
from youmi.mcp.vault import ToolContextTier, ToolVault
from youmi.observability.audit import AuditLogger


# ===================================================================
# 辅助
# ===================================================================

def _candidate(name: str = "tool_x", risk: str = RiskLevel.LOW,
               perms: list[str] | None = None) -> ToolSearchResult:
    return ToolSearchResult(
        tool_name=name, score=0.9, summary="候选摘要",
        risk_level=risk, required_permissions=perms or [],
        lineage_id=name,
    )


def _make_gate(risk_threshold: str = RiskLevel.HIGH,
               with_manager: bool = True,
               logger: AuditLogger | None = None) -> AuditGate:
    return AuditGate(
        approval_manager=ApprovalManager() if with_manager else None,
        risk_threshold=risk_threshold,
        audit_logger=logger if logger is not None else AuditLogger(path=""),
    )


# ===================================================================
# 1. 三态判定
# ===================================================================

class TestAuditGateDecisions:

    @pytest.mark.asyncio
    async def test_low_risk_passes(self):
        gate = _make_gate()
        result = await gate.check(_candidate(risk=RiskLevel.LOW), "agent-1")
        assert result.passed
        assert result.decision is AuditDecision.PASS
        assert result.record_id == ""

    @pytest.mark.asyncio
    async def test_missing_permission_blocks(self):
        gate = _make_gate()
        result = await gate.check(
            _candidate(perms=["fs:write"]), "agent-1",
            granted_permissions={"fs:read"},
        )
        assert result.blocked
        assert result.decision is AuditDecision.BLOCK
        assert "fs:write" in result.reason

    @pytest.mark.asyncio
    async def test_block_reason_lists_missing_only(self):
        """reason 只列出缺失的权限, 已授予的不列"""
        gate = _make_gate()
        result = await gate.check(
            _candidate(perms=["fs:read", "fs:write", "net:http"]), "agent-1",
            granted_permissions={"fs:read"},
        )
        assert result.blocked
        assert "fs:write" in result.reason and "net:http" in result.reason
        # fs:read 已授予, 不应出现在"缺失"列表中
        missing_part = result.reason.split("未被授予")[0]
        assert "fs:read" not in missing_part

    @pytest.mark.asyncio
    async def test_none_granted_is_unrestricted(self):
        """granted=None 表示无权限系统, 权限规则不生效"""
        gate = _make_gate()
        result = await gate.check(
            _candidate(perms=["fs:delete"]), "agent-1",
            granted_permissions=None,
        )
        assert result.passed

    @pytest.mark.asyncio
    async def test_empty_required_always_passes(self):
        """无门槛工具 (required=[]) 即便 granted 为空集也 PASS"""
        gate = _make_gate()
        result = await gate.check(
            _candidate(perms=[]), "agent-1", granted_permissions=set(),
        )
        assert result.passed

    @pytest.mark.asyncio
    async def test_critical_exceeds_default_threshold(self):
        """默认阈值 high: critical 超过 → MANUAL"""
        gate = _make_gate()
        result = await gate.check(_candidate(risk=RiskLevel.CRITICAL), "agent-1")
        assert result.manual
        assert result.decision is AuditDecision.MANUAL

    @pytest.mark.asyncio
    async def test_high_not_exceeding_default_threshold(self):
        """默认阈值 high: high 不超过 → PASS"""
        gate = _make_gate()
        result = await gate.check(_candidate(risk=RiskLevel.HIGH), "agent-1")
        assert result.passed

    @pytest.mark.asyncio
    async def test_custom_threshold(self):
        """自定义阈值 low: medium 即超阈值 → MANUAL"""
        gate = _make_gate(risk_threshold=RiskLevel.LOW)
        result = await gate.check(_candidate(risk=RiskLevel.MEDIUM), "agent-1")
        assert result.manual

    @pytest.mark.asyncio
    async def test_permission_priority_over_risk(self):
        """权限 BLOCK 优先于风险 MANUAL (权限不满足时直接拦截)"""
        gate = _make_gate()
        result = await gate.check(
            _candidate(risk=RiskLevel.CRITICAL, perms=["fs:delete"]),
            "agent-1", granted_permissions=set(),
        )
        assert result.blocked  # 而非 MANUAL


# ===================================================================
# 2. MANUAL 入待审队列
# ===================================================================

class TestManualApprovalQueue:

    @pytest.mark.asyncio
    async def test_manual_submits_request_with_risk(self):
        manager = ApprovalManager()
        gate = AuditGate(approval_manager=manager, audit_logger=None)
        result = await gate.check(_candidate(risk=RiskLevel.CRITICAL), "agent-42")

        assert result.manual
        assert result.record_id
        pending = manager.get_pending_for_agent("agent-42")
        assert len(pending) == 1
        assert pending[0].tool_name == "tool_x"
        assert pending[0].record_id == result.record_id
        assert pending[0].decision is ApprovalDecision.PENDING

    @pytest.mark.asyncio
    async def test_manual_without_manager_has_no_record(self):
        gate = _make_gate(with_manager=False)
        result = await gate.check(_candidate(risk=RiskLevel.CRITICAL), "agent-1")
        assert result.manual
        assert result.record_id == ""  # 无 manager 仍 MANUAL, 仅无审批记录

    @pytest.mark.asyncio
    async def test_pass_and_block_do_not_submit(self):
        manager = ApprovalManager()
        gate = AuditGate(approval_manager=manager, audit_logger=None)
        await gate.check(_candidate(), "agent-1")  # PASS
        await gate.check(  # BLOCK
            _candidate(perms=["fs:write"]), "agent-1",
            granted_permissions=set(),
        )
        assert manager.total_requests == 0


# ===================================================================
# 3. 审计事件 (内存 + JSONL 落盘)
# ===================================================================

class TestAuditGateLogging:

    @pytest.mark.asyncio
    async def test_all_decisions_logged(self):
        audit = AuditLogger(path="")
        gate = _make_gate(logger=audit)

        await gate.check(_candidate(), "a1")  # PASS
        await gate.check(  # BLOCK
            _candidate(name="gated", perms=["fs:write"]), "a1",
            granted_permissions=set(),
        )
        await gate.check(  # MANUAL
            _candidate(name="crit", risk=RiskLevel.CRITICAL), "a1",
        )

        events = list(audit._events)
        assert len(events) == 3
        assert all(e.event_type == AUDIT_EVENT_TYPE for e in events)
        assert [e.status for e in events] == ["ok", "blocked", "pending"]
        assert [e.agent_id for e in events] == ["a1", "a1", "a1"]
        # detail 携带锥形元数据
        assert events[0].detail["risk_level"] == RiskLevel.LOW
        assert events[1].detail["missing_permissions"] == ["fs:write"]
        assert events[2].detail["decision"] == "manual"
        assert events[2].detail["record_id"]

    @pytest.mark.asyncio
    async def test_block_event_contains_reason(self):
        audit = AuditLogger(path="")
        gate = _make_gate(logger=audit)
        await gate.check(
            _candidate(name="gated", perms=["fs:delete"]), "a1",
            granted_permissions={"fs:read"},
        )
        event = list(audit._events)[0]
        assert event.status == "blocked"
        assert "fs:delete" in event.detail["reason"]

    @pytest.mark.asyncio
    async def test_events_written_to_jsonl(self, tmp_path):
        """审计事件落 JSONL 文件 (复用 observability 通道)"""
        path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(path))
        gate = _make_gate(logger=audit)

        await gate.check(_candidate(), "a1")
        await gate.check(_candidate(risk=RiskLevel.CRITICAL), "a1")

        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["event_type"] == AUDIT_EVENT_TYPE
        assert first["status"] == "ok"
        second = json.loads(lines[1])
        assert second["status"] == "pending"
        assert second["detail"]["risk_level"] == RiskLevel.CRITICAL

    @pytest.mark.asyncio
    async def test_no_logger_is_noop(self):
        gate = AuditGate(approval_manager=None, audit_logger=None)
        result = await gate.check(_candidate(), "a1")
        assert result.passed  # 无 logger 不影响判定


# ===================================================================
# 4. Bridge 集成 — _load_discovered_tool 加载 schema 前拦截
# ===================================================================

class MockEmbeddingClient:
    """确定性字符级向量 (与 test_recall_confirm.py 一致)"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    async def embed(self, texts):
        return [self._text_to_vec(t) for t in texts]

    async def embed_one(self, text):
        return self._text_to_vec(text)

    def _text_to_vec(self, text):
        vec = [0.0] * self.dim
        for i, c in enumerate(text):
            vec[i % self.dim] += ord(c) / 100.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    async def similarity(self, query_vec, candidates):
        out = []
        for c in candidates:
            dot = sum(a * b for a, b in zip(query_vec, c))
            norm_a = math.sqrt(sum(x * x for x in query_vec))
            norm_b = math.sqrt(sum(x * x for x in c))
            out.append(dot / (norm_a * norm_b) if norm_a and norm_b else 0.0)
        return out


def _mock_client() -> MagicMock:
    client = MagicMock(spec=MCPClient)
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
    client.to_openai_tools = MagicMock(return_value=[])
    return client


def _entry(name: str, desc: str, risk: str = RiskLevel.LOW,
           perms: list[str] | None = None) -> ToolEntry:
    defn = ToolDefinition(
        name=name, description=desc,
        parameters=[ToolParameter(name="input", type="string")],
        risk_level=risk, required_permissions=perms or [],
    )
    return ToolEntry(
        tool_name=name, definition=defn,
        summary=desc[:80], tier=ToolContextTier.COLD,
    )


def _make_bridge(vault: ToolVault, gate: AuditGate | None,
                 allowed_tools: list[str] | None = None) -> ToolBridge:
    ctx = AgentToolContext(agent_id="agent-gate", vault=vault)
    ctx.init_tools(essential_names=set(), hot_names=set())
    return ToolBridge(
        agent_id="agent-gate",
        mcp_client=_mock_client(),
        vault=vault,
        context=ctx,
        allowed_tools=allowed_tools,
        audit_gate=gate,
        search_meta_tool=True,
    )


class TestBridgeLoadGateIntegration:

    @pytest.mark.asyncio
    async def test_load_blocked_by_permission(self):
        """受限 Agent load 权限不足工具 → failure + 加入排除列表"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "gated_tool", "受限工具描述", perms=["fs:write"],
        ))
        bridge = _make_bridge(vault, _make_gate(), allowed_tools=["base_tool"])

        result = await bridge._search_new_tools_impl({"load": "gated_tool"})
        assert result.is_error
        assert "审计拦截" in result.text
        # BLOCK 后排除, 后续搜索不再推荐
        assert "gated_tool" in bridge._rejected_tools
        # 未加载: 不在白名单、不 HOT
        assert "gated_tool" not in bridge.allowed_tools
        assert bridge.context.get_tier("gated_tool") != ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_load_manual_pending_approval(self):
        """load 高风险工具 → 待审批提示, schema 不加载"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "crit_tool", "高风险工具描述", risk=RiskLevel.CRITICAL,
        ))
        gate = _make_gate()
        bridge = _make_bridge(vault, gate)

        result = await bridge._search_new_tools_impl({"load": "crit_tool"})
        assert not result.is_error
        payload = json.loads(result.text)
        assert payload["pending_approval"] == "crit_tool"
        assert payload["record_id"]  # 已入待审队列
        # schema 未加载
        assert "crit_tool" not in (bridge.allowed_tools or set())
        assert bridge.context.get_tier("crit_tool") != ToolContextTier.HOT
        # 审批队列确实有待审记录
        pending = gate.approval_manager.get_pending_for_agent("agent-gate")
        assert any(r.tool_name == "crit_tool" for r in pending)

    @pytest.mark.asyncio
    async def test_load_pass_loads_schema(self):
        """低风险无门槛工具 → 正常加载"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry("safe_tool", "安全工具描述"))
        bridge = _make_bridge(vault, _make_gate())

        result = await bridge._search_new_tools_impl({"load": "safe_tool"})
        assert not result.is_error
        payload = json.loads(result.text)
        assert payload["loaded"] == "safe_tool"
        assert bridge.context.get_tier("safe_tool") == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_no_gate_keeps_zero_friction(self):
        """不注入 audit_gate (默认) → 行为与现状一致, 直通加载"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry("any_tool", "任意工具描述"))
        bridge = _make_bridge(vault, gate=None)

        result = await bridge._search_new_tools_impl({"load": "any_tool"})
        assert not result.is_error
        assert json.loads(result.text)["loaded"] == "any_tool"

    @pytest.mark.asyncio
    async def test_audit_event_precedes_schema_load(self):
        """审计事件先于 schema 加载产生 (单工具 MANUAL 场景验证顺序)"""
        audit = AuditLogger(path="")
        gate = _make_gate(logger=audit)
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "crit_tool", "高风险描述", risk=RiskLevel.CRITICAL,
        ))
        bridge = _make_bridge(vault, gate)

        await bridge._search_new_tools_impl({"load": "crit_tool"})

        # 审计事件已产生, 而 schema 并未加载 → 事件先于加载
        events = [e for e in audit._events
                  if e.event_type == AUDIT_EVENT_TYPE]
        assert len(events) == 1
        assert events[0].tool_name == "crit_tool"
        assert events[0].status == "pending"
        assert bridge.context.get_tier("crit_tool") != ToolContextTier.HOT


# ===================================================================
# 5. Bridge 集成 — search_and_confirm 确认后拦截
# ===================================================================

class _PermissionlessCone:
    """无权限边界的检索器 (模拟旧式检索器/外部数据源)

    正常链路中锥形检索的权限边界会先于 AuditGate 裁掉候选
    (纵深防御的里层); 注入本检索器模拟检索层无权限信息的
    场景，使候选能到达 gate —— 验证 gate 的外层拦截能力。
    """

    def __init__(self, vault: ToolVault):
        self._vault = vault

    async def retrieve(self, cone):
        from youmi.core.tool import risk_rank
        results = await self._vault.search(
            cone.query, top_k=cone.top_k, min_score=cone.min_score,
            exclude=cone.exclude_names,
        )
        ceiling = risk_rank(cone.risk_ceiling)
        out = []
        for r in results:
            if risk_rank(r.risk_level) > ceiling:
                continue
            lineage = r.lineage_id or r.tool_name
            if cone.exclude_lineages and lineage in cone.exclude_lineages:
                continue
            out.append(r)  # 不做权限过滤
        return out


class TestBridgeSearchConfirmGate:

    @pytest.mark.asyncio
    async def test_blocked_candidate_continues_to_next(self):
        """确认者接受的候选被 gate BLOCK → 视同否决, 闭环继续下一候选

        注入无权限边界的检索器 (纵深防御场景: 检索层未裁剪权限)，
        验证 gate 在加载 schema 前拦截。
        """
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "gated_top", "高危受限最高相似", perms=["fs:write"],
        ))
        await vault.add_tool(_entry("ok_tool", "普通可用工具"))
        # 受限 Agent: 白名单不含 fs:write → gated_top 被权限 BLOCK
        ctx = AgentToolContext(agent_id="agent-gate", vault=vault)
        ctx.init_tools(essential_names=set(), hot_names=set())
        bridge = ToolBridge(
            agent_id="agent-gate", mcp_client=_mock_client(),
            vault=vault, context=ctx, allowed_tools=["base_tool"],
            audit_gate=_make_gate(),
            cone=_PermissionlessCone(vault),
        )

        result = await bridge.search_and_confirm(
            "高危受限", max_retries=2, top_k=1, min_score=0.0,
        )
        # 第一候选被 BLOCK, 闭环继续找到可加载的 ok_tool
        assert result is not None
        assert result.tool_name == "ok_tool"
        assert ctx.get_tier("ok_tool") == ToolContextTier.HOT
        # 被 BLOCK 的候选未加载且未进入白名单
        assert "gated_top" not in bridge.allowed_tools
        assert ctx.get_tier("gated_top") != ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_all_candidates_blocked_returns_none(self):
        """所有候选都被 BLOCK → 全部入排除列表, 轮数耗尽返回 None

        同样注入无权限检索器使候选可达 gate。
        """
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "gated_a", "受限工具A描述", perms=["fs:write"],
        ))
        await vault.add_tool(_entry(
            "gated_b", "受限工具B描述", perms=["net:http"],
        ))
        ctx = AgentToolContext(agent_id="agent-gate", vault=vault)
        ctx.init_tools(essential_names=set(), hot_names=set())
        # 白名单不含任何所需权限 → 全部 BLOCK
        bridge = ToolBridge(
            agent_id="agent-gate", mcp_client=_mock_client(),
            vault=vault, context=ctx, allowed_tools=["base_tool"],
            audit_gate=_make_gate(),
            cone=_PermissionlessCone(vault),
        )

        result = await bridge.search_and_confirm(
            "受限工具", max_retries=1, top_k=5, min_score=0.0,
        )
        assert result is None
        # BLOCK 视同否决: 均进入排除列表 (无成功激活故未清空)
        assert bridge._rejected_tools == {"gated_a", "gated_b"}

    @pytest.mark.asyncio
    async def test_manual_returns_candidate_without_activation(self):
        """高风险候选确认后 MANUAL → 返回候选但不加载 (等价 activate=False)"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "crit_tool", "高风险邮件工具", risk=RiskLevel.CRITICAL,
        ))
        gate = _make_gate()
        bridge = _make_bridge(vault, gate)

        result = await bridge.search_and_confirm(
            "高风险邮件", max_retries=1, top_k=1, min_score=0.0,
        )
        assert result is not None
        assert result.tool_name == "crit_tool"
        # 候选返回但 schema 未加载
        assert bridge.context.get_tier("crit_tool") != ToolContextTier.HOT
        # 已提交待审队列
        pending = gate.approval_manager.get_pending_for_agent("agent-gate")
        assert any(r.tool_name == "crit_tool" for r in pending)

    @pytest.mark.asyncio
    async def test_pass_candidate_activates_normally(self):
        """低风险候选确认后正常加载"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry("safe_tool", "安全邮件工具"))
        bridge = _make_bridge(vault, _make_gate())

        result = await bridge.search_and_confirm(
            "安全邮件", max_retries=1, top_k=1, min_score=0.0,
        )
        assert result is not None
        assert result.tool_name == "safe_tool"
        assert bridge.context.get_tier("safe_tool") == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_set_audit_gate_runtime(self):
        """运行时注入/移除 gate"""
        vault = ToolVault(embedding_client=MockEmbeddingClient())
        await vault.add_tool(_entry(
            "crit_tool", "高风险描述", risk=RiskLevel.CRITICAL,
        ))
        bridge = _make_bridge(vault, gate=None)
        assert bridge.audit_gate is None

        gate = _make_gate()
        bridge.set_audit_gate(gate)
        assert bridge.audit_gate is gate
        result = await bridge._search_new_tools_impl({"load": "crit_tool"})
        assert json.loads(result.text).get("pending_approval") == "crit_tool"

        bridge.set_audit_gate(None)
        result = await bridge._search_new_tools_impl({"load": "crit_tool"})
        assert json.loads(result.text).get("loaded") == "crit_tool"


# ===================================================================
# 6. AuditResult 数据类
# ===================================================================

class TestAuditResultDataclass:

    def test_default_pass(self):
        r = AuditResult()
        assert r.passed and not r.blocked and not r.manual
        assert r.reason == "" and r.record_id == ""

    def test_flags_mutually_exclusive(self):
        r = AuditResult(decision=AuditDecision.BLOCK, reason="x")
        assert r.blocked and not r.passed and not r.manual
