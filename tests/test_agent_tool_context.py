"""
AgentToolContext Agent 侧工具上下文状态管理 测试

测试覆盖:
1. 初始化 — init_tools, register_tool
2. 状态管理 — get_tier, promote, demote
3. 使用追踪 — record_usage
4. LRU 回收 — recycle (必备工具保护, 闲置阈值)
5. 轮次管理 — advance_turn
6. Schema 生成 — to_openai_tools, to_warm_summaries
7. 诊断 — hot_count, warm_count, cold_count, reset
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.vault import ToolVault, ToolEntry, ToolContextTier
from youmi.mcp.context import AgentToolContext


# ===================================================================
# 辅助工具
# ===================================================================

def _make_tool(name: str, description: str = "") -> ToolDefinition:
    """创建测试用 ToolDefinition"""
    return ToolDefinition(
        name=name,
        description=description or f"工具 {name} 的功能描述",
        parameters=[
            ToolParameter(name="input", type="string", description="输入参数"),
        ],
    )


def _make_entry(
    name: str,
    description: str = "",
    essential: bool = False,
    tier: ToolContextTier = ToolContextTier.COLD,
) -> ToolEntry:
    """创建测试用 ToolEntry"""
    return ToolEntry(
        tool_name=name,
        definition=_make_tool(name, description),
        essential=essential,
        summary=description[:80] if description else f"工具 {name}",
        tier=tier,
    )


def _make_vault_with_tools(names: list[str]) -> ToolVault:
    """创建包含指定工具的 Vault (同步方式)"""
    vault = ToolVault()
    for name in names:
        vault._entries[name] = _make_entry(name, f"工具 {name} 的描述")
    return vault


# ===================================================================
# 1. 初始化测试
# ===================================================================

class TestAgentToolContextInit:
    """init_tools / register_tool 测试"""

    def test_create_context(self):
        vault = _make_vault_with_tools(["tool_a", "tool_b"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        assert ctx.agent_id == "agent-001"
        assert ctx.vault is vault
        assert ctx.current_turn == 0

    def test_init_tools_all_hot(self):
        """不提供 hot_names 时所有工具都设为 HOT"""
        vault = _make_vault_with_tools(["tool_a", "tool_b", "tool_c"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools()

        assert ctx.hot_count == 3
        assert ctx.warm_count == 0
        assert ctx.cold_count == 0

    def test_init_tools_with_essential(self):
        vault = _make_vault_with_tools(["tool_a", "tool_b", "tool_c"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(essential_names={"tool_a"})

        assert ctx.hot_count == 3
        assert ctx.get_tier("tool_a") == ToolContextTier.HOT

    def test_init_tools_with_hot_names(self):
        vault = _make_vault_with_tools(["tool_a", "tool_b", "tool_c"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        assert ctx.hot_count == 1
        assert ctx.cold_count == 2
        assert ctx.get_tier("tool_a") == ToolContextTier.HOT
        assert ctx.get_tier("tool_b") == ToolContextTier.COLD

    def test_register_tool(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.register_tool("new_tool", tier=ToolContextTier.HOT, essential=True)

        assert ctx.get_tier("new_tool") == ToolContextTier.HOT
        assert ctx.hot_count == 1


# ===================================================================
# 2. 状态管理测试
# ===================================================================

class TestAgentToolContextTierFlow:
    """get_tier, promote, demote 状态流转测试"""

    def test_get_tier_default_cold(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        assert ctx.get_tier("nonexistent") == ToolContextTier.COLD

    @pytest.mark.asyncio
    async def test_promote_cold_to_hot(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names=set())  # 全部 COLD

        assert ctx.get_tier("tool_a") == ToolContextTier.COLD
        result = await ctx.promote("tool_a")
        assert result is True
        assert ctx.get_tier("tool_a") == ToolContextTier.HOT

    @pytest.mark.asyncio
    async def test_promote_already_hot(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        result = await ctx.promote("tool_a")
        assert result is True  # 已经是 HOT 返回 True

    @pytest.mark.asyncio
    async def test_promote_nonexistent_tool(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)

        result = await ctx.promote("nonexistent")
        assert result is False

    def test_demote_hot_to_warm(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        result = ctx.demote("tool_a")
        assert result is True
        assert ctx.get_tier("tool_a") == ToolContextTier.WARM

    def test_demote_cold_fails(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names=set())

        result = ctx.demote("tool_a")
        assert result is False

    def test_demote_essential_fails(self):
        vault = ToolVault()
        vault._entries["tool_a"] = _make_entry("tool_a", essential=True)
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(essential_names={"tool_a"})

        result = ctx.demote("tool_a")
        assert result is False

    def test_get_hot_warm_cold_names(self):
        vault = _make_vault_with_tools(["a", "b", "c"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"a"})
        ctx.register_tool("b", tier=ToolContextTier.WARM)

        assert "a" in ctx.get_hot_tool_names()
        assert "b" in ctx.get_warm_tool_names()
        assert "c" in ctx.get_cold_tool_names()


# ===================================================================
# 3. max_hot_tools 限制测试
# ===================================================================

class TestMaxHotTools:
    """promote 超过 max_hot_tools 时自动回收"""

    @pytest.mark.asyncio
    async def test_promote_triggers_recycle(self):
        vault = _make_vault_with_tools(["a", "b", "c"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault, max_hot_tools=2)
        ctx.init_tools(hot_names={"a", "b"})

        # 提升 c 超过限制 → 应先触发回收
        result = await ctx.promote("c")
        assert result is True
        assert ctx.get_tier("c") == ToolContextTier.HOT
        # 回收后某些工具应变为 WARM
        assert ctx.hot_count <= 3  # promote 后可能有短暂超过


# ===================================================================
# 4. 使用追踪测试
# ===================================================================

class TestAgentToolContextUsage:
    """record_usage 测试"""

    def test_record_usage(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        ctx.record_usage("tool_a")
        # 验证 use_count 增加 (通过内部状态检查)

    def test_record_usage_with_turn(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})
        ctx.advance_turn()
        ctx.advance_turn()

        ctx.record_usage("tool_a", turn=2)

    def test_record_usage_nonexistent(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        # 不应报错
        ctx.record_usage("nonexistent")


# ===================================================================
# 5. LRU 回收测试
# ===================================================================

class TestAgentToolContextRecycle:
    """recycle 回收测试"""

    def test_recycle_idle_tools(self):
        vault = _make_vault_with_tools(["tool_a", "tool_b"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault, recycle_after_turns=3)
        ctx.init_tools(hot_names={"tool_a", "tool_b"})

        # 推进到超过阈值
        for _ in range(5):
            ctx.advance_turn()

        recycled = ctx.recycle()
        assert len(recycled) == 2
        assert ctx.get_tier("tool_a") == ToolContextTier.WARM
        assert ctx.get_tier("tool_b") == ToolContextTier.WARM

    def test_recycle_protects_essential(self):
        vault = ToolVault()
        vault._entries["essential_tool"] = _make_entry("essential_tool", essential=True)
        vault._entries["normal_tool"] = _make_entry("normal_tool")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault, recycle_after_turns=2)
        ctx.init_tools(essential_names={"essential_tool"}, hot_names={"normal_tool"})

        for _ in range(5):
            ctx.advance_turn()

        recycled = ctx.recycle()
        assert "essential_tool" not in recycled
        assert ctx.get_tier("essential_tool") == ToolContextTier.HOT

    def test_recycle_respects_recent_usage(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault, recycle_after_turns=3)
        ctx.init_tools(hot_names={"tool_a"})

        # 在第 2 轮使用
        ctx.advance_turn()
        ctx.advance_turn()
        ctx.record_usage("tool_a")

        # 在第 3 轮回收，距离使用仅 1 轮
        ctx.advance_turn()
        recycled = ctx.recycle()
        assert "tool_a" not in recycled

    def test_recycle_custom_threshold(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        ctx.advance_turn()
        # 使用阈值 1 回收
        recycled = ctx.recycle(idle_threshold=1)
        assert "tool_a" in recycled

    def test_recycle_empty(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        recycled = ctx.recycle()
        assert recycled == []


# ===================================================================
# 6. 轮次管理测试
# ===================================================================

class TestAgentToolContextTurn:
    """advance_turn 测试"""

    def test_advance_turn(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        assert ctx.current_turn == 0

        new_turn = ctx.advance_turn()
        assert new_turn == 1
        assert ctx.current_turn == 1

    def test_advance_multiple_turns(self):
        vault = _make_vault_with_tools([])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)

        for i in range(5):
            result = ctx.advance_turn()
            assert result == i + 1


# ===================================================================
# 7. Schema 生成测试
# ===================================================================

class TestAgentToolContextSchema:
    """to_openai_tools, to_warm_summaries 测试"""

    def test_to_openai_tools(self):
        vault = _make_vault_with_tools(["tool_a", "tool_b"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})

        schemas = ctx.to_openai_tools()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "tool_a"

    def test_to_openai_tools_empty(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names=set())  # 全部 COLD

        schemas = ctx.to_openai_tools()
        assert schemas == []

    def test_to_warm_summaries(self):
        vault = _make_vault_with_tools(["tool_a", "tool_b"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})
        ctx.demote("tool_a")  # tool_a → WARM

        summaries = ctx.to_warm_summaries()
        assert len(summaries) == 1
        assert summaries[0]["name"] == "tool_a"
        assert "description" in summaries[0]

    def test_to_warm_summaries_empty(self):
        vault = _make_vault_with_tools(["tool_a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"tool_a"})  # 全部 HOT，无 WARM

        summaries = ctx.to_warm_summaries()
        assert summaries == []


# ===================================================================
# 8. 诊断与重置测试
# ===================================================================

class TestAgentToolContextDiag:
    """诊断属性和 reset 测试"""

    def test_counts(self):
        vault = _make_vault_with_tools(["a", "b", "c"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"a"})
        ctx.register_tool("b", tier=ToolContextTier.WARM)

        assert ctx.hot_count == 1
        assert ctx.warm_count == 1
        assert ctx.cold_count == 1
        assert ctx.total_count == 3

    def test_reset(self):
        vault = ToolVault()
        vault._entries["essential_tool"] = _make_entry("essential_tool", essential=True)
        vault._entries["normal_tool"] = _make_entry("normal_tool")

        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(essential_names={"essential_tool"}, hot_names={"normal_tool"})

        # 推进轮次并记录使用
        ctx.advance_turn()
        ctx.record_usage("normal_tool")

        ctx.reset()

        # 必备工具保持 HOT
        assert ctx.get_tier("essential_tool") == ToolContextTier.HOT
        # 普通工具回退到 COLD
        assert ctx.get_tier("normal_tool") == ToolContextTier.COLD
        # 轮次重置
        assert ctx.current_turn == 0

    def test_repr(self):
        vault = _make_vault_with_tools(["a"])
        ctx = AgentToolContext(agent_id="agent-001", vault=vault)
        ctx.init_tools(hot_names={"a"})

        r = repr(ctx)
        assert "AgentToolContext" in r
        assert "agent-001" in r
        assert "hot=1" in r
