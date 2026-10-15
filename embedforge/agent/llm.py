"""Tool-calling LLM client used by the agent loop: the Anthropic Claude API (Messages API, official SDK) and
OpenAI-compatible providers (Google Gemini, Nebius Token Factory, NVIDIA NIM). The loop speaks one message format
(OpenAI-style role/content/tool_calls); the Claude path translates it to and from the Messages API."""

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from embedforge.core.config import config
from embedforge.core.logger import logger


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str
    api_key: Optional[str]

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_key.strip())


def providers() -> Dict[str, Provider]:
    return {
        "anthropic": Provider("anthropic", "Anthropic Claude API", "https://api.anthropic.com", config.anthropic_api_key),
        "gemini": Provider("gemini", "Google Gemini API", config.gemini_base_url.rstrip("/"), config.gemini_api_key),
        "nebius": Provider("nebius", "Nebius Token Factory", config.nebius_base_url.rstrip("/"), config.nebius_api_key),
        "nvidia": Provider("nvidia", "NVIDIA API (NIM)", config.nvidia_base_url.rstrip("/"), config.nvidia_api_key),
    }


# Provider order when nothing is forced: NVIDIA Nemotron on Nebius Token Factory first, then Nemotron from the NVIDIA
# API catalog, then Claude, then Gemini (EMBEDFORGE_PROVIDER forces one)
PREFERRED = ("nebius", "nvidia", "anthropic", "gemini")


def default_provider() -> str:
    """EMBEDFORGE_PROVIDER if set; otherwise the first configured provider in PREFERRED (Nebius when none is)."""
    if config.llm_provider in providers():
        return config.llm_provider
    ps = providers()
    return next((pid for pid in PREFERRED if ps[pid].configured), "nebius")


def default_model(provider_id: str) -> str:
    return {"anthropic": config.anthropic_model, "gemini": config.gemini_model}.get(provider_id, config.agent_model)


def resolve_provider(requested: Optional[str]) -> str:
    """The provider a run uses: a forced EMBEDFORGE_PROVIDER wins; a project keeps the provider it saved while that
    provider is configured; otherwise the default."""
    if config.llm_provider in providers():
        return config.llm_provider
    p = providers().get(requested or "")
    return requested if p is not None and p.configured else default_provider()


# (base URL, model) pairs whose provider rejected Nemotron's chat_template_kwargs: later runs send none
_NO_TEMPLATE_KWARGS: set = set()

