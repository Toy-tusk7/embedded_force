"""Betaflight build backend (runs in WSL): build a custom board config for an in-tree Betaflight target.

A Betaflight project keeps its board definition at `betaflight/configs/<BOARD>/config.h` (written by the agent
from the imported schematic). The build uses a pinned Betaflight release checked out in the WSL home, its own
toolchain (`make arm_sdk_install`), and for RP2350 targets the Pico SDK + picotool, which needs CMake; a portable
CMake is fetched into the user's home so no sudo is needed. Nothing is installed system-wide.
"""

import re
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def explain_undefined(symbols: List[str], limit: int = 3) -> Dict[str, str]:
    """For linker 'undefined reference to X': where X is defined in the Betaflight source and which #if guards it,
    e.g. 'pico_msc_active is defined in src/platform/PICO/usb/usb_msc_pico.c:135 only under #ifdef USE_USB_MSC'."""
    syms = [s for s in dict.fromkeys(symbols) if re.fullmatch(r"[A-Za-z_]\w{2,80}", s)][:limit]
    if not syms:
        return {}
    pat = "|".join(syms)
    code, lines, _ = wsl.run(f"cd {BF_DIR} && grep -rnwE --include=*.c '({pat})' src/main src/platform 2>/dev/null "
                             f"| grep -vE ':[0-9]+:\\s*(extern|//|\\*)' | head -40", None, 120)
    out: Dict[str, str] = {}
    for ln in lines if code in (0, 1) else []:
        m = re.match(r"([^:]+):(\d+):(.*)$", ln)
        if not m:
            continue
        path, num, src = m.group(1), int(m.group(2)), m.group(3)
        sym = next((s for s in syms if s not in out and re.match(rf"^\w[\w\s\*]*\b{s}\s*(\(|=|;|\[)", src.strip())), None)
        if not sym:
            continue
        c2, head, _ = wsl.run(f"cd {BF_DIR} && sed -n '1,{num}p' {wsl.q(path)}", None, 60)
        stack: List[str] = []
        for h in head if c2 == 0 else []:
            hs = h.strip()
            if re.match(r"#\s*if", hs):
                stack.append(hs)
            elif re.match(r"#\s*endif", hs) and stack:
                stack.pop()
            elif re.match(r"#\s*el(se|if)", hs) and stack:
                stack[-1] = f"{stack[-1]} … {hs}"
        guard = " and ".join(stack[-2:])  # the innermost conditions are the ones a config switches
        out[sym] = (f"{sym} is defined in {path}:{num}" + (f" only under {guard}" if guard else "")
                    + (" — your config must not #undef/omit that" if guard else ""))
    return out


def target_for_part(part: str, targets: List[str]) -> Optional[str]:
    """The in-tree target for an MCU name or full part number: exact target, RP2354A/B (RP2350 with stacked flash)
    -> RP2350A/B, or the longest target that prefixes the part (STM32F405RGT6 -> STM32F405)."""
    p = re.sub(r"[^A-Z0-9]", "", (part or "").upper())
    if not p:
        return None
    if p in targets:
        return p
    rp = re.fullmatch(r"RP235([04])([AB])\w*", p)
    if rp and f"RP2350{rp.group(2)}" in targets:
        return f"RP2350{rp.group(2)}"
    pref = [t for t in targets if p.startswith(t) and len(t) >= 6]
    return max(pref, key=len) if pref else None


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


