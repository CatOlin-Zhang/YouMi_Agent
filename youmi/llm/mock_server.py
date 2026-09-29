"""
OpenAI 兼容 Mock LLM HTTP 服务器 (mock_server)

为「对 LLM 依赖路径做确定性集成测试 + 离线 eval」提供 **真实 HTTP 形态** 的
模型替身：被测代码把 ``base_url`` 指向本服务器，即可跑完整生产调用链路
（LLMClient → httpx → HTTP → OpenAI Chat Completions 协议），无需真实模型与网络。

特性：
- OpenAI Chat Completions 兼容：``POST /chat/completions`` 与 ``/v1/chat/completions``
- 脚本化响应：FIFO 队列（``script`` / ``enqueue``）+ 请求匹配器（``when``）
- 流式 SSE：content / tool_calls（分片累积）/ usage，帧格式对齐 OpenAI
- 错误注入：任意 HTTP 状态码 + OpenAI 风格错误体（验证客户端重试 / 熔断路径）
- 延迟注入：``delay_s`` 模拟慢模型
- 请求记录：``requests`` / ``request_headers`` 供测试断言（工具结果回填、鉴权头）
- 确定性：无随机、无网络、无外部服务; ``port=0`` 自动分配随机端口避免冲突

用法::

    from youmi.core.types import LLMConfig
    from youmi.llm import LLMClient
    from youmi.llm.mock_server import MockLLMServer, MockResponse

    server = MockLLMServer()
    await server.start()
    server.script([
        MockResponse.tool_call("get_weather", {"city": "北京"}),
        MockResponse.text("北京今天晴，25℃。"),
    ])

    client = LLMClient(LLMConfig(model="mock", base_url=server.base_url))
    r1 = await client.chat(messages, tools=tools)   # → tool_calls
    r2 = await client.chat(messages + tool_result)  # → 最终文本
    await client.close()
    await server.stop()

支持异步上下文管理器::

    async with MockLLMServer() as server:
        ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

try:
    from aiohttp import web
except ImportError as exc:  # pragma: no cover - 取决于可选依赖
    raise ImportError(
        "MockLLMServer 依赖 aiohttp，请先安装: pip install aiohttp"
    ) from exc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _split_chunks(text: str, size: int) -> list[str]:
    """把文本按 ``size`` 字符切块；空文本返回空列表"""
    if not text:
        return []
    if size <= 0:
        return [text]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _normalize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把工具调用归一化为 OpenAI ``tool_calls`` 格式。

    支持两种输入写法::

        {"name": "get_weather", "arguments": {"city": "北京"}}
        {"id": "call_x", "type": "function",
         "function": {"name": "get_weather", "arguments": "{...}"}}

    ``arguments`` 为 dict/list 时自动 JSON 序列化；``id`` 缺省时生成 ``call_{i}``。
    """
    normalized: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        fn_block = tc.get("function")
        if isinstance(fn_block, dict):
            name = fn_block.get("name", "")
            raw_args = fn_block.get("arguments", "")
        else:
            name = tc.get("name", "")
            raw_args = tc.get("arguments", "")
        if isinstance(raw_args, (dict, list)):
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        normalized.append({
            "id": tc.get("id") or f"call_{i}",
            "type": "function",
            "function": {"name": name, "arguments": str(raw_args or "")},
        })
    return normalized


def _estimate_usage(messages: list[dict[str, Any]], content: str) -> dict[str, int]:
    """粗略 token 估算（中文 ~4 字符/token），保证确定性"""
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    prompt = chars // 4 + 1
    completion = len(content) // 4 + 1
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _error_payload(status: int, message: str) -> dict[str, Any]:
    """OpenAI 风格错误体"""
    return {
        "error": {
            "message": message or f"Mock server error (HTTP {status})",
            "type": "server_error" if status >= 500 else "invalid_request_error",
            "code": status,
        }
    }


# ---------------------------------------------------------------------------
# 脚本化响应定义
# ---------------------------------------------------------------------------

