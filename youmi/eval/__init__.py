"""
Eval 基准模块 (youmi.eval)

对「LLM 依赖路径」做确定性集成评测：

- 数据集：``EvalDataset`` / ``EvalTask`` / ``EvalStep`` + 内置数据集
- 执行器：``EvalRunner``（真实 LLMClient → MockLLMServer → 真实 Agent ReAct 循环）
- 评分器：完成率 / 工具准确率 / 成本（``score_task`` / ``summarize``）
- CLI：``python -m youmi.eval``

快速使用::

    from youmi.eval import EvalRunner, builtin_dataset, score_task, summarize

    dataset = builtin_dataset()
    runner = EvalRunner(dataset)
    runs = await runner.run()
    summary = summarize(dataset, [score_task(r, t) for r, t in zip(runs, dataset.tasks)])
    print(summary.format_text())
"""

from youmi.eval.dataset import EvalDataset, EvalStep, EvalTask, builtin_dataset
from youmi.eval.runner import DEFAULT_SYSTEM_PROMPT, EvalAgent, EvalRunner, TaskRun
from youmi.eval.scorer import EvalSummary, TaskScore, score_task, summarize
from youmi.eval.tools import build_default_tools

__all__ = [
    # 数据集
    "EvalDataset",
    "EvalStep",
    "EvalTask",
    "builtin_dataset",
    # 执行器
    "EvalRunner",
    "EvalAgent",
    "TaskRun",
    "DEFAULT_SYSTEM_PROMPT",
    # 评分
    "TaskScore",
    "EvalSummary",
    "score_task",
    "summarize",
    # 工具
    "build_default_tools",
]
