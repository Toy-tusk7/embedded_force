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


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PLATFORMIO_CORE_DIR"] = str(core_dir())
    env["PLATFORMIO_DISABLE_PROGRESSBAR"] = "true"
    env["PLATFORMIO_NO_ANSI"] = "true"
    env["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] = "no"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _pio_cmd(args: List[str]) -> List[str]:
    return [sys.executable, "-m", "platformio", *args]


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
    lines: List[str] = []
    try:
        proc = subprocess.Popen(
            _pio_cmd(["run", "-d", str(project_dir)]), cwd=str(project_dir), env=_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as e:
        return BuildResult(ok=False, env=env_name, error=f"Could not launch PlatformIO: {e}")
    assert proc.stdout is not None
    deadline = t0 + timeout_s
    for raw in proc.stdout:
        line = raw.rstrip("\r\n")
        lines.append(line)
        if on_line:
            on_line(line)
        if time.time() > deadline:
            proc.kill()
            lines.append("Build timed out.")
            break
    proc.wait()
    res = BuildResult(ok=proc.returncode == 0, env=env_name, duration_s=round(time.time() - t0, 1))
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
            hex_path = bdir / "firmware.hex"
            if make_hex(Path(res.artifacts["elf"]), hex_path):
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


def framework_roots() -> List[Path]:
    pk = core_dir() / "packages"
    return sorted(pk.glob("framework-*")) if pk.is_dir() else []
