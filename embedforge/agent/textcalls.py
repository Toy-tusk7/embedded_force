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

or Hermes style  <tool_call>{"name": "read_source", "arguments": {...}}</tool_call>, or a bare JSON object in the text
(often in a ```json fence): {"tool": "resolve_target", "arguments": {...}}.
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


_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARG_KEYS = ("arguments", "parameters", "args", "input")


def _call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": f"call_text_{uuid.uuid4().hex[:12]}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _bare_json_calls(content: str, names: set) -> Tuple[List[Dict[str, Any]], str]:
    """JSON objects in the text that are exactly a call of a real tool: {"tool"|"name": <tool>, "arguments": {...}}."""
    calls: List[Dict[str, Any]] = []
    spans: List[Tuple[int, int]] = []
    dec = json.JSONDecoder()
    i = content.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(content, i)
        except ValueError:
            i = content.find("{", i + 1)
            continue
        name = next((obj.get(k) for k in _NAME_KEYS if isinstance(obj, dict) and isinstance(obj.get(k), str)), None)
        args = next((obj.get(k) for k in _ARG_KEYS if isinstance(obj, dict) and k in obj), {}) if name else None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        if name in names and isinstance(args, dict) and set(obj) <= set(_NAME_KEYS) | set(_ARG_KEYS) | {"id", "type"}:
            calls.append(_call(name, args))
            spans.append((i, end))
        i = content.find("{", end)
    rest = content
    for a, b in reversed(spans):
        rest = rest[:a] + rest[b:]
    rest = re.sub(r"```(?:json)?\s*```", "", rest).strip() if calls else content
    return calls, rest


def parse_text_tool_calls(content: str, tool_names: Iterable[str]) -> Tuple[List[Dict[str, Any]], str]:
    """(calls in the OpenAI tool_calls shape, the text with the call blocks removed)."""
    names = set(tool_names)
    calls: List[Dict[str, Any]] = []
    if "<tool_call>" not in (content or ""):
        return _bare_json_calls(content or "", names)
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
            calls.append(_call(name, args))
    rest = _BLOCK.sub("", content).strip() if calls else content
    return calls, rest
