"""Nemotron 3 requests on Nebius: the model card's sampling and chat-template options, and the fallback when a
provider does not take the options. Offline: the HTTP call is faked."""

from types import SimpleNamespace

import pytest

from embedforge.agent import llm
from embedforge.agent.llm import AgentLLM
from embedforge.core.config import config

OK = {"choices": [{"finish_reason": "stop", "message": {"content": "done", "reasoning_content": "thought"}}], "usage": {}}


def _client(responses, sent):
    class Fake:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            sent.append(dict(json))
            status, body = responses.pop(0)
            return SimpleNamespace(status_code=status, text=body if isinstance(body, str) else "", json=lambda: body)
    return Fake


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(config, "agent_temperature", None)
    monkeypatch.setattr(config, "agent_top_p", None)
    monkeypatch.setattr(llm, "_NO_TEMPLATE_KWARGS", set())


def test_nemotron3_gets_the_model_card_template_options(monkeypatch):
    sent = []
    monkeypatch.setattr(llm.httpx, "Client", _client([(200, OK)], sent))
    out = AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius").chat([{"role": "user", "content": "go"}])
    body = sent[0]
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "force_nonempty_content": True}
    assert body["temperature"] == 1.0 and body["top_p"] == 0.95
    assert (out["content"], out["reasoning"]) == ("done", "thought")
    monkeypatch.setenv("EMBEDFORGE_NEMOTRON_EFFORT", "medium")
    assert AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius").template_kwargs()["medium_effort"] is True


def test_a_provider_that_rejects_the_template_options_is_used_without_them(monkeypatch):
    sent = []
    monkeypatch.setattr(llm.httpx, "Client", _client(
        [(400, "Unrecognized request argument supplied: chat_template_kwargs"), (200, OK), (200, OK)], sent))
    assert AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius").chat([{"role": "user", "content": "go"}])["content"] == "done"
    assert "chat_template_kwargs" in sent[0] and "chat_template_kwargs" not in sent[1]
    AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius").chat([{"role": "user", "content": "next run"}])
    assert "chat_template_kwargs" not in sent[2]   # remembered for that provider and model


def test_a_self_hosted_nemotron_server_comes_first_and_gets_the_same_options(monkeypatch):
    monkeypatch.setattr(config, "selfhosted_base_url", "http://127.0.0.1:8001/v1")
    monkeypatch.setattr(config, "selfhosted_model", "nemotron-3-super")
    assert llm.default_provider() == "selfhosted"
    a = AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius")      # a project saved on Token Factory
    assert a.provider.id == "nebius"                                 # keeps its provider while that is configured
    b = AgentLLM(None, None)
    assert (b.provider.id, b.model, b.base_url) == ("selfhosted", "nemotron-3-super", "http://127.0.0.1:8001/v1")
    sent = []
    monkeypatch.setattr(llm.httpx, "Client", _client([(200, OK)], sent))
    b.chat([{"role": "user", "content": "go"}])
    assert sent[0]["model"] == "nemotron-3-super" and sent[0]["chat_template_kwargs"]["enable_thinking"] is True
    assert sent[0]["temperature"] == 1.0 and sent[0]["top_p"] == 0.95


def test_the_nvidia_api_gets_its_own_model_id(monkeypatch):
    monkeypatch.setattr(config, "nebius_api_key", None)
    a = AgentLLM("nvidia/Nemotron-3-Ultra-550b-a55b", "nebius")     # saved on Nebius; only the NVIDIA API is configured
    assert (a.provider.id, a.model) == ("nvidia", config.nvidia_model) and a.model == "nvidia/nemotron-3-ultra-550b-a55b"
    assert AgentLLM("nvidia/nemotron-3-nano-30b-a3b", "nvidia").model == "nvidia/nemotron-3-nano-30b-a3b"   # picked there
    assert a.template_kwargs()["enable_thinking"] is True and a.sampling() == {"temperature": 1.0, "top_p": 0.95}


def test_server_errors_are_retried_with_backoff_and_reported_as_transient(monkeypatch):
    waits, sent = [], []
    monkeypatch.setattr(llm.time, "sleep", waits.append)
    monkeypatch.setattr(llm.httpx, "Client", _client([(500, "Internal server error"), (503, "busy"), (200, OK)], sent))
    a = AgentLLM("nvidia/nemotron-3-ultra-550b-a55b", "nvidia")
    assert a.chat([{"role": "user", "content": "go"}])["content"] == "done"
    assert waits == [2.0, 4.0] and len(sent) == 3
    waits.clear()
    monkeypatch.setattr(llm.httpx, "Client", _client([(500, "Internal server error")] * 6, sent))
    with pytest.raises(llm.LLMError) as e:
        a.chat([{"role": "user", "content": "go"}])
    assert e.value.transient and "after 6 attempts: HTTP 500" in str(e.value)
    assert waits == [2.0, 4.0, 8.0, 16.0, 32.0]   # about a minute of waiting, none after the last attempt
    monkeypatch.setattr(llm.httpx, "Client", _client([(401, "Unauthorized")], sent))
    with pytest.raises(llm.LLMError) as e:
        a.chat([{"role": "user", "content": "go"}])
    assert not e.value.transient


def test_other_models_get_no_template_options(monkeypatch):
    sent = []
    monkeypatch.setattr(llm.httpx, "Client", _client([(200, OK)], sent))
    AgentLLM("meta-llama/Llama-3.3-70B-Instruct", "nebius").chat([{"role": "user", "content": "go"}])
    assert "chat_template_kwargs" not in sent[0]
