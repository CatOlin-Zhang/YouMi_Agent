"""
消息总线 — 消息模型

定义工作流消息协议:
- WorkflowMessageType: 消息类型枚举 (task / feedback / status / query)
- WorkflowMessage: 工作流消息模型，扩展 AgentMessage，增加 workflow_id 和 msg_type
- BusEnvelope: 传输层信封，用于 WebSocket 序列化传输

设计约定:
- 所有消息均携带 workflow_id，Broker 按 workflow 隔离路由
- 消息类型决定写入策略：task/feedback 写入 Agent 记忆；status/query 仅入队
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from youmi.core.types import AgentMessage, MessageRole


# ---------------------------------------------------------------------------
# 消息类型枚举
# ---------------------------------------------------------------------------

class WorkflowMessageType(str, Enum):
    """工作流消息类型

    - task: 任务分配（MasterAgent → SubAgent）
    - feedback: 结果反馈（SubAgent → MasterAgent）
    - status: 状态更新（广播）
    - query: 跨 Agent 询问（点对点）
    """

    TASK = "task"
    FEEDBACK = "feedback"
    STATUS = "status"
    QUERY = "query"

    @property
    def writes_to_memory(self) -> bool:
        """是否写入 Agent 记忆系统

        task 和 feedback 写入记忆；status 和 query 仅入队不写记忆。
        """
        return self in (WorkflowMessageType.TASK, WorkflowMessageType.FEEDBACK)


# ---------------------------------------------------------------------------
# 工作流消息
# ---------------------------------------------------------------------------

class WorkflowMessage(BaseModel):
    """工作流消息 — Agent 间通信的核心数据模型

    相比 AgentMessage，增加了:
    - workflow_id: 工作流标识，Broker 据此隔离消息通道
    - msg_type: 消息类型，决定路由策略和记忆写入行为
    - ack_id: 确认标识，用于 at-least-once 投递语义
    """

    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str = Field(default="", description="工作流标识")
    from_agent_id: str = ""
    to_agent_id: str | None = Field(default=None, description="目标 Agent ID，None 表示广播")
    msg_type: WorkflowMessageType = WorkflowMessageType.STATUS
    role: MessageRole = MessageRole.AGENT
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ack_id: str = Field(default="", description="ACK 标识，空字符串表示不需要确认")

    @property
    def is_broadcast(self) -> bool:
        return self.to_agent_id is None or self.to_agent_id == "*"

    @property
    def needs_ack(self) -> bool:
        return bool(self.ack_id)

    def to_agent_message(self) -> AgentMessage:
        """转换为 Agent 基类可接收的 AgentMessage"""
        return AgentMessage(
            message_id=self.message_id,
            from_agent_id=self.from_agent_id,
            to_agent_id=self.to_agent_id,
            role=self.role,
            content=self.content,
            metadata={**self.metadata, "workflow_id": self.workflow_id, "msg_type": self.msg_type.value},
            timestamp=self.timestamp,
        )

    @classmethod
    def from_agent_message(
        cls,
        msg: AgentMessage,
        workflow_id: str = "",
        msg_type: WorkflowMessageType = WorkflowMessageType.STATUS,
    ) -> WorkflowMessage:
        """从 AgentMessage 构造 WorkflowMessage"""
        return cls(
            message_id=msg.message_id,
            workflow_id=workflow_id,
            from_agent_id=msg.from_agent_id,
            to_agent_id=msg.to_agent_id,
            msg_type=msg_type,
            role=msg.role,
            content=msg.content,
            metadata=msg.metadata,
            timestamp=msg.timestamp,
        )


# ---------------------------------------------------------------------------
# 传输层信封 — 用于 WebSocket 序列化
# ---------------------------------------------------------------------------

class BusEnvelope(BaseModel):
    """WebSocket 传输信封

    所有 WebSocket 通信均包装为 BusEnvelope，统一 JSON 序列化格式。

    信封类型:
    - message: 携带 WorkflowMessage 的消息投递
    - subscribe: Agent 注册订阅
    - unsubscribe: Agent 取消订阅
    - ack: 消息确认回执
    - heartbeat: 心跳保活
    """

    envelope_type: str = "message"  # message | subscribe | unsubscribe | ack | heartbeat
    agent_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def wrap_message(cls, msg: WorkflowMessage, agent_id: str = "") -> BusEnvelope:
        """包装一条 WorkflowMessage"""
        return cls(
            envelope_type="message",
            agent_id=agent_id,
            payload=msg.model_dump(mode="json"),
        )

    @classmethod
    def subscribe(cls, agent_id: str, workflow_id: str = "") -> BusEnvelope:
        """构造订阅信封"""
        return cls(
            envelope_type="subscribe",
            agent_id=agent_id,
            payload={"workflow_id": workflow_id},
        )

    @classmethod
    def ack(cls, message_id: str, agent_id: str) -> BusEnvelope:
        """构造 ACK 回执信封"""
        return cls(
            envelope_type="ack",
            agent_id=agent_id,
            payload={"message_id": message_id},
        )

    @classmethod
    def heartbeat(cls, agent_id: str) -> BusEnvelope:
        """构造心跳信封"""
        return cls(
            envelope_type="heartbeat",
            agent_id=agent_id,
        )

    def unwrap_message(self) -> WorkflowMessage | None:
        """从信封中解包 WorkflowMessage"""
        if self.envelope_type != "message":
            return None
        return WorkflowMessage(**self.payload)
