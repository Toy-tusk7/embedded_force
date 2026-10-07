"""Betaflight build backend (runs in WSL): build a custom board config for an in-tree Betaflight target.

A Betaflight project keeps its board definition at `betaflight/configs/<BOARD>/config.h` (written by the agent
from the imported schematic). The build uses a pinned Betaflight release checked out in the WSL home, its own
toolchain (`make arm_sdk_install`), and for RP2350 targets the Pico SDK + picotool, which needs CMake; a portable
CMake is fetched into the user's home so no sudo is needed. Nothing is installed system-wide.
"""

import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from embedforge.core.logger import logger
from embedforge.toolchain import wsl
from embedforge.toolchain.pio import parse_diagnostics

LineSink = Optional[Callable[[str], None]]

BF_TAG = "2026.6.2"
BF_REPO = "https://github.com/betaflight/betaflight.git"
CMAKE_VERSION = "3.31.6"
HOME = "$HOME/.embedforge"
BF_DIR = f"{HOME}/betaflight-{BF_TAG}"
TOOLS = f"{HOME}/tools"
_TARGET_RE = re.compile(r"^[A-Z0-9_]{3,24}$")


def available_targets() -> List[str]:
    """In-tree MCU targets of the pinned checkout (what FC_TARGET_MCU may name), e.g. RP2350B, STM32F405."""
    code, lines, _ = wsl.run(targets_script(), None, 120)
    return sorted({ln.strip() for ln in lines if _TARGET_RE.match(ln.strip())}) if code == 0 else []


def setup_script() -> str:
    """Idempotent: Betaflight checkout, portable CMake, Arm GCC, Pico SDK and picotool."""
    return f"""set -e
mkdir -p {HOME} {TOOLS}
if [ ! -d {BF_DIR}/.git ]; then
  echo "== Cloning Betaflight {BF_TAG}"
  git clone --depth 1 --branch {BF_TAG} {BF_REPO} {BF_DIR}
fi
CM={TOOLS}/cmake-{CMAKE_VERSION}-linux-x86_64
if [ ! -x $CM/bin/cmake ]; then
  echo "== Fetching portable CMake {CMAKE_VERSION}"
  cd {TOOLS}
  base=https://github.com/Kitware/CMake/releases/download/v{CMAKE_VERSION}
  curl -fsSL -o cmake.tgz $base/cmake-{CMAKE_VERSION}-linux-x86_64.tar.gz
  curl -fsSL -o cmake.sha $base/cmake-{CMAKE_VERSION}-SHA-256.txt
  want=$(grep "cmake-{CMAKE_VERSION}-linux-x86_64.tar.gz" cmake.sha | cut -d' ' -f1)
  got=$(sha256sum cmake.tgz | cut -d' ' -f1)
  [ "$want" = "$got" ] || {{ echo "CMake checksum mismatch"; exit 1; }}
  tar xzf cmake.tgz && rm -f cmake.tgz cmake.sha
fi
export PATH=$CM/bin:$PATH
cd {BF_DIR}
echo "== Arm toolchain"; make arm_sdk_install
echo "== Pico SDK"; make pico_sdk
echo "== picotool"; make picotool_install
if [ ! -d src/config/configs ]; then echo "== Board configs (reference)"; make configs; fi
echo "== Setup complete"
"""


def setup_done_script() -> str:
    cm = f"{TOOLS}/cmake-{CMAKE_VERSION}-linux-x86_64/bin/cmake"
    return f"[ -d {BF_DIR}/.git ] && [ -x {cm} ] && [ -d {BF_DIR}/src/config/configs ] && ls {BF_DIR}/tools/picotool/picotool >/dev/null 2>&1 && echo READY"


def is_ready() -> bool:
    code, lines, _ = wsl.run(setup_done_script(), None, 120)
    return code == 0 and "READY" in lines


def reference_configs(target: str, limit: int = 12) -> List[str]:
    """Official board configs (betaflight/config repo) built for the same MCU target — real examples to follow."""
    if not _TARGET_RE.match(target or ""):
        return []
    script = (f"cd {BF_DIR} && grep -rlE '^\\s*#\\s*define\\s+FC_TARGET_MCU\\s+{target}\\b' src/config/configs "
              f"--include=config.h 2>/dev/null | sort | head -{int(limit)}")
    code, lines, _ = wsl.run(script, None, 120)
    return [f"bf:{ln.strip()}" for ln in lines if ln.strip().endswith("config.h")] if code == 0 else []


_BF_PATH_RE = re.compile(r"^bf:(src/[A-Za-z0-9_./\-]+)$")


def search(pattern: str, limit: int = 40) -> List[str]:
    """grep -E over the pinned Betaflight source and board configs; results are 'bf:<path>:<line>: <text>'."""
    script = (f"cd {BF_DIR} && grep -rnE --include=*.h --include=*.c -e {wsl.q(pattern)} src/main src/platform/PICO "
              f"src/platform/common src/config/configs 2>/dev/null | head -{int(limit)} | cut -c1-220")
    code, lines, _ = wsl.run(script, None, 180)
    return [f"bf:{ln}" for ln in lines if ln.strip()] if code in (0, 1) else []


