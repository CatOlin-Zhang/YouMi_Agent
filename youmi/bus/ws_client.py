"""
消息总线 — WebSocket 客户端

BusClient 实现 WebSocket 客户端，让 Agent 通过 WebSocket 连接到 BusServer:
- 自动连接与断线重连
- 发送消息（publish）
- 接收消息（wait_for_message / pending_messages）
- 心跳保活
- 实现 MessageBroker 接口，可直接替代 InProcessBroker

架构:
    Agent ──▶ BusClient ──WebSocket──▶ BusServer ──▶ InProcessBroker ──▶ 其他 Agent
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection, connect as ws_connect

from youmi.bus.broker import MessageBroker
from youmi.bus.message import BusEnvelope, WorkflowMessage, WorkflowMessageType

logger = logging.getLogger(__name__)


class BusClient(MessageBroker):
    """WebSocket 消息总线客户端

    通过 WebSocket 连接到 BusServer，实现 MessageBroker 接口。
    Agent 可以无缝从 InProcessBroker 切换到 BusClient。

    用法::

        client = BusClient(agent_id="agent-1", url="ws://localhost:8765")
        await client.connect(workflow_id="wf-0001")
        await client.publish(msg)
        response = await client.wait_for_message("agent-1", timeout=10.0)
        await client.disconnect()
    """

    def __init__(
        self,
        agent_id: str,
        url: str = "ws://localhost:8765",
        reconnect_interval: float = 3.0,
        max_reconnect_attempts: int = 5,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self._agent_id = agent_id
        self._url = url
        self._reconnect_interval = reconnect_interval
        self._max_reconnect_attempts = max_reconnect_attempts
        self._heartbeat_interval = heartbeat_interval

        self._ws: ClientConnection | None = None
        self._connected = False
        self._workflow_id: str = ""

        # 本地接收队列（从 WebSocket 收到的消息暂存）
        self._recv_queue: asyncio.Queue[WorkflowMessage] = asyncio.Queue()
        # 后台任务
        self._recv_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self, workflow_id: str = "") -> None:
        """连接到 BusServer 并订阅

        Args:
            workflow_id: 要加入的工作流 ID
        """
        self._workflow_id = workflow_id

        for attempt in range(1, self._max_reconnect_attempts + 1):
            try:
                self._ws = await ws_connect(self._url)
                self._connected = True

                # 发送订阅信封
                envelope = BusEnvelope.subscribe(self._agent_id, workflow_id)
                await self._ws.send(envelope.model_dump_json())

                # 等待订阅确认
                raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                resp = json.loads(raw)
                if resp.get("envelope_type") == "subscribed":
                    logger.info(
                        "BusClient '%s' connected to %s (workflow=%s)",
                        self._agent_id, self._url, workflow_id or "*",
                    )
                else:
                    logger.warning("Unexpected subscribe response: %s", resp)

                # 启动接收循环和心跳
                self._recv_task = asyncio.create_task(self._recv_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                return

            except Exception as e:
                logger.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt, self._max_reconnect_attempts, e,
                )
                if attempt < self._max_reconnect_attempts:
                    await asyncio.sleep(self._reconnect_interval)

        raise ConnectionError(
            f"Failed to connect to {self._url} after {self._max_reconnect_attempts} attempts"
        )

    async def disconnect(self) -> None:
        """断开连接"""
        self._connected = False

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("BusClient '%s' disconnected", self._agent_id)

    # -----------------------------------------------------------------------
    # MessageBroker 接口实现
    # -----------------------------------------------------------------------

    async def subscribe(self, agent_id: str, workflow_id: str = "") -> None:
        """追加订阅工作流"""
        if not self.is_connected:
            raise ConnectionError("Not connected to BusServer")
        envelope = BusEnvelope(
            envelope_type="subscribe",
            agent_id=self._agent_id,
            payload={"workflow_id": workflow_id},
        )
        await self._ws.send(envelope.model_dump_json())

    async def unsubscribe(self, agent_id: str) -> None:
        """取消订阅（等同于断开连接）"""
        await self.disconnect()

    async def publish(self, message: WorkflowMessage) -> None:
        """通过 WebSocket 发布消息"""
        if not self.is_connected:
            raise ConnectionError("Not connected to BusServer")
        envelope = BusEnvelope.wrap_message(message, self._agent_id)
        await self._ws.send(envelope.model_dump_json())

    async def wait_for_message(
        self,
        agent_id: str,
        timeout: float = 30.0,
    ) -> WorkflowMessage | None:
        """阻塞等待一条消息"""
        try:
            return await asyncio.wait_for(self._recv_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def pending_messages(self, agent_id: str) -> list[WorkflowMessage]:
        """非阻塞获取所有待处理消息"""
        messages: list[WorkflowMessage] = []
        while not self._recv_queue.empty():
            messages.append(self._recv_queue.get_nowait())
        return messages

    async def ack(self, agent_id: str, message_id: str) -> None:
        """发送消息确认"""
        if not self.is_connected:
            return
        envelope = BusEnvelope.ack(message_id, self._agent_id)
        await self._ws.send(envelope.model_dump_json())

    async def create_workflow(self) -> str:
        """创建新工作流（通过 Server 端分配）

        注意: 客户端不直接创建 workflow_id，此处生成一个基于 agent_id 的临时 ID。
        正式场景中应由 MasterAgent 或 Server 端分配。
        """
        import uuid
        return f"wf-{uuid.uuid4().hex[:8]}"

    async def close(self) -> None:
        """关闭客户端"""
        await self.disconnect()

    # -----------------------------------------------------------------------
    # 内部方法
    # -----------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """持续接收 WebSocket 消息并放入本地队列"""
        try:
            while self._connected and self._ws:
                try:
                    raw = await self._ws.recv()
                    envelope = BusEnvelope(**json.loads(raw))

                    if envelope.envelope_type == "message":
                        msg = envelope.unwrap_message()
                        if msg:
                            await self._recv_queue.put(msg)
                            # 自动 ACK
                            if msg.needs_ack:
                                await self.ack(self._agent_id, msg.message_id)

                    elif envelope.envelope_type == "heartbeat_ack":
                        pass  # 心跳回复，无需处理

                    elif envelope.envelope_type == "error":
                        logger.warning("Server error: %s", envelope.payload)

                except websockets.ConnectionClosed:
                    logger.warning("Connection closed, attempting reconnect...")
                    self._connected = False
                    await self._reconnect()
                    break
                except json.JSONDecodeError as e:
                    logger.warning("Invalid JSON from server: %s", e)

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Recv loop error for agent '%s'", self._agent_id)

    async def _heartbeat_loop(self) -> None:
        """定期发送心跳"""
        try:
            while self._connected:
                if self.is_connected:
                    try:
                        envelope = BusEnvelope.heartbeat(self._agent_id)
                        await self._ws.send(envelope.model_dump_json())
                    except websockets.ConnectionClosed:
                        break
                await asyncio.sleep(self._heartbeat_interval)
        except asyncio.CancelledError:
            pass

    async def _reconnect(self) -> None:
        """断线重连"""
        for attempt in range(1, self._max_reconnect_attempts + 1):
            try:
                await asyncio.sleep(self._reconnect_interval)
                self._ws = await ws_connect(self._url)
                self._connected = True

                # 重新订阅
                envelope = BusEnvelope.subscribe(self._agent_id, self._workflow_id)
                await self._ws.send(envelope.model_dump_json())

                # 等待确认
                raw = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                resp = json.loads(raw)
                if resp.get("envelope_type") == "subscribed":
                    logger.info("BusClient '%s' reconnected", self._agent_id)
                    # 重新启动接收循环
                    self._recv_task = asyncio.create_task(self._recv_loop())
                    return
            except Exception as e:
                logger.warning(
                    "Reconnect attempt %d/%d failed: %s",
                    attempt, self._max_reconnect_attempts, e,
                )

        logger.error("BusClient '%s' failed to reconnect", self._agent_id)
