"""临时探针：按任务 dump 逐条运行记录，定位均值背后的极端样本。"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
rows = [json.loads(line) for line in (Path(__file__).parent.parent / '.ab' / 'results-tools.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]

want = sys.argv[1:] or None
for row in rows:
    if want and row['taskId'] not in want:
        continue
    calls = [str(name).replace('mcp__tools__', '') for name in row['toolCalls'] if 'youmi_tool_' not in str(name)]
    print(
        f"{row['taskId']:<13}{row['group']} r{row['rep']} ok={int(row['success'])} "
        f"步={row['steps']:>2} in={row['inputTokens']:>6} cache={row['cacheReadTokens']:>7} "
        f"out={row['outputTokens']:>5} 加权={row['weightedTokens']:>6} {row['wallMs'] / 1000:>5.1f}s "
        f"检索={row['searchCalls']}"
    )
    print(f"   调用: {calls}")
