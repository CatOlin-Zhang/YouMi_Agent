"""
基准构造测试 (test_benchmark)

验证 Benchmark 提供的真值标签与任务流正确：
- 每个族包含 seed + variants + gold plan
- plans_by_task / family_of 真值映射正确
- 骨架等价性判定（same_skeleton）正确
"""

from __future__ import annotations

from youmi.coordinator.plan import WorkflowPlan, WorkflowStep

from experiments.benchmark import (
    Benchmark,
    same_skeleton,
    same_role_skeleton,
    role_skeleton_key,
    skeleton_key,
)


def _plan(steps: list[dict]) -> WorkflowPlan:
    return WorkflowPlan(name="p", steps=[WorkflowStep(**s) for s in steps])


def test_families_structure():
    b = Benchmark()
    assert len(b.families) >= 3
    for fam in b.families:
        assert fam.seed
        assert len(fam.variants) >= 2
        assert not fam.gold_plan.validate()  # gold plan 必须合法


def test_plans_by_task_covers_all():
    b = Benchmark()
    for task in b.all_tasks():
        assert b.gold_plan_for(task) is not None, f"缺少 gold plan: {task}"


def test_family_of_mapping():
    b = Benchmark()
    # seed 与 variants 同族
    fam = b.families[0]
    assert b.family_of(fam.seed) == fam.family_id
    for v in fam.variants:
        assert b.family_of(v) == fam.family_id
    # novel 任务不属于任何族
    for n in b.novel:
        assert b.family_of(n.task) is None


def test_task_stream_partition():
    b = Benchmark()
    warmup = b.warmup_tasks()
    test = b.test_tasks()
    assert set(warmup).isdisjoint(set(test))
    assert len(warmup) == len(b.families)
    assert len(test) == len(b.variant_tasks()) + len(b.novel_tasks())


def test_same_skeleton_ignores_task_text():
    """骨架等价性只比较角色+依赖，忽略 task 文本。"""
    a = _plan([
        {"step_id": "s1", "role": "r", "task": "任务A", "depends_on": []},
        {"step_id": "s2", "role": "w", "task": "任务B", "depends_on": ["s1"]},
    ])
    b = _plan([
        {"step_id": "s1", "role": "r", "task": "完全不同的任务文本", "depends_on": []},
        {"step_id": "s2", "role": "w", "task": "另一段文本", "depends_on": ["s1"]},
    ])
    assert skeleton_key(a) == skeleton_key(b)
    assert same_skeleton(a, b)


def test_same_skeleton_detects_different_topology():
    a = _plan([
        {"step_id": "s1", "role": "r", "task": "t", "depends_on": []},
        {"step_id": "s2", "role": "w", "task": "t", "depends_on": ["s1"]},
    ])
    c = _plan([
        {"step_id": "s1", "role": "r", "task": "t", "depends_on": []},
        {"step_id": "s2", "role": "w", "task": "t", "depends_on": []},  # 无依赖
    ])
    assert not same_skeleton(a, c)


def test_role_skeleton_ignores_step_id_naming():
    """角色级骨架忽略 step_id 命名差异，只看角色 + 依赖拓扑。

    对应真实 LLM 场景：7b 把 role 名直接当 step_id（researcher），而手写 gold 用
    语义化 id（research），两者角色链一致、应判等价。
    """
    gold = _plan([
        {"step_id": "research", "role": "researcher", "task": "t", "depends_on": []},
        {"step_id": "analyze", "role": "analyst", "task": "t", "depends_on": ["research"]},
    ])
    llm = _plan([
        {"step_id": "researcher", "role": "researcher", "task": "t2", "depends_on": []},
        {"step_id": "analyst", "role": "analyst", "task": "t2", "depends_on": ["researcher"]},
    ])
    # 严格骨架（含 step_id）判不同
    assert not same_skeleton(gold, llm)
    # 角色级骨架判等价
    assert same_role_skeleton(gold, llm)
    assert role_skeleton_key(gold) == role_skeleton_key(llm)


def test_role_skeleton_detects_topology_diff():
    a = _plan([
        {"step_id": "x1", "role": "r", "task": "t", "depends_on": []},
        {"step_id": "x2", "role": "w", "task": "t", "depends_on": ["x1"]},
    ])
    b = _plan([
        {"step_id": "y1", "role": "r", "task": "t", "depends_on": []},
        {"step_id": "y2", "role": "w", "task": "t", "depends_on": []},
    ])
    assert not same_role_skeleton(a, b)


def test_benchmark_subset():
    """n_families 子集（真实模型小规模跑通用）。"""
    full = Benchmark()
    sub = Benchmark(n_families=2)
    assert len(sub.families) == 2
    assert len(sub.warmup_tasks()) == 2
    # novel 保留（仍能验证陌生任务拒绝）
    assert len(sub.novel) == len(full.novel)
    # 子集族是全量族的前缀
    assert [f.family_id for f in sub.families] == [
        f.family_id for f in full.families[:2]
    ]
