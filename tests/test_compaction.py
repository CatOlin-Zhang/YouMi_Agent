"""上下文压缩引擎测试 (P0: Compaction)

测试覆盖:
1. estimate_tokens / estimate_messages_tokens — token 估算
2. ContextCompactor.needs_compaction — 阈值判断
3. ContextCompactor.maybe_compact — 有 LLM 摘要压缩
4. ContextCompactor.maybe_compact — 无 LLM 硬截断 fallback
5. 增量压缩 — 多次压缩时摘要合并
6. snapshot / reset — 状态管理
7. Agent._observe() 集成 compactor
"""

import asyncio

from youmi.memory.compaction import (
    ContextCompactor,
    estimate_tokens,
    estimate_messages_tokens,
    DEFAULT_COMPACTION_PROMPT,
)
from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought
from youmi.core.types import (
    AgentMetadata,
    CompactionConfig,
    LLMConfig,
    MemoryConfig,
)


# =========================================================================
# 辅助工具
# =========================================================================

async def fake_llm_call(messages: list[dict[str, str]]) -> str:
    """模拟 LLM 摘要 — 返回固定摘要"""
    return "对话摘要: 用户讨论了代码问题，助手提供了建议。"


async def failing_llm_call(messages: list[dict[str, str]]) -> str:
    """模拟 LLM 调用失败"""
    raise RuntimeError("LLM 调用失败")


def make_messages(n: int, content_len: int = 100) -> list[dict[str, str]]:
    """生成 n 条测试消息, 每条 content 约 content_len 字符"""
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"msg-{i}-" + "x" * (content_len - 6)})
    return msgs


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
# 测试1: token 估算
# =========================================================================

async def test_token_estimation():
    print("\n=== Test 1: Token Estimation ===")

    # 空字符串
    check("空字符串", estimate_tokens("") == 0)

    # 纯英文
    tokens = estimate_tokens("hello world this is a test")
    check("英文估算合理", 5 <= tokens <= 10, f"got {tokens}")

    # 纯中文 (10字 / 3.5 ≈ 2 tokens)
    tokens_cn = estimate_tokens("你好世界这是一个测试")
    check("中文估算合理", 2 <= tokens_cn <= 10, f"got {tokens_cn}")

    # 长中文 (50字 → ~14 tokens)
    tokens_cn_long = estimate_tokens("你好世界" * 12 + "测试")  # 50 chars
    check("长中文估算合理", 10 <= tokens_cn_long <= 20, f"got {tokens_cn_long}")

    # 消息列表估算
    msgs = make_messages(10, content_len=100)
    total = estimate_messages_tokens(msgs)
    # 10 条消息, 每条 100 字符 → 约 285 tokens/条 + 4 开销 → ~2900
    check("消息列表估算合理", 200 <= total <= 500, f"got {total}")

    # tool_calls 额外开销
    msgs_with_tools = [
        {"role": "assistant", "content": "ok", "tool_calls": [{"id": "c1", "function": {"name": "test", "arguments": "{}"}}]},
    ]
    t1 = estimate_messages_tokens(msgs_with_tools)
    t2 = estimate_messages_tokens([{"role": "assistant", "content": "ok"}])
    check("tool_calls增加开销", t1 > t2)


# =========================================================================
# 测试2: needs_compaction 阈值判断
# =========================================================================

async def test_needs_compaction():
    print("\n=== Test 2: needs_compaction ===")

    # max_tokens=0 → 永不压缩
    c0 = ContextCompactor(max_context_tokens=0)
    check("max=0不压缩", c0.needs_compaction(make_messages(100)) is False)

    # 少量消息不触发
    c1 = ContextCompactor(max_context_tokens=8000, reserve_ratio=0.8)
    small_msgs = make_messages(3, content_len=50)
    check("少量不触发", c1.needs_compaction(small_msgs) is False)

    # 大量消息触发
    big_msgs = make_messages(50, content_len=500)
    check("大量触发", c1.needs_compaction(big_msgs) is True)

    # 刚好在阈值附近
    # reserve_ratio=0.5, max=100 → 阈值50
    c2 = ContextCompactor(max_context_tokens=100, reserve_ratio=0.5)
    # 构造刚好超阈值的消息
    msgs = [{"role": "user", "content": "a" * 200}]  # ~57 tokens + 4 = ~61
    check("超阈值触发", c2.needs_compaction(msgs) is True)

    # 低于阈值
    small = [{"role": "user", "content": "hi"}]  # ~1 + 4 = 5 tokens
    check("低于阈值不触发", c2.needs_compaction(small) is False)


