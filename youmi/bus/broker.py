"""
消息总线 — Broker 抽象与进程内实现

提供:
- MessageBroker: 消息路由抽象基类，定义 publish / subscribe / wait_for_message 接口
- InProcessBroker: 基于 asyncio.Queue 的进程内实现，同进程多 Agent 通信

Broker 职责:
1. 为每个 Agent 维护独立的消息队列
2. 按 workflow_id 隔离消息通道
3. 支持点对点投递和广播
4. 提供阻塞等待接口 wait_for_message()
5. 可选的 at-least-once 投递语义（ACK 机制）
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from youmi.bus.message import WorkflowMessage, WorkflowMessageType

logger = logging.getLogger(__name__)

# 消息回调签名
MessageCallback = Callable[[WorkflowMessage], Awaitable[None]]


# ---------------------------------------------------------------------------
# MessageBroker 抽象基类
# ---------------------------------------------------------------------------

class MessageBroker(ABC):
    """消息路由抽象基类

    所有 Broker 实现（进程内、WebSocket 等）均继承此类，
    保证 Agent 层代码无需关心底层传输方式。
    """

    @abstractmethod
    async def subscribe(self, agent_id: str, workflow_id: str = "") -> None:
        """注册 Agent 订阅

        Args:
            agent_id: Agent 唯一标识
            workflow_id: 订阅的工作流 ID，空字符串表示订阅所有
        """
        ...

    @abstractmethod
    async def unsubscribe(self, agent_id: str) -> None:
        """取消 Agent 订阅"""
        ...

    @abstractmethod
    async def publish(self, message: WorkflowMessage) -> None:
        """发布消息到总线

        Broker 根据 to_agent_id 决定点对点投递还是广播。
        """
        ...

    @abstractmethod
    async def wait_for_message(
        self,
        agent_id: str,
        timeout: float = 30.0,
    ) -> WorkflowMessage | None:
        """阻塞等待一条消息

        Args:
            agent_id: 等待消息的 Agent ID
            timeout: 超时秒数

        Returns:
            WorkflowMessage 或 None（超时时）
        """
        ...

    @abstractmethod
    async def pending_messages(self, agent_id: str) -> list[WorkflowMessage]:
        """非阻塞获取所有待处理消息"""
        ...

    @abstractmethod
    async def ack(self, agent_id: str, message_id: str) -> None:
        """确认消息已接收"""
        ...

    @abstractmethod
    async def create_workflow(self) -> str:
        """创建新工作流，返回 workflow_id"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭 Broker，释放资源"""
        ...


# ---------------------------------------------------------------------------
# InProcessBroker — 进程内实现
# ---------------------------------------------------------------------------

