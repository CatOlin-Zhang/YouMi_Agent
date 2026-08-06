"""记忆策略系统完整测试

测试覆盖:
1. FullMemoryStrategy — 全量存储
2. SummaryMemoryStrategy — 对话摘要
3. LSTMMemoryStrategy — 长短时记忆
4. 自定义策略文件动态加载
5. MemoryManager 统一接口
6. Agent 集成记忆策略
"""

import asyncio
import os

from youmi.memory.memory import MemoryManager
from youmi.memory.strategies import (
    FullMemoryStrategy,
    SummaryMemoryStrategy,
    LSTMMemoryStrategy,
    create_strategy,
    list_strategies,
)
from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought
from youmi.core.types import AgentMetadata, MemoryConfig


# =========================================================================
# 辅助工具
# =========================================================================

async def fake_llm_call(messages: list[dict[str, str]]) -> str:
    """模拟 LLM 调用 — 返回固定摘要"""
    return "这是对话摘要: 用户请求了代码帮助。"


async def fake_classify_llm(messages: list[dict[str, str]]) -> str:
    """模拟 LLM 分类 — 根据内容判断"""
    # _classify 会将内容包裹在分类提示词中，需要提取原始内容
    full_prompt = messages[0]["content"]
    # 提示词格式: "...对话内容:\n{content}\n\n请只回答..."
    # 从提示词中提取实际对话内容
    content = full_prompt.split("对话内容:\n")[-1].split("\n\n请只回答")[0]
    content_lower = content.lower()
    if "记住" in content_lower or "偏好" in content_lower:
        return "long_term"
    return "short_term"


class TestAgent(Agent):
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
# 测试1: FullMemoryStrategy
# =========================================================================

async def test_full_strategy():
    print("\n=== Test 1: FullMemoryStrategy ===")

    s = FullMemoryStrategy(agent_id="a1")
    check("策略名称", s.strategy_name == "full")

    # 存储消息
    await s.on_message("user", "你好")
    await s.on_message("assistant", "你好，有什么可以帮助你的？")
    await s.on_message("user", "帮我写代码")

    # 获取上下文
    ctx = await s.get_context()
    check("消息数量", len(ctx) == 3)
    check("第一条是user", ctx[0]["role"] == "user" and "你好" in ctx[0]["content"])
    check("第二条是assistant", ctx[1]["role"] == "assistant")

    # 快照
    snap = await s.snapshot()
    check("快照total", snap["total_messages"] == 3)
    check("快照user数", snap["user_messages"] == 2)

    # 清空
    await s.clear()
    ctx = await s.get_context()
    check("清空后为空", len(ctx) == 0)

    # FIFO 淘汰
    s2 = FullMemoryStrategy(agent_id="a2", config={"max_messages": 5})
    for i in range(10):
        await s2.on_message("user", f"msg-{i}")
    ctx = await s2.get_context()
    check("FIFO淘汰", len(ctx) == 5)
    check("保留最新", ctx[-1]["content"] == "msg-9")
    check("淘汰最早", ctx[0]["content"] == "msg-5")


# =========================================================================
# 测试2: SummaryMemoryStrategy
# =========================================================================

async def test_summary_strategy():
    print("\n=== Test 2: SummaryMemoryStrategy ===")

    # 无 LLM 时退化为全量
    s1 = SummaryMemoryStrategy(agent_id="a1", config={"buffer_size": 3})
    for i in range(5):
        await s1.on_message("user", f"msg-{i}")
    ctx = await s1.get_context()
    check("无LLM退化全量", len(ctx) == 3)  # 只返回最近 buffer_size 条
    check("无LLM无摘要", all(m["role"] != "system" for m in ctx))

    # 有 LLM 时生成摘要
    s2 = SummaryMemoryStrategy(
        agent_id="a2",
        config={"buffer_size": 3, "summary_interval": 5},
        llm_call=fake_llm_call,
    )
    for i in range(6):
        await s2.on_message("user", f"question-{i}")
        await s2.on_message("assistant", f"answer-{i}")

    ctx = await s2.get_context()
    # 应该有摘要 + 最近 buffer_size 条
    has_summary = any(m["role"] == "system" and "摘要" in m["content"] for m in ctx)
    check("LLM生成摘要", has_summary)
    check("摘要+近期消息", len(ctx) <= 4)  # 1 summary + 3 buffer

    # 会话结束触发最终摘要
    s3 = SummaryMemoryStrategy(
        agent_id="a3",
        config={"buffer_size": 10, "summary_interval": 100},
        llm_call=fake_llm_call,
    )
    await s3.on_message("user", "你好")
    await s3.on_message("assistant", "你好")
    await s3.on_session_end()
    ctx = await s3.get_context()
    has_summary = any(m["role"] == "system" for m in ctx)
    check("会话结束生成摘要", has_summary)

    # 快照
    snap = await s2.snapshot()
    check("快照有摘要", snap["has_summary"] is True)
    check("快照有LLM", snap["has_llm"] is True)


