"""
计划复用消融实验 (plan_reuse_eval)

验证创新点 **Plan-then-Execute + PlanMemory** 的核心收益：

- 基线 (baseline)：无记忆，每个任务都触发 LLM 全量规划
- 复用 (reuse)：命中 PlanMemory 时复用骨架，仅做轻量适配

对比维度：规划 LLM 调用次数、token 成本、延迟、命中率、复用精度/召回、
以及「复用是否保持骨架正确」（对应论文中的 cost/latency 节省 + 精度保持）。

流程：
1. warmup：各族的 seed 任务（复用模式下首次规划后写入 PlanMemory）
2. test：同族 variants（应命中）+ 陌生 novel 任务（应 miss）
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from youmi.coordinator.plan_memory import PlanMemory
from youmi.coordinator.planner import WorkflowPlanner

from experiments.benchmark import Benchmark, same_skeleton, same_role_skeleton, skeleton_key
from experiments.fake_llm import FakeLLMClient, FakeEmbeddingClient
from experiments.metrics import Comparison, PlanReuseResult


def _fidelity_holds(plan: Any, template_skeleton: Any) -> bool:
    """适配后 plan 骨架是否 == 命中模板骨架（骨架保真率判定）。

    ``template_skeleton`` 是 planner 写入 metadata 的模板骨架签名，经 JSON 序列化后
    为 list of [step_id, role, [depends_on...]]，需归一化为与 ``skeleton_key`` 相同的
    tuple 结构再比较。
    """
    if not template_skeleton:
        return False
    try:
        tpl_key = tuple(sorted(
            (item[0], item[1], tuple(sorted(item[2])))
            for item in template_skeleton
        ))
        return skeleton_key(plan) == tpl_key
    except (TypeError, IndexError):
        return False


async def run_plan_reuse(
    benchmark: Benchmark,
    use_memory: bool,
    similarity_threshold: float = 0.55,
    embedding_dim: int = 256,
    llm_client: Any | None = None,
    embedding_client: Any | None = None,
    use_role_skeleton: bool = False,
) -> PlanReuseResult:
    """运行一轮计划复用实验。

    Args:
        benchmark: 合成任务基准（带真值标签）
        use_memory: True = 启用 PlanMemory（复用），False = 基线
        similarity_threshold: PlanMemory 命中阈值
        embedding_dim: 假 embedding 维度（仅 mock 模式生效）
        llm_client: 可选**真实** LLM 客户端（需实现 ``complete()`` 并暴露
            full_calls/adapt_calls/total_tokens/total_cost/total_latency_ms
            记账属性，如 ``real_llm.MeteredLLMClient``）。缺省用确定性 mock。
        embedding_client: 可选真实 embedding 客户端（需实现 ``embed_one()``）。
            缺省用 mock。真实 embedding 时请把 ``embedding_dim`` 设为模型真实维度
            （如 nomic-embed-text 为 768）。
        use_role_skeleton: True 时复用精度用「角色级骨架」判定（忽略 step_id 命名
            差异）。真实模型会自由命名 step_id，与手写 gold 不一致，故真实评测应
            置 True；mock 模式（返回 gold plan）保持默认 False。
    """
    skeleton_check = same_role_skeleton if use_role_skeleton else same_skeleton
    if llm_client is not None:
        fake = llm_client  # 真实客户端（已实现 complete + 记账）
    else:
        fake = FakeLLMClient(plans_by_task=benchmark.plans_by_task)

    if embedding_client is not None:
        embedder = embedding_client
    else:
        embedder = FakeEmbeddingClient(dim=embedding_dim)

    mem: PlanMemory | None = None
    if use_memory:
        mem = PlanMemory(
            db_path=":memory:",
            embedding_client=embedder,
            similarity_threshold=similarity_threshold,
            embedding_dim=embedding_dim,
        )
        await mem.initialize()

    master = SimpleNamespace(_llm_client=fake)
    planner = WorkflowPlanner(master, plan_memory=mem, max_retries=0)

    res = PlanReuseResult(label="reuse" if use_memory else "baseline")

    warmup = benchmark.warmup_tasks()
    test = benchmark.test_tasks()
    res.warmup_tasks = len(warmup)
    res.test_tasks = len(test)
    res.total_tasks = len(warmup) + len(test)

    # ---- warmup（复用模式下写入记忆）----
    for task in warmup:
        plan = await planner.generate_plan(task)
        if mem is not None:
            await mem.save_plan(task, plan, success=True)

    # ---- test（计量主体）----
    for task in test:
        plan = await planner.generate_plan(task)
        _count_validity(res, plan)

        is_hit = plan.metadata.get("source") == "memory"
        fam = benchmark.family_of(task)

        if is_hit:
            res.cache_hits += 1
            gold = benchmark.gold_plan_for(task)
            if gold is not None and skeleton_check(plan, gold):
                res.hit_correct_skeleton += 1
            # 骨架保真率（真实模型质量口径）：适配后骨架 == 命中模板骨架
            if _fidelity_holds(plan, plan.metadata.get("template_skeleton")):
                res.hit_fidelity += 1
        else:
            res.cache_misses += 1

        if fam is not None:
            # 同族改写任务（recall 分母）
            res.variant_total += 1
            if is_hit:
                res.variant_hits += 1
        else:
            # 陌生任务（应 miss）
            res.novel_total += 1
            if not is_hit:
                res.novel_missed += 1

    # ---- 成本（整轮累计）----
    res.full_gen_calls = fake.full_calls
    res.adapt_calls = fake.adapt_calls
    res.total_tokens = fake.total_tokens
    res.total_cost = fake.total_cost
    res.total_latency_ms = fake.total_latency_ms

    if mem is not None:
        await mem.close()

    return res


def _count_validity(res: PlanReuseResult, plan: Any) -> None:
    if plan.validate():
        res.invalid_plans += 1
    else:
        res.valid_plans += 1


async def run_plan_reuse_comparison(
    benchmark: Benchmark | None = None,
    similarity_threshold: float = 0.55,
    embedding_dim: int = 256,
    llm_client: Any | None = None,
    embedding_client: Any | None = None,
    use_role_skeleton: bool = False,
) -> Comparison:
    """运行 baseline vs reuse 对比，返回 Comparison。

    传入 ``llm_client`` / ``embedding_client`` 即可用真实模型跑真实消融；
    二者缺省时用确定性 mock（离线、可复现）。
    """
    benchmark = benchmark or Benchmark()
    baseline = await run_plan_reuse(
        benchmark, use_memory=False,
        similarity_threshold=similarity_threshold, embedding_dim=embedding_dim,
        llm_client=llm_client, embedding_client=embedding_client,
        use_role_skeleton=use_role_skeleton,
    )
    reuse = await run_plan_reuse(
        benchmark, use_memory=True,
        similarity_threshold=similarity_threshold, embedding_dim=embedding_dim,
        llm_client=llm_client, embedding_client=embedding_client,
        use_role_skeleton=use_role_skeleton,
    )
    return Comparison(baseline=baseline, reuse=reuse)


__all__ = [
    "run_plan_reuse",
    "run_plan_reuse_comparison",
]
