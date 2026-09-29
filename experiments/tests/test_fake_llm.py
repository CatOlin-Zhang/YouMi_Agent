"""
mock LLM / Embedding 测试 (test_fake_llm)

验证：
- complete() 全量规划返回合法 JSON
- complete() 模板适配保留骨架
- 成本记账正确（full vs adapt）
- FakeEmbeddingClient 语义相近 → 高相似度
"""

from __future__ import annotations

import json

import pytest

from youmi.coordinator.plan import WorkflowPlan

from experiments.benchmark import Benchmark, skeleton_key
from experiments.fake_llm import FakeLLMClient, FakeEmbeddingClient, _extract_json_block


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
            "请输出适配后的 WorkflowPlan JSON（仅修改各步骤的 task 字段）："
        )},
    ]


async def test_complete_full_returns_valid_plan():
    b = Benchmark()
    task = b.warmup_tasks()[0]
    fake = FakeLLMClient(plans_by_task=b.plans_by_task)

    out = await fake.complete(_plan_messages(task))

    plan = WorkflowPlan.model_validate_json(out)
    assert not plan.validate()
    assert fake.full_calls == 1
    assert fake.adapt_calls == 0
    # 返回的是该任务的 gold 骨架
    assert skeleton_key(plan) == skeleton_key(b.gold_plan_for(task))


async def test_complete_adapt_preserves_skeleton():
    b = Benchmark()
    fam = b.families[0]
    fake = FakeLLMClient(plans_by_task=b.plans_by_task)
    skeleton = fam.gold_plan.model_dump_json()

    out = await fake.complete(_adapt_messages("调研 GPU 行业现状", skeleton))

    data = json.loads(out)
    plan = WorkflowPlan.model_validate_json(out)
    assert not plan.validate()
    # 骨架（角色+依赖）保持不变
    assert skeleton_key(plan) == skeleton_key(fam.gold_plan)
    # task 文本被改写
    assert all("GPU" in s["task"] for s in data["steps"])
    assert fake.adapt_calls == 1
    assert fake.full_calls == 0


async def test_cost_ledger_accounting():
    b = Benchmark()
    fake = FakeLLMClient(plans_by_task=b.plans_by_task)
    task = b.warmup_tasks()[0]

    await fake.complete(_plan_messages(task))
    assert fake.total_tokens > 0
    assert fake.total_latency_ms > 0
    assert len(fake.calls) == 1
    assert fake.calls[0]["mode"] == "full"

    # 适配成本应低于全量规划
    fam = b.families[0]
    await fake.complete(_adapt_messages("新任务", fam.gold_plan.model_dump_json()))
    full_cost = fake.calls[0]
    adapt_cost = fake.calls[1]
    assert adapt_cost["mode"] == "adapt"
    assert adapt_cost["latency_ms"] < full_cost["latency_ms"]
    # 价格加权成本：适配用廉价小模型，即使输入含完整骨架也远低于全量规划
    assert adapt_cost["cost"] < full_cost["cost"]


async def test_embedding_similarity():
    emb = FakeEmbeddingClient(dim=256)
    a = await emb.embed_one("调研 HBM 行业现状并生成投资分析报告")
    b2 = await emb.embed_one("调研 GPU 行业现状并生成投资分析报告")
    c = await emb.embed_one("帮我写一首关于春天的五言绝句")

    def cos(x, y):
        return sum(i * j for i, j in zip(x, y))

    assert cos(a, b2) > 0.7      # 同族改写 → 高相似
    assert cos(a, c) < 0.3       # 无关任务 → 低相似


def test_extract_json_block_tolerates_noise():
    raw = "前面噪声 ```json\n" + json.dumps({"a": 1, "b": {"c": 2}}) + "\n``` 后面噪声"
    block = _extract_json_block(raw)
    assert json.loads(block) == {"a": 1, "b": {"c": 2}}


def test_extract_json_block_raises_on_no_json():
    with pytest.raises(ValueError):
        _extract_json_block("没有 json")
