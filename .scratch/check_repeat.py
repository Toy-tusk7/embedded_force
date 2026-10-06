import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('workspace/projects/20261003-210530-76b6/conversation.json', 'r', encoding='utf-8') as f:
    conv = json.load(f)

for i, m in enumerate(conv[-50:]):
    if m.get('role') == 'assistant' and m.get('tool_calls'):
        for tc in m['tool_calls']:
            fn = tc.get('function', {})
            print(f"[{i}] Tool: {fn.get('name')} -> Args: {fn.get('arguments')}")
    elif m.get('role') == 'tool':
        c = str(m.get('content', ''))
        print(f"     Result: {c[:220]}")
