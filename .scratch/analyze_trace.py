import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('workspace/projects/20261003-210530-76b6/trace.jsonl', 'r', encoding='utf-8') as f:
    lines = [json.loads(line) for line in f if line.strip()]

for idx in range(100, len(lines)):
    entry = lines[idx]
    step = entry.get('step')
    tcs = entry.get('tool_calls') or []
    print(f"Index {idx} Step {step} Tool calls type: {type(tcs)}: {tcs}")
