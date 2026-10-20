#!/usr/bin/env python
"""回检 lazy 模式的召回：每个任务的正确工具必须落在 search 返回的 top_k 里。

B 组能不能成功，前提是检索把正确工具递到模型眼前。如果这里不通过，
后面 48 轮跑出来的 B 组失败率反映的是任务集设计问题，不是架构差异。

用法::

    python scripts/ab/check_recall.py
    python scripts/ab/check_recall.py --top-k 3 --catalog-size 120
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mock_tool_server import MockServer, build_catalog  # noqa: E402

TASKS_FILE = Path(__file__).parent / 'tool-tasks.mjs'


def load_tasks() -> list[dict[str, str]]:
    """从 JS 任务文件里抠出 id / prompt / expect。

    只解析顶层的三行字段，不引入 JS 运行时；格式受 tool-tasks.mjs 的写法约束。
    """
    text = TASKS_FILE.read_text(encoding='utf-8')
    tasks: list[dict[str, str]] = []
    for block in re.split(r'\n  \{\n', text)[1:]:
        found = {}
        for key in ('id', 'prompt', 'expect'):
            match = re.search(rf"{key}: '([^']*)'", block)
            if match:
                found[key] = match.group(1)
        if len(found) == 3:
            tasks.append(found)
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--top-k', type=int, default=3)
    parser.add_argument('--catalog-size', type=int, default=120)
    args = parser.parse_args()

    server = MockServer(build_catalog(args.catalog_size), eager=False)
    tasks = load_tasks()
    print(f'目录 {len(server.catalog)} 个工具，{len(tasks)} 个任务，top_k={args.top_k}\n')

    failures = 0
    for task in tasks:
        hits = server._search(task['prompt'], args.top_k)
        names = [tool['name'] for tool in hits]
        rank = names.index(task['expect']) + 1 if task['expect'] in names else 0
        flag = f'#{rank}' if rank else 'MISS'
        if not rank:
            failures += 1
        print(f"{task['id']:<14} expect={task['expect']:<24} {flag:<6} {names}")

    print(f'\n召回 {len(tasks) - failures}/{len(tasks)}')
    if failures:
        print('有任务未进 top_k，需要调整任务措辞或检索打分，否则 B 组的失败不可归因于架构。')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
