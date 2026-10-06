"""Headless runner:  python -m embedforge.agent "I'm using STM32F401 with an MPU6050 ..." [--project ID]"""

import argparse
import sys
import threading

from embedforge.agent import AgentRunner, ProjectManager, RunSession


def main() -> int:
    ap = argparse.ArgumentParser(prog="embedforge.agent")
    ap.add_argument("prompt")
    ap.add_argument("--project", help="Continue an existing project id")
    ap.add_argument("--quiet-build", action="store_true")
    a = ap.parse_args()

    project = ProjectManager.open(a.project) if a.project else ProjectManager.create()
    session = RunSession("cli", project.id)
    t = threading.Thread(target=AgentRunner(project, session).run, args=(a.prompt,), daemon=True)
    t.start()
    print(f"[project {project.id}]  {project.root}")
    idx = 0
    while True:
        evs = session.read_from(idx, timeout=5)
        if evs is None:
            break
        for e in evs:
            idx = e["i"] + 1
            ty = e["type"]
            if ty == "build_log":
                if not a.quiet_build:
                    print("   |", e["line"])
            elif ty == "message":
                print("\n🤖", e["text"])
            elif ty == "thinking":
                print("   💭", e["text"][:300].replace("\n", " "))
            elif ty == "tool_call":
                print(f"→ {e['name']}({', '.join(f'{k}={str(v)[:60]!r}' for k, v in e['args'].items())})")
            elif ty == "tool_result":
                print(f"  {'✓' if e['ok'] else '✗'} {e['summary'][:400]}")
            elif ty in ("ask_user", "done", "error", "verify", "status"):
                print(f"[{ty}]", {k: v for k, v in e.items() if k not in ("i", "t", "type", "report")})
    t.join()
    print("\nFINAL STATUS:", session.status)
    return 0 if session.status in ("completed", "needs_input") else 1


if __name__ == "__main__":
    sys.exit(main())
