"""The Claude provider: provider selection and the translation between the loop's OpenAI-style history and the
Messages API. Offline: no request is sent to any model."""

import json
from types import SimpleNamespace

import pytest

from embedforge.agent import llm
from embedforge.agent.llm import AgentLLM, from_anthropic_message, to_anthropic_messages, to_anthropic_tools
from embedforge.core.config import config


@pytest.fixture
def claude_configured(monkeypatch):
    monkeypatch.setattr(config, "anthropic_api_key", "test-key")
    monkeypatch.setattr(config, "llm_provider", "")
    return config


def test_nemotron_on_nebius_comes_first_and_a_saved_provider_is_kept(claude_configured, monkeypatch):
    assert llm.default_provider() == "nebius"                          # Nemotron on Nebius even with Claude configured
    a = AgentLLM("claude-opus-5-5", "anthropic")                       # a project that picked Claude keeps it
    assert a.provider.id == "anthropic" and a.model == "claude-opus-5-5" and a.sampling() == {}
    monkeypatch.setattr(config, "nebius_api_key", None)
    monkeypatch.setattr(config, "nvidia_api_key", None)
    b = AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius")      # no Nemotron access: Claude
    assert b.provider.id == "anthropic" and b.model == config.anthropic_model
    monkeypatch.setattr(config, "nebius_api_key", "k")
    monkeypatch.setattr(config, "llm_provider", "anthropic")           # EMBEDFORGE_PROVIDER forces a provider
    assert AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius").provider.id == "anthropic"


def test_history_translation():
    tool_args = {"query": "U3"}
    messages = [
        {"role": "system", "content": "You are EmbedForge.\n\nPROJECT STATE (generated)\n- NEXT STEP: build"},
        {"role": "user", "content": "Write a BMP388 driver"},
        {"role": "assistant", "content": "Looking at the board.",
         "tool_calls": [{"id": "chatcmpl-tool.1", "type": "function", "function": {"name": "query_board", "arguments": json.dumps(tool_args)}},
                        {"id": "c2", "type": "function", "function": {"name": "list_facts", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "chatcmpl-tool.1", "content": '{"lines": ["U3 pin 4 (SDI)"]}'},
        {"role": "tool", "tool_call_id": "c2", "content": '{"error": "boom"}'},
        {"role": "tool", "tool_call_id": "orphan", "content": "no call"},
        {"role": "user", "content": "[System Notice] continue"},
        {"role": "assistant", "content": ""},                            # empty turn: dropped
    ]
    system, msgs = to_anthropic_messages(messages)
    assert system[0]["text"] == "You are EmbedForge." and system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["text"].startswith("PROJECT STATE")                 # the per-step state is after the cache breakpoint
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    uses = [b for b in msgs[1]["content"] if b["type"] == "tool_use"]
    assert uses[0]["id"] == "chatcmpl-tool_1" and uses[0]["input"] == tool_args
    results = [b for b in msgs[2]["content"] if b["type"] == "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["chatcmpl-tool_1", "c2"] and results[1]["is_error"] is True
    assert msgs[2]["content"][-1] == {"type": "text", "text": "[System Notice] continue"}


def test_tool_use_without_result_is_answered():
    system, msgs = to_anthropic_messages([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "function": {"name": "build", "arguments": "{}"}}]},
    ])
    assert msgs[-1]["role"] == "user" and msgs[-1]["content"][0]["tool_use_id"] == "a"


def test_tool_specs_and_response_translation():
    specs = to_anthropic_tools([{"type": "function", "function": {"name": "build", "description": "Build", "parameters": {"type": "object", "properties": {}}}}])
    assert specs == [{"name": "build", "description": "Build", "input_schema": {"type": "object", "properties": {}}}]
    msg = SimpleNamespace(stop_reason="tool_use", stop_details=None, usage=SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=0),
        content=[SimpleNamespace(type="thinking", thinking="plan"), SimpleNamespace(type="text", text="Building."),
                 SimpleNamespace(type="tool_use", id="t1", name="build", input={}), SimpleNamespace(type="fallback")])
    out = from_anthropic_message(msg)
    assert out["content"] == "Building." and out["reasoning"] == "plan" and out["finish_reason"] == "tool_calls"
    assert out["tool_calls"] == [{"id": "t1", "type": "function", "function": {"name": "build", "arguments": "{}"}}]
    assert out["usage"]["prompt_tokens"] == 110
    cut = from_anthropic_message(SimpleNamespace(stop_reason="max_tokens", stop_details=None, usage=None,
                                                 content=[SimpleNamespace(type="tool_use", id="t2", name="write_file", input={"path": "src/a"})]))
    assert cut["tool_calls"] == [] and cut["finish_reason"] == "length"     # a truncated tool input never runs
    refused = from_anthropic_message(SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
                                                     usage=None, content=[]))
    assert "declined" in refused["content"] and refused["tool_calls"] == []