# =========================================================================
# 测试3: LSTMMemoryStrategy
# =========================================================================

async def test_lstm_strategy():
    print("\n=== Test 3: LSTMMemoryStrategy ===")

    s = LSTMMemoryStrategy(agent_id="a1", config={"keywords": ["记住", "偏好"]})

    # 短期消息
    await s.on_message("user", "帮我写一个排序函数")
    await s.on_message("assistant", "好的，这是冒泡排序的实现...")

    # 长期消息 (包含关键词)
    await s.on_message("user", "记住，我喜欢用 Python 3.10")
    await s.on_message("user", "以后编码偏好使用 type hints")

    # 验证分类
    lt = await s.get_long_term_memories()
    st = await s.get_short_term_memories()
    check("长期记忆数", len(lt) == 2, f"期望2，实际{len(lt)}")
    check("短期记忆数", len(st) == 2, f"期望2，实际{len(st)}")
    check("长期含偏好", any("偏好" in m["content"] for m in lt))
    check("长期含记住", any("记住" in m["content"] for m in lt))

    # get_context 包含长期记忆
    ctx = await s.get_context()
    has_lt_system = any(m["role"] == "system" and "长期记忆" in m["content"] for m in ctx)
    check("上下文含长期记忆", has_lt_system)
    # 短期记忆也在上下文中 (只有不含关键词的 user/assistant 消息在短期)
    user_msgs = [m for m in ctx if m["role"] == "user"]
    check("上下文含短期消息", len(user_msgs) == 1)  # 只有"帮我写一个排序函数"在短期

    # clear 只清短期
    await s.clear()
    st = await s.get_short_term_memories()
    lt = await s.get_long_term_memories()
    check("clear清空短期", len(st) == 0)
    check("clear保留长期", len(lt) == 2)

    # on_session_end 清空短期
    await s.on_message("user", "新的对话")
    await s.on_session_end()
    st = await s.get_short_term_memories()
    check("session_end清空短期", len(st) == 0)
    lt = await s.get_long_term_memories()
    check("session_end保留长期", len(lt) == 2)

    # LLM 分类模式
    s2 = LSTMMemoryStrategy(agent_id="a2", llm_call=fake_classify_llm)
    await s2.on_message("user", "帮我调试代码")
    await s2.on_message("user", "记住我的编程习惯")
    lt2 = await s2.get_long_term_memories()
    st2 = await s2.get_short_term_memories()
    check("LLM分类-长期", len(lt2) == 1)
    check("LLM分类-短期", len(st2) == 1)

    # 快照
    snap = await s.snapshot()
    check("快照long_term", snap["long_term_count"] == 2)
    check("快照keywords", snap["keywords_count"] == 2)


# =========================================================================
# 测试4: 自定义策略文件动态加载
# =========================================================================

async def test_custom_strategy_loading():
    print("\n=== Test 4: 自定义策略文件加载 ===")

    # 列出预置策略
    strategies = list_strategies()
    check("预置策略存在", "full" in strategies and "summary" in strategies and "lstm" in strategies)

    # 从文件路径加载
    custom_path = os.path.join(os.path.dirname(__file__), "custom_memory_strategy.py")
    s = create_strategy(custom_path, agent_id="a1", config={"prefix": ">>>"})
    check("加载成功", s is not None)
    check("策略名称", s.strategy_name == "only_user")

    # 验证功能
    await s.on_message("user", "你好")
    await s.on_message("assistant", "你好！")  # 应该被忽略
    await s.on_message("user", "再见")

    ctx = await s.get_context()
    check("只存user消息", len(ctx) == 2)
    check("自定义prefix", ctx[0]["content"].startswith(">>>"))

    # 无效策略名
    try:
        create_strategy("nonexistent", agent_id="a1")
        check("无效策略抛异常", False)
    except ValueError as e:
        check("无效策略抛异常", "未知记忆策略" in str(e))

    # 无效文件路径
    try:
        create_strategy("/nonexistent/path.py", agent_id="a1")
        check("无效文件抛异常", False)
    except (ValueError, ImportError):
        check("无效文件抛异常", True)


# =========================================================================
# 测试5: MemoryManager 统一接口
# =========================================================================