# ------------------------------------------------------------------ model catalogue
_MODEL_CACHE: Dict[str, Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE_TTL = 600
_NON_CHAT = re.compile(r"embed|rerank|guard|reward|clip|whisper|tts|parse|retriever|-v-|vision|ocr|"
                       r"image|veo|imagen|aqa|native-audio|-live|robotics", re.I)


def _describe(model_id: str) -> Dict[str, Any]:
    """Family + size label parsed from the model id (e.g. '30B · 3B active'); no claims beyond the id itself."""
    low = model_id.lower()
    family = model_id.split("/", 1)[0] if "/" in model_id else ""
    is_nvidia = family.lower() == "nvidia" or "nemotron" in low
    size = ""
    m = re.search(r"(\d+(?:\.\d+)?)b[-_]a(\d+(?:\.\d+)?)b", low)
    if m:
        size = f"{m.group(1).upper()}B · {m.group(2).upper()}B active"
    else:
        m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b(?![a-z])", low)
        if m:
            size = f"{m.group(1)}B"
    label = model_id.split("/", 1)[-1]
    return {"family": family, "nvidia": is_nvidia, "size": size, "label": label}


def list_models(provider_id: str, force: bool = False) -> Dict[str, Any]:
    """Chat models available from a provider's /models endpoint (cached). Never raises."""
    p = providers().get(provider_id)
    if p is None or not p.configured:
        return {"provider": provider_id, "models": [], "error": "not configured"}
    with _MODEL_CACHE_LOCK:
        hit = _MODEL_CACHE.get(provider_id)
        if hit and not force and time.time() - hit["t"] < _MODEL_CACHE_TTL:
            return hit["v"]
    if provider_id == "anthropic":
        try:
            import anthropic
            ids = sorted({m.id for m in anthropic.Anthropic(api_key=p.api_key).models.list()})
            out = {"provider": provider_id, "error": "",
                   "models": [{"id": i, "provider": provider_id, "family": "anthropic", "nvidia": False, "size": "", "label": i} for i in ids]}
        except Exception as e:
            out = {"provider": provider_id, "models": [], "error": f"{type(e).__name__}: {str(e)[:200]}"}
        with _MODEL_CACHE_LOCK:
            _MODEL_CACHE[provider_id] = {"t": time.time(), "v": out}
        return out
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.get(f"{p.base_url}/models", headers={"Authorization": f"Bearer {p.api_key}"})
        r.raise_for_status()
        # Gemini's native ids carry a 'models/' prefix; requests take the bare name
        ids = sorted({str(m.get("id")).removeprefix("models/") for m in r.json().get("data", []) if m.get("id")})
        models = [{"id": i, "provider": provider_id, **_describe(i)} for i in ids if not _NON_CHAT.search(i)]
        models.sort(key=lambda m: (not m["nvidia"], m["id"].lower()))
        out = {"provider": provider_id, "models": models, "error": ""}
    except Exception as e:  # offline / bad key: the UI still shows the configured default
        out = {"provider": provider_id, "models": [], "error": f"{type(e).__name__}: {str(e)[:200]}"}
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE[provider_id] = {"t": time.time(), "v": out}
    return out


class AgentLLM:
    def __init__(self, model: Optional[str] = None, provider: Optional[str] = None):
        pid = resolve_provider(provider)
        p = providers().get(pid)
        if p is None:
            raise LLMError(f"Unknown LLM provider '{pid}'.")
        self.provider = p
        # a project saved with another provider's model keeps working: the run uses this provider's default model
        if pid == "anthropic":
            self.model = model if model and model.startswith("claude-") else config.anthropic_model
        elif pid == "gemini":
            self.model = model if model and model.startswith("gemini") else config.gemini_model
        else:
            self.model = model if model and not model.startswith(("claude-", "gemini")) else config.agent_model
        self.base_url = p.base_url
        self.api_key = p.api_key

    def sampling(self) -> Dict[str, float]:
        """Nemotron reasoning models loop and repeat when run near-greedy. The Nemotron 3 model card samples at
        temperature 1.0 / top_p 0.95; older Nemotron reasoning models at 0.6 / 0.95. Other models keep a low temperature.
        EMBEDFORGE_TEMPERATURE / EMBEDFORGE_TOP_P override."""
        if self.provider.id == "anthropic":
            return {}   # Claude's current models take no sampling parameters (effort and adaptive thinking instead)
        if self.provider.id == "gemini" and config.agent_temperature is None and config.agent_top_p is None:
            return {}   # Gemini 3 is tuned for its default temperature (1.0); Google warns lower values can make it loop
        mid = self.model.lower()
        nemotron = "nemotron" in mid
        default_t = (1.0 if re.search(r"nemotron-?3", mid) else 0.6) if nemotron else 0.1
        out = {"temperature": config.agent_temperature if config.agent_temperature is not None else default_t}
        top_p = config.agent_top_p if config.agent_top_p is not None else (0.95 if nemotron else None)
        if top_p is not None:
            out["top_p"] = top_p
        return out

    def template_kwargs(self) -> Dict[str, Any]:
        """Nemotron 3 chat-template options, as NVIDIA's model card recommends for coding agents and tool calling:
        reasoning on, and a non-empty answer after the reasoning (so a reply is never only a thought);
        EMBEDFORGE_NEMOTRON_EFFORT=medium asks for markedly shorter reasoning."""
        if self.provider.id not in ("nebius", "nvidia") or (self.base_url, self.model) in _NO_TEMPLATE_KWARGS:
            return {}
        if not re.search(r"nemotron-?3", self.model.lower()):
            return {}
        out: Dict[str, Any] = {"enable_thinking": True, "force_nonempty_content": True}
        if os.getenv("EMBEDFORGE_NEMOTRON_EFFORT", "").strip().lower() == "medium":
            out["medium_effort"] = True
        return out

    def default_max_tokens(self) -> int:
        """Reasoning models spend thousands of tokens thinking before a config rewrite; give them room."""
        if getattr(self, "_max_tokens_cap", None):
            return self._max_tokens_cap
        env = os.getenv("EMBEDFORGE_MAX_TOKENS")
        if env and env.isdigit():
            return int(env)
        if self.provider.id == "gemini":
            return 32768   # Gemini 3 always thinks, and its thinking counts toward the output limit
        return 16384 if re.search(r"nemotron|reason|r1|qwq|thinking", self.model.lower()) else 8192

    def chat(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None, retries: int = 4,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise LLMError(f"{self.provider.name} API key is not configured; the agent cannot reason without an LLM.")
        if self.provider.id == "anthropic":
            return self._chat_anthropic(messages, tools, max_tokens)
        payload: Dict[str, Any] = {"model": self.model, "messages": messages,
                                   "max_tokens": max_tokens or self.default_max_tokens(), **self.sampling()}
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.provider.id == "gemini":   # Gemini takes minimal | low | medium | high
            effort = config.anthropic_effort.lower()
            payload["reasoning_effort"] = effort if effort in ("minimal", "low", "medium") else "high"
        template = self.template_kwargs()
        if template:
            payload["chat_template_kwargs"] = template
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last: Optional[str] = None
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=180.0) as c:
                    r = c.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep(min(2 ** attempt * 1.5, 20))
                    continue
                if r.status_code == 400 and "max_tokens" in r.text and payload["max_tokens"] > 8192:
                    self._max_tokens_cap = payload["max_tokens"] = 8192  # provider limit: remember it for this model
                    continue
                if r.status_code in (400, 422) and "chat_template_kwargs" in payload and re.search(
                        r"chat_template|template_kwargs|force_nonempty|medium_effort|extra (inputs|fields)|unrecognized|not permitted", r.text, re.I):
                    _NO_TEMPLATE_KWARGS.add((self.base_url, self.model))   # this provider does not take them: run without
                    payload.pop("chat_template_kwargs")
                    continue
                if r.status_code >= 400:
                    raise LLMError(f"LLM request failed ({self.model}): HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
                if "</think>" in content:  # some served models put their reasoning inline instead of a separate field
                    head, _, content = content.rpartition("</think>")
                    reasoning = (reasoning + "\n" + head.replace("<think>", "")).strip()
                    content = content.strip()
                elif content.startswith("<think>"):  # cut off mid-thought: all of it is reasoning, none of it an answer
                    reasoning, content = (reasoning + "\n" + content[len("<think>"):]).strip(), ""
                calls = []
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    a = fn.get("arguments")
                    call = {"id": tc.get("id") or f"call_{int(time.time() * 1000)}_{i}", "type": "function",
                            "function": {"name": fn.get("name") or "", "arguments": a if isinstance(a, str) else json.dumps(a or {})}}
                    if tc.get("extra_content"):   # Gemini 3's thought signature: it must come back with the call
                        call["extra_content"] = tc["extra_content"]
                    calls.append(call)
                return {
                    "content": content,
                    "reasoning": reasoning,
                    "tool_calls": calls,
                    "finish_reason": data["choices"][0].get("finish_reason"),
                    "usage": data.get("usage", {}),
                }
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = str(e)
                time.sleep(min(2 ** attempt * 1.5, 20))
        logger.log_event("LLM_ERROR", f"gave up after {retries} attempts: {last}")
        raise LLMError(f"LLM unavailable after {retries} attempts: {last}")


    # ------------------------------------------------------------------ Claude (Messages API)
    _STATE_MARK = "PROJECT STATE"

    def _chat_anthropic(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]],
                        max_tokens: Optional[int]) -> Dict[str, Any]:
        """One agent step on Claude. The harness rebuilds its context every step (a fresh state block, digests of old
        tool results), so the history is not append-only: thinking blocks are never replayed (preserved thinking would
        reject an edited history), the stable system prompt and the tools are prompt-cached, and the per-step state
        block follows the cache breakpoint."""
        import anthropic

        system, msgs = to_anthropic_messages(messages)
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or int(os.getenv("EMBEDFORGE_MAX_TOKENS") or 32000),
            "system": system,
            "messages": msgs,
            "thinking": {"type": "adaptive", "display": "summarized"},   # summaries feed the UI's reasoning panel
            "output_config": {"effort": config.anthropic_effort},
            # on a safety decline the API re-runs the request on a fallback model it picks by refusal category
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        if tools:
            kwargs["tools"] = to_anthropic_tools(tools)
        client = anthropic.Anthropic(api_key=self.api_key, max_retries=4)
        try:
            with client.beta.messages.stream(**kwargs) as stream:   # streaming: long turns never hit HTTP timeouts
                msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError(f"Claude API key rejected: {e.message}")
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"Claude API permission denied: {e.message}")
        except anthropic.BadRequestError as e:
            raise LLMError(f"Claude API rejected the request ({self.model}): {e.message}")
        except anthropic.RateLimitError as e:
            raise LLMError(f"Claude API rate limit (after retries): {e.message}")
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error HTTP {e.status_code}: {e.message}")
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Claude API unreachable: {e}")
        return from_anthropic_message(msg)


