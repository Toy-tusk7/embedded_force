"""The Gemini provider (OpenAI-compatible endpoint): provider selection, the request it sends, and the thought signature
that has to travel back with each tool call. Offline: the HTTP call is faked."""

from types import SimpleNamespace

import pytest

from embedforge.agent import llm
from embedforge.agent.context import build_context
from embedforge.agent.llm import AgentLLM
from embedforge.core.config import config

SIG = {"google": {"thought_signature": "c2lnbmF0dXJl"}}


@pytest.fixture
def gemini_only(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(config, "anthropic_api_key", None)
    monkeypatch.setattr(config, "llm_provider", "")
    monkeypatch.setattr(config, "agent_temperature", None)
    monkeypatch.setattr(config, "agent_top_p", None)


def test_gemini_replaces_nemotron_and_a_chosen_provider_is_kept(gemini_only, monkeypatch):
    a = AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius")      # a project saved with the Nemotron model
    assert a.provider.id == "gemini" and a.model == config.gemini_model and a.sampling() == {}
    monkeypatch.setattr(config, "anthropic_api_key", "test-key-2")
    assert AgentLLM(None, "nebius").provider.id == "anthropic"       # Claude first when both are configured
    b = AgentLLM("gemini-3.5-flash", "gemini")                         # ... unless the project picked Gemini
    assert b.provider.id == "gemini" and b.model == "gemini-3.5-flash"
    monkeypatch.setattr(config, "llm_provider", "nebius")              # EMBEDFORGE_PROVIDER forces a provider
    assert AgentLLM(None, "gemini").provider.id == "nebius"


def test_request_shape_and_thought_signature_round_trip(gemini_only, monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent.append((url, headers, json))
            body = {"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
                {"id": "f1", "type": "function", "function": {"name": "build", "arguments": "{}"}, "extra_content": SIG}]}}],
                "usage": {"prompt_tokens": 12}}
            return SimpleNamespace(status_code=200, text="", json=lambda: body)

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    tools = [{"type": "function", "function": {"name": "build", "description": "Build", "parameters": {"type": "object", "properties": {}}}}]
    out = AgentLLM(None, "gemini").chat([{"role": "user", "content": "go"}], tools)
    url, headers, body = sent[0]
    assert url == config.gemini_base_url.rstrip("/") + "/chat/completions" and headers["Authorization"] == "Bearer test-key"
    assert body["model"] == config.gemini_model and body["reasoning_effort"] in ("minimal", "low", "medium", "high")
    assert "temperature" not in body and body["tool_choice"] == "auto" and body["max_tokens"] >= 32768
    assert out["tool_calls"][0]["extra_content"] == SIG
    # the signature stays on the call in the context the loop sends next, also once old calls are digested
    history = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "", "tool_calls": out["tool_calls"]},
               {"role": "tool", "tool_call_id": "f1", "content": "{}", "_digest": "build ok"}]
    for verbatim in (10, 0):
        msgs = build_context(history, verbatim_turns=verbatim)
        assert msgs[1]["tool_calls"][0]["extra_content"] == SIG