def read_file(path: str, start: int = 1, end: int = 160) -> Optional[Dict[str, Any]]:
    m = _BF_PATH_RE.match(path.strip())
    if not m or ".." in m.group(1):
        return None
    start, end = max(1, int(start)), max(int(start), min(int(end), int(start) + 240))
    code, lines, _ = wsl.run(f"cd {BF_DIR} && wc -l < {wsl.q(m.group(1))} && sed -n '{start},{end}p' {wsl.q(m.group(1))}", None, 120)
    if code != 0 or not lines:
        return None
    total = int(lines[0]) if lines[0].strip().isdigit() else 0
    body = lines[1:]
    return {"path": path, "start": start, "end": start + len(body) - 1, "total_lines": total,
            "text": "\n".join(f"{start + i}: {t}" for i, t in enumerate(body))}


def build_script(config_dir_wsl: str, board: str) -> str:
    return f"""set -e
export PATH={TOOLS}/cmake-{CMAKE_VERSION}-linux-x86_64/bin:$PATH
cd {BF_DIR}
make CONFIG={wsl.q(board)} CONFIG_DIR={wsl.q(config_dir_wsl)} -j"$(nproc)"
echo "== OUTPUTS"
ls -1 "$PWD"/obj/*_{board}.uf2 "$PWD"/obj/*_{board}.hex "$PWD"/obj/*_{board}.bin 2>/dev/null || true
ls -1 "$PWD"/obj/main/*_{board}.elf 2>/dev/null || true
"""


def targets_script() -> str:
    return f"ls -1d {BF_DIR}/src/platform/*/target/*/ 2>/dev/null | xargs -n1 basename | grep -v '^common$'"


@dataclass
class BFResult:
    ok: bool
    board: str = ""
    target: str = ""
    duration_s: float = 0.0
    diagnostics: List[Any] = field(default_factory=list)
    flash_used: int = 0
    flash_total: int = 0
    ram_used: int = 0
    ram_total: int = 0
    artifacts: Dict[str, str] = field(default_factory=dict)
    log_tail: str = ""
    error: str = ""

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d["diagnostics"] = [x.public() if hasattr(x, "public") else x for x in self.diagnostics]
        return d


MANUFACTURER_ID = "CUST"   # Betaflight's reserved manufacturer id for homebrew boards


def config_files(project_dir: Path) -> List[Path]:
    """config.h files in either layout: configs/<BOARD>/ or configs/<MANUFACTURER_ID>/<BOARD>/."""
    base = Path(project_dir) / "betaflight" / "configs"
    if not base.is_dir():
        return []
    return sorted(set(base.glob("*/config.h")) | set(base.glob("*/*/config.h")))


def configs(project_dir: Path) -> List[str]:
    return [p.parent.name for p in config_files(project_dir)]


def write_flash_override(board_dir: Path, flash_mb: int) -> List[str]:
    """Per-board flash size for RP2350 targets (Betaflight 2026.6+), in the exact form the official
    RASPBERRY_PI_UAVFC config uses. Generated from fixed templates — never model-written, since config.mk is make code."""
    if flash_mb not in (2, 4, 8, 16):
        raise ValueError("flash_mb must be 2, 4, 8 or 16")
    kb, nbytes = flash_mb * 1024, flash_mb * 1024 * 1024
    font = "0" if flash_mb <= 2 else "16K"   # FB_OSD font space; a 2 MB part cannot spare it
    (board_dir / "config.mk").write_text(
        "# Generated by EmbedForge: flash size for this board (see pico_flash_mem.ld).\n"
        f"TARGET_FLASH_SIZE = {kb}\n"
        "PICO_FLASH_DEFINES = \\\n"
        "                   -DPICO_FLASH_SPI_CLKDIV=2 \\\n"
        f"                   -DPICO_FLASH_SIZE_BYTES={nbytes} \\\n"
        "                   -DPICO_BOOT_STAGE2_CHOOSE_W25Q080=1\n", encoding="utf-8")
    (board_dir / "pico_flash_mem.ld").write_text(
        "/* Generated by EmbedForge: primary flash and FB_OSD font allocation for this board. */\n"
        f"PRIMARY_FLASH_LENGTH = {flash_mb}M;\nFONT_LENGTH = {font};\n", encoding="utf-8")
    return ["config.mk", "pico_flash_mem.ld"]


def config_target(config_h: Path) -> str:
    m = re.search(r"^\s*#\s*define\s+FC_TARGET_MCU\s+(\w+)", config_h.read_text(encoding="utf-8", errors="ignore"), re.M)
    return m.group(1) if m else ""


