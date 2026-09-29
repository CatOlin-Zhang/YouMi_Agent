"""
计划复用实验测试 (test_plan_reuse_eval)

验证消融实验的机制正确性：
- 复用模式相对基线减少全量规划调用、降低 token/延迟成本
- 命中走 fast path（source == memory）
- 复用精度 / 召回 / 陌生任务拒绝率在合理区间
- 降低阈值会引入跨族误命中（精度 < 1），验证精度指标的敏感度
"""

from __future__ import annotations

import pytest

from experiments.benchmark import Benchmark
from experiments.plan_reuse_eval import (
    run_plan_reuse,
    run_plan_reuse_comparison,
)


async def test_reuse_reduces_cost_vs_baseline():
    cmp = await run_plan_reuse_comparison(Benchmark(), similarity_threshold=0.55)

    # 复用减少全量规划调用
    assert cmp.reuse.full_gen_calls < cmp.baseline.full_gen_calls
    # 昂贵全量规划调用数下降
    assert cmp.full_gen_call_savings > 0
    # 价格加权成本、延迟下降（复用把昂贵全量规划换成廉价适配）
    assert cmp.cost_savings > 0
    assert cmp.latency_savings > 0
    # 复用模式产生命中
    assert cmp.reuse.cache_hits > 0


async def test_reuse_quality_metrics():
    cmp = await run_plan_reuse_comparison(Benchmark(), similarity_threshold=0.55)
    r = cmp.reuse

    # 同族改写任务大多命中
    assert r.reuse_recall >= 0.8
    # 命中的骨架基本正确
    assert r.reuse_precision >= 0.9
    # 陌生任务不应误命中
    assert r.novel_precision == 1.0
    # 所有 plan 合法
    assert r.plan_validity >= 0.9


async def test_baseline_never_hits_cache():
    b = Benchmark()
    res = await run_plan_reuse(b, use_memory=False, similarity_threshold=0.55)
    assert res.cache_hits == 0
    assert res.cache_misses == res.test_tasks
    # 基线无适配调用
    assert res.adapt_calls == 0


async def test_low_threshold_introduces_false_positive():
    """阈值极低时，陌生任务也会误命中 → 精度下降（验证精度指标的敏感度）。"""
    b = Benchmark()
    res = await run_plan_reuse(b, use_memory=True, similarity_threshold=0.0)
    # 低阈值下陌生任务被误复用
    assert res.novel_precision < 1.0
    assert res.reuse_precision < 1.0


async def test_deterministic_rerun():
    """同参数重跑结果应完全一致（可复现性）。"""
    b1 = Benchmark()
    b2 = Benchmark()
    c1 = await run_plan_reuse_comparison(b1, similarity_threshold=0.55)
    c2 = await run_plan_reuse_comparison(b2, similarity_threshold=0.55)
    assert c1.baseline.as_dict() == c2.baseline.as_dict()
    assert c1.reuse.as_dict() == c2.reuse.as_dict()
