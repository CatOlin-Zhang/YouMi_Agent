"""消息总线单元测试

验证:
1. WorkflowMessage 消息模型
2. InProcessBroker 进程内消息路由
3. Agent + Broker 集成（connect_bus / send_message / wait_for_message）
4. WebSocket BusServer + BusClient 跨进程通信
5. 多 Agent 并发通信
6. 广播与点对点投递
7. ACK 确认机制
"""

import asyncio
import json
from datetime import datetime

import pytest

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.types import AgentMessage, AgentMetadata, LLMConfig, LLMProvider, MemoryConfig
from youmi.bus.message import (
    BusEnvelope,
    WorkflowMessage,
    WorkflowMessageType,
)
from youmi.bus.broker import InProcessBroker, MessageBroker
from youmi.bus.server import BusServer
from youmi.bus.ws_client import BusClient


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def make_config(name: str = "TestAgent", **overrides) -> AgentConfig:
    return AgentConfig(
        name=name,
        llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="test", api_key=""),
        memory_config=MemoryConfig(strategy="full"),
        **overrides,
    )


# ===========================================================================
# 1. WorkflowMessage 模型测试
# ===========================================================================

class TestWorkflowMessage:
    """WorkflowMessage 消息模型"""

    def test_create_default(self):
        msg = WorkflowMessage(
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            content="Hello",
        )
        assert msg.from_agent_id == "agent-a"
        assert msg.to_agent_id == "agent-b"
        assert msg.content == "Hello"
        assert msg.msg_type == WorkflowMessageType.STATUS
        assert not msg.is_broadcast
        assert msg.message_id  # 自动生成

    def test_broadcast(self):
        msg = WorkflowMessage(
            from_agent_id="agent-a",
            to_agent_id=None,
            content="Broadcast!",
        )
        assert msg.is_broadcast

        msg2 = WorkflowMessage(
            from_agent_id="agent-a",
            to_agent_id="*",
            content="Broadcast!",
        )
        assert msg2.is_broadcast

    def test_message_types(self):
        assert WorkflowMessageType.TASK.writes_to_memory is True
        assert WorkflowMessageType.FEEDBACK.writes_to_memory is True
        assert WorkflowMessageType.STATUS.writes_to_memory is False
        assert WorkflowMessageType.QUERY.writes_to_memory is False

    def test_to_agent_message(self):
        wf_msg = WorkflowMessage(
            workflow_id="wf-0001",
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            msg_type=WorkflowMessageType.TASK,
            content="Do something",
        )
        agent_msg = wf_msg.to_agent_message()
        assert isinstance(agent_msg, AgentMessage)
        assert agent_msg.from_agent_id == "agent-a"
        assert agent_msg.to_agent_id == "agent-b"
        assert agent_msg.metadata["workflow_id"] == "wf-0001"
        assert agent_msg.metadata["msg_type"] == "task"

    def test_from_agent_message(self):
        agent_msg = AgentMessage(
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            content="Test",
        )
        wf_msg = WorkflowMessage.from_agent_message(
            agent_msg, workflow_id="wf-0002", msg_type=WorkflowMessageType.FEEDBACK,
        )
        assert wf_msg.workflow_id == "wf-0002"
        assert wf_msg.msg_type == WorkflowMessageType.FEEDBACK
        assert wf_msg.content == "Test"

    def test_ack(self):
        msg = WorkflowMessage(
            from_agent_id="a", to_agent_id="b", ack_id="abc123",
        )
        assert msg.needs_ack

        msg2 = WorkflowMessage(from_agent_id="a", to_agent_id="b")
        assert not msg2.needs_ack


# ===========================================================================
# 2. BusEnvelope 测试
# ===========================================================================

