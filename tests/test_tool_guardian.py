"""
ToolGuardianAgent 端到端测试

测试内容:
1. ToolIssueType / ToolIssueReport 数据模型
2. LocalFunctionProvider.update_tool_description
3. MCPServer.update_tool_description 路由
4. Agent.report_tool_issue 和自动分类
5. Agent._execute_tool_call 自动汇报闭环
6. ToolGuardianAgent 完整工作流（接收汇报 → 分析 → 修正描述）
7. ToolGuardianAgent 内置工具
8. 多 Agent 汇报 → Guardian 统一处理闭环
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.tool import ToolDefinition, ToolParameter, ToolRegistry
from youmi.core.types import LLMConfig, AgentMetadata
from youmi.mcp.protocol import (
    ToolIssueType,
    ToolIssueReport,
    ToolContext,
    MCPToolResult,
)
from youmi.mcp.provider import LocalFunctionProvider
from youmi.mcp.server import MCPServer
from youmi.coordinator.tool_guardian import ToolGuardianAgent, ToolModification
from youmi.bus.broker import InProcessBroker
from youmi.bus.message import WorkflowMessage, WorkflowMessageType


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [OK] {label}")
    else:
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f"  —  {detail}"
        print(msg)
        raise AssertionError(f"FAILED: {label}")


# =========================================================================
# 工具函数
# =========================================================================

def get_weather(city: str, unit: str = "celsius") -> str:
    """获取指定城市的天气"""
    weather_db = {
        "北京": {"celsius": "25°C", "fahrenheit": "77°F"},
        "上海": {"celsius": "28°C", "fahrenheit": "82°F"},
    }
    data = weather_db.get(city)
    if data is None:
        raise ValueError(f"城市 '{city}' 不在支持列表中")
    return data.get(unit, data.get("celsius", "未知"))


async def calculate(expression: str) -> str:
    """计算数学表达式"""
    try:
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


def divide(a: float, b: float) -> str:
    """执行除法运算"""
    if b == 0:
        raise ZeroDivisionError("除数不能为零")
    return str(a / b)


# =========================================================================
# Test 1: ToolIssueType / ToolIssueReport 数据模型
# =========================================================================

async def test_issue_report_model() -> None:
    print("\n=== Test 1: ToolIssueType / ToolIssueReport 数据模型 ===")

    # ToolIssueType
    check("枚举值数量", len(ToolIssueType) == 6)
    check("unclear_description", ToolIssueType.UNCLEAR_DESCRIPTION.value == "unclear_description")
    check("parameter_boundary", ToolIssueType.PARAMETER_BOUNDARY.value == "parameter_boundary")
    check("missing_feature", ToolIssueType.MISSING_FEATURE.value == "missing_feature")
    check("unexpected_behavior", ToolIssueType.UNEXPECTED_BEHAVIOR.value == "unexpected_behavior")
    check("error_handling", ToolIssueType.ERROR_HANDLING.value == "error_handling")
    check("other", ToolIssueType.OTHER.value == "other")

    # ToolIssueReport
    report = ToolIssueReport(
        reporter_agent_id="agent-001",
        tool_name="get_weather",
        issue_type=ToolIssueType.PARAMETER_BOUNDARY,
        error_message="城市 '天津' 不在支持列表中",
        call_arguments={"city": "天津"},
        suggestion="建议在描述中列出支持的城市列表",
    )
    check("report_id 自动生成", len(report.report_id) == 12)
    check("reporter_agent_id", report.reporter_agent_id == "agent-001")
    check("tool_name", report.tool_name == "get_weather")
    check("issue_type", report.issue_type == ToolIssueType.PARAMETER_BOUNDARY)
    check("error_message", report.error_message == "城市 '天津' 不在支持列表中")
    check("suggestion", report.suggestion == "建议在描述中列出支持的城市列表")
    check("timestamp 非空", len(report.timestamp) > 0)

    # 序列化
    report_dict = report.model_dump()
    check("序列化包含 issue_type", "issue_type" in report_dict)

    # JSON 序列化往返
    report_json = report.model_dump_json()
    report2 = ToolIssueReport.model_validate_json(report_json)
    check("JSON 往返 tool_name", report2.tool_name == "get_weather")
    check("JSON 往返 issue_type", report2.issue_type == ToolIssueType.PARAMETER_BOUNDARY)


# =========================================================================
# Test 2: LocalFunctionProvider.update_tool_description
# =========================================================================

async def test_provider_update_description() -> None:
    print("\n=== Test 2: LocalFunctionProvider.update_tool_description ===")

    provider = LocalFunctionProvider(provider_id="test")
    provider.register_function(get_weather)
    provider.register_function(divide)

    # 获取当前描述
    defn = provider.get_tool_definition("get_weather")
    check("初始描述", defn.description == "获取指定城市的天气")

    # 更新描述
    success = provider.update_tool_description(
        "get_weather",
        description="获取指定城市的天气（仅支持北京、上海）",
    )
    check("update 返回 True", success)

    defn2 = provider.get_tool_definition("get_weather")
    check("描述已更新", defn2.description == "获取指定城市的天气（仅支持北京、上海）")

    # 更新参数描述
    success2 = provider.update_tool_description(
        "get_weather",
        param_descriptions={"city": "城市名称，目前仅支持: 北京、上海"},
    )
    check("参数更新返回 True", success2)

    defn3 = provider.get_tool_definition("get_weather")
    city_param = next((p for p in defn3.parameters if p.name == "city"), None)
    check("参数描述已更新", city_param is not None and "仅支持" in city_param.description)

    # 更新不存在的工具
    success3 = provider.update_tool_description("non_exist", description="test")
    check("不存在的工具返回 False", not success3)

    # description=None 表示不修改
    old_desc = provider.get_tool_definition("divide").description
    provider.update_tool_description("divide", description=None)
    check("description=None 不修改", provider.get_tool_definition("divide").description == old_desc)


# =========================================================================
# Test 3: MCPServer.update_tool_description 路由
# =========================================================================

async def test_server_update_description() -> None:
    print("\n=== Test 3: MCPServer.update_tool_description 路由 ===")

    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    provider.register_function(divide)
    await server.register_provider(provider)

    check("server 有 2 个工具", server.tool_count == 2)

    # 通过 server 更新
    success = server.update_tool_description(
        "get_weather",
        description="获取城市天气（Guardian 修正）",
    )
    check("server.update 成功", success)

    # 验证修改生效
    defn = server.get_tool_definition("get_weather")
    check("server 读取到修改后的描述", defn is not None and "Guardian" in defn.description)

    # 不存在的工具
    success2 = server.update_tool_description("not_exist", description="test")
    check("不存在工具返回 False", not success2)


# =========================================================================
# Test 4: Agent 错误分类和汇报
# =========================================================================

async def test_agent_error_classification() -> None:
    print("\n=== Test 4: Agent 错误分类 ===")

    agent = Agent(AgentConfig(name="TestAgent"))

    # 测试自动分类
    from youmi.mcp.protocol import ToolIssueType

    t1 = agent._classify_tool_error("工具 'xxx' 未找到")
    check("未找到 → UNCLEAR_DESCRIPTION", t1 == ToolIssueType.UNCLEAR_DESCRIPTION)

    t2 = agent._classify_tool_error("参数类型错误 invalid type")
    check("invalid → PARAMETER_BOUNDARY", t2 == ToolIssueType.PARAMETER_BOUNDARY)

    t3 = agent._classify_tool_error("功能不支持 not supported")
    check("not supported → MISSING_FEATURE", t3 == ToolIssueType.MISSING_FEATURE)

    t4 = agent._classify_tool_error("连接超时 timeout")
    check("timeout → ERROR_HANDLING", t4 == ToolIssueType.ERROR_HANDLING)

    t5 = agent._classify_tool_error("发生了未知错误")
    check("未知 → UNEXPECTED_BEHAVIOR", t5 == ToolIssueType.UNEXPECTED_BEHAVIOR)

    t6 = agent._classify_tool_error("工具未注册")
    check("未注册 → UNCLEAR_DESCRIPTION", t6 == ToolIssueType.UNCLEAR_DESCRIPTION)

    t7 = agent._classify_tool_error("参数超出范围 out of range")
    check("out of range → PARAMETER_BOUNDARY", t7 == ToolIssueType.PARAMETER_BOUNDARY)


async def test_agent_report_with_bus() -> None:
    print("\n=== Test 4b: Agent 通过消息总线汇报 ===")

    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()

    # 创建汇报者和 Guardian
    reporter = Agent(AgentConfig(name="Reporter"))
    await broker.subscribe(reporter.agent_id, workflow_id)

    guardian_id = "guardian-test-001"
    await broker.subscribe(guardian_id, workflow_id)

    reporter.connect_guardian(guardian_id, broker, workflow_id)

    # 汇报
    await reporter.report_tool_issue(
        tool_name="get_weather",
        error_message="城市 '天津' 不在支持列表中",
        call_arguments={"city": "天津"},
        suggestion="应在描述中列出支持的城市",
    )

    # Guardian 端接收
    msg = await broker.wait_for_message(guardian_id, timeout=2.0)
    check("Guardian 收到消息", msg is not None)
    check("消息类型为 FEEDBACK", msg.msg_type == WorkflowMessageType.FEEDBACK)
    check("metadata 包含 report_type", msg.metadata.get("report_type") == "tool_issue")

    # 解析 ToolIssueReport
    report = ToolIssueReport.model_validate_json(msg.content)
    check("解析后 tool_name", report.tool_name == "get_weather")
    check("解析后 issue_type", report.issue_type == ToolIssueType.PARAMETER_BOUNDARY)
    check("解析后 reporter", report.reporter_agent_id == reporter.agent_id)

    await broker.close()


# =========================================================================
# Test 5: Agent 工具调用失败自动汇报
# =========================================================================

async def test_auto_report_on_tool_failure() -> None:
    print("\n=== Test 5: 工具调用失败自动汇报 ===")

    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()

    # 创建 Agent 并注册工具
    config = AgentConfig(name="Worker", system_prompt="test", max_iterations=1)
    agent = Agent(config)

    # 注册一个会报错的工具
    def failing_tool(x: int) -> str:
        """会失败的工具"""
        raise ValueError("参数 x 不能为负数")

    agent.register_tool(failing_tool)

    guardian_id = "guardian-auto-001"
    await broker.subscribe(agent.agent_id, workflow_id)
    await broker.subscribe(guardian_id, workflow_id)
    agent.connect_guardian(guardian_id, broker, workflow_id)

    # 手动调用 _execute_tool_call 模拟失败
    result = await agent._execute_tool_call({
        "name": "failing_tool",
        "arguments": {"x": -1},
        "tool_call_id": "tc-001",
    })

    check("工具调用失败", not result.success)
    check("错误信息包含 ValueError", "ValueError" in result.error or "负数" in result.error)

    # Guardian 端应收到汇报
    msg = await broker.wait_for_message(guardian_id, timeout=2.0)
    check("自动汇报已发送", msg is not None)

    if msg:
        report = ToolIssueReport.model_validate_json(msg.content)
        check("汇报工具名", report.tool_name == "failing_tool")
        check("汇报包含错误信息", "负数" in report.error_message or "ValueError" in report.error_message)

    await broker.close()


# =========================================================================
# Test 6: ToolGuardianAgent 完整工作流
# =========================================================================

async def test_guardian_full_workflow() -> None:
    print("\n=== Test 6: ToolGuardianAgent 完整工作流 ===")

    # 1. 搭建 MCPServer
    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    provider.register_function(divide)
    await server.register_provider(provider)

    check("初始工具描述", server.get_tool_definition("get_weather").description == "获取指定城市的天气")

    # 2. 创建 ToolGuardianAgent
    guardian = ToolGuardianAgent(mcp_server=server)
    await guardian.initialize()
    check("Guardian 已初始化", guardian.status == AgentStatus.IDLE)

    # 3. 模拟收到汇报
    report1 = ToolIssueReport(
        reporter_agent_id="worker-001",
        tool_name="get_weather",
        issue_type=ToolIssueType.PARAMETER_BOUNDARY,
        error_message="城市 '天津' 不在支持列表中",
        call_arguments={"city": "天津"},
        suggestion="建议在描述中明确列出支持的城市",
    )

    report2 = ToolIssueReport(
        reporter_agent_id="worker-002",
        tool_name="get_weather",
        issue_type=ToolIssueType.PARAMETER_BOUNDARY,
        error_message="城市 '广州' 不在支持列表中",
        call_arguments={"city": "广州"},
    )

    report3 = ToolIssueReport(
        reporter_agent_id="worker-003",
        tool_name="divide",
        issue_type=ToolIssueType.UNEXPECTED_BEHAVIOR,
        error_message="ZeroDivisionError: 除数不能为零",
        call_arguments={"a": 10, "b": 0},
        suggestion="应在描述中说明除数不能为零",
    )

    await guardian.receive_report(report1)
    await guardian.receive_report(report2)
    await guardian.receive_report(report3)

    check("汇报总数", guardian.report_count == 3)
    check("待处理数", guardian.pending_count == 3)

    # 4. 处理汇报
    results = await guardian.process_reports()
    check("处理结果数", len(results) == 2)  # 2 个不同的工具

    check("待处理数清零", guardian.pending_count == 0)
    check("修改数", guardian.modification_count >= 2)

    # 5. 验证工具描述已更新
    weather_defn = server.get_tool_definition("get_weather")
    check(
        "get_weather 描述包含修正",
        "Guardian" in weather_defn.description or "支持" in weather_defn.description,
        f"got: {weather_defn.description}",
    )

    divide_defn = server.get_tool_definition("divide")
    check(
        "divide 描述包含修正",
        "Guardian" in divide_defn.description or "零" in divide_defn.description,
        f"got: {divide_defn.description}",
    )

    # 6. 验证修改历史
    weather_history = guardian._modification_history.get("get_weather", [])
    check("get_weather 有修改记录", len(weather_history) >= 1)
    check("记录包含旧描述", weather_history[0].old_description == "获取指定城市的天气")

    # 7. 摘要
    summary = guardian.to_summary()
    check("摘要含 report_count", summary["report_count"] == 3)
    check("摘要含 tools_modified", len(summary["tools_modified"]) == 2)


# =========================================================================
# Test 7: ToolGuardianAgent 内置工具
# =========================================================================

async def test_guardian_builtin_tools() -> None:
    print("\n=== Test 7: ToolGuardianAgent 内置工具 ===")

    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    await server.register_provider(provider)

    guardian = ToolGuardianAgent(mcp_server=server)
    await guardian.initialize()

    # 检查内置工具已注册
    tool_names = guardian.tool_registry.tool_names
    check("list_tool_reports 已注册", "list_tool_reports" in tool_names)
    check("list_tool_definitions 已注册", "list_tool_definitions" in tool_names)
    check("update_tool_description 已注册", "update_tool_description" in tool_names)
    check("process_pending_reports 已注册", "process_pending_reports" in tool_names)
    check("get_modification_history 已注册", "get_modification_history" in tool_names)

    # 执行 list_tool_definitions
    result = await guardian.tool_registry.execute("list_tool_definitions", {})
    data = json.loads(result)
    check("list_tool_definitions 返回工具列表", len(data["tools"]) >= 1)

    # 执行 list_tool_reports（无汇报时）
    result2 = await guardian.tool_registry.execute("list_tool_reports", {})
    data2 = json.loads(result2)
    check("无汇报时返回空", data2["total_tools_with_reports"] == 0)

    # 添加汇报后再查
    await guardian.receive_report(ToolIssueReport(
        reporter_agent_id="test",
        tool_name="get_weather",
        issue_type=ToolIssueType.UNCLEAR_DESCRIPTION,
        error_message="test error",
    ))

    result3 = await guardian.tool_registry.execute("list_tool_reports", {"tool_name": "get_weather"})
    data3 = json.loads(result3)
    check("汇报后返回 1 条", data3["report_count"] == 1)

    # 执行 update_tool_description
    result4 = await guardian.tool_registry.execute("update_tool_description", {
        "tool_name": "get_weather",
        "new_description": "手动更新的天气查询描述",
    })
    data4 = json.loads(result4)
    check("手动更新成功", data4["status"] == "updated")

    defn = server.get_tool_definition("get_weather")
    check("描述已手动更新", defn.description == "手动更新的天气查询描述")

    # 执行 process_pending_reports
    result5 = await guardian.tool_registry.execute("process_pending_reports", {})
    data5 = json.loads(result5)
    check("process 返回 processed 字段", "processed" in data5)

    # 执行 get_modification_history
    result6 = await guardian.tool_registry.execute("get_modification_history", {})
    data6 = json.loads(result6)
    check("history 返回 total_tools_modified", "total_tools_modified" in data6)


# =========================================================================
# Test 8: 多 Agent 汇报 → Guardian 统一处理闭环
# =========================================================================

async def test_multi_agent_guardian_loop() -> None:
    print("\n=== Test 8: 多 Agent 汇报 → Guardian 统一处理闭环 ===")

    # 1. 搭建基础设施
    server = MCPServer()
    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()

    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    provider.register_function(divide)
    await server.register_provider(provider)

    # 2. 创建 Guardian
    guardian = ToolGuardianAgent(mcp_server=server)
    await guardian.initialize()
    await broker.subscribe(guardian.agent_id, workflow_id)

    # 3. 创建多个 Worker Agent
    workers = []
    for i in range(3):
        config = AgentConfig(name=f"Worker-{i}", system_prompt="test", max_iterations=5)
        worker = Agent(config)
        worker.register_builtin_tools()
        await broker.subscribe(worker.agent_id, workflow_id)
        worker.connect_guardian(guardian.agent_id, broker, workflow_id)
        workers.append(worker)

    # 4. 模拟各 Worker 汇报不同的工具问题
    await workers[0].report_tool_issue(
        tool_name="get_weather",
        error_message="城市 '天津' 不在支持列表中",
        call_arguments={"city": "天津"},
        issue_type=ToolIssueType.PARAMETER_BOUNDARY.value,
    )

    await workers[1].report_tool_issue(
        tool_name="divide",
        error_message="ZeroDivisionError: 除数不能为零",
        call_arguments={"a": 5, "b": 0},
    )

    await workers[2].report_tool_issue(
        tool_name="get_weather",
        error_message="unit 参数传入了 'kelvin' 但不支持",
        call_arguments={"city": "北京", "unit": "kelvin"},
        issue_type=ToolIssueType.MISSING_FEATURE.value,
    )

    # 5. Guardian 接收所有消息
    await asyncio.sleep(0.1)  # 等待消息投递
    messages = await broker.pending_messages(guardian.agent_id)
    check("Guardian 收到 3 条消息", len(messages) == 3)

    for msg in messages:
        await guardian.receive_message(msg.to_agent_message())

    check("汇报总数", guardian.report_count == 3)

    # 6. Guardian 处理汇报
    results = await guardian.process_reports()
    check("处理了 2 个工具的汇报", len(results) == 2)

    # 7. 验证 MCP 工具描述已修正
    weather_defn = server.get_tool_definition("get_weather")
    check(
        "get_weather 描述已修正",
        "Guardian" in weather_defn.description or "支持" in weather_defn.description,
        f"got: {weather_defn.description[:80]}",
    )

    divide_defn = server.get_tool_definition("divide")
    check(
        "divide 描述已修正",
        "Guardian" in divide_defn.description or "零" in divide_defn.description,
        f"got: {divide_defn.description[:80]}",
    )

    # 8. 验证修改历史可追溯
    summary = guardian.to_summary()
    check("摘要 tools_modified", len(summary["tools_modified"]) == 2)
    check("摘要 report_count", summary["report_count"] == 3)

    # 9. 验证通过消息总线接收的汇报也能正确处理
    result_from_msg = next(
        (r for r in results if r["tool_name"] == "get_weather"),
        None,
    )
    check("从消息中处理的 get_weather", result_from_msg is not None)
    check("get_weather report_count", result_from_msg["report_count"] >= 1)

    await broker.close()


# =========================================================================
# Test 9: receive_message 自动解析 ToolIssueReport
# =========================================================================

async def test_receive_message_auto_parse() -> None:
    print("\n=== Test 9: receive_message 自动解析 ToolIssueReport ===")

    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    await server.register_provider(provider)

    guardian = ToolGuardianAgent(mcp_server=server)
    await guardian.initialize()

    report = ToolIssueReport(
        reporter_agent_id="ext-agent",
        tool_name="get_weather",
        issue_type=ToolIssueType.UNCLEAR_DESCRIPTION,
        error_message="描述不清楚导致调用失败",
    )

    # 构造 AgentMessage 模拟消息总线投递
    from youmi.core.types import AgentMessage, MessageRole
    msg = AgentMessage(
        from_agent_id="ext-agent",
        to_agent_id=guardian.agent_id,
        role=MessageRole.AGENT,
        content=report.model_dump_json(),
        metadata={"report_type": "tool_issue"},
    )

    await guardian.receive_message(msg)
    check("自动解析后 report_count=1", guardian.report_count == 1)
    check("自动解析后 pending_count=1", guardian.pending_count == 1)


# =========================================================================
# Test 10: from_config_dir 工厂方法
# =========================================================================

async def test_from_config_dir() -> None:
    print("\n=== Test 10: from_config_dir 工厂方法 ===")

    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    await server.register_provider(provider)

    # 从配置目录加载
    guardian = ToolGuardianAgent.from_config_dir(mcp_server=server)
    check("名称来自 YAML", guardian.name == "ToolGuardian")
    check("metadata role", guardian.metadata.role == "tool_guardian")
    check("metadata display_name", guardian.metadata.display_name == "工具记忆守护")
    check("system_prompt 非空", len(guardian.config.system_prompt) > 0)
    check("max_iterations", guardian.config.max_iterations == 10)
    check("llm_config model", guardian.config.llm_config.model == "gpt-4o")
    check("llm_config temperature", guardian.config.llm_config.temperature == 0.3)

    # 初始化验证
    await guardian.initialize()
    check("Guardian 已初始化", guardian.status == AgentStatus.IDLE)

    # 覆盖配置测试
    guardian2 = ToolGuardianAgent.from_config_dir(
        mcp_server=server,
        overrides={"max_iterations": 20},
    )
    check("覆盖 max_iterations", guardian2.config.max_iterations == 20)

    # 内置工具仍正常注册
    check("内置工具已注册", "list_tool_reports" in guardian.tool_registry.tool_names)


# =========================================================================
# Main
# =========================================================================

async def main() -> None:
    print("=" * 60)
    print("ToolGuardianAgent 端到端测试")
    print("=" * 60)

    await test_issue_report_model()
    await test_provider_update_description()
    await test_server_update_description()
    await test_agent_error_classification()
    await test_agent_report_with_bus()
    await test_auto_report_on_tool_failure()
    await test_guardian_full_workflow()
    await test_guardian_builtin_tools()
    await test_multi_agent_guardian_loop()
    await test_receive_message_auto_parse()
    await test_from_config_dir()

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
