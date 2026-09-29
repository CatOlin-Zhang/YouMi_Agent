"""
报告渲染 (report)

将 Comparison / ExperienceResult 渲染为可读的文本 + Markdown 报告。
"""

from __future__ import annotations

from experiments.metrics import Comparison, ExperienceResult


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _sav(x: float) -> str:
    """带符号的节省比例（正 = 节省，负 = 额外开销）。"""
    sign = "+" if x >= 0 else "-"
    return f"{sign}{abs(x) * 100:.1f}%"


def render_comparison(cmp: Comparison) -> str:
    b = cmp.baseline
    r = cmp.reuse
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("实验一：计划复用 (Plan-then-Execute + PlanMemory)")
    lines.append("=" * 64)
    lines.append(f"{'指标':<22}{'基线(无记忆)':>16}{'复用(有记忆)':>16}")
    lines.append("-" * 64)
    rows = [
        ("LLM 调用次数", b.llm_calls, r.llm_calls),
        ("  其中全量规划", b.full_gen_calls, r.full_gen_calls),
        ("  其中模板适配", b.adapt_calls, r.adapt_calls),
        ("规划成本(价格加权)", round(b.total_cost, 1), round(r.total_cost, 1)),
        ("  Token 总量(原始)", b.total_tokens, r.total_tokens),
        ("延迟(估, ms)", round(b.total_latency_ms, 1), round(r.total_latency_ms, 1)),
        ("缓存命中率", _pct(b.hit_rate), _pct(r.hit_rate)),
        ("复用精度(骨架正确)", _pct(b.reuse_precision), _pct(r.reuse_precision)),
        ("骨架保真率(适配忠实)", _pct(b.fidelity_rate), _pct(r.fidelity_rate)),
        ("复用召回(同族改写)", _pct(b.reuse_recall), _pct(r.reuse_recall)),
        ("Plan 合法率", _pct(b.plan_validity), _pct(r.plan_validity)),
        ("陌生任务正确拒绝率", _pct(b.novel_precision), _pct(r.novel_precision)),
    ]
    for name, bv, rv in rows:
        lines.append(f"{name:<22}{bv:>16}{rv:>16}")
    lines.append("-" * 64)
    lines.append("相对基线变化（正 = 节省）：")
    lines.append(f"  全量规划调用(昂贵模型) {_sav(cmp.full_gen_call_savings)}")
    lines.append(f"  规划成本(价格加权) {_sav(cmp.cost_savings)}")
    lines.append(f"  延迟 {_sav(cmp.latency_savings)}")
    lines.append(f"  LLM 总调用 {_sav(cmp.llm_call_savings)}")
    lines.append(f"  Token 总量(原始) {_sav(cmp.token_savings)}")
    return "\n".join(lines)


def render_experience(exp: ExperienceResult) -> str:
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("实验二：经验沉淀闭环 (GlobalMemory + FeedbackCollector)")
    lines.append("=" * 64)
    lines.append(f"  工具: {exp.tool_name}   负反馈阈值: {exp.negative_threshold}")
    lines.append(f"  触发修复所需任务数 (time-to-fix): {exp.tasks_to_fix}")
    lines.append(f"  修复前工具成功率: {_pct(exp.pre_fix_success_rate)}")
    lines.append(f"  修复后工具成功率: {_pct(exp.post_fix_success_rate)}")
    lines.append(f"  成功率提升: +{_pct(exp.success_delta)}")
    lines.append(f"  未解决经验: {exp.known_issues_before} -> {exp.known_issues_after}")
    lines.append(f"  已解决条目: {exp.resolved_entries}")
    lines.append(f"  沉淀最佳实践: {exp.best_practices}")
    lines.append("  学习曲线 (任务 → 成功/失败)：")
    curve = "".join(
        "✓" if x["success"] else "✗" for x in exp.learning_curve
    )
    lines.append(f"    [{curve}]")
    lines.append("    ↑ 前段为故障期，达到阈值后修复，后段全部成功")
    return "\n".join(lines)


def render_markdown(cmp: Comparison, exp: ExperienceResult) -> str:
    """生成 Markdown 版报告（可写盘）。"""
    b, r = cmp.baseline, cmp.reuse
    md: list[str] = []
    md.append("# YouMi Agent 创新点验证报告")
    md.append("")
    md.append("> 由 `experiments/run_all.py` 生成，使用确定性 mock LLM，可离线复现。")
    md.append("")
    md.append("## 实验一：计划复用 (Plan-then-Execute + PlanMemory)")
    md.append("")
    md.append("| 指标 | 基线(无记忆) | 复用(有记忆) |")
    md.append("|------|------|------|")
    md.append(f"| LLM 调用次数 | {b.llm_calls} | {r.llm_calls} |")
    md.append(f"| 规划成本(价格加权) | {round(b.total_cost, 1)} | {round(r.total_cost, 1)} |")
    md.append(f"| Token 总量(原始) | {b.total_tokens} | {r.total_tokens} |")
    md.append(f"| 延迟(估, ms) | {round(b.total_latency_ms, 1)} | {round(r.total_latency_ms, 1)} |")
    md.append(f"| 缓存命中率 | {_pct(b.hit_rate)} | {_pct(r.hit_rate)} |")
    md.append(f"| 复用精度(骨架正确) | {_pct(b.reuse_precision)} | {_pct(r.reuse_precision)} |")
    md.append(f"| 骨架保真率(适配忠实) | {_pct(b.fidelity_rate)} | {_pct(r.fidelity_rate)} |")
    md.append(f"| 复用召回(同族改写) | {_pct(b.reuse_recall)} | {_pct(r.reuse_recall)} |")
    md.append(f"| Plan 合法率 | {_pct(b.plan_validity)} | {_pct(r.plan_validity)} |")
    md.append("")
    md.append("**相对基线变化（正 = 节省）：**")
    md.append("")
    md.append(f"- 全量规划调用(昂贵模型) {_sav(cmp.full_gen_call_savings)}")
    md.append(f"- 规划成本(价格加权) {_sav(cmp.cost_savings)}")
    md.append(f"- 延迟 {_sav(cmp.latency_savings)}")
    md.append(f"- LLM 总调用 {_sav(cmp.llm_call_savings)}")
    md.append(f"- Token 总量(原始) {_sav(cmp.token_savings)}")
    md.append("")
    md.append("## 实验二：经验沉淀闭环")
    md.append("")
    md.append("| 指标 | 值 |")
    md.append("|------|------|")
    md.append(f"| 触发修复任务数 | {exp.tasks_to_fix} |")
    md.append(f"| 修复前成功率 | {_pct(exp.pre_fix_success_rate)} |")
    md.append(f"| 修复后成功率 | {_pct(exp.post_fix_success_rate)} |")
    md.append(f"| 成功率提升 | +{_pct(exp.success_delta)} |")
    md.append(f"| 未解决经验 | {exp.known_issues_before} → {exp.known_issues_after} |")
    md.append(f"| 已解决条目 | {exp.resolved_entries} |")
    return "\n".join(md)


__all__ = [
    "render_comparison",
    "render_experience",
    "render_markdown",
]
