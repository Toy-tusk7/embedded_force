"""Which provider and model every agent run used, read from each project's trace.jsonl (every run records its provider,
model, sampling and harness version at run_start, and its outcome at run_end).

  python -m embedforge.runlog              # per provider and model: runs, dates, verified outcomes
  python -m embedforge.runlog --runs       # one line per run
"""

import collections
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from embedforge.core.config import config


def runs(projects_dir: Path) -> List[Dict[str, Any]]:
    out = []
    for tr in sorted(Path(projects_dir).glob("*/trace.jsonl")):
        events = []
        for line in tr.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        for i, e in enumerate(events):
            if e.get("event") != "run_start":
                continue
            end = next((x for x in events[i + 1:] if x.get("event") == "run_end"), {})
            out.append({"t": e.get("t", 0), "project": tr.parent.name, "provider": e.get("provider") or "?",
                        "model": e.get("model") or "?", "harness": e.get("harness", ""), "status": end.get("status"),
                        "steps": end.get("steps"), "built": end.get("build_ok"), "verified": end.get("verify_ok")})
    return sorted(out, key=lambda r: r["t"])


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    rs = runs(Path(config.workspace_root) / "workspace" / "projects")
    day = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    if "--runs" in sys.argv:
        for r in rs:
            print(f"{datetime.datetime.fromtimestamp(r['t']):%Y-%m-%d %H:%M}  {r['provider']:10} {r['model']:42} "
                  f"{str(r['status']):12} steps={r['steps']} built={r['built']} verified={r['verified']}  {r['project']}")
        return
    groups: Dict[tuple, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rs:
        groups[(r["provider"], r["model"])].append(r)
    print(f"{len(rs)} agent runs")
    for (p, m), g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ok = sum(1 for r in g if r["status"] == "completed" and r["built"] and r["verified"])
        print(f"  {p:10} {m:42} {len(g):4} runs  {day(g[0]['t'])} .. {day(g[-1]['t'])}  completed with build + verify: {ok}")


if __name__ == "__main__":
    main()
