"""
Eval CLI — ``python -m youmi.eval``

用法::

    # 运行内置数据集
    python -m youmi.eval

    # 运行自定义数据集（JSON / YAML）
    python -m youmi.eval --dataset my_eval.yaml

    # 只跑部分任务，输出 JSON 报告
    python -m youmi.eval --tasks weather_beijing,calc_expression --report report.json

    # 查看数据集任务清单
    python -m youmi.eval --list

退出码：达标（默认完成率与工具准确率均为 100%）→ 0，否则 1，
可通过 ``--min-completion`` / ``--min-tool-accuracy`` 放宽阈值。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from youmi.eval.dataset import EvalDataset, builtin_dataset
from youmi.eval.runner import EvalRunner
from youmi.eval.scorer import score_task, summarize


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m youmi.eval",
        description="YouMi Eval 基准 — 确定性 LLM 依赖路径评测",
    )
    parser.add_argument(
        "--dataset", default="builtin",
        help="数据集：'builtin' 或 JSON/YAML 文件路径（默认 builtin）",
    )
    parser.add_argument(
        "--tasks", default="",
        help="逗号分隔的任务 ID 子集（默认全部）",
    )
    parser.add_argument(
        "--report", default="",
        help="输出 JSON 报告的文件路径（可选）",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="仅列出数据集任务后退出",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="输出每个任务的详细轨迹",
    )
    parser.add_argument(
        "--min-completion", type=float, default=1.0,
        help="完成率阈值（低于则退出码 1，默认 1.0）",
    )
    parser.add_argument(
        "--min-tool-accuracy", type=float, default=1.0,
        help="工具准确率阈值（低于则退出码 1，默认 1.0）",
    )
    return parser


def _load_dataset(spec: str) -> EvalDataset:
    if spec == "builtin":
        return builtin_dataset()
    return EvalDataset.load(spec)


async def _run(args: argparse.Namespace) -> int:
    dataset = _load_dataset(args.dataset)

    if args.list:
        print(f"数据集: {dataset.name} v{dataset.version}（{len(dataset.tasks)} 个任务）")
        for t in dataset.tasks:
            print(f"  - {t.task_id}  tools={t.expected_tools or '[]'}  {t.task}")
        return 0

    task_ids = [s.strip() for s in args.tasks.split(",") if s.strip()] or None
    if task_ids:
        unknown = [tid for tid in task_ids if dataset.get(tid) is None]
        if unknown:
            print(f"错误: 未知任务 ID: {', '.join(unknown)}", file=sys.stderr)
            return 2

    runner = EvalRunner(dataset)
    runs = await runner.run(task_ids)
    selected = dataset.select(task_ids)
    scores = [
        score_task(run, task)
        for run, task in zip(runs, selected.tasks)
    ]
    summary = summarize(selected, scores)

    if args.verbose:
        for run in runs:
            print(f"--- {run.task_id} ---")
            print(f"  status={run.status} iterations={run.iterations} "
                  f"llm_calls={run.llm_calls}")
            print(f"  tools={run.actual_tools}")
            for i, (name, res) in enumerate(zip(run.actual_tools, run.tool_results)):
                print(f"    [{i}] {name} → {res[:120]}")
            print(f"  output={run.output[:200]}")
        print()

    print(summary.format_text())

    if args.report:
        report_path = Path(args.report)
        report_path.write_text(
            json.dumps(summary.as_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON 报告已写入: {report_path}")

    ok = summary.passed(
        min_completion=args.min_completion,
        min_tool_accuracy=args.min_tool_accuracy,
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台兼容（避免非 UTF-8 代码页下中文输出报错）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