class InProcessBroker(MessageBroker):
    """进程内消息 Broker — 基于 asyncio.Queue

    适用于所有 Agent 运行在同一进程内的场景。
    未来可替换为 WebSocketBroker 而不改变 Agent 层代码。

    特性:
    - 每个 Agent 独立的 asyncio.Queue
    - 按 workflow_id 隔离消息通道
    - 可选的 ACK 确认机制（at-least-once 投递）
    - 支持消息回调（观察者模式）
    """

    def __init__(self) -> None:
        # agent_id → asyncio.Queue
        self._queues: dict[str, asyncio.Queue[WorkflowMessage]] = {}
        # agent_id → workflow_id 集合
        self._subscriptions: dict[str, set[str]] = {}
        # workflow_id → agent_id 集合
        self._workflow_members: dict[str, set[str]] = {}
        # message_id → 待确认消息（ACK 追踪）
        self._pending_acks: dict[str, WorkflowMessage] = {}
        # agent_id → 回调列表
        self._callbacks: dict[str, list[MessageCallback]] = {}
        # workflow_id 计数器
        self._workflow_counter: int = 0
        self._lock = asyncio.Lock()

    async def subscribe(self, agent_id: str, workflow_id: str = "") -> None:
        """注册 Agent 订阅"""
        async with self._lock:
            if agent_id not in self._queues:
                self._queues[agent_id] = asyncio.Queue()
            if agent_id not in self._subscriptions:
                self._subscriptions[agent_id] = set()

            if workflow_id:
                self._subscriptions[agent_id].add(workflow_id)
                if workflow_id not in self._workflow_members:
                    self._workflow_members[workflow_id] = set()
                self._workflow_members[workflow_id].add(agent_id)

            logger.debug("Agent '%s' subscribed (workflow=%s)", agent_id, workflow_id or "*")

    async def unsubscribe(self, agent_id: str) -> None:
        """取消 Agent 订阅"""
        async with self._lock:
            # 从所有 workflow 中移除
            for wf_id in self._subscriptions.get(agent_id, set()):
                members = self._workflow_members.get(wf_id, set())
                members.discard(agent_id)
                if not members:
                    self._workflow_members.pop(wf_id, None)

            self._subscriptions.pop(agent_id, None)
            self._queues.pop(agent_id, None)
            self._callbacks.pop(agent_id, None)
            logger.debug("Agent '%s' unsubscribed", agent_id)

    async def publish(self, message: WorkflowMessage) -> None:
        """发布消息到总线"""
        # 自动分配 ACK ID（task 和 feedback 类型需要确认）
        if message.msg_type.writes_to_memory and not message.ack_id:
            message = message.model_copy(update={"ack_id": uuid.uuid4().hex[:8]})

        targets = self._resolve_targets(message)

        for target_id in targets:
            queue = self._queues.get(target_id)
            if queue is None:
                logger.warning(
                    "Message target '%s' not subscribed, dropping message %s",
                    target_id, message.message_id,
                )
                continue

            await queue.put(message)

            # 追踪需要 ACK 的消息
            if message.needs_ack:
                self._pending_acks[f"{message.message_id}:{target_id}"] = message

            # 触发回调
            for cb in self._callbacks.get(target_id, []):
                try:
                    await cb(message)
                except Exception:
                    logger.exception("Message callback error for agent '%s'", target_id)

            logger.debug(
                "Message delivered: %s → %s (type=%s, wf=%s)",
                message.from_agent_id, target_id, message.msg_type.value, message.workflow_id,
            )

    async def wait_for_message(
        self,
        agent_id: str,
        timeout: float = 30.0,
    ) -> WorkflowMessage | None:
        """阻塞等待一条消息"""
        queue = self._queues.get(agent_id)
        if queue is None:
            logger.warning("Agent '%s' not subscribed, cannot wait for message", agent_id)
            return None

        try:
            msg = await asyncio.wait_for(queue.get(), timeout=timeout)
            # 自动 ACK
            if msg.needs_ack:
                await self.ack(agent_id, msg.message_id)
            return msg
        except asyncio.TimeoutError:
            logger.debug("wait_for_message timeout for agent '%s' (%.1fs)", agent_id, timeout)
            return None

    async def pending_messages(self, agent_id: str) -> list[WorkflowMessage]:
        """非阻塞获取所有待处理消息"""
        queue = self._queues.get(agent_id)
        if queue is None:
            return []

        messages: list[WorkflowMessage] = []
        while not queue.empty():
            messages.append(queue.get_nowait())
        return messages

    async def ack(self, agent_id: str, message_id: str) -> None:
        """确认消息已接收"""
        key = f"{message_id}:{agent_id}"
        self._pending_acks.pop(key, None)
        logger.debug("ACK received: agent=%s message=%s", agent_id, message_id)

    async def create_workflow(self) -> str:
        """创建新工作流，返回 workflow_id"""
        async with self._lock:
            self._workflow_counter += 1
            workflow_id = f"wf-{self._workflow_counter:04d}"
            self._workflow_members[workflow_id] = set()
            return workflow_id

    async def close(self) -> None:
        """关闭 Broker，清空所有状态"""
        async with self._lock:
            self._queues.clear()
            self._subscriptions.clear()
            self._workflow_members.clear()
            self._pending_acks.clear()
            self._callbacks.clear()
            logger.info("InProcessBroker closed")

    # -----------------------------------------------------------------------
    # 扩展方法
    # -----------------------------------------------------------------------

    def on_message(self, agent_id: str, callback: MessageCallback) -> None:
        """注册消息回调（观察者模式）

        Args:
            agent_id: 目标 Agent ID
            callback: 异步回调函数 async def(msg: WorkflowMessage) -> None
        """
        if agent_id not in self._callbacks:
            self._callbacks[agent_id] = []
        self._callbacks[agent_id].append(callback)

    def get_workflow_members(self, workflow_id: str) -> set[str]:
        """获取工作流中的所有 Agent ID"""
        return set(self._workflow_members.get(workflow_id, set()))

    def get_pending_ack_count(self) -> int:
        """获取待确认消息数量"""
        return len(self._pending_acks)

    # -----------------------------------------------------------------------
    # 内部方法
    # -----------------------------------------------------------------------

    def _resolve_targets(self, message: WorkflowMessage) -> list[str]:
        """解析消息的目标 Agent 列表"""
        if message.is_broadcast:
            # 广播：同 workflow 的所有 Agent（排除发送者）
            if message.workflow_id:
                members = self._workflow_members.get(message.workflow_id, set())
                return [aid for aid in members if aid != message.from_agent_id]
            else:
                # 无 workflow_id 的广播：所有已订阅的 Agent（排除发送者）
                return [aid for aid in self._queues if aid != message.from_agent_id]
        else:
            # 点对点
            return [message.to_agent_id] if message.to_agent_id else []
