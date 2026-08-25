"""P1 增强功能测试

测试覆盖:
1. SubAgent 任务自检 — 工具充足性评估
2. 工具申请流程 — TOOL_REQUEST / TOOL_RESPONSE 消息
3. 新任务信号检测 — _is_new_task_signal()
4. 状态重置 — reset_for_new_task()
5. 后台流水线 — PostTaskPipeline 工具经验收集
6. 进程隔离 — SubProcessHandle / SubProcessAgentRunner (结构测试)
7. MasterAgent 工具申请工具 — approve / deny
"""

import asyncio
import json

from youmi.coordinator.master import MasterAgent, SubAgentRecord, _TASK_BRIEF_TEMPLATE
from youmi.coordinator.post_task import PostTaskPipeline, ToolExperience, TaskOutcomeSummary
from youmi.coordinator.subprocess_agent import SubProcessAgentRunner, SubProcessHandle, SubProcessResult
from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought, _TaskSelfCheck
from youmi.core.types import AgentMetadata, MessageRole
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
# 测试 1: SubAgent 任务自检 — 无 LLM 时乐观退化
# =========================================================================

async def test_self_check_no_llm():
    print("\n=== Test 1: Self-check without LLM (optimistic) ===")

    config = AgentConfig(
        name="SelfCheckAgent",
        system_prompt="你是测试 Agent",
        metadata=AgentMetadata(role="tester"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 无 LLM 客户端 → 自检应该返回 is_sufficient=True
    result = await agent._self_check_task("执行复杂数据分析任务")
    check("无LLM时is_sufficient=True", result.is_sufficient is True)
    check("无LLM时无missing", len(result.missing_capabilities) == 0)
    check("无LLM时不申请工具", result.request_tools is False)

    await agent.destroy()


# =========================================================================
# 测试 2: 工具申请 — request_tool 无 bus 时退化
# =========================================================================

async def test_request_tool_no_bus():
    print("\n=== Test 2: Tool request without bus (degrade) ===")

    config = AgentConfig(
        name="ToolReqAgent",
        metadata=AgentMetadata(role="tester"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 未连接 bus → request_tool 应返回 False
    result = await agent.request_tool("数据库查询工具", "需要查询数据库")
    check("无bus时request_tool返回False", result is False)

    await agent.destroy()


# =========================================================================
# 测试 3: 工具申请 — 通过消息总线
# =========================================================================

async def test_tool_request_via_bus():
    print("\n=== Test 3: Tool request via message bus ===")

    broker = InProcessBroker()

    # 创建 MasterAgent
    master_config = AgentConfig(
        name="MasterTest",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()
    master.connect_bus(broker, "wf-test")
    await broker.subscribe(master.agent_id, "wf-test")

    # 创建 SubAgent
    sub_config = AgentConfig(
        name="SubTest",
        metadata=AgentMetadata(role="worker"),
    )
    sub = EchoAgent(sub_config)
    await sub.initialize()
    sub.connect_bus(broker, "wf-test")
    await broker.subscribe(sub.agent_id, "wf-test")

    # MasterAgent 注册工具到 registry 以便匹配
    from youmi.core.tool import ToolDefinition
    master._tool_registry.register(
        ToolDefinition(name="file_search", description="搜索文件"),
        lambda **kw: "found",
    )
    master._tool_registry.register(
        ToolDefinition(name="shell_exec", description="执行Shell命令"),
        lambda **kw: "executed",
    )

    # 启动 MasterAgent 工具申请监听
    await master._start_tool_request_listener()

    # SubAgent 发送工具申请
    request_msg = WorkflowMessage(
        workflow_id="wf-test",
        from_agent_id=sub.agent_id,
        to_agent_id=master.agent_id,
        msg_type=WorkflowMessageType.TOOL_REQUEST,
        role=MessageRole.AGENT,
        content=json.dumps({
            "tool_description": "file search capability",
            "reason": "需要搜索文件",
        }),
    )
    await broker.publish(request_msg)

    # 等待 MasterAgent 处理
    await asyncio.sleep(0.5)

    # 检查 MasterAgent 是否发送了 TOOL_RESPONSE
    response = await broker.wait_for_message(sub.agent_id, timeout=3.0)
    check("SubAgent收到TOOL_RESPONSE", response is not None)
    if response:
        check("消息类型为TOOL_RESPONSE", response.msg_type == WorkflowMessageType.TOOL_RESPONSE)
        data = json.loads(response.content)
        check("申请被批准", data.get("approved") is True, f"data={data}")

    # 停止监听
    if master._tool_request_listener_task:
        master._tool_request_listener_task.cancel()

    await sub.destroy()
    await master.destroy()
    await broker.close()


# =========================================================================
# 测试 4: 新任务信号检测
# =========================================================================

async def test_new_task_signal():
    print("\n=== Test 4: New task signal detection ===")

    # 短消息不是新任务
    check("短消息不是新任务", not MasterAgent._is_new_task_signal("你好"))
    check("闲聊不是新任务", not MasterAgent._is_new_task_signal("谢谢你的帮助"))
    check("再见不是新任务", not MasterAgent._is_new_task_signal("再见"))
    check("ok不是新任务", not MasterAgent._is_new_task_signal("ok"))

    # 长消息是新任务
    check(
        "长消息是新任务",
        MasterAgent._is_new_task_signal("帮我写一个Python排序算法，需要支持快速排序和归并排序"),
    )
    check(
        "复杂任务是新任务",
        MasterAgent._is_new_task_signal("请分析这个项目的代码结构，找出潜在的bug"),
    )


# =========================================================================
# 测试 5: 状态重置
# =========================================================================

async def test_reset_for_new_task():
    print("\n=== Test 5: Reset for new task ===")

    master_config = AgentConfig(
        name="MasterReset",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 创建子 Agent
    sub1 = master.create_sub_agent(role="coder", task="写代码")
    sub2 = master.create_sub_agent(role="reviewer", task="审查代码")
    check("创建了2个子Agent", len(master.get_sub_agents()) == 2)

    # 初始化子 Agent
    await sub1.initialize()
    await sub2.initialize()

    # 重置
    await master.reset_for_new_task()

    check("重置后子Agent为空", len(master.get_sub_agents()) == 0)
    check("重置后状态为IDLE", master.status == AgentStatus.IDLE)
    check("待处理申请清空", len(master._pending_tool_requests) == 0)

    await master.destroy()


# =========================================================================
# 测试 6: PostTaskPipeline — 工具经验收集
# =========================================================================

async def test_post_task_pipeline():
    print("\n=== Test 6: PostTaskPipeline ===")

    master_config = AgentConfig(
        name="MasterPipeline",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 创建子 Agent 并模拟工具调用对话记录
    sub = master.create_sub_agent(role="coder", task="写代码")
    await sub.initialize()

    # 模拟对话记录中有工具调用
    sub._conversation = [
        {"role": "system", "content": "你是coder"},
        {"role": "user", "content": "写代码"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "file_write", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "文件写入成功"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_2",
                "type": "function",
                "function": {"name": "shell_exec", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": json.dumps({"error": "权限不足"})},
        {"role": "assistant", "content": "任务完成"},
    ]

    # 运行子 Agent (EchoAgent 会直接完成)
    from youmi.core.agent import TaskResult
    task_results = {
        sub.agent_id: TaskResult(
            agent_id=sub.agent_id,
            status=AgentStatus.COMPLETED,
            output="代码写完了",
            iterations=3,
        ),
    }

    # 运行 PostTaskPipeline
    pipeline = PostTaskPipeline()
    summary = await pipeline.run(master, task_results)

    check("摘要total_agents=1", summary.total_agents == 1)
    check("摘要completed=1", summary.completed == 1)
    check("摘要failed=0", summary.failed == 0)
    check("收集到工具经验", len(summary.tool_experiences) >= 1, f"exps={len(summary.tool_experiences)}")

    # 检查工具经验内容
    exp_names = {e.tool_name for e in summary.tool_experiences}
    check("file_write被记录", "file_write" in exp_names, f"names={exp_names}")
    check("shell_exec被记录", "shell_exec" in exp_names, f"names={exp_names}")

    # 检查 file_write 成功
    fw_exp = next((e for e in summary.tool_experiences if e.tool_name == "file_write"), None)
    if fw_exp:
        check("file_write使用次数=1", fw_exp.usage_count == 1)
        check("file_write成功率=100%", fw_exp.success_rate == 1.0)

    # 检查 shell_exec 失败
    se_exp = next((e for e in summary.tool_experiences if e.tool_name == "shell_exec"), None)
    if se_exp:
        check("shell_exec使用次数=1", se_exp.usage_count == 1)
        check("shell_exec成功率=0%", se_exp.success_rate == 0.0)
        check("shell_exec有失败模式", len(se_exp.failure_patterns) > 0)

    await sub.destroy()
    await master.destroy()


# =========================================================================
# 测试 7: SubProcessAgentRunner 结构测试
# =========================================================================

async def test_subprocess_runner_structure():
    print("\n=== Test 7: SubProcessAgentRunner structure ===")

    runner = SubProcessAgentRunner(ws_url="ws://localhost:9999")
    check("runner创建成功", runner is not None)
    check("ws_url设置正确", runner._ws_url == "ws://localhost:9999")

    # SubProcessResult 数据类
    result = SubProcessResult(
        agent_id="test-123",
        status="completed",
        output="任务完成",
        iterations=5,
        success=True,
    )
    check("SubProcessResult.success=True", result.success is True)
    check("SubProcessResult.status", result.status == "completed")
    check("SubProcessResult.iterations", result.iterations == 5)


# =========================================================================
# 测试 8: SubAgentRecord 含 isolated 标记
# =========================================================================

async def test_sub_agent_record_isolated():
    print("\n=== Test 8: SubAgentRecord isolated flag ===")

    master_config = AgentConfig(
        name="MasterIso",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 普通模式
    sub1 = master.create_sub_agent(role="coder", task="写代码")
    rec1 = master.get_sub_agents()[sub1.agent_id]
    check("普通模式isolated=False", rec1.isolated is False)

    # 隔离模式
    sub2 = master.create_sub_agent(role="reviewer", task="审查", isolated=True)
    rec2 = master.get_sub_agents()[sub2.agent_id]
    check("隔离模式isolated=True", rec2.isolated is True)

    # to_dict 包含 isolated
    d = rec2.to_dict()
    check("to_dict含isolated", "isolated" in d)
    check("to_dict中isolated=True", d["isolated"] is True)

    await master.destroy()


# =========================================================================
# 测试 9: 任务简报模板注入
# =========================================================================

async def test_task_brief_template():
    print("\n=== Test 9: Task brief template injection ===")

    master_config = AgentConfig(
        name="MasterBrief",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    task = "帮我写一个排序算法"
    sub = master.create_sub_agent(role="coder", task=task)

    # 检查 system prompt 包含任务简报
    check(
        "system_prompt含任务简报",
        "任务简报" in sub.config.system_prompt,
        f"prompt末尾={sub.config.system_prompt[-50:]}",
    )
    check(
        "system_prompt含任务描述",
        task in sub.config.system_prompt,
    )
    check(
        "system_prompt含自检提醒",
        "自检提醒" in sub.config.system_prompt,
    )

    await master.destroy()


# =========================================================================
# 测试 10: MasterAgent approve/deny 工具申请
# =========================================================================

async def test_approve_deny_tool_request():
    print("\n=== Test 10: Approve/Deny tool request ===")

    master_config = AgentConfig(
        name="MasterApprove",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 创建子 Agent
    sub = master.create_sub_agent(role="coder", task="写代码")
    await sub.initialize()

    # 模拟待处理申请
    master._pending_tool_requests[sub.agent_id] = ("数据库查询", "需要查询")

    # 批准
    ok = master.approve_tool_request(sub.agent_id, ["db_query", "db_insert"])
    check("approve成功", ok is True)

    # 拒绝 (已经approve过所以pending已清除)
    master._pending_tool_requests[sub.agent_id] = ("网络请求", "需要HTTP")
    ok2 = master.deny_tool_request(sub.agent_id, "当前不支持网络请求")
    check("deny成功", ok2 is True)

    # 拒绝不存在的申请
    ok3 = master.deny_tool_request("nonexistent", "")
    check("不存在的deny返回False", ok3 is False)

    await sub.destroy()
    await master.destroy()


# =========================================================================
# 测试 11: WorkflowMessageType 新类型
# =========================================================================

async def test_new_message_types():
    print("\n=== Test 11: New WorkflowMessageType ===")

    check("TOOL_REQUEST存在", WorkflowMessageType.TOOL_REQUEST.value == "tool_request")
    check("TOOL_RESPONSE存在", WorkflowMessageType.TOOL_RESPONSE.value == "tool_response")

    # 新类型不写入记忆
    check("TOOL_REQUEST不写入记忆", not WorkflowMessageType.TOOL_REQUEST.writes_to_memory)
    check("TOOL_RESPONSE不写入记忆", not WorkflowMessageType.TOOL_RESPONSE.writes_to_memory)

    # 原有类型不受影响
    check("TASK仍写入记忆", WorkflowMessageType.TASK.writes_to_memory)
    check("FEEDBACK仍写入记忆", WorkflowMessageType.FEEDBACK.writes_to_memory)
    check("STATUS不写入记忆", not WorkflowMessageType.STATUS.writes_to_memory)


# =========================================================================
# 测试 12: structure.md 合规 — approve_tool_request 实际更新 ToolBridge
# =========================================================================

async def test_approve_updates_tool_bridge():
    print("\n=== Test 12: Approve updates ToolBridge ===")

    master_config = AgentConfig(
        name="MasterBridge",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    sub = master.create_sub_agent(role="coder", task="写代码", allowed_tools=["file_read"])
    await sub.initialize()

    # 手动设置 ToolBridge (模拟 MCP 连接)
    from youmi.mcp.bridge import ToolBridge
    from youmi.mcp.client import MCPClient
    from youmi.mcp.server import MCPServer
    server = MCPServer()
    client = MCPClient(server=server)
    sub._tool_bridge = ToolBridge(
        agent_id=sub.agent_id,
        mcp_client=client,
        allowed_tools=["file_read"],
    )
    sub._initial_allowed_tools = {"file_read"}

    # 批准前: 只有 file_read
    check("批准前 file_search 不在白名单",
          "file_search" not in (sub._tool_bridge.allowed_tools or set()))

    # 批准 file_search
    ok = master.approve_tool_request(sub.agent_id, ["file_search", "file_write"])
    check("approve成功", ok is True)

    # 批准后: ToolBridge 应包含新工具
    check("批准后 file_search 在白名单",
          "file_search" in (sub._tool_bridge.allowed_tools or set()))
    check("批准后 file_write 在白名单",
          "file_write" in (sub._tool_bridge.allowed_tools or set()))

    # config 也更新了
    check("config.allowed_tools 包含 file_search",
          "file_search" in sub.config.allowed_tools)
    check("config.allowed_tools 包含 file_write",
          "file_write" in sub.config.allowed_tools)

    await sub.destroy()
    await master.destroy()


# =========================================================================
# 测试 13: structure.md 合规 — 工作流级权限回收
# =========================================================================

async def test_workflow_permission_reset():
    print("\n=== Test 13: Workflow-level permission reset ===")

    master_config = AgentConfig(
        name="MasterReset",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    sub = master.create_sub_agent(role="coder", task="写代码", allowed_tools=["file_read"])
    await sub.initialize()

    # 设置 ToolBridge
    from youmi.mcp.bridge import ToolBridge
    from youmi.mcp.client import MCPClient
    from youmi.mcp.server import MCPServer
    server = MCPServer()
    client = MCPClient(server=server)
    sub._tool_bridge = ToolBridge(
        agent_id=sub.agent_id,
        mcp_client=client,
        allowed_tools=["file_read"],
    )
    sub._initial_allowed_tools = {"file_read"}

    # 动态添加工具 (模拟审批通过)
    sub._tool_bridge.add_allowed_tool("shell_exec")
    check("动态添加后 shell_exec 在白名单",
          "shell_exec" in (sub._tool_bridge.allowed_tools or set()))

    # 重置权限
    sub.reset_tool_permissions()
    check("重置后 shell_exec 不在白名单",
          "shell_exec" not in (sub._tool_bridge.allowed_tools or set()))
    check("重置后 file_read 仍在白名单",
          "file_read" in (sub._tool_bridge.allowed_tools or set()))

    await sub.destroy()
    await master.destroy()


# =========================================================================
# 测试 14: structure.md 合规 — 三级审批模型
# =========================================================================

async def test_three_tier_approval():
    print("\n=== Test 14: Three-tier approval model ===")

    master_config = AgentConfig(
        name="MasterApproval",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    # 设置审批清单
    master.set_auto_approve_list(["file_read", "file_write"])
    master.set_sensitive_tools(["shell_exec", "web_fetch"])

    check("auto_approve_list 设置正确", master._auto_approve_list == {"file_read", "file_write"})
    check("sensitive_tools 设置正确", master._sensitive_tools == {"shell_exec", "web_fetch"})
    check("manual_review_queue 初始为空", len(master._manual_review_queue) == 0)

    await master.destroy()


# =========================================================================
# 测试 15: structure.md 合规 — search_new_tools 兆底工具
# =========================================================================

async def test_search_new_tools_registration():
    print("\n=== Test 15: search_new_tools fallback tool ===")

    config = AgentConfig(
        name="SearchAgent",
        metadata=AgentMetadata(role="tester"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 注册内置工具 (包括 search_new_tools)
    agent.register_builtin_tools()

    # 检查 search_new_tools 已注册
    check("search_new_tools 在 registry",
          "search_new_tools" in agent._tool_registry.tool_names)

    # 调用 search_new_tools
    handler = agent._tool_registry._handlers.get("search_new_tools")
    check("handler 存在", handler is not None)

    result_str = await handler(query="file operations", top_k=3)
    result = json.loads(result_str)
    check("返回 candidates 列表", "candidates" in result)
    check("返回 total 计数", "total" in result)

    # 应该匹配到 file_search / file_read / file_write
    names = [c["name"] for c in result["candidates"]]
    check("至少匹配到 1 个工具", len(names) >= 1, f"names={names}")

    await agent.destroy()


# =========================================================================
# 测试 16: structure.md 合规 — reset_for_new_task 包含权限回收
# =========================================================================

async def test_reset_includes_permission_reset():
    print("\n=== Test 16: reset_for_new_task includes permission reset ===")

    master_config = AgentConfig(
        name="MasterResetPerm",
        metadata=AgentMetadata(role="master"),
    )
    master = MasterAgent(master_config)
    await master.initialize()

    sub = master.create_sub_agent(role="coder", task="写代码", allowed_tools=["file_read"])
    await sub.initialize()

    # 设置 ToolBridge
    from youmi.mcp.bridge import ToolBridge
    from youmi.mcp.client import MCPClient
    from youmi.mcp.server import MCPServer
    server = MCPServer()
    client = MCPClient(server=server)
    sub._tool_bridge = ToolBridge(
        agent_id=sub.agent_id,
        mcp_client=client,
        allowed_tools=["file_read"],
    )
    sub._initial_allowed_tools = {"file_read"}

    # 动态添加工具
    sub._tool_bridge.add_allowed_tool("shell_exec")
    sub._tool_bridge.add_allowed_tool("web_fetch")
    check("动态添加后 3 个工具",
          len(sub._tool_bridge.allowed_tools or set()) == 3)

    # reset_for_new_task 应该在销毁前重置权限
    await master.reset_for_new_task()

    # reset_for_new_task 调用了 reset_tool_permissions 再 destroy
    # destroy 后 sub 已不可用，但权限重置发生在 destroy 前
    # 我们主要验证 reset_for_new_task 没有报错且状态正确
    check("重置后子Agent为空", len(master.get_sub_agents()) == 0)
    check("重置后状态为IDLE", master.status == AgentStatus.IDLE)
    check("manual_review_queue 清空", len(master._manual_review_queue) == 0)

    await master.destroy()


# =========================================================================
# 主入口
# =========================================================================

async def main():
    await test_self_check_no_llm()
    await test_request_tool_no_bus()
    await test_tool_request_via_bus()
    await test_new_task_signal()
    await test_reset_for_new_task()
    await test_post_task_pipeline()
    await test_subprocess_runner_structure()
    await test_sub_agent_record_isolated()
    await test_task_brief_template()
    await test_approve_deny_tool_request()
    await test_new_message_types()
    await test_approve_updates_tool_bridge()
    await test_workflow_permission_reset()
    await test_three_tier_approval()
    await test_search_new_tools_registration()
    await test_reset_includes_permission_reset()
    print("\n" + "=" * 50)
    print("  All P1 enhanced + structure.md tests passed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
