"""
韧性模块 — 重试退避与熔断器 (M1: P0 可靠性)

提供生产级容错原语:
- ``retry_async``          — 通用异步重试执行器（指数/线性/固定退避）
- ``CircuitBreaker``       — 熔断器（CLOSED → OPEN → HALF_OPEN 状态机）
- ``CircuitBreakerRegistry`` — 按名称管理多个熔断器（LLM / 工具各一路）

设计原则:
- 纯 asyncio 单线程实现，状态变更为同步操作（无 await 间隙），天然原子
- 优雅降级: 熔断器可整体禁用；重试策略缺省时使用 ``RetryPolicy()`` 默认值
- 与上层解耦: 本模块只依赖 ``youmi.core.types``，不感知 LLM / MCP 细节

用法::

    from youmi.core.resilience import retry_async, get_breaker_registry

    breakers = get_breaker_registry()
    breaker = breakers.get("llm:gpt-4o")

    result = await breaker.call(
        retry_async, lambda: do_request(), policy=RetryPolicy(max_retries=3),
    )
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import random
import time
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, Field

from youmi.core.types import BackoffStrategy, RetryPolicy

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 工具调用默认超时（秒）— 统一超时兜底，可被 YOUMI_TOOL_TIMEOUT_S 覆盖
DEFAULT_TOOL_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    """熔断器状态"""

    CLOSED = "closed"        # 正常通行
    OPEN = "open"            # 已熔断，快速失败
    HALF_OPEN = "half_open"  # 半开试探（允许少量请求验证恢复）


class CircuitOpenError(RuntimeError):
    """熔断器打开时抛出 — 调用被快速拒绝"""

    def __init__(self, name: str, retry_after_s: float = 0.0) -> None:
        self.name = name
        self.retry_after_s = retry_after_s
        super().__init__(
            f"熔断器 '{name}' 已打开（连续失败触发保护），"
            f"约 {retry_after_s:.1f}s 后进入半开试探"
        )


class CircuitBreakerConfig(BaseModel):
    """熔断器配置

    Args:
        enabled: 是否启用（False 时恒定放行，用于关闭保护）
        failure_threshold: 连续失败次数达到该值后熔断
        recovery_timeout_s: 熔断后等待多久进入半开试探
        half_open_max_calls: 半开状态下允许的并发试探次数
    """

    enabled: bool = True
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_s: float = Field(default=30.0, gt=0.0)
    half_open_max_calls: int = Field(default=1, ge=1)

    model_config = {"frozen": True}


class CircuitBreaker:
    """熔断器 — 防止对高失败率目标的持续调用引发雪崩

    状态流转::

        CLOSED --连续失败>=threshold--> OPEN
        OPEN --recovery_timeout 到期--> HALF_OPEN（允许试探）
        HALF_OPEN --试探成功--> CLOSED
        HALF_OPEN --试探失败--> OPEN（重新计时）

    ``before_call()`` 在放行时返回 None；拒绝时抛出 ``CircuitOpenError``。
    调用方负责在完成后调用 ``record_success()`` / ``record_failure()``。
    """

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None) -> None:
        self._name = name
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0
        # 总额统计（诊断用）
        self._total_success = 0
        self._total_failure = 0
        self._rejected = 0

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    def before_call(self) -> None:
        """调用前检查

        Raises:
            CircuitOpenError: 熔断器处于 OPEN（或半开满载）状态
        """
        if not self._config.enabled:
            return

        if self._state == CircuitState.CLOSED:
            return

        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._config.recovery_timeout_s:
                # 进入半开试探
                self._state = CircuitState.HALF_OPEN
                self._half_open_in_flight = 0
                self._success_count = 0
                logger.info("CircuitBreaker '%s': OPEN → HALF_OPEN（开始试探）", self._name)
            else:
                self._rejected += 1
                raise CircuitOpenError(
                    self._name, self._config.recovery_timeout_s - elapsed,
                )

        # HALF_OPEN: 限制并发试探数
        if self._state == CircuitState.HALF_OPEN:
            if self._half_open_in_flight >= self._config.half_open_max_calls:
                self._rejected += 1
                raise CircuitOpenError(self._name, 0.0)
            self._half_open_in_flight += 1

    def record_success(self) -> None:
        """记录一次成功调用"""
        self._total_success += 1

        if not self._config.enabled:
            return

        if self._state == CircuitState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._success_count += 1
            # 试探成功 → 恢复通行
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            logger.info("CircuitBreaker '%s': HALF_OPEN → CLOSED（服务已恢复）", self._name)
            return

        self._failure_count = 0

    def record_failure(self) -> None:
        """记录一次失败调用"""
        self._total_failure += 1

        if not self._config.enabled:
            return

        if self._state == CircuitState.HALF_OPEN:
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            self._trip()
            return

        self._failure_count += 1
        if self._failure_count >= self._config.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        """触发熔断"""
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()  # monotonic 时钟（等效 loop.time，不依赖事件循环）
        logger.warning(
            "CircuitBreaker '%s': → OPEN（连续失败 %d 次，熔断 %.1fs）",
            self._name, self._failure_count, self._config.recovery_timeout_s,
        )

    async def call(
        self,
        fn: Callable[..., Awaitable[Any]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """在熔断器保护下执行异步函数

        Raises:
            CircuitOpenError: 熔断打开时快速失败
        """
        self.before_call()
        try:
            result = await fn(*args, **kwargs)
        except CircuitOpenError:
            # 内部嵌套熔断：不重复计数
            self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            raise
        except Exception:
            self.record_failure()
            raise
        else:
            self.record_success()
            return result

    # ------------------------------------------------------------------
    # 诊断 / 维护
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置为初始状态（测试与运维手动恢复用）"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_in_flight = 0

    def stats(self) -> dict[str, Any]:
        """熔断器统计信息"""
        return {
            "name": self._name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "total_success": self._total_success,
            "total_failure": self._total_failure,
            "rejected": self._rejected,
            "enabled": self._config.enabled,
        }

    def __repr__(self) -> str:
        return (
            f"<CircuitBreaker {self._name!r} state={self._state.value} "
            f"failures={self._failure_count}/{self._config.failure_threshold}>"
        )


class CircuitBreakerRegistry:
    """按名称管理多个熔断器

    典型命名: ``llm:<base_url>`` / ``tool:<tool_name>``。
    """

    def __init__(self, default_config: CircuitBreakerConfig | None = None) -> None:
        self._default_config = default_config or CircuitBreakerConfig()
        self._breakers: dict[str, CircuitBreaker] = {}

    @property
    def default_config(self) -> CircuitBreakerConfig:
        return self._default_config

    def get(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
    ) -> CircuitBreaker:
        """获取（或懒创建）指定名称的熔断器"""
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name, config or self._default_config)
            self._breakers[name] = breaker
        elif config is not None and not breaker.config.enabled and config.enabled:
            # 已存在但被禁用，而调用方要求启用 → 替换
            breaker = CircuitBreaker(name, config)
            self._breakers[name] = breaker
        return breaker

    def stats(self) -> list[dict[str, Any]]:
        """所有熔断器统计"""
        return [b.stats() for b in self._breakers.values()]

    def reset_all(self) -> None:
        """重置所有熔断器"""
        for breaker in self._breakers.values():
            breaker.reset()

    def __len__(self) -> int:
        return len(self._breakers)

    def __repr__(self) -> str:
        open_count = sum(
            1 for b in self._breakers.values() if b.state != CircuitState.CLOSED
        )
        return f"<CircuitBreakerRegistry breakers={len(self._breakers)} open={open_count}>"


# ---------------------------------------------------------------------------
# 模块级默认注册表（进程级共享）
# ---------------------------------------------------------------------------

_registry: CircuitBreakerRegistry | None = None


def get_breaker_registry() -> CircuitBreakerRegistry:
    """获取进程级默认熔断器注册表（懒初始化）"""
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry


def configure_breaker_registry(registry: CircuitBreakerRegistry) -> None:
    """替换进程级默认注册表（启动配置用）"""
    global _registry
    _registry = registry


def reset_breaker_registry() -> None:
    """清空进程级注册表（测试隔离用）"""
    global _registry
    _registry = None


# ---------------------------------------------------------------------------
# 重试退避
# ---------------------------------------------------------------------------

def compute_backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    jitter_ratio: float = 0.0,
) -> float:
    """计算第 ``attempt`` 次重试前的等待时间（attempt 从 1 开始）

    Args:
        attempt: 第几次重试（1 = 第一次重试）
        policy: 重试策略
        jitter_ratio: 随机抖动比例（0 = 无抖动），如 0.1 表示最多 +10%

    Returns:
        等待秒数（不超过 policy.max_delay_s）
    """
    if policy.backoff == BackoffStrategy.FIXED:
        delay = policy.base_delay_s
    elif policy.backoff == BackoffStrategy.LINEAR:
        delay = policy.base_delay_s * attempt
    else:  # EXPONENTIAL
        delay = policy.base_delay_s * (2 ** (attempt - 1))

    delay = min(delay, policy.max_delay_s)

    if jitter_ratio > 0 and delay > 0:
        delay += random.uniform(0.0, delay * jitter_ratio)

    return delay


def _is_retryable_by_policy(exc: BaseException, policy: RetryPolicy) -> bool:
    """按异常类名（含 MRO 全链）匹配 policy.retryable_exceptions"""
    names = policy.retryable_exceptions
    return any(cls.__name__ in names for cls in type(exc).__mro__)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    is_retryable: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[int, BaseException, float], Any] | None = None,
    op_name: str = "",
) -> T:
    """带退避重试的异步执行器

    Args:
        fn: 无参异步可调用对象（每次重试重新调用）
        policy: 重试策略，None 时使用 ``RetryPolicy()`` 默认值
        is_retryable: 自定义可重试判定（优先于 policy.retryable_exceptions）
        on_retry: 重试回调 ``(attempt, exc, delay_s)``，可为同步或异步函数
        op_name: 操作名称（日志用）

    Returns:
        fn 的返回值

    Raises:
        最后一次尝试的原始异常（重试耗尽或不可重试时）
    """
    policy = policy or RetryPolicy()
    label = op_name or getattr(fn, "__name__", "op")

    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except Exception as exc:
            retryable = (
                is_retryable(exc)
                if is_retryable is not None
                else _is_retryable_by_policy(exc, policy)
            )
            if not retryable or attempt > policy.max_retries:
                raise

            delay = compute_backoff_delay(attempt, policy)
            logger.warning(
                "%s: 第 %d/%d 次尝试失败（%s: %s），%.2fs 后重试",
                label, attempt, policy.max_retries + 1,
                type(exc).__name__, str(exc)[:200], delay,
            )

            if on_retry is not None:
                result = on_retry(attempt, exc, delay)
                if inspect.isawaitable(result):
                    await result

            if delay > 0:
                await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# 工具调用兜底超时
# ---------------------------------------------------------------------------

def get_tool_timeout_s() -> float:
    """工具调用统一超时兜底（秒）

    读取环境变量 ``YOUMI_TOOL_TIMEOUT_S``，默认 ``DEFAULT_TOOL_TIMEOUT_S``。
    工具自身的超时参数若更小，以更小者为准。
    """
    raw = os.environ.get("YOUMI_TOOL_TIMEOUT_S", "")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning("无效的 YOUMI_TOOL_TIMEOUT_S=%r，使用默认值", raw)
    return DEFAULT_TOOL_TIMEOUT_S