def build_script(config_dir_wsl: str, board: str, target: str = "") -> str:
    """One shared checkout serves every project, and its objects are keyed by board name only. So: objects built from
    another project's config are wiped first (make's dependency files point at THAT project's config.h and would
    happily reuse them), and previous firmware outputs are deleted so only this make's results can be copied back."""
    objdir = f"obj/main/{target}_{board}" if target else ""
    stamp = f"{objdir}/.embedforge_config_dir" if objdir else ""
    guard = (f"""if [ -d {wsl.q(objdir)} ] && [ "$(cat {wsl.q(stamp)} 2>/dev/null)" != {wsl.q(config_dir_wsl)} ]; then
  echo "== Objects for {board} were built from another project: cleaning them"
  rm -rf {wsl.q(objdir)}
fi
rm -f obj/main/betaflight_{target}_{board}.elf obj/main/betaflight_{target}_{board}.map obj/betaflight_{BF_TAG}_{target}_{board}.*
mkdir -p {wsl.q(objdir)} && printf %s {wsl.q(config_dir_wsl)} > {wsl.q(stamp)}
""" if objdir else "")
    # exact output names: a glob like obj/*_RP2354B.uf2 would also match another project's ..._CUSTOM_RP2354B.uf2
    outs = (f'"$PWD"/obj/betaflight_{BF_TAG}_{target}_{board}.uf2 "$PWD"/obj/betaflight_{BF_TAG}_{target}_{board}.hex '
            f'"$PWD"/obj/betaflight_{BF_TAG}_{target}_{board}.bin' if target else
            f'"$PWD"/obj/*_{board}.uf2 "$PWD"/obj/*_{board}.hex "$PWD"/obj/*_{board}.bin')
    elf = f'"$PWD"/obj/main/betaflight_{target}_{board}.elf' if target else f'"$PWD"/obj/main/*_{board}.elf'
    return f"""set -e
export PATH={TOOLS}/cmake-{CMAKE_VERSION}-linux-x86_64/bin:$PATH
cd {BF_DIR}
{guard}make CONFIG={wsl.q(board)} CONFIG_DIR={wsl.q(config_dir_wsl)} -j"$(nproc)"
echo "== OUTPUTS"
ls -1 {outs} 2>/dev/null || true
ls -1 {elf} 2>/dev/null || true
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


_LINT_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)\b[ \t]*(.*?)\s*$")
_LINT_TAG = re.compile(r"FACT-[^\s*/,;)]+")


def _strip_c_comments(text: str) -> List[str]:
    """Per-line code with /* */ and // comments removed (keeps line numbering)."""
    out, in_block = [], False
    for raw in text.split("\n"):
        code, i = "", 0
        while i < len(raw):
            if in_block:
                end = raw.find("*/", i)
                if end < 0:
                    i = len(raw)
                else:
                    in_block, i = False, end + 2
            elif raw.startswith("/*", i):
                in_block, i = True, i + 2
            elif raw.startswith("//", i):
                break
            else:
                code += raw[i]
                i += 1
        out.append(code)
    return out