# =========================================================================
# 测试3: maybe_compact — 有 LLM 摘要
# =========================================================================

async def test_compact_with_llm():
    print("\n=== Test 3: maybe_compact with LLM ===")

    c = ContextCompactor(
        max_context_tokens=100,
        reserve_ratio=0.5,  # 阈值 50 tokens
        keep_recent=2,
        llm_call=fake_llm_call,
    )

    # 构造超阈值的消息 (6 条, 每条 ~30 tokens)
    msgs = make_messages(6, content_len=100)
    check("压缩前需要压缩", c.needs_compaction(msgs) is True)

    result = await c.maybe_compact(msgs)

    # 验证: 结果应有 system 摘要 + 最近 2 条
    check("压缩后消息减少", len(result) < len(msgs), f"{len(result)} < {len(msgs)}")

    # 检查摘要 system 消息
    system_msgs = [m for m in result if m["role"] == "system"]
    check("有摘要system", len(system_msgs) >= 1)
    check("摘要内容", "摘要" in system_msgs[-1]["content"])

    # 保留最近 2 条
    non_system = [m for m in result if m["role"] != "system"]
    check("保留最近消息", len(non_system) == 2, f"got {len(non_system)}")

    # 压缩计数
    check("压缩计数=1", c.compaction_count == 1)
    check("有摘要", c.current_summary is not None)


# =========================================================================
# 测试4: maybe_compact — 无 LLM 硬截断
# =========================================================================

async def test_compact_without_llm():
    print("\n=== Test 4: maybe_compact without LLM (hard truncation) ===")

    c = ContextCompactor(
        max_context_tokens=100,
        reserve_ratio=0.5,
        keep_recent=3,
        llm_call=None,  # 无 LLM
    )

    msgs = make_messages(8, content_len=100)
    result = await c.maybe_compact(msgs)

    # 硬截断: 只保留 system (无) + 最近 3 条
    check("截断后减少", len(result) < len(msgs))
    check("保留最近3条", len(result) == 3, f"got {len(result)}")

    # 无摘要
    check("无摘要", c.current_summary is None)
    check("压缩计数=1", c.compaction_count == 1)


# =========================================================================
# 测试5: LLM 失败 fallback 到硬截断
# =========================================================================

async def test_compact_llm_failure():
    print("\n=== Test 5: LLM failure fallback ===")

    c = ContextCompactor(
        max_context_tokens=100,
        reserve_ratio=0.5,
        keep_recent=2,
        llm_call=failing_llm_call,
    )

    msgs = make_messages(6, content_len=100)
    result = await c.maybe_compact(msgs)

    # 失败后 fallback 到硬截断
    check("失败后截断", len(result) < len(msgs))
    non_system = [m for m in result if m["role"] != "system"]
    check("保留最近2条", len(non_system) == 2, f"got {len(non_system)}")


# =========================================================================
# 测试6: 增量压缩 — 多次压缩摘要合并
# =========================================================================

