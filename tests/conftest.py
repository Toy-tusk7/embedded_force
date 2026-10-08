"""Tests never write into the real workspace: projects created through the API go to a temporary directory."""

import os
from pathlib import Path

import pytest

from embedforge.core.config import config


@pytest.fixture(autouse=True)
def _temporary_workspace(tmp_path, monkeypatch):
    if not os.getenv("PLATFORMIO_CORE_DIR"):   # the installed toolchains stay where they are
        monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(Path(config.workspace_root) / ".pio_core"))
    monkeypatch.setattr(config, "workspace_root", tmp_path / "workspace_root")
