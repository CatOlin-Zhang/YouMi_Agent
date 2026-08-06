"""
消息总线 — WebSocket 服务端

BusServer 基于 websockets 库实现 WebSocket 服务端:
- 管理所有 Agent 客户端的 WebSocket 连接
- 将 WebSocket 消息解包为 WorkflowMessage 并路由到 InProcessBroker
- 支持 Agent 动态加入/离开工作流
- 心跳检测与断线清理

架构:
    Agent (ws_client) ──WebSocket──▶ BusServer ──▶ InProcessBroker ──▶ 目标 Agent
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
import websockets.server
from websockets.asyncio.server import Server as WsServer, ServerConnection

from youmi.bus.broker import InProcessBroker, MessageBroker
from youmi.bus.message import BusEnvelope, WorkflowMessage

logger = logging.getLogger(__name__)


class BusServer:
    """WebSocket 消息总线服务端

    将 InProcessBroker 包装为 WebSocket 服务，允许远程 Agent 通过
    WebSocket 协议加入消息总线。

    用法::

        broker = InProcessBroker()
        server = BusServer(broker)
        await server.start(host="localhost", port=8765)
        # ... Agent 通过 WebSocket 连接 ...
        await server.stop()
    """

    def __init__(self, broker: MessageBroker | None = None) -> None:
        self._broker = broker or InProcessBroker()
        self._ws_server: WsServer | None = None
        # agent_id → WebSocket connection
        self._connections: dict[str, ServerConnection] = {}
        # agent_id → last heartbeat timestamp
        self._heartbeats: dict[str, float] = {}
        self._running = False

    @property
    def broker(self) -> MessageBroker:
        """底层 MessageBroker"""
        return self._broker

    @property
    def connected_agents(self) -> list[str]:
        """当前已连接的 Agent ID 列表"""
        return list(self._connections.keys())

    async def start(
        self,
        host: str = "localhost",
        port: int = 8765,
        **kwargs: Any,
    ) -> None:
        """启动 WebSocket 服务

        Args:
            host: 监听地址
            port: 监听端口
            **kwargs: 传给 websockets.serve 的额外参数
        """
        self._running = True
        self._ws_server = await websockets.serve(
            self._handle_connection,
            host,
            port,
            **kwargs,
        )
        logger.info("BusServer started on ws://%s:%d", host, port)

    async def stop(self) -> None:
        """停止服务，关闭所有连接"""
        self._running = False

        # 关闭所有 Agent 连接
        for agent_id, conn in list(self._connections.items()):
            await conn.close()
            await self._broker.unsubscribe(agent_id)
            logger.info("Disconnected agent: %s", agent_id)

        self._connections.clear()
        self._heartbeats.clear()

        # 关闭 WebSocket 服务
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None

        await self._broker.close()
        logger.info("BusServer stopped")

    async def broadcast_envelope(self, envelope: BusEnvelope) -> None:
        """向所有连接的 Agent 广播信封"""
        data = envelope.model_dump_json()
        disconnected = []
        for agent_id, conn in self._connections.items():
            try:
                await conn.send(data)
            except websockets.ConnectionClosed:
                disconnected.append(agent_id)

        for agent_id in disconnected:
            await self._cleanup_agent(agent_id)

    # -----------------------------------------------------------------------
    # 内部方法
    # -----------------------------------------------------------------------

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        """处理新的 WebSocket 连接"""
        agent_id: str | None = None

        try:
            async for raw_message in websocket:
                try:
                    envelope = BusEnvelope(**json.loads(raw_message))
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning("Invalid envelope from %s: %s", websocket.remote_address, e)
                    await websocket.send(json.dumps({
                        "envelope_type": "error",
                        "payload": {"error": f"Invalid envelope: {e}"},
                    }))
                    continue

                # 首次消息必须是 subscribe
                if agent_id is None:
                    if envelope.envelope_type != "subscribe":
                        await websocket.send(json.dumps({
                            "envelope_type": "error",
                            "payload": {"error": "First message must be 'subscribe'"},
                        }))
                        continue

                    agent_id = envelope.agent_id
                    workflow_id = envelope.payload.get("workflow_id", "")

                    # 注册到 Broker
                    await self._broker.subscribe(agent_id, workflow_id)
                    self._connections[agent_id] = websocket
                    self._heartbeats[agent_id] = asyncio.get_event_loop().time()

                    # 启动监听 Broker → WebSocket 推送的任务
                    push_task = asyncio.create_task(
                        self._push_loop(agent_id, websocket)
                    )

                    # 确认订阅
                    await websocket.send(json.dumps({
                        "envelope_type": "subscribed",
                        "agent_id": agent_id,
                        "payload": {"workflow_id": workflow_id},
                    }))
                    logger.info("Agent '%s' connected (workflow=%s)", agent_id, workflow_id or "*")
                    continue

                # 处理后续消息
                await self._handle_envelope(agent_id, envelope, websocket)

        except websockets.ConnectionClosed:
            logger.info("Agent '%s' connection closed", agent_id)
        except Exception:
            logger.exception("Error handling connection for agent '%s'", agent_id)
        finally:
            if agent_id:
                push_task.cancel()
                await self._cleanup_agent(agent_id)

    async def _handle_envelope(
        self,
        agent_id: str,
        envelope: BusEnvelope,
        websocket: ServerConnection,
    ) -> None:
        """处理收到的信封"""
        self._heartbeats[agent_id] = asyncio.get_event_loop().time()

        if envelope.envelope_type == "message":
            # 解包并发布到 Broker
            msg = envelope.unwrap_message()
            if msg:
                await self._broker.publish(msg)

        elif envelope.envelope_type == "ack":
            message_id = envelope.payload.get("message_id", "")
            if message_id:
                await self._broker.ack(agent_id, message_id)

        elif envelope.envelope_type == "heartbeat":
            # 回复心跳
            await websocket.send(json.dumps({
                "envelope_type": "heartbeat_ack",
                "agent_id": agent_id,
            }))

        elif envelope.envelope_type == "subscribe":
            # 追加订阅其他 workflow
            workflow_id = envelope.payload.get("workflow_id", "")
            if workflow_id:
                await self._broker.subscribe(agent_id, workflow_id)
                await websocket.send(json.dumps({
                    "envelope_type": "subscribed",
                    "agent_id": agent_id,
                    "payload": {"workflow_id": workflow_id},
                }))

        else:
            logger.warning("Unknown envelope type from '%s': %s", agent_id, envelope.envelope_type)

    async def _push_loop(self, agent_id: str, websocket: ServerConnection) -> None:
        """持续将 Broker 中的消息推送到 WebSocket 客户端

        当 Broker 中有新消息到达该 Agent 的队列时，
        此循环将其序列化为 BusEnvelope 并通过 WebSocket 发送。
        """
        try:
            while self._running:
                msg = await self._broker.wait_for_message(agent_id, timeout=1.0)
                if msg is not None:
                    envelope = BusEnvelope.wrap_message(msg, agent_id)
                    try:
                        await websocket.send(envelope.model_dump_json())
                    except websockets.ConnectionClosed:
                        break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Push loop error for agent '%s'", agent_id)

    async def _cleanup_agent(self, agent_id: str) -> None:
        """清理断开的 Agent 连接"""
        self._connections.pop(agent_id, None)
        self._heartbeats.pop(agent_id, None)
        await self._broker.unsubscribe(agent_id)
        logger.info("Cleaned up agent: %s", agent_id)
