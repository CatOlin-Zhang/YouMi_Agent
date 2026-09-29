"""
真实模型接入接口（Ollama） — real_llm

把 `experiments/` 从「确定性 mock」扩展到「真实本地模型」，核心是补齐规划层的
模型契约（`LLMClient.complete()`，见 youmi/llm/client.py）并封装 Ollama 接入。

设计原则：
- **先留接口，不主动联网**。模型下载中 / 服务未启动时，本模块只负责构造客户端
  与健康检查，调用方应先 `await check_ollama(...)` 确认目标模型就绪，再跑真实实验。
- Ollama 通过 OpenAI 兼容端点 `/v1` 提供服务，`LLMClient` / `EmbeddingClient`
  本身已支持（provider=LOCAL / base_url 指向 /v1），这里只做「约定集中管理」。

用法::

    from experiments.real_llm import make_ollama_llm, make_ollama_embedding, check_ollama

    BASE = "http://localhost:11434/v1"

    # 1) 先探活（模型未下载会给出友好提示，不会抛异常）
    status = await check_ollama(BASE, model="qwen2.5:3b")
    print(status)

    # 2) 就绪后构造客户端，交给 run_plan_reuse_comparison 跑真实消融
    llm = make_ollama_llm(base_url=BASE, model="qwen2.5:3b")
    emb = make_ollama_embedding(base_url=BASE, model="nomic-embed-text")
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from youmi.core.types import LLMConfig, LLMProvider
from youmi.llm.client import LLMClient
from youmi.llm.embeddings import EmbeddingClient

from experiments.fake_llm import CostModel

logger = logging.getLogger(__name__)

# Ollama 默认服务地址（OpenAI 兼容 /v1 端点）
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"

# 项目约定：规划用 Qwen2.5 7B instruct（量化版，YouMi_Agent 真实模型），
# 向量用 bge-m3（生成模型无法走 /v1/embeddings，必须用专门 embedding 模型）。
# bge-m3 中文语义区分度优于 nomic-embed-text（同族/跨族 gap +0.29 vs +0.21），
# 且跨族相似度更低（0.53 vs 0.67），更利于 PlanMemory 干净切分命中阈值。
DEFAULT_LLM_MODEL = "qwen2.5:7b-instruct-q3_K_M"
DEFAULT_EMBEDDING_MODEL = "bge-m3"

# 真实模型规划输出常见噪声（qwen 会把 JSON 包进 markdown 代码块 / 字符串），
# 解析时按顺序尝试这些剥离策略。
_JSON_FENCE_PREFIXES = ("```json", "```")


# ---------------------------------------------------------------------------
# 客户端工厂
# ---------------------------------------------------------------------------

def make_ollama_llm(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    model: str = DEFAULT_LLM_MODEL,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    timeout_s: int = 300,
    **extra_params: Any,
) -> LLMClient:
    """构造走 Ollama 的 `LLMClient`（已实现 `complete()` 契约）。

    Args:
        base_url: Ollama OpenAI 兼容端点，形如 ``http://localhost:11434/v1``
        model: 已下载的模型名（如 ``qwen2.5:3b``）
        temperature: 规划类任务建议 0.0（确定性输出，便于骨架复用）
        max_tokens: 单次回复最大 token
        timeout_s: 本地小模型推理较慢，默认放宽到 300s
        **extra_params: 透传到 LLMConfig.extra_params（如 num_ctx 等 Ollama 特有参数）
    """
    config = LLMConfig(
        provider=LLMProvider.LOCAL,
        model=model,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        extra_params=extra_params,
    )
    return LLMClient(config)


def make_ollama_embedding(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    timeout: int = 60,
) -> EmbeddingClient:
    """构造走 Ollama 的 `EmbeddingClient`。

    Args:
        base_url: Ollama OpenAI 兼容端点（含 /v1）
        model: 已下载的 embedding 模型名（如 ``nomic-embed-text``）
        timeout: 请求超时秒数
    """
    return EmbeddingClient(base_url=base_url, model=model, timeout=timeout)


# ---------------------------------------------------------------------------
# 记账适配器（真实模型 → 复用现有 metrics 逻辑）
# ---------------------------------------------------------------------------

class MeteredLLMClient:
    """包装真实 LLM 客户端，提供与 `FakeLLMClient` 一致的记账接口。

    `plan_reuse_eval.run_plan_reuse` 依赖一组记账属性（``full_calls`` /
    ``adapt_calls`` / ``total_tokens`` / ``total_cost`` / ``total_latency_ms``）。
    本类把真实客户端包一层，让真实实验也能走同一套指标计算：

    - 调用次数：依据消息是否含「已有骨架」区分 full（全量规划）与 adapt（模板适配）；
    - token：优先取真实 ``usage``（prompt + completion），缺失时按 4 字符/token 估算；
    - 延迟：真实计时；
    - 成本：``tokens × price_per_token``（沿用 CostModel 的「昂贵 vs 廉价」权重）。

    .. note::
        本地 Ollama 若 full 与 adapt 用**同一个模型**，则两者单价相同，此时应把
        ``cost_full`` 与 ``cost_adapt`` 都设为相同单价，复用收益主要体现在**延迟**
        而非成本（甚至因重传骨架使 token 略增）。这正是 APC 采用「大模型规划 +
        小模型适配」分层的原因——要省钱，需要模型价格分层。
    """

    def __init__(
        self,
        inner: Any,
        inner_adapt: Any | None = None,
        cost_full: CostModel | None = None,
        cost_adapt: CostModel | None = None,
    ) -> None:
        self.inner = inner
        # inner_adapt 为空时与 inner 同模型（单模型：复用收益主要在延迟）；
        # 指定后实现「大模型规划 + 小模型适配」分层（对应 APC 论文，复用才真正省钱）。
        self.inner_adapt = inner_adapt or inner
        self.cost_full = cost_full or CostModel(price_per_token=1.0)
        self.cost_adapt = cost_adapt or CostModel(price_per_token=1.0)

        self.full_calls: int = 0
        self.adapt_calls: int = 0
        self.total_tokens: int = 0
        self.total_cost: float = 0.0
        self.total_latency_ms: float = 0.0

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        text = self._join_content(messages)
        is_adapt = "已有骨架" in text
        model = self.cost_adapt if is_adapt else self.cost_full
        inner = self.inner_adapt if is_adapt else self.inner

        t0 = time.perf_counter()
        resp = await inner.chat(messages)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        out = resp.content or ""

        # 真实 token 优先，缺失回退估算
        usage = getattr(resp, "usage", {}) or {}
        in_tok = usage.get("prompt_tokens")
        out_tok = usage.get("completion_tokens")
        if in_tok is None or out_tok is None:
            in_tok = int(len(text) / model.chars_per_token) + 1
            out_tok = int(len(out) / model.chars_per_token) + 1
        tokens = int(in_tok) + int(out_tok)

        if is_adapt:
            self.adapt_calls += 1
        else:
            self.full_calls += 1
        self.total_tokens += tokens
        self.total_cost += tokens * model.price_per_token
        self.total_latency_ms += latency_ms
        return out

    @staticmethod
    def _join_content(messages: list[dict[str, Any]]) -> str:
        return "\n".join(m.get("content") or "" for m in messages)

    async def close(self) -> None:
        """关闭底层 HTTP 连接（分层时两个 inner 都要关，去重避免关两次）。"""
        seen: set[int] = set()
        for inner in (self.inner, self.inner_adapt):
            if id(inner) in seen:
                continue
            seen.add(id(inner))
            if hasattr(inner, "close"):
                await inner.close()

    def __repr__(self) -> str:
        return (
            f"<MeteredLLMClient inner={self.inner!r} "
            f"full={self.full_calls} adapt={self.adapt_calls} "
            f"latency={self.total_latency_ms:.0f}ms>"
        )


def make_metered_ollama_llm(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    model: str = DEFAULT_LLM_MODEL,
    *,
    adapt_model: str | None = None,
    cost_full: CostModel | None = None,
    cost_adapt: CostModel | None = None,
    **llm_kwargs: Any,
) -> MeteredLLMClient:
    """构造「可记账」的 Ollama LLM 客户端，可直接交给 ``run_plan_reuse``。

    等价于 `MeteredLLMClient(make_ollama_llm(...))`。默认 full 与 adapt 同模型
    （本地单模型，如实反映 token 成本）；传 ``adapt_model``（如 ``qwen2.5:0.5b``）
    实现「大模型规划 + 小模型适配」分层定价，此时可同时传 ``cost_full`` /
    ``cost_adapt`` 表达两档单价。
    """
    inner = make_ollama_llm(base_url=base_url, model=model, **llm_kwargs)
    inner_adapt = None
    if adapt_model is not None:
        # 适配小模型可放宽 max_tokens（只改 task 字段，输出更短）
        inner_adapt = make_ollama_llm(
            base_url=base_url, model=adapt_model,
            max_tokens=llm_kwargs.get("max_tokens", 4096),
            **{k: v for k, v in llm_kwargs.items() if k != "max_tokens"},
        )
    return MeteredLLMClient(
        inner,
        inner_adapt=inner_adapt,
        cost_full=cost_full,
        cost_adapt=cost_adapt,
    )


# ---------------------------------------------------------------------------
# 健康检查 / 探活
# ---------------------------------------------------------------------------

async def check_ollama(
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    model: str = DEFAULT_LLM_MODEL,
) -> dict[str, Any]:
    """探活 Ollama 服务并检查目标模型是否已就绪（不抛异常）。

    通过 OpenAI 兼容的 ``GET /models`` 列出可用模型，判断 ``model`` 是否在列。

    Returns:
        dict，形如::

            {"ok": True, "model": "qwen2.5:3b", "available": [...], "hint": ""}

        服务未启动 / 模型未下载时 ``ok=False``，并给出可读的 ``hint``。
    """
    url = base_url.rstrip("/") + "/models"
    result: dict[str, Any] = {
        "ok": False,
        "model": model,
        "available": [],
        "hint": "",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            result["hint"] = (
                f"Ollama 返回 {resp.status_code}。请确认服务已启动（`ollama serve`）"
                f"且地址 {base_url} 正确。"
            )
            return result
        data = resp.json()
    except httpx.HTTPError as exc:
        result["hint"] = (
            f"无法连接 Ollama（{base_url}）：{exc}。请确认服务已启动（`ollama serve`）。"
        )
        return result

    # OpenAI /models 返回 {"data": [{"id": "qwen2.5:3b", ...}, ...]}
    # 注意：Ollama 会把首次下载的模型记为 ``<name>:latest``，调用时传 ``<name>``
    # 也能解析，故这里做「去掉 :latest 后缀」的归一化匹配。
    models = [m.get("id", "") for m in data.get("data", [])]
    result["available"] = models

    def _norm(name: str) -> str:
        return name[:-len(":latest")] if name.endswith(":latest") else name

    norm_model = _norm(model)
    if any(_norm(m) == norm_model for m in models):
        result["ok"] = True
        result["hint"] = f"模型 {model} 已就绪。"
    else:
        result["hint"] = (
            f"模型 {model} 尚未就绪（当前可用: {models or '无'}）。"
            f"请先执行 `ollama pull {model}` 下载。"
        )
    return result


__all__ = [
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_EMBEDDING_MODEL",
    "make_ollama_llm",
    "make_ollama_embedding",
    "make_metered_ollama_llm",
    "MeteredLLMClient",
    "check_ollama",
]
