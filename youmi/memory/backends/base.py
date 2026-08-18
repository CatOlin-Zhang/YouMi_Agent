"""
持久化后端抽象基类与数据模型

定义 session/message 的存储接口，以及用于传输的数据记录模型。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

class MessageRecord(BaseModel):
    """持久化的消息记录

    与 OpenAI messages 格式对齐，额外存储原始数据 (tool_calls 等)。
    """

    role: str
    content: str
    raw_data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    def to_openai_message(self) -> dict[str, Any]:
        """转换为 OpenAI messages 格式

        如果 raw_data 中有额外字段 (tool_calls, tool_call_id 等)，
        合并到输出中。
        """
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        # 合并 raw_data 中的额外字段
        for key in ("tool_calls", "tool_call_id", "name", "function_call"):
            if key in self.raw_data:
                msg[key] = self.raw_data[key]
        return msg

    @classmethod
    def from_openai_message(cls, msg: dict[str, Any]) -> MessageRecord:
        """从 OpenAI messages 格式构造

        Args:
            msg: OpenAI 格式消息 (含 role, content, 可能有 tool_calls 等)
        """
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""
        # 提取额外字段到 raw_data
        raw_data: dict[str, Any] = {}
        for key in ("tool_calls", "tool_call_id", "name", "function_call"):
            if key in msg:
                raw_data[key] = msg[key]
        return cls(role=role, content=content, raw_data=raw_data)


class SessionRecord(BaseModel):
    """Session 元数据"""

    session_id: str
    agent_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class PersistenceBackend(ABC):
    """Session 持久化后端抽象基类

    定义 session 和 message 的 CRUD 接口。
    子类实现具体的存储逻辑 (SQLite / JSON 文件 / Redis 等)。

    用法::

        backend = SQLiteBackend(db_path="sessions.db")
        await backend.initialize()

        # 保存
        await backend.save_session("sess_001", "agent_001", messages)

        # 加载
        messages = await backend.load_messages("sess_001")

        # 列出
        sessions = await backend.list_sessions("agent_001")
    """

    @abstractmethod
    async def initialize(self) -> None:
        """初始化后端 (建表、建立连接等)

        在 MemoryManager.initialize() 或 Agent.initialize() 时调用。
        """
        ...

    @abstractmethod
    async def save_session(
        self,
        session_id: str,
        agent_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """保存一个 session 的完整对话记录

        覆盖式写入: 先删除该 session 的旧消息，再写入新消息。

        Args:
            session_id: session 唯一 ID
            agent_id: 所属 Agent ID
            messages: OpenAI 格式的消息列表
            metadata: 可选的 session 元数据
        """
        ...

    @abstractmethod
    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        """加载指定 session 的消息列表

        Args:
            session_id: session 唯一 ID

        Returns:
            OpenAI 格式的消息列表，session 不存在时返回空列表
        """
        ...

    @abstractmethod
    async def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        """列出指定 Agent 的所有 session

        按 updated_at 降序排列 (最近的排前面)。

        Args:
            agent_id: Agent 唯一 ID

        Returns:
            SessionRecord 列表
        """
        ...

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """删除指定 session 及其所有消息

        Args:
            session_id: session 唯一 ID
        """
        ...

    @abstractmethod
    async def get_latest_session(self, agent_id: str) -> SessionRecord | None:
        """获取指定 Agent 最近的 session

        Args:
            agent_id: Agent 唯一 ID

        Returns:
            最近的 SessionRecord，无 session 时返回 None
        """
        ...

    async def close(self) -> None:
        """关闭后端 (释放连接等)。默认无操作。"""
        pass
