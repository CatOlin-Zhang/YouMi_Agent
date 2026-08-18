"""P1 功能测试: WorkflowPlan + HeartbeatScheduler + Handoff

测试覆盖:
1. WorkflowPlan — 步骤定义、依赖验证、拓扑排序、循环检测
2. WorkflowExecutor — 串行/并行执行、依赖控制、fail-fast
3. HeartbeatScheduler — 任务添加/移除、定时触发、单次执行、手动触发
4. Handoff — 规则匹配、handoff() 委派、HandoffProtocol 管理
"""

import asyncio
import time

from youmi.coordinator.plan import (
    WorkflowPlan,
    WorkflowStep,
    WorkflowExecutor,
    StepResult,
    StepStatus,
)
from youmi.scheduler import HeartbeatScheduler, ScheduledTask
from youmi.coordinator.handoff import HandoffProtocol
from youmi.coordinator.master import MasterAgent
from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought
from youmi.core.types import (
    AgentMetadata,
    HandoffConfig,
    HandoffRule,
)
from youmi.bus.broker import InProcessBroker
from youmi.bus.message import WorkflowMessage, WorkflowMessageType


# =========================================================================
# 辅助工具
# =========================================================================

class EchoAgent(Agent):
    """测试用 Agent — 回显最后一条用户消息"""

    async def _think(self, observation: _Observation) -> _Thought:
        last = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last = msg.get("content", "")
                break
        return _Thought(
            reasoning="echo",
            action_type="respond",
            action_payload={"response": f"Echo: {last}"},
            should_continue=False,
        )


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "[OK]" if condition else "[FAIL]"
    msg = f"{status} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    assert condition, f"FAILED: {label} {detail}"


# =========================================================================
# 测试1: WorkflowPlan — 验证与拓扑排序
# =========================================================================

async def test_workflow_plan():
    print("\n=== Test 1: WorkflowPlan ===")

    # 1a: 基本验证
    plan = WorkflowPlan(
        name="test-plan",
        steps=[
            WorkflowStep(step_id="a", role="researcher", task="研究"),
            WorkflowStep(step_id="b", role="coder", task="编码", depends_on=["a"]),
            WorkflowStep(step_id="c", role="reviewer", task="审查", depends_on=["b"]),
        ],
    )
    errors = plan.validate()
    check("有效计划无错误", len(errors) == 0, f"errors={errors}")

    # 1b: 拓扑排序
    layers = plan.get_execution_order()
    check("层级数=3", len(layers) == 3, f"layers={layers}")
    check("第一层=[a]", layers[0] == ["a"])
    check("第二层=[b]", layers[1] == ["b"])
    check("第三层=[c]", layers[2] == ["c"])

    # 1c: 并行步骤
    plan2 = WorkflowPlan(
        name="parallel-plan",
        steps=[
            WorkflowStep(step_id="a", role="a", task="A"),
            WorkflowStep(step_id="b", role="b", task="B"),
            WorkflowStep(step_id="c", role="c", task="C", depends_on=["a", "b"]),
        ],
    )
    layers2 = plan2.get_execution_order()
    check("并行层级=2", len(layers2) == 2)
    check("第一层含ab", set(layers2[0]) == {"a", "b"})
    check("第二层=[c]", layers2[1] == ["c"])

    # 1d: 重复 step_id
    plan3 = WorkflowPlan(
        name="dup",
        steps=[
            WorkflowStep(step_id="a", role="a", task="A"),
            WorkflowStep(step_id="a", role="b", task="B"),
        ],
    )
    errors3 = plan3.validate()
    check("重复ID检测", len(errors3) > 0 and "重复" in errors3[0])

    # 1e: 不存在的依赖
    plan4 = WorkflowPlan(
        name="bad-dep",
        steps=[
            WorkflowStep(step_id="a", role="a", task="A", depends_on=["nonexistent"]),
        ],
    )
    errors4 = plan4.validate()
    check("不存在依赖检测", len(errors4) > 0 and "不存在" in errors4[0])

    # 1f: 循环依赖
    plan5 = WorkflowPlan(
        name="cycle",
        steps=[
            WorkflowStep(step_id="a", role="a", task="A", depends_on=["c"]),
            WorkflowStep(step_id="b", role="b", task="B", depends_on=["a"]),
            WorkflowStep(step_id="c", role="c", task="C", depends_on=["b"]),
        ],
    )
    errors5 = plan5.validate()
    check("循环依赖检测", len(errors5) > 0 and "循环" in errors5[0])


