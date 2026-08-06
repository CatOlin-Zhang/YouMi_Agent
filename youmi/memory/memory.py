"""
记忆系统统一调用接口

面向 Agent 的唯一入口。Agent 通过 MemoryManager 与记忆策略交互，
无需关心底层使用的是哪种策略。

用法::

    from youmi.memory import MemoryManager

    # 方式1: 使用预置策略名称
    manager = MemoryManager(agent_id="a1", strategy="full")

    # 方式2: 使用带 LLM 的策略
    manager = MemoryManager(agent_id="a1", strategy="summary", llm_call=my_llm)

    # 方式3: 使用自定义策略文件
    manager = MemoryManager(agent_id="a1", strategy="/path/to/my_strategy.py")

    # 方式4: 直接传入策略实例
    manager = MemoryManager(agent_id="a1", strategy=FullMemoryStrategy(agent_id="a1"))

    # 统一调用
    await manager.initialize()
    await manager.on_message("user", "你好")
    context = await manager.get_context()
    await manager.on_session_end()
"""

from __future__ import annotations

from typing import Any

from youmi.memory.strategies.base import MemoryStrategy
from youmi.memory.strategies import (
    FullMemoryStrategy,
    SummaryMemoryStrategy,
    LSTMMemoryStrategy,
    create_strategy,
    list_strategies,
    register_strategy,
    LLMCallFn,
)


class MemoryManager:
    """记忆管理器 — Agent 与记忆策略之间的统一桥梁

    屏蔽策略差异，提供一致的调用接口。

    Args:
        agent_id: Agent 唯一 ID
        strategy: 记忆策略。支持以下形式:
            - str: 预置策略名称 ("full" / "summary" / "lstm")
            - str: 自定义策略 .py 文件路径
            - MemoryStrategy: 策略实例 (直接使用)
        config: 策略配置参数
        llm_call: LLM 调用函数 (summary / lstm 策略使用)
    """

    def __init__(
        self,
        agent_id: str,
        strategy: str | MemoryStrategy = "full",
        config: dict[str, Any] | None = None,
        llm_call: LLMCallFn | None = None,
    ) -> None:
        self._agent_id = agent_id

        if isinstance(strategy, MemoryStrategy):
            self._strategy = strategy
        else:
            self._strategy = create_strategy(
                strategy=strategy,
                agent_id=agent_id,
                config=config,
                llm_call=llm_call,
            )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def strategy(self) -> MemoryStrategy:
        """当前使用的记忆策略实例"""
        return self._strategy

    @property
    def strategy_name(self) -> str:
        """当前策略名称"""
        return self._strategy.strategy_name

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # ------------------------------------------------------------------
    # 生命周期 (与 Agent 生命周期对齐)
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化记忆策略 (建立连接、加载历史数据等)"""
        await self._strategy.initialize()

    async def on_session_end(self) -> None:
        """会话结束钩子 (归档、持久化、生成摘要等)"""
        await self._strategy.on_session_end()

    # ------------------------------------------------------------------
    # 核心操作
    # ------------------------------------------------------------------

    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        """记录一条消息

        Args:
            role: 消息角色 ("user" / "assistant" / "system" / "tool" / "agent")
            content: 消息内容
            **kwargs: 扩展参数
        """
        await self._strategy.on_message(role, content, **kwargs)

    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        """获取当前记忆上下文 (供 LLM 推理使用)

        Returns:
            OpenAI messages 格式的消息列表:
            [{"role": "user", "content": "..."}, ...]
        """
        return await self._strategy.get_context(**kwargs)

    async def clear(self) -> None:
        """清空记忆 (具体行为由策略决定)"""
        await self._strategy.clear()

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """获取记忆状态快照"""
        return await self._strategy.snapshot()

    def __repr__(self) -> str:
        return (
            f"<MemoryManager agent={self._agent_id!r} "
            f"strategy={self._strategy.strategy_name!r}>"
        )


__all__ = [
    "MemoryManager",
    "MemoryStrategy",
    "FullMemoryStrategy",
    "SummaryMemoryStrategy",
    "LSTMMemoryStrategy",
    "create_strategy",
    "list_strategies",
    "register_strategy",
    "LLMCallFn",
]
