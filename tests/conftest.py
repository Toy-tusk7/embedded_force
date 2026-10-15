"""Tests never write into the real workspace: projects created through the API go to a temporary directory."""

import os
from pathlib import Path

import pytest

from embedforge.core.config import config
from embedforge.core.telemetry import tracer


@pytest.fixture(autouse=True)
def _temporary_workspace(tmp_path, monkeypatch):
    # tracing set up in the developer's .env must not send test runs to LangSmith (and wait on the network)
    monkeypatch.setattr(tracer, "tracing_enabled", False)
    if not os.getenv("PLATFORMIO_CORE_DIR"):   # the installed toolchains stay where they are
        monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(Path(config.workspace_root) / ".pio_core"))
    monkeypatch.setattr(config, "workspace_root", tmp_path / "workspace_root")
    # keys in the developer's .env must not pick the provider a test runs with
    monkeypatch.setattr(config, "anthropic_api_key", None)
    monkeypatch.setattr(config, "gemini_api_key", None)
    monkeypatch.setattr(config, "llm_provider", "")
    # ... nor ever reach a paid API: a call a test forgot to fake fails authentication instead of spending credits
    for key in ("nebius_api_key", "nvidia_api_key", "tavily_api_key"):
        monkeypatch.setattr(config, key, "test-key-not-real")
    monkeypatch.setattr(config, "server_password", "")   # a hosted setup's password must not lock the tests out
    monkeypatch.setattr(config, "selfhosted_base_url", "")   # nor a self-hosted model server be picked or called
