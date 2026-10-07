"""The EmbedForge agent loop: LLM decides, deterministic tools act, truth layer gates the result."""

import hashlib
import json
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from embedforge.agent.events import RunSession
from embedforge.agent.llm import AgentLLM, LLMError, parse_tool_args
from embedforge.agent.prompts import get_active_prompt_text
from embedforge.agent.tools import TOOL_SPECS, ToolBelt
from embedforge.agent.workspace import Project
from embedforge.core.config import config
from embedforge.core.logger import logger
from embedforge.core.telemetry import tracer
from embedforge.truth import FactLedger, SourceStore

MAX_CONTEXT_CHARS = 45_000
KEEP_RECENT = 6

# Tools whose success means the run moved forward (resets the research / SDK budgets).
PROGRESS_TOOLS = {"record_fact", "record_user_fact", "init_project", "write_file", "replace_in_file", "build", "verify"}
RESEARCH_TOOLS = {"web_search", "fetch_document"}
SDK_TOOLS = {"search_framework", "read_framework_file"}
MAX_RESEARCH_WITHOUT_PROGRESS = 6
MAX_SDK_WITHOUT_PROGRESS = 10
STALL_NUDGE_STEPS = 14   # LLM steps without progress before the agent is told to change course
STALL_STOP_STEPS = 28    # ... before the run pauses and hands control back to the user


_CONTEXT_MARKERS = re.compile(r"\n\n\[(?:Requirement Engine|Board imported)")


def visible_user_text(content: str) -> str:
    """The user's own words, without the context blocks the runner appends for the model."""
    return _CONTEXT_MARKERS.split(content or "", maxsplit=1)[0]


def _summ(result: Any, limit: int = 500) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _compact(messages: List[Dict[str, Any]]) -> None:
    """Shrink old messages safely while preserving turn integrity and valid tool-call pairs."""
    if len(messages) > 20:
        # Walk forward to a clean cut point that is not in the middle of tool responses
        cut_idx = max(1, len(messages) - 16)
        while cut_idx < len(messages) and messages[cut_idx].get("role") == "tool":
            cut_idx += 1

        user_msgs = [m for m in messages[:cut_idx] if m.get("role") == "user"]
        recent = messages[cut_idx:]
        messages.clear()
        messages.extend(user_msgs + recent)

    total = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or [])) for m in messages)
    if total <= MAX_CONTEXT_CHARS:
        return
    for m in messages[: max(0, len(messages) - KEEP_RECENT)]:
        if m.get("role") == "tool":
            c = m.get("content") or ""
            if len(c) > 400:
                try:
                    data = json.loads(c)
                    if isinstance(data, dict):
                        if "excerpts" in data and len(data["excerpts"]) > 1:
                            data["excerpts"] = data["excerpts"][:1]
                        if "preview" in data and len(data["preview"]) > 200:
                            data["preview"] = data["preview"][:200] + "…"
                        if isinstance(data.get("results"), list):
                            for r in data["results"]:
                                if isinstance(r, dict) and len(r.get("excerpt", "")) > 200:
                                    r["excerpt"] = r["excerpt"][:200] + "…"
                        m["content"] = json.dumps(data)
                    else:
                        m["content"] = c[:300] + " …[truncated]"
                except Exception:
                    m["content"] = c[:300] + " …[truncated]"


