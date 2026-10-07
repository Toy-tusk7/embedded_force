"""Tool-calling LLM client for OpenAI-compatible providers (Nebius Token Factory, NVIDIA NIM) used by the agent loop."""

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
        "nebius": Provider("nebius", "Nebius Token Factory", config.nebius_base_url.rstrip("/"), config.nebius_api_key),
        "nvidia": Provider("nvidia", "NVIDIA API (NIM)", config.nvidia_base_url.rstrip("/"), config.nvidia_api_key),
    }


def default_provider() -> str:
    return "nebius" if config.has_nebius or not config.has_nvidia else "nvidia"


# ------------------------------------------------------------------ model catalogue
_MODEL_CACHE: Dict[str, Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE_TTL = 600
_NON_CHAT = re.compile(r"embed|rerank|guard|reward|clip|whisper|tts|parse|retriever|-v-|vision|ocr", re.I)


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
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.get(f"{p.base_url}/models", headers={"Authorization": f"Bearer {p.api_key}"})
        r.raise_for_status()
        ids = sorted({m.get("id") for m in r.json().get("data", []) if m.get("id")})
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
        pid = provider or default_provider()
        p = providers().get(pid)
        if p is None:
            raise LLMError(f"Unknown LLM provider '{pid}'.")
        self.provider = p
        self.model = model or config.agent_model
        self.base_url = p.base_url
        self.api_key = p.api_key

    def sampling(self) -> Dict[str, float]:
        """Nemotron reasoning models loop and repeat when run near-greedy. The Nemotron 3 model card samples at
        temperature 1.0 / top_p 0.95; older Nemotron reasoning models at 0.6 / 0.95. Other models keep a low temperature.
        EMBEDFORGE_TEMPERATURE / EMBEDFORGE_TOP_P override."""
        mid = self.model.lower()
        nemotron = "nemotron" in mid
        default_t = (1.0 if re.search(r"nemotron-?3", mid) else 0.6) if nemotron else 0.1
        out = {"temperature": config.agent_temperature if config.agent_temperature is not None else default_t}
        top_p = config.agent_top_p if config.agent_top_p is not None else (0.95 if nemotron else None)
        if top_p is not None:
            out["top_p"] = top_p
        return out

    def default_max_tokens(self) -> int:
        """Reasoning models spend thousands of tokens thinking before a config rewrite; give them room."""
        if getattr(self, "_max_tokens_cap", None):
            return self._max_tokens_cap
        env = os.getenv("EMBEDFORGE_MAX_TOKENS")
        if env and env.isdigit():
            return int(env)
        return 16384 if re.search(r"nemotron|reason|r1|qwq|thinking", self.model.lower()) else 8192

    def chat(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None, retries: int = 4,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise LLMError(f"{self.provider.name} API key is not configured; the agent cannot reason without an LLM.")
        payload: Dict[str, Any] = {"model": self.model, "messages": messages,
                                   "max_tokens": max_tokens or self.default_max_tokens(), **self.sampling()}
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
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
                    calls.append({"id": tc.get("id") or f"call_{int(time.time() * 1000)}_{i}", "type": "function",
                                  "function": {"name": fn.get("name") or "", "arguments": a if isinstance(a, str) else json.dumps(a or {})}})
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
