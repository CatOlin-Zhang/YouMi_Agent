"""临时探针：确认最近一次 headless 运行实际请求的模型与推理档位。"""
import json
import os
import sys
from pathlib import Path

import zstandard

sys.stdout.reconfigure(encoding='utf-8')
BASE = Path(os.environ['USERPROFILE']) / '.dsh' / 'sessions'
logs = sorted(BASE.glob('*/**/session.v4.jsonl.zstd'), key=lambda p: p.stat().st_mtime, reverse=True)
print(f'找到 {len(logs)} 个 session log，取最新 {logs[0].parent.name if logs else "无"}')

with logs[0].open('rb') as handle:
    raw = zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True).read()

for line in raw.decode('utf-8', 'replace').splitlines():
    if not line.strip():
        continue
    try:
        event = json.loads(line)
    except Exception:
        continue
    if event.get('type') != 'request/header':
        continue
    header = (event.get('data') or {}).get('header') or {}
    tools = header.get('tools') or []
    shown = {key: value for key, value in header.items() if key != 'tools'}
    print(f'工具数={len(tools)}')
    print(json.dumps(shown, ensure_ascii=False, indent=2)[:1500])
    break
