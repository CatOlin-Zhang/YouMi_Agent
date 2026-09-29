"""
M1 LLM 韧性测试 — LLMClient 重试 / 熔断 / 审计集成

覆盖:
- chat: 成功 / 429 限流重试 / 网络错误重试 / 400 不重试 / 重试耗尽
- chat: 熔断器打开后快速失败（CircuitOpenError）且不再发请求
- chat: 业务错误（400）不触发熔断；成功重置连续失败计数
- chat: 重试策略来自 config.retry_policy
- chat_stream: 正常流 / usage 捕获 / tool_calls 累积
- chat_stream: 连接建立阶段可重试；已产出内容后不重试
- chat_stream: 熔断拒绝快速失败
- 审计: llm_call 事件的 status / attempts / tokens 字段
"""

from __future__ import annotations

import json

import httpx
import pytest

from youmi.core.resilience import (
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitOpenError,
    CircuitState,
)
from youmi.core.types import LLMConfig, RetryPolicy
from youmi.llm.client import LLMClient
from youmi.observability import AuditLogger

BASE_URL = "http://test.local/v1"
BREAKER_NAME = f"llm:{BASE_URL}"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _ok_json(content: str = "hello") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
    }


def _sse_response(*chunks: dict) -> httpx.Response:
    """构造静态 SSE 响应（data: ... 行 + [DONE]）"""
    lines = [f"data: {json.dumps(c)}" for c in chunks]
    lines.append("data: [DONE]")
    body = ("\n\n".join(lines) + "\n\n").encode("utf-8")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


async def _make_client(
    handler,
    *,
    retry_policy: RetryPolicy | None = None,
    breaker_registry: CircuitBreakerRegistry | None = None,
    audit: AuditLogger | None = None,
    base_url: str = BASE_URL,
) -> LLMClient:
    """构造使用 MockTransport 的 LLMClient（默认独立熔断器与审计实例）"""
    config = LLMConfig(model="test-model", base_url=base_url, api_key="sk-test")
    client = LLMClient(
        config,
        retry_policy=(
            retry_policy if retry_policy is not None
            else RetryPolicy(max_retries=2, base_delay_s=0.0)
        ),
        breaker_registry=(
            breaker_registry if breaker_registry is not None
            else CircuitBreakerRegistry()
        ),
        audit=audit if audit is not None else AuditLogger(),
    )
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=base_url,
    )
    return client


def _msg() -> list[dict]:
    return [{"role": "user", "content": "hi"}]


class _BrokenByteStream(httpx.AsyncByteStream):
    """先产出一段 SSE 数据，随后模拟连接中断"""

    def __init__(self, first_chunk: bytes) -> None:
        self._first_chunk = first_chunk

    async def __aiter__(self):
        yield self._first_chunk
        raise httpx.ReadError("connection reset")


# ---------------------------------------------------------------------------
# chat: 重试与审计
# ---------------------------------------------------------------------------

class TestChatResilience:
    async def test_success_audited(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.url.path == "/v1/chat/completions"
            return httpx.Response(200, json=_ok_json("你好"))

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "你好"
            assert len(calls) == 1

            events = audit.get_recent(event_type="llm_call")
            assert len(events) == 1
            ev = events[0]
            assert ev.status == "ok"
            assert ev.detail["model"] == "test-model"
            assert ev.detail["attempts"] == 1
            assert ev.detail["tokens"]["total_tokens"] == 10
        finally:
            await client.close()

    async def test_retry_on_429_then_success(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json=_ok_json("成功"))

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "成功"
            assert len(calls) == 3  # 1 + 2 次重试
            ev = audit.get_recent(event_type="llm_call")[0]
            assert ev.status == "ok"
            assert ev.detail["attempts"] == 3
        finally:
            await client.close()

    async def test_retry_on_network_error(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("connection refused")
            return httpx.Response(200, json=_ok_json("ok"))

        client = await _make_client(handler)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "ok"
            assert len(calls) == 2
        finally:
            await client.close()

    async def test_no_retry_on_400(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400, json={"error": "bad request"})

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())
            assert len(calls) == 1  # 400 不可重试
            ev = audit.get_recent(event_type="llm_call")[0]
            assert ev.status == "error"
            assert ev.detail["attempts"] == 1
            assert "HTTPStatusError" in ev.error
        finally:
            await client.close()

    async def test_retry_exhausted(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, json={"error": "unavailable"})

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())
            assert len(calls) == 3  # 1 + 2 次重试后放弃
            ev = audit.get_recent(event_type="llm_call")[0]
            assert ev.status == "error"
            assert ev.detail["attempts"] == 3
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# chat: 熔断器
# ---------------------------------------------------------------------------