class TestBusEnvelope:
    """传输层信封"""

    def test_wrap_message(self):
        msg = WorkflowMessage(
            from_agent_id="a", to_agent_id="b", content="hello",
        )
        env = BusEnvelope.wrap_message(msg, agent_id="a")
        assert env.envelope_type == "message"
        assert env.agent_id == "a"

        # 解包
        unwrapped = env.unwrap_message()
        assert unwrapped.content == "hello"
        assert unwrapped.from_agent_id == "a"

    def test_subscribe(self):
        env = BusEnvelope.subscribe("agent-1", "wf-0001")
        assert env.envelope_type == "subscribe"
        assert env.payload["workflow_id"] == "wf-0001"

    def test_ack(self):
        env = BusEnvelope.ack("msg-123", "agent-1")
        assert env.envelope_type == "ack"
        assert env.payload["message_id"] == "msg-123"

    def test_heartbeat(self):
        env = BusEnvelope.heartbeat("agent-1")
        assert env.envelope_type == "heartbeat"

    def test_json_serialization(self):
        msg = WorkflowMessage(
            from_agent_id="a", to_agent_id="b", content="test",
            workflow_id="wf-001",
        )
        env = BusEnvelope.wrap_message(msg)
        json_str = env.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["envelope_type"] == "message"

        # 反序列化
        env2 = BusEnvelope(**parsed)
        msg2 = env2.unwrap_message()
        assert msg2.content == "test"
        assert msg2.workflow_id == "wf-001"

    def test_unwrap_non_message(self):
        env = BusEnvelope(envelope_type="heartbeat", agent_id="a")
        assert env.unwrap_message() is None


# ===========================================================================
# 3. InProcessBroker 测试
# ===========================================================================

