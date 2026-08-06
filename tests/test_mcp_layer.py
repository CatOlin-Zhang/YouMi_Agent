"""
MCP 层端到端测试

测试内容:
1. MCPToolResult / MCPRequest / MCPResponse 序列化
2. LocalFunctionProvider 注册和执行
3. MCPServer 多 Provider 路由
4. MCPClient 调用链路
5. ToolBridge 权限校验
6. Agent + connect_mcp() 完整闭环 (mock LLM)
7. Agent 双模式兼容 (MCP / ToolRegistry 退化)
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.tool import ToolDefinition, ToolRegistry, ToolParameter
from youmi.core.types import LLMConfig, LLMProvider
from youmi.llm.client import LLMClient, LLMResponse
from youmi.mcp.protocol import (
    MCPRequest, MCPResponse, MCPError,
    MCPToolInfo, MCPToolResult, MCPListToolsResult,
    MCPCallToolParams, ToolContext,
    MCP_ERROR_TOOL_NOT_FOUND,
)
from youmi.mcp.provider import ToolProvider, LocalFunctionProvider
from youmi.mcp.server import MCPServer
from youmi.mcp.client import MCPClient
from youmi.mcp.bridge import ToolBridge


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
    data = weather_db.get(city, {"celsius": "未知"})
    return data.get(unit, data.get("celsius", "未知"))


async def calculate(expression: str) -> str:
    """计算数学表达式"""
    try:
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


def search_web(query: str) -> str:
    """搜索互联网"""
    return f"搜索结果: 关于'{query}'的相关信息..."


# =========================================================================
# Test 1: MCP 协议消息类型
# =========================================================================

async def test_protocol_messages() -> None:
    print("\n=== Test 1: MCP 协议消息类型 ===")

    # MCPRequest
    req = MCPRequest(method="tools/list")
    check("request jsonrpc=2.0", req.jsonrpc == "2.0")
    check("request method", req.method == "tools/list")
    check("request id 自动生成", len(req.id) == 12)
    check("request params 默认空", req.params == {})

    req2 = MCPRequest(method="tools/call", params={"name": "get_weather", "arguments": {"city": "北京"}})
    check("request 带 params", req2.params["name"] == "get_weather")

    # MCPError
    err = MCPError(code=-32001, message="工具未找到")
    check("error code", err.code == -32001)
    check("error message", err.message == "工具未找到")
    check("error 标准码常量", MCP_ERROR_TOOL_NOT_FOUND == -32001)

    # MCPResponse
    resp_ok = MCPResponse(id="abc", result={"tools": []})
    check("response 无 error", not resp_ok.is_error)
    check("response result", resp_ok.result == {"tools": []})

    resp_err = MCPResponse(id="abc", error=MCPError(code=-32001, message="not found"))
    check("response is_error", resp_err.is_error)

    # MCPToolInfo
    info = MCPToolInfo(name="get_weather", description="天气查询", provider_id="local")
    check("tool info name", info.name == "get_weather")
    check("tool info provider", info.provider_id == "local")

    # MCPToolResult
    ok_result = MCPToolResult.success("天气 25°C")
    check("success result text", ok_result.text == "天气 25°C")
    check("success result not error", not ok_result.is_error)

    fail_result = MCPToolResult.failure("权限拒绝")
    check("failure result text", fail_result.text == "权限拒绝")
    check("failure result is_error", fail_result.is_error)

    empty_result = MCPToolResult()
    check("empty result text", empty_result.text == "")

    # ToolContext
    ctx = ToolContext(agent_id="a1", task_id="t1")
    check("context agent_id", ctx.agent_id == "a1")
    check("context trace_id 自动生成", len(ctx.trace_id) == 16)

    # 序列化往返
    req_dict = req2.model_dump()
    req3 = MCPRequest(**req_dict)
    check("序列化往返 method", req3.method == "tools/call")
    check("序列化往返 params", req3.params["name"] == "get_weather")


# =========================================================================
# Test 2: LocalFunctionProvider
# =========================================================================

async def test_local_function_provider() -> None:
    print("\n=== Test 2: LocalFunctionProvider ===")

    provider = LocalFunctionProvider(provider_id="test-local")
    check("provider_id", provider.provider_id == "test-local")
    check("初始为空", len(provider) == 0)

    # register_function — 同步函数
    provider.register_function(get_weather)
    check("注册同步函数后 len=1", len(provider) == 1)
    check("tool_names 含 get_weather", "get_weather" in provider.tool_names)

    # register_function — 异步函数
    provider.register_function(calculate)
    check("注册异步函数后 len=2", len(provider) == 2)

    # register — 手动定义
    defn = ToolDefinition(
        name="add",
        description="加法",
        parameters=[
            ToolParameter(name="a", type="number"),
            ToolParameter(name="b", type="number"),
        ],
    )
    provider.register(defn, handler=lambda a, b: str(a + b))
    check("手动注册后 len=3", len(provider) == 3)

    # get_tools
    tools = await provider.get_tools()
    check("get_tools 返回 3 个", len(tools) == 3)
    weather_tool = next(t for t in tools if t.name == "get_weather")
    check("tool info name", weather_tool.name == "get_weather")
    check("tool info provider_id", weather_tool.provider_id == "test-local")
    check("tool info has schema", "properties" in weather_tool.input_schema)

    # execute — 同步函数
    ctx = ToolContext(agent_id="test-agent")
    result = await provider.execute("get_weather", {"city": "北京"}, ctx)
    check("同步函数执行成功", not result.is_error)
    check("同步函数结果", result.text == "25°C", f"got: {result.text}")

    # execute — 异步函数
    result2 = await provider.execute("calculate", {"expression": "2 + 3"}, ctx)
    check("异步函数执行成功", not result2.is_error)
    check("异步函数结果", result2.text == "5", f"got: {result2.text}")

    # execute — 手动注册的 lambda
    result3 = await provider.execute("add", {"a": 10, "b": 20}, ctx)
    check("lambda 执行", result3.text == "30", f"got: {result3.text}")

    # execute — 带默认参数
    result4 = await provider.execute("get_weather", {"city": "上海", "unit": "fahrenheit"}, ctx)
    check("带默认参数", result4.text == "82°F", f"got: {result4.text}")

    # execute — 未注册工具
    result5 = await provider.execute("not_exist", {}, ctx)
    check("未注册工具返回 error", result5.is_error)

    # unregister
    provider.unregister("add")
    check("注销后 len=2", len(provider) == 2)
    check("已注销工具不存在", "add" not in provider.tool_names)

    # repr
    check("repr 含 provider_id", "test-local" in repr(provider))


# =========================================================================
# Test 3: MCPServer 多 Provider 路由
# =========================================================================

async def test_mcp_server() -> None:
    print("\n=== Test 3: MCPServer 多 Provider 路由 ===")

    server = MCPServer()
    check("初始 provider 数=0", len(server.provider_ids) == 0)
    check("初始 tool_count=0", server.tool_count == 0)

    # Provider 1: 本地工具
    local = LocalFunctionProvider(provider_id="local")
    local.register_function(get_weather)
    local.register_function(calculate)
    await server.register_provider(local)

    check("注册后 provider 数=1", len(server.provider_ids) == 1)
    check("tool_count=2", server.tool_count == 2)

    # Provider 2: 搜索工具
    search_provider = LocalFunctionProvider(provider_id="search")
    search_provider.register_function(search_web)
    await server.register_provider(search_provider)

    check("两个 provider", len(server.provider_ids) == 2)
    check("tool_count=3", server.tool_count == 3)

    # list_tools
    tools = await server.list_tools()
    check("list_tools 返回 3 个", len(tools) == 3)
    names = {t.name for t in tools}
    check("包含所有工具名", names == {"get_weather", "calculate", "search_web"})

    # call_tool — 路由到正确的 provider
    result = await server.call_tool("get_weather", {"city": "北京"})
    check("call_tool get_weather", not result.is_error)
    check("结果正确", result.text == "25°C", f"got: {result.text}")

    result2 = await server.call_tool("search_web", {"query": "Python"})
    check("call_tool search_web", not result2.is_error)
    check("搜索结果含关键字", "Python" in result2.text)

    # call_tool — 未注册工具
    result3 = await server.call_tool("not_exist", {})
    check("未注册工具返回 error", result3.is_error)

    # handle — tools/list
    list_req = MCPRequest(method="tools/list")
    list_resp = await server.handle(list_req)
    check("handle tools/list 无 error", not list_resp.is_error)
    check("result 含 tools 列表", len(list_resp.result["tools"]) == 3)

    # handle — tools/call
    call_req = MCPRequest(method="tools/call", params={
        "name": "calculate",
        "arguments": {"expression": "10 * 5"},
    })
    call_resp = await server.handle(call_req)
    check("handle tools/call 无 error", not call_resp.is_error)
    check("结果正确", "50" in str(call_resp.result))

    # handle — 未知方法
    unknown_req = MCPRequest(method="unknown/method")
    unknown_resp = await server.handle(unknown_req)
    check("未知方法返回 error", unknown_resp.is_error)
    check("error code=-32601", unknown_resp.error.code == -32601)

    # to_openai_tools
    schemas = server.to_openai_tools()
    check("schema 数量=3", len(schemas) == 3)
    check("schema[0] type=function", schemas[0]["type"] == "function")

    # stats (handle 调用计数: tools/call=1, unknown=1)
    stats = server.stats
    check("stats calls>=1", stats["calls"] >= 1, f"got: {stats['calls']}")
    check("stats errors=0 (handle调用成功)", stats["errors"] == 0, f"got: {stats['errors']}")

    # unregister_provider
    await server.unregister_provider("search")
    check("注销后 provider 数=1", len(server.provider_ids) == 1)
    check("tool_count=2", server.tool_count == 2)

    # start / stop
    await server.start()
    await server.stop()
    check("stop 后 provider 清空", len(server.provider_ids) == 0)


# =========================================================================
# Test 4: MCPClient 调用链路
# =========================================================================

async def test_mcp_client() -> None:
    print("\n=== Test 4: MCPClient 调用链路 ===")

    # 搭建 server
    server = MCPServer()
    local = LocalFunctionProvider(provider_id="local")
    local.register_function(get_weather)
    local.register_function(calculate)
    await server.register_provider(local)

    client = MCPClient(server=server)
    check("client.server is server", client.server is server)

    # list_tools
    tools = await client.list_tools()
    check("list_tools 返回 2 个", len(tools) == 2)
    check("工具类型正确", all(isinstance(t, MCPToolInfo) for t in tools))

    # call_tool — 成功
    result = await client.call_tool("get_weather", {"city": "上海"})
    check("call_tool 成功", not result.is_error)
    check("结果正确", result.text == "28°C", f"got: {result.text}")

    # call_tool — 带 context
    ctx = ToolContext(agent_id="test-agent", task_id="task-001")
    result2 = await client.call_tool("calculate", {"expression": "7 * 8"}, ctx)
    check("带 context 调用", not result2.is_error)
    check("结果正确", result2.text == "56", f"got: {result2.text}")

    # call_tool — 未注册
    result3 = await client.call_tool("not_exist", {})
    check("未注册工具返回 error", result3.is_error)
    check("error 含工具名", "not_exist" in result3.text)

    # to_openai_tools
    schemas = client.to_openai_tools()
    check("schema 数量=2", len(schemas) == 2)

    # close (不报错即可)
    await client.close()
    check("close 无异常", True)


# =========================================================================
# Test 5: ToolBridge 权限校验
# =========================================================================

async def test_tool_bridge() -> None:
    print("\n=== Test 5: ToolBridge 权限校验 ===")

    # 搭建 server + client
    server = MCPServer()
    local = LocalFunctionProvider(provider_id="local")
    local.register_function(get_weather)
    local.register_function(calculate)
    local.register_function(search_web)
    await server.register_provider(local)

    client = MCPClient(server=server)

    # Bridge 1: 限制授权
    bridge = ToolBridge(
        agent_id="agent-001",
        mcp_client=client,
        allowed_tools=["get_weather", "calculate"],
    )
    check("bridge agent_id", bridge.agent_id == "agent-001")
    check("allowed_tools", bridge.allowed_tools == {"get_weather", "calculate"})
    check("初始 call_count=0", bridge.call_count == 0)

    # 调用授权工具
    result = await bridge.call_tool("get_weather", {"city": "北京"})
    check("授权工具调用成功", not result.is_error)
    check("结果正确", result.text == "25°C")
    check("call_count=1", bridge.call_count == 1)

    # 调用未授权工具
    result2 = await bridge.call_tool("search_web", {"query": "test"})
    check("未授权工具返回 error", result2.is_error)
    check("error 含权限信息", "权限" in result2.text)
    check("call_count 不增加 (被拒绝)", bridge.call_count == 1)

    # list_tools — 只返回授权的
    tools = await bridge.list_tools()
    check("list_tools 只返回授权工具", len(tools) == 2)
    names = {t.name for t in tools}
    check("工具名正确", names == {"get_weather", "calculate"})

    # to_openai_tools — 只包含授权工具
    schemas = bridge.to_openai_tools()
    check("schema 数量=2 (授权过滤)", len(schemas) == 2)

    # Bridge 2: 无限制 (allowed_tools=None)
    bridge2 = ToolBridge(
        agent_id="agent-002",
        mcp_client=client,
        allowed_tools=None,
    )
    check("无限制 bridge allowed_tools=None", bridge2.allowed_tools is None)

    result3 = await bridge2.call_tool("search_web", {"query": "AI"})
    check("无限制可调用任何工具", not result3.is_error)

    tools2 = await bridge2.list_tools()
    check("无限制 list_tools 返回全部", len(tools2) == 3)

    # 权限管理
    bridge.add_allowed_tool("search_web")
    check("add_allowed_tool", "search_web" in bridge.allowed_tools)

    bridge.remove_allowed_tool("calculate")
    check("remove_allowed_tool", "calculate" not in bridge.allowed_tools)

    # repr
    check("repr 含 agent_id", "agent-001" in repr(bridge))


# =========================================================================
# Mock LLMClient (供 Agent 集成测试)
# =========================================================================

class MockLLMClient:
    """模拟 LLM 客户端"""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.call_history: list[dict] = []

    async def chat(self, messages, tools=None, tool_choice=None, **extra):
        self.call_history.append({"messages": messages, "tools": tools})
        idx = min(self._call_count, len(self._responses) - 1)
        resp_data = self._responses[idx]
        self._call_count += 1
        return LLMResponse(resp_data)

    async def close(self):
        pass


def make_text_response(content: str) -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def make_tool_call_response(tool_name: str, arguments: dict, tool_call_id: str = "call_001") -> dict:
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# =========================================================================
# Test 6: Agent + connect_mcp() 完整闭环
# =========================================================================

async def test_agent_mcp_integration() -> None:
    print("\n=== Test 6: Agent + connect_mcp() 完整闭环 ===")

    # 创建 MCPServer
    server = MCPServer()

    # 创建 Agent
    config = AgentConfig(
        name="MCP-Agent",
        system_prompt="你是一个助手",
        llm_config=LLMConfig(base_url="http://localhost:11434/v1", model="test"),
    )
    agent = Agent(config)

    # 先注册工具到 ToolRegistry
    agent.register_tool(get_weather)

    # 连接 MCP
    agent.connect_mcp(server, provider_id="agent-local")
    check("connect_mcp 后 tool_bridge 非空", agent.tool_bridge is not None)
    check("tool_bridge 类型", isinstance(agent.tool_bridge, ToolBridge))

    # initialize — 注册 Provider 到 Server
    await agent.initialize()
    check("initialize 后 server 有 provider", len(server.provider_ids) == 1)
    check("server tool_count>=1", server.tool_count >= 1)

    # 注入 mock LLM: 先调用工具，再给出最终回复
    mock_llm = MockLLMClient([
        make_tool_call_response("get_weather", {"city": "北京"}, "call_001"),
        make_text_response("北京今天 25°C，天气不错！"),
    ])
    agent._llm_client = mock_llm

    # 执行任务
    result = await agent.run("北京天气怎么样？")
    check("任务完成", result.status == AgentStatus.COMPLETED)
    check("输出含天气信息", "25°C" in str(result.output), f"got: {result.output}")
    check("迭代次数>=1", result.iterations >= 1)

    # 验证 conversation 包含 tool 消息
    tool_msgs = [m for m in agent._conversation if m.get("role") == "tool"]
    check("conversation 含 tool 消息", len(tool_msgs) >= 1)
    check("tool 消息含结果", "25°C" in tool_msgs[0].get("content", ""))

    # 验证 LLM 第 1 次调用时 tools schema 来自 MCP
    first_call = mock_llm.call_history[0]
    check("LLM 收到 tools schema", first_call["tools"] is not None)
    check("tools schema 含 get_weather",
          any(s.get("function", {}).get("name") == "get_weather" for s in first_call["tools"]))

    # ToolBridge call_count
    check("ToolBridge 被调用过", agent.tool_bridge.call_count >= 1)


# =========================================================================
# Test 7: Agent 双模式兼容 (MCP / ToolRegistry 退化)
# =========================================================================

async def test_agent_dual_mode() -> None:
    print("\n=== Test 7: Agent 双模式兼容 ===")

    # 模式 A: 无 MCP — 退化到 ToolRegistry
    config_a = AgentConfig(
        name="Legacy-Agent",
        llm_config=LLMConfig(base_url="http://localhost:11434/v1", model="test"),
    )
    agent_a = Agent(config_a)
    agent_a.register_tool(get_weather)
    await agent_a.initialize()

    check("无 MCP 时 tool_bridge=None", agent_a.tool_bridge is None)

    mock_llm_a = MockLLMClient([
        make_tool_call_response("get_weather", {"city": "上海"}, "call_a1"),
        make_text_response("上海 28°C"),
    ])
    agent_a._llm_client = mock_llm_a

    result_a = await agent_a.run("上海天气如何？")
    check("退化模式任务完成", result_a.status == AgentStatus.COMPLETED)
    check("退化模式结果正确", "28°C" in str(result_a.output), f"got: {result_a.output}")

    # 模式 B: 有 MCP — 通过 ToolBridge
    config_b = AgentConfig(
        name="MCP-Agent",
        llm_config=LLMConfig(base_url="http://localhost:11434/v1", model="test"),
    )
    agent_b = Agent(config_b)
    agent_b.register_tool(get_weather)

    server = MCPServer()
    agent_b.connect_mcp(server)
    await agent_b.initialize()

    check("MCP 模式 tool_bridge 非空", agent_b.tool_bridge is not None)

    mock_llm_b = MockLLMClient([
        make_tool_call_response("get_weather", {"city": "上海"}, "call_b1"),
        make_text_response("上海 28°C via MCP"),
    ])
    agent_b._llm_client = mock_llm_b

    result_b = await agent_b.run("上海天气如何？")
    check("MCP 模式任务完成", result_b.status == AgentStatus.COMPLETED)
    check("MCP 模式结果正确", "28°C" in str(result_b.output), f"got: {result_b.output}")
    check("ToolBridge 被调用", agent_b.tool_bridge.call_count >= 1)


# =========================================================================
# Test 8: Agent + MCP 权限控制
# =========================================================================

async def test_agent_mcp_permissions() -> None:
    print("\n=== Test 8: Agent + MCP 权限控制 ===")

    server = MCPServer()

    # 在 server 上直接注册一个搜索 provider
    search_prov = LocalFunctionProvider(provider_id="search")
    search_prov.register_function(search_web)
    await server.register_provider(search_prov)

    # Agent 只授权 get_weather
    config = AgentConfig(
        name="Restricted-Agent",
        allowed_tools=["get_weather"],
        llm_config=LLMConfig(base_url="http://localhost:11434/v1", model="test"),
    )
    agent = Agent(config)
    agent.register_tool(get_weather)
    agent.connect_mcp(server, provider_id="local")
    await agent.initialize()

    bridge = agent.tool_bridge
    check("allowed_tools 含 get_weather", "get_weather" in bridge.allowed_tools)
    check("allowed_tools 不含 search_web", "search_web" not in bridge.allowed_tools)

    # 调用授权工具 → 成功
    result = await bridge.call_tool("get_weather", {"city": "北京"})
    check("授权工具成功", not result.is_error)

    # 调用未授权工具 → 拒绝
    result2 = await bridge.call_tool("search_web", {"query": "test"})
    check("未授权工具被拒绝", result2.is_error)
    check("拒绝原因含权限", "权限" in result2.text)

    # schema 只包含授权工具
    schemas = bridge.to_openai_tools()
    schema_names = {s.get("function", {}).get("name") for s in schemas}
    check("schema 只含授权工具", "get_weather" in schema_names)
    check("schema 不含未授权工具", "search_web" not in schema_names)


# =========================================================================
# 主入口
# =========================================================================

async def main() -> None:
    print("=" * 60)
    print("MCP 层端到端测试")
    print("=" * 60)

    await test_protocol_messages()
    await test_local_function_provider()
    await test_mcp_server()
    await test_mcp_client()
    await test_tool_bridge()
    await test_agent_mcp_integration()
    await test_agent_dual_mode()
    await test_agent_mcp_permissions()

    print("\n" + "=" * 60)
    print("All MCP layer tests passed!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