async def test_memory_manager():
    print("\n=== Test 5: MemoryManager ===")

    # 方式1: 预置策略名称
    m1 = MemoryManager(agent_id="a1", strategy="full")
    check("策略名称", m1.strategy_name == "full")

    # 方式2: 带 LLM
    m2 = MemoryManager(agent_id="a2", strategy="summary", llm_call=fake_llm_call)
    check("摘要策略", m2.strategy_name == "summary")

    # 方式3: 自定义文件
    custom_path = os.path.join(os.path.dirname(__file__), "custom_memory_strategy.py")
    m3 = MemoryManager(agent_id="a3", strategy=custom_path, config={"prefix": "##"})
    check("自定义策略", m3.strategy_name == "only_user")

    # 方式4: 直接传入实例
    s = LSTMMemoryStrategy(agent_id="a4")
    m4 = MemoryManager(agent_id="a4", strategy=s)
    check("实例传入", m4.strategy_name == "lstm")

    # 统一操作接口
    await m1.initialize()
    await m1.on_message("user", "测试消息")
    ctx = await m1.get_context()
    check("统一on_message", len(ctx) == 1)

    snap = await m1.snapshot()
    check("统一snapshot", snap["strategy"] == "full")

    await m1.clear()
    ctx = await m1.get_context()
    check("统一clear", len(ctx) == 0)


# =========================================================================
# 测试6: Agent 集成记忆策略
# =========================================================================

async def test_agent_with_strategies():
    print("\n=== Test 6: Agent + MemoryStrategy ===")

    # 6a: 默认策略 (full)
    config = AgentConfig(
        name="Agent-Full",
        metadata=AgentMetadata(role="test", tags=["test"]),
    )
    agent = TestAgent(config)
    await agent.initialize()
    check("默认策略=full", agent.memory.strategy_name == "full")

    result = await agent.run(task="写一个Hello World", task_id="t1")
    check("任务成功", result.success)
    check("输出正确", "Echo" in str(result.output))

    ctx = await agent.memory.get_context()
    check("记忆有内容", len(ctx) >= 2)
    await agent.destroy()

    # 6b: 通过 config 指定 lstm 策略
    config2 = AgentConfig(
        name="Agent-LSTM",
        memory_config=MemoryConfig(
            strategy="lstm",
            strategy_config={"keywords": ["记住", "重要"]},
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent2 = TestAgent(config2)
    await agent2.initialize()
    check("config指定策略", agent2.memory.strategy_name == "lstm")

    result2 = await agent2.run(task="记住我喜欢Python", task_id="t2")
    check("LSTM任务成功", result2.success)

    # 验证长期记忆
    strategy = agent2.memory.strategy
    if isinstance(strategy, LSTMMemoryStrategy):
        lt = await strategy.get_long_term_memories()
        check("长期记忆已分类", len(lt) >= 1)
    await agent2.destroy()

    # 6c: 通过构造参数覆盖策略
    config3 = AgentConfig(
        name="Agent-Summary",
        memory_config=MemoryConfig(strategy="full"),  # config 说是 full
        metadata=AgentMetadata(role="test"),
    )
    agent3 = TestAgent(config3, memory_strategy="summary", llm_call=fake_llm_call)
    await agent3.initialize()
    check("参数覆盖策略", agent3.memory.strategy_name == "summary")
    await agent3.destroy()

    # 6d: 自定义策略文件
    custom_path = os.path.join(os.path.dirname(__file__), "custom_memory_strategy.py")
    config4 = AgentConfig(
        name="Agent-Custom",
        memory_config=MemoryConfig(
            strategy=custom_path,
            strategy_config={"prefix": "[TEST]"},
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent4 = TestAgent(config4)
    await agent4.initialize()
    check("自定义策略加载", agent4.memory.strategy_name == "only_user")

    result4 = await agent4.run(task="自定义策略测试", task_id="t4")
    check("自定义策略任务成功", result4.success)

    # 自定义策略只存 user 消息
    ctx = await agent4.memory.get_context()
    check("自定义策略只存user", all(m["role"] == "user" for m in ctx))
    check("自定义prefix", ctx[0]["content"].startswith("[TEST]"))
    await agent4.destroy()


# =========================================================================
# 主入口
# =========================================================================

async def main():
    await test_full_strategy()
    await test_summary_strategy()
    await test_lstm_strategy()
    await test_custom_strategy_loading()
    await test_memory_manager()
    await test_agent_with_strategies()
    print("\n" + "=" * 50)
    print("  All memory strategy tests passed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