class TestInProcessBroker:
    """进程内消息 Broker"""

    async def test_create_workflow(self):
        broker = InProcessBroker()
        wf1 = await broker.create_workflow()
        wf2 = await broker.create_workflow()
        assert wf1 == "wf-0001"
        assert wf2 == "wf-0002"
        await broker.close()

    async def test_subscribe_and_publish(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        await broker.subscribe("agent-a", wf_id)
        await broker.subscribe("agent-b", wf_id)

        msg = WorkflowMessage(
            workflow_id=wf_id,
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            content="Hello B",
            msg_type=WorkflowMessageType.STATUS,
        )
        await broker.publish(msg)

        # agent-b 应该收到消息
        messages = await broker.pending_messages("agent-b")
        assert len(messages) == 1
        assert messages[0].content == "Hello B"

        # agent-a 不应收到自己的消息
        messages_a = await broker.pending_messages("agent-a")
        assert len(messages_a) == 0

        await broker.close()

    async def test_broadcast(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        await broker.subscribe("agent-a", wf_id)
        await broker.subscribe("agent-b", wf_id)
        await broker.subscribe("agent-c", wf_id)

        msg = WorkflowMessage(
            workflow_id=wf_id,
            from_agent_id="agent-a",
            to_agent_id=None,  # 广播
            content="Broadcast!",
            msg_type=WorkflowMessageType.STATUS,
        )
        await broker.publish(msg)

        msgs_b = await broker.pending_messages("agent-b")
        msgs_c = await broker.pending_messages("agent-c")
        msgs_a = await broker.pending_messages("agent-a")

        assert len(msgs_b) == 1
        assert len(msgs_c) == 1
        assert len(msgs_a) == 0  # 发送者不收到自己的广播

        await broker.close()

    async def test_wait_for_message(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        await broker.subscribe("agent-a", wf_id)
        await broker.subscribe("agent-b", wf_id)

        # 异步发送
        async def send_after_delay():
            await asyncio.sleep(0.1)
            msg = WorkflowMessage(
                workflow_id=wf_id,
                from_agent_id="agent-b",
                to_agent_id="agent-a",
                content="Delayed message",
                msg_type=WorkflowMessageType.STATUS,
            )
            await broker.publish(msg)

        asyncio.create_task(send_after_delay())

        # agent-a 阻塞等待
        result = await broker.wait_for_message("agent-a", timeout=5.0)
        assert result is not None
        assert result.content == "Delayed message"

        await broker.close()

    async def test_wait_for_message_timeout(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        await broker.subscribe("agent-a", wf_id)

        result = await broker.wait_for_message("agent-a", timeout=0.1)
        assert result is None

        await broker.close()

    async def test_workflow_isolation(self):
        """不同 workflow 的消息互相隔离"""
        broker = InProcessBroker()
        wf1 = await broker.create_workflow()
        wf2 = await broker.create_workflow()

        await broker.subscribe("agent-a", wf1)
        await broker.subscribe("agent-b", wf2)

        # agent-a 向 wf1 广播
        msg = WorkflowMessage(
            workflow_id=wf1,
            from_agent_id="agent-a",
            to_agent_id=None,
            content="WF1 message",
        )
        await broker.publish(msg)

        # agent-b 在 wf2，不应收到
        msgs_b = await broker.pending_messages("agent-b")
        assert len(msgs_b) == 0

        await broker.close()

    async def test_ack(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        await broker.subscribe("agent-a", wf_id)
        await broker.subscribe("agent-b", wf_id)

        # task 类型消息自动分配 ack_id
        msg = WorkflowMessage(
            workflow_id=wf_id,
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            content="Task for B",
            msg_type=WorkflowMessageType.TASK,
        )
        await broker.publish(msg)

        # 有待 ACK 的消息
        assert broker.get_pending_ack_count() > 0

        msgs = await broker.pending_messages("agent-b")
        assert len(msgs) == 1

        # ACK
        await broker.ack("agent-b", msgs[0].message_id)

        await broker.close()

    async def test_unsubscribe(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        await broker.subscribe("agent-a", wf_id)

        await broker.unsubscribe("agent-a")

        # 取消订阅后不应收到消息
        msgs = await broker.pending_messages("agent-a")
        assert len(msgs) == 0

        await broker.close()

    async def test_get_workflow_members(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        await broker.subscribe("agent-a", wf_id)
        await broker.subscribe("agent-b", wf_id)

        members = broker.get_workflow_members(wf_id)
        assert "agent-a" in members
        assert "agent-b" in members

        await broker.close()

    async def test_message_callback(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        await broker.subscribe("agent-a", wf_id)
        await broker.subscribe("agent-b", wf_id)

        received = []

        async def on_msg(msg: WorkflowMessage):
            received.append(msg)

        broker.on_message("agent-b", on_msg)

        msg = WorkflowMessage(
            workflow_id=wf_id,
            from_agent_id="agent-a",
            to_agent_id="agent-b",
            content="Callback test",
        )
        await broker.publish(msg)

        assert len(received) == 1
        assert received[0].content == "Callback test"

        await broker.close()


# ===========================================================================
# 4. Agent + Broker 集成测试
# ===========================================================================

class TestAgentBrokerIntegration:
    """Agent 连接消息总线后的行为"""

    async def test_connect_bus(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        agent = Agent(make_config(name="TestAgent"))
        await agent.initialize()

        agent.connect_bus(broker, wf_id)
        assert agent.bus is broker
        assert agent.workflow_id == wf_id

        await broker.subscribe(agent.agent_id, wf_id)
        await broker.close()

    async def test_send_message_via_broker(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        agent_a = Agent(make_config(name="AgentA"))
        agent_b = Agent(make_config(name="AgentB"))
        await agent_a.initialize()
        await agent_b.initialize()

        await broker.subscribe(agent_a.agent_id, wf_id)
        await broker.subscribe(agent_b.agent_id, wf_id)

        agent_a.connect_bus(broker, wf_id)
        agent_b.connect_bus(broker, wf_id)

        # agent_a 发送消息给 agent_b
        msg = await agent_a.send_message(agent_b.agent_id, "Hello from A")
        assert msg.from_agent_id == agent_a.agent_id

        # agent_b 收到
        messages = await agent_b.pending_messages()
        assert len(messages) == 1
        assert messages[0].content == "Hello from A"

        await broker.close()

    async def test_wait_for_message_via_agent(self):
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        agent_a = Agent(make_config(name="AgentA"))
        agent_b = Agent(make_config(name="AgentB"))
        await agent_a.initialize()
        await agent_b.initialize()

        await broker.subscribe(agent_a.agent_id, wf_id)
        await broker.subscribe(agent_b.agent_id, wf_id)

        agent_a.connect_bus(broker, wf_id)
        agent_b.connect_bus(broker, wf_id)

        # 异步发送
        async def send_later():
            await asyncio.sleep(0.1)
            await agent_a.send_message(agent_b.agent_id, "Wait test")

        asyncio.create_task(send_later())

        # agent_b 阻塞等待
        result = await agent_b.wait_for_message(timeout=5.0)
        assert result is not None
        assert result.content == "Wait test"

        await broker.close()

    async def test_wait_for_message_no_bus_raises(self):
        agent = Agent(make_config(name="NoBusAgent"))
        await agent.initialize()

        with pytest.raises(RuntimeError, match="未连接消息总线"):
            await agent.wait_for_message(timeout=1.0)

    async def test_send_without_bus_still_works(self):
        """未连接 Broker 时 send_message 退化为原行为"""
        agent = Agent(make_config(name="StandaloneAgent"))
        await agent.initialize()

        msg = await agent.send_message("some-id", "Hello")
        assert msg.content == "Hello"
        assert msg.to_agent_id == "some-id"

    async def test_to_summary_includes_bus_info(self):
        broker = InProcessBroker()
        agent = Agent(make_config(name="SummaryAgent"))
        await agent.initialize()

        summary = agent.to_summary()
        assert summary["bus_connected"] is False
        assert summary["workflow_id"] == ""

        agent.connect_bus(broker, "wf-test")
        summary = agent.to_summary()
        assert summary["bus_connected"] is True
        assert summary["workflow_id"] == "wf-test"

        await broker.close()


# ===========================================================================
# 5. WebSocket Server + Client 测试
# ===========================================================================

class TestWebSocketBus:
    """WebSocket 消息总线端到端测试"""

    async def test_server_start_stop(self):
        broker = InProcessBroker()
        server = BusServer(broker)
        await server.start(host="localhost", port=18765)

        assert server.broker is broker
        assert len(server.connected_agents) == 0

        await server.stop()

    async def test_client_connect_disconnect(self):
        broker = InProcessBroker()
        server = BusServer(broker)
        await server.start(host="localhost", port=18766)

        try:
            client = BusClient(
                agent_id="ws-agent-1",
                url="ws://localhost:18766",
            )
            await client.connect(workflow_id="wf-ws-1")
            assert client.is_connected

            # 等待 server 注册
            await asyncio.sleep(0.1)
            assert "ws-agent-1" in server.connected_agents

            await client.disconnect()
            assert not client.is_connected
        finally:
            await server.stop()

    async def test_ws_publish_receive(self):
        """通过 WebSocket 发送并接收消息"""
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        server = BusServer(broker)
        await server.start(host="localhost", port=18767)

        try:
            # 两个客户端连接到同一个 server
            client_a = BusClient(agent_id="ws-a", url="ws://localhost:18767")
            client_b = BusClient(agent_id="ws-b", url="ws://localhost:18767")

            await client_a.connect(workflow_id=wf_id)
            await client_b.connect(workflow_id=wf_id)

            await asyncio.sleep(0.2)  # 等待连接建立

            # client_a 发布消息给 client_b
            msg = WorkflowMessage(
                workflow_id=wf_id,
                from_agent_id="ws-a",
                to_agent_id="ws-b",
                content="WebSocket message",
                msg_type=WorkflowMessageType.STATUS,
            )
            await client_a.publish(msg)

            # client_b 等待接收
            result = await client_b.wait_for_message("ws-b", timeout=5.0)
            assert result is not None
            assert result.content == "WebSocket message"

            await client_a.disconnect()
            await client_b.disconnect()
        finally:
            await server.stop()

    async def test_ws_inprocess_hybrid(self):
        """WebSocket 客户端与进程内 Agent 混合通信"""
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()
        server = BusServer(broker)
        await server.start(host="localhost", port=18768)

        try:
            # 进程内 Agent
            local_agent = Agent(make_config(name="LocalAgent"))
            await local_agent.initialize()
            await broker.subscribe(local_agent.agent_id, wf_id)
            local_agent.connect_bus(broker, wf_id)

            # WebSocket 客户端
            ws_client = BusClient(agent_id="ws-remote", url="ws://localhost:18768")
            await ws_client.connect(workflow_id=wf_id)

            await asyncio.sleep(0.2)

            # WebSocket → 进程内
            msg = WorkflowMessage(
                workflow_id=wf_id,
                from_agent_id="ws-remote",
                to_agent_id=local_agent.agent_id,
                content="From remote",
                msg_type=WorkflowMessageType.STATUS,
            )
            await ws_client.publish(msg)

            # 进程内 Agent 收到
            result = await local_agent.wait_for_message(timeout=5.0)
            assert result is not None
            assert result.content == "From remote"

            await ws_client.disconnect()
        finally:
            await server.stop()

    async def test_client_reconnect_failure(self):
        """连接失败时的错误处理"""
        client = BusClient(
            agent_id="fail-agent",
            url="ws://localhost:19999",  # 不存在的服务器
            max_reconnect_attempts=2,
            reconnect_interval=0.1,
        )
        with pytest.raises(ConnectionError):
            await client.connect()


# ===========================================================================
# 6. 多 Agent 并发通信测试
# ===========================================================================

class TestMultiAgentCommunication:
    """多 Agent 并发通信场景"""

    async def test_three_agent_pipeline(self):
        """三 Agent 流水线: A → B → C"""
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        agents = []
        for name in ("PipeA", "PipeB", "PipeC"):
            a = Agent(make_config(name=name))
            await a.initialize()
            await broker.subscribe(a.agent_id, wf_id)
            a.connect_bus(broker, wf_id)
            agents.append(a)

        a, b, c = agents

        # A → B
        await a.send_message(b.agent_id, "Step 1")
        msgs_b = await b.pending_messages()
        assert len(msgs_b) == 1
        assert msgs_b[0].content == "Step 1"

        # B → C
        await b.send_message(c.agent_id, "Step 2")
        msgs_c = await c.pending_messages()
        assert len(msgs_c) == 1
        assert msgs_c[0].content == "Step 2"

        await broker.close()

    async def test_concurrent_send(self):
        """并发发送消息"""
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        agent_a = Agent(make_config(name="ConcurrentA"))
        agent_b = Agent(make_config(name="ConcurrentB"))
        await agent_a.initialize()
        await agent_b.initialize()

        await broker.subscribe(agent_a.agent_id, wf_id)
        await broker.subscribe(agent_b.agent_id, wf_id)

        agent_a.connect_bus(broker, wf_id)
        agent_b.connect_bus(broker, wf_id)

        # 并发发送 10 条消息
        tasks = [
            agent_a.send_message(agent_b.agent_id, f"Message {i}")
            for i in range(10)
        ]
        await asyncio.gather(*tasks)

        msgs = await agent_b.pending_messages()
        assert len(msgs) == 10

        await broker.close()

    async def test_broadcast_status_update(self):
        """广播状态更新"""
        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        agents = []
        for name in ("BroadA", "BroadB", "BroadC"):
            a = Agent(make_config(name=name))
            await a.initialize()
            await broker.subscribe(a.agent_id, wf_id)
            a.connect_bus(broker, wf_id)
            agents.append(a)

        # agent_a 广播
        msg = WorkflowMessage(
            workflow_id=wf_id,
            from_agent_id=agents[0].agent_id,
            to_agent_id=None,
            content="Status update",
            msg_type=WorkflowMessageType.STATUS,
        )
        await broker.publish(msg)

        for agent in agents[1:]:
            msgs = await agent.pending_messages()
            assert len(msgs) == 1
            assert msgs[0].content == "Status update"

        await broker.close()
