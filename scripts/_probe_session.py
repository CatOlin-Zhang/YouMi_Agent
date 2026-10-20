# 临时探针：DSH session log 是「多帧 zstd + 每帧一行 JSON」，Python 的 stream_reader 能跨帧读。
import json
import sys

import zstandard

sys.stdout.reconfigure(encoding='utf-8')

with open(sys.argv[1], 'rb') as handle:
    raw = zstandard.ZstdDecompressor().stream_reader(handle, read_across_frames=True).read()

lines = [line for line in raw.decode('utf-8').split('\n') if line.strip()]
types = {}
samples = {}
for line in lines:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    types[event.get('type')] = types.get(event.get('type'), 0) + 1
    samples.setdefault(event.get('type'), line)

print(f'== lines: {len(lines)}')
print('== types:', json.dumps(types, indent=2, ensure_ascii=False))
print('\n== one sample per type ==')
for type_name, line in samples.items():
    print(f'\n--- {type_name} ---\n{line[:700]}')

print('\n== lines containing usage/token ==')
hits = [line for line in lines if 'usage' in line.lower() or 'prompttoken' in line.lower()]
print(f'hit count: {len(hits)}')
for line in hits[:3]:
    print(f'\n{line[:900]}')
