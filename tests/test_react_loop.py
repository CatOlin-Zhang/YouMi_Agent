"""
ReAct 闭环端到端测试

测试内容:
1. ToolDefinition.from_function — 自动生成工具 schema
2. ToolRegistry — 注册、查找、执行工具
3. Agent + mock LLMClient — 纯文本回复闭环
4. Agent + mock LLMClient — tool_calls → 执行 → 再回复闭环
5. Agent._execute_tool_call — 工具结果回注 conversation
6. 无 LLM 客户端时退化 echo 模式
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
# 工具函数 (供测试注册)
# =========================================================================

def get_weather(city: str, unit: str = "celsius") -> str:
    """获取指定城市的天气"""
    weather_db = {
        "北京": {"celsius": "25°C", "fahrenheit": "77°F"},
        "上海": {"celsius": "28°C", "fahrenheit": "82°F"},
        "深圳": {"celsius": "32°C", "fahrenheit": "90°F"},
    }
    data = weather_db.get(city, {"celsius": "未知", "fahrenheit": "未知"})
    return data.get(unit, data.get("celsius", "未知"))


async def calculate(expression: str) -> str:
    """计算数学表达式"""
    try:
        result = eval(expression)
        return str(result)
    except Exception as e:
        return f"计算错误: {e}"


# =========================================================================
# Test 1: ToolDefinition.from_function
# =========================================================================

async def test_tool_definition() -> None:
    print("\n=== Test 1: ToolDefinition.from_function ===")

    # 同步函数
    defn = ToolDefinition.from_function(get_weather)
    check("名称自动提取", defn.name == "get_weather")
    check("描述自动提取", "天气" in defn.description)
    check("参数数量=2", len(defn.parameters) == 2)

    city_param = defn.parameters[0]
    check("city参数名", city_param.name == "city")
    check("city必填", city_param.required is True)
    check("city类型=string", city_param.type == "string")

    unit_param = defn.parameters[1]
    check("unit参数名", unit_param.name == "unit")
    check("unit可选", unit_param.required is False)

    # 异步函数
    defn2 = ToolDefinition.from_function(calculate, description="计算器")
    check("异步函数名称", defn2.name == "calculate")
    check("自定义描述", defn2.description == "计算器")
    check("参数数量=1", len(defn2.parameters) == 1)

    # OpenAI schema
    schema = defn.to_openai_function_schema()
    check("schema有type", schema["type"] == "function")
    check("schema有function.name", schema["function"]["name"] == "get_weather")
    check("schema有parameters", "properties" in schema["function"]["parameters"])
    check("required含city", "city" in schema["function"]["parameters"]["required"])


# =========================================================================
# Test 2: ToolRegistry
# =========================================================================

async def test_tool_registry() -> None:
    print("\n=== Test 2: ToolRegistry ===")

    registry = ToolRegistry()
    check("初始为空", len(registry) == 0)

    # register_function
    registry.register_function(get_weather)
    check("注册后长度=1", len(registry) == 1)
    check("包含get_weather", "get_weather" in registry)
    check("tool_names", registry.tool_names == ["get_weather"])

    # register with custom definition
    defn = ToolDefinition(
        name="add_numbers",
        description="加法",
        parameters=[
            ToolParameter(name="a", type="number"),
            ToolParameter(name="b", type="number"),
        ],
    )
    registry.register(defn, handler=lambda a, b: a + b)
    check("注册后长度=2", len(registry) == 2)

    # to_openai_tools
    schemas = registry.to_openai_tools()
    check("schema数量=2", len(schemas) == 2)
    check("schema[0]有name", schemas[0]["function"]["name"] == "get_weather")

    # execute 同步
    result = await registry.execute("get_weather", {"city": "北京"})
    check("同步执行结果", result == "25°C", f"got: {result}")

    result2 = await registry.execute("get_weather", {"city": "上海", "unit": "fahrenheit"})
    check("带默认参数执行", result2 == "82°F", f"got: {result2}")

    # execute 自定义handler
    result3 = await registry.execute("add_numbers", {"a": 3, "b": 5})
    check("lambda执行", result3 == 8, f"got: {result3}")

    # execute 异步
    registry.register_function(calculate)
    result4 = await registry.execute("calculate", {"expression": "2 + 3 * 4"})
    check("异步执行", result4 == "14", f"got: {result4}")

    # execute 未注册
    try:
        await registry.execute("not_exist", {})
        check("未注册工具抛异常", False)
    except KeyError:
        check("未注册工具抛异常", True)

    # unregister
    registry.unregister("add_numbers")
    check("注销后长度=2", len(registry) == 2)
    check("已注销工具不存在", "add_numbers" not in registry)


# =========================================================================
# Mock LLMClient
# =========================================================================

class MockLLMClient:
    """模拟 LLM 客户端，按预设脚本返回响应"""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self._call_count = 0
        self.call_history: list[dict] = []

    async def chat(self, messages, tools=None, tool_choice=None, **extra):
        self.call_history.append({
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        })
        idx = min(self._call_count, len(self._responses) - 1)
        resp_data = self._responses[idx]
        self._call_count += 1
        return LLMResponse(resp_data)

    async def close(self):
        pass


def make_text_response(content: str) -> dict:
    """构造纯文本 LLM 响应"""
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def make_tool_call_response(tool_name: str, arguments: dict, tool_call_id: str = "call_001") -> dict:
    """构造 tool_calls LLM 响应"""
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
        "usage": {"prompt_tokens": 15, "completion_tokens": 8},
    }


# =========================================================================
# Test 3: Agent 纯文本闭环
# =========================================================================

async def test_agent_text_response() -> None:
    print("\n=== Test 3: Agent 纯文本闭环 ===")

    mock_llm = MockLLMClient([
        make_text_response("你好！我是YouMi Agent，很高兴为你服务。"),
    ])

    config = AgentConfig(
        name="TestAgent",
        system_prompt="你是一个测试Agent。",
        llm_config=LLMConfig(model="mock-model"),
    )
    agent = Agent(config)
    agent._llm_client = mock_llm  # 注入 mock
    await agent.initialize()

    check("状态=idle", agent.status == AgentStatus.IDLE)

    result = await agent.run("你好")
    check("任务成功", result.success)
    check("输出含问候", "你好" in str(result.output) or "Agent" in str(result.output))
    check("迭代次数=1", result.iterations == 1)
    check("LLM被调用1次", mock_llm._call_count == 1)

    # 检查 conversation 结构
    conv = agent._conversation
    check("conversation有system", conv[0]["role"] == "system")
    check("conversation有user", conv[1]["role"] == "user")
    check("conversation有assistant", conv[2]["role"] == "assistant")
    check("assistant内容正确", "YouMi Agent" in conv[2]["content"])

    await agent.destroy()


# =========================================================================
# Test 4: Agent tool_calls 闭环
# =========================================================================

async def test_agent_tool_call_loop() -> None:
    print("\n=== Test 4: Agent tool_calls 闭环 ===")

    # 模拟: LLM 先请求调用 get_weather，拿到结果后给出最终回复
    mock_llm = MockLLMClient([
        # 第1次调用: LLM 请求调用工具
        make_tool_call_response("get_weather", {"city": "北京"}, "call_abc"),
        # 第2次调用: LLM 看到工具结果，给出最终回复
        make_text_response("北京今天的天气是 25°C，适合外出。"),
    ])

    config = AgentConfig(
        name="WeatherAgent",
        system_prompt="你是天气助手，可以查询天气。",
        llm_config=LLMConfig(model="mock-model"),
        max_iterations=5,
    )
    agent = Agent(config)
    agent._llm_client = mock_llm
    agent.register_tool(get_weather, param_descriptions={"city": "城市名"})
    await agent.initialize()

    check("工具已注册", "get_weather" in agent.tool_registry)

    result = await agent.run("北京今天天气怎么样？")
    check("任务成功", result.success)
    check("输出含天气", "25°C" in str(result.output) or "天气" in str(result.output), f"got: {result.output}")
    check("迭代次数=2", result.iterations == 2, f"got: {result.iterations}")
    check("LLM被调用2次", mock_llm._call_count == 2, f"got: {mock_llm._call_count}")

    # 检查 conversation 完整性
    conv = agent._conversation
    check("conv[0]=system", conv[0]["role"] == "system")
    check("conv[1]=user", conv[1]["role"] == "user")
    check("conv[2]=assistant+tool_calls", conv[2]["role"] == "assistant" and "tool_calls" in conv[2])
    check("conv[3]=tool result", conv[3]["role"] == "tool" and "25°C" in conv[3]["content"])
    check("conv[4]=assistant final", conv[4]["role"] == "assistant" and "25°C" in conv[4]["content"])

    # 检查 tools schema 被传给 LLM
    first_call = mock_llm.call_history[0]
    check("tools schema已传", first_call["tools"] is not None and len(first_call["tools"]) > 0)
    check("tools含get_weather", first_call["tools"][0]["function"]["name"] == "get_weather")

    await agent.destroy()


# =========================================================================
# Test 5: 多工具注册与顺序调用
# =========================================================================

async def test_multi_tool_chain() -> None:
    print("\n=== Test 5: 多工具链式调用 ===")

    mock_llm = MockLLMClient([
        # 第1次: 调用 get_weather
        make_tool_call_response("get_weather", {"city": "上海"}, "call_001"),
        # 第2次: 调用 calculate
        make_tool_call_response("calculate", {"expression": "28 + 5"}, "call_002"),
        # 第3次: 最终回复
        make_text_response("上海气温28°C，加5度后是33°C。"),
    ])

    config = AgentConfig(
        name="MultiToolAgent",
        system_prompt="你可以查天气和做计算。",
        llm_config=LLMConfig(model="mock-model"),
        max_iterations=5,
    )
    agent = Agent(config)
    agent._llm_client = mock_llm
    agent.register_tool(get_weather)
    agent.register_tool(calculate)
    await agent.initialize()

    check("注册2个工具", len(agent.tool_registry) == 2)

    result = await agent.run("上海气温加5度是多少？")
    check("任务成功", result.success)
    check("迭代3次", result.iterations == 3, f"got: {result.iterations}")
    check("LLM调用3次", mock_llm._call_count == 3)

    # conversation 应有: system + user + (assistant_tc + tool) × 2 + assistant_final = 7
    conv = agent._conversation
    check("conv长度=7", len(conv) == 7, f"got: {len(conv)}")
    check("第1个tool result含28°C", "28°C" in conv[3]["content"], f"got: {conv[3]}")
    check("第2个tool result含33", "33" in conv[5]["content"], f"got: {conv[5]}")

    await agent.destroy()


# =========================================================================
# Test 6: 无 LLM 客户端 — echo 退化
# =========================================================================

async def test_no_llm_echo() -> None:
    print("\n=== Test 6: 无LLM客户端 echo 退化 ===")

    config = AgentConfig(name="EchoAgent")
    agent = Agent(config)
    # 不注入 _llm_client
    await agent.initialize()

    result = await agent.run("测试消息")
    check("任务成功", result.success)
    check("输出含echo标记", "[无LLM客户端]" in str(result.output))
    check("输出含原始消息", "测试消息" in str(result.output))
    check("迭代1次", result.iterations == 1)

    await agent.destroy()


# =========================================================================
# Test 7: 工具不存在时的错误处理
# =========================================================================

async def test_missing_tool_error_handling() -> None:
    print("\n=== Test 7: 工具不存在错误处理 ===")

    mock_llm = MockLLMClient([
        # LLM 请求调用一个未注册的工具
        make_tool_call_response("nonexistent_tool", {"x": 1}, "call_err"),
        # LLM 收到错误后的回复
        make_text_response("抱歉，我无法查询该信息。"),
    ])

    config = AgentConfig(
        name="ErrorAgent",
        system_prompt="你是一个助手。",
        llm_config=LLMConfig(model="mock-model"),
        max_iterations=3,
    )
    agent = Agent(config)
    agent._llm_client = mock_llm
    # 不注册任何工具
    await agent.initialize()

    result = await agent.run("查询不存在的数据")
    check("任务仍然成功 (优雅降级)", result.success)
    check("LLM调用2次 (错误回注后重试)", mock_llm._call_count == 2)

    # 检查错误信息被回注到 conversation
    conv = agent._conversation
    tool_msgs = [m for m in conv if m.get("role") == "tool"]
    check("有tool消息", len(tool_msgs) > 0)
    check("tool消息含错误", "error" in tool_msgs[0].get("content", ""))

    await agent.destroy()


# =========================================================================
# Test 8: LLMResponse 解析
# =========================================================================

async def test_llm_response_parsing() -> None:
    print("\n=== Test 8: LLMResponse 解析 ===")

    # 纯文本
    text_resp = LLMResponse(make_text_response("hello world"))
    check("text content", text_resp.content == "hello world")
    check("no tool_calls", not text_resp.has_tool_calls)
    check("tool_calls空列表", text_resp.tool_calls == [])
    check("finish_reason=stop", text_resp.finish_reason == "stop")
    check("raw_message role", text_resp.raw_message["role"] == "assistant")

    # tool_calls
    tc_resp = LLMResponse(make_tool_call_response("test_fn", {"a": 1}, "call_99"))
    check("tc has_tool_calls", tc_resp.has_tool_calls)
    check("tc tool_calls数量", len(tc_resp.tool_calls) == 1)
    check("tc name", tc_resp.tool_calls[0]["function"]["name"] == "test_fn")
    check("tc raw_message有tool_calls", "tool_calls" in tc_resp.raw_message)
    check("tc finish_reason", tc_resp.finish_reason == "tool_calls")


# =========================================================================
# Main
# =========================================================================

async def main() -> None:
    await test_tool_definition()
    await test_tool_registry()
    await test_llm_response_parsing()
    await test_agent_text_response()
    await test_agent_tool_call_loop()
    await test_multi_tool_chain()
    await test_no_llm_echo()
    await test_missing_tool_error_handling()

    print("\n" + "=" * 50)
    print("  All ReAct loop tests passed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
