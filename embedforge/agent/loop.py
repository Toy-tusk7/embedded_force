"""The EmbedForge agent loop: LLM decides, deterministic tools act, truth layer gates the result."""

import json
import time
from typing import Any, Dict, List, Optional

from embedforge.agent.events import RunSession
from embedforge.agent.llm import AgentLLM, LLMError, parse_tool_args
from embedforge.agent.prompts import SYSTEM_PROMPT, get_active_prompt_text
from embedforge.agent.tools import TOOL_SPECS, ToolBelt
from embedforge.agent.workspace import Project
from embedforge.core.config import config
from embedforge.core.logger import logger
from embedforge.core.telemetry import tracer
from embedforge.truth import FactLedger, SourceStore

MAX_CONTEXT_CHARS = 45_000
KEEP_RECENT = 6


def _summ(result: Any, limit: int = 500) -> str:
    s = json.dumps(result, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _compact(messages: List[Dict[str, Any]]) -> None:
    """Shrink old messages safely while preserving turn integrity and valid tool-call pairs."""
    if len(messages) > 20:
        # Walk back to find a clean cut point that is not in the middle of tool responses
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
                        m["content"] = json.dumps(data)
                    else:
                        m["content"] = c[:300] + " …[truncated]"
                except Exception:
                    m["content"] = c[:300] + " …[truncated]"


class AgentRunner:
    def __init__(self, project: Project, session: RunSession, model: Optional[str] = None):
        self.project = project
        self.session = session
        self.llm = AgentLLM(model)

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

    def _walkthrough(self, belt: ToolBelt, store: SourceStore, ledger: FactLedger) -> str:
        meta = self.project.meta()
        builds = self.project.builds()
        lines = [f"# Walkthrough — {meta.get('name')}", "",
                 f"- **Target:** {meta.get('board')} ({meta.get('mcu')}), framework `{meta.get('framework')}`", ""]
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

    # ------------------------------------------------------------------ run
    def run(self, user_message: str) -> None:
        s = self.session
        project = self.project
        store = SourceStore(project.root)
        ledger = FactLedger(project.root, store)
        belt = ToolBelt(project, s, store, ledger)
        span = f"agent-{s.run_id}"
        tracer.start_span(span, "EmbedForge Agent Run", run_type="chain", inputs={"user_message": user_message, "project": project.id})
        status = "failed"
        messages: List[Dict[str, Any]] = []
        try:
            messages = project.load_conversation()
            is_first = not messages
            req_ctx = self._requirement_context(user_message) if is_first else ""
            messages.append({"role": "user", "content": user_message + req_ctx})
            store.add_user_text("\n".join(m["content"].split("\n\n[Requirement Engine")[0] for m in messages if m["role"] == "user"))
            if is_first and not project.meta().get("title_set"):
                project.update_meta(name=user_message.strip().split("\n")[0][:48] or project.id, title_set=True)
                s.emit("project", meta=project.meta())

            s.emit("status", text="Agent started", model=self.llm.model)
            consecutive_searches = 0
            for step in range(config.agent_max_steps):
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
                               "tool_calls": [tc["function"]["name"] for tc in tool_calls]})
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
                    # Pause cleanly and wait for the real user.
                    status = "needs_input"
                    break

                for tc in tool_calls:
                    name = tc["function"]["name"]
                    if name in ("web_search", "fetch_document"):
                        consecutive_searches += 1
                    elif name not in ("read_source", "search_framework", "read_framework_file", "list_facts", "list_files", "read_file"):
                        consecutive_searches = 0

                    if name in ("web_search", "fetch_document") and consecutive_searches > 5:
                        result = {
                            "error": (
                                "Web search limit reached (5 consecutive queries). Standard MCUs (STM32, AT32, GD32, ESP32, RP2040) "
                                "use standard vendor CMSIS/HAL definitions (e.g. TIM1/TMR1 complementary PWM on PA8..PA10/PA7..PB1). "
                                "Discrete peripherals (gate drivers like FD6288/IR2101) have NO register map — drive them with complementary PWM and GPIO. "
                                "DO NOT ask the user for architectural trivia. Call init_project, write_file, and build now."
                            )
                        }
                    else:
                        try:
                            args = parse_tool_args(tc["function"].get("arguments"))
                            s.emit("tool_call", id=tc["id"], name=name, args={k: (v if len(str(v)) < 400 else str(v)[:400] + "…") for k, v in args.items()})
                            tracer.start_span(f"{span}-{tc['id']}", name, run_type="tool", inputs=args)
                            result = belt.call(name, args)
                            if isinstance(result, dict) and consecutive_searches >= 3:
                                result["_guidance"] = (
                                    "Notice: Discrete ICs (e.g. FD6288) have no register map. Standard MCU timers (TIM1/TMR1) have standard pinouts "
                                    "(PA8..PA10 CH1..3, PA7/PB0/PB1 CH1N..3N). Do NOT search endlessly; proceed to init_project and write_file."
                                )
                        except ValueError as e:
                            result = {"error": str(e)}
                            s.emit("tool_call", id=tc["id"], name=name, args={})
                    tracer.end_span(f"{span}-{tc['id']}", outputs={"summary": _summ(result, 300)})
                    ok = not (isinstance(result, dict) and result.get("error"))
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
            else:
                s.emit("error", message=f"Step budget ({config.agent_max_steps}) exhausted before completion. Send a message to continue.")
                status = "failed"
        except Exception as e:  # never let the worker thread die silently
            logger.log_event("AGENT_CRASH", f"{type(e).__name__}: {e}")
            s.emit("error", message=f"{type(e).__name__}: {e}")
            status = "failed"
        finally:
            try:
                project.save_conversation(messages)  # type: ignore[possibly-undefined]
            except Exception:
                pass
            tracer.end_span(span, outputs={"status": status})
            s.emit("state", ledger=[f.public() for f in ledger.facts.values()], builds=project.builds(), files=project.list_files())
            s.finish(status)
