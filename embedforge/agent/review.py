"""An independent reviewer (a subagent: a separate model call with its own context) for the agent's own claims about
datasheet requirements. resolve_requirements lets the agent say "implemented" or "not applicable"; a claim is not
evidence, and the BMP388 rehearsal showed why: the agent called the rule 'settings first, then the mode' implemented
in code that sets the mode first, and called the measurement-time limit 'informational'. The reviewer sees the
requirement, the claim and the code, and agrees or disagrees; verify fails on a disagreement until the code is fixed or
the claim is settled with a record (record_constraint / record_fact / record_sequence) that verify can check."""

import json
import re
from typing import Any, Callable, Dict, List

REVIEW_SYSTEM = (
    "You are an independent reviewer of embedded firmware. For each datasheet requirement you get the author's claim "
    "about it and you see the code. Decide whether the claim is right.\n"
    "- 'implemented' is right only if the code does what the requirement says, in the order and with the timing it says.\n"
    "- 'not_applicable' is right only if the requirement concerns something the code does not use (another mode, an "
    "interrupt the code never enables, the FIFO, a board-level or electrical matter). A rule that limits values or "
    "timing the code does set (a conversion time against the sampling period, a recommended setting for the use "
    "case, waiting for data to be ready before reading) applies.\n"
    "- A requirement that only says what the device does by itself (it delays a command until a measurement ends, "
    "ignores a command, clears a flag after a read) asks nothing of the code but not to work against it: 'implemented' "
    "is right when the code does nothing that relies on the opposite (e.g. it does not send a second mode change and "
    "expect the first to take effect at once). Do not ask the code to show or re-implement the device's behaviour.\n"
    "Answer with JSON only: a list of {\"id\": \"REQ-nnn\", \"verdict\": \"agree\" | \"disagree\", "
    "\"where\": \"file:line or ''\", \"why\": \"one sentence\"}. Disagree only for a concrete reason you can point to.")


def _code_text(files: Dict[str, str], limit: int = 24000) -> str:
    out, used = [], 0
    for rel, text in files.items():
        numbered = "\n".join(f"{i:4}: {ln}" for i, ln in enumerate(text.splitlines(), start=1))
        chunk = f"=== {rel} ===\n{numbered}\n"
        if used + len(chunk) > limit:
            chunk = chunk[:max(0, limit - used)] + "\n[... truncated]\n"
        out.append(chunk)
        used += len(chunk)
        if used >= limit:
            break
    return "".join(out)


def _parse(content: str) -> List[Dict[str, Any]]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("id")]


def review_claims(ask: Callable[[List[Dict[str, Any]]], str], claims: List[Any], files: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Ask the reviewer about claims (Requirement objects with status implemented / not_applicable). Returns
    id -> {verdict, where, why}; a reply that cannot be read leaves the claims unreviewed (no verdict)."""
    if not claims:
        return {}
    listed = "\n".join(f"- {r.id} [{r.section}] \"{r.text}\" — author: {r.status}: {r.reason}" for r in claims)
    messages = [{"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": f"CODE\n{_code_text(files)}\nCLAIMS\n{listed}\n\nJSON list, one entry per claim:"}]
    out: Dict[str, Dict[str, str]] = {}
    for d in _parse(ask(messages)):
        rid = re.sub(r"^\s*REQ[-_ ]?0*(\d+)\s*$", lambda m: f"REQ-{int(m.group(1)):03d}", str(d.get("id")), flags=re.I)
        verdict = str(d.get("verdict", "")).strip().lower()
        if verdict in ("agree", "disagree"):
            out[rid] = {"verdict": verdict, "where": str(d.get("where") or "")[:80], "why": str(d.get("why") or "")[:300]}
    return out