class ProgressGuard:
    """Stops unproductive loops (endless searching / SDK grepping) with tool-agnostic budgets.

    Budgets reset whenever a progress tool succeeds, so a run that keeps recording facts or editing
    code is never blocked; a run that only searches is told exactly what to do instead.
    """

    def __init__(self) -> None:
        self.research = 0
        self.sdk = 0
        self.steps_since_progress = 0
        self.calls: Counter = Counter()
        self.failures: Counter = Counter()

    def before(self, name: str, n_sources: int) -> Optional[Dict[str, Any]]:
        if name in RESEARCH_TOOLS and self.research >= MAX_RESEARCH_WITHOUT_PROGRESS:
            return {"error": (
                f"Research budget reached: {self.research} searches/fetches since your last recorded fact or code change "
                f"({n_sources} sources already stored). Searching is blocked until you make progress. Next: read_source on "
                "the best datasheet you have and record_fact, record_user_fact for what the user stated, write the code with "
                "SDK symbols, or ask_user for the specific value you cannot find."
            ), "guard": "research_budget"}
        if name in SDK_TOOLS and self.sdk >= MAX_SDK_WITHOUT_PROGRESS:
            return {"error": (
                f"SDK search budget reached ({self.sdk} lookups without writing code). Write the firmware with the "
                "functions you have found, then build — compiler errors will tell you exactly which names are wrong."
            ), "guard": "sdk_budget"}
        return None

    def after(self, name: str, ok: bool) -> None:
        self.calls[name] += 1
        if not ok:
            self.failures[name] += 1
        if name in PROGRESS_TOOLS and ok:
            self.research = self.sdk = self.steps_since_progress = 0
        elif name in RESEARCH_TOOLS:
            self.research += 1
        elif name in SDK_TOOLS:
            self.sdk += 1


