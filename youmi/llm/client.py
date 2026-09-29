"""
LLM 客户端

通过 HTTP 调用 OpenAI 兼容 API (支持 tool_calls / function calling)。
使用 httpx 作为异步 HTTP 客户端，兼容:
- OpenAI API (https://api.openai.com/v1)
- Anthropic 兼容代理
- 本地部署 (Ollama / vLLM / llama.cpp server)
- 任意 OpenAI Chat Completions 兼容接口

用法::

    from youmi.llm import LLMClient
    from youmi.core.types import LLMConfig

    config = LLMConfig(model="gpt-4o", api_key="sk-...")
    client = LLMClient(config)

    # 普通对话
    response = await client.chat(messages=[{"role": "user", "content": "你好"}])

    # 带工具调用
    response = await client.chat(messages=[...], tools=[...])

    await client.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from youmi.core.resilience import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    compute_backoff_delay,
    get_breaker_registry,
    retry_async,
)
from youmi.core.types import LLMConfig, LLMProvider, RetryPolicy
from youmi.observability import (
    AuditLogger,
    get_audit_logger,
    set_span_attributes,
    span,
)

logger = logging.getLogger(__name__)

# 可重试的 HTTP 状态码 — 限流(429) / 超时(408) / 服务端临时故障(5xx)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# 响应数据结构
# ---------------------------------------------------------------------------

class LLMResponse:
    """LLM 响应封装

    统一封装不同 provider 的返回格式，提供一致的访问接口。
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self._message = raw.get("choices", [{}])[0].get("message", {})

    @property
    def content(self) -> str:
        """文本回复内容 (可能为空，当有 tool_calls 时)"""
        return self._message.get("content", "") or ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """工具调用请求列表

        格式::
            [
                {
                    "id": "call_xxx",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": "{\"city\": \"北京\"}"
                    }
                }
            ]
        """
        return self._message.get("tool_calls", []) or []

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def finish_reason(self) -> str:
        return self._raw.get("choices", [{}])[0].get("finish_reason", "")

    @property
    def usage(self) -> dict[str, int]:
        return self._raw.get("usage", {})

    @property
    def raw_message(self) -> dict[str, Any]:
        """原始 message 字典 (可直接追加到 messages 列表)"""
        # 某些 API (Ollama/MiniMax) 要求 tool_calls 时 content 为 null 而非空字符串
        content = self.content
        msg: dict[str, Any] = {"role": "assistant", "content": content if content else None}
        if self.has_tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg

    @property
    def raw(self) -> dict[str, Any]:
        return self._raw

    def __repr__(self) -> str:
        if self.has_tool_calls:
            names = [tc["function"]["name"] for tc in self.tool_calls]
            return f"<LLMResponse tool_calls={names}>"
        return f"<LLMResponse content={self.content[:50]!r}...>"


# ---------------------------------------------------------------------------
# LLM 客户端
# ---------------------------------------------------------------------------

