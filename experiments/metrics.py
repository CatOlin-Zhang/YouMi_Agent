"""
评测指标数据结构 (metrics)

统一承载两类实验的结果与对比逻辑：
- PlanReuseResult / Comparison：计划复用消融
- ExperienceResult：经验沉淀闭环

所有指标均为确定性计算，便于断言与报告渲染。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 计划复用
# ---------------------------------------------------------------------------

@dataclass
class PlanReuseResult:
    """一次计划复用实验的结果（baseline 或 reuse 各自一份）。"""

    label: str = ""

    # 任务统计
    total_tasks: int = 0
    warmup_tasks: int = 0
    test_tasks: int = 0

    # 成本（整轮运行累计）
    full_gen_calls: int = 0
    adapt_calls: int = 0
    total_tokens: int = 0          # 原始 token 总量（输入+输出，未加权）
    total_cost: float = 0.0        # 价格加权成本（对齐「昂贵大模型 vs 廉价小模型」）
    total_latency_ms: float = 0.0

    # 命中与正确性（仅 test 阶段）
    cache_hits: int = 0
    cache_misses: int = 0
    valid_plans: int = 0
    invalid_plans: int = 0
    hit_correct_skeleton: int = 0          # 命中且骨架正确（mock 用 gold 判定）
    hit_fidelity: int = 0                  # 命中且骨架保真（适配后骨架 == 命中模板骨架，真实模型口径）
    variant_hits: int = 0                  # 同族改写任务中被命中的数量
    variant_total: int = 0                 # 同族改写任务总数
    novel_missed: int = 0                  # 陌生任务中正确 miss 的数量
    novel_total: int = 0                   # 陌生任务总数

    # ------- 派生指标 -------
    @property
    def llm_calls(self) -> int:
        return self.full_gen_calls + self.adapt_calls

    @property
    def hit_rate(self) -> float:
        denom = self.cache_hits + self.cache_misses
        return self.cache_hits / denom if denom else 0.0

    @property
    def reuse_precision(self) -> float:
        return self.hit_correct_skeleton / self.cache_hits if self.cache_hits else 1.0

    @property
    def fidelity_rate(self) -> float:
        """骨架保真率：命中且适配后骨架 == 模板骨架的比例（真实模型质量口径）。"""
        return self.hit_fidelity / self.cache_hits if self.cache_hits else 1.0

    @property
    def reuse_recall(self) -> float:
        return self.variant_hits / self.variant_total if self.variant_total else 0.0

    @property
    def plan_validity(self) -> float:
        denom = self.valid_plans + self.invalid_plans
        return self.valid_plans / denom if denom else 0.0

    @property
    def novel_precision(self) -> float:
        """陌生任务正确 miss 比例（越低 = 越容易误复用陌生任务）。"""
        return self.novel_missed / self.novel_total if self.novel_total else 1.0

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "total_tasks": self.total_tasks,
            "llm_calls": self.llm_calls,
            "full_gen_calls": self.full_gen_calls,
            "adapt_calls": self.adapt_calls,
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 1),
            "total_latency_ms": round(self.total_latency_ms, 1),
            "hit_rate": round(self.hit_rate, 4),
            "reuse_precision": round(self.reuse_precision, 4),
            "fidelity_rate": round(self.fidelity_rate, 4),
            "reuse_recall": round(self.reuse_recall, 4),
            "plan_validity": round(self.plan_validity, 4),
            "novel_precision": round(self.novel_precision, 4),
        }


@dataclass
class Comparison:
    """baseline vs reuse 的对比结果。"""

    baseline: PlanReuseResult
    reuse: PlanReuseResult

    @staticmethod
    def _savings(base: float, new: float) -> float:
        return (base - new) / base if base else 0.0

    @property
    def token_savings(self) -> float:
        return self._savings(self.baseline.total_tokens, self.reuse.total_tokens)

    @property
    def cost_savings(self) -> float:
        """价格加权成本的节省比例（对齐 APC 的省钱论证，正 = 节省）。"""
        return self._savings(self.baseline.total_cost, self.reuse.total_cost)

    @property
    def latency_savings(self) -> float:
        return self._savings(self.baseline.total_latency_ms, self.reuse.total_latency_ms)

    @property
    def llm_call_savings(self) -> float:
        return self._savings(self.baseline.llm_calls, self.reuse.llm_calls)

    @property
    def full_gen_call_savings(self) -> float:
        """全量规划（昂贵模型）调用次数的节省比例。

        注意：复用不会减少 LLM 总调用次数（full → adapt 只是换了更廉价的模型），
        真正下降的是「昂贵全量规划」的调用次数。
        """
        return self._savings(self.baseline.full_gen_calls, self.reuse.full_gen_calls)

    def as_dict(self) -> dict:
        return {
            "token_savings": round(self.token_savings, 4),
            "cost_savings": round(self.cost_savings, 4),
            "latency_savings": round(self.latency_savings, 4),
            "llm_call_savings": round(self.llm_call_savings, 4),
            "full_gen_call_savings": round(self.full_gen_call_savings, 4),
            "baseline": self.baseline.as_dict(),
            "reuse": self.reuse.as_dict(),
        }


# ---------------------------------------------------------------------------
# 经验沉淀闭环
# ---------------------------------------------------------------------------

@dataclass
class ExperienceResult:
    """经验沉淀闭环实验结果。"""

    tool_name: str = ""
    negative_threshold: int = 3

    tasks_to_fix: int = 0                 # 触发修复所需的负反馈任务数
    pre_fix_success_rate: float = 0.0     # 修复前工具成功率
    post_fix_success_rate: float = 0.0    # 修复后工具成功率
    known_issues_before: int = 0          # 修复前未解决问题数
    known_issues_after: int = 0           # 修复后未解决问题数
    resolved_entries: int = 0             # 被标记 resolved 的负反馈条目
    best_practices: int = 0               # 沉淀的最佳实践条数

    learning_curve: list[dict] = field(default_factory=list)
    # learning_curve 每项: {"task": int, "success": bool, "phase": "pre"/"post"}

    @property
    def success_delta(self) -> float:
        return self.post_fix_success_rate - self.pre_fix_success_rate

    def as_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "negative_threshold": self.negative_threshold,
            "tasks_to_fix": self.tasks_to_fix,
            "pre_fix_success_rate": round(self.pre_fix_success_rate, 4),
            "post_fix_success_rate": round(self.post_fix_success_rate, 4),
            "success_delta": round(self.success_delta, 4),
            "known_issues_before": self.known_issues_before,
            "known_issues_after": self.known_issues_after,
            "resolved_entries": self.resolved_entries,
            "best_practices": self.best_practices,
        }


__all__ = [
    "PlanReuseResult",
    "Comparison",
    "ExperienceResult",
]
