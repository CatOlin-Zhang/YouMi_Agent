"""
Mock LLM Server 测试 (mock01)

覆盖:
- 非流式：text / tool_calls / FIFO 脚本顺序 / 匹配器优先级 / 默认兜底
- 流式 SSE：文本分块重组 / tool_calls 增量累积 / usage 捕获 / usage 关闭
- 错误注入：500 重试恢复 / 429 重试耗尽 / 400 不重试 / 错误体格式
- 请求记录：消息回填 / 路径 / 鉴权头
- ReAct 两轮场景：tool_call → 工具结果 → 最终文本（走真实 LLMClient 链路）
- 延迟注入与双路径（/chat/completions 与 /v1/chat/completions）
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from youmi.core.resilience import CircuitBreakerRegistry
from youmi.core.types import LLMConfig, RetryPolicy
from youmi.llm import LLMClient
from youmi.llm.mock_server import MockLLMServer, MockResponse
from youmi.observability import AuditLogger


@pytest.fixture
async def server():
    srv = MockLLMServer()
    await srv.start()
    try:
        yield srv
    finally:
        await srv.stop()


def _make_client(
    server: MockLLMServer,
    *,
    max_retries: int = 2,
    timeout_s: int = 10,
) -> LLMClient:
    config = LLMConfig(
        model="mock-model",
        base_url=server.base_url,
        api_key="sk-mock",
        timeout_s=timeout_s,
    )
    return LLMClient(
        config,
        retry_policy=RetryPolicy(max_retries=max_retries, base_delay_s=0.0),
        breaker_registry=CircuitBreakerRegistry(),
        audit=AuditLogger(),
    )


def _msg(text: str = "hi") -> list[dict]:
    return [{"role": "user", "content": text}]


# =========================================================================
# 非流式对话
# =========================================================================

class TestChatNonStreaming:

    async def test_text_response_and_recording(self, server):
        server.script([MockResponse.text("你好，世界")])
        client = _make_client(server)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "你好，世界"
            assert resp.finish_reason == "stop"
            assert not resp.has_tool_calls

            # 请求记录
            assert server.call_count == 1
            assert server.requests[0]["model"] == "mock-model"
            assert server.requests[0]["messages"][0]["content"] == "hi"
            assert server.request_paths[0] == "/v1/chat/completions"
            assert server.request_headers[0]["Authorization"] == "Bearer sk-mock"

            # 自动估算 usage
            assert resp.usage["total_tokens"] > 0
        finally:
            await client.close()

    async def test_tool_call_response(self, server):
        server.script([MockResponse.tool_call("get_weather", {"city": "北京"})])
        client = _make_client(server)
        try:
            tools = [{"type": "function", "function": {"name": "get_weather"}}]
            resp = await client.chat(_msg(), tools=tools)
            assert resp.has_tool_calls
            tc = resp.tool_calls[0]
            assert tc["function"]["name"] == "get_weather"
            assert json.loads(tc["function"]["arguments"]) == {"city": "北京"}
            assert tc["id"] == "call_0"
            assert resp.finish_reason == "tool_calls"
            # 请求里带上了 tools 定义
            assert server.requests[0]["tools"] == tools
        finally:
            await client.close()

    async def test_fifo_script_order(self, server):
        server.script([
            MockResponse.text("第一个"),
            MockResponse.text("第二个"),
        ])
        client = _make_client(server)
        try:
            r1 = await client.chat(_msg())
            r2 = await client.chat(_msg())
            assert (r1.content, r2.content) == ("第一个", "第二个")
        finally:
            await client.close()

    async def test_matcher_overrides_queue(self, server):
        """匹配器优先于 FIFO 队列且不消耗队列"""
        server.enqueue(MockResponse.tool_call("t1", {}))
        server.when_tool_result(MockResponse.text("最终答案"))
        client = _make_client(server)
        try:
            # 无工具结果 → 队列响应（tool_call）
            r1 = await client.chat(_msg())
            assert r1.has_tool_calls
            # 有工具结果 → 匹配器响应
            r2 = await client.chat([
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "call_0", "content": "结果"},
            ])
            assert r2.content == "最终答案"
            # 队列已被第一次消耗；无工具消息 → 匹配器不命中 → 兜底
            r3 = await client.chat(_msg())
            assert r3.content == "mock-response"
        finally:
            await client.close()

    async def test_default_fallback(self, server):
        """无脚本 / 无匹配器 → 兜底响应"""
        client = _make_client(server)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "mock-response"
        finally:
            await client.close()

    async def test_custom_default_response(self):
        srv = MockLLMServer(default_response=MockResponse.text("自定义兜底"))
        await srv.start()
        client = _make_client(srv)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "自定义兜底"
        finally:
            await client.close()
            await srv.stop()

    async def test_when_content_contains(self, server):
        server.when_content_contains("天气", MockResponse.text("晴天"))
        client = _make_client(server)
        try:
            r = await client.chat(_msg("北京天气如何"))
            assert r.content == "晴天"
            r = await client.chat(_msg("其它问题"))
            assert r.content == "mock-response"
        finally:
            await client.close()


# =========================================================================
# 流式 SSE
# =========================================================================

class TestStreaming:

    async def test_stream_text_reassembled(self, server):
        text = "流式输出测试文本，用于验证分块拼接与 usage 捕获。"
        server.script([MockResponse.text(text, chunk_size=4)])
        client = _make_client(server)
        try:
            chunks = [c async for c in client.chat_stream(_msg())]
            assert len(chunks) > 3  # 确实被切成了多块
            assert "".join(chunks) == text

            final = client._last_stream_response
            assert final.content == text
            assert final.finish_reason == "stop"
            assert final.usage["total_tokens"] > 0
        finally:
            await client.close()

    async def test_stream_tool_calls_accumulated(self, server):
        server.script([MockResponse.tool_call("calc", {"a": 1, "b": 2}, chunk_size=2)])
        client = _make_client(server)
        try:
            chunks = [c async for c in client.chat_stream(_msg())]
            assert chunks == []  # tool_calls 场景无文本块

            final = client._last_stream_response
            assert final.has_tool_calls
            tc = final.tool_calls[0]
            assert tc["function"]["name"] == "calc"
            assert json.loads(tc["function"]["arguments"]) == {"a": 1, "b": 2}
            assert final.finish_reason == "tool_calls"
        finally:
            await client.close()

    async def test_stream_usage_disabled(self, server):
        server.script([MockResponse.text("hi", stream_usage=False)])
        client = _make_client(server)
        try:
            chunks = [c async for c in client.chat_stream(_msg())]
            assert "".join(chunks) == "hi"
            assert client._last_stream_response.usage == {}
        finally:
            await client.close()

    async def test_stream_request_has_stream_flag(self, server):
        server.script([MockResponse.text("ok")])
        client = _make_client(server)
        try:
            _ = [c async for c in client.chat_stream(_msg())]
            assert server.requests[0]["stream"] is True
        finally:
            await client.close()


# =========================================================================
# 错误注入
# =========================================================================

class TestErrorInjection:

    async def test_500_retry_then_success(self, server):
        server.script([
            MockResponse.error(500, "internal error"),
            MockResponse.text("恢复成功"),
        ])
        client = _make_client(server)
        try:
            resp = await client.chat(_msg())
            assert resp.content == "恢复成功"
            assert server.call_count == 2
        finally:
            await client.close()

    async def test_429_exhausted(self, server):
        server.script([
            MockResponse.error(429, "rate limited"),
            MockResponse.error(429, "rate limited"),
            MockResponse.error(429, "rate limited"),
        ])
        client = _make_client(server, max_retries=2)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.chat(_msg())
            assert server.call_count == 3  # 1 次原始 + 2 次重试
        finally:
            await client.close()

    async def test_400_no_retry(self, server):
        server.script([MockResponse.error(400, "bad request")])
        client = _make_client(server)
        try:
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await client.chat(_msg())
            assert server.call_count == 1  # 400 不重试
            body = ei.value.response.json()
            assert body["error"]["message"] == "bad request"
            assert body["error"]["code"] == 400
            assert body["error"]["type"] == "invalid_request_error"
        finally:
            await client.close()

    async def test_error_body_server_error_type(self, server):
        server.script([MockResponse.error(503)])
        client = _make_client(server, max_retries=0)
        try:
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await client.chat(_msg())
            body = ei.value.response.json()
            assert body["error"]["type"] == "server_error"
            assert "503" in body["error"]["message"]
        finally:
            await client.close()


# =========================================================================
# 延迟注入
# =========================================================================

class TestDelay:

    async def test_delay_applied(self, server):
        server.script([MockResponse.text("slow", delay_s=0.15)])
        client = _make_client(server)
        try:
            start = time.monotonic()
            resp = await client.chat(_msg())
            elapsed = time.monotonic() - start
            assert resp.content == "slow"
            assert elapsed >= 0.1
        finally:
            await client.close()


# =========================================================================
# 双路径 & 生命周期
# =========================================================================

class TestPathsAndLifecycle:

    async def test_plain_path_without_v1(self, server):
        """不带 /v1 前缀的路径同样可用"""
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"{server.url}/chat/completions",
                json={"model": "x", "messages": []},
            )
            assert r.status_code == 200
            assert r.json()["choices"][0]["message"]["content"] == "mock-response"
        assert server.request_paths[-1] == "/chat/completions"

    async def test_health_endpoint(self, server):
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{server.url}/health")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    async def test_context_manager_and_reuse(self):
        async with MockLLMServer() as srv:
            port = srv.port
            assert port > 0
            assert srv.base_url == f"http://127.0.0.1:{port}/v1"
            # 重复 start 幂等
            await srv.start()
            assert srv.port == port
        # 停止后端口不再接受连接（Windows 下可能表现为连接超时而非拒绝）
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            async with httpx.AsyncClient(timeout=2) as http:
                await http.get(f"http://127.0.0.1:{port}/health")

    async def test_reset_clears_state(self, server):
        server.script([MockResponse.text("a")])
        server.when(lambda b: True, MockResponse.text("b"))
        client = _make_client(server)
        try:
            await client.chat(_msg())
            assert server.call_count == 1
            server.reset()
            assert server.call_count == 0
            resp = await client.chat(_msg())  # 匹配器也被清空 → 兜底
            assert resp.content == "mock-response"
        finally:
            await client.close()


# =========================================================================
# ReAct 两轮场景（真实 LLMClient 链路）
# =========================================================================

class TestReactScenario:

    async def test_two_round_tool_flow(self, server):
        """模拟 ReAct 一轮工具调用：
        请求1（带 tools）→ tool_call；请求2（含工具结果）→ 最终文本。
        """
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查询城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }]
        server.script([
            MockResponse.tool_call("get_weather", {"city": "北京"}),
            MockResponse.text("北京今天晴，25℃。"),
        ])

        client = _make_client(server)
        try:
            messages = [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "北京天气如何？"},
            ]
            r1 = await client.chat(messages, tools=tools)
            assert r1.has_tool_calls
            tc = r1.tool_calls[0]

            messages.append(r1.raw_message)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": "晴，25℃",
            })
            r2 = await client.chat(messages, tools=tools)
            assert r2.content == "北京今天晴，25℃。"

            # 第二轮请求携带了工具执行结果
            second = server.requests[1]
            tool_msgs = [m for m in second["messages"] if m.get("role") == "tool"]
            assert len(tool_msgs) == 1
            assert tool_msgs[0]["tool_call_id"] == tc["id"]
            assert tool_msgs[0]["content"] == "晴，25℃"
            assert server.call_count == 2
        finally:
            await client.close()

    async def test_react_flow_with_streaming(self, server):
        """流式形态的 ReAct：第一轮 tool_calls 由分片累积，第二轮流式文本。"""
        server.script([
            MockResponse.tool_call("search", {"q": "P1 进展"}, chunk_size=3),
            MockResponse.text("P1 已全部落地。", chunk_size=3),
        ])
        client = _make_client(server)
        try:
            r1_chunks = [c async for c in client.chat_stream(_msg("查一下 P1"))]
            assert r1_chunks == []
            r1 = client._last_stream_response
            assert r1.has_tool_calls
            tc = r1.tool_calls[0]
            assert tc["function"]["name"] == "search"
            assert json.loads(tc["function"]["arguments"]) == {"q": "P1 进展"}

            messages = _msg("查一下 P1")
            messages.append(r1.raw_message)
            messages.append({
                "role": "tool", "tool_call_id": tc["id"], "content": "完成",
            })
            r2_chunks = [c async for c in client.chat_stream(messages)]
            assert "".join(r2_chunks) == "P1 已全部落地。"
        finally:
            await client.close()
