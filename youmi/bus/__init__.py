"""
消息总线模块 (youmi.bus)

提供 Agent 间可靠的消息通信基础设施:

- WorkflowMessage: 工作流消息模型（含 workflow_id、msg_type）
- WorkflowMessageType: 消息类型枚举（task / feedback / status / query）
- BusEnvelope: WebSocket 传输信封
- MessageBroker: 消息路由抽象基类
- InProcessBroker: 进程内 Broker（asyncio.Queue）
- BusServer: WebSocket 服务端
- BusClient: WebSocket 客户端

典型用法::

    # 进程内通信
    broker = InProcessBroker()
    wf_id = await broker.create_workflow()
    await broker.subscribe("agent-a", wf_id)
    await broker.subscribe("agent-b", wf_id)
    await broker.publish(msg)

    # WebSocket 跨进程通信
    server = BusServer(broker)
    await server.start(port=8765)

    client = BusClient(agent_id="agent-c", url="ws://localhost:8765")
    await client.connect(workflow_id=wf_id)
    await client.publish(msg)
"""

from youmi.bus.message import (
    BusEnvelope,
    WorkflowMessage,
    WorkflowMessageType,
)
from youmi.bus.broker import (
    MessageBroker,
    InProcessBroker,
)
from youmi.bus.server import BusServer
from youmi.bus.ws_client import BusClient

__all__ = [
    "BusEnvelope",
    "WorkflowMessage",
    "WorkflowMessageType",
    "MessageBroker",
    "InProcessBroker",
    "BusServer",
    "BusClient",
]
