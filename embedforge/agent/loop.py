"""The EmbedForge agent loop: LLM decides, deterministic tools act, truth layer gates the result."""

import hashlib
import json
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from embedforge.agent import context as ctx
from embedforge.agent.context import visible_user_text  # noqa: F401  (re-exported; server.py imports it from here)
from embedforge.agent.events import RunSession
from embedforge.agent.llm import AgentLLM, LLMError, parse_tool_args
from embedforge.agent.prompts import get_active_prompt_text
from embedforge.agent.tools import ToolBelt, tools_for
from embedforge.agent.workspace import Project
from embedforge.core.config import config
from embedforge.core.logger import logger
from embedforge.core.telemetry import tracer
from embedforge.truth import FactLedger, SourceStore

# Tools whose success means the run moved forward (resets the research / SDK budgets).
PROGRESS_TOOLS = {"record_fact", "record_user_fact", "record_board_pins", "init_project", "init_betaflight", "query_board",
                  "write_file", "replace_in_file", "rename_file", "build", "verify", "record_facts", "record_sequence", "record_registers",
                  "record_layouts", "record_formula", "record_constraint", "resolve_requirements"}
RESEARCH_TOOLS = {"web_search", "fetch_document", "find_reference_code", "find_documents"}
SDK_TOOLS = {"search_framework", "read_framework_file"}
MAX_RESEARCH_WITHOUT_PROGRESS = 6
MAX_SDK_WITHOUT_PROGRESS = 10
MAX_CONSECUTIVE_FAILURES = 3   # the same tool failing in a row (e.g. resolve_target for an unsupported MCU)
MAX_IDENTICAL_READS = 2        # the same read-only call with identical arguments while its result is still visible
READ_ONLY_TOOLS = {"read_file", "list_files", "read_framework_file", "search_framework", "query_board", "list_facts",
                   "read_source", "read_section", "datasheet_outline", "coding_brief", "load_skill", "read_figure", "init_betaflight", "init_project", "resolve_target",
                   "set_plan"}
ONCE_PROGRESS_TOOLS = {"query_board", "init_betaflight", "init_project"}  # progress only the first time per arguments
# failing builds/verifies between edits are the normal fix loop; finish and the record tools have their own truth gates
NO_FAILURE_STREAK_TOOLS = {"build", "verify", "finish", "record_fact", "record_user_fact", "record_board_pins", "record_facts",
                           "record_sequence", "record_registers", "record_layouts", "record_formula", "record_constraint", "resolve_requirements"}
MAX_UNBUILT_CODE_NUDGES = 2
MAX_TRUNCATION_RETRIES = 2
MAX_CONTINUE_NUDGES = 2
STALL_NUDGE_STEPS = 14   # LLM steps without progress before the agent is told to change course
STALL_STOP_STEPS = 28    # ... before the run pauses and hands control back to the user
STEP_EXTENSION = 30      # steps added when the budget runs out while the run is still making progress (or can finish)
MAX_STEP_EXTENSIONS = 3
STEPS_LEFT_WARNING = 5   # tell the model when this many steps remain and no extension will follow
_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_BUILD_CMD = re.compile(r"\b(cmake|ninja|make\s+\w|pio\s+run|platformio\s+run|dfu-util|picotool|openocd|st-flash|esptool)\b", re.I)


def unbuilt_code_problem(content: str, built: bool) -> Optional[str]:
    """Firmware or build/flash steps presented in chat without a successful build of the project sources."""
    if built or not content:
        return None
    blocks = _CODE_BLOCK.findall(content)
    code_lines = sum(b.count("\n") for b in blocks)
    if code_lines >= 15:
        return (f"Your reply contains {code_lines} lines of code in chat, but nothing was written or built. Code in chat is not "
                "delivered firmware: write it to project files with write_file, then build and verify. If you cannot build "
                "it with this project's toolchain, say so plainly instead of presenting it as finished.")
    if any(_BUILD_CMD.search(b) for b in blocks):
        return ("Your reply gives build/flash commands for firmware that was never built by the build tool. Do not claim or "
                "imply a build exists: build it here, or tell the user exactly what is not done and why.")
    return None


