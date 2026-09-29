"""召回确认闭环测试 — P1 / Phase 4

覆盖:
1. 默认自动确认: 搜索 → 自动接受 → 加载到上下文 (白名单 + HOT + usage)
2. 自定义确认策略: 否决 → 排除 → 扩大搜索 → 接受
3. 多轮耗尽 → None («没有该功能的工具»)
4. activate=False: 仅返回候选，调用方 confirm_search_result() 后加载
5. confirm_search_result 完整加载语义 (白名单 + HOT + usage + 清空否决)
6. set_confirmer 持久策略与单次覆盖
7. confirmer 兼容性: bool 返回 / 同步回调 / 异常视为否决
8. LLMConfirmer: JSON 判定 / 低置信度否决 / 解析失败与调用异常降级
9. 已可见 (HOT) 工具不被重复推荐
10. TOOL_REQUEST 审批链路: approve_tool_request → 加载到上下文
11. GitLineageGuard: 同 lineage 一次任务只命中一次
    (激活登记/二次搜索不召回/二次 load 复用/reset_lineages)
"""

from __future__ import annotations

import json
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.bridge import ToolBridge
from youmi.mcp.client import MCPClient
from youmi.mcp.confirm import (
    ConfirmDecision,
    LLMConfirmer,
    auto_confirm,
    build_llm_confirmer,
    call_confirmer,
    _extract_json_object,
)
from youmi.mcp.context import AgentToolContext
from youmi.mcp.vault import ToolContextTier, ToolVault, ToolEntry


# ===================================================================
# 辅助
# ===================================================================

def _make_tool(name: str, description: str = "") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description or f"工具 {name} 的功能描述",
        parameters=[
            ToolParameter(name="input", type="string", description="输入参数"),
        ],
    )


def _make_entry(name: str, description: str = "") -> ToolEntry:
    return ToolEntry(
        tool_name=name,
        definition=_make_tool(name, description),
        summary=(description or f"工具 {name}")[:80],
        embedding=[],
    )


class MockEmbeddingClient:
    """确定性字符级向量 (与 test_phase4_integration 保持一致)"""

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

    async def similarity(self, query_vec, candidates):
        out = []
        for c in candidates:
            dot = sum(a * b for a, b in zip(query_vec, c))
            norm_a = math.sqrt(sum(x * x for x in query_vec))
            norm_b = math.sqrt(sum(x * x for x in c))
            out.append(dot / (norm_a * norm_b) if norm_a and norm_b else 0.0)
        return out

    async def close(self):
        pass


def _mock_client() -> MagicMock:
    client = MagicMock(spec=MCPClient)
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value=MagicMock(is_error=False, text="ok"))
    client.to_openai_tools = MagicMock(return_value=[])
    return client


async def _make_vault(*tools: tuple[str, str]) -> ToolVault:
    vault = ToolVault(embedding_client=MockEmbeddingClient())
    for name, desc in tools:
        await vault.add_tool(_make_entry(name, desc))
    return vault


def _make_bridge(vault=None, context=None, allowed_tools=None) -> ToolBridge:
    return ToolBridge(
        agent_id="agent-rc",
        mcp_client=_mock_client(),
        vault=vault,
        context=context,
        allowed_tools=allowed_tools,
    )


# ===================================================================
# 1. 默认自动确认 — 搜索 → 自动接受 → 加载
# ===================================================================

