"""Crash-safe writes for a project's records. A server stopped mid-write (a closed terminal, a killed process) used to
leave a truncated file: a run's read log came back as 316 blank bytes, and the same could empty the fact ledger. The
text goes to a temporary file next to the target, is flushed to disk, and replaces the target in one step, so a
reader sees the old file or the new one, never half of one."""

import json
import os
from pathlib import Path
from typing import Any

# flush to the disk before the swap, so a power cut keeps the file too (about 10 ms a write on Windows; the test suite,
# which writes thousands of records, turns it off)
FSYNC = True


def write_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding=encoding) as f:   # newlines as Path.write_text writes them
        f.write(text)
        if FSYNC:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    """The parsed file, or default when there is none. A file that does not parse is moved aside to <name>.corrupt
    (the next save would otherwise overwrite what is left of it, silently) and default is returned."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        from embedforge.core.logger import logger
        logger.log_event("CORRUPT_FILE", f"{path} did not parse; kept as {path.name}.corrupt")
        return default
