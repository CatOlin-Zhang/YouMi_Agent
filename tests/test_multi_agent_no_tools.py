"""
无工具调用的多 Agent 协作集成测试

验证场景:
1. MasterAgent 程序化编排多个子 Agent（无需 LLM）
2. 子 Agent 串行流水线执行（Writer → Reviewer → Summarizer）
3. 子 Agent 并行执行
4. Agent 间通过消息总线直接通信（Peer-to-Peer）
5. 记忆系统在多 Agent 场景下的正确性
6. 完整生命周期（创建 → 初始化 → 运行 → 销毁）
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought
from youmi.core.types import AgentMetadata, LLMConfig, LLMProvider, MemoryConfig
from youmi.coordinator.master import MasterAgent
from youmi.bus.broker import InProcessBroker
from youmi.bus.message import WorkflowMessage, WorkflowMessageType


# ---------------------------------------------------------------------------
# 辅助类 — 不同类型的 EchoAgent，模拟无需工具的文本处理 Agent
# ---------------------------------------------------------------------------

class WriterAgent(Agent):
    """写作 Agent — 根据主题生成段落（模拟）"""

    async def _think(self, observation: _Observation) -> _Thought:
        last_user = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last_user = msg.get("content", "")
                break
        return _Thought(
            reasoning=f"Writer: 根据主题 '{last_user}' 撰写内容",
            action_type="respond",
            action_payload={
                "response": f"[Writer输出] 关于「{last_user}」的详细描述：这是一段由Writer Agent生成的内容。"
            },
            should_continue=False,
        )


class ReviewerAgent(Agent):
    """审核 Agent — 对文本进行审核改进（模拟）"""

    async def _think(self, observation: _Observation) -> _Thought:
        last_user = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last_user = msg.get("content", "")
                break
        return _Thought(
            reasoning="Reviewer: 审核并改进内容",
            action_type="respond",
            action_payload={
                "response": f"[Reviewer审核通过] {last_user} —— 审核结论：内容质量良好，无需修改。"
            },
            should_continue=False,
        )


class SummarizerAgent(Agent):
    """摘要 Agent — 对文本生成摘要（模拟）"""

    async def _think(self, observation: _Observation) -> _Thought:
        last_user = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last_user = msg.get("content", "")
                break
        return _Thought(
            reasoning="Summarizer: 生成一句话摘要",
            action_type="respond",
            action_payload={
                "response": f"[摘要] 本文描述了「{last_user[:30]}...」的核心要点。"
            },
            should_continue=False,
        )


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    if condition:
        print(f"  ✓ {label}")
        passed += 1
    else:
        print(f"  ✗ {label}")
        failed += 1


def make_config(name: str, role: str, system_prompt: str = "") -> AgentConfig:
    return AgentConfig(
        name=name,
        system_prompt=system_prompt or f"你是{role}角色的Agent。",
        llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="test", api_key=""),
        memory_config=MemoryConfig(strategy="full"),
        metadata=AgentMetadata(
            display_name=name,
            role=role,
            tags=["test", role],
        ),
    )


# =========================================================================
# Test 1: MasterAgent 串行流水线编排
# =========================================================================

async def test_serial_pipeline() -> None:
    """MasterAgent 创建 Writer → Reviewer → Summarizer 流水线"""
    print("\n=== Test 1: MasterAgent 串行流水线编排 ===")

    # 创建 MasterAgent（无 LLM，程序化编排）
    master = MasterAgent(make_config("TestMaster", "master"))
    await master.initialize()
    check("MasterAgent 初始化完成", master.status == AgentStatus.IDLE)

    # 阶段 1: Writer 生成内容
    writer = master.create_sub_agent(
        role="writer",
        task="人工智能的发展趋势",
        system_prompt="你是一个专业的技术写作Agent。",
    )
    # 替换为 WriterAgent 的逻辑 — 直接覆写 _think
    writer.__class__ = WriterAgent
    await writer.initialize()

    writer_result = await writer.run("人工智能的发展趋势")
    check("Writer 执行成功", writer_result.status == AgentStatus.COMPLETED)
    check("Writer 输出包含主题", "人工智能" in str(writer_result.output))
    print(f"  → Writer 输出: {str(writer_result.output)[:80]}...")

    # 阶段 2: Reviewer 审核 Writer 输出
    reviewer = master.create_sub_agent(
        role="reviewer",
        task="审核Writer的内容",
        system_prompt="你是一个严格的内容审核Agent。",
    )
    reviewer.__class__ = ReviewerAgent
    await reviewer.initialize()

    reviewer_result = await reviewer.run(str(writer_result.output))
    check("Reviewer 执行成功", reviewer_result.status == AgentStatus.COMPLETED)
    check("Reviewer 输出包含审核结论", "审核" in str(reviewer_result.output))
    print(f"  → Reviewer 输出: {str(reviewer_result.output)[:80]}...")

    # 阶段 3: Summarizer 生成摘要
    summarizer = master.create_sub_agent(
        role="summarizer",
        task="生成摘要",
        system_prompt="你是一个摘要生成Agent。",
    )
    summarizer.__class__ = SummarizerAgent
    await summarizer.initialize()

    summary_result = await summarizer.run(str(reviewer_result.output))
    check("Summarizer 执行成功", summary_result.status == AgentStatus.COMPLETED)
    check("Summarizer 输出包含摘要标记", "摘要" in str(summary_result.output))
    print(f"  → Summarizer 输出: {str(summary_result.output)[:80]}...")

    # 验证子 Agent 注册表
    sub_agents = master.get_sub_agents()
    check("MasterAgent 记录了 3 个子 Agent", len(sub_agents) == 3)

    # 销毁
    await master.destroy()
    check("MasterAgent 销毁完成", master.status == AgentStatus.DESTROYED)


# =========================================================================
# Test 2: MasterAgent 并行执行多个子 Agent
# =========================================================================

async def test_parallel_execution() -> None:
    """MasterAgent 并行运行多个子 Agent"""
    print("\n=== Test 2: MasterAgent 并行执行 ===")

    master = MasterAgent(make_config("ParallelMaster", "master"))
    await master.initialize()

    # 创建 3 个独立 Writer
    for i in range(3):
        agent = master.create_sub_agent(
            role="writer",
            name=f"writer_{i}",
            task=f"主题{i}: Python编程技巧",
        )
        agent.__class__ = WriterAgent

    # 并行运行
    results = await master.run_all_sub_agents(parallel=True)
    check("3 个子 Agent 全部完成", len(results) == 3)
    check("全部成功", all(r.status == AgentStatus.COMPLETED for r in results.values()))

    for aid, result in results.items():
        agent = master.get_sub_agent(aid)
        print(f"  → {agent.name}: {str(result.output)[:60]}...")

    await master.destroy()


# =========================================================================
# Test 3: Agent 间消息总线通信 (Peer-to-Peer)
# =========================================================================

async def test_message_bus_communication() -> None:
    """两个 Agent 通过 InProcessBroker 直接通信"""
    print("\n=== Test 3: Agent 间消息总线通信 ===")

    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()
    check(f"工作流创建成功: {workflow_id}", bool(workflow_id))

    # 创建两个 Agent
    agent_a = Agent(make_config("AgentA", "writer", "你是写作Agent。"))
    agent_b = Agent(make_config("AgentB", "reviewer", "你是审核Agent。"))

    # 连接消息总线
    await broker.subscribe(agent_a.agent_id, workflow_id)
    await broker.subscribe(agent_b.agent_id, workflow_id)
    agent_a.connect_bus(broker, workflow_id)
    agent_b.connect_bus(broker, workflow_id)

    # 初始化
    await agent_a.initialize()
    await agent_b.initialize()

    # Agent A 发送消息给 Agent B
    msg = await agent_a.send_message(
        to_agent_id=agent_b.agent_id,
        content="请审核以下内容：Python 是一种优秀的编程语言。",
    )
    check("消息发送成功", msg.from_agent_id == agent_a.agent_id)

    # Agent B 接收消息
    received = await agent_b.wait_for_message(timeout=5.0)
    check("Agent B 收到消息", received is not None)
    if received:
        check("消息内容正确", "Python" in received.content)
        check("消息来自 Agent A", received.from_agent_id == agent_a.agent_id)
        print(f"  → Agent B 收到: {received.content[:60]}...")

    # Agent B 回复给 Agent A
    reply = await agent_b.send_message(
        to_agent_id=agent_a.agent_id,
        content="审核通过，内容质量良好。",
    )

    received_reply = await agent_a.wait_for_message(timeout=5.0)
    check("Agent A 收到回复", received_reply is not None)
    if received_reply:
        check("回复内容正确", "审核通过" in received_reply.content)

    await agent_a.destroy()
    await agent_b.destroy()
    await broker.close()


# =========================================================================
# Test 4: 多 Agent 协作流水线 + 消息总线
# =========================================================================

async def test_pipeline_with_bus() -> None:
    """完整的流水线：Agent 通过消息总线传递中间结果"""
    print("\n=== Test 4: 流水线 + 消息总线协作 ===")

    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()

    # 创建 WriterAgent 和 ReviewerAgent
    writer = WriterAgent(make_config("PipelineWriter", "writer", "你是写作Agent。"))
    reviewer = ReviewerAgent(make_config("PipelineReviewer", "reviewer", "你是审核Agent。"))
    summarizer = SummarizerAgent(make_config("PipelineSummarizer", "summarizer", "你是摘要Agent。"))

    # 连接总线
    for agent in (writer, reviewer, summarizer):
        await broker.subscribe(agent.agent_id, workflow_id)
        agent.connect_bus(broker, workflow_id)
        await agent.initialize()

    # 阶段 1: Writer 执行任务
    writer_result = await writer.run("量子计算的原理与应用")
    check("Writer 完成", writer_result.success)

    # Writer 通过总线将结果发给 Reviewer
    await writer.send_message(
        to_agent_id=reviewer.agent_id,
        content=str(writer_result.output),
    )

    # 阶段 2: Reviewer 接收并处理
    task_msg = await reviewer.wait_for_message(timeout=5.0)
    check("Reviewer 收到 Writer 的输出", task_msg is not None)

    reviewer_result = await reviewer.run(task_msg.content if task_msg else "")
    check("Reviewer 完成", reviewer_result.success)

    # Reviewer 将结果发给 Summarizer
    await reviewer.send_message(
        to_agent_id=summarizer.agent_id,
        content=str(reviewer_result.output),
    )

    # 阶段 3: Summarizer 接收并处理
    review_msg = await summarizer.wait_for_message(timeout=5.0)
    check("Summarizer 收到 Reviewer 的输出", review_msg is not None)

    summary_result = await summarizer.run(review_msg.content if review_msg else "")
    check("Summarizer 完成", summary_result.success)
    check("最终摘要非空", bool(summary_result.output))
    print(f"  → 最终摘要: {str(summary_result.output)[:80]}...")

    # 清理
    for agent in (writer, reviewer, summarizer):
        await agent.destroy()
    await broker.close()


# =========================================================================
# Test 5: 记忆系统在多 Agent 场景下的验证
# =========================================================================

async def test_memory_across_agents() -> None:
    """验证各 Agent 的记忆相互独立且正确记录"""
    print("\n=== Test 5: 记忆系统验证 ===")

    agent_a = WriterAgent(make_config("MemoryWriter", "writer"))
    agent_b = ReviewerAgent(make_config("MemoryReviewer", "reviewer"))

    await agent_a.initialize()
    await agent_b.initialize()

    # Agent A 执行任务
    result_a = await agent_a.run("测试记忆系统")
    check("Agent A 执行成功", result_a.success)

    # Agent B 执行不同任务
    result_b = await agent_b.run("审核另一段内容")
    check("Agent B 执行成功", result_b.success)

    # 验证记忆独立
    context_a = await agent_a.memory.get_context()
    context_b = await agent_b.memory.get_context()

    check("Agent A 有对话记录", len(context_a) >= 2)
    check("Agent B 有对话记录", len(context_b) >= 2)

    # Agent A 的记忆不应包含 Agent B 的内容
    a_contents = " ".join(str(m) for m in context_a)
    check("Agent A 记忆不含 B 的内容", "审核另一段内容" not in a_contents)

    print(f"  → Agent A 记忆: {len(context_a)} 条")
    print(f"  → Agent B 记忆: {len(context_b)} 条")

    await agent_a.destroy()
    await agent_b.destroy()


# =========================================================================
# Test 6: 完整生命周期验证
# =========================================================================

async def test_full_lifecycle() -> None:
    """创建 → 初始化 → 运行 → 状态变更 → 销毁"""
    print("\n=== Test 6: 完整生命周期 ===")

    config = make_config("LifecycleAgent", "writer")
    agent = WriterAgent(config)

    # CREATED
    check("初始状态: CREATED", agent.status == AgentStatus.CREATED)
    check("is_alive: True", agent.is_alive)

    # IDLE
    await agent.initialize()
    check("初始化后: IDLE", agent.status == AgentStatus.IDLE)

    # RUNNING → COMPLETED
    result = await agent.run("生命周期测试任务")
    check("运行后: COMPLETED", result.status == AgentStatus.COMPLETED)
    check("迭代次数 = 1", result.iterations == 1)
    check("输出非空", bool(result.output))

    # DESTROYED
    await agent.destroy()
    check("销毁后: DESTROYED", agent.status == AgentStatus.DESTROYED)
    check("is_alive: False", not agent.is_alive)

    # 验证 TaskResult 时间戳
    check("started_at 有值", result.started_at is not None)
    check("finished_at 有值", result.finished_at is not None)
    check("finished_at >= started_at", result.finished_at >= result.started_at)


# =========================================================================
# Test 7: MasterAgent 使用内置工具管理子 Agent（程序化调用）
# =========================================================================

async def test_master_builtin_tools_programmatic() -> None:
    """直接通过 MasterAgent API 管理子 Agent（模拟内置工具行为）"""
    print("\n=== Test 7: MasterAgent 程序化 API ===")

    master = MasterAgent(make_config("APIMaster", "master"))
    await master.initialize()

    # 创建子 Agent
    sub1 = master.create_sub_agent(role="writer", task="写一篇文章")
    sub2 = master.create_sub_agent(role="reviewer", task="审核代码")

    # 查看子 Agent 列表
    all_subs = master.get_sub_agents()
    check("子 Agent 数量 = 2", len(all_subs) == 2)

    # 获取指定子 Agent
    found = master.get_sub_agent(sub1.agent_id)
    check("能通过 ID 获取子 Agent", found is not None)
    check("获取到的 Agent 名称正确", found.name == "writer" if found else False)

    # 运行一个子 Agent
    result = await master.run_sub_agent(sub1.agent_id)
    check("子 Agent 运行成功", result.status == AgentStatus.COMPLETED)

    # 查看摘要
    summary = master.to_summary()
    check("摘要包含子 Agent 信息", "sub_agents" in summary)
    check("摘要子 Agent 数量正确", summary["sub_agent_count"] == 2)

    await master.destroy()


# =========================================================================
# Main
# =========================================================================

async def main() -> None:
    global passed, failed

    print("=" * 60)
    print("  YouMi Agent — 无工具调用多Agent协作集成测试")
    print("=" * 60)

    await test_serial_pipeline()
    await test_parallel_execution()
    await test_message_bus_communication()
    await test_pipeline_with_bus()
    await test_memory_across_agents()
    await test_full_lifecycle()
    await test_master_builtin_tools_programmatic()

    print("\n" + "=" * 60)
    print(f"  结果: {passed} 通过, {failed} 失败")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
    print("\n=== 全部测试通过！ ===")


if __name__ == "__main__":
    asyncio.run(main())