async def test_incremental_compaction():
    print("\n=== Test 6: Incremental compaction ===")

    c = ContextCompactor(
        max_context_tokens=100,
        reserve_ratio=0.5,
        keep_recent=2,
        llm_call=fake_llm_call,
    )

    # 第一次压缩
    msgs1 = make_messages(6, content_len=100)
    result1 = await c.maybe_compact(msgs1)
    check("第一次压缩", c.compaction_count == 1)
    summary1 = c.current_summary

    # 第二次压缩 (模拟新的对话追加)
    # 在压缩后的结果上追加新消息
    new_msgs = result1 + make_messages(6, content_len=100)
    result2 = await c.maybe_compact(new_msgs)
    check("第二次压缩", c.compaction_count == 2)

    # 摘要应该更新 (增量合并)
    check("摘要更新", c.current_summary is not None)
    # 第二次压缩时使用了第一次的摘要作为 previous_summary
    # (fake_llm_call 返回固定字符串，所以内容一样但确认没报错)


# =========================================================================
# 测试7: snapshot / reset
# =========================================================================

async def test_snapshot_reset():
    print("\n=== Test 7: snapshot & reset ===")

    c = ContextCompactor(
        max_context_tokens=8000,
        reserve_ratio=0.8,
        keep_recent=10,
        llm_call=fake_llm_call,
    )

    snap = c.snapshot()
    check("初始max_tokens", snap["max_tokens"] == 8000)
    check("初始压缩次数", snap["compaction_count"] == 0)
    check("初始无摘要", snap["has_summary"] is False)

    # 触发压缩
    msgs = make_messages(50, content_len=500)
    await c.maybe_compact(msgs)

    snap2 = c.snapshot()
    check("压缩后次数>0", snap2["compaction_count"] > 0)
    check("压缩后有摘要", snap2["has_summary"] is True)

    # 重置
    c.reset()
    snap3 = c.snapshot()
    check("重置后次数=0", snap3["compaction_count"] == 0)
    check("重置后无摘要", snap3["has_summary"] is False)


# =========================================================================
# 测试8: 不需要压缩时直接返回原消息
# =========================================================================

async def test_no_compaction_needed():
    print("\n=== Test 8: No compaction needed ===")

    c = ContextCompactor(
        max_context_tokens=8000,
        reserve_ratio=0.8,
        keep_recent=10,
        llm_call=fake_llm_call,
    )

    msgs = make_messages(3, content_len=50)
    result = await c.maybe_compact(msgs)

    check("不压缩时返回原列表", result is msgs)
    check("压缩次数=0", c.compaction_count == 0)


# =========================================================================
# 测试9: Agent 集成 compactor
# =========================================================================

async def test_agent_compactor_integration():
    print("\n=== Test 9: Agent + Compactor integration ===")

    # 9a: compaction 启用时 Agent 有 compactor
    config = AgentConfig(
        name="CompactAgent",
        llm_config=LLMConfig(max_context_tokens=8000),
        memory_config=MemoryConfig(
            compaction=CompactionConfig(enabled=True, reserve_ratio=0.8, keep_recent=5),
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent = EchoAgent(config, llm_call=fake_llm_call)
    await agent.initialize()

    check("compactor已创建", agent.compactor is not None)
    check("compactor类型", isinstance(agent.compactor, ContextCompactor))

    result = await agent.run(task="测试压缩功能", task_id="t1")
    check("任务成功", result.success)
    await agent.destroy()

    # 9b: compaction 禁用时 Agent 无 compactor
    config2 = AgentConfig(
        name="NoCompactAgent",
        memory_config=MemoryConfig(
            compaction=CompactionConfig(enabled=False),
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent2 = EchoAgent(config2)
    await agent2.initialize()

    check("compactor为None", agent2.compactor is None)

    result2 = await agent2.run(task="不压缩", task_id="t2")
    check("无压缩任务成功", result2.success)
    await agent2.destroy()


# =========================================================================
# 主入口
# =========================================================================

async def main():
    await test_token_estimation()
    await test_needs_compaction()
    await test_compact_with_llm()
    await test_compact_without_llm()
    await test_compact_llm_failure()
    await test_incremental_compaction()
    await test_snapshot_reset()
    await test_no_compaction_needed()
    await test_agent_compactor_integration()
    print("\n" + "=" * 50)
    print("  All compaction tests passed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
