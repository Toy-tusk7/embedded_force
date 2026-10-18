"""Which provider and model every agent run used, and what it cost, read from each project's trace.jsonl (every run
records its provider, model, sampling and harness version at run_start, the token usage and model time of each step,
and its outcome at run_end).

  python -m embedforge.runlog              # per provider and model: runs, steps, tokens, model time, verified outcomes
  python -m embedforge.runlog --markdown   # the same as a Markdown table (README)
  python -m embedforge.runlog --runs       # one line per run
"""

import collections
import datetime
import json
import statistics
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
        starts = [i for i, e in enumerate(events) if e.get("event") == "run_start"] + [len(events)]
        for i, nxt in zip(starts, starts[1:]):
            e, body = events[i], events[i + 1:nxt]   # a run's events end where the next run starts
            end = next((x for x in body if x.get("event") == "run_end"), {})
            steps = [x for x in body if "step" in x and "usage" in x]
            usage = [x.get("usage") or {} for x in steps]
            out.append({"t": e.get("t", 0), "project": tr.parent.name, "provider": e.get("provider") or "?",
                        "model": e.get("model") or "?", "harness": e.get("harness", ""),
                        "status": end.get("status") or "interrupted", "steps": end.get("steps", len(steps)),
                        "built": end.get("build_ok"), "verified": end.get("verify_ok"),
                        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usage),
                        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usage),
                        "model_seconds": sum(float(x.get("llm_seconds") or 0) for x in steps)})
    return sorted(out, key=lambda r: r["t"])


def by_model(rs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per provider and model: runs, the runs that ended built and verified, steps, tokens and model time."""
    groups: Dict[tuple, List[Dict[str, Any]]] = collections.defaultdict(list)
    for r in rs:
        groups[(r["provider"], r["model"])].append(r)
    out = []
    for (p, m), g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        steps = [int(r["steps"] or 0) for r in g]
        n_steps = max(1, sum(steps))
        out.append({"provider": p, "model": m, "runs": len(g), "first": g[0]["t"], "last": g[-1]["t"],
                    "verified": sum(1 for r in g if r["built"] and r["verified"]),
                    "steps": sum(steps), "median_steps": statistics.median(steps) if steps else 0,
                    "prompt_tokens": sum(r["prompt_tokens"] for r in g), "completion_tokens": sum(r["completion_tokens"] for r in g),
                    "seconds_per_step": sum(r["model_seconds"] for r in g) / n_steps,
                    "prompt_per_step": sum(r["prompt_tokens"] for r in g) / n_steps})
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    rs = runs(Path(config.workspace_root) / "workspace" / "projects")
    day = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d")
    if "--runs" in sys.argv:
        for r in rs:
            print(f"{datetime.datetime.fromtimestamp(r['t']):%Y-%m-%d %H:%M}  {r['provider']:10} {r['model']:42} "
                  f"{str(r['status']):12} steps={r['steps']} built={r['built']} verified={r['verified']} "
                  f"tokens={r['prompt_tokens']}+{r['completion_tokens']}  {r['project']}")
        return
    rows = by_model(rs)
    if "--markdown" in sys.argv:
        print("| Provider | Model | Runs | Ended built + verified | Steps (median per run) | Prompt + completion tokens "
              "| Model time per step | Dates |")
        print("|---|---|---|---|---|---|---|---|")
        for g in rows:
            print(f"| {g['provider']} | {g['model']} | {g['runs']} | {g['verified']} | {g['steps']} ({g['median_steps']:g}) | "
                  f"{g['prompt_tokens'] / 1e6:.1f}M + {g['completion_tokens'] / 1e3:.0f}k | {g['seconds_per_step']:.0f} s | "
                  f"{day(g['first'])} to {day(g['last'])} |")
        return
    print(f"{len(rs)} agent runs")
    for g in rows:
        print(f"  {g['provider']:10} {g['model']:42} {g['runs']:4} runs  {day(g['first'])} .. {day(g['last'])}  "
              f"built + verified: {g['verified']}  steps: {g['steps']} (median {g['median_steps']:g})  "
              f"tokens: {g['prompt_tokens'] / 1e6:.1f}M + {g['completion_tokens'] / 1e3:.0f}k  "
              f"{g['seconds_per_step']:.0f} s/step  {g['prompt_per_step'] / 1e3:.0f}k prompt/step")


if __name__ == "__main__":
    main()
