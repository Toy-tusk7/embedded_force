"""Real firmware toolchain: PlatformIO-driven cross-compilation producing .elf/.bin/.hex.

Nothing here is simulated: boards come from PlatformIO's board registry, compilation uses the
vendor GCC toolchain + SDK packages, and the .hex is produced with the toolchain's objcopy.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

from embedforge.core.config import config
from embedforge.core.logger import logger

LineSink = Optional[Callable[[str], None]]

_DIAG_RE = re.compile(r"^(?P<file>[^\s:][^:\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>fatal error|error|warning):\s*(?P<msg>.+)$")
_LD_RE = re.compile(r"(undefined reference to .+|multiple definition of .+|region `.+' overflowed.+|cannot find .+)")
_MEM_RE = re.compile(r"^(RAM|Flash):\s*\[[^\]]*\]\s*([\d.]+)%\s*\(used (\d+) bytes from (\d+) bytes\)")


def core_dir() -> Path:
    """Project-local PlatformIO home so toolchains are cached once and never touch global state."""
    p = Path(os.getenv("PLATFORMIO_CORE_DIR") or (Path(config.workspace_root) / ".pio_core"))
    p.mkdir(parents=True, exist_ok=True)
    return p


# The project's own sources (never the framework) are compiled with GCC's bug-finding warnings on: they change no
# generated code, and verify turns the classes that mark run-time misbehaviour into errors (see BUG_WARNINGS).
STRICT_WARNINGS = "-Wall -Wextra -Wshadow -Wformat=2 -Wnull-dereference -Wduplicated-cond -Wlogical-op"

# GCC warning classes that mean the program can misbehave when it runs (not style): verify reports them as errors
BUG_WARNINGS = re.compile(r"\[-W(uninitialized|maybe-uninitialized|array-bounds|stringop-\w+|shift-count-\w+|shift-overflow\S*|"
                          r"shift-negative-value|overflow|return-type|sequence-point|format\S*|null-dereference|type-limits|"
                          r"duplicated-cond|logical-op|div-by-zero|int-in-bool-context|tautological-compare|misleading-indentation|"
                          r"implicit-function-declaration|incompatible-pointer-types|int-conversion|dangling-\w+|return-local-addr|"
                          r"free-nonheap-object|use-after-free|address|enum-compare|aggressive-loop-optimizations|"
                          r"restrict|nonnull|bool-compare|sizeof-pointer-\w+|memset-\w+|class-memaccess)\]")


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PLATFORMIO_CORE_DIR"] = str(core_dir())
    env["PLATFORMIO_DISABLE_PROGRESSBAR"] = "true"
    env["PLATFORMIO_NO_ANSI"] = "true"
    env["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] = "no"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PLATFORMIO_BUILD_SRC_FLAGS"] = (env.get("PLATFORMIO_BUILD_SRC_FLAGS", "") + " " + STRICT_WARNINGS).strip()
    return env


def _pio_cmd(args: List[str]) -> List[str]:
    return [sys.executable, "-m", "platformio", *args]


# PlatformIO platforms that are not in the default registry. Only these URLs are ever installed
# automatically; MCU part-number prefixes map to the platform that supports them. `family` is the registry
# MCU name to list boards by when the exact part (e.g. RP2354B) has no board of its own.
VENDOR_PLATFORMS: Dict[str, Dict[str, Any]] = {
    "arterytekat32": {
        "url": "https://github.com/ArteryTek/platform-arterytekat32.git",
        "prefixes": ("AT32",),
        "vendor": "ArteryTek",
        "framework": "at32firmlib",
        "official": True,
    },
    # RP2350 / RP2354 are not supported by PlatformIO's official raspberrypi platform (RP2040 only). This fork with
    # Earle Philhower's Arduino-Pico core is the de-facto PlatformIO route; enabled at the user's request.
    "rp2350": {
        "url": "https://github.com/maxgerhardt/platform-raspberrypi.git",
        "prefixes": ("RP2350", "RP2354", "PICO2"),
        "vendor": "community (maxgerhardt / Arduino-Pico)",
        "framework": "arduino",
        "family": "rp2350",
        "official": False,
        "use_url": True,  # its installed name 'raspberrypi' collides with the official RP2040-only platform
        "note": ("RP2354A/RP2354B are the RP2350A (QFN-60, 30 GPIO) / RP2350B (QFN-80, 48 GPIO) with 2 MB of in-package "
                 "flash (https://www.raspberrypi.com/news/rp2350-a4-rp2354-and-a-new-hacking-challenge/). There is no "
                 "RP2354 board definition: pick an RP2350B board for an RP2354B (e.g. weact_rp2350b) or an RP2350A board "
                 "for an RP2354A, and pass options=['board_upload.maximum_size = 2097152'] to init_project."),
    },
}


def vendor_platform_for(query: str) -> Optional[str]:
    """Return the vendor platform key whose MCU prefixes match `query` (e.g. 'AT32F421' -> 'arterytekat32')."""
    q = re.sub(r"[^A-Z0-9]", "", query.upper())
    for name, spec in VENDOR_PLATFORMS.items():
        if any(q.startswith(p) or p in q for p in spec["prefixes"]):
            return name
    return None


def _platform_source(d: Path) -> str:
    """Where an installed platform came from: git origin, PlatformIO's install record, or its manifest."""
    gitcfg = d / ".git" / "config"
    if gitcfg.is_file():
        m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', gitcfg.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return m.group(1)
    for rec, getter in ((d / ".piopm", lambda j: (j.get("spec") or {}).get("uri", "")),
                        (d / "platform.json", lambda j: (j.get("repository") or {}).get("url", ""))):
        if rec.is_file():
            try:
                src = getter(json.loads(rec.read_text(encoding="utf-8")))
            except Exception:
                src = ""
            if src:
                return src
    return ""


