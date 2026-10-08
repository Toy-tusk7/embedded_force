"""Tool calls a model wrote as text instead of the API's tool_calls field.

Some models (Nemotron / Qwen-coder style) sometimes put their calls in the message text, and the provider does not
always turn them into structured calls:

    <tool_call>
    <function=read_source>            (or, malformed, <read_source>)
    <parameter=source_id>
    SRC-002
    </parameter>
    </function>
    </tool_call>

or Hermes style  <tool_call>{"name": "read_source", "arguments": {...}}</tool_call>.
Only names of real tools are accepted; anything else stays text.
"""

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Tuple

_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
_PARAM = re.compile(r"<parameter=([A-Za-z_]\w*)>\s*\n?(.*?)\n?\s*</parameter>", re.S)


def _value(raw: str) -> Any:
    s = raw.strip()
    if s[:1] in "[{" or re.fullmatch(r"-?\d+(\.\d+)?|true|false|null", s):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return raw.strip("\n")


def parse_text_tool_calls(content: str, tool_names: Iterable[str]) -> Tuple[List[Dict[str, Any]], str]:
    """(calls in the OpenAI tool_calls shape, the text with the call blocks removed)."""
    names = set(tool_names)
    calls: List[Dict[str, Any]] = []
    if "<tool_call>" not in (content or ""):
        return calls, content or ""
    for m in _BLOCK.finditer(content):
        body = m.group(1)
        name, args = None, None
        if body.lstrip().startswith("{"):
            try:
                obj = json.loads(body[body.index("{"):body.rindex("}") + 1])
                name, args = obj.get("name"), obj.get("arguments") or obj.get("parameters") or {}
                if isinstance(args, str):
                    args = json.loads(args)
            except ValueError:
                continue
        else:
            head = re.match(r"\s*<(?:function=)?([A-Za-z_]\w*)\s*>", body)
            if head:
                name = head.group(1)
                args = {k: _value(v) for k, v in _PARAM.findall(body)}
        if name in names and isinstance(args, dict):
            calls.append({"id": f"call_text_{uuid.uuid4().hex[:12]}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}})
    rest = _BLOCK.sub("", content).strip() if calls else content
    return calls, rest
