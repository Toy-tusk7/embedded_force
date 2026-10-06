"""NVIDIA Nemotron client connecting to Nebius Token Factory / AI Cloud."""

import json
from typing import Dict, Any, List, Optional
import httpx
from embedforge.core.config import config
from embedforge.core.logger import logger

class NemotronClient:
    """Client for NVIDIA Nemotron-3-Nano-30B-A3B hosted on Nebius Token Factory."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or config.nebius_api_key
        self.base_url = (base_url or config.nebius_base_url).rstrip('/')
        self.model = model or config.nebius_model

    def complete(self, messages: List[Dict[str, str]], temperature: float = 0.2, max_tokens: int = 2048) -> Dict[str, Any]:
        """Sends a completion request to Nebius Token Factory endpoint."""
        if not self.api_key:
            logger.log_event("NEMOTRON", "Nebius API key not set. Using deterministic fallback generator.")
            return {
                "content": "Deterministic fallback output",
                "reasoning": "Offline mode / No API key configured."
            }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        try:
            with httpx.Client(timeout=45.0) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload
                )
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                content = message.get("content", "") or ""
                reasoning = message.get("reasoning", "") or ""
                return {
                    "content": content.strip(),
                    "reasoning": reasoning.strip(),
                    "usage": data.get("usage", {})
                }
        except Exception as e:
            logger.log_event("NEMOTRON_ERROR", f"Nebius API call failed: {e}")
            raise RuntimeError(f"Nemotron inference failure on Nebius Token Factory: {e}") from e

    def reason_and_structure(self, task_instruction: str, context: Dict[str, Any], schema_desc: str) -> Dict[str, Any]:
        """Prompts Nemotron to perform engineering reasoning and produce strict JSON."""
        system_prompt = (
            "You are an expert embedded systems AI architect and reasoning engine within EmbedForge.\n"
            "Analyze the verified specifications, data contracts, and requirements.\n"
            "Output your reasoning followed by a strictly valid JSON object matching the requested schema.\n"
            f"Schema format:\n{schema_desc}"
        )
        user_prompt = f"Task: {task_instruction}\n\nContext:\n{json.dumps(context, indent=2)}\n\nRespond with valid JSON."
        
        res = self.complete(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1
        )
        content = res["content"]
        # Extract JSON from markdown fences if present
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()

        try:
            parsed = json.loads(content)
            return {"parsed": parsed, "reasoning": res["reasoning"]}
        except json.JSONDecodeError:
            # Fallback if Nemotron wrapped with introductory text
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                parsed = json.loads(content[start:end+1])
                return {"parsed": parsed, "reasoning": res["reasoning"]}
            raise ValueError(f"Could not parse JSON from Nemotron output:\n{content}")