def lint_config(text: str, board: str, target: str) -> Dict[str, List[str]]:
    """Cheap checks on a Betaflight config.h before running make. 'errors' would certainly fail the build."""
    errors: List[str] = []
    warnings: List[str] = []
    code = _strip_c_comments(text)
    depth, opened = 0, []
    defines: Dict[str, Tuple[str, int]] = {}
    gpio_users: Dict[str, List[str]] = {}
    for n, line in enumerate(code, start=1):
        s = line.strip()
        if re.match(r"#\s*if(n?def)?\b", s):
            depth += 1
            opened.append(n)
        elif re.match(r"#\s*endif\b", s):
            if depth == 0:
                errors.append(f"line {n}: #endif without a matching #if (configs use '#pragma once', not include guards)")
            else:
                depth -= 1
                opened.pop()
        m = _LINT_DEFINE.match(line)
        if m:
            name, value = m.group(1), m.group(2).strip()
            if name in defines and defines[name][0] != value:
                warnings.append(f"line {n}: {name} redefined ('{defines[name][0]}' on line {defines[name][1]}, now '{value}')")
            defines[name] = (value, n)
            pm = re.fullmatch(r"PA(\d{1,2})", value)
            if name.endswith("_PIN") and pm:
                gpio_users.setdefault(pm.group(1), []).append(name)
    for n in opened:
        errors.append(f"line {n}: #if/#ifdef without a matching #endif")
    if "HSE_VALUE" in defines:
        errors.append(f"line {defines['HSE_VALUE'][1]}: do not define HSE_VALUE in a config (the target's build sets it"
                      + ("" if (target or "").startswith("RP") else "; an external crystal is declared with SYSTEM_HSE_MHZ") + ")")
    if "ADC_ESC_CURRENT_PIN" in defines:
        errors.append(f"line {defines['ADC_ESC_CURRENT_PIN'][1]}: ADC_ESC_CURRENT_PIN is not a Betaflight define; the "
                      "current-sensor ADC input is ADC_CURR_PIN")
    for name, (value, n) in defines.items():  # an instance needs that bus's pins
        im = re.fullmatch(r"\(?(SPI|I2CDEV_)(\d)\)?", value)
        if not im or not name.endswith(("_SPI_INSTANCE", "_I2C_INSTANCE")):
            continue
        bus = f"SPI{im.group(2)}" if im.group(1) == "SPI" else f"I2C{im.group(2)}"
        need = ["SCK", "SDI", "SDO"] if bus.startswith("SPI") else ["SDA", "SCL"]
        missing = [f"{bus}_{p}_PIN" for p in need if f"{bus}_{p}_PIN" not in defines]
        if missing:
            errors.append(f"line {n}: {name} is {value}, but {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not "
                          f"defined — use the bus whose pins you defined")
    tgt = defines.get("FC_TARGET_MCU", ("", 0))[0]
    if not tgt:
        errors.append(f"missing '#define FC_TARGET_MCU {target}'")
    elif target and tgt != target:
        errors.append(f"line {defines['FC_TARGET_MCU'][1]}: FC_TARGET_MCU is {tgt}, but this project's target is {target}")
    bn = defines.get("BOARD_NAME", ("", 0))[0]
    if not bn:
        errors.append(f"missing '#define BOARD_NAME {board}'")
    elif board and bn != board:
        errors.append(f"line {defines['BOARD_NAME'][1]}: BOARD_NAME is {bn}, but the config directory is {board}")
    if not defines.get("MANUFACTURER_ID"):
        errors.append(f"missing '#define MANUFACTURER_ID {MANUFACTURER_ID}'")
    for name, (value, n) in defines.items():  # bus instances are enum values, not peripheral names
        if name.endswith("_I2C_INSTANCE") and not re.fullmatch(r"\(?I2CDEV_\d\)?", value):
            fix = re.sub(r"^\(?I2C(\d)\)?$", r"I2CDEV_\1", value)
            errors.append(f"line {n}: {name} is '{value}', but I2C instances are I2CDEV_n" + (f" (here {fix})" if fix != value else ""))
        elif name.endswith("_SPI_INSTANCE") and not re.fullmatch(r"\(?SPI\d\)?", value):
            errors.append(f"line {n}: {name} is '{value}', but SPI instances are SPIn (e.g. SPI0)")
    undefs = re.findall(r"^\s*#\s*undef\s+(USE_\w+)", text, re.M)
    if undefs:
        warnings.append(f"#undef {', '.join(sorted(set(undefs))[:8])}: removing features the target enables often breaks the "
                        "link (e.g. USE_USB_MSC). Only #undef what a reference config for this target also removes.")
    if not re.search(r"^\s*#\s*pragma\s+once\b", text, re.M):
        warnings.append("no '#pragma once' at the top (every Betaflight config starts with it)")
    bad_tags: Dict[str, List[int]] = {}
    for n, raw in enumerate(text.split("\n"), start=1):
        for tag in _LINT_TAG.findall(raw):
            tag = tag.rstrip(":;,.")
            if not re.fullmatch(r"FACT-\d{3,}", tag):
                bad_tags.setdefault(tag, []).append(n)
    for tag, lines in bad_tags.items():  # compiles fine, but verify rejects it: warn now, don't block the build
        where = ", ".join(map(str, lines[:8])) + (f" (+{len(lines) - 8} more)" if len(lines) > 8 else "")
        warnings.append(f"line{'s' if len(lines) > 1 else ''} {where}: '{tag}' is not a fact id — use the FACT-nnn ids "
                        "record_board_pins/record_fact returned (verify fails on it)")
    for gpio, names in gpio_users.items():
        if len(names) > 1:
            warnings.append(f"PA{gpio} is used by {', '.join(names)}")
    return {"errors": errors, "warnings": warnings}


