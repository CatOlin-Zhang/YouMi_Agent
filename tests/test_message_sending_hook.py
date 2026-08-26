"""MESSAGE_SENDING / MESSAGE_RECEIVED 钩子桩测试

验证引擎层补桩（方案 A）的正确性：
1. MESSAGE_SENDING 钩子在 Agent.send_message() 中正确触发
2. MESSAGE_SENDING 支持 PASS / MODIFY / BLOCK 三种决策
3. MESSAGE_RECEIVED 钩子在 Agent.receive_message() 中正确触发
4. 无注册钩子时行为不受影响（向后兼容）
"""

import asyncio
import pytest

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.hooks import HookContext, HookDecision, HookDecisionType, HookType
from youmi.core.types import AgentMessage, AgentMetadata, MessageRole


# =========================================================================
# 辅助
# =========================================================================

def _make_agent(name: str = "TestAgent") -> Agent:
    config = AgentConfig(
        name=name,
        system_prompt="测试 Agent",
        metadata=AgentMetadata(role="test"),
    )
    return Agent(config)


class _HookCollector:
    """收集钩子调用记录。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def on_message_sending(self, ctx: HookContext) -> HookDecision:
        self.calls.append({
            "hook": "message_sending",
            "agent_id": ctx.agent_id,
            "agent_name": ctx.agent_name,
            "message": ctx.message,
        })
        return HookDecision.pass_through()

    async def on_message_sending_modify(self, ctx: HookContext) -> HookDecision:
        self.calls.append({"hook": "message_sending_modify"})
        return HookDecision.modify(content="[已修改] 原始内容")

    async def on_message_sending_block(self, ctx: HookContext) -> HookDecision:
        self.calls.append({"hook": "message_sending_block"})
        return HookDecision.block(reason="测试拦截")

    async def on_message_received(self, ctx: HookContext) -> HookDecision:
        self.calls.append({
            "hook": "message_received",
            "agent_id": ctx.agent_id,
            "message": ctx.message,
        })
        return HookDecision.pass_through()


# =========================================================================
# MESSAGE_SENDING 钩子
# =========================================================================

class TestMessageSendingHook:
    """测试 send_message() 中 MESSAGE_SENDING 钩子触发。"""

    @pytest.fixture
    async def agent(self):
        a = _make_agent("Sender")
        await a.initialize()
        yield a
        await a.destroy()

    @pytest.mark.asyncio
    async def test_no_hooks_backward_compatible(self, agent: Agent):
        """无钩子注册时，send_message 正常返回，行为不变。"""
        msg = await agent.send_message("target-001", "你好")
        assert msg.content == "你好"
        assert msg.from_agent_id == agent.agent_id
        assert msg.to_agent_id == "target-001"

    @pytest.mark.asyncio
    async def test_hook_fires_on_send(self, agent: Agent):
        """注册 MESSAGE_SENDING 钩子后，send_message 触发钩子。"""
        collector = _HookCollector()
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending,
            plugin_name="test",
        )

        msg = await agent.send_message("target-001", "测试消息")

        assert len(collector.calls) == 1
        call = collector.calls[0]
        assert call["hook"] == "message_sending"
        assert call["agent_id"] == agent.agent_id
        assert call["agent_name"] == "Sender"
        # 消息内容正常传递
        assert msg.content == "测试消息"

    @pytest.mark.asyncio
    async def test_hook_modify_content(self, agent: Agent):
        """MODIFY 决策替换消息内容。"""
        collector = _HookCollector()
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending_modify,
            plugin_name="test",
        )

        msg = await agent.send_message("target-001", "原始内容")

        assert len(collector.calls) == 1
        assert msg.content == "[已修改] 原始内容"
        assert msg.to_agent_id == "target-001"

    @pytest.mark.asyncio
    async def test_hook_block_message(self, agent: Agent):
        """BLOCK 决策阻止消息发送，返回原消息。"""
        collector = _HookCollector()
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending_block,
            plugin_name="test",
        )

        msg = await agent.send_message("target-001", "被拦截的消息")

        assert len(collector.calls) == 1
        assert collector.calls[0]["hook"] == "message_sending_block"
        # BLOCK 后仍返回 AgentMessage（内容不变），但不会投递
        assert msg.content == "被拦截的消息"

    @pytest.mark.asyncio
    async def test_multiple_hooks_chain(self, agent: Agent):
        """多个钩子按优先级链式调用。"""
        collector = _HookCollector()

        # 低优先级先执行 — PASS
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending,
            priority=10,
            plugin_name="test_pass",
        )
        # 高优先级后执行 — MODIFY
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending_modify,
            priority=20,
            plugin_name="test_modify",
        )

        msg = await agent.send_message("target-001", "链式测试")

        # 两个钩子都被调用
        assert len(collector.calls) == 2
        assert collector.calls[0]["hook"] == "message_sending"
        assert collector.calls[1]["hook"] == "message_sending_modify"
        # 最终内容被 MODIFY
        assert msg.content == "[已修改] 原始内容"

    @pytest.mark.asyncio
    async def test_block_stops_chain(self, agent: Agent):
        """BLOCK 决策立即终止后续钩子。"""
        collector = _HookCollector()

        # 第一个钩子 BLOCK
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending_block,
            priority=10,
            plugin_name="test_block",
        )
        # 第二个钩子不应被执行
        agent.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending,
            priority=20,
            plugin_name="test_after_block",
        )

        msg = await agent.send_message("target-001", "链终止")

        # 只有 BLOCK 钩子被调用
        assert len(collector.calls) == 1
        assert collector.calls[0]["hook"] == "message_sending_block"


# =========================================================================
# MESSAGE_RECEIVED 钩子
# =========================================================================

class TestMessageReceivedHook:
    """测试 receive_message() 中 MESSAGE_RECEIVED 钩子触发。"""

    @pytest.fixture
    async def agent(self):
        a = _make_agent("Receiver")
        await a.initialize()
        yield a
        await a.destroy()

    @pytest.mark.asyncio
    async def test_no_hooks_backward_compatible(self, agent: Agent):
        """无钩子注册时，receive_message 正常工作。"""
        msg = AgentMessage(
            from_agent_id="sender-001",
            to_agent_id=agent.agent_id,
            role=MessageRole.AGENT,
            content="你好",
        )
        await agent.receive_message(msg)
        # 不报错即通过

    @pytest.mark.asyncio
    async def test_hook_fires_on_receive(self, agent: Agent):
        """注册 MESSAGE_RECEIVED 钩子后，receive_message 触发钩子。"""
        collector = _HookCollector()
        agent.hook_registry.register(
            HookType.MESSAGE_RECEIVED,
            collector.on_message_received,
            plugin_name="test",
        )

        msg = AgentMessage(
            from_agent_id="sender-001",
            to_agent_id=agent.agent_id,
            role=MessageRole.AGENT,
            content="收到这条消息",
        )
        await agent.receive_message(msg)

        assert len(collector.calls) == 1
        call = collector.calls[0]
        assert call["hook"] == "message_received"
        assert call["agent_id"] == agent.agent_id
        assert call["message"] is msg


# =========================================================================
# 与消息总线集成
# =========================================================================

class TestHookWithBus:
    """验证钩子与 InProcessBroker 的协作。"""

    @pytest.mark.asyncio
    async def test_sending_hook_with_bus(self):
        """连接总线后，MESSAGE_SENDING 钩子仍正常触发。"""
        from youmi.bus.broker import InProcessBroker

        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        sender = _make_agent("BusSender")
        receiver = _make_agent("BusReceiver")
        await sender.initialize()
        await receiver.initialize()

        await broker.subscribe(sender.agent_id, wf_id)
        await broker.subscribe(receiver.agent_id, wf_id)
        sender.connect_bus(broker, wf_id)
        receiver.connect_bus(broker, wf_id)

        collector = _HookCollector()
        sender.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending,
            plugin_name="test",
        )

        msg = await sender.send_message(receiver.agent_id, "总线消息")

        # 钩子被触发
        assert len(collector.calls) == 1
        assert msg.content == "总线消息"

        # 消息确实投递到了接收方
        pending = await broker.pending_messages(receiver.agent_id)
        assert len(pending) == 1
        assert pending[0].content == "总线消息"

        await sender.destroy()
        await receiver.destroy()
        await broker.close()

    @pytest.mark.asyncio
    async def test_modify_hook_changes_bus_content(self):
        """MODIFY 钩子修改内容后，总线上投递的是修改后的内容。"""
        from youmi.bus.broker import InProcessBroker

        broker = InProcessBroker()
        wf_id = await broker.create_workflow()

        sender = _make_agent("ModifySender")
        receiver = _make_agent("ModifyReceiver")
        await sender.initialize()
        await receiver.initialize()

        await broker.subscribe(sender.agent_id, wf_id)
        await broker.subscribe(receiver.agent_id, wf_id)
        sender.connect_bus(broker, wf_id)
        receiver.connect_bus(broker, wf_id)

        collector = _HookCollector()
        sender.hook_registry.register(
            HookType.MESSAGE_SENDING,
            collector.on_message_sending_modify,
            plugin_name="test",
        )

        msg = await sender.send_message(receiver.agent_id, "原始")

        # 返回的消息已被修改
        assert msg.content == "[已修改] 原始内容"

        # 总线上投递的也是修改后的内容
        pending = await broker.pending_messages(receiver.agent_id)
        assert len(pending) == 1
        assert pending[0].content == "[已修改] 原始内容"

        await sender.destroy()
        await receiver.destroy()
        await broker.close()
