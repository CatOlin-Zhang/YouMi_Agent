"""
一键运行全部实验并输出报告 (run_all)

用法::

    python -m experiments.run_all            # 控制台报告
    python -m experiments.run_all --md report.md   # 同时写 Markdown 报告

或::

    python experiments/run_all.py
"""

from __future__ import annotations

import argparse
import asyncio

from experiments.benchmark import Benchmark
from experiments.plan_reuse_eval import run_plan_reuse_comparison
from experiments.experience_eval import run_experience_loop
from experiments.report import (
    render_comparison,
    render_experience,
    render_markdown,
)


async def _main(md_path: str | None) -> None:
    benchmark = Benchmark()
    print(f"基准规模: {benchmark.summary()}")

    cmp = await run_plan_reuse_comparison(benchmark)
    exp = await run_experience_loop()

    print()
    print(render_comparison(cmp))
    print()
    print(render_experience(exp))
    print()

    if md_path:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(render_markdown(cmp, exp))
        print(f"Markdown 报告已写入: {md_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="YouMi Agent 创新点验证实验")
    parser.add_argument(
        "--md", metavar="PATH", default=None,
        help="额外输出 Markdown 报告到指定路径",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.md))


if __name__ == "__main__":
    main()
