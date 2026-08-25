"""
Embedding 客户端

通过 OpenAI 兼容的 /v1/embeddings 端点生成文本向量。
兼容 OpenAI API 和 Ollama Embeddings API。

用法::

    from youmi.llm.embeddings import EmbeddingClient

    client = EmbeddingClient(
        base_url="http://localhost:11434/v1",
        model="nomic-embed-text",
    )

    vectors = await client.embed(["搜索文件", "读取文件内容"])
    single = await client.embed_one("发送邮件的工具")

    scores = await client.similarity(query_vec, candidate_vecs)
"""

from __future__ import annotations

import logging
import math
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """Embedding 客户端 — 调用 OpenAI 兼容 /v1/embeddings API

    Args:
        base_url: API 基础 URL (不含 /v1/embeddings 路径)
        api_key: API 密钥 (可选)
        model: Embedding 模型名称
        timeout: 请求超时秒数
        dimensions: 向量维度 (None = 模型默认)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "nomic-embed-text",
        timeout: int = 30,
        dimensions: int | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量生成文本向量

        Args:
            texts: 待向量化的文本列表

        Returns:
            与 texts 等长的向量列表

        Raises:
            EmbeddingError: API 调用失败
        """
        if not texts:
            return []

        payload: dict[str, Any] = {
            "model": self._model,
            "input": texts,
        }
        if self._dimensions is not None:
            payload["dimensions"] = self._dimensions

        try:
            response = await self._client.post("/embeddings", json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding API 调用失败: {exc}") from exc

        # OpenAI 格式: {"data": [{"embedding": [...], "index": 0}, ...]}
        items = data.get("data", [])
        # 按 index 排序保证顺序一致
        items.sort(key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]

    async def embed_one(self, text: str) -> list[float]:
        """单条文本生成向量"""
        results = await self.embed([text])
        if not results:
            raise EmbeddingError("Embedding API 返回空结果")
        return results[0]

    # ------------------------------------------------------------------
    # 相似度计算 (纯 Python, 无 numpy 依赖)
    # ------------------------------------------------------------------

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度

        Returns:
            相似度 [-1, 1], 0 表示正交, 1 表示完全相同
        """
        if not a or not b or len(a) != len(b):
            return 0.0

        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot / (norm_a * norm_b)

    async def similarity(
        self,
        query_vec: list[float],
        candidates: list[list[float]],
    ) -> list[float]:
        """批量计算查询向量与候选向量列表的余弦相似度

        Args:
            query_vec: 查询向量
            candidates: 候选向量列表

        Returns:
            与 candidates 等长的相似度分数列表
        """
        return [self.cosine_similarity(query_vec, c) for c in candidates]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """关闭 HTTP 连接"""
        await self._client.aclose()

    async def __aenter__(self) -> "EmbeddingClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"<EmbeddingClient model={self._model!r} base_url={self._base_url!r}>"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class EmbeddingError(Exception):
    """Embedding API 调用异常"""
    pass
