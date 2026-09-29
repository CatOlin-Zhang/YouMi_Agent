"""LLM 模块"""

from typing import Any

from youmi.llm.client import LLMClient, LLMResponse
from youmi.llm.embeddings import EmbeddingClient, EmbeddingError

__all__ = [
    "LLMClient", "LLMResponse", "EmbeddingClient", "EmbeddingError",
    "MockLLMServer", "MockResponse",
]


def __getattr__(name: str) -> Any:
    """延迟导入 mock server（aiohttp 为可选依赖，仅访问时才要求安装）"""
    if name in ("MockLLMServer", "MockResponse"):
        from youmi.llm import mock_server

        return getattr(mock_server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
