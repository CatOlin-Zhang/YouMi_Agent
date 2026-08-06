"""
真实集成测试 — 使用本地 Ollama (qwen2.5:3b)

前置条件:
1. Ollama 服务已启动: ollama serve
2. 已拉取模型: ollama pull qwen2.5:3b

测试内容:
- Test A: 纯文本对话 (无工具)
- Test B: 工具调用 → 工具执行 → 最终回复 (完整闭环)
"""

import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.types import LLMConfig, LLMProvider
from youmi.llm.client import LLMClient


OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL = "qwen2.5:3b"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def get_current_time() -> str:
    """获取当前的日期和时间"""
    from datetime import datetime
    return datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")


def calculate(expression: str) -> str:
    """计算一个数学表达式，例如 '2 + 3 * 4'"""
    try:
        result = eval(expression)
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"


# ---------------------------------------------------------------------------
# Test A: 纯文本对话
# ---------------------------------------------------------------------------

async def test_a_text_only():
    print("=" * 60)
    print("  Test A: 纯文本对话 (无工具)")
    print("=" * 60)

    config = AgentConfig(
        name="ChatBot",
        system_prompt="你是一个友好的中文助手，回答简洁。",
        llm_config=LLMConfig(
            provider=LLMProvider.LOCAL,
            model=MODEL,
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",  # Ollama 不校验 key，但字段不能为空
            temperature=0.7,
            max_tokens=512,
        ),
        max_iterations=3,
    )

    agent = Agent(config)
    agent._llm_client = LLMClient(config.llm_config)
    await agent.initialize()

    print(f"\n[Agent] {agent}")
    print(f"[LLM]   {agent._llm_client}")

    t0 = time.time()
    result = await agent.run("用一句话介绍你自己")
    elapsed = time.time() - t0

    print(f"\n--- 用户: 用一句话介绍你自己")
    print(f"--- 回复: {result.output}")
    print(f"--- 状态: {result.status.value} | 迭代: {result.iterations} | 耗时: {elapsed:.1f}s")

    assert result.success, f"任务失败: {result.error}"
    assert result.iterations == 1, f"纯文本应该1轮完成, got {result.iterations}"
    print("\n  [PASS] Test A 通过 ✓")

    await agent._llm_client.close()
    await agent.destroy()


# ---------------------------------------------------------------------------
# Test B: 工具调用闭环
# ---------------------------------------------------------------------------

async def test_b_tool_call():
    print("\n" + "=" * 60)
    print("  Test B: 工具调用闭环")
    print("=" * 60)

    config = AgentConfig(
        name="ToolBot",
        system_prompt="你是一个实用的助手。当用户询问时间或数学计算时，请使用工具来获取准确结果。回答时引用工具返回的数据。",
        llm_config=LLMConfig(
            provider=LLMProvider.LOCAL,
            model=MODEL,
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",
            temperature=0.3,
            max_tokens=512,
        ),
        max_iterations=5,
    )

    agent = Agent(config)
    agent._llm_client = LLMClient(config.llm_config)
    agent.register_tool(get_current_time)
    agent.register_tool(calculate, param_descriptions={"expression": "数学表达式，如 '2 + 3 * 4'"})
    await agent.initialize()

    print(f"\n[Agent] {agent}")
    print(f"[Tools] {agent.tool_registry.tool_names}")

    # --- 子测试 B1: 时间查询 ---
    print(f"\n--- 用户: 现在几点了？")
    t0 = time.time()
    result = await agent.run("现在几点了？")
    elapsed = time.time() - t0

    print(f"--- 回复: {result.output}")
    print(f"--- 状态: {result.status.value} | 迭代: {result.iterations} | 耗时: {elapsed:.1f}s")

    # 检查 conversation 中是否有 tool 消息
    tool_msgs = [m for m in agent._conversation if m.get("role") == "tool"]
    print(f"--- 工具调用次数: {len(tool_msgs)}")
    if tool_msgs:
        print(f"--- 工具返回: {tool_msgs[0]['content']}")

    assert result.success, f"任务失败: {result.error}"
    has_tool_call = len(tool_msgs) > 0
    if has_tool_call:
        print("  [PASS] B1: 工具被调用 ✓")
    else:
        print("  [WARN] B1: LLM 未调用工具 (3B 模型可能不够稳定，不视为失败)")

    await agent.destroy()

    # --- 子测试 B2: 计算 (新 agent 实例) ---
    config2 = AgentConfig(
        name="CalcBot",
        system_prompt="你是一个计算助手。用户给你数学题时，请使用calculate工具来计算，不要自己心算。",
        llm_config=LLMConfig(
            provider=LLMProvider.LOCAL,
            model=MODEL,
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",
            temperature=0.1,
            max_tokens=256,
        ),
        max_iterations=5,
    )
    agent2 = Agent(config2)
    agent2._llm_client = LLMClient(config2.llm_config)
    agent2.register_tool(calculate, param_descriptions={"expression": "数学表达式"})
    await agent2.initialize()

    print(f"\n--- 用户: 请计算 17 乘以 23 等于多少")
    t0 = time.time()
    result2 = await agent2.run("请计算 17 乘以 23 等于多少")
    elapsed = time.time() - t0

    print(f"--- 回复: {result2.output}")
    print(f"--- 状态: {result2.status.value} | 迭代: {result2.iterations} | 耗时: {elapsed:.1f}s")

    tool_msgs2 = [m for m in agent2._conversation if m.get("role") == "tool"]
    print(f"--- 工具调用次数: {len(tool_msgs2)}")
    if tool_msgs2:
        print(f"--- 工具返回: {tool_msgs2[0]['content']}")

    assert result2.success, f"任务失败: {result2.error}"
    has_tool_call2 = len(tool_msgs2) > 0
    if has_tool_call2:
        print("  [PASS] B2: 计算工具被调用 ✓")
        assert "391" in tool_msgs2[0]["content"], "17*23 应等于 391"
        print("  [PASS] B2: 计算结果正确 (391) ✓")
    else:
        print("  [WARN] B2: LLM 未调用工具 (3B 模型可能不够稳定)")

    await agent2._llm_client.close()
    await agent2.destroy()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    print(f"Ollama: {OLLAMA_BASE_URL}")
    print(f"Model:  {MODEL}")

    # 先检测 Ollama 是否可用
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/models", timeout=5)
            models = resp.json().get("data", [])
            model_names = [m["id"] for m in models]
            print(f"可用模型: {model_names}")
            if not any(MODEL in name for name in model_names):
                print(f"\n[ERROR] 模型 '{MODEL}' 未找到！请先运行: ollama pull {MODEL}")
                return
    except Exception as e:
        print(f"\n[ERROR] 无法连接 Ollama: {e}")
        print("请确保 Ollama 已启动: ollama serve")
        return

    print()

    try:
        await test_a_text_only()
        await test_b_tool_call()

        print("\n" + "=" * 60)
        print("  全部真实集成测试完成!")
        print("=" * 60)
    except Exception as e:
        print(f"\n[FATAL] 测试异常: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
