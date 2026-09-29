"""
生成「YouMi Agent × 前沿论文对比」HTML 报告 (run_paper_comparison)

用法::

    python -m experiments.run_paper_comparison --html experiments/comparison.html

生成的 HTML 自包含（无外链 / CDN），本地双击即可查看。
"""

from __future__ import annotations

import argparse

from experiments.render_html import write_html


def main() -> None:
    parser = argparse.ArgumentParser(description="生成论文对比 HTML 报告")
    parser.add_argument(
        "--html", metavar="PATH", default="experiments/comparison.html",
        help="输出 HTML 路径（默认 experiments/comparison.html）",
    )
    args = parser.parse_args()
    write_html(args.html)
    print(f"对比报告已生成: {args.html}")


if __name__ == "__main__":
    main()
