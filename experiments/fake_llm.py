"""
确定性 mock LLM / Embedding (fake_llm)

为评测提供**离线、可复现**的模型替身，不依赖网络或真实模型：

- `FakeLLMClient`：实现 WorkflowPlanner 所依赖的 `complete(messages) -> str`
  契约（注意：真实 `LLMClient` 目前只有 `chat` / `chat_stream`，**没有
  `complete`**，故规划层在接入真实模型时存在契约缺口，见 README 一节）。
  同时实现成本/延迟记账，用于量化「复用 vs 全量规划」。

- `FakeEmbeddingClient`：基于字符 n-gram 哈希的确定性嵌入，语义相近的文本
  得到相近向量，供 PlanMemory / GlobalMemory 的向量检索路径使用。

成本模型（对齐 Agentic Plan Caching 的「昂贵大模型 vs 廉价小模型」逻辑）：

- 全量规划 (full)：昂贵推理模型，价格高、延迟高
- 模板适配 (adapt)：轻量小模型，价格低（默认 1/20）、延迟低

每次调用的成本 = (输入 token + 输出 token) × 模型单价，
其中 token 按「中文 ~4 字符/token」估算。这样即使适配调用输入包含完整骨架，
其总成本仍远低于全量规划，正确体现「复用省钱」的收益。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from youmi.llm.client import LLMResponse
from youmi.coordinator.plan import WorkflowPlan


# ---------------------------------------------------------------------------
# 成本模型
# ---------------------------------------------------------------------------

@dataclass
class CostModel:
    """一次 LLM 调用的成本/延迟估算模型。

    Args:
        price_per_token: 模型单价（相对值，越大越贵）
        chars_per_token: 估算 token 用的「字符/token」系数（中文约 4）
        latency_base_ms: 固定延迟
        latency_per_char_ms: 每字符延迟
    """

    price_per_token: float = 1.0
    chars_per_token: float = 4.0
    latency_base_ms: float = 500.0
    latency_per_char_ms: float = 10.0

    def estimate(self, input_text: str, output_text: str) -> dict[str, Any]:
        in_tokens = int(len(input_text) / self.chars_per_token) + 1
        out_tokens = int(len(output_text) / self.chars_per_token) + 1
        tokens = in_tokens + out_tokens
        cost = tokens * self.price_per_token
        latency_ms = self.latency_base_ms + len(output_text) * self.latency_per_char_ms
        return {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "tokens": tokens,
            "cost": cost,
            "latency_ms": latency_ms,
        }


# ---------------------------------------------------------------------------
# JSON 提取
# ---------------------------------------------------------------------------

def _extract_json_block(s: str) -> str:
    """从文本中提取第一个平衡的大括号 JSON 块（容忍前后噪声）。"""
    start = s.find("{")
    if start == -1:
        raise ValueError("文本中不含 JSON 对象")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    raise ValueError("JSON 括号不平衡")


# ---------------------------------------------------------------------------
# FakeLLMClient
# ---------------------------------------------------------------------------

class FakeLLMClient:
    """确定性 mock LLM 客户端，实现 `complete()` 契约并记账成本。

    Args:
        plans_by_task: task 文本 -> gold WorkflowPlan 映射（用于全量规划返回）
        cost_full: 全量规划成本模型（昂贵模型）
        cost_adapt: 模板适配成本模型（廉价模型）
    """

    def __init__(
        self,
        plans_by_task: dict[str, WorkflowPlan] | None = None,
        cost_full: CostModel | None = None,
        cost_adapt: CostModel | None = None,
    ) -> None:
        self.plans_by_task = plans_by_task or {}
        self.cost_full = cost_full or CostModel(
            price_per_token=1.0,
            latency_base_ms=500.0, latency_per_char_ms=10.0,
        )
        self.cost_adapt = cost_adapt or CostModel(
            price_per_token=0.05,  # 小模型约为大模型 1/20 单价
            latency_base_ms=100.0, latency_per_char_ms=2.0,
        )

        # 记账
        self.calls: list[dict[str, Any]] = []
        self.full_calls: int = 0
        self.adapt_calls: int = 0
        self.total_tokens: int = 0      # 原始 token 总量（输入+输出）
        self.total_cost: float = 0.0    # 价格加权成本
        self.total_latency_ms: float = 0.0

    # ------------------------------------------------------------------
    # 记账快照（用于隔离 warmup / test 阶段成本）
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "full_calls": self.full_calls,
            "adapt_calls": self.adapt_calls,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "total_latency_ms": self.total_latency_ms,
        }

    def reset(self) -> None:
        self.calls.clear()
        self.full_calls = 0
        self.adapt_calls = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.total_latency_ms = 0.0

    # ------------------------------------------------------------------
    # complete —— WorkflowPlanner 依赖的契约
    # ------------------------------------------------------------------

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        """返回一个合法 WorkflowPlan JSON（字符串）。

        依据消息内容区分「全量规划」与「模板适配」两种模式并分别记账。
        """
        text = self._join_content(messages)
        if "已有骨架" in text:
            out = self._handle_adapt(text)
            self._record(self.cost_adapt, "adapt", text, out)
            return out
        out = self._handle_full(text)
        self._record(self.cost_full, "full", text, out)
        return out

    def _handle_full(self, text: str) -> str:
        task = self._extract_task_full(text)
        plan = self.plans_by_task.get(task)
        if plan is None:
            # 未知任务：兜底单步 Plan
            plan = WorkflowPlan.model_validate({
                "name": "通用任务",
                "steps": [{
                    "step_id": "step1",
                    "role": "generalist",
                    "task": task[:200],
                    "depends_on": [],
                }],
            })
        return plan.model_dump_json()

    def _handle_adapt(self, text: str) -> str:
        task, skeleton = self._extract_adapt(text)
        data = json.loads(skeleton)
        # 只改写各步骤 task 文本，保留 step_id / role / depends_on 骨架
        for s in data.get("steps", []):
            s["task"] = f"{task}（{s.get('role', '')}步骤）"
        return json.dumps(data, ensure_ascii=False)

    def _record(
        self,
        model: CostModel,
        mode: str,
        input_text: str,
        output_text: str,
    ) -> None:
        est = model.estimate(input_text, output_text)
        if mode == "full":
            self.full_calls += 1
        else:
            self.adapt_calls += 1
        self.total_tokens += est["tokens"]
        self.total_cost += est["cost"]
        self.total_latency_ms += est["latency_ms"]
        self.calls.append({"mode": mode, **est})

    # ------------------------------------------------------------------
    # 文本解析
    # ------------------------------------------------------------------

    @staticmethod
    def _join_content(messages: list[dict[str, Any]]) -> str:
        return "\n".join(m.get("content") or "" for m in messages)

    @staticmethod
    def _extract_task_full(text: str) -> str:
        marker = "生成 WorkflowPlan："
        idx = text.find(marker)
        if idx != -1:
            return text[idx + len(marker):].strip()
        return text.strip()

    @staticmethod
    def _extract_adapt(text: str) -> tuple[str, str]:
        if "新任务：" not in text or "已有骨架：" not in text:
            raise ValueError("adapt 消息格式不符")
        head = text.split("新任务：", 1)[1]
        task, rest = head.split("已有骨架：", 1)
        skeleton = _extract_json_block(rest)
        return task.strip(), skeleton

    # ------------------------------------------------------------------
    # chat —— 兜底（供未来扩展到 Agent 执行路径）
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> LLMResponse:
        """最小 chat 实现，返回固定文本（不含 tool_calls）。"""
        raw = {
            "choices": [{
                "message": {"role": "assistant", "content": "mock-response"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return LLMResponse(raw)

    def __repr__(self) -> str:
        return (
            f"<FakeLLMClient full_calls={self.full_calls} "
            f"adapt_calls={self.adapt_calls} cost={self.total_cost:.1f}>"
        )


# ---------------------------------------------------------------------------
# FakeEmbeddingClient
# ---------------------------------------------------------------------------

class FakeEmbeddingClient:
    """确定性字符 n-gram 哈希嵌入。

    语义相近文本（共享字符 n-gram）得到相近向量；无需网络，可复现。
    """

    def __init__(self, dim: int = 256, n: int = 3, seed: int = 0) -> None:
        self.dim = dim
        self.n = n
        self.seed = seed
        self.calls: int = 0

    def _ngrams(self, text: str) -> list[str]:
        t = text.lower()
        if len(t) < self.n:
            return [t]
        return [t[i:i + self.n] for i in range(len(t) - self.n + 1)]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for gram in self._ngrams(text):
            h = int(hashlib.md5(gram.encode("utf-8")).hexdigest(), 16) % self.dim
            vec[h] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def embed_one(self, text: str) -> list[float]:
        self.calls += 1
        return self._vector(text)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def __repr__(self) -> str:
        return f"<FakeEmbeddingClient dim={self.dim} n={self.n} calls={self.calls}>"


__all__ = [
    "FakeLLMClient",
    "FakeEmbeddingClient",
    "CostModel",
    "_extract_json_block",
]