class AgentRunner:
    def __init__(self, project: Project, session: RunSession, model: Optional[str] = None, provider: Optional[str] = None):
        self.project = project
        self.session = session
        meta = project.meta()
        self.llm = AgentLLM(model or meta.get("model"), provider or meta.get("provider"))

    # ------------------------------------------------------------------ helpers
    def _requirement_context(self, text: str) -> str:
        """Run the deterministic Requirement Engine on the user's request (explicit facts + gaps only)."""
        try:
            from embedforge.engines.requirements import RequirementParser
            rs = RequirementParser().parse(text)
            reqs = [{"id": r.id, "category": r.category.value, "facts": r.explicit_facts} for r in rs.requirements]
            if not reqs:
                return ""
            gaps = [{"id": g.id, "type": g.gap_type, "subject": g.target_subject, "property": g.target_property,
                     "criticality": g.criticality} for g in rs.information_gaps]
            self.session.emit("requirements", requirements=reqs, gaps=gaps)
            return (
                "\n\n[Requirement Engine — explicit facts stated by user]:\n"
                f"explicit: {json.dumps(reqs)}\ngaps: {json.dumps(gaps)}"
            )
        except Exception as e:
            logger.log_event("REQ_ENGINE_WARN", str(e))
            return ""

    def _board_context(self) -> str:
        """Announce an imported schematic once (and again whenever it changes)."""
        bp = self.project.root / "board.json"
        if not bp.exists():
            return ""
        digest = hashlib.sha256(bp.read_bytes()).hexdigest()[:16]
        if self.project.meta().get("board_seen") == digest:
            return ""
        from embedforge import eda
        board = eda.load_board(self.project.root)
        if board is None:
            return ""
        s = board.summary()
        mcus = ", ".join(f"{m['ref']} {m['mpn'] or m['value']} ({m['pin_count']} pins)" for m in s["mcus"]) or "none identified"
        self.project.update_meta(board_seen=digest)
        self.session.emit("board", summary=s)
        return (f"\n\n[Board imported from {', '.join(i['file'] for i in s['imported'])}: {s['parts']} parts, {s['nets']} nets; "
                f"MCU: {mcus}. It is the ground truth for pins and wiring — use query_board.]")

    def _walkthrough(self, belt: ToolBelt, store: SourceStore, ledger: FactLedger) -> str:
        meta = self.project.meta()
        builds = self.project.builds()
        lines = [f"# Walkthrough — {meta.get('name')}", "",
                 f"- **Target:** {meta.get('board')} ({meta.get('mcu')}), framework `{meta.get('framework')}`",
                 f"- **Model:** `{self.llm.model}` via {self.llm.provider.name}", ""]
        lines += ["## Facts used (each with verbatim quote & source)", "", "| ID | Subject | Property | Value | Tier | Source |", "|---|---|---|---|---|---|"]
        for f in ledger.facts.values():
            lines.append(f"| {f.id} | {f.subject} | {f.property} | `{f.value}` | {f.tier} | {f.source_url} |")
        lines += ["", "## Builds", ""]
        for b in builds:
            hx = b.get("files", {}).get("hex", "—")
            lines.append(f"- v{b['version']}: flash {b.get('flash_used')}/{b.get('flash_total')} B, RAM {b.get('ram_used')}/{b.get('ram_total')} B, "
                         f"hex `{hx}` sha256 `{(b.get('sha256') or {}).get('hex', '—')[:16]}…`, verified: {b.get('verified')}")
        lines += ["", "## Files", ""] + [f"- `{f['path']}` ({f['bytes']} B)" for f in self.project.list_files()]
        lines += ["", "## Hardware boundary", "",
                  "This firmware was compiled and statically verified against datasheet facts. It has **not** been "
                  "flashed or tested on hardware by EmbedForge. Flash the `.hex` yourself and report any symptom to start a debug session."]
        text = "\n".join(lines)
        (self.project.root / "artifacts" / "walkthrough.md").write_text(text, encoding="utf-8")
        self.session.emit("artifact", name="walkthrough.md", markdown=text)
        return text

    def _state_digest(self, belt: ToolBelt, store: SourceStore, ledger: FactLedger) -> str:
        has_ini = (self.project.root / "platformio.ini").exists()
        srcs = [f["path"] for f in self.project.list_files() if f["path"].startswith(("src/", "include/"))]
        return (f"facts={len(ledger.facts)}, sources={len(store.sources)}, project_initialised={has_ini}, "
                f"source_files={len(srcs)}, last_build_ok={belt.state['build_ok']}, verify_ok={belt.state['verify_ok']}")

    # ------------------------------------------------------------------ run
    def run(self, user_message: str) -> None:
        s = self.session
        project = self.project
        store = SourceStore(project.root)
        ledger = FactLedger(project.root, store)
        belt = ToolBelt(project, s, store, ledger)
        guard = ProgressGuard()
        span = f"agent-{s.run_id}"
        tracer.start_span(span, "EmbedForge Agent Run", run_type="chain", inputs={"user_message": user_message, "project": project.id})
        status = "failed"
        steps = 0
        messages: List[Dict[str, Any]] = []
        try:
            messages = project.load_conversation()
            is_first = not messages
            req_ctx = self._requirement_context(user_message) if is_first else ""
            messages.append({"role": "user", "content": user_message + req_ctx + self._board_context()})
            store.add_user_text("\n".join(visible_user_text(m["content"]) for m in messages if m["role"] == "user"))
            if is_first and not project.meta().get("title_set"):
                project.update_meta(name=user_message.strip().split("\n")[0][:48] or project.id, title_set=True)
                s.emit("project", meta=project.meta())

            s.emit("status", text="Agent started", model=self.llm.model, provider=self.llm.provider.id)
            for step in range(config.agent_max_steps):
                steps = step + 1
                if s.cancel_requested:
                    status = "cancelled"
                    s.emit("status", text="Cancelled by user")
                    break
                _compact(messages)
                t0 = time.time()
                try:
                    resp = self.llm.chat([{"role": "system", "content": get_active_prompt_text()}] + messages, TOOL_SPECS)
                except LLMError as e:
                    s.emit("error", message=str(e))
                    status = "failed"
                    break
                dt = round(time.time() - t0, 1)
                if resp["reasoning"]:
                    s.emit("thinking", text=resp["reasoning"][:3000])
                tool_calls = resp["tool_calls"]
                amsg: Dict[str, Any] = {"role": "assistant", "content": resp["content"] or ""}
                if tool_calls:
                    amsg["tool_calls"] = tool_calls
                messages.append(amsg)
                project.trace({"step": step, "llm_seconds": dt, "usage": resp["usage"], "content": resp["content"][:2000],
                               "model": self.llm.model, "tool_calls": [tc["function"]["name"] for tc in tool_calls]})
                if resp["content"]:
                    s.emit("message", text=resp["content"], final=not tool_calls)

                if resp.get("finish_reason") == "length" and not resp["content"] and not tool_calls:
                    # Model hit max_tokens during reasoning without producing output. Prompt to execute directly.
                    s.emit("thinking", text="[Thinking token budget reached — recovering and executing next action directly]")
                    messages.append({
                        "role": "user",
                        "content": "[System Notice]: You reached the token limit during reasoning. Keep your thinking concise and immediately invoke the required tool or give your answer."
                    })
                    continue

                if not tool_calls:
                    # Model gave a conversational response or asked for input without calling tools.
                    status = "needs_input"
                    break

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = parse_tool_args(tc["function"].get("arguments"))
                    except ValueError as e:
                        args, result = {}, {"error": str(e)}
                    else:
                        result = guard.before(name, len(store.sources))
                    s.emit("tool_call", id=tc["id"], name=name, args={k: (v if len(str(v)) < 400 else str(v)[:400] + "…") for k, v in args.items()})
                    if result is None:
                        tracer.start_span(f"{span}-{tc['id']}", name, run_type="tool", inputs=args)
                        result = belt.call(name, args)
                        tracer.end_span(f"{span}-{tc['id']}", outputs={"summary": _summ(result, 300)})
                    elif result.get("guard"):
                        s.emit("guard", kind=result["guard"], text=result["error"])
                    ok = not (isinstance(result, dict) and (result.get("error") or result.get("ok") is False))
                    guard.after(name, ok)
                    s.emit("tool_result", id=tc["id"], name=name, ok=ok, summary=_summ(result))
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, default=str)})
                    if belt.ended:
                        break

                if belt.ended == "ask_user":
                    s.emit("ask_user", **belt.final)
                    status = "needs_input"
                    break
                if belt.ended == "finish":
                    fin = belt.final
                    self._walkthrough(belt, store, ledger)
                    s.emit("done", status=fin["status"], summary=fin["summary"])
                    status = "completed" if fin["status"] == "success" else "failed"
                    break

                guard.steps_since_progress += 1
                if guard.steps_since_progress == STALL_NUDGE_STEPS:
                    digest = self._state_digest(belt, store, ledger)
                    text = (f"No progress in the last {STALL_NUDGE_STEPS} steps ({digest}). Change approach now: record facts from "
                            "the sources you already have, write the code and build, or ask_user for what is missing.")
                    s.emit("guard", kind="stall_nudge", text=text)
                    messages.append({"role": "user", "content": f"[System Notice]: {text}"})
                elif guard.steps_since_progress >= STALL_STOP_STEPS:
                    digest = self._state_digest(belt, store, ledger)
                    text = (f"Paused: {STALL_STOP_STEPS} steps without recording a fact, editing code or building ({digest}). "
                            "Reply with the missing detail (e.g. a datasheet link or the value) or try a stronger model.")
                    s.emit("guard", kind="stalled", text=text)
                    s.emit("error", message=text)
                    status = "needs_input"
                    break
            else:
                s.emit("error", message=f"Step budget ({config.agent_max_steps}) exhausted before completion. Send a message to continue.")
                status = "failed"
        except Exception as e:  # never let the worker thread die silently
            logger.log_event("AGENT_CRASH", f"{type(e).__name__}: {e}")
            s.emit("error", message=f"{type(e).__name__}: {e}")
            status = "failed"
        finally:
            try:
                project.save_conversation(messages)
            except Exception:
                pass
            tracer.end_span(span, outputs={"status": status})
            s.emit("run_stats", steps=steps, tools=dict(guard.calls), failures=dict(guard.failures),
                   facts=len(ledger.facts), sources=len(store.sources), builds=len(project.builds()),
                   build_ok=belt.state["build_ok"], verify_ok=belt.state["verify_ok"], model=self.llm.model)
            s.emit("state", ledger=[f.public() for f in ledger.facts.values()], builds=project.builds(), files=project.list_files())
            s.finish(status)
