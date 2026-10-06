import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('workspace/projects/20261003-210530-76b6/conversation.json', 'r', encoding='utf-8') as f:
    conv = json.load(f)

print(f"Total messages in conversation: {len(conv)}")

for idx, msg in enumerate(conv[-40:]):
    role = msg.get('role')
    tcs = msg.get('tool_calls')
    content = msg.get('content')
    tool_call_id = msg.get('tool_call_id')
    name = msg.get('name')
    
    if role == 'user':
        print(f"\n[USER]: {content[:200]}")
    elif role == 'assistant':
        if tcs:
            for tc in tcs:
                fn = tc.get('function', {})
                print(f"  [ASSISTANT TOOL CALL]: {fn.get('name')} -> {fn.get('arguments')}")
        if content:
            print(f"  [ASSISTANT MSG]: {str(content)[:200]}")
    elif role == 'tool':
        c_str = str(content)
        print(f"  [TOOL RESULT ({name})]: {c_str[:150]} (length: {len(c_str)})")
