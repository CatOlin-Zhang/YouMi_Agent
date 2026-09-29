"""
M1 可靠性模块测试 — 重试退避与熔断器

覆盖:
- compute_backoff_delay: 固定/线性/指数 + 上限截断
- retry_async: 重试成功 / 重试耗尽 / 不可重试快速失败 / 自定义判定 / on_retry 回调
- CircuitBreaker: 状态机流转（CLOSED→OPEN→HALF_OPEN→CLOSED/OPEN）
- CircuitBreakerRegistry: 懒创建 / 统计 / 重置
- 工具超时环境变量解析
"""

from __future__ import annotations

import asyncio
import os

import pytest

from youmi.core.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitOpenError,
    CircuitState,
    compute_backoff_delay,
    get_breaker_registry,
    get_tool_timeout_s,
    reset_breaker_registry,
    retry_async,
)
from youmi.core.types import BackoffStrategy, RetryPolicy


# ---------------------------------------------------------------------------
# 退避计算
# ---------------------------------------------------------------------------

class TestComputeBackoffDelay:
    def test_exponential(self):
        policy = RetryPolicy(
            base_delay_s=1.0, max_delay_s=60.0,
            backoff=BackoffStrategy.EXPONENTIAL,
        )
        assert compute_backoff_delay(1, policy) == 1.0
        assert compute_backoff_delay(2, policy) == 2.0
        assert compute_backoff_delay(3, policy) == 4.0

    def test_linear(self):
        policy = RetryPolicy(
            base_delay_s=2.0, max_delay_s=60.0,
            backoff=BackoffStrategy.LINEAR,
        )
        assert compute_backoff_delay(1, policy) == 2.0
        assert compute_backoff_delay(3, policy) == 6.0

    def test_fixed(self):
        policy = RetryPolicy(
            base_delay_s=5.0, max_delay_s=60.0,
            backoff=BackoffStrategy.FIXED,
        )
        assert compute_backoff_delay(1, policy) == 5.0
        assert compute_backoff_delay(4, policy) == 5.0

    def test_capped_at_max(self):
        policy = RetryPolicy(
            base_delay_s=10.0, max_delay_s=15.0,
            backoff=BackoffStrategy.EXPONENTIAL,
        )
        assert compute_backoff_delay(5, policy) == 15.0


# ---------------------------------------------------------------------------
# 重试执行器
# ---------------------------------------------------------------------------