def _summ(result: Any, limit: int = 500) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _no_op(result: Any) -> bool:
    """A 'successful' call that changed nothing: a duplicate fact, or a write that left the file identical."""
    if not isinstance(result, dict):
        return False
    if any("Duplicate" in str(w) for w in result.get("warnings") or []):
        return True
    if result.get("unchanged") or (isinstance(result.get("recorded"), list) and result.get("all_duplicates")):
        return True
    return False


def _harness_version() -> str:
    """git HEAD of the harness (+ '-dirty'), so every trace says which code produced it."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "embedforge"], cwd=root, capture_output=True, text=True, timeout=5).stdout.strip()
        return head + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def notice(text: str) -> Dict[str, Any]:
    """A harness message to the model: shown while relevant, never persisted, never a 'user statement'."""
    return {"role": "user", "content": f"[System Notice]: {text}", "_ephemeral": True}


class ProgressGuard:
    """Stops unproductive loops (endless searching / SDK grepping) with tool-agnostic budgets.

    Budgets reset whenever a progress tool succeeds, so a run that keeps recording facts or editing
    code is never blocked; a run that only searches is told exactly what to do instead.
    """

    def __init__(self) -> None:
        self.research = 0
        self.sdk = 0
        self.steps_since_progress = 0
        self.step = 0
        self.calls: Counter = Counter()
        self.failures: Counter = Counter()
        self.blocked: Counter = Counter()
        self.fail_name = ""
        self.fail_streak = 0
        self.code_nudges = 0
        self.seen_queries: set = set()
        self.reads_since_change: List[tuple] = []   # (key, step, digest of its result)

    def before(self, name: str, n_sources: int, args: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        key = name + json.dumps(args or {}, sort_keys=True)
        if name in READ_ONLY_TOOLS:
            # identical reads since the last project change whose result is still verbatim in the context
            recent = [r for r in self.reads_since_change if r[0] == key and self.step - r[1] < ctx.VERBATIM_TURNS]
            if len(recent) >= MAX_IDENTICAL_READS:
                return {"error": (f"You already made this exact {name} call {len(recent)} times since the project last changed; "
                                  f"its result is in your context above ({recent[-1][2]}). Act on it (record facts, edit, "
                                  "build) or read something else."),
                        "guard": "identical_call"}
        else:
            self.reads_since_change.clear()
        lookup = name in READ_ONLY_TOOLS and name != "resolve_target"  # misses with new arguments are cheap information
        if name == self.fail_name and self.fail_streak >= MAX_CONSECUTIVE_FAILURES and name not in NO_FAILURE_STREAK_TOOLS and not lookup:
            if name != "resolve_target":
                self.fail_streak = MAX_CONSECUTIVE_FAILURES - 1  # the block expires: the next call may try once more
            return {"error": (
                f"{name} has failed {self.fail_streak} times in a row. Retrying with variations will not help. "
                + ("This MCU/board is not in the PlatformIO registry, so this project's toolchain cannot build for it — tell "
                   "the user plainly (finish with status='blocked' or ask_user) instead of searching again."
                   if name == "resolve_target" else
                   "Change approach, or explain the blocker to the user with ask_user / finish(status='blocked').")
            ), "guard": "repeated_failure"}
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

    def after(self, name: str, ok: bool, result: Any = None, args: Optional[Dict[str, Any]] = None,
              counted: bool = True, digest: str = "") -> None:
        """Record a call. Guard blocks and unparseable arguments are not counted as tool failures (counted=False)."""
        self.calls[name] += 1
        if not counted:
            self.blocked[name] += 1
            return
        if name in READ_ONLY_TOOLS:
            self.reads_since_change.append((name + json.dumps(args or {}, sort_keys=True), self.step, digest[:160]))
        failed = not ok or (isinstance(result, dict) and result.get("found") is False)
        if failed:
            self.failures[name] += 1
            self.fail_streak = self.fail_streak + 1 if name == self.fail_name else 1
            self.fail_name = name
        elif name == self.fail_name:
            self.fail_name, self.fail_streak = "", 0
        if name in ONCE_PROGRESS_TOOLS:  # lookups / setup count as progress only while they do something new
            key = name + json.dumps(args or {}, sort_keys=True).lower()
            fresh = ok and key not in self.seen_queries
            self.seen_queries.add(key)
            if not fresh:
                return
        if name in PROGRESS_TOOLS and ok and not _no_op(result):
            self.research = self.sdk = self.steps_since_progress = 0
            self.fail_name, self.fail_streak = "", 0
        elif name in RESEARCH_TOOLS:
            self.research += 1
        elif name in SDK_TOOLS and not failed:   # a wrong path taught nothing about the SDK; it is not a lookup spent
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

    def _announce_board(self) -> None:
        """Tell the UI about an imported schematic once (and again whenever it changes). The model learns about it from
        the PROJECT STATE block, so nothing is added to the conversation."""
        bp = self.project.root / "board.json"
        if not bp.exists():
            return
        digest = hashlib.sha256(bp.read_bytes()).hexdigest()[:16]
        if self.project.meta().get("board_seen") == digest:
            return
        from embedforge import eda
        board = eda.load_board(self.project.root)
        if board is None:
            return
        self.project.update_meta(board_seen=digest)
        self.session.emit("board", summary=board.summary())

    def _user_statements(self, history: List[Dict[str, Any]], user_message: str) -> List[str]:
        """The user's own messages (append-only log; seeded once from an older saved conversation)."""
        seed = [visible_user_text(str(m.get("content") or "")) for m in history if m.get("role") == "user" and not ctx.is_notice(m)]
        prior = self.project.user_messages(seed=seed)
        self.project.append_user_message(user_message)
        return prior + [user_message]

    def _mutation_report(self, belt: ToolBelt) -> None:
        """Verification strength on the delivered code: plant faults one at a time and count those verify catches
        (embedforge/truth/mutate.py). Only for sources that built and verified; written to artifacts/mutation_report.json."""
        h = belt.source_hash()
        if not config.mutation_test or not (belt.state["build_ok"] and belt.state["build_hash"] == h
                                            and belt.state["verify_ok"] and belt.state["verify_hash"] == h):
            return
        from embedforge.agent.events import RunSession
        from embedforge.agent.workspace import Project
        from embedforge.truth import mutate
        root = self.project.root

        def verify(copy) -> List[str]:
            p = Project(self.project.id, copy)
            st = SourceStore(copy)
            r = ToolBelt(p, RunSession("mutate", self.project.id), st, FactLedger(copy, st)).verify()
            return [f"{f['code']} {f.get('file')}:{f.get('line') or ''}" for f in r.get("findings", []) if f.get("severity") == "ERROR"]
        self.session.emit("status", text="Measuring verification strength: planting faults in the delivered code…")
        try:
            res = mutate.run(root, verify, write_funcs=mutate.write_functions(root))
        except Exception as e:   # the measurement must never cost the delivery
            self.session.emit("status", text=f"Mutation test could not run: {type(e).__name__}: {e}")
            return
        (root / "artifacts").mkdir(exist_ok=True)
        (root / "artifacts" / "mutation_report.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        self.project.trace({"event": "mutation_test", **{k: res[k] for k in ("total", "caught", "score", "by_operator")}})
        self.session.emit("status", text=f"Verification strength: {res['caught']}/{res['total']} planted faults caught")

    def _walkthrough(self, belt: ToolBelt, store: SourceStore, ledger: FactLedger, fin: Optional[Dict[str, Any]] = None) -> str:
        meta = self.project.meta()
        builds = self.project.builds()
        lines = [f"# Walkthrough — {meta.get('name')}", "",
                 f"- **Target:** {meta.get('board')} ({meta.get('mcu')}), framework `{meta.get('framework')}`",
                 f"- **Model:** `{self.llm.model}` via {self.llm.provider.name}", ""]
        lines += ["## Facts used (each with verbatim quote & source)", "",
                  "| ID | Subject | Property | Value | Tier | Source | Found by |", "|---|---|---|---|---|---|---|"]
        for f in ledger.facts.values():
            src = store.get(f.source_id)
            found = (f"{src.via}" + (f" `{src.tavily_request[:12]}`" if src.tavily_request else "")) if src and src.via else "—"
            lines.append(f"| {f.id} | {f.subject} | {f.property} | `{f.value}` | {f.tier} | {f.source_url} | {found} |")
        usage = store.tavily_usage()
        if usage["calls"]:
            web_facts = sum(1 for f in ledger.facts.values() if str(f.source_url).startswith("http"))
            lines += ["", "## Research (Tavily)", "",
                      "- Calls: " + ", ".join(f"{op} ×{n}" for op, n in sorted(usage["calls"].items())),
                      f"- Credits: {usage['credits']:g}" + (f" ({usage['credits'] / web_facts:.2f} per datasheet fact)" if web_facts else "")]
        lines += ["", "## Builds", ""]
        for b in builds:
            hx = b.get("files", {}).get("hex", "—")
            lines.append(f"- v{b['version']}: flash {b.get('flash_used')}/{b.get('flash_total')} B, RAM {b.get('ram_used')}/{b.get('ram_total')} B, "
                         f"hex `{hx}` sha256 `{(b.get('sha256') or {}).get('hex', '—')[:16]}…`, verified: {b.get('verified')}")
        from embedforge.truth.knowledge import Requirements
        rq = Requirements(self.project.root)
        if rq.items:
            c = rq.counts()
            lines += ["", "## Datasheet requirements", "",
                      f"{len(rq.items)} rule sentences from the datasheet sections this code depends on: {c['covered']} covered by "
                      f"quoted records (checked by verify), {c['implemented']} implemented (the agent's claim, not machine-checked), "
                      f"{c['not_applicable']} not applicable, {c['open']} open."]
            lines += [f"- {r.id} ({r.status}{': ' + r.reason if r.reason else ''}) {r.text[:140]}"
                      for r in rq.items.values() if r.status in ("implemented", "not_applicable", "open")][:30]
        mr = self.project.root / "artifacts" / "mutation_report.json"
        if mr.exists():
            try:
                m = json.loads(mr.read_text(encoding="utf-8"))
            except ValueError:
                m = {}
            if m.get("total"):
                lines += ["", "## Verification strength (mutation test)", "",
                          f"{m['caught']} of {m['total']} faults planted in this code one at a time were caught by verify "
                          f"({m['score']:.0%}). By kind: " + ", ".join(f"{k} {v}" for k, v in m.get("by_operator", {}).items()) + "."]
                if m.get("survivors"):
                    lines += ["", "Not caught (review each: a gap in what is on record, or a change that does not matter):", ""]
                    lines += [f"- `{s['file']}:{s['line']}` {s['what']}: `{s['before'].strip()}` → `{s['after'].strip() or '(deleted)'}`"
                              for s in m["survivors"][:12]]
        lines += ["", "## Files", ""] + [f"- `{f['path']}` ({f['bytes']} B)" for f in self.project.list_files()]
        h = belt.source_hash()
        built = bool(belt.state["build_ok"] and belt.state["build_hash"] == h)
        verified = bool(belt.state["verify_ok"] and belt.state["verify_hash"] == h)
        if (fin or {}).get("status") == "success" and built and verified:
            boundary = ("This firmware was compiled and statically verified against the recorded facts. It has **not** been "
                        "flashed or tested on hardware by EmbedForge. Flash it yourself and report any symptom to start a debug session.")
        else:
            boundary = (f"**Not delivered.** The run ended with status `{(fin or {}).get('status', 'unknown')}`. Current sources: "
                        f"{'built' if built else 'NOT built'}, {'verified' if verified else 'NOT verified'}. Nothing here has been "
                        "flashed or tested on hardware.")
        lines += ["", "## Hardware boundary", "", boundary]
        text = "\n".join(lines)
        (self.project.root / "artifacts" / "walkthrough.md").write_text(text, encoding="utf-8")
        self.session.emit("artifact", name="walkthrough.md", markdown=text)
        return text

    # ------------------------------------------------------------------ run
    def run(self, user_message: str) -> None:
        s = self.session
        project = self.project
        store = SourceStore(project.root)
        ledger = FactLedger(project.root, store)
        belt = ToolBelt(project, s, store, ledger)
        if config.claim_review:   # a separate call with its own context: a second opinion on the agent's claims
            belt.reviewer = lambda messages: self.llm.chat(messages, None, retries=3)["content"]
        guard = ProgressGuard()
        span = f"agent-{s.run_id}"
        tracer.start_span(span, "EmbedForge Agent Run", run_type="chain", inputs={"user_message": user_message, "project": project.id})
        status = "failed"
        steps = 0
        history: List[Dict[str, Any]] = []
        nxt = ""
        truncations = continue_nudges = 0
        consecutive_cuts = 0
        try:
            history = [m for m in project.load_conversation() if not ctx.is_notice(m)]  # older saves may still hold notices
            is_first = not history
            project.trace({"event": "run_start", "model": self.llm.model, "provider": getattr(self.llm.provider, "id", ""),
                           "sampling": getattr(self.llm, "sampling", lambda: {})(),
                           "template": getattr(self.llm, "template_kwargs", lambda: {})(), "max_steps": config.agent_max_steps,
                           "kind": project.meta().get("kind"), "harness": _harness_version()})
            req_ctx = self._requirement_context(user_message) if is_first else ""
            store.add_user_text("\n".join(self._user_statements(history, user_message)))
            history.append({"role": "user", "content": user_message + req_ctx})
            self._announce_board()
            if is_first and not project.meta().get("title_set"):
                project.update_meta(name=user_message.strip().split("\n")[0][:48] or project.id, title_set=True)
                s.emit("project", meta=project.meta())

            s.emit("status", text="Agent started", model=self.llm.model, provider=self.llm.provider.id)
            budget, extensions, step = config.agent_max_steps, 0, -1
            # context size; shortened for the rest of the run if the provider keeps failing (some hosted endpoints answer
            # a long request with HTTP 500 instead of rejecting it)
            ctx_budget, verbatim, shortened = ctx.CONTEXT_BUDGET_CHARS, ctx.VERBATIM_TURNS, False

            def can_extend() -> bool:   # still moving (a recent fact, edit or build), or one call from done
                return extensions < MAX_STEP_EXTENSIONS and (guard.steps_since_progress < STALL_NUDGE_STEPS or nxt.startswith("finish"))

            while True:
                step += 1
                if step >= budget:
                    if not can_extend():
                        first = ((project.meta().get("last_build") or {}).get("diagnostics") or [""])[0]
                        s.emit("error", message=f"Step budget ({budget}) used up. Next step: {nxt}"
                                                + (f" (last build error: {first})" if first else "") + ". Send 'continue' to resume.")
                        status = "needs_input"
                        break
                    extensions += 1
                    budget += STEP_EXTENSION
                    s.emit("status", text=f"Step budget extended to {budget}: the run is still making progress")
                    project.trace({"event": "budget_extended", "budget": budget})
                if budget - step == STEPS_LEFT_WARNING and not (extensions < MAX_STEP_EXTENSIONS and guard.steps_since_progress < STALL_NUDGE_STEPS - STEPS_LEFT_WARNING):
                    history.append(notice(f"{STEPS_LEFT_WARNING} steps left in this run. If the current sources built and "
                                          f"verified, call finish now; otherwise make the most important fix. NEXT STEP: {nxt}"))
                    project.trace({"event": "notice", "kind": "steps_left"})
                steps = step + 1
                guard.step = step
                if s.cancel_requested:
                    status = "cancelled"
                    s.emit("status", text="Cancelled by user")
                    break
                meta = project.meta()
                state_text, nxt = ctx.state_block(project, ledger, belt, history)
                messages = [{"role": "system", "content": get_active_prompt_text(meta.get("kind") or "") + "\n\n" + state_text}]
                messages += ctx.build_context(history, ctx_budget, verbatim)
                ctx_chars = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or [])) for m in messages)
                t0 = time.time()
                try:
                    try:
                        resp = self.llm.chat(messages, tools_for(meta))
                    except LLMError as e:
                        if not e.transient or shortened:
                            raise
                        shortened = True
                        ctx_budget = int((ctx_chars - len(messages[0]["content"])) * 0.6)
                        verbatim = max(3, verbatim // 2)
                        s.emit("status", text=f"The model provider kept failing ({str(e)[:120]}) — retrying with a shorter context")
                        project.trace({"event": "context_shortened", "error": str(e)[:300], "ctx_chars": ctx_chars,
                                       "budget": ctx_budget, "verbatim_turns": verbatim})
                        messages = messages[:1] + ctx.build_context(history, ctx_budget, verbatim)
                        resp = self.llm.chat(messages, tools_for(meta), retries=2)
                except LLMError as e:
                    if e.transient:   # the provider's side: nothing is lost, the next message resumes the run
                        s.emit("error", message=f"The model provider ({self.llm.provider.name}) is not answering: {e}. "
                                                f"Everything so far is saved; send 'continue' to resume. Next step: {nxt}")
                        status = "needs_input"
                    else:
                        s.emit("error", message=str(e))
                        status = "failed"
                    break
                dt = round(time.time() - t0, 1)
                if resp["reasoning"]:
                    s.emit("thinking", text=resp["reasoning"][:3000])
                tool_calls = resp["tool_calls"]
                if not tool_calls and ("{" in (resp["content"] or "") or "<tool_call>" in (resp["content"] or "")):
                    # calls written as text (the provider did not parse them): run them as real calls
                    from embedforge.agent.textcalls import parse_text_tool_calls
                    parsed, rest = parse_text_tool_calls(resp["content"], [t["function"]["name"] for t in tools_for(meta)])
                    if parsed:
                        tool_calls, resp = parsed, {**resp, "content": rest}
                truncated = resp.get("finish_reason") == "length" and not tool_calls
                amsg: Dict[str, Any] = {"role": "assistant", "content": resp["content"] or ""}
                if tool_calls:
                    amsg["tool_calls"] = tool_calls
                trace: Dict[str, Any] = {"step": step, "llm_seconds": dt, "usage": resp["usage"], "content": resp["content"][:2000],
                                         "model": self.llm.model, "finish_reason": resp.get("finish_reason"), "ctx_chars": ctx_chars,
                                         "ctx_messages": len(messages), "next_step": nxt, "reasoning_chars": len(resp["reasoning"] or ""),
                                         "tool_calls": [tc["function"]["name"] for tc in tool_calls]}

                if truncated:
                    # Cut off at max_tokens (usually mid-reasoning): a partial answer is not an answer, never show or keep it.
                    truncations += 1
                    consecutive_cuts += 1
                    project.trace({**trace, "event": "truncated"})
                    if consecutive_cuts > MAX_TRUNCATION_RETRIES or truncations > 3 * MAX_TRUNCATION_RETRIES:
                        s.emit("error", message=f"The model's reply was cut off at the token limit {consecutive_cuts} times in a "
                                                f"row. Next step was: {nxt}. Send 'continue' to retry.")
                        status = "needs_input"
                        break
                    s.emit("thinking", text="[Reply cut off at the token limit — asking the agent to act directly]")
                    history.append(notice(f"Your reply was cut off at the token limit. Think briefly and call the next tool "
                                          f"now. NEXT STEP: {nxt}"))
                    continue
                consecutive_cuts = 0
                if tool_calls and resp.get("finish_reason") == "length":  # cut off while writing a call: run none of the broken ones
                    for tc in tool_calls:
                        try:
                            json.loads(tc["function"].get("arguments") or "{}")
                        except ValueError:
                            tc["_cut_off"] = True
                history.append({**amsg, "tool_calls": [{k: v for k, v in tc.items() if k != "_cut_off"} for tc in tool_calls]}
                               if tool_calls else amsg)
                if resp["content"]:
                    s.emit("message", text=resp["content"], final=not tool_calls)

                if not tool_calls:
                    project.trace(trace)
                    # Before handing back to the user, make sure the reply is not presenting unbuilt firmware
                    # (code in chat / build+flash steps) as if it were delivered.
                    built = belt.state["build_ok"] and belt.state["build_hash"] == belt.source_hash()
                    problem = unbuilt_code_problem(resp["content"], built)
                    if problem and guard.code_nudges < MAX_UNBUILT_CODE_NUDGES:
                        guard.code_nudges += 1
                        s.emit("guard", kind="unbuilt_code", text="The reply above contains firmware that was not written to files or built — asking the agent to fix that.")
                        history.append(notice(problem))
                        continue
                    if problem:
                        s.emit("guard", kind="unbuilt_code", text="Warning: the reply above presents code or build steps that EmbedForge did not build or verify.")
                    # A task in progress whose next step needs no user input: one reminder before stopping.
                    kind = meta.get("kind") or ("platformio" if (project.root / "platformio.ini").exists() else "")
                    asked_question = user_message.rstrip().endswith("?")  # the user asked something; an answer is the result
                    # before setup only when the request itself named the target (next step resolve_target / init_betaflight)
                    task_named = bool(kind) or nxt.startswith(("resolve_target", "init_betaflight"))
                    in_progress = task_named and not asked_question and not nxt.startswith(("finish", "set up the target"))
                    if in_progress and continue_nudges < MAX_CONTINUE_NUDGES and not problem:
                        continue_nudges += 1
                        s.emit("guard", kind="continue", text=f"The task is not finished — reminding the agent of the next step: {nxt}")
                        history.append(notice(
                            f"The task is not finished. NEXT STEP: {nxt}. Continue with tools. If you need information that "
                            "is not in the schematic or the stored sources, call ask_user with the specific question."))
                        project.trace({"event": "notice", "kind": "continue"})
                        continue
                    status = "needs_input"
                    break

                results_trace = []
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    counted = True
                    try:
                        if tc.get("_cut_off"):
                            raise ValueError("Not executed: your reply was cut off at the token limit while writing this call. "
                                             "Re-issue it alone; for config edits use replace_in_file instead of rewriting the file.")
                        args = parse_tool_args(tc["function"].get("arguments"))
                    except ValueError as e:
                        args, result, counted = {}, {"error": str(e) if tc.get("_cut_off") else f"{e} Send the arguments as one JSON object."}, False
                    else:
                        result = guard.before(name, len(store.sources), args)
                        if result is not None:
                            counted = False
                    s.emit("tool_call", id=tc["id"], name=name, args={k: (v if len(str(v)) < 400 else str(v)[:400] + "…") for k, v in args.items()})
                    if result is None:
                        tracer.start_span(f"{span}-{tc['id']}", name, run_type="tool", inputs=args)
                        result = belt.call(name, args)
                        tracer.end_span(f"{span}-{tc['id']}", outputs={"summary": _summ(result, 300)})
                    elif result.get("guard"):
                        s.emit("guard", kind=result["guard"], text=result["error"])
                    ok = not (isinstance(result, dict) and (result.get("error") or result.get("ok") is False or result.get("found") is False))
                    dg = ctx.digest(name, args, result)
                    guard.after(name, ok, result, args, counted=counted, digest=dg)
                    s.emit("tool_result", id=tc["id"], name=name, ok=ok, summary=_summ(result))
                    history.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, default=str), "_digest": dg})
                    targs = {k: (v if len(str(v)) <= 200 else f"<{len(str(v))} chars>") for k, v in args.items()}
                    results_trace.append({"name": name, "args": targs, "ok": ok, "digest": dg[:300], **({} if counted else {"blocked": True})})
                    if belt.ended:
                        break
                project.trace({**trace, "results": results_trace})

                if belt.ended in ("ask_user", "finish"):  # the same unbuilt-firmware check as for plain replies
                    built = belt.state["build_ok"] and belt.state["build_hash"] == belt.source_hash()
                    text = belt.final.get("question") or belt.final.get("summary") or ""
                    problem = unbuilt_code_problem(text, built)
                    if problem and guard.code_nudges < MAX_UNBUILT_CODE_NUDGES:
                        guard.code_nudges += 1
                        s.emit("guard", kind="unbuilt_code", text="The agent's final text contains firmware that was not built — sending it back.")
                        belt.ended, belt.final = None, {}
                        history.append(notice(problem))
                        project.trace({"event": "notice", "kind": "unbuilt_code"})
                        continue
                if belt.ended == "ask_user":
                    s.emit("ask_user", **belt.final)
                    status = "needs_input"
                    break
                if belt.ended == "finish":
                    fin = belt.final
                    if fin.get("status") == "success":
                        self._mutation_report(belt)
                    self._walkthrough(belt, store, ledger, fin)
                    s.emit("done", status=fin["status"], summary=fin["summary"])
                    status = "completed" if fin["status"] == "success" else "failed"
                    break

                guard.steps_since_progress += 1
                if guard.steps_since_progress == STALL_NUDGE_STEPS:
                    text = (f"No progress in the last {STALL_NUDGE_STEPS} steps (no new fact, edit or build). "
                            f"NEXT STEP: {nxt}. Do that now, or ask_user for what is missing.")
                    s.emit("guard", kind="stall_nudge", text=text)
                    history.append(notice(text))
                    project.trace({"event": "notice", "kind": "stall_nudge"})
                elif guard.steps_since_progress >= STALL_STOP_STEPS:
                    text = (f"Paused after {STALL_STOP_STEPS} steps with no new fact, edit or build. Stuck at: {nxt}. "
                            "Reply 'continue' to resume, or give the missing detail.")
                    s.emit("guard", kind="stalled", text=text)
                    s.emit("error", message=text)
                    status = "needs_input"
                    break
        except Exception as e:  # never let the worker thread die silently
            logger.log_event("AGENT_CRASH", f"{type(e).__name__}: {e}")
            s.emit("error", message=f"{type(e).__name__}: {e}")
            status = "failed"
        finally:
            try:
                project.save_conversation(ctx.for_persistence(history))
            except Exception:
                pass
            tracer.end_span(span, outputs={"status": status})
            stats = dict(steps=steps, tools=dict(guard.calls), failures=dict(guard.failures), blocked=dict(guard.blocked),
                         facts=len(ledger.facts), sources=len(store.sources), builds=len(project.builds()),
                         build_ok=belt.state["build_ok"], verify_ok=belt.state["verify_ok"], model=self.llm.model,
                         status=status, next_step=nxt, truncations=truncations, continue_nudges=continue_nudges,
                         tavily=store.tavily_usage())
            try:
                project.trace({"event": "run_end", **stats})
            except Exception:
                pass
            s.emit("run_stats", **stats)
            s.emit("state", ledger=[f.public() for f in ledger.facts.values()], builds=project.builds(), files=project.list_files())
            s.finish(status)