# =========================================================================
# 测试2: WorkflowExecutor — 串行执行
# =========================================================================

async def test_executor_serial():
    print("\n=== Test 2: WorkflowExecutor (serial) ===")

    # 创建 MasterAgent (EchoAgent 没有 create_sub_agent)
    master_config = AgentConfig(
        name="MasterTest",
        system_prompt="你是测试主Agent",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 创建串行计划
    plan = WorkflowPlan(
        name="serial-test",
        steps=[
            WorkflowStep(step_id="step1", role="worker", task="执行步骤1"),
            WorkflowStep(step_id="step2", role="worker", task="执行步骤2", depends_on=["step1"]),
            WorkflowStep(step_id="step3", role="worker", task="执行步骤3", depends_on=["step2"]),
        ],
    )

    executor = WorkflowExecutor(master, plan, parallel=False)
    results = await executor.execute()

    check("3个步骤全执行", len(results) == 3)
    check("step1成功", results["step1"].success)
    check("step2成功", results["step2"].success)
    check("step3成功", results["step3"].success)

    # 验证执行顺序 (通过 started_at)
    check(
        "顺序正确",
        results["step1"].started_at <= results["step2"].started_at <= results["step3"].started_at,
    )

    # 摘要
    summary = executor.get_summary()
    check("摘要completed=3", summary["completed"] == 3)
    check("摘要failed=0", summary["failed"] == 0)

    await master.destroy()


# =========================================================================
# 测试3: WorkflowExecutor — 并行执行
# =========================================================================

async def test_executor_parallel():
    print("\n=== Test 3: WorkflowExecutor (parallel) ===")

    master_config = AgentConfig(
        name="MasterParallel",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 并行计划: a 和 b 无依赖可并行, c 依赖 a 和 b
    plan = WorkflowPlan(
        name="parallel-test",
        steps=[
            WorkflowStep(step_id="a", role="worker", task="任务A"),
            WorkflowStep(step_id="b", role="worker", task="任务B"),
            WorkflowStep(step_id="c", role="worker", task="任务C", depends_on=["a", "b"]),
        ],
    )

    executor = WorkflowExecutor(master, plan, parallel=True)
    results = await executor.execute()

    check("3个步骤全执行", len(results) == 3)
    check("a成功", results["a"].success)
    check("b成功", results["b"].success)
    check("c成功", results["c"].success)
    check("c在ab之后", results["c"].started_at >= max(results["a"].started_at, results["b"].started_at))

    await master.destroy()


# =========================================================================
# 测试4: HeartbeatScheduler — 任务管理
# =========================================================================

async def test_scheduler_task_management():
    print("\n=== Test 4: HeartbeatScheduler task management ===")

    scheduler = HeartbeatScheduler()

    # 添加任务
    scheduler.add_task(ScheduledTask(
        name="task1",
        interval_seconds=60,
        task_description="每分钟检查",
    ))
    scheduler.add_task(ScheduledTask(
        name="task2",
        interval_seconds=0,
        task_description="单次执行",
        delay_seconds=0,
    ))

    check("任务列表", set(scheduler.task_names) == {"task1", "task2"})

    # 重复添加报错
    try:
        scheduler.add_task(ScheduledTask(name="task1", interval_seconds=30, task_description="重复"))
        check("重复添加报错", False)
    except ValueError:
        check("重复添加报错", True)

    # 获取状态
    state = scheduler.get_task_state("task1")
    check("状态存在", state is not None)
    check("状态name", state["name"] == "task1")
    check("初始run_count=0", state["run_count"] == 0)

    # 移除
    ok = scheduler.remove_task("task2")
    check("移除成功", ok is True)
    check("移除后只剩1个", len(scheduler.task_names) == 1)

    # 移除不存在的
    ok2 = scheduler.remove_task("nonexistent")
    check("移除不存在返回False", ok2 is False)

    # 快照
    snap = scheduler.snapshot()
    check("快照running=False", snap["running"] is False)
    check("快照任务数", len(snap["tasks"]) == 1)


# =========================================================================
# 测试5: HeartbeatScheduler — 执行
# =========================================================================

async def test_scheduler_execution():
    print("\n=== Test 5: HeartbeatScheduler execution ===")

    # 创建 Agent
    config = AgentConfig(
        name="HeartbeatAgent",
        metadata=AgentMetadata(role="test"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 自定义 handler (不调用 agent.run)
    call_count = 0

    async def mock_handler(ag, task):
        nonlocal call_count
        call_count += 1
        return f"handled: {task.task_description}"

    scheduler = HeartbeatScheduler(handler=mock_handler)
    scheduler.bind_agent(agent)

    # 添加快速任务 (间隔 0.1s, 最多执行 3 次)
    scheduler.add_task(ScheduledTask(
        name="fast_task",
        interval_seconds=0.1,
        task_description="快速任务",
        max_runs=3,
    ))

    # 启动
    await scheduler.start()
    check("已启动", scheduler.is_running)

    # 等待执行
    await asyncio.sleep(0.5)

    # 停止
    await scheduler.stop()
    check("已停止", not scheduler.is_running)

    state = scheduler.get_task_state("fast_task")
    check("执行了多次", state["run_count"] >= 2, f"count={state['run_count']}")

    await agent.destroy()


# =========================================================================
# 测试6: HeartbeatScheduler — 单次执行 + 手动触发
# =========================================================================

async def test_scheduler_single_run():
    print("\n=== Test 6: Scheduler single run + manual trigger ===")

    config = AgentConfig(name="SingleRunAgent", metadata=AgentMetadata(role="test"))
    agent = EchoAgent(config)
    await agent.initialize()

    call_log = []

    async def track_handler(ag, task):
        call_log.append(task.name)
        return "ok"

    scheduler = HeartbeatScheduler(handler=track_handler)
    scheduler.bind_agent(agent)

    # 单次任务
    scheduler.add_task(ScheduledTask(
        name="once",
        interval_seconds=0,
        task_description="单次",
    ))

    await scheduler.start()
    await asyncio.sleep(0.3)
    await scheduler.stop()

    check("单次执行1次", len(call_log) == 1, f"calls={call_log}")

    # 手动触发
    result = await scheduler.run_once("once")
    check("手动触发结果", result == "ok")
    check("手动触发记录", len(call_log) == 2)

    await agent.destroy()


# =========================================================================
# 测试7: Handoff — 规则匹配
# =========================================================================

async def test_handoff_rule_matching():
    print("\n=== Test 7: Handoff rule matching ===")

    config = AgentConfig(
        name="HandoffAgent",
        handoff=HandoffConfig(
            enabled=True,
            rules=[
                HandoffRule(
                    name="code_review",
                    target_agent_id="reviewer-001",
                    trigger_keywords=["审查", "review", "CR"],
                ),
                HandoffRule(
                    name="deploy",
                    target_agent_id="deployer-001",
                    trigger_keywords=["部署", "deploy", "发布"],
                ),
            ],
        ),
        metadata=AgentMetadata(role="coordinator"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 匹配审查规则
    rule1 = agent.match_handoff_rule("请帮我审查这段代码")
    check("匹配审查规则", rule1 is not None)
    check("审查target", rule1.target_agent_id == "reviewer-001")

    # 匹配部署规则
    rule2 = agent.match_handoff_rule("请部署到生产环境")
    check("匹配部署规则", rule2 is not None)
    check("部署target", rule2.target_agent_id == "deployer-001")

    # 不匹配
    rule3 = agent.match_handoff_rule("今天天气不错")
    check("不匹配返回None", rule3 is None)

    # 禁用 handoff
    config2 = AgentConfig(
        name="NoHandoffAgent",
        handoff=HandoffConfig(enabled=False),
        metadata=AgentMetadata(role="test"),
    )
    agent2 = EchoAgent(config2)
    await agent2.initialize()
    rule4 = agent2.match_handoff_rule("审查代码")
    check("禁用时无匹配", rule4 is None)

    await agent.destroy()
    await agent2.destroy()


# =========================================================================
# 测试8: HandoffProtocol — 注册与管理
# =========================================================================

async def test_handoff_protocol():
    print("\n=== Test 8: HandoffProtocol ===")

    broker = InProcessBroker()
    protocol = HandoffProtocol(broker=broker, workflow_id="wf-test")

    # 注册 Agent
    config_a = AgentConfig(name="AgentA", metadata=AgentMetadata(role="sender"))
    config_b = AgentConfig(name="AgentB", metadata=AgentMetadata(role="receiver"))
    agent_a = EchoAgent(config_a)
    agent_b = EchoAgent(config_b)

    await agent_a.initialize()
    await agent_b.initialize()

    # 连接 bus
    agent_a.connect_bus(broker, "wf-test")
    agent_b.connect_bus(broker, "wf-test")

    protocol.register_agent(agent_a)
    protocol.register_agent(agent_b)

    check("注册2个Agent", len(protocol.registered_agents) == 2)

    # 快照
    snap = protocol.snapshot()
    check("快照workflow", snap["workflow_id"] == "wf-test")
    check("快照agents", len(snap["registered_agents"]) == 2)

    # 未注册的目标 → 报错
    result = await protocol.handoff(agent_a, "nonexistent", "test task")
    check("未注册目标失败", not result.success)
    check("错误消息", "注册" in result.error)

    await agent_a.destroy()
    await agent_b.destroy()
    await broker.close()


# =========================================================================
# 测试9: Agent._execute_delegation — 无 bus 时退化
# =========================================================================

async def test_delegation_no_bus():
    print("\n=== Test 9: Delegation without bus ===")

    config = AgentConfig(
        name="NoBusAgent",
        handoff=HandoffConfig(enabled=True),
        metadata=AgentMetadata(role="test"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 无 bus → handoff 失败
    result = await agent.handoff("target-id", "test task")
    check("无bus失败", not result.success)
    check("错误含消息总线", "消息总线" in result.error)

    await agent.destroy()


# =========================================================================
# 测试10: Handoff 深度限制
# =========================================================================

async def test_handoff_depth_limit():
    print("\n=== Test 10: Handoff depth limit ===")

    config = AgentConfig(
        name="DeepAgent",
        handoff=HandoffConfig(
            enabled=True,
            default_max_depth=2,
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # depth >= max_depth → 拒绝
    result = await agent._execute_delegation({
        "target_agent_id": "other",
        "task": "test",
        "depth": 2,  # == max_depth
    })
    check("深度限制拒绝", not result.success)
    check("错误含深度", "深度" in result.error)

    await agent.destroy()


# =========================================================================
# 主入口
# =========================================================================

async def main():
    await test_workflow_plan()
    await test_executor_serial()
    await test_executor_parallel()
    await test_scheduler_task_management()
    await test_scheduler_execution()
    await test_scheduler_single_run()
    await test_handoff_rule_matching()
    await test_handoff_protocol()
    await test_delegation_no_bus()
    await test_handoff_depth_limit()
    print("\n" + "=" * 50)
    print("  All P1 tests passed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