class TestAutoConfirm:

    @pytest.mark.asyncio
    async def test_auto_confirm_loads_context(self):
        """默认模式下确认最佳候选并加载到上下文"""
        vault = await _make_vault(("send_email", "发送电子邮件到指定地址"))
        ctx = AgentToolContext(agent_id="agent-rc", vault=vault)
        ctx.init_tools(essential_names=set(), hot_names=set())
        bridge = _make_bridge(vault=vault, context=ctx, allowed_tools=["essential_x"])

        result = await bridge.search_and_confirm(
            "发送邮件的工具", max_retries=1, min_score=0.0,
        )
        assert result is not None
        assert result.tool_name == "send_email"
        # 加载到上下文: 白名单 + HOT
        assert "send_email" in bridge.allowed_tools
        assert ctx.get_tier("send_email") == ToolContextTier.HOT
        # usage 已记录 (防止立即回收)
        assert ctx.to_openai_tools()  # schema 可生成

    @pytest.mark.asyncio
    async def test_no_vault_returns_none(self):
        bridge = _make_bridge()
        assert await bridge.search_and_confirm("任何查询") is None

    @pytest.mark.asyncio
    async def test_visible_tools_excluded(self):
        """已 HOT 的工具不会被重复推荐"""
        vault = await _make_vault(
            ("already_hot", "发送邮件工具"),
            ("cold_tool", "另一个待发现工具"),
        )
        ctx = AgentToolContext(agent_id="agent-rc", vault=vault)
        ctx.init_tools(essential_names={"already_hot"}, hot_names=set())
        bridge = _make_bridge(vault=vault, context=ctx)

        seen: list[str] = []

        async def confirmer(candidate, query):
            seen.append(candidate.tool_name)
            return ConfirmDecision(confirmed=True)

        result = await bridge.search_and_confirm(
            "发送邮件", confirmer=confirmer, max_retries=1, min_score=0.0,
        )
        assert result is not None
        assert "already_hot" not in seen
        assert result.tool_name == "cold_tool"


# ===================================================================
# 2. 否决 → 排除 → 扩大搜索
# ===================================================================

class TestRejectAndExpand:

    @pytest.mark.asyncio
    async def test_reject_then_second_round_accept(self):
        """第一轮候选全被否决 → 排除后第二轮找到合适工具"""
        vault = await _make_vault(
            ("bad_tool", "不相关的工具"),
            ("good_tool", "真正需要的工具"),
        )
        bridge = _make_bridge(vault=vault)

        round_seen: list[tuple[int, str]] = []
        call_count = {"n": 0}

        async def confirmer(candidate, query):
            call_count["n"] += 1
            round_seen.append((call_count["n"], candidate.tool_name))
            # 只接受 good_tool，其余否决
            return ConfirmDecision(
                confirmed=(candidate.tool_name == "good_tool"),
                reason="pick best",
            )

        result = await bridge.search_and_confirm(
            "需要的工具", confirmer=confirmer, max_retries=3,
            top_k=1, min_score=0.0,
        )
        assert result is not None
        assert result.tool_name == "good_tool"
        # 被否决的候选进入排除列表后又在确认时被清空
        assert "bad_tool" not in bridge._rejected_tools

    @pytest.mark.asyncio
    async def test_all_rejected_exhausts_rounds(self):
        """所有候选全否决 → 多轮耗尽返回 None"""
        vault = await _make_vault(
            ("tool_a", "工具A"),
            ("tool_b", "工具B"),
        )
        bridge = _make_bridge(vault=vault)
        rejected_seen: list[str] = []

        async def confirmer(candidate, query):
            rejected_seen.append(candidate.tool_name)
            return ConfirmDecision(confirmed=False, reason="nope")

        result = await bridge.search_and_confirm(
            "不存在的功能", confirmer=confirmer, max_retries=2,
            top_k=10, min_score=0.0,
        )
        assert result is None
        # 第一轮否决后进入排除列表，工具不应被再次推荐
        assert len(rejected_seen) == len(set(rejected_seen))
        assert set(rejected_seen) == {"tool_a", "tool_b"}
        assert bridge._rejected_tools == {"tool_a", "tool_b"}

    @pytest.mark.asyncio
    async def test_max_retries_floor(self):
        """max_retries=0 时至少执行一轮"""
        vault = await _make_vault(("only_tool", "唯一的工具"))
        bridge = _make_bridge(vault=vault)
        result = await bridge.search_and_confirm(
            "工具", max_retries=0, min_score=0.0,
        )
        assert result is not None and result.tool_name == "only_tool"


# ===================================================================
# 3. activate=False — 仅确认返回，调用方后置加载
# ===================================================================

