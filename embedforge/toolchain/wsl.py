"""Run Linux build steps through WSL (used for firmware projects with their own make-based build, e.g. Betaflight).

Commands run as the default WSL user inside the configured distro, never with sudo. Output is streamed
line by line, and a watchdog kills the command if it exceeds its timeout even when it prints nothing.
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import PureWindowsPath
from typing import Callable, List, Optional, Tuple

from embedforge.toolchain.pio import _run_streaming

LineSink = Optional[Callable[[str], None]]
DISTRO = os.getenv("EMBEDFORGE_WSL_DISTRO", "Ubuntu")


def wsl_exe() -> Optional[str]:
    return shutil.which("wsl.exe") or shutil.which("wsl")


def available() -> Tuple[bool, str]:
    """(ok, detail): whether the configured distro exists and can run bash."""
    exe = wsl_exe()
    if not exe:
        return False, "WSL is not installed (wsl.exe not found)."
    try:
        r = subprocess.run([exe, "-d", DISTRO, "-e", "bash", "-c", "echo ok"], capture_output=True, timeout=120)
    except Exception as e:
        return False, f"WSL did not start: {e}"
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode != 0 or out != "ok":
        err = (r.stderr or r.stdout).decode("utf-16-le" if b"\x00" in (r.stderr or r.stdout) else "utf-8", "replace")
        return False, f"WSL distro '{DISTRO}' is not usable: {err.strip()[:300]}"
    return True, DISTRO


def to_wsl_path(win_path: str) -> str:
    """C:\\Users\\x\\proj -> /mnt/c/Users/x/proj"""
    p = PureWindowsPath(win_path)
    drive = p.drive.rstrip(":").lower()
    rest = "/".join(p.parts[1:])
    return f"/mnt/{drive}/{rest}" if drive else str(win_path).replace("\\", "/")


def run(script: str, on_line: LineSink = None, timeout_s: int = 3600) -> Tuple[int, List[str], bool]:
    """Run a bash script in the distro. Returns (exit code, output lines, timed_out)."""
    exe = wsl_exe()
    if not exe:
        return -1, ["WSL is not installed."], False
    # -e runs bash directly; '--' would first pass the command line through the default shell, expanding $vars early
    cmd = [exe, "-d", DISTRO, "-e", "bash", "-lc", script]
    return _run_streaming(cmd, None, on_line, timeout_s)


def q(s: str) -> str:
    """Shell-quote a value for the bash script."""
    return shlex.quote(str(s))


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def safe_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name or ""))