def to_anthropic_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI function specs -> Claude tool definitions (cached together with the system prompt, which renders after them)."""
    out = []
    for t in tools:
        fn = t.get("function") or t
        schema = dict(fn.get("parameters") or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        out.append({"name": fn["name"], "description": fn.get("description", ""), "input_schema": schema})
    return out


def _tool_id(raw: Any) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(raw or "call"))[:64]


def to_anthropic_messages(messages: List[Dict[str, Any]]):
    """OpenAI-style history -> (system blocks, Claude messages). Tool calls become tool_use blocks, tool results are
    gathered into one user message, consecutive same-role turns are merged and empty turns dropped."""
    system_text = "\n\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
    cut = system_text.find(AgentLLM._STATE_MARK)
    stable, state = (system_text[:cut], system_text[cut:]) if cut > 0 else (system_text, "")
    system: List[Dict[str, Any]] = [{"type": "text", "text": stable.strip() or "You are EmbedForge.", "cache_control": {"type": "ephemeral"}}]
    if state.strip():
        system.append({"type": "text", "text": state.strip()})
    out: List[Dict[str, Any]] = []

    def push(role: str, blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    pending_tool_ids: set = set()
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            text = str(m.get("content") or "").strip()
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = parse_tool_args(fn.get("arguments"))
                except ValueError:
                    args = {}
                tid = _tool_id(tc.get("id"))
                pending_tool_ids.add(tid)
                blocks.append({"type": "tool_use", "id": tid, "name": fn.get("name") or "unknown", "input": args})
            push("assistant", blocks)
        elif role == "tool":
            tid = _tool_id(m.get("tool_call_id"))
            if tid not in pending_tool_ids:
                continue   # no matching tool_use in the history sent: the API would reject it
            content = str(m.get("content") or "")
            push("user", [{"type": "tool_result", "tool_use_id": tid, "content": content or "(empty)",
                           **({"is_error": True} if content.startswith('{"error"') else {})}])
        else:
            text = m.get("content")
            if isinstance(text, list):
                push("user", [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in text])
            elif str(text or "").strip():
                push("user", [{"type": "text", "text": str(text)}])
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue the task."}]})
    # every tool_use needs its tool_result in the next user turn: answer any that the history lost
    for i, m in enumerate(out):
        if m["role"] != "assistant":
            continue
        ids = [b["id"] for b in m["content"] if b["type"] == "tool_use"]
        nxt = out[i + 1] if i + 1 < len(out) else None
        have = {b.get("tool_use_id") for b in (nxt["content"] if nxt and nxt["role"] == "user" else [])}
        missing = [x for x in ids if x not in have]
        if missing:
            results = [{"type": "tool_result", "tool_use_id": x, "content": "(result not kept in context)"} for x in missing]
            if nxt and nxt["role"] == "user":
                nxt["content"][:0] = results
            else:
                out.insert(i + 1, {"role": "user", "content": results})
    return system, out


def from_anthropic_message(msg: Any) -> Dict[str, Any]:
    """A Claude message -> the loop's response shape (content, reasoning, tool_calls, finish_reason, usage)."""
    texts, thoughts, calls = [], [], []
    for b in msg.content:
        t = getattr(b, "type", "")
        if t == "text":
            texts.append(b.text)
        elif t == "thinking":
            if getattr(b, "thinking", ""):
                thoughts.append(b.thinking)
        elif t == "tool_use":
            calls.append({"id": b.id, "type": "function",
                          "function": {"name": b.name, "arguments": json.dumps(b.input if isinstance(b.input, dict) else {})}})
    stop = getattr(msg, "stop_reason", None)
    content = "\n".join(x for x in texts if x).strip()
    if stop == "max_tokens":
        calls = []   # a tool input cut off at the token limit parses as a partial object: never run it
    if stop == "refusal":
        details = getattr(msg, "stop_details", None)
        cat = getattr(details, "category", None) if details else None
        calls = []   # a declined turn's tool calls may be cut off mid-input: never run them
        content = (content + "\n\n" if content else "") + f"[The model declined this request{f' ({cat})' if cat else ''}.]"
    u = getattr(msg, "usage", None)
    cached = (getattr(u, "cache_read_input_tokens", 0) or 0) if u else 0
    created = (getattr(u, "cache_creation_input_tokens", 0) or 0) if u else 0
    usage = {"prompt_tokens": ((getattr(u, "input_tokens", 0) or 0) + cached + created) if u else 0,
             "completion_tokens": (getattr(u, "output_tokens", 0) or 0) if u else 0,
             "cache_read_input_tokens": cached, "cache_creation_input_tokens": created}
    finish = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop",
              "pause_turn": "stop", "refusal": "refusal"}.get(stop or "", stop)
    return {"content": content, "reasoning": "\n\n".join(thoughts).strip(), "tool_calls": calls,
            "finish_reason": finish, "usage": usage}


def parse_tool_args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError("Tool arguments were not valid JSON.")
