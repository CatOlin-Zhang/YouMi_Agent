"""
全量记忆策略 (FullMemoryStrategy)

将所有对话按 user/assistant 配对全量存储，不做任何压缩或摘要。
适用于: 对话轮次较少、需要完整上下文的场景。
"""

from __future__ import annotations

from typing import Any

from youmi.memory.strategies.base import MemoryStrategy


class FullMemoryStrategy(MemoryStrategy):
    """全量记忆管理

    存储策略:
    - 每条消息原样保留，按时间顺序存储
    - get_context() 返回全部历史消息
    - 支持 max_messages 上限，超出后 FIFO 淘汰最早消息
    """

    strategy_name = "full"

    def __init__(self, agent_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(agent_id, config)
        self._max_messages: int = self._config.get("max_messages", 200)
        self._messages: list[dict[str, str]] = []

    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        """存储每条消息，超出上限时淘汰最早的"""
        self._messages.append({"role": role, "content": content})
        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages:]

    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        """返回全部历史消息"""
        limit = kwargs.get("limit", len(self._messages))
        return list(self._messages[-limit:])

    async def clear(self) -> None:
        self._messages.clear()

    async def snapshot(self) -> dict[str, Any]:
        base = await super().snapshot()
        user_count = sum(1 for m in self._messages if m["role"] == "user")
        assistant_count = sum(1 for m in self._messages if m["role"] == "assistant")
        base.update({
            "total_messages": len(self._messages),
            "user_messages": user_count,
            "assistant_messages": assistant_count,
            "max_messages": self._max_messages,
        })
        return base