def _norm_url(u: str) -> str:
    return re.sub(r"^git\+", "", u.strip().lower()).removesuffix("/").removesuffix(".git")


def _installed_platform_dir(spec: Dict[str, Any]) -> Optional[Path]:
    """Installed platform that came from the spec URL (names can collide: the RP2350 fork is also 'raspberrypi')."""
    base = core_dir() / "platforms"
    if not base.is_dir():
        return None
    want = _norm_url(spec["url"])
    for d in base.iterdir():
        if d.is_dir() and _norm_url(_platform_source(d)) == want:
            return d
    return None


def platform_installed(name: str) -> bool:
    spec = VENDOR_PLATFORMS.get(name)
    return _installed_platform_dir(spec) is not None if spec else (core_dir() / "platforms" / name).is_dir()


def vendor_platform_name(name: str) -> Optional[str]:
    """PlatformIO's own name for an installed vendor platform (what `pio boards` reports as `platform`)."""
    spec = VENDOR_PLATFORMS.get(name)
    d = _installed_platform_dir(spec) if spec else None
    if d is None:
        return None
    try:
        return json.loads((d / "platform.json").read_text(encoding="utf-8")).get("name")
    except Exception:
        return None


def _run_streaming(cmd: List[str], cwd: Optional[Path], on_line: LineSink, timeout_s: int) -> tuple:
    """Run a command, stream merged stdout/stderr lines, and kill it if it exceeds `timeout_s` even when silent."""
    import threading

    lines: List[str] = []
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as e:
        return -1, [f"Could not launch: {e}"], False
    timed_out = {"v": False}

    def _kill() -> None:
        timed_out["v"] = True
        proc.kill()

    watchdog = threading.Timer(timeout_s, _kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
        proc.wait()
    finally:
        watchdog.cancel()
    if timed_out["v"]:
        lines.append(f"Timed out after {timeout_s}s.")
    return proc.returncode, lines, timed_out["v"]


def install_platform(name: str, on_line: LineSink = None, timeout_s: int = 1800) -> Dict[str, Any]:
    """Install an allow-listed vendor platform into the project-local PlatformIO core."""
    spec = VENDOR_PLATFORMS.get(name)
    if spec is None:
        return {"ok": False, "error": f"'{name}' is not an allow-listed vendor platform."}
    if platform_installed(name):
        return {"ok": True, "already_installed": True}
    code, lines, timed_out = _run_streaming(_pio_cmd(["pkg", "install", "-g", "-p", spec["url"]]), None, on_line, timeout_s)
    ok = code == 0 and not timed_out
    if ok:
        cache = core_dir() / "boards_cache.json"
        if cache.exists():
            cache.unlink()
    logger.log_event("PIO_PLATFORM", f"install {name}: ok={ok}")
    return {"ok": ok, "error": "" if ok else "\n".join(lines[-15:])}


def install_project_packages(project_dir: Path, on_line: LineSink = None, timeout_s: int = 1800) -> Dict[str, Any]:
    """Download the project's platform, toolchain and SDK framework without compiling (so SDK headers are searchable)."""
    code, lines, timed_out = _run_streaming(_pio_cmd(["pkg", "install", "-d", str(project_dir)]), project_dir, on_line, timeout_s)
    ok = code == 0 and not timed_out
    logger.log_event("PIO_PKG", f"install for {project_dir.name}: ok={ok}")
    return {"ok": ok, "error": "" if ok else "\n".join(lines[-15:])}


def is_available() -> bool:
    try:
        r = subprocess.run(_pio_cmd(["--version"]), capture_output=True, text=True, timeout=60, env=_env())
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------- boards
def _board_registry(max_age_s: int = 86400) -> List[Dict[str, Any]]:
    """Full PlatformIO board registry, cached on disk (the raw list is ~1.5k boards)."""
    cache = core_dir() / "boards_cache.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_s:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    r = subprocess.run(_pio_cmd(["boards", "--json-output"]), capture_output=True, text=True, timeout=300, env=_env(), encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"pio boards failed: {r.stderr.strip()[:400]}")
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse board list: {e}")
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def list_boards(query: str = "", limit: int = 25) -> List[Dict[str, Any]]:
    """Search the PlatformIO board registry (real data: MCU, clock, RAM/flash, frameworks)."""
    data = _board_registry()
    norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
    q = [norm(t) for t in re.split(r"[\s,/]+", query) if norm(t)]
    out = []
    for b in data:
        hay = norm(" ".join(str(b.get(k, "")) for k in ("id", "name", "mcu", "vendor", "platform")))
        if q and not all(t in hay for t in q):
            continue
        score = sum(1 for t in q if t in hay) + (3 if q and q[0] in (norm(b.get("id", "")), norm(b.get("mcu", ""))) else 0)
        out.append({
            "id": b.get("id"), "name": b.get("name"), "vendor": b.get("vendor"), "platform": b.get("platform"),
            "mcu": b.get("mcu"), "fcpu_hz": b.get("fcpu"), "ram_bytes": b.get("ram"), "flash_bytes": b.get("rom"),
            "frameworks": b.get("frameworks", []), "_score": score,
        })
    if not out and len(q) > 1:
        # e.g. "AT32F421 custom ESC": fall back to the tokens that look like part numbers
        parts = [t for t in q if re.search(r"\d", t) and re.search(r"[a-z]", t)]
        if parts and len(parts) < len(q):
            return list_boards(" ".join(parts), limit)
    out.sort(key=lambda x: (-x["_score"], len(x["id"] or "")))
    for o in out:
        o.pop("_score", None)
    return out[:limit]


# ---------------------------------------------------------------- build
@dataclass
class Diagnostic:
    severity: str
    file: str
    line: int
    message: str

    def public(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BuildResult:
    ok: bool
    env: str = ""
    duration_s: float = 0.0
    diagnostics: List[Diagnostic] = field(default_factory=list)
    flash_used: int = 0
    flash_total: int = 0
    ram_used: int = 0
    ram_total: int = 0
    artifacts: Dict[str, str] = field(default_factory=dict)
    log_tail: str = ""
    error: str = ""

    def public(self) -> Dict[str, Any]:
        d = asdict(self)
        d["diagnostics"] = [x.public() for x in self.diagnostics]
        return d


def parse_diagnostics(lines: List[str]) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    seen = set()
    for ln in lines:
        ln = ln.rstrip()
        m = _DIAG_RE.match(ln)
        if m:
            sev = "error" if "error" in m.group("sev") else "warning"
            key = (m.group("file"), m.group("line"), m.group("msg"))
            if key in seen:
                continue
            seen.add(key)
            out.append(Diagnostic(sev, m.group("file").replace("\\", "/"), int(m.group("line")), m.group("msg").strip()))
            continue
        m = _LD_RE.search(ln)
        if m and ln not in seen:
            seen.add(ln)
            out.append(Diagnostic("error", "<linker>", 0, m.group(1).strip()))
        elif ln.startswith("Error:") and ln not in seen:
            seen.add(ln)
            out.append(Diagnostic("error", "platformio.ini", 0, ln[6:].strip()))
    return out


def _objcopy_candidates() -> List[Path]:
    pk = core_dir() / "packages"
    if not pk.is_dir():
        return []
    return sorted(pk.glob("toolchain-*/bin/*objcopy*"))


def make_hex(elf: Path, out_hex: Path) -> bool:
    """Convert ELF -> Intel HEX using the toolchain's objcopy (tries each installed toolchain)."""
    for oc in _objcopy_candidates():
        try:
            r = subprocess.run([str(oc), "-O", "ihex", str(elf), str(out_hex)], capture_output=True, text=True, timeout=60)
            if r.returncode == 0 and out_hex.exists() and out_hex.stat().st_size > 0:
                return True
        except Exception:
            continue
    return False


def _parse_env_name(project_dir: Path) -> str:
    ini = project_dir / "platformio.ini"
    if ini.exists():
        m = re.search(r"^\[env:([^\]]+)\]", ini.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip()
    return ""


def build(project_dir: Path, on_line: LineSink = None, timeout_s: int = 1500) -> BuildResult:
    """Run a real PlatformIO build and collect .elf/.bin/.hex plus structured diagnostics."""
    project_dir = Path(project_dir).resolve()
    t0 = time.time()
    env_name = _parse_env_name(project_dir)
    if not env_name:
        return BuildResult(ok=False, error="platformio.ini missing or has no [env:...] section. Call init_project first.")
    code, lines, timed_out = _run_streaming(_pio_cmd(["run", "-d", str(project_dir)]), project_dir, on_line, timeout_s)
    if code not in (0, -1) and not timed_out and any("CreateProcess: No such file or directory" in ln for ln in lines[-400:]):
        # Windows sometimes refuses to start a freshly unpacked compiler (antivirus scan in progress): one clean retry
        if on_line:
            on_line("Compiler could not be started (Windows file lock); retrying the build once…")
        code, lines, timed_out = _run_streaming(_pio_cmd(["run", "-d", str(project_dir)]), project_dir, on_line, timeout_s)
    if code == -1:
        return BuildResult(ok=False, env=env_name, error=lines[0] if lines else "Could not launch PlatformIO")
    res = BuildResult(ok=code == 0 and not timed_out, env=env_name, duration_s=round(time.time() - t0, 1))
    if timed_out:
        res.error = f"Build timed out after {timeout_s}s."
    res.diagnostics = parse_diagnostics(lines)
    for ln in lines:
        m = _MEM_RE.match(ln.strip())
        if m:
            used, total = int(m.group(3)), int(m.group(4))
            if m.group(1) == "RAM":
                res.ram_used, res.ram_total = used, total
            else:
                res.flash_used, res.flash_total = used, total
    res.log_tail = "\n".join(lines[-60:])
    if res.ok:
        bdir = project_dir / ".pio" / "build" / env_name
        for ext in ("elf", "bin", "hex", "uf2"):
            p = bdir / f"firmware.{ext}"
            if p.exists():
                res.artifacts[ext] = str(p)
        if "hex" not in res.artifacts and "elf" in res.artifacts:
            from embedforge.toolchain.fwformat import elf_to_hex
            hex_path = bdir / "firmware.hex"
            if make_hex(Path(res.artifacts["elf"]), hex_path) or elf_to_hex(Path(res.artifacts["elf"]), hex_path):
                res.artifacts["hex"] = str(hex_path)
        if "hex" not in res.artifacts:
            res.error = "Build succeeded but no .hex could be produced for this target (see .bin/.elf)."
    elif not res.diagnostics:
        res.error = "Build failed; see log tail."
    logger.log_event("BUILD", f"{env_name}: ok={res.ok} flash={res.flash_used}/{res.flash_total} artifacts={list(res.artifacts)}")
    return res


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def framework_roots(framework: str = "") -> List[Path]:
    """Installed SDK package roots; narrowed to the project's framework when one is given and installed."""
    pk = core_dir() / "packages"
    if not pk.is_dir():
        return []
    roots = sorted(pk.glob("framework-*"))
    fw = re.sub(r"[^a-z0-9]", "", (framework or "").lower())
    if fw:
        mine = [r for r in roots if re.sub(r"[^a-z0-9]", "", r.name.lower()).startswith("framework" + fw)]
        if mine:
            return mine
    return roots
