"""Betaflight backend: write policy, project setup and build dispatch (WSL mocked)."""

from pathlib import Path

import pytest

from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt, check_agent_write
from embedforge.agent.workspace import Project
from embedforge.toolchain import betaflight, wsl
from embedforge.truth import FactLedger, SourceStore


@pytest.fixture
def belt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(wsl, "available", lambda: (True, "Ubuntu"))
    monkeypatch.setattr(betaflight, "is_ready", lambda: True)
    monkeypatch.setattr(betaflight, "available_targets", lambda: ["RP2350A", "RP2350B", "STM32F405"])
    monkeypatch.setattr(betaflight, "reference_configs", lambda t: [f"bf:src/config/configs/EXAMPLE_{t}/config.h"])
    project = Project("bf1", tmp_path / "bf1")
    store = SourceStore(project.root)
    return ToolBelt(project, RunSession("r", project.id), store, FactLedger(project.root, store))


def test_write_policy_for_betaflight_configs():
    assert check_agent_write("betaflight/configs/MYFC/config.h", "#define FC_TARGET_MCU RP2350B") is None
    assert check_agent_write("betaflight/configs/MYFC/config.c", "") is None
    assert check_agent_write("betaflight/configs/CUST/MYFC/config.h", "") is None
    assert check_agent_write("betaflight/configs/MYFC/config.mk", "x := $(shell id)") is not None   # make can run commands
    assert check_agent_write("betaflight/configs/CUST/MYFC/config.mk", "") is not None
    assert check_agent_write("betaflight/configs/CUST/MYFC/pico_flash_mem.ld", "") is not None      # generated only
    assert check_agent_write("betaflight/Makefile", "") is not None
    assert check_agent_write("betaflight/configs/../../evil/config.h", "") is not None


def test_init_betaflight_sets_project_kind_and_returns_references(belt):
    res = belt.init_betaflight("rp2354b fc", "RP2350B", flash_mb=2)
    assert res["ok"] and res["config_path"] == "betaflight/configs/CUST/RP2354B_FC/config.h"
    board_dir = belt.project.root / "betaflight" / "configs" / "CUST" / "RP2354B_FC"
    mk = (board_dir / "config.mk").read_text(encoding="utf-8")
    assert "TARGET_FLASH_SIZE = 2048" in mk and "PICO_FLASH_SIZE_BYTES=2097152" in mk
    assert "PRIMARY_FLASH_LENGTH = 2M;" in (board_dir / "pico_flash_mem.ld").read_text(encoding="utf-8")
    assert "error" in belt.init_betaflight("x", "STM32F405", flash_mb=2)
    assert res["reference_configs"] == ["bf:src/config/configs/EXAMPLE_RP2350B/config.h"]
    meta = belt.project.meta()
    assert (meta["kind"], meta["bf_target"], meta["board"]) == ("betaflight", "RP2350B", "RP2354B_FC")
    assert "error" in belt.init_betaflight("x", "RP2040")          # not an in-tree target


def test_build_dispatches_to_betaflight_and_registers_uf2(belt, tmp_path, monkeypatch):
    belt.init_betaflight("myfc", "RP2350B")
    assert "error" not in belt.write_file("betaflight/configs/CUST/MYFC/config.h", "#define FC_TARGET_MCU RP2350B\n#define BOARD_NAME MYFC\n")
    uf2 = tmp_path / "fw.uf2"
    uf2.write_bytes(b"UF2\n" * 64)

    def fake_build(root, on_line=None):
        on_line and on_line("make CONFIG=MYFC")
        return betaflight.BFResult(ok=True, board="MYFC", target="RP2350B", flash_used=500_000, flash_total=4_194_304,
                                   artifacts={"uf2": str(uf2)})

    monkeypatch.setattr(betaflight, "build", fake_build)
    out = belt.build()
    assert out["ok"] and out["version"] == 1
    mf = belt.project.builds()[0]
    assert mf["files"]["uf2"].endswith(".uf2") and mf["mcu"] == "RP2350B"


def test_config_target_and_listing(tmp_path):
    cfg = tmp_path / "betaflight" / "configs" / "CUST" / "MYFC"
    cfg.mkdir(parents=True)
    (cfg / "config.h").write_text("#pragma once\n#define FC_TARGET_MCU     RP2350B\n", encoding="utf-8")
    assert betaflight.configs(tmp_path) == ["MYFC"]
    assert betaflight.config_target(cfg / "config.h") == "RP2350B"


def test_wsl_path_conversion():
    assert wsl.to_wsl_path(r"C:\Users\srika\Music\emb\workspace\projects\x\betaflight") == "/mnt/c/Users/srika/Music/emb/workspace/projects/x/betaflight"
