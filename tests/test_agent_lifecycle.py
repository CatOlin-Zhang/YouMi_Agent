"""快速验证 Agent 基类的完整生命周期"""

import asyncio
from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought, _Reflection, _ActionResult
from youmi.core.types import AgentMetadata, LLMConfig, LLMProvider


class EchoAgent(Agent):
    """测试用 Agent — 原样回复输入"""

    async def _think(self, observation: _Observation) -> _Thought:
        # 取最后一条 user 消息作为回复
        last_user_msg = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break

        return _Thought(
            reasoning=f"Echo: {last_user_msg}",
            action_type="respond",
            action_payload={"response": f"Echo: {last_user_msg}"},
            should_continue=False,
        )


async def main():
    # 1. 创建配置
    config = AgentConfig(
        name="EchoBot",
        system_prompt="你是一个回声机器人",
        llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="echo-v1", api_key="test"),
        metadata=AgentMetadata(
            display_name="回声机器人",
            role="echo",
            tags=["test", "echo"],
            capabilities=["echo_reply"],
            version="0.1.0",
        ),
    )

    # 2. 实例化
    agent = EchoAgent(config)
    assert agent.status == AgentStatus.CREATED
    assert agent.is_alive
    print(f"[OK] Created: {agent}")

    # 3. 初始化
    await agent.initialize()
    assert agent.status == AgentStatus.IDLE
    print(f"[OK] Initialized: status={agent.status.value}")

    # 4. 记忆验证
    assert agent.memory is not None
    snapshot = await agent.memory.snapshot()
    print(f"[OK] Memory snapshot: {snapshot}")

    # 5. 执行任务
    result = await agent.run(task="Hello World!", task_id="test-001")
    assert result.success
    assert result.output == "Echo: Hello World!"
    assert result.iterations == 1
    print(f"[OK] Task result: output={result.output!r}, iterations={result.iterations}")

    # 6. 记忆验证 (对话应已记录)
    context = await agent.memory.get_context()
    assert len(context) >= 2  # user + assistant
    print(f"[OK] Conversation stored: {len(context)} messages")

    # 7. 状态摘要
    summary = agent.to_summary()
    assert summary["name"] == "EchoBot"
    assert summary["status"] == "completed"
    print(f"[OK] Summary: role={summary['role']}, tags={summary['tags']}")

    # 8. 消息收发
    msg = await agent.send_message(to_agent_id="other-agent", content="协作请求")
    assert msg.from_agent_id == agent.agent_id
    assert msg.to_agent_id == "other-agent"
    print(f"[OK] Message sent: {msg.message_id}")

    # 9. 销毁
    await agent.destroy()
    assert agent.status == AgentStatus.DESTROYED
    assert not agent.is_alive
    print(f"[OK] Destroyed: {agent}")

    print("\n=== All tests passed! ===")


if __name__ == "__main__":
    asyncio.run(main())
