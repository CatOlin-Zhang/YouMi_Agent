"""
Eval 评分器 (eval.scorer)

将 ``TaskRun`` 轨迹转换为可断言的评分结果：

- **完成率**：任务是否在迭代预算内产出符合预期的最终回复
  （Agent 完成 + 非迭代耗尽 + ``expect_final_contains`` 命中）
- **工具准确率**：实际工具调用与 ``expected_tools`` 的匹配
  （``tool_order_strict`` 时按序列，否则按去重集合；同时给出漏调/多调明细）
- **成本**：token 用量 / LLM 调用次数 / 工具调用次数 / 耗时
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from youmi.eval.dataset import EvalDataset, EvalTask
from youmi.eval.runner import TaskRun

_MAX_ITER_HINT = "达到最大迭代次数"


# ---------------------------------------------------------------------------
# 单任务评分
# ---------------------------------------------------------------------------

@dataclass
class TaskScore:
    """单个任务的评分结果"""

    task_id: str
    completed: bool
    tool_match: bool

    expected_tools: list[str] = field(default_factory=list)
    actual_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    extra_tools: list[str] = field(default_factory=list)

    final_answer: str = ""
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0

    script_exhausted: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "completed": self.completed,
            "tool_match": self.tool_match,
            "expected_tools": self.expected_tools,
            "actual_tools": self.actual_tools,
            "missing_tools": self.missing_tools,
            "extra_tools": self.extra_tools,
            "final_answer": self.final_answer,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "duration_ms": round(self.duration_ms, 2),
            "script_exhausted": self.script_exhausted,
            "error": self.error,
        }


def score_task(run: TaskRun, task: EvalTask) -> TaskScore:
    """对照任务期望，对执行轨迹评分"""
    completed = _is_completed(run, task)
    tool_match, missing, extra = _match_tools(run.actual_tools, task)

    return TaskScore(
        task_id=task.task_id,
        completed=completed,
        tool_match=tool_match,
        expected_tools=list(task.expected_tools),
        actual_tools=list(run.actual_tools),
        missing_tools=missing,
        extra_tools=extra,
        final_answer=run.output,
        llm_calls=run.llm_calls,
        tool_calls=len(run.actual_tools),
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        total_tokens=run.total_tokens,
        duration_ms=run.duration_ms,
        script_exhausted=run.script_exhausted,
        error=run.error,
    )


def _is_completed(run: TaskRun, task: EvalTask) -> bool:
    """完成判定：Agent 正常结束 + 有最终回复 + 预期内容命中"""
    if run.error:
        return False
    if run.status != "completed":
        return False
    if not run.output or run.output.startswith(_MAX_ITER_HINT):
        return False
    if task.expect_final_contains and task.expect_final_contains not in run.output:
        return False
    return True


def _match_tools(
    actual: list[str],
    task: EvalTask,
) -> tuple[bool, list[str], list[str]]:
    """工具匹配判定，返回 (match, missing, extra)"""
    expected = task.expected_tools
    if task.tool_order_strict:
        match = actual == expected
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        return match, missing, extra

    actual_set, expected_set = set(actual), set(expected)
    match = actual_set == expected_set
    missing = sorted(expected_set - actual_set)
    extra = sorted(actual_set - expected_set)
    return match, missing, extra


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

@dataclass
class EvalSummary:
    """数据集级评分汇总"""

    dataset: str
    version: str = "1.0"
    total: int = 0
    completed: int = 0
    tool_matches: int = 0

    total_llm_calls: int = 0
    total_tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: float = 0.0

    tasks: list[TaskScore] = field(default_factory=list)

    # ------- 派生指标 -------

    @property
    def completion_rate(self) -> float:
        return self.completed / self.total if self.total else 0.0

    @property
    def tool_accuracy(self) -> float:
        return self.tool_matches / self.total if self.total else 0.0

    @property
    def avg_tokens_per_task(self) -> float:
        return self.total_tokens / self.total if self.total else 0.0

    def passed(self, *, min_completion: float = 1.0, min_tool_accuracy: float = 1.0) -> bool:
        """是否达到阈值（CLI 退出码判定用）"""
        return (
            self.completion_rate >= min_completion
            and self.tool_accuracy >= min_tool_accuracy
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "version": self.version,
            "total": self.total,
            "completed": self.completed,
            "completion_rate": round(self.completion_rate, 4),
            "tool_matches": self.tool_matches,
            "tool_accuracy": round(self.tool_accuracy, 4),
            "total_llm_calls": self.total_llm_calls,
            "total_tool_calls": self.total_tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_tokens_per_task": round(self.avg_tokens_per_task, 1),
            "duration_ms": round(self.duration_ms, 2),
            "tasks": [t.as_dict() for t in self.tasks],
        }

    def format_text(self) -> str:
        """人类可读的多行报告（CLI 输出用）"""
        lines = [
            f"=== Eval: {self.dataset} v{self.version} ===",
            f"任务完成率: {self.completed}/{self.total} ({self.completion_rate:.1%})",
            f"工具准确率: {self.tool_matches}/{self.total} ({self.tool_accuracy:.1%})",
            (
                f"LLM 调用: {self.total_llm_calls} 次 | "
                f"工具调用: {self.total_tool_calls} 次"
            ),
            (
                f"Token: {self.total_tokens} "
                f"(prompt {self.prompt_tokens} / completion {self.completion_tokens}) | "
                f"平均 {self.avg_tokens_per_task:.0f}/任务"
            ),
            f"耗时: {self.duration_ms:.0f} ms",
            "",
            "各任务:",
        ]
        for t in self.tasks:
            flag = "PASS" if (t.completed and t.tool_match) else "FAIL"
            line = (
                f"  [{flag}] {t.task_id}  "
                f"tools={t.actual_tools or '[]'}  tokens={t.total_tokens}"
            )
            if not t.completed:
                reason = "未完成"
                if t.error:
                    reason = f"error={t.error[:80]}"
                elif t.final_answer.startswith(_MAX_ITER_HINT):
                    reason = "迭代耗尽"
                elif t.script_exhausted:
                    reason = "脚本耗尽（模型行为超预期）"
                line += f"  ← {reason}"
            elif not t.tool_match:
                line += f"  ← 工具不匹配(missing={t.missing_tools} extra={t.extra_tools})"
            lines.append(line)
        return "\n".join(lines)


def summarize(dataset: EvalDataset, scores: list[TaskScore]) -> EvalSummary:
    """把单任务评分汇总为数据集级报告"""
    summary = EvalSummary(
        dataset=dataset.name,
        version=dataset.version,
        total=len(scores),
        completed=sum(1 for s in scores if s.completed),
        tool_matches=sum(1 for s in scores if s.tool_match),
        total_llm_calls=sum(s.llm_calls for s in scores),
        total_tool_calls=sum(s.tool_calls for s in scores),
        prompt_tokens=sum(s.prompt_tokens for s in scores),
        completion_tokens=sum(s.completion_tokens for s in scores),
        total_tokens=sum(s.total_tokens for s in scores),
        duration_ms=sum(s.duration_ms for s in scores),
        tasks=list(scores),
    )
    return summary


__all__ = ["TaskScore", "EvalSummary", "score_task", "summarize"]