@dataclass
class MockResponse:
    """一次脚本化响应的完整定义。

    Args:
        content: 文本回复
        tool_calls: 工具调用列表（简化格式或 OpenAI 格式，见 ``_normalize_tool_calls``）
        finish_reason: 结束原因；空时自动推导（有 tool_calls → ``tool_calls``，否则 ``stop``）
        usage: token 用量；None 时按文本长度估算
        status: HTTP 状态码，>= 400 即错误注入（此时忽略 content / tool_calls）
        error_message: 错误注入时的错误消息（装入 OpenAI 风格错误体）
        delay_s: 响应前延迟（模拟慢模型 / 配合客户端超时测试）
        chunk_size: 流式模式下每个 SSE 帧的字符数
        stream_usage: 流式模式是否在末尾发送 usage 帧（OpenAI include_usage 语义）
    """

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] | None = None
    status: int = 200
    error_message: str = ""
    delay_s: float = 0.0
    chunk_size: int = 8
    stream_usage: bool = True

    @property
    def resolved_finish_reason(self) -> str:
        if self.finish_reason:
            return self.finish_reason
        return "tool_calls" if self.tool_calls else "stop"

    # ---- 便捷构造 ----

    @classmethod
    def text(cls, content: str, **kwargs: Any) -> "MockResponse":
        """文本回复"""
        return cls(content=content, **kwargs)

    @classmethod
    def tool_call(
        cls,
        name: str,
        arguments: dict | list | str | None = None,
        **kwargs: Any,
    ) -> "MockResponse":
        """单个工具调用回复"""
        return cls(
            tool_calls=[{"name": name, "arguments": arguments if arguments is not None else {}}],
            **kwargs,
        )

    @classmethod
    def error(cls, status: int, message: str = "", **kwargs: Any) -> "MockResponse":
        """错误注入回复"""
        return cls(status=status, error_message=message, **kwargs)


# ---------------------------------------------------------------------------
# Mock LLM Server
# ---------------------------------------------------------------------------

