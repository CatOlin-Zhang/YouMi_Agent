"""自定义记忆策略测试文件 — 用于验证动态加载"""

from __future__ import annotations

from typing import Any

from youmi.memory.strategies.base import MemoryStrategy


class OnlyUserMemoryStrategy(MemoryStrategy):
    """只存储用户消息的记忆策略 (测试用)"""

    strategy_name = "only_user"

    def __init__(self, agent_id: str, config: dict[str, Any] | None = None) -> None:
        super().__init__(agent_id, config)
        self._messages: list[dict[str, str]] = []
        self._prefix: str = (config or {}).get("prefix", "[User]")

    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        if role == "user":
            self._messages.append({"role": role, "content": f"{self._prefix} {content}"})

    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        return list(self._messages)

    async def clear(self) -> None:
        self._messages.clear()

    async def snapshot(self) -> dict[str, Any]:
        base = await super().snapshot()
        base["user_messages"] = len(self._messages)
        return base