class TestActivateFalse:

    @pytest.mark.asyncio
    async def test_no_activation_on_confirm(self):
        vault = await _make_vault(("gated_tool", "需要审批的工具"))
        ctx = AgentToolContext(agent_id="agent-rc", vault=vault)
        ctx.init_tools(essential_names=set(), hot_names=set())
        bridge = _make_bridge(vault=vault, context=ctx, allowed_tools=["base_tool"])

        result = await bridge.search_and_confirm(
            "审批工具", max_retries=1, min_score=0.0, activate=False,
        )
        assert result is not None
        # 仅返回候选，未加载
        assert ctx.get_tier("gated_tool") != ToolContextTier.HOT
        assert "gated_tool" not in bridge.allowed_tools

        # 调用方走完审批后手动确认加载
        promoted = await bridge.confirm_search_result(result.tool_name)
        assert promoted is True
        assert ctx.get_tier("gated_tool") == ToolContextTier.HOT
        assert "gated_tool" in bridge.allowed_tools


# ===================================================================
# 4. confirm_search_result 完整加载语义
# ===================================================================

class TestConfirmSearchResult:

    @pytest.mark.asyncio
    async def test_full_load_semantics(self):
        vault = await _make_vault(("load_me", "待加载工具"))
        ctx = AgentToolContext(agent_id="agent-rc", vault=vault)
        ctx.init_tools(essential_names=set(), hot_names=set())
        bridge = _make_bridge(vault=vault, context=ctx, allowed_tools=["base"])
        bridge.reject_search_result("old_candidate")

        promoted = await bridge.confirm_search_result("load_me")
        assert promoted is True
        assert "load_me" in bridge.allowed_tools
        assert ctx.get_tier("load_me") == ToolContextTier.HOT
        assert bridge._rejected_tools == set()  # 否决列表已清理

    @pytest.mark.asyncio
    async def test_missing_tool_not_promoted(self):
        vault = await _make_vault(("real_tool", "真实工具"))
        ctx = AgentToolContext(agent_id="agent-rc", vault=vault)
        ctx.init_tools(essential_names=set(), hot_names=set())
        bridge = _make_bridge(vault=vault, context=ctx)

        promoted = await bridge.confirm_search_result("ghost_tool")
        assert promoted is False

    @pytest.mark.asyncio
    async def test_unrestricted_bridge_no_whitelist_narrowing(self):
        """未设置白名单的 Agent 确认工具后仍保持无限制"""
        vault = await _make_vault(("any_tool", "任意工具"))
        bridge = _make_bridge(vault=vault, allowed_tools=None)
        await bridge.confirm_search_result("any_tool")
        assert bridge.allowed_tools is None  # 不回退为 {any_tool}


# ===================================================================
# 5. 可插拔策略 — set_confirmer / bool / 同步回调 / 异常
# ===================================================================

class TestConfirmerPlugin:

    @pytest.mark.asyncio
    async def test_set_confirmer_persistent(self):
        vault = await _make_vault(("tool_x", "工具X"))
        bridge = _make_bridge(vault=vault)
        calls: list[str] = []

        def persistent(candidate, query):  # 同步回调
            calls.append(candidate.tool_name)
            return True  # bool 返回

        bridge.set_confirmer(persistent)
        result = await bridge.search_and_confirm(
            "工具", max_retries=1, min_score=0.0,
        )
        assert result is not None
        assert calls == ["tool_x"]

    @pytest.mark.asyncio
    async def test_single_call_confirmer_overrides_persistent(self):
        vault = await _make_vault(("tool_y", "工具Y"))
        bridge = _make_bridge(vault=vault)
        bridge.set_confirmer(lambda c, q: False)  # 持久策略 = 全否决

        result = await bridge.search_and_confirm(
            "工具", confirmer=lambda c, q: True,  # 单次覆盖 = 全接受
            max_retries=1, min_score=0.0,
        )
        assert result is not None and result.tool_name == "tool_y"

    @pytest.mark.asyncio
    async def test_confirmer_exception_treated_as_reject(self):
        vault = await _make_vault(("tool_z", "工具Z"))
        bridge = _make_bridge(vault=vault)

        def broken(candidate, query):
            raise RuntimeError("confirmer crash")

        result = await bridge.search_and_confirm(
            "工具", confirmer=broken, max_retries=1, min_score=0.0,
        )
        assert result is None
        assert "tool_z" in bridge._rejected_tools

    @pytest.mark.asyncio
    async def test_auto_confirm_direct(self):
        decision = await auto_confirm(None, "query")
        assert decision.confirmed is True

    @pytest.mark.asyncio
    async def test_call_confirmer_bool_shorthand(self):
        decision = await call_confirmer(lambda c, q: False, None, "q")
        assert decision.confirmed is False


