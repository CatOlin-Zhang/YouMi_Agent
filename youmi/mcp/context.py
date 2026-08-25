"""
AgentToolContext — Agent 侧的工具上下文状态管理

将 HOT/WARM/COLD 三级状态从 ToolVault 迁移到 Agent 侧:
- ToolVault / ToolStore 只存工具定义，不存上下文状态
- 每个 Agent 持有自己的 AgentToolContext，独立管理工具状态
- 不同 Agent 可以同时拥有不同的工具上下文视图

状态流转:
    COLD → HOT  (通过语义搜索发现后加载)
    HOT  → WARM (LRU 回收: 连续 N 轮未使用)
    WARM → HOT  (再次需要时直接加载, 跳过发现)

用法::

    from youmi.mcp.context import AgentToolContext
    from youmi.mcp.vault import ToolVault

    vault = ToolVault(...)
    ctx = AgentToolContext(agent_id="agent-001", vault=vault)

    # 初始化工具: 将必备工具设为 HOT
    ctx.init_tools(essential_names={"file_read", "file_write"})

    # 提升工具到 HOT
    await ctx.promote("search_web")

    # 记录使用
    ctx.record_usage("file_read")

    # LRU 回收
    recycled = ctx.recycle(idle_threshold=3)

    # 生成 schema
    hot_schemas = ctx.to_openai_tools()
    warm_summaries = ctx.to_warm_summaries()
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from youmi.mcp.vault import ToolContextTier

if TYPE_CHECKING:
    from youmi.mcp.vault import ToolVault, ToolEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具上下文状态条目
# ---------------------------------------------------------------------------

class _ContextEntry(BaseModel):
    """单个工具的上下文状态

    仅存储 Agent 侧的状态信息，工具定义从 Vault 获取。

    Args:
        tool_name: 工具名称
        tier: 当前上下文层级
        last_used_turn: 上次使用的对话轮次 (-1 = 从未使用)
        use_count: 总使用次数
        essential: 是否必备 (永不回收)
    """

    tool_name: str
    tier: ToolContextTier = ToolContextTier.COLD
    last_used_turn: int = -1
    use_count: int = 0
    essential: bool = False

    model_config = {"frozen": False}


# ---------------------------------------------------------------------------
# AgentToolContext 核心
# ---------------------------------------------------------------------------

class AgentToolContext:
    """Agent 侧的工具上下文状态管理

    每个 Agent 持有自己的 AgentToolContext，管理 HOT/WARM/COLD 状态。
    ToolVault / ToolStore 只存工具定义，不存上下文状态。

    Args:
        agent_id: Agent 唯一 ID
        vault: ToolVault 实例 (用于获取工具定义)
        max_hot_tools: 最大热态工具数 (超过时强制回收)
        recycle_after_turns: N 轮未使用则降级
    """

    def __init__(
        self,
        agent_id: str,
        vault: ToolVault,
        max_hot_tools: int = 20,
        recycle_after_turns: int = 3,
    ) -> None:
        self._agent_id = agent_id
        self._vault = vault
        self._max_hot_tools = max_hot_tools
        self._recycle_after_turns = recycle_after_turns
        self._contexts: dict[str, _ContextEntry] = {}
        self._current_turn: int = 0

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def vault(self) -> ToolVault:
        return self._vault

    @property
    def current_turn(self) -> int:
        return self._current_turn

    # ==================================================================
    # 初始化
    # ==================================================================

    def init_tools(
        self,
        essential_names: set[str] | None = None,
        hot_names: set[str] | None = None,
    ) -> None:
        """初始化上下文: 为 Vault 中的工具设置初始状态

        Args:
            essential_names: 必备工具名称集合 (设为 HOT, 永不回收)
            hot_names: 初始热态工具名称集合 (设为 HOT, 可回收)
                如果为 None, 则所有 Vault 中的工具都设为 HOT
        """
        essential = essential_names or set()
        hot = hot_names if hot_names is not None else set(self._vault.tool_names)

        for name in self._vault.tool_names:
            is_essential = name in essential
            is_hot = name in hot or is_essential

            self._contexts[name] = _ContextEntry(
                tool_name=name,
                tier=ToolContextTier.HOT if is_hot else ToolContextTier.COLD,
                essential=is_essential,
            )

        logger.debug(
            "AgentToolContext[%s]: initialized %d tools (%d hot, %d essential)",
            self._agent_id, len(self._contexts),
            sum(1 for c in self._contexts.values() if c.tier == ToolContextTier.HOT),
            len(essential),
        )

    def register_tool(self, tool_name: str, tier: ToolContextTier = ToolContextTier.HOT,
                      essential: bool = False) -> None:
        """注册单个工具到上下文

        Args:
            tool_name: 工具名称
            tier: 初始层级
            essential: 是否必备
        """
        self._contexts[tool_name] = _ContextEntry(
            tool_name=tool_name,
            tier=tier,
            essential=essential,
        )

    # ==================================================================
    # 状态管理
    # ==================================================================

    def get_tier(self, tool_name: str) -> ToolContextTier:
        """获取工具的当前上下文层级

        Args:
            tool_name: 工具名称

        Returns:
            ToolContextTier (不存在返回 COLD)
        """
        ctx = self._contexts.get(tool_name)
        if ctx is None:
            return ToolContextTier.COLD
        return ctx.tier

    async def promote(self, tool_name: str) -> bool:
        """将工具从 COLD/WARM 提升到 HOT

        如果超过 max_hot_tools 限制，先执行一轮回收。

        Args:
            tool_name: 工具名称

        Returns:
            是否成功提升
        """
        # 确保工具在 Vault 中存在
        entry = self._vault.get_entry(tool_name)
        if entry is None:
            return False

        ctx = self._contexts.get(tool_name)
        if ctx is None:
            # 新工具: 创建上下文条目
            ctx = _ContextEntry(tool_name=tool_name)
            self._contexts[tool_name] = ctx

        if ctx.tier == ToolContextTier.HOT:
            return True  # 已经是 HOT

        # 检查 max_hot_tools 限制
        hot_count = sum(1 for c in self._contexts.values() if c.tier == ToolContextTier.HOT)
        if hot_count >= self._max_hot_tools:
            # 先回收
            self.recycle(idle_threshold=1)

        ctx.tier = ToolContextTier.HOT
        logger.debug("AgentToolContext[%s]: promoted '%s' → HOT", self._agent_id, tool_name)
        return True

    def demote(self, tool_name: str) -> bool:
        """将工具从 HOT 降级到 WARM

        Args:
            tool_name: 工具名称

        Returns:
            是否成功降级
        """
        ctx = self._contexts.get(tool_name)
        if ctx is None or ctx.tier != ToolContextTier.HOT:
            return False
        if ctx.essential:
            return False  # 必备工具不可降级

        ctx.tier = ToolContextTier.WARM
        logger.debug("AgentToolContext[%s]: demoted '%s' → WARM", self._agent_id, tool_name)
        return True

    def get_hot_tool_names(self) -> list[str]:
        """获取所有热态工具名称"""
        return [name for name, ctx in self._contexts.items()
                if ctx.tier == ToolContextTier.HOT]

    def get_warm_tool_names(self) -> list[str]:
        """获取所有温态工具名称"""
        return [name for name, ctx in self._contexts.items()
                if ctx.tier == ToolContextTier.WARM]

    def get_cold_tool_names(self) -> list[str]:
        """获取所有冷态工具名称"""
        return [name for name, ctx in self._contexts.items()
                if ctx.tier == ToolContextTier.COLD]

    # ==================================================================
    # 使用追踪
    # ==================================================================

    def record_usage(self, tool_name: str, turn: int | None = None) -> None:
        """记录工具使用

        Args:
            tool_name: 工具名称
            turn: 使用时的对话轮次 (None = 当前轮次)
        """
        ctx = self._contexts.get(tool_name)
        if ctx is None:
            return

        actual_turn = turn if turn is not None else self._current_turn
        ctx.last_used_turn = actual_turn
        ctx.use_count += 1

    # ==================================================================
    # LRU 回收
    # ==================================================================

    def recycle(self, idle_threshold: int | None = None) -> list[str]:
        """LRU 回收: 将闲置的非必备热态工具降级为温态

        规则:
        - 必备工具 (essential=True) 永不回收
        - 从未使用过的非必备热态工具，如果当前轮次 >= idle_threshold，降级
        - 上次使用距今超过 idle_threshold 轮的热态工具，降级

        Args:
            idle_threshold: 闲置轮次阈值 (None = 使用默认值)

        Returns:
            被回收 (降级) 的工具名列表
        """
        threshold = idle_threshold if idle_threshold is not None else self._recycle_after_turns
        recycled: list[str] = []

        for name, ctx in self._contexts.items():
            if ctx.tier != ToolContextTier.HOT:
                continue
            if ctx.essential:
                continue

            if ctx.last_used_turn < 0:
                if self._current_turn >= threshold:
                    ctx.tier = ToolContextTier.WARM
                    recycled.append(name)
            else:
                idle_turns = self._current_turn - ctx.last_used_turn
                if idle_turns >= threshold:
                    ctx.tier = ToolContextTier.WARM
                    recycled.append(name)

        if recycled:
            logger.info("AgentToolContext[%s]: recycled %d tools → WARM: %s",
                         self._agent_id, len(recycled), recycled)

        return recycled

    # ==================================================================
    # 轮次管理
    # ==================================================================

    def advance_turn(self) -> int:
        """推进对话轮次计数器

        Returns:
            新的当前轮次
        """
        self._current_turn += 1
        return self._current_turn

    # ==================================================================
    # Schema 生成
    # ==================================================================

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """生成所有热态工具的 OpenAI tools schema

        从 Vault 获取工具定义，根据 AgentToolContext 的 HOT 状态过滤。
        """
        schemas: list[dict[str, Any]] = []
        for name, ctx in self._contexts.items():
            if ctx.tier == ToolContextTier.HOT:
                entry = self._vault.get_entry(name)
                if entry is not None:
                    schemas.append(entry.definition.to_openai_function_schema())
        return schemas

    def to_warm_summaries(self) -> list[dict[str, str]]:
        """生成所有温态工具的摘要列表

        格式: [{"name": "tool_name", "description": "一句话摘要"}]
        """
        summaries: list[dict[str, str]] = []
        for name, ctx in self._contexts.items():
            if ctx.tier == ToolContextTier.WARM:
                entry = self._vault.get_entry(name)
                if entry is not None:
                    summaries.append({
                        "name": name,
                        "description": entry.summary or entry.definition.description[:80],
                    })
        return summaries

    # ==================================================================
    # 诊断
    # ==================================================================

    @property
    def hot_count(self) -> int:
        return sum(1 for c in self._contexts.values() if c.tier == ToolContextTier.HOT)

    @property
    def warm_count(self) -> int:
        return sum(1 for c in self._contexts.values() if c.tier == ToolContextTier.WARM)

    @property
    def cold_count(self) -> int:
        return sum(1 for c in self._contexts.values() if c.tier == ToolContextTier.COLD)

    @property
    def total_count(self) -> int:
        return len(self._contexts)

    def reset(self) -> None:
        """重置所有上下文状态 (保留 essential 标记)"""
        for ctx in self._contexts.values():
            if ctx.essential:
                ctx.tier = ToolContextTier.HOT
            else:
                ctx.tier = ToolContextTier.COLD
                ctx.last_used_turn = -1
                ctx.use_count = 0

        self._current_turn = 0
        logger.debug("AgentToolContext[%s]: reset", self._agent_id)

    def __repr__(self) -> str:
        return (
            f"<AgentToolContext agent={self._agent_id!r} "
            f"hot={self.hot_count} warm={self.warm_count} cold={self.cold_count}>"
        )
