#!/usr/bin/env python
"""把 .ab/results-tools.jsonl 汇总成 A/B 对比报告。

驱动本身会打印汇总，但 PowerShell 的 `>` 重定向会把 UTF-8 中文写坏，
所以这里直接读 jsonl 重算，作为唯一的可信口径。

用法::

    python scripts/ab-tools-report.py
    python scripts/ab-tools-report.py --file .ab/results-tools.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def table(headers: list[str], rows: list[list[str]], aligns: str) -> str:
    widths = [len(head) for head in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def line(cells: list[str]) -> str:
        parts = []
        for cell, width, align in zip(cells, widths, aligns):
            parts.append(cell.rjust(width) if align == 'r' else cell.ljust(width))
        return '  '.join(parts)
    out = [line(headers), '  '.join('-' * width for width in widths)]
    out.extend(line(row) for row in rows)
    return '\n'.join(out)


def summarize(rows: list[dict]) -> str:
    headers = ['组', 'n', '命中', '命中率', '输入', '缓存读', '缓存率', '输出', '总tok', '加权',
               '步数', '耗时s', '首token', '检索', '注入']
    table_rows = []
    index = {}
    for group in sorted({row['group'] for row in rows}):
        subset = [row for row in rows if row['group'] == group]
        hit = sum(1 for row in subset if row['success'])
        tokens_in = sum(row['inputTokens'] for row in subset)
        cache = sum(row['cacheReadTokens'] for row in subset)
        index[group] = subset
        table_rows.append([
            group,
            str(len(subset)),
            str(hit),
            f'{hit / len(subset) * 100:.1f}%',
            str(round(sum(row['inputTokens'] for row in subset) / len(subset))),
            str(round(cache / len(subset))),
            f'{cache / max(tokens_in + cache, 1) * 100:.1f}%',
            str(round(sum(row['outputTokens'] for row in subset) / len(subset))),
            str(round(sum(row['totalTokens'] for row in subset) / len(subset))),
            str(round(sum(row['weightedTokens'] for row in subset) / len(subset))),
            f'{sum(row["steps"] for row in subset) / len(subset):.1f}',
            f'{sum(row["wallMs"] for row in subset) / len(subset) / 1000:.1f}',
            str(round(sum(row['ttftMs'] for row in subset) / len(subset))),
            f'{sum(row["searchCalls"] for row in subset) / len(subset):.1f}',
            f'{sum(row["listChanges"] for row in subset) / len(subset):.1f}',
        ])
    return table(headers, table_rows, 'lrrrrrrrrrrrrrr')


def robustness(rows: list[dict]) -> str:
    """均值会被缓存命中与否放大，中位数更能代表典型一次运行。"""
    headers = ['组', '加权(均值)', '加权(中位)', '输入(中位)', '缓存读(中位)', '耗时(中位)s']
    table_rows = []
    for group in sorted({row['group'] for row in rows}):
        subset = [row for row in rows if row['group'] == group]
        table_rows.append([
            group,
            str(round(sum(row['weightedTokens'] for row in subset) / len(subset))),
            str(round(median([row['weightedTokens'] for row in subset]))),
            str(round(median([row['inputTokens'] for row in subset]))),
            str(round(median([row['cacheReadTokens'] for row in subset]))),
            f'{median([row["wallMs"] for row in subset]) / 1000:.1f}',
        ])
    return table(headers, table_rows, 'lrrrrr')


def by_task(rows: list[dict]) -> str:
    grouped: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row['taskId']][row['group']].append(row)

    headers = ['任务', 'A命中', 'B命中', 'A加权', 'B加权', '加权差', 'A步', 'B步', 'A耗时', 'B耗时', 'B检索']
    table_rows = []
    for task_id, groups in grouped.items():
        a = groups.get('A', [])
        b = groups.get('B', [])
        def hit_rate(subset: list[dict]) -> str:
            return f'{sum(1 for row in subset if row["success"]) / len(subset) * 100:.0f}%' if subset else '—'
        def mean_weighted(subset: list[dict]) -> float:
            return sum(row['weightedTokens'] for row in subset) / len(subset) if subset else 0.0
        def mean_of(subset: list[dict], key: str) -> float:
            return sum(row[key] for row in subset) / len(subset) if subset else 0.0
        wa, wb = mean_weighted(a), mean_weighted(b)
        table_rows.append([
            task_id,
            hit_rate(a),
            hit_rate(b),
            str(round(wa)),
            str(round(wb)),
            f'{(wb - wa) / wa * 100:+.0f}%' if wa else '—',
            f'{mean_of(a, "steps"):.1f}',
            f'{mean_of(b, "steps"):.1f}',
            f'{mean_of(a, "wallMs") / 1000:.1f}',
            f'{mean_of(b, "wallMs") / 1000:.1f}',
            f'{mean_of(b, "searchCalls"):.1f}',
        ])
    return table(headers, table_rows, 'llrrrrrrrrr')


def cache_profile(rows: list[dict]) -> str:
    """按步序铺开未缓存输入 / 缓存读，看前缀复用在哪里断掉。"""
    headers = ['组']
    for step in range(1, 7):
        headers.extend([f'步{step}输入', f'步{step}缓存'])
    table_rows = []
    for group in sorted({row['group'] for row in rows}):
        subset = [row for row in rows if row['group'] == group]
        cells = [group]
        for step in range(6):
            inputs = [row['stepUsages'][step]['input'] for row in subset if len(row['stepUsages']) > step]
            caches = [row['stepUsages'][step]['cacheRead'] for row in subset if len(row['stepUsages']) > step]
            cells.append(str(round(median(inputs))) if inputs else '—')
            cells.append(str(round(median(caches))) if caches else '—')
        table_rows.append(cells)
    return table(headers, table_rows, 'l' + 'r' * 12)


def wrong_picks(rows: list[dict]) -> str:
    """错选集中在哪些工具上，直接反映「全量暴露」带来的选择噪声。"""
    lines = []
    for group in sorted({row['group'] for row in rows}):
        subset = [row for row in rows if row['group'] == group]
        counter = Counter()
        for row in subset:
            counter.update(row['wrongPicks'])
        runs = sum(1 for row in subset if row['wrongPicks'])
        summary = '、'.join(f'{name}×{count}' for name, count in counter.most_common(5)) or '无'
        lines.append(f'{group} 组：{runs}/{len(subset)} 次运行触发过错选  {summary}')
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', default=str(ROOT / '.ab' / 'results-tools.jsonl'))
    parser.add_argument('--out', default='', help='写入文件而不是打印；PowerShell 重定向会写坏中文')
    args = parser.parse_args()

    rows = load(Path(args.file))
    if not rows:
        print('没有数据')
        return 1

    sizes = sorted({row['catalogSize'] for row in rows})
    blocks = [
        f'样本 {len(rows)} 次，工具量 {"、".join(map(str, sizes))}',
        '== 汇总 ==',
        summarize(rows),
        '\n== 稳健性（中位数）==',
        robustness(rows),
        '\n== 逐任务 ==',
        by_task(rows),
        '\n== 缓存剖面（按步序中位数）==',
        cache_profile(rows),
        '\n== 错选分布 ==',
        wrong_picks(rows),
    ]
    text = '\n'.join(blocks)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding='utf-8')
        print(f'已写入 {args.out}')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