# ===================================================================
# 6. LLMConfirmer
# ===================================================================

class _FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.calls: list[list[dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return _FakeLLMResponse(self.reply)


class _Candidate:
    tool_name = "candidate_tool"
    score = 0.82
    summary = "候选工具摘要"


class TestLLMConfirmer:

    @pytest.mark.asyncio
    async def test_llm_accept(self):
        llm = _FakeLLM('{"suitable": true, "confidence": 0.9, "reason": "符合"}')
        confirmer = build_llm_confirmer(llm)
        decision = await confirmer(_Candidate(), "需要这个功能")
        assert decision.confirmed is True
        assert "符合" in decision.reason
        assert len(llm.calls) == 1

    @pytest.mark.asyncio
    async def test_llm_reject(self):
        llm = _FakeLLM('{"suitable": false, "confidence": 0.9, "reason": "不相关"}')
        decision = await LLMConfirmer(llm)(_Candidate(), "需要这个功能")
        assert decision.confirmed is False

    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self):
        llm = _FakeLLM('{"suitable": true, "confidence": 0.2, "reason": "不太确定"}')
        decision = await LLMConfirmer(llm, min_confidence=0.5)(
            _Candidate(), "query",
        )
        assert decision.confirmed is False

    @pytest.mark.asyncio
    async def test_markdown_wrapped_json(self):
        llm = _FakeLLM('```json\n{"suitable": true, "confidence": 0.8}\n```')
        decision = await build_llm_confirmer(llm)(_Candidate(), "query")
        assert decision.confirmed is True

    @pytest.mark.asyncio
    async def test_parse_failure_degrades_to_accept(self):
        llm = _FakeLLM("我觉得可以用")  # 无 JSON
        decision = await build_llm_confirmer(llm)(_Candidate(), "query")
        assert decision.confirmed is True  # 降级不卡死闭环
        assert "llm_confirm_error" in decision.reason

    @pytest.mark.asyncio
    async def test_llm_call_error_degrades_to_accept(self):
        llm = _FakeLLM(RuntimeError("api down"))
        decision = await build_llm_confirmer(llm)(_Candidate(), "query")
        assert decision.confirmed is True
        assert "llm_confirm_error" in decision.reason

    def test_extract_json_object(self):
        assert _extract_json_object('prefix {"suitable": true} suffix') == {
            "suitable": True,
        }
        with pytest.raises(ValueError):
            _extract_json_object("no json here")


# ===================================================================
# 7. TOOL_REQUEST 审批链路 — 批准即加载到上下文
# ===================================================================

class TestApprovalLoadsContext:

    @pytest.mark.asyncio
    async def test_approve_tool_request_loads_hot(self):
        """人工批准 → confirm_search_result → 白名单 + HOT"""
        from youmi.coordinator.master import MasterAgent
        from youmi.core.agent import AgentConfig
        from youmi.core.types import AgentMetadata

        master = MasterAgent(AgentConfig(
            name="MasterRC", metadata=AgentMetadata(role="master"),
        ))
        await master.initialize()
        try:
            sub = master.create_sub_agent(
                role="coder", task="写代码", allowed_tools=["file_read"],
            )
            await sub.initialize()

            vault = await _make_vault(("db_query", "数据库查询工具"))
            ctx = AgentToolContext(agent_id=sub.agent_id, vault=vault)
            ctx.init_tools(essential_names={"file_read"}, hot_names=set())
            bridge = _make_bridge(
                vault=vault, context=ctx, allowed_tools=["file_read"],
            )
            sub._tool_bridge = bridge
            sub._initial_allowed_tools = {"file_read"}

            ok = await master.approve_tool_request(sub.agent_id, ["db_query"])
            assert ok is True
            assert "db_query" in bridge.allowed_tools
            assert ctx.get_tier("db_query") == ToolContextTier.HOT
            # 下一轮 LLM schema 中可见
            names = {
                s["function"]["name"] for s in bridge.to_openai_tools()
            }
            assert "db_query" in names
        finally:
            await master.destroy()


# ===================================================================
# 11. GitLineageGuard — 版本链去重复用
# ===================================================================

class TestGitLineageGuard:

    @pytest.mark.asyncio
    async def test_activation_registers_lineage(self):
        """激活工具后登记 lineage (lineage_id → 已加载 tool_id)"""
        vault = await _make_vault(("tool_a", "工具A描述"))
        bridge = _make_bridge(vault=vault)

        assert bridge.active_lineages == {}
        await bridge.confirm_search_result("tool_a")
        # entry.version 默认 0.0.1 → tool_id = tool_a@0.0.1
        assert bridge.active_lineages == {"tool_a": "tool_a@0.0.1"}

    @pytest.mark.asyncio
    async def test_lineage_exclusion_prevents_recall_after_unload(self):
        """工具降级后同 lineage 仍不重复召回 (一次任务只命中一次)

        将激活工具手动降级 WARM 使其脱离 visible(HOT) 名单后，
        唯一阻止再次召回的就是 lineage 排除集。
        """
        vault = await _make_vault(("tool_a", "目标功能工具"))
        bridge = _make_bridge(vault=vault)

        # 第一次: 搜索确认并激活
        result = await bridge.search_and_confirm(
            "目标功能", max_retries=1, min_score=0.0,
        )
        assert result is not None and result.tool_name == "tool_a"

        # 模拟 LRU 回收: 降级 WARM → 不在 visible (HOT) 名单
        assert vault.unload_tool("tool_a") is True

        # 二次搜索: 无 lineage 排除则 tool_a 会被再次召回;
        # GitLineageGuard 语义 → 同 lineage 不重复召回 → None
        result2 = await bridge.search_and_confirm(
            "目标功能", max_retries=1, min_score=0.0,
        )
        assert result2 is None

    @pytest.mark.asyncio
    async def test_second_load_reuses_active_lineage(self):
        """二次 load 同 lineage 工具 → 复用提示, 不重复加载"""
        vault = await _make_vault(("tool_a", "工具A描述"))
        bridge = _make_bridge(vault=vault)

        # 第一次 load: 正常加载
        r1 = await bridge._search_new_tools_impl({"load": "tool_a"})
        payload1 = json.loads(r1.text)
        assert payload1["loaded"] == "tool_a"

        # 第二次 load 同一工具: 复用提示
        r2 = await bridge._search_new_tools_impl({"load": "tool_a"})
        payload2 = json.loads(r2.text)
        assert payload2["reused"] == "tool_a@0.0.1"
        assert "复用" in payload2["message"]

    @pytest.mark.asyncio
    async def test_search_cone_query_carries_lineage_excludes(self):
        """搜索路径的锥形查询携带已激活 lineage 排除集"""
        vault = await _make_vault(
            ("tool_a", "工具A描述"), ("tool_b", "工具B描述"),
        )
        bridge = _make_bridge(vault=vault)
        await bridge.confirm_search_result("tool_a")

        cone = bridge._get_cone()
        captured: dict = {}
        orig_retrieve = cone.retrieve

        async def spy(cone_query):
            captured["exclude_lineages"] = cone_query.exclude_lineages
            return await orig_retrieve(cone_query)

        cone.retrieve = spy  # 实例级替换
        try:
            await bridge.search_and_confirm(
                "工具", max_retries=1, min_score=0.0,
            )
        finally:
            del cone.retrieve  # 恢复类方法

        assert captured["exclude_lineages"] == {"tool_a"}

    @pytest.mark.asyncio
    async def test_reset_lineages_allows_recall_again(self):
        """reset_lineages (任务边界) 后同 lineage 可再次召回"""
        vault = await _make_vault(("tool_a", "目标功能工具"))
        bridge = _make_bridge(vault=vault)

        result = await bridge.search_and_confirm(
            "目标功能", max_retries=1, min_score=0.0,
        )
        assert result.tool_name == "tool_a"
        assert vault.unload_tool("tool_a") is True

        # 任务边界: 新 task 清空 lineage
        bridge.reset_lineages()
        assert bridge.active_lineages == {}

        # 同 lineage 工具可再次召回
        result2 = await bridge.search_and_confirm(
            "目标功能", max_retries=1, min_score=0.0,
        )
        assert result2 is not None and result2.tool_name == "tool_a"
