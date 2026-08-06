"""
记忆策略抽象基类

所有记忆管理方案 (预置 & 用户自定义) 均继承此类。
框架通过该接口与具体策略交互，策略内部可自由实现存储逻辑。

用法:
    class MyCustomStrategy(MemoryStrategy):
        strategy_name = "my_custom"

        async def on_message(self, role, content, **kwargs):
            # 自定义存储逻辑
            ...

        async def get_context(self, **kwargs):
            # 返回供 LLM 使用的上下文
            ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class MemoryStrategy(ABC):
    """记忆管理策略抽象基类

    每个策略是一个独立的记忆管理器，负责:
    - 接收对话消息并决定如何存储
    - 为 LLM 推理提供上下文
    - 管理自身的生命周期 (初始化 / 清理)

    子类必须实现:
    - strategy_name (类属性): 策略唯一标识
    - on_message(): 处理新消息
    - get_context(): 获取当前记忆上下文

    可选覆写:
    - initialize(): 策略初始化 (如建立数据库连接)
    - clear(): 清空记忆
    - snapshot(): 返回状态快照
    """

    strategy_name: str = "base"

    def __init__(self, agent_id: str, config: dict[str, Any] | None = None) -> None:
        """
        Args:
            agent_id: 所属 Agent 的唯一 ID，用于数据隔离
            config: 策略自定义配置参数
        """
        self._agent_id = agent_id
        self._config = config or {}

    @property
    def agent_id(self) -> str:
        return self._agent_id

    # ------------------------------------------------------------------
    # 必须实现
    # ------------------------------------------------------------------

    @abstractmethod
    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        """接收一条新消息，由策略决定如何存储

        Args:
            role: 消息角色 ("user" / "assistant" / "system" / "tool" / "agent")
            content: 消息内容
            **kwargs: 扩展参数 (如 metadata, task_id 等)
        """
        ...

    @abstractmethod
    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        """获取供 LLM 使用的上下文消息列表

        返回格式与 OpenAI messages 兼容:
            [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]

        Returns:
            消息列表，每条包含 role 和 content
        """
        ...

    # ------------------------------------------------------------------
    # 可选覆写
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """策略初始化 — 在 Agent.initialize() 时调用

        用于建立数据库连接、加载历史数据等。默认无操作。
        """
        pass

    async def clear(self) -> None:
        """清空所有记忆 — 默认无操作"""
        pass

    async def on_session_end(self) -> None:
        """会话结束钩子 — 在 Agent.on_stop() 时调用

        用于归档、持久化、生成摘要等。默认无操作。
        """
        pass

    async def snapshot(self) -> dict[str, Any]:
        """返回当前记忆状态快照 (调试/可观测)

        Returns:
            包含策略名称、记忆条目数等信息的字典
        """
        return {
            "strategy": self.strategy_name,
            "agent_id": self._agent_id,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} agent={self._agent_id!r}>"
