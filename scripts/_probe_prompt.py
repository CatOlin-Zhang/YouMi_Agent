"""临时探针：确认 B 组的 system prompt / 工具清单 / 模型推理，定位 youmi 零调用的原因。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import zstandard

BASE = Path(os.environ['USERPROFILE']) / '.dsh' / 'sessions'


def pick_logs() -> list[Path]:
    if len(sys.argv) > 1:
        return [Path(sys.argv[1])]
    candidates = sorted(
        BASE.glob('*cold-B*/**/session.v4.jsonl.zstd'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    print(f'找到 {len(candidates)} 个 B 组 session log')
    return candidates[:1]


for log in pick_logs():
    with log.open('rb') as handle:
        raw = zstandard.ZstdDecompressor().stream_reader(
            handle, read_across_frames=True,
        ).read()
    text = raw.decode('utf-8', 'replace')

    print(f'== {log.name}  解压 {len(raw)} 字节')

    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        etype = event.get('type', '')
        data = event.get('data') or {}

        # 1) system prompt：引导语到底在不在、有多显眼
        if etype == 'system/message':
            message = data.get('message') or {}
            body = ''.join(
                block.get('text', '') for block in message.get('content', [])
                if isinstance(block, dict)
            )
            marker = '## YouMi 能力库'
            position = body.find(marker)
            print(f'\n[system prompt] 长度={len(body)}  引导语起始位置={position}')
            if position >= 0:
                print('  引导语原文：')
                for text_line in body[position:position + 1000].splitlines():
                    print(f'    {text_line}')
            continue

        # 2) 工具清单：youmi 的 10 个工具注册上了没有
        if etype == 'request/header':
            header = data.get('header') or {}
            tools = header.get('tools') or []
            names = [t.get('name') for t in tools if isinstance(t, dict)]
            hit = [n for n in names if 'youmi' in str(n)]
            print(f'\n[request/header] 工具总数={len(names)}  youmi={len(hit)}')
            if hit:
                print(f'   {hit}')
            continue

        # 3) 模型推理：有没有考虑过用 youmi
        if etype == 'assistant/message':
            message = data.get('message') or {}
            for block in message.get('content', []):
                if not isinstance(block, dict):
                    continue
                kind = block.get('type')
                if kind == 'reasoning':
                    print(f'\n[思考] {str(block.get("text", ""))[:600]}')
                elif kind == 'tool-call':
                    print(f'[调用] {block.get("toolName") or block.get("name")}')
            continue

        # 4) 工具结果：确认没有 youmi 调用
        if etype == 'tool/call':
            name = data.get('name')
            args = str(data.get('arguments', ''))[:160]
            print(f'[tool/call] {name}  {args}')