def setup(on_line: LineSink = None, timeout_s: int = 3600) -> Dict[str, Any]:
    ok, detail = wsl.available()
    if not ok:
        return {"ok": False, "error": detail}
    code, lines, timed_out = wsl.run(setup_script(), on_line, timeout_s)
    good = code == 0 and not timed_out and any("Setup complete" in ln for ln in lines)
    logger.log_event("BETAFLIGHT_SETUP", f"ok={good}")
    return {"ok": good, "error": "" if good else "\n".join(lines[-25:])}


_MEM_RE = re.compile(r"^\s*(FLASH|FLASH1|RAM|SRAM\w*|MAIN_RAM)\s*:\s*(\d+)\s*([KMG]?B)\s+(\d+)\s*([KMG]?B)", re.I)


def _bytes(n: str, unit: str) -> int:
    return int(n) * {"B": 1, "KB": 1024, "MB": 1024 * 1024, "GB": 1024 ** 3}[unit.upper()]


def build(project_dir: Path, on_line: LineSink = None, timeout_s: int = 3600, board_hint: str = "") -> BFResult:
    """Build the project's Betaflight config in WSL and copy the outputs into the project."""
    t0 = time.time()
    project_dir = Path(project_dir).resolve()
    files = config_files(project_dir)
    if not files:
        return BFResult(ok=False, error="No betaflight/configs/CUST/<BOARD>/config.h in this project.")
    files = [f for f in files if f.parent.name == board_hint] or files
    board = files[0].parent.name
    if not wsl.safe_name(board):
        return BFResult(ok=False, board=board, error="Board name must be letters, digits, '_' or '-'.")
    target = config_target(files[0])
    ok, detail = wsl.available()
    if not ok:
        return BFResult(ok=False, board=board, target=target, error=detail)
    known = available_targets()
    if target not in known:
        return BFResult(ok=False, board=board, target=target,
                        error=f"config.h must '#define FC_TARGET_MCU <target>' naming an in-tree Betaflight {BF_TAG} target: "
                              + (", ".join(known) or "(run Betaflight setup first)"))
    if on_line:
        on_line(f"$ make CONFIG={board}   (Betaflight {BF_TAG}, target {target}, in WSL)")
    code, lines, timed_out = wsl.run(build_script(wsl.to_wsl_path(str(project_dir / "betaflight")), board), on_line, timeout_s)
    res = BFResult(ok=code == 0 and not timed_out, board=board, target=target, duration_s=round(time.time() - t0, 1))
    res.diagnostics = parse_diagnostics(lines)
    proj_wsl = wsl.to_wsl_path(str(project_dir)).rstrip("/") + "/"
    for d in res.diagnostics:  # report project files relative to the project, like PlatformIO builds do
        if d.file.startswith(proj_wsl):
            d.file = d.file[len(proj_wsl):]
        elif d.file.startswith(("/home/", "./src/", "src/")):
            d.file = "betaflight-src:" + d.file.split("/betaflight-" + BF_TAG + "/")[-1].lstrip("./")
    for ln in lines:
        m = _MEM_RE.match(ln)
        if m:
            used, total = _bytes(m.group(2), m.group(3)), _bytes(m.group(4), m.group(5))
            if m.group(1).upper().startswith("FLASH"):
                res.flash_used, res.flash_total = used, total
            elif not res.ram_total:
                res.ram_used, res.ram_total = used, total
    res.log_tail = "\n".join(lines[-60:])
    if timed_out:
        res.error = f"Build timed out after {timeout_s}s."
    if res.ok:
        out_dir = project_dir / ".pio" / "betaflight"   # managed build folder (not editable by the agent)
        out_dir.mkdir(parents=True, exist_ok=True)
        tail = lines[lines.index("== OUTPUTS") + 1:] if "== OUTPUTS" in lines else []
        for wpath in tail:
            wpath = wpath.strip()
            ext = wpath.rsplit(".", 1)[-1].lower() if "." in wpath else ""
            if ext not in ("uf2", "hex", "bin", "elf"):
                continue
            c2, out, _ = wsl.run(f"wslpath -w {wsl.q(wpath)}", None, 60)
            win = out[-1].strip() if c2 == 0 and out else ""
            # the checkout lives in the Linux filesystem: copy through \\wsl$ into the project
            if win:
                dst = out_dir / Path(wpath).name
                try:
                    shutil.copy2(win, dst)
                    res.artifacts[ext] = str(dst)
                except OSError as e:
                    res.error = f"Built, but could not copy {wpath}: {e}"
        if not res.artifacts:
            res.ok = False
            res.error = res.error or "Build finished but no firmware files were found in obj/."
    elif not res.error and not res.diagnostics:
        res.error = "Build failed; see log tail."
    logger.log_event("BETAFLIGHT_BUILD", f"{board}/{target}: ok={res.ok} artifacts={list(res.artifacts)}")
    return res
