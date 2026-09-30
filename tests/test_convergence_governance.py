"""
收敛治理与 LLM 流式分支测试

覆盖:
- _inject_convergence_reminders:
  - 连续 3 次工具失败 → 注入 user 提醒 + 计数重置（每 3 次一档）
  - 成功调用清零失败计数
  - 迭代预算尾声（剩余 ≤ 2 轮）→ 注入收尾提醒，全程仅一次
- _call_llm（Agent 统一 LLM 调用入口）:
  - 设置流式监听器且客户端支持 chat_stream → 走流式并逐块转发
  - 流式失败 → 自动回退非流式 chat
  - 未设置监听器 → 行为与非流式完全一致
  - 监听器异常不打断流（展示层增强不改变 ReAct 语义）

背景: 子 Agent 调研任务曾出现「无限调研不收敛」与
「GUI 全程无流式输出」两个问题——前者由收敛提醒治理，
后者根因是 gpt-oss 思考内容在 delta.reasoning 而客户端只取 content。
"""

from __future__ import annotations

import pytest

from youmi.core.agent import Agent, AgentConfig
from youmi.core.models import _ActionResult
from youmi.core.types import LLMConfig
from youmi.llm.client import LLMResponse


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------

def _make_agent(max_iterations: int = 20) -> Agent:
    """最小 Agent 实例（无需 initialize，直接驱动内部方法）"""
    config = AgentConfig(
        name="ConvAgent",
        system_prompt="测试 Agent",
        llm_config=LLMConfig(model="mock-model"),
        max_iterations=max_iterations,
    )
    return Agent(config)


class _StreamLLMClient:
    """支持 chat_stream 的最小 LLM 客户端桩

    - chunks: (kind, text) 序列，流式时逐块经 on_delta 转发并 yield
    - fail: True 时 chat_stream 抛异常（触发回退路径）
    - 流结束后设置 _last_stream_response（与真实 LLMClient 行为一致）
    """

    def __init__(self, chunks=None, fail: bool = False) -> None:
        self.chunks = chunks or []
        self.fail = fail
        self._last_stream_response = None
        self.chat_calls = 0
        self.stream_calls = 0

    async def chat(self, messages, tools=None, tool_choice=None, **extra):
        self.chat_calls += 1
        return LLMResponse({
            "choices": [{
                "message": {"role": "assistant", "content": "非流式回退"},
                "finish_reason": "stop",
            }],
        })

    async def chat_stream(self, messages, tools=None, tool_choice=None,
                          on_delta=None, **extra):
        self.stream_calls += 1
        if self.fail:
            raise RuntimeError("stream boom")
        for kind, text in self.chunks:
            if on_delta is not None:
                await on_delta(kind, text)
            yield text
        self._last_stream_response = LLMResponse({
            "choices": [{
                "message": {"role": "assistant", "content": "流式结果"},
                "finish_reason": "stop",
            }],
        })


# ---------------------------------------------------------------------------
# 收敛治理: 连续工具失败提醒
# ---------------------------------------------------------------------------

class TestConvergenceToolFailures:

    async def test_three_consecutive_failures_inject_reminder(self):
        agent = _make_agent()
        fail = _ActionResult(success=False, error="boom")

        # 1-2 次失败不注入
        agent._inject_convergence_reminders(fail)
        agent._inject_convergence_reminders(fail)
        assert agent._conversation == []

        # 第 3 次失败 → 注入 user 提醒
        agent._inject_convergence_reminders(fail)
        assert len(agent._conversation) == 1
        msg = agent._conversation[-1]
        assert msg["role"] == "user"
        assert "连续 3 次" in msg["content"]
        assert "最终结果" in msg["content"]

        # 计数重置: 再失败 2 次不注入，第 3 次再次注入（每 3 次一档）
        agent._inject_convergence_reminders(fail)
        agent._inject_convergence_reminders(fail)
        assert len(agent._conversation) == 1
        agent._inject_convergence_reminders(fail)
        assert len(agent._conversation) == 2

    async def test_success_resets_failure_count(self):
        agent = _make_agent()
        fail = _ActionResult(success=False, error="boom")
        ok = _ActionResult(success=True, output="done")

        agent._inject_convergence_reminders(fail)
        agent._inject_convergence_reminders(fail)
        agent._inject_convergence_reminders(ok)  # 成功清零
        agent._inject_convergence_reminders(fail)
        agent._inject_convergence_reminders(fail)
        # 失败被打断，累计仅 2 次 → 不注入
        assert agent._conversation == []


# ---------------------------------------------------------------------------
# 收敛治理: 迭代预算尾声提醒
# ---------------------------------------------------------------------------

