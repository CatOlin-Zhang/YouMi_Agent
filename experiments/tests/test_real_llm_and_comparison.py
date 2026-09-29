"""
真实模型接口 + 论文对比 测试 (test_real_llm & test_paper_comparison)

验证：
- LLMClient 已补齐 `complete()` 契约
- MeteredLLMClient 记账正确（复用 FakeLLMClient 的 LLMResponse）
- 论文对比数据完整、ours 唯一
- HTML 渲染包含关键区块
- check_ollama 服务不可达时优雅返回（不抛异常）
"""

from __future__ import annotations

import pytest

from youmi.llm.client import LLMClient

from experiments import paper_comparison, render_html
from experiments.benchmark import Benchmark
from experiments.fake_llm import FakeLLMClient
from experiments.real_llm import MeteredLLMClient, check_ollama


def _plan_messages(task: str) -> list[dict]:
    return [
        {"role": "system", "content": "你是一个工作流规划专家。"},
        {"role": "user", "content": f"请为以下任务生成 WorkflowPlan：\n\n{task}"},
    ]


def _adapt_messages(task: str, skeleton: str) -> list[dict]:
    return [
        {"role": "system", "content": "你是一个工作流适配专家。"},
        {"role": "user", "content": (
            f"新任务：{task}\n\n已有骨架：\n{skeleton}\n\n"
            "请输出适配后的 WorkflowPlan JSON："
        )},
    ]


# ---------------------------------------------------------------------------
# 契约补齐
# ---------------------------------------------------------------------------

def test_llm_client_has_complete():
    """规划层依赖的 complete() 契约已补齐（之前是缺口）。"""
    assert hasattr(LLMClient, "complete")
    assert callable(LLMClient.complete)


# ---------------------------------------------------------------------------
# MeteredLLMClient 记账
# ---------------------------------------------------------------------------

async def test_metered_llm_accounting():
    b = Benchmark()
    inner = FakeLLMClient(plans_by_task=b.plans_by_task)
    metered = MeteredLLMClient(inner)

    task = b.warmup_tasks()[0]
    await metered.complete(_plan_messages(task))

    assert metered.full_calls == 1
    assert metered.adapt_calls == 0
    assert metered.total_tokens > 0          # 取真实 usage（FakeLLM 的 mock usage）
    assert metered.total_cost > 0
    assert metered.total_latency_ms >= 0     # 真实计时

    # 适配调用 → adapt_calls 递增
    fam = b.families[0]
    await metered.complete(_adapt_messages("新任务", fam.gold_plan.model_dump_json()))
    assert metered.adapt_calls == 1
    assert metered.full_calls == 1
    assert metered.total_tokens > 0


async def test_metered_same_price_reflects_token_cost():
    """本地单模型（full/adapt 同价）时，成本 = token 总量（如实反映重传骨架开销）。"""
    b = Benchmark()
    metered = MeteredLLMClient(FakeLLMClient(plans_by_task=b.plans_by_task))
    task = b.warmup_tasks()[0]
    await metered.complete(_plan_messages(task))
    assert metered.total_cost == pytest.approx(metered.total_tokens * 1.0)


async def test_metered_layered_adapt_uses_small_model():
    """分层（inner_adapt 指定小模型）时，适配调用走小模型、全量走大模型。"""
    from youmi.llm.client import LLMResponse

    class _TaggedInner:
        def __init__(self, tag: str) -> None:
            self.tag = tag
            self.calls = 0

        async def chat(self, messages: list[dict]) -> LLMResponse:
            self.calls += 1
            return LLMResponse({
                "choices": [{"message": {"role": "assistant", "content": self.tag},
                             "finish_reason": "stop"}],
                "usage": {},
            })

    full_inner = _TaggedInner("FULL")
    adapt_inner = _TaggedInner("ADAPT")
    metered = MeteredLLMClient(full_inner, inner_adapt=adapt_inner)

    b = Benchmark()
    fam = b.families[0]
    out_adapt = await metered.complete(
        _adapt_messages("新任务", fam.gold_plan.model_dump_json())
    )
    out_full = await metered.complete(_plan_messages(b.warmup_tasks()[0]))

    # 返回值证明路由到了正确的 inner
    assert out_adapt == "ADAPT" and out_full == "FULL"
    assert adapt_inner.calls == 1 and full_inner.calls == 1
    assert metered.adapt_calls == 1 and metered.full_calls == 1


async def test_metered_close_dedup_inner():
    """close() 去重：单模型（inner==inner_adapt）不应重复关闭。"""
    b = Benchmark()
    inner = FakeLLMClient(plans_by_task=b.plans_by_task)
    metered = MeteredLLMClient(inner)  # inner_adapt 默认 = inner
    await metered.complete(_plan_messages(b.warmup_tasks()[0]))
    # FakeLLMClient 无 close()，这里仅验证不抛异常
    await metered.close()


# ---------------------------------------------------------------------------
# 论文对比数据
# ---------------------------------------------------------------------------

def test_methods_integrity():
    assert len(paper_comparison.METHODS) == 7
    keys = [m.key for m in paper_comparison.METHODS]
    assert len(set(keys)) == len(keys)          # key 唯一
    ours = [m for m in paper_comparison.METHODS if m.is_ours]
    assert len(ours) == 1                        # 本项目唯一


def test_quant_schemes():
    names = [m.name for m in paper_comparison.QUANT_SCHEMES]
    assert "YouMi Agent" in names
    assert "APC" in names
    assert "AgentReuse" in names


def test_paper_facts():
    """关键论文事实与检索结果一致（防止手滑改错）。"""
    apc = paper_comparison.get("apc")
    assert apc.year == 2025 and apc.venue == "NeurIPS"
    assert apc.cost_savings_pct == pytest.approx(50.31)
    assert apc.latency_savings_pct == pytest.approx(27.28)

    ar = paper_comparison.get("agentreuse")
    assert ar.year == 2024
    assert ar.latency_savings_pct == pytest.approx(93.12)


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------

def test_render_html_contains_sections():
    html = render_html.render_html()
    for kw in ("定位图", "定量收益对比", "机制对比矩阵", "YouMi Agent", "APC", "LEGOMem"):
        assert kw in html, f"HTML 缺少区块: {kw}"


# ---------------------------------------------------------------------------
# check_ollama 优雅降级
# ---------------------------------------------------------------------------

async def test_check_ollama_graceful_on_connection_error():
    """服务不可达时返回 ok=False + hint，而非抛异常。"""
    res = await check_ollama(base_url="http://127.0.0.1:59999/v1", model="qwen2.5:3b")
    assert res["ok"] is False
    assert res["hint"]
    assert res["model"] == "qwen2.5:3b"


# ---------------------------------------------------------------------------
# 阈值/维度按 embedding 模型自适应
# ---------------------------------------------------------------------------

def test_embedding_dim_and_threshold_by_model():
    from experiments.run_real_eval import _embedding_dim, _threshold_for

    assert _embedding_dim("bge-m3") == 1024
    assert _embedding_dim("nomic-embed-text") == 768
    assert _embedding_dim("unknown-model") == 768  # 未知默认 768

    assert _threshold_for("bge-m3") == 0.66
    assert _threshold_for("nomic-embed-text") == 0.60
    assert _threshold_for("unknown-model") == 0.66  # 未知默认 bge-m3 定标