_KNOWN_NAMES: Dict[str, bool] = {}   # define name -> referenced by the pinned Betaflight source (fixed per release)
_ALWAYS_KNOWN = {"FC_TARGET_MCU", "BOARD_NAME", "MANUFACTURER_ID"}


def unknown_defines(names: List[str]) -> Optional[List[str]]:
    """Names that no Betaflight source file references (so defining them has no effect), e.g. MOTOR0_PIN or
    USE_GYRO_BMI270. None when the source cannot be searched."""
    todo = sorted({n for n in names if n not in _KNOWN_NAMES and n not in _ALWAYS_KNOWN
                   and re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", n)})
    if todo:
        args = " ".join(f"-e {n}" for n in todo)
        # src/main + src/platform + mk: the code that consumes config defines (not src/config, other boards' configs)
        code, lines, _ = wsl.run(f"cd {BF_DIR} && grep -rhowF --include=*.c --include=*.h --include=*.mk {args} "
                                 f"src/main src/platform mk 2>/dev/null | sort -u", None, 180)
        if code not in (0, 1):
            return None
        found = {ln.strip() for ln in lines}
        for n in todo:
            _KNOWN_NAMES[n] = n in found
    return [n for n in names if n not in _ALWAYS_KNOWN and _KNOWN_NAMES.get(n) is False]


_FLASH_PART = re.compile(r"W25|GD25|M25P|IS25|MX25|AT25|XT25|BY25|ZD25", re.I)
_SD_PART = re.compile(r"\bSD\b|SD-?CARD|MICRO\s*SD|\bTF\b|TF-?CARD", re.I)


def board_consistency(text: str, board: Any) -> List[Tuple[str, str, int, str, str]]:
    """Config claims the schematic contradicts (verify ERRORs): one GPIO driving two defines, and storage devices the
    board does not have (typical when a reference config's hardware is copied)."""
    out: List[Tuple[str, str, int, str, str]] = []
    users: Dict[str, List[Tuple[str, int]]] = {}
    defines: Dict[str, int] = {}
    for n, line in enumerate(_strip_c_comments(text), start=1):
        m = re.match(r"\s*#\s*define\s+(\w+)(?:\s+(\S+))?", line)
        if not m:
            continue
        defines.setdefault(m.group(1), n)
        pm = re.fullmatch(r"PA(\d{1,2})", m.group(2) or "")
        if m.group(1).endswith("_PIN") and pm:
            users.setdefault(pm.group(1), []).append((m.group(1), n))
    for gpio, names in users.items():
        if len({x for x, _ in names}) > 1:
            out.append(("ERROR", "PIN_USED_TWICE", names[1][1], f"PA{gpio} is used by {', '.join(x for x, _ in names)}.",
                        "One GPIO drives one function: keep the define its schematic net belongs to and remove the others."))
    if board is None:
        return out
    parts = " ".join(f"{p.value} {p.mpn} {p.footprint}" for p in board.parts.values())
    norm_parts = re.sub(r"[^A-Z0-9 ]", "", parts.upper())
    flash = [d for d in defines if d.startswith("USE_FLASH_") or d == "FLASH_CS_PIN"]
    if flash and not _FLASH_PART.search(parts):
        out.append(("ERROR", "DEVICE_NOT_ON_BOARD", defines[flash[0]], f"{flash[0]} enables an SPI flash chip, but the schematic has none.",
                    "Remove the flash defines (and BLACKBOX_DEVICE_FLASH); they were probably copied from a reference config."))
    if "BLACKBOX_DEVICE_SDCARD" in text and not _SD_PART.search(parts):
        out.append(("ERROR", "DEVICE_NOT_ON_BOARD", 0, "BLACKBOX_DEVICE_SDCARD is set, but the schematic has no SD socket.",
                    "Remove the SD card blackbox setting."))
    # chip drivers: USE_ACCGYRO_BMI270, USE_BARO_BMP388, USE_GYRO_SPI_ICM42688P, USE_MAX7456 ... need that chip on the board
    for d, n in defines.items():
        m = re.fullmatch(r"USE_(?:ACCGYRO|ACC|GYRO|BARO|MAG|FLASH)(?:_SPI|_I2C)?_([A-Z][A-Z0-9]{3,})", d)
        chip = m.group(1) if m else ("MAX7456" if d == "USE_MAX7456" else "")
        if not chip or chip in ("CHIP", "SPI", "I2C") or (m and d.startswith("USE_FLASH")):
            continue
        alts = {chip[:6]} | ({"AT7456"} if chip == "MAX7456" else set())
        if not any(a in norm_parts.replace(" ", "") for a in alts):
            out.append(("ERROR", "DEVICE_NOT_ON_BOARD", n, f"{d} enables a {chip}, but no such chip is on the schematic.",
                        "Enable only drivers for chips on this board (check the Parts list); remove this define."))
    # bus <-> device: a device's chip select and its bus clock must reach the same chip
    def gpio_of(name: str) -> Optional[int]:
        m = re.search(rf"^\s*#\s*define\s+{name}\s+PA(\d{{1,2}})\b", text, re.M)
        return int(m.group(1)) if m else None

    def devices(gpio: Optional[int]) -> set:
        if gpio is None:
            return set()
        _, refs = board.gpio_net(gpio)
        return {r for r in refs if not re.match(r"^(R|C|L|FB|TP)\d", r, re.I)}

    for dev, cs_name, inst_name in (("gyro", "GYRO_1_CS_PIN", "GYRO_1_SPI_INSTANCE"), ("gyro 2", "GYRO_2_CS_PIN", "GYRO_2_SPI_INSTANCE"),
                                    ("SD card", "SDCARD_SPI_CS_PIN", "SDCARD_SPI_INSTANCE"),
                                    ("OSD", "MAX7456_SPI_CS_PIN", "MAX7456_SPI_INSTANCE"), ("flash", "FLASH_CS_PIN", "FLASH_SPI_INSTANCE")):
        im = re.search(rf"^\s*#\s*define\s+{inst_name}\s+\(?SPI(\d)\)?", text, re.M)
        if not im:
            continue
        cs = gpio_of(cs_name)
        if cs is None:
            if dev == "SD card" or dev == "gyro":
                out.append(("ERROR", "BUS_DEVICE_MISMATCH", defines.get(inst_name, 0), f"{inst_name} is set but {cs_name} is not defined.",
                            f"Define {cs_name} with the {dev}'s chip-select pin from the schematic, or remove the {dev}."))
            continue
        on_cs, on_sck = devices(cs), devices(gpio_of(f"SPI{im.group(1)}_SCK_PIN"))
        if on_cs and not (on_cs & on_sck):
            out.append(("ERROR", "BUS_DEVICE_MISMATCH", defines.get(inst_name, 0),
                        f"{cs_name} PA{cs} goes to {', '.join(sorted(on_cs))}, but SPI{im.group(1)}_SCK_PIN does not reach that part.",
                        f"Use the SPI bus whose SCK/SDI/SDO pins are wired to {', '.join(sorted(on_cs))} for {inst_name}."))
    if "USE_SDCARD" in defines and "SDCARD_SPI_INSTANCE" not in defines:
        out.append(("ERROR", "BUS_DEVICE_MISMATCH", defines["USE_SDCARD"], "USE_SDCARD is set without SDCARD_SPI_INSTANCE and SDCARD_SPI_CS_PIN.",
                    "Give the SD card its bus and chip select from the schematic, or remove USE_SDCARD."))
    # wired MCU pins the config does not use (a warning: the finish summary must say what was left out and why)
    used = {int(g) for g in users}
    unused = []
    for m in board.mcus()[:1]:
        for pin in m.pins.values():
            g = re.match(r"GPIO(\d+)", pin.name or "", re.I)
            if g and pin.net and int(g.group(1)) not in used:
                net, refs = board.gpio_net(int(g.group(1)))
                real = [r for r in refs if not re.match(r"^(R|C|L|FB)\d", r, re.I)]
                if real:
                    unused.append(f"GPIO{int(g.group(1))} ({net} -> {', '.join(real[:3])})")
    if unused:
        out.append(("WARNING", "WIRED_PIN_UNUSED", 0, f"Wired MCU pins not used by the config: {'; '.join(sorted(unused)[:16])}.",
                    "Configure each function the board wires (e.g. GYRO_1_EXTI_PIN, ADC_CURR_PIN, SD card pins), or say in the "
                    "finish summary why it is left out."))
    return out


def pin_summary(text: str) -> List[str]:
    """'NAME = PAn [FACT-nnn]' for each pin define, so the model can see its pin map without re-reading the file."""
    out = []
    for raw in text.split("\n"):
        m = re.match(r"\s*#\s*define\s+(\w+_PIN)\s+(PA\d+|NONE)\b(.*)", raw)
        if m:
            tags = re.findall(r"FACT-\d{3,}", m.group(3))
            out.append(f"{m.group(1)} = {m.group(2)}" + (f" [{', '.join(tags)}]" if tags else " [untagged]"))
    return out


_BUILD_LOCK = threading.Lock()  # one checkout: concurrent makes would share obj/ and the config symlinks


def build(project_dir: Path, on_line: LineSink = None, timeout_s: int = 3600, board_hint: str = "") -> BFResult:
    """Build the project's Betaflight config in WSL and copy the outputs into the project (serialised)."""
    if not _BUILD_LOCK.acquire(blocking=False):
        if on_line:
            on_line("Another Betaflight build is running — waiting for it to finish…")
        _BUILD_LOCK.acquire()
    try:
        return _build(project_dir, on_line, timeout_s, board_hint)
    finally:
        _BUILD_LOCK.release()


def _build(project_dir: Path, on_line: LineSink, timeout_s: int, board_hint: str) -> BFResult:
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
    lint = lint_config(files[0].read_text(encoding="utf-8", errors="ignore"), board, target)
    if lint["errors"]:
        from embedforge.toolchain.pio import Diagnostic
        rel = str(files[0].relative_to(project_dir)).replace("\\", "/")
        diags = []
        for e in lint["errors"]:
            m = re.match(r"line (\d+): (.*)", e)
            diags.append(Diagnostic("error", rel, int(m.group(1)) if m else 0, m.group(2) if m else e))
        return BFResult(ok=False, board=board, target=target, diagnostics=diags,
                        error="Config check failed before make (no build was run).")
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
    code, lines, timed_out = wsl.run(build_script(wsl.to_wsl_path(str(project_dir / "betaflight")), board, target), on_line, timeout_s)
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
    undefined = [m.group(1) for d in res.diagnostics for m in [re.search(r"undefined reference to `([^']+)'", d.message)] if m]
    if undefined:  # linker errors have no source line: say where the symbol lives and what guards it
        why = explain_undefined(undefined)
        for d in res.diagnostics:
            m = re.search(r"undefined reference to `([^']+)'", d.message)
            if m and m.group(1) in why:
                d.message = f"{d.message} [{why[m.group(1)]}]"
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