class TestRetryAsync:
    async def test_success_after_retries(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("boom")
            return "ok"

        policy = RetryPolicy(max_retries=3, base_delay_s=0.0)
        result = await retry_async(flaky, policy=policy, op_name="test")
        assert result == "ok"
        assert calls["n"] == 3

    async def test_raises_after_exhausted(self):
        calls = {"n": 0}

        async def always_fail():
            calls["n"] += 1
            raise ConnectionError("boom")

        policy = RetryPolicy(max_retries=2, base_delay_s=0.0)
        with pytest.raises(ConnectionError):
            await retry_async(always_fail, policy=policy)
        # 1 次初始 + 2 次重试 = 3 次
        assert calls["n"] == 3

    async def test_non_retryable_raises_immediately(self):
        calls = {"n": 0}

        async def bad():
            calls["n"] += 1
            raise ValueError("not retryable")

        policy = RetryPolicy(max_retries=3, base_delay_s=0.0,
                             retryable_exceptions=["ConnectionError"])
        with pytest.raises(ValueError):
            await retry_async(bad, policy=policy)
        assert calls["n"] == 1

    async def test_custom_is_retryable(self):
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise ValueError("custom retryable")
            return 42

        result = await retry_async(
            flaky,
            policy=RetryPolicy(max_retries=1, base_delay_s=0.0),
            is_retryable=lambda exc: isinstance(exc, ValueError),
        )
        assert result == 42

    async def test_on_retry_callback(self):
        events: list[tuple] = []

        async def always_fail():
            raise ConnectionError("x")

        def on_retry(attempt, exc, delay):
            events.append((attempt, type(exc).__name__, delay))

        with pytest.raises(ConnectionError):
            await retry_async(
                always_fail,
                policy=RetryPolicy(max_retries=2, base_delay_s=0.0),
                on_retry=on_retry,
            )
        assert len(events) == 2
        assert events[0][0] == 1 and events[1][0] == 2

    async def test_mro_name_matching(self):
        """异常类按 MRO 名称匹配（TimeoutError 的子类可命中）"""

        class MyTimeoutError(TimeoutError):
            pass

        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise MyTimeoutError("t")
            return "done"

        result = await retry_async(
            flaky, policy=RetryPolicy(max_retries=1, base_delay_s=0.0),
        )
        assert result == "done"


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        breaker = CircuitBreaker(
            "t1", CircuitBreakerConfig(failure_threshold=3, recovery_timeout_s=60.0),
        )
        for _ in range(3):
            breaker.before_call()
            breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # 打开后快速失败
        with pytest.raises(CircuitOpenError) as ei:
            breaker.before_call()
        assert "t1" in str(ei.value)

    def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(
            "t2", CircuitBreakerConfig(failure_threshold=3),
        )
        breaker.before_call()
        breaker.record_failure()
        breaker.before_call()
        breaker.record_success()
        assert breaker.failure_count == 0
        assert breaker.state == CircuitState.CLOSED

    async def test_half_open_recovery(self):
        breaker = CircuitBreaker(
            "t3", CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=0.05),
        )
        breaker.before_call()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # 等待恢复窗口（0.2s ≫ 0.05s，避免 Windows 计时粒度抖动）
        await asyncio.sleep(0.2)

        # 半开试探 — 成功即恢复
        breaker.before_call()
        assert breaker.state == CircuitState.HALF_OPEN
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    async def test_half_open_failure_reopens(self):
        breaker = CircuitBreaker(
            "t4", CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=0.05),
        )
        breaker.before_call()
        breaker.record_failure()

        await asyncio.sleep(0.2)
        breaker.before_call()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    def test_disabled_always_allows(self):
        breaker = CircuitBreaker("t5", CircuitBreakerConfig(enabled=False,
                                                            failure_threshold=1))
        for _ in range(10):
            breaker.before_call()
            breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        breaker.before_call()  # 不抛异常

    async def test_call_wrapper(self):
        breaker = CircuitBreaker(
            "t6", CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0),
        )

        async def fail():
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await breaker.call(fail)
        with pytest.raises(ConnectionError):
            await breaker.call(fail)
        assert breaker.state == CircuitState.OPEN

        # 熔断后 call 直接快速失败，不再执行函数
        executed = {"n": 0}

        async def probe():
            executed["n"] += 1
            return 1

        with pytest.raises(CircuitOpenError):
            await breaker.call(probe)
        assert executed["n"] == 0

    async def test_half_open_max_calls(self):
        breaker = CircuitBreaker(
            "t7",
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=0.05,
                                 half_open_max_calls=1),
        )
        breaker.before_call()
        breaker.record_failure()
        await asyncio.sleep(0.2)

        breaker.before_call()  # 第一个试探名额占用
        with pytest.raises(CircuitOpenError):
            breaker.before_call()  # 第二个被拒绝
        breaker.record_success()

    def test_stats(self):
        breaker = CircuitBreaker("t8", CircuitBreakerConfig(failure_threshold=5))
        breaker.before_call()
        breaker.record_success()
        stats = breaker.stats()
        assert stats["name"] == "t8"
        assert stats["state"] == "closed"
        assert stats["total_success"] == 1


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_get_same_instance(self):
        registry = CircuitBreakerRegistry()
        b1 = registry.get("llm:x")
        b2 = registry.get("llm:x")
        assert b1 is b2
        assert len(registry) == 1

    def test_reset_all(self):
        registry = CircuitBreakerRegistry(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=60.0),
        )
        breaker = registry.get("tool:a")
        breaker.before_call()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        registry.reset_all()
        assert breaker.state == CircuitState.CLOSED

    def test_stats_list(self):
        registry = CircuitBreakerRegistry()
        registry.get("a")
        registry.get("b")
        assert len(registry.stats()) == 2

    def test_process_default_registry(self):
        reset_breaker_registry()
        r1 = get_breaker_registry()
        r2 = get_breaker_registry()
        assert r1 is r2
        reset_breaker_registry()
        assert get_breaker_registry() is not r1


# ---------------------------------------------------------------------------
# 工具超时
# ---------------------------------------------------------------------------

class TestToolTimeout:
    def test_default(self):
        old = os.environ.pop("YOUMI_TOOL_TIMEOUT_S", None)
        try:
            assert get_tool_timeout_s() == 180.0
        finally:
            if old is not None:
                os.environ["YOUMI_TOOL_TIMEOUT_S"] = old

    def test_env_override(self):
        os.environ["YOUMI_TOOL_TIMEOUT_S"] = "30"
        try:
            assert get_tool_timeout_s() == 30.0
        finally:
            os.environ.pop("YOUMI_TOOL_TIMEOUT_S", None)

    def test_invalid_env_falls_back(self):
        os.environ["YOUMI_TOOL_TIMEOUT_S"] = "abc"
        try:
            assert get_tool_timeout_s() == 180.0
        finally:
            os.environ.pop("YOUMI_TOOL_TIMEOUT_S", None)