class MockLLMServer:
    """OpenAI 兼容 mock LLM HTTP 服务器。

    响应解析优先级：
    1. 匹配器（``when`` 注册顺序，第一个命中者胜出；命中不消耗脚本队列）
    2. 脚本队列（FIFO，每次请求弹出队首）
    3. ``default_response``；未配置时兜底返回 ``content="mock-response"``

    Args:
        host: 监听地址
        port: 监听端口；0 = 自动分配随机端口（推荐，避免测试/并行冲突）
        model: 默认模型名（请求体带 model 时以请求为准）
        default_response: 队列耗尽且无匹配时的兜底响应
        stream_chunk_delay_s: 流式模式每帧之间的延迟（模拟慢速流）
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        model: str = "mock-model",
        default_response: MockResponse | None = None,
        stream_chunk_delay_s: float = 0.0,
    ) -> None:
        self._host = host
        self._port = port
        self._model = model
        self._default_response = default_response
        self._stream_chunk_delay_s = stream_chunk_delay_s
        self._queue: deque[MockResponse] = deque()
        self._matchers: list[tuple[Callable[[dict[str, Any]], bool], MockResponse]] = []
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._seq = 0

        # 请求记录（供测试断言）
        self.requests: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str]] = []
        self.request_paths: list[str] = []

    # ------------------------------------------------------------------
    # 脚本编排
    # ------------------------------------------------------------------

    def script(self, responses: Iterable[MockResponse]) -> None:
        """替换脚本队列（FIFO 顺序返回）"""
        self._queue = deque(responses)

    def enqueue(self, response: MockResponse) -> None:
        """向脚本队列追加一个响应"""
        self._queue.append(response)

    def when(
        self,
        matcher: Callable[[dict[str, Any]], bool],
        response: MockResponse,
    ) -> None:
        """注册请求匹配器：``matcher(请求体)`` 为 True 时返回 response。

        匹配器按注册顺序求值，命中者不消耗脚本队列；匹配器抛异常视为不命中。
        """
        self._matchers.append((matcher, response))

    def when_content_contains(self, substring: str, response: MockResponse) -> None:
        """便捷匹配器：任意 message 的 content 含 ``substring`` 时命中"""
        def _match(body: dict[str, Any]) -> bool:
            for m in body.get("messages") or []:
                if substring in str(m.get("content") or ""):
                    return True
            return False

        self.when(_match, response)

    def when_tool_result(self, response: MockResponse) -> None:
        """便捷匹配器：messages 中出现工具结果（role == "tool"）时命中"""
        self.when(
            lambda body: any(
                m.get("role") == "tool" for m in (body.get("messages") or [])
            ),
            response,
        )

    def reset(self) -> None:
        """清空脚本 / 匹配器 / 请求记录"""
        self._queue.clear()
        self._matchers.clear()
        self.requests.clear()
        self.request_headers.clear()
        self.request_paths.clear()
        self._seq = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> "MockLLMServer":
        """启动 HTTP 服务（重复调用幂等）"""
        if self._runner is not None:
            return self
        app = web.Application()
        app.router.add_post("/chat/completions", self._handle_chat_completions)
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        app.router.add_get("/health", self._handle_health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._runner = runner
        self._site = site
        logger.debug("MockLLMServer 已启动: %s", self.base_url)
        return self

    async def stop(self) -> None:
        """停止 HTTP 服务"""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def __aenter__(self) -> "MockLLMServer":
        return await self.start()

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # 地址信息
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        return "127.0.0.1" if self._host in ("", "0.0.0.0") else self._host

    @property
    def port(self) -> int:
        """实际监听端口（port=0 启动后解析随机端口）"""
        site = self._site
        if site is not None:
            server = getattr(site, "_server", None)
            sockets = getattr(server, "sockets", None)
            if sockets:
                return sockets[0].getsockname()[1]
        return self._port

    @property
    def url(self) -> str:
        """服务根地址（如 ``http://127.0.0.1:38123``）"""
        return f"http://{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        """OpenAI 兼容 base_url（可直接赋给 ``LLMConfig.base_url``）"""
        return f"{self.url}/v1"

    @property
    def call_count(self) -> int:
        return len(self.requests)

    # ------------------------------------------------------------------
    # HTTP 处理
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "model": self._model})

    async def _handle_chat_completions(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(_error_payload(400, "请求体不是合法 JSON"), status=400)
        if not isinstance(body, dict):
            body = {}

        self.requests.append(body)
        self.request_headers.append({k: v for k, v in request.headers.items()})
        self.request_paths.append(request.path)
        self._seq += 1
        seq = self._seq

        spec = self._next_response(body)
        if spec.delay_s > 0:
            await asyncio.sleep(spec.delay_s)

        if spec.status >= 400:
            return web.json_response(
                _error_payload(spec.status, spec.error_message), status=spec.status,
            )

        if body.get("stream"):
            return await self._stream_response(request, spec, body, seq)
        return web.json_response(self._completion_payload(spec, body, seq))

    def _next_response(self, body: dict[str, Any]) -> MockResponse:
        """按优先级解析本次请求应返回的响应定义"""
        for matcher, response in self._matchers:
            try:
                if matcher(body):
                    return response
            except Exception:  # 匹配器异常 → 视为不命中
                continue
        if self._queue:
            return self._queue.popleft()
        if self._default_response is not None:
            return self._default_response
        return MockResponse(content="mock-response")

    def _completion_payload(
        self, spec: MockResponse, body: dict[str, Any], seq: int,
    ) -> dict[str, Any]:
        """构建非流式 Chat Completion 响应体"""
        tool_calls = _normalize_tool_calls(spec.tool_calls)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": spec.content or None,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = spec.usage or _estimate_usage(body.get("messages") or [], spec.content)
        return {
            "id": f"chatcmpl-mock-{seq}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model") or self._model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": spec.resolved_finish_reason,
            }],
            "usage": usage,
        }

    async def _stream_response(
        self,
        request: web.Request,
        spec: MockResponse,
        body: dict[str, Any],
        seq: int,
    ) -> web.StreamResponse:
        """构建流式 SSE 响应（帧格式对齐 OpenAI Chat Completions chunk）"""
        stream = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
            },
        )
        await stream.prepare(request)

        base: dict[str, Any] = {
            "id": f"chatcmpl-mock-{seq}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": body.get("model") or self._model,
        }

        async def emit(chunk: dict[str, Any]) -> None:
            data = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            await stream.write(data.encode("utf-8"))
            if self._stream_chunk_delay_s > 0:
                await asyncio.sleep(self._stream_chunk_delay_s)

        def choice(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
            return {
                **base,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }

        size = max(1, spec.chunk_size)

        # 1) 角色帧
        await emit(choice({"role": "assistant", "content": ""}))
        # 2) 正文分片
        for piece in _split_chunks(spec.content, size):
            await emit(choice({"content": piece}))
        # 3) tool_calls 分片（首帧带 id / name，arguments 逐帧累积）
        for idx, tc in enumerate(_normalize_tool_calls(spec.tool_calls)):
            await emit(choice({"tool_calls": [{
                "index": idx,
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": ""},
            }]}))
            for piece in _split_chunks(tc["function"]["arguments"], size):
                await emit(choice({"tool_calls": [{
                    "index": idx,
                    "function": {"arguments": piece},
                }]}))
        # 4) 结束帧
        await emit(choice({}, spec.resolved_finish_reason))
        # 5) usage 帧（OpenAI include_usage 语义：choices 为空）
        if spec.stream_usage:
            usage = spec.usage or _estimate_usage(body.get("messages") or [], spec.content)
            await emit({**base, "choices": [], "usage": usage})
        # 6) 终止
        await stream.write(b"data: [DONE]\n\n")
        await stream.write_eof()
        return stream