class TestChatBreaker:
    async def test_breaker_opens_and_fast_fails(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500, json={"error": "boom"})

        registry = CircuitBreakerRegistry(
            CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0)
        )
        policy = RetryPolicy(max_retries=0, base_delay_s=0.0)
        client = await _make_client(
            handler, retry_policy=policy, breaker_registry=registry,
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())
            assert len(calls) == 2

            breaker = registry.get(BREAKER_NAME)
            assert breaker.state == CircuitState.OPEN

            # 第三次：熔断拒绝，不再发请求
            with pytest.raises(CircuitOpenError):
                await client.chat(_msg())
            assert len(calls) == 2
        finally:
            await client.close()

    async def test_business_error_does_not_trip(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(400, json={"error": "bad request"})

        registry = CircuitBreakerRegistry(
            CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0)
        )
        policy = RetryPolicy(max_retries=0, base_delay_s=0.0)
        client = await _make_client(
            handler, retry_policy=policy, breaker_registry=registry,
        )
        try:
            for _ in range(3):
                with pytest.raises(httpx.HTTPStatusError):
                    await client.chat(_msg())
            assert len(calls) == 3  # 全部发出 — 未被熔断
            assert registry.get(BREAKER_NAME).state == CircuitState.CLOSED
        finally:
            await client.close()

    async def test_success_resets_failure_count(self):
        seq = [500, 200, 500, 200]
        statuses = []

        def handler(request: httpx.Request) -> httpx.Response:
            status = seq[len(statuses)]
            statuses.append(status)
            if status == 200:
                return httpx.Response(200, json=_ok_json("ok"))
            return httpx.Response(status, json={"error": "err"})

        registry = CircuitBreakerRegistry(
            CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0)
        )
        policy = RetryPolicy(max_retries=0, base_delay_s=0.0)
        client = await _make_client(
            handler, retry_policy=policy, breaker_registry=registry,
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())  # 500 → failure_count=1
            await client.chat(_msg())      # 200 → 计数重置
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())  # 500 → failure_count=1（未达阈值）
            await client.chat(_msg())      # 200
            assert len(statuses) == 4
            breaker = registry.get(BREAKER_NAME)
            assert breaker.state == CircuitState.CLOSED
            assert breaker.failure_count == 0
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# 重试策略来源
# ---------------------------------------------------------------------------

class TestRetryPolicySource:
    async def test_config_retry_policy_used(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, json={"error": "unavailable"})

        config = LLMConfig(
            model="test-model",
            base_url="http://cfg.local/v1",
            retry_policy=RetryPolicy(max_retries=1, base_delay_s=0.0),
        )
        client = LLMClient(
            config,
            breaker_registry=CircuitBreakerRegistry(),
            audit=AuditLogger(),
        )
        await client._client.aclose()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://cfg.local/v1",
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())
            assert len(calls) == 2  # 1 + 1 次重试（来自 config.retry_policy）
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# chat_stream
# ---------------------------------------------------------------------------

class TestChatStream:
    async def test_stream_success(self):
        chunks = [
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}}],
             "usage": {"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2}},
        ]
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(1)
            return _sse_response(*chunks)

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            parts = []
            async for chunk in client.chat_stream(_msg()):
                parts.append(chunk)
            assert "".join(parts) == "你好"

            final = client._last_stream_response
            assert final.content == "你好"
            assert final.usage["total_tokens"] == 7

            ev = audit.get_recent(event_type="llm_call")[0]
            assert ev.status == "ok"
            assert ev.detail["attempts"] == 1
            assert ev.detail["tokens"]["total_tokens"] == 7
            assert len(requests) == 1
        finally:
            await client.close()

    async def test_stream_tool_calls_collected(self):
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "file_read", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"path": '}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"a.txt"}'}}]}}]},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return _sse_response(*chunks)

        client = await _make_client(handler)
        try:
            parts = []
            async for chunk in client.chat_stream(_msg()):
                parts.append(chunk)
            assert parts == []  # tool_calls 不产出文本块

            final = client._last_stream_response
            assert final.has_tool_calls
            tc = final.tool_calls[0]
            assert tc["function"]["name"] == "file_read"
            assert json.loads(tc["function"]["arguments"]) == {"path": "a.txt"}
        finally:
            await client.close()

    async def test_stream_retry_on_connect_error(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("boom")
            return _sse_response({"choices": [{"delta": {"content": "恢复"}}]})

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            parts = []
            async for chunk in client.chat_stream(_msg()):
                parts.append(chunk)
            assert "".join(parts) == "恢复"
            assert len(calls) == 2
            ev = audit.get_recent(event_type="llm_call")[0]
            assert ev.status == "ok"
            assert ev.detail["attempts"] == 2
        finally:
            await client.close()

    async def test_stream_midway_failure_no_retry(self):
        calls = []

        first_chunk = (
            "data: " + json.dumps(
                {"choices": [{"delta": {"content": "部分"}}]}
            ) + "\n\n"
        ).encode("utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_BrokenByteStream(first_chunk),
            )

        audit = AuditLogger()
        client = await _make_client(handler, audit=audit)
        try:
            parts = []
            with pytest.raises(httpx.ReadError):
                async for chunk in client.chat_stream(_msg()):
                    parts.append(chunk)
            assert "".join(parts) == "部分"
            # 已产出内容 → 不重试
            assert len(calls) == 1
            ev = audit.get_recent(event_type="llm_call")[0]
            assert ev.status == "error"
            assert ev.detail["attempts"] == 1
        finally:
            await client.close()

    async def test_stream_breaker_open_fast_fail(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500, json={"error": "boom"})

        registry = CircuitBreakerRegistry(
            CircuitBreakerConfig(failure_threshold=1, recovery_timeout_s=60.0)
        )
        policy = RetryPolicy(max_retries=0, base_delay_s=0.0)
        client = await _make_client(
            handler, retry_policy=policy, breaker_registry=registry,
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                async for _ in client.chat_stream(_msg()):
                    pass
            assert len(calls) == 1
            assert registry.get(BREAKER_NAME).state == CircuitState.OPEN

            # 第二次：熔断拒绝，快速失败且不再发请求
            with pytest.raises(CircuitOpenError):
                async for _ in client.chat_stream(_msg()):
                    pass
            assert len(calls) == 1
        finally:
            await client.close()
