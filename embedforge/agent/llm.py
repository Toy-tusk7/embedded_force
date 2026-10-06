"""Tool-calling LLM client for Nebius Token Factory (OpenAI-compatible) used by the agent loop."""

import json
import time
from typing import Any, Dict, List, Optional

import httpx

from embedforge.core.config import config
from embedforge.core.logger import logger


class LLMError(RuntimeError):
    pass


class AgentLLM:
    def __init__(self, model: Optional[str] = None):
        self.model = model or config.agent_model
        self.base_url = config.nebius_base_url.rstrip("/")
        self.api_key = config.nebius_api_key

    def chat(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1, max_tokens: int = 8192, retries: int = 4,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise LLMError("NEBIUS_API_KEY is not configured; the agent cannot reason without an LLM.")
        payload: Dict[str, Any] = {
            "model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
        }
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
                if r.status_code >= 400:
                    raise LLMError(f"LLM request failed: HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                msg = data["choices"][0]["message"]
                return {
                    "content": (msg.get("content") or "").strip(),
                    "reasoning": (msg.get("reasoning_content") or msg.get("reasoning") or "").strip(),
                    "tool_calls": msg.get("tool_calls") or [],
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