class LLMClient:
    """异步 LLM HTTP 客户端

    支持 OpenAI Chat Completions API 格式，包括 function calling。

    M1 增强:
    - 可靠性: 指数退避重试（``RetryPolicy``）+ 熔断保护（按 base_url 隔离）
    - 可观测性: 每次调用产生 OTel span（``llm.chat`` / ``llm.chat_stream``）
      并写入审计日志（``llm_call`` 事件）

    Args:
        config: LLM 连接配置
        retry_policy: 重试策略（None 时使用 config.retry_policy 或默认值）
        breaker_registry: 熔断器注册表（None 时使用进程级默认注册表）
        audit: 审计记录器（None 时使用进程级默认实例）
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        retry_policy: RetryPolicy | None = None,
        breaker_registry: CircuitBreakerRegistry | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._config = config
        self._base_url = self._resolve_base_url(config)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._build_headers(config),
            timeout=httpx.Timeout(config.timeout_s),
        )
        # M1: 可靠性 — 重试策略（显式参数 > 配置 > 默认值）
        self._retry_policy: RetryPolicy = (
            retry_policy if retry_policy is not None
            else config.retry_policy if config.retry_policy is not None
            else RetryPolicy()
        )
        # 注意: CircuitBreakerRegistry 定义了 __len__，空实例为 falsy，
        # 必须用 is not None 判断而非 or
        self._breakers = (
            breaker_registry if breaker_registry is not None
            else get_breaker_registry()
        )
        self._breaker: CircuitBreaker = self._breakers.get(f"llm:{self._base_url}")
        # M1: 可观测性 — 审计记录器
        self._audit: AuditLogger = (
            audit if audit is not None else get_audit_logger()
        )

    @staticmethod
    def _resolve_base_url(config: LLMConfig) -> str:
        """解析 API 基础 URL"""
        if config.base_url:
            return config.base_url.rstrip("/")
        if config.provider == LLMProvider.OPENAI:
            return "https://api.openai.com/v1"
        if config.provider == LLMProvider.ANTHROPIC:
            return "https://api.anthropic.com/v1"
        return "https://api.openai.com/v1"

    @staticmethod
    def _build_headers(config: LLMConfig) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        headers.update(config.extra_headers)
        return headers

    # ------------------------------------------------------------------
    # M1 辅助: 请求构建 / 重试判定 / 审计
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict | None,
        extra_params: dict[str, Any],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """构建 Chat Completions 请求体"""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        if stream:
            payload["stream"] = True

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"

        payload.update(self._config.extra_params)
        payload.update(extra_params)
        return payload

    @staticmethod
    def _is_retryable_llm_error(exc: BaseException) -> bool:
        """判定异常是否值得重试（限流 / 服务端临时故障 / 网络层错误）"""
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in _RETRYABLE_STATUS_CODES
        if isinstance(exc, httpx.TransportError):
            return True
        return False

    def _audit_llm_call(
        self,
        *,
        success: bool,
        duration_ms: float,
        attempts: int,
        tokens: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        """写入 LLM 调用审计（失败仅告警，不影响主流程）"""
        try:
            self._audit.log_llm_call(
                self._config.model,
                provider=self._config.provider.value,
                success=success,
                duration_ms=round(duration_ms, 2),
                tokens=tokens or {},
                attempts=attempts,
                error=error[:500],
            )
        except Exception as exc:
            logger.debug("LLM 审计写入失败: %s", exc)

    # ------------------------------------------------------------------
    # 核心调用
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        **extra_params: Any,
    ) -> LLMResponse:
        """调用 Chat Completions API

        M1 增强: 指数退避重试 / 熔断保护 / OTel span / 审计日志。

        Args:
            messages: OpenAI 格式的消息列表
            tools: 工具定义列表 (OpenAI tools 格式)，传入即启用 function calling
            tool_choice: 工具选择策略 ("auto" / "none" / {"type": "function", "function": {"name": "..."}})
            **extra_params: 其他 API 参数

        Returns:
            LLMResponse 封装的响应

        Raises:
            CircuitOpenError: 熔断器打开（连续失败触发保护）
            Exception: 重试耗尽后的原始异常
        """
        payload = self._build_payload(messages, tools, tool_choice, extra_params)

        logger.debug("LLM request: model=%s messages=%d tools=%d",
                      self._config.model, len(messages), len(tools or []))

        attempts = 0

        def _on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            nonlocal attempts
            attempts = attempt

        async def _do_post() -> dict[str, Any]:
            response = await self._client.post("/chat/completions", json=payload)
            response.raise_for_status()
            return response.json()

        # 熔断检查 — 整轮（含重试）视为一次调用；拒绝时快速失败不计入统计
        self._breaker.before_call()

        start = time.monotonic()
        with span("llm.chat", attributes={
            "llm.model": self._config.model,
            "llm.provider": self._config.provider.value,
            "llm.base_url": self._base_url,
            "llm.messages_count": len(messages),
            "llm.tools_count": len(tools or []),
        }) as sp:
            try:
                data = await retry_async(
                    _do_post,
                    policy=self._retry_policy,
                    is_retryable=self._is_retryable_llm_error,
                    on_retry=_on_retry,
                    op_name=f"LLM[{self._config.model}]",
                )
            except Exception as exc:
                # 服务不可用类错误计入熔断；业务错误（400 等）不触发保护
                if self._is_retryable_llm_error(exc):
                    self._breaker.record_failure()
                else:
                    self._breaker.record_success()
                duration_ms = (time.monotonic() - start) * 1000.0
                set_span_attributes(sp, {
                    "youmi.success": False,
                    "youmi.attempts": attempts + 1,
                    "youmi.duration_ms": round(duration_ms, 2),
                })
                self._audit_llm_call(
                    success=False,
                    duration_ms=duration_ms,
                    attempts=attempts + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise

            self._breaker.record_success()
            resp = LLMResponse(data)
            duration_ms = (time.monotonic() - start) * 1000.0
            set_span_attributes(sp, {
                "llm.tokens.total": resp.usage.get("total_tokens"),
                "llm.tokens.prompt": resp.usage.get("prompt_tokens"),
                "llm.tokens.completion": resp.usage.get("completion_tokens"),
                "llm.finish_reason": resp.finish_reason or None,
                "llm.tool_calls_count": len(resp.tool_calls) if resp.has_tool_calls else None,
                "youmi.success": True,
                "youmi.attempts": attempts + 1,
                "youmi.duration_ms": round(duration_ms, 2),
            })
            self._audit_llm_call(
                success=True,
                duration_ms=duration_ms,
                attempts=attempts + 1,
                tokens=resp.usage,
            )

        logger.debug("LLM response: finish_reason=%s usage=%s",
                      data.get("choices", [{}])[0].get("finish_reason", ""),
                      data.get("usage", {}))

        return resp

    # ------------------------------------------------------------------
    # 便捷调用：complete
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **extra_params: Any,
    ) -> str:
        """调用 Chat Completions 并返回纯文本 content。

        供 `WorkflowPlanner` 等需要「一次性拿到完整文本回复」的调用方使用
        （planner 依赖 `llm_client.complete(messages) -> str` 契约）。

        Args:
            messages: OpenAI 格式的消息列表
            tools: 工具定义列表（可选）
            **extra_params: 其他 API 参数

        Returns:
            助手回复的文本内容

        Raises:
            ValueError: 回复不含文本内容（例如模型返回了 tool_calls 而非正文）
        """
        response = await self.chat(messages, tools=tools, **extra_params)
        content = response.content
        if not content:
            raise ValueError(
                "complete(): 模型未返回文本内容（可能返回了 tool_calls）。"
                "规划类调用应要求模型输出纯文本 JSON。"
            )
        return content

    # ------------------------------------------------------------------
    # 流式调用
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        **extra_params: Any,
    ) -> AsyncGenerator[str, None]:
        """流式调用 Chat Completions API (SSE)

        M1 增强: 连接建立阶段支持重试（已产出内容后不再重试，避免重复
        输出）；熔断保护 / OTel span / 审计日志。

        异步生成器，逐块产出文本内容。完成后通过 .final_response 获取完整响应。

        Yields:
            str: 文本块（仅 content delta，不含 tool_calls）
        """
        payload = self._build_payload(
            messages, tools, tool_choice, extra_params, stream=True,
        )

        logger.debug("LLM stream request: model=%s messages=%d tools=%d",
                      self._config.model, len(messages), len(tools or []))

        start = time.monotonic()
        attempts = 0
        received_content = False
        collected_content: list[str] = []
        collected_tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage: dict[str, Any] = {}

        # 熔断检查 — 整轮（含连接重试）视为一次调用；拒绝时快速失败不计入统计
        self._breaker.before_call()

        with span("llm.chat_stream", attributes={
            "llm.model": self._config.model,
            "llm.provider": self._config.provider.value,
            "llm.base_url": self._base_url,
            "llm.messages_count": len(messages),
            "llm.tools_count": len(tools or []),
        }) as sp:
            while True:
                attempts += 1
                try:
                    async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                        if response.status_code >= 400:
                            # 读取错误详情
                            error_body = await response.aread()
                            logger.error("LLM API error %d: %s | messages_count=%d tools_count=%d",
                                         response.status_code, error_body.decode("utf-8", errors="replace")[:2000],
                                         len(messages), len(tools or []))
                            response.raise_for_status()

                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data_str = line[6:]
                            if data_str.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            chunk_usage = chunk.get("usage")
                            if chunk_usage:
                                usage = chunk_usage

                            # choices 可能为空数组（OpenAI include_usage 的 usage 帧）
                            choices = chunk.get("choices") or [{}]
                            delta = choices[0].get("delta", {}) if choices else {}
                            fr = choices[0].get("finish_reason") if choices else None
                            if fr:
                                finish_reason = fr

                            # 文本内容
                            content = delta.get("content", "")
                            if content:
                                collected_content.append(content)
                                received_content = True
                                yield content

                            # 工具调用（累积 delta）
                            for tc_delta in delta.get("tool_calls", []):
                                idx = tc_delta["index"]
                                if idx not in collected_tool_calls:
                                    collected_tool_calls[idx] = {
                                        "id": tc_delta.get("id", ""),
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                existing = collected_tool_calls[idx]
                                fn = tc_delta.get("function", {})
                                if fn.get("name"):
                                    existing["function"]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    existing["function"]["arguments"] += fn["arguments"]
                                if tc_delta.get("id"):
                                    existing["id"] = tc_delta["id"]

                    # 流正常结束
                    self._breaker.record_success()
                    break
                except Exception as exc:
                    # 仅连接建立阶段（未产出任何内容）可重试
                    if (not received_content
                            and self._is_retryable_llm_error(exc)
                            and attempts <= self._retry_policy.max_retries):
                        # 重置半途累积状态，避免重试后数据混杂
                        collected_content.clear()
                        collected_tool_calls.clear()
                        finish_reason = ""
                        usage = {}
                        delay = compute_backoff_delay(attempts, self._retry_policy)
                        logger.warning(
                            "LLM stream: 第 %d/%d 次连接失败（%s: %s），%.2fs 后重试",
                            attempts, self._retry_policy.max_retries + 1,
                            type(exc).__name__, str(exc)[:200], delay,
                        )
                        if delay > 0:
                            await asyncio.sleep(delay)
                        continue

                    # 不可重试 / 已产出内容 / 重试耗尽
                    if self._is_retryable_llm_error(exc):
                        self._breaker.record_failure()
                    else:
                        self._breaker.record_success()
                    duration_ms = (time.monotonic() - start) * 1000.0
                    set_span_attributes(sp, {
                        "youmi.success": False,
                        "youmi.attempts": attempts,
                        "youmi.duration_ms": round(duration_ms, 2),
                    })
                    self._audit_llm_call(
                        success=False,
                        duration_ms=duration_ms,
                        attempts=attempts,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    raise

            # 流式完成 — 记录成功指标与审计
            duration_ms = (time.monotonic() - start) * 1000.0
            set_span_attributes(sp, {
                "llm.tokens.total": usage.get("total_tokens"),
                "llm.tokens.prompt": usage.get("prompt_tokens"),
                "llm.tokens.completion": usage.get("completion_tokens"),
                "llm.finish_reason": finish_reason or None,
                "llm.tool_calls_count": len(collected_tool_calls) or None,
                "youmi.success": True,
                "youmi.attempts": attempts,
                "youmi.duration_ms": round(duration_ms, 2),
            })
            self._audit_llm_call(
                success=True,
                duration_ms=duration_ms,
                attempts=attempts,
                tokens=usage,
            )

        # 构建完整响应
        full_content = "".join(collected_content)
        tool_calls_list = [
            collected_tool_calls[k] for k in sorted(collected_tool_calls)
        ] if collected_tool_calls else []

        # 修补流式传输中可能缺失的 tool_call_id
        for i, tc in enumerate(tool_calls_list):
            if not tc.get("id"):
                tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
                logger.warning("Stream: tool_call[%d] missing id, generated: %s", i, tc["id"])
            if not tc.get("type"):
                tc["type"] = "function"

        raw = {
            "choices": [{"message": {"role": "assistant",
                                     "content": full_content or None},
                         "finish_reason": finish_reason}],
            "usage": usage,
        }
        if tool_calls_list:
            raw["choices"][0]["message"]["tool_calls"] = tool_calls_list

        self._last_stream_response = LLMResponse(raw)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """关闭 HTTP 连接"""
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"<LLMClient model={self._config.model!r} base_url={self._base_url!r}>"
