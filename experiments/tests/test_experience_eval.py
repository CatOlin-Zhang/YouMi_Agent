"""
经验沉淀闭环实验测试 (test_experience_eval)

验证闭环机制：
- 负反馈累积到阈值触发修复（time-to-fix）
- 修复前后成功率形成学习曲线（提升）
- 未解决经验在修复后清零、沉淀最佳实践
"""

from __future__ import annotations

from experiments.experience_eval import run_experience_loop


async def test_fix_triggered_at_threshold():
    exp = await run_experience_loop(n_tasks=8, negative_threshold=3)
    assert exp.tasks_to_fix == 3  # 第 3 个失败任务达到阈值触发修复


async def test_learning_curve_improves_success():
    exp = await run_experience_loop(n_tasks=8, negative_threshold=3)
    assert exp.pre_fix_success_rate == 0.0
    assert exp.post_fix_success_rate == 1.0
    assert exp.success_delta == 1.0


async def test_known_issues_resolved_after_fix():
    exp = await run_experience_loop(n_tasks=8, negative_threshold=3)
    assert exp.known_issues_before >= exp.negative_threshold
    assert exp.known_issues_after == 0
    assert exp.resolved_entries >= exp.negative_threshold


async def test_no_fix_if_tasks_insufficient():
    """任务数不足阈值时不应触发修复，也无 post 阶段。"""
    exp = await run_experience_loop(n_tasks=2, negative_threshold=3)
    assert exp.tasks_to_fix == 0
    assert exp.post_fix_success_rate == 1.0  # 无 post 样本时默认 1.0
    # 学习曲线全为 pre
    assert all(x["phase"] == "pre" for x in exp.learning_curve)


async def test_learning_curve_shape():
    exp = await run_experience_loop(n_tasks=8, negative_threshold=3)
    curve = exp.learning_curve
    assert len(curve) == 8
    # 前 3 个失败，后 5 个成功
    assert [x["success"] for x in curve] == [
        False, False, False, True, True, True, True, True,
    ]
