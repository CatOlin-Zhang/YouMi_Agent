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

import json
import logging
import uuid
from typing import Any

import httpx

from youmi.core.types import LLMConfig, LLMProvider

logger = logging.getLogger(__name__)


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

    Args:
        config: LLM 连接配置
    """

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._base_url = self._resolve_base_url(config)
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._build_headers(config),
            timeout=httpx.Timeout(config.timeout_s),
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

        Args:
            messages: OpenAI 格式的消息列表
            tools: 工具定义列表 (OpenAI tools 格式)，传入即启用 function calling
            tool_choice: 工具选择策略 ("auto" / "none" / {"type": "function", "function": {"name": "..."}})
            **extra_params: 其他 API 参数

        Returns:
            LLMResponse 封装的响应
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }

        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
            else:
                payload["tool_choice"] = "auto"

        payload.update(self._config.extra_params)
        payload.update(extra_params)

        logger.debug("LLM request: model=%s messages=%d tools=%d",
                      self._config.model, len(messages), len(tools or []))

        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()

        logger.debug("LLM response: finish_reason=%s usage=%s",
                      data.get("choices", [{}])[0].get("finish_reason", ""),
                      data.get("usage", {}))

        return LLMResponse(data)

    # ------------------------------------------------------------------
    # 流式调用
    # ------------------------------------------------------------------

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        **extra_params: Any,
    ) -> Any:
        """流式调用 Chat Completions API (SSE)

        异步生成器，逐块产出文本内容。完成后通过 .final_response 获取完整响应。

        Yields:
            str: 文本块（仅 content delta，不含 tool_calls）
        """
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": True,
        }

        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
            else:
                payload["tool_choice"] = "auto"

        payload.update(self._config.extra_params)
        payload.update(extra_params)

        logger.debug("LLM stream request: model=%s messages=%d tools=%d",
                      self._config.model, len(messages), len(tools or []))

        collected_content: list[str] = []
        collected_tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""

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

                delta = chunk.get("choices", [{}])[0].get("delta", {})
                fr = chunk.get("choices", [{}])[0].get("finish_reason")
                if fr:
                    finish_reason = fr

                # 文本内容
                content = delta.get("content", "")
                if content:
                    collected_content.append(content)
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
            "usage": {},
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
