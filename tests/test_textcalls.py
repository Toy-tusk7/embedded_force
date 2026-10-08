"""Tool calls written in the message text are recovered as real calls."""

import json

from embedforge.agent.textcalls import parse_text_tool_calls

NAMES = ["read_source", "record_registers", "build"]


def test_xml_style_call_with_function_tag():
    text = ("I will read it.\n<tool_call>\n<function=read_source>\n<parameter=source_id>\nSRC-002\n</parameter>\n"
            "<parameter=max_chunks>\n5\n</parameter>\n<parameter=query>\nPWR_CTRL mode\n</parameter>\n</function>\n</tool_call>")
    calls, rest = parse_text_tool_calls(text, NAMES)
    assert rest == "I will read it." and len(calls) == 1
    assert calls[0]["function"]["name"] == "read_source"
    assert json.loads(calls[0]["function"]["arguments"]) == {"source_id": "SRC-002", "max_chunks": 5, "query": "PWR_CTRL mode"}


def test_malformed_tag_and_json_values():
    text = ('<tool_call>\n<record_registers>\n<parameter=device>\nBMP388\n</parameter>\n<parameter=registers>\n'
            '[{"name": "CHIP_ID", "addr": "0x00"}]\n</parameter>\n</function>\n</tool_call>')
    calls, _ = parse_text_tool_calls(text, NAMES)
    args = json.loads(calls[0]["function"]["arguments"])
    assert calls[0]["function"]["name"] == "record_registers" and args["registers"] == [{"name": "CHIP_ID", "addr": "0x00"}]


def test_hermes_json_and_unknown_names():
    calls, _ = parse_text_tool_calls('<tool_call>{"name": "build", "arguments": {}}</tool_call>', NAMES)
    assert [c["function"]["name"] for c in calls] == ["build"]
    calls, rest = parse_text_tool_calls("<tool_call>\n<function=rm_rf>\n</function>\n</tool_call>", NAMES)
    assert calls == [] and "rm_rf" in rest