class TestConvergenceBudgetReminder:

    async def test_budget_tail_reminder_injected_once(self):
        agent = _make_agent(max_iterations=10)
        ok = _ActionResult(success=True)
        agent._iteration_count = 8  # remaining = 2

        agent._inject_convergence_reminders(ok)
        assert len(agent._conversation) == 1
        content = agent._conversation[-1]["content"]
        assert "8/10" in content
        assert "剩余轮次不多" in content

        # 只注入一次: 后续轮次不再重复
        agent._iteration_count = 9
        agent._inject_convergence_reminders(ok)
        assert len(agent._conversation) == 1

    async def test_no_reminder_when_budget_plenty(self):
        agent = _make_agent(max_iterations=20)
        agent._iteration_count = 5  # remaining = 15

        agent._inject_convergence_reminders(_ActionResult(success=True))
        assert agent._conversation == []

    async def test_no_reminder_when_task_finishing(self):
        """respond 轮（任务即将结束）不注入 — 避免污染 conversation

        回归保护: 曾在 max_iterations=5、第 3 轮 respond 后因 remaining=2
        误注入，导致 test_multi_tool_chain conversation 长度 7→8。
        """
        agent = _make_agent(max_iterations=5)
        agent._iteration_count = 3  # remaining = 2，但本轮已 respond

        agent._inject_convergence_reminders(
            _ActionResult(success=True), will_continue=False,
        )
        assert agent._conversation == []
        # 失败计数触发器同样受门控
        agent._inject_convergence_reminders(
            _ActionResult(success=False, error="boom"), will_continue=False,
        )
        assert agent._conversation == []

        # 门控期间不累积失败计数: 解除门控后需重新累计 3 次才注入
        # （独立 Agent，预算充足，避开预算尾声触发器干扰）
        agent2 = _make_agent(max_iterations=20)
        agent2._inject_convergence_reminders(
            _ActionResult(success=False, error="boom"), will_continue=False,
        )
        agent2._inject_convergence_reminders(
            _ActionResult(success=False, error="boom"), will_continue=False,
        )
        agent2._inject_convergence_reminders(_ActionResult(success=False, error="x"))
        agent2._inject_convergence_reminders(_ActionResult(success=False, error="x"))
        assert agent2._conversation == []  # 门控的 2 次 + 累计 2 次，均未达阈值
        agent2._inject_convergence_reminders(_ActionResult(success=False, error="x"))
        assert len(agent2._conversation) == 1  # 第 3 次累计才注入
        assert "连续 3 次" in agent2._conversation[-1]["content"]


# ---------------------------------------------------------------------------
# _call_llm: 流式分支与回退
# ---------------------------------------------------------------------------

class TestCallLlmStreaming:

    async def test_stream_preferred_when_listener_set(self):
        agent = _make_agent()
        client = _StreamLLMClient(
            chunks=[("reasoning", "思考"), ("content", "回答")],
        )
        agent._llm_client = client

        received = []

        async def listener(kind: str, text: str) -> None:
            received.append((kind, text))

        agent.set_llm_stream_listener(listener)
        resp = await agent._call_llm(
            [{"role": "user", "content": "hi"}], None,
        )

        # 走流式路径，delta 逐块转发给监听器
        assert client.stream_calls == 1
        assert client.chat_calls == 0
        assert received == [("reasoning", "思考"), ("content", "回答")]
        # 返回流式聚合响应
        assert resp.content == "流式结果"

    async def test_fallback_to_chat_on_stream_failure(self):
        agent = _make_agent()
        client = _StreamLLMClient(fail=True)
        agent._llm_client = client

        async def listener(kind: str, text: str) -> None:
            pass

        agent.set_llm_stream_listener(listener)
        resp = await agent._call_llm(
            [{"role": "user", "content": "hi"}], None,
        )

        # 流式失败一次后回退非流式
        assert client.stream_calls == 1
        assert client.chat_calls == 1
        assert resp.content == "非流式回退"

    async def test_plain_chat_without_listener(self):
        agent = _make_agent()
        client = _StreamLLMClient()
        agent._llm_client = client

        resp = await agent._call_llm(
            [{"role": "user", "content": "hi"}], None,
        )

        # 未设置监听器 → 直接非流式，行为不变
        assert client.stream_calls == 0
        assert client.chat_calls == 1
        assert resp.content == "非流式回退"

    async def test_listener_exception_does_not_break_stream(self):
        agent = _make_agent()
        client = _StreamLLMClient(
            chunks=[("reasoning", "a"), ("content", "b")],
        )
        agent._llm_client = client

        async def bad_listener(kind: str, text: str) -> None:
            raise ValueError("listener boom")

        agent.set_llm_stream_listener(bad_listener)
        resp = await agent._call_llm(
            [{"role": "user", "content": "hi"}], None,
        )

        # 监听器异常被吞掉，流正常完成
        assert client.stream_calls == 1
        assert client.chat_calls == 0
        assert resp.content == "流式结果"
