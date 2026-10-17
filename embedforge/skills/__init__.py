"""Skills: playbooks the agent loads on demand (the format of NVIDIA's agent skills: a markdown file with a name and a
one-line description in its front matter). The system prompt lists them; load_skill returns one."""

import re
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

SKILLS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _skills() -> Tuple[Tuple[str, str, str], ...]:
    out = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        m = re.match(r"---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
        if not m:
            continue
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M))
        out.append((meta.get("name", p.stem).strip(), meta.get("description", "").strip(), m.group(2).strip()))
    return tuple(out)


def list_skills() -> List[Tuple[str, str]]:
    return [(n, d) for n, d, _ in _skills()]


def load_skill(name: str) -> Optional[str]:
    key = str(name or "").strip().lower()
    return next((body for n, _, body in _skills() if n.lower() == key), None)
