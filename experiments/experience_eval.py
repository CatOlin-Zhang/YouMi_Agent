"""
经验沉淀闭环实验 (experience_eval)

验证创新点 **GlobalMemory + FeedbackCollector + ToolGuardian** 的闭环机制：

「工具调用失败 → 沉淀 TOOL_EXPERIENCE / 人工负反馈 → 负反馈累计到阈值
 → ToolGuardian 触发诊断修复 → mark_resolved 标记解决 + 沉淀 BUG_FIX
 → 后续任务工具成功率上升」

本实验在**机制层**用确定性模拟驱动真实组件（GlobalMemory / FeedbackCollector），
产出学习曲线（修复前后成功率），不依赖真实 LLM。

指标：
- tasks_to_fix：触发修复所需任务数（时间到修复）
- pre/post_fix_success_rate：修复前后工具成功率（学习曲线）
- known_issues before/after：未解决经验数量变化
- resolved_entries / best_practices：修复闭环与知识沉淀规模
"""

from __future__ import annotations

from youmi.knowledge.global_memory import GlobalMemory
from youmi.knowledge.feedback import FeedbackCollector
from youmi.knowledge.models import KnowledgeCategory

from experiments.metrics import ExperienceResult


async def run_experience_loop(
    tool_name: str = "unit_convert",
    n_tasks: int = 8,
    negative_threshold: int = 3,
) -> ExperienceResult:
    """运行一轮经验沉淀闭环实验。

    前若干任务工具处于「故障」状态（确定性失败），累积负反馈；
    达到阈值后触发 ToolGuardian 修复（mark_resolved + BUG_FIX），
    后续任务转为成功，形成清晰的学习曲线。
    """
    gm = GlobalMemory(db_path=":memory:")
    await gm.initialize()
    collector = FeedbackCollector(gm, negative_threshold=negative_threshold)

    res = ExperienceResult(tool_name=tool_name, negative_threshold=negative_threshold)
    buggy = True
    fix_triggered = False

    for i in range(1, n_tasks + 1):
        success = not buggy  # buggy 状态 → 失败；修复后 → 成功
        task_id = f"task_{i:02d}"

        if success:
            await gm.add_experience(
                tool_name=tool_name,
                content=f"{task_id} 换算结果正确",
                category=KnowledgeCategory.TOOL_EXPERIENCE,
                source_task_id=task_id,
                success_rate=1.0,
            )
            await collector.record_feedback(
                tool_name, "换算结果正确", rating="positive", task_id=task_id,
            )
        else:
            await gm.add_experience(
                tool_name=tool_name,
                content=f"{task_id} 换算系数错误，结果偏差",
                category=KnowledgeCategory.TOOL_EXPERIENCE,
                source_task_id=task_id,
                success_rate=0.0,
            )
            await collector.record_feedback(
                tool_name, "换算系数错误，结果完全错误", rating="negative", task_id=task_id,
            )

        res.learning_curve.append({
            "task": i,
            "success": success,
            "phase": "post" if not buggy else "pre",
        })

        # 阈值检查（在下一任务前触发修复）
        if buggy:
            over = await collector.tools_over_threshold()
            if over:
                res.tasks_to_fix = i
                res.known_issues_before = len(
                    (await gm.get_tool_knowledge(tool_name)).known_issues
                )
                await _apply_tool_guardian_fix(gm, tool_name, res)
                buggy = False
                fix_triggered = True

    # 修复后统计
    know = await gm.get_tool_knowledge(tool_name)
    res.known_issues_after = len(know.known_issues)
    res.best_practices = len(know.best_practices)

    pre = [x for x in res.learning_curve if x["phase"] == "pre"]
    post = [x for x in res.learning_curve if x["phase"] == "post"]
    res.pre_fix_success_rate = (
        sum(x["success"] for x in pre) / len(pre) if pre else 0.0
    )
    res.post_fix_success_rate = (
        sum(x["success"] for x in post) / len(post) if post else 1.0
    )

    if not fix_triggered:
        # 任务太少未触发阈值时，仍给出有意义的 before/after
        res.known_issues_before = res.known_issues_after

    await gm.close()
    return res


async def _apply_tool_guardian_fix(
    gm: GlobalMemory,
    tool_name: str,
    res: ExperienceResult,
) -> None:
    """模拟 ToolGuardian 修复动作：标记负反馈 + 失败经验为已解决，沉淀 BUG_FIX。"""
    resolution = "修正换算系数，改用正确公式（F = 1.8*C + 32）"

    # 1. 未解决的负反馈 → resolved
    feedbacks = await gm.list_entries(
        tool_name=tool_name, category=KnowledgeCategory.HUMAN_FEEDBACK,
    )
    for e in feedbacks:
        if (e.metadata or {}).get("rating") == "negative" and not e.resolved:
            await gm.mark_resolved(e.entry_id, resolution)
            res.resolved_entries += 1

    # 2. 未解决的低成功率经验 → resolved
    exp_entries = await gm.list_entries(
        tool_name=tool_name,
        category=KnowledgeCategory.TOOL_EXPERIENCE,
        unresolved_only=True,
    )
    for e in exp_entries:
        if e.success_rate < 0.8:
            await gm.mark_resolved(e.entry_id, resolution)

    # 3. 沉淀一条 BUG_FIX 修复记录（进 fix_history）
    await gm.add_experience(
        tool_name=tool_name,
        content="unit_convert 必须使用 F = 1.8*C + 32，禁止使用近似系数 1.5",
        category=KnowledgeCategory.BUG_FIX,
    )


__all__ = ["run_experience_loop"]
