"""Scratch probe: does the configured Nebius model support native tool calling? (no secrets printed)"""
import json, time, httpx
from embedforge.core.config import config

print("base_url:", config.nebius_base_url, "| model:", config.nebius_model, "| key set:", config.has_nebius)
h = {"Authorization": f"Bearer {config.nebius_api_key}", "Content-Type": "application/json"}

# 1. list models (to see what is actually available)
try:
    r = httpx.get(f"{config.nebius_base_url.rstrip('/')}/models", headers=h, timeout=30)
    print("models status:", r.status_code)
    if r.status_code == 200:
        ids = sorted(m["id"] for m in r.json().get("data", []))
        print("count:", len(ids))
        for i in ids:
            if any(k in i.lower() for k in ("nemotron", "qwen", "llama-3.3", "deepseek", "gpt-oss", "glm", "kimi")):
                print("  ", i)
except Exception as e:
    print("models err:", e)

# 2. tool-calling probe
tools = [{"type": "function", "function": {
    "name": "search_web", "description": "Search the web",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]
payload = {"model": config.nebius_model, "temperature": 0.1, "max_tokens": 512, "tools": tools,
           "messages": [{"role": "user", "content": "Find the I2C address of the MPU6050 using the tool."}]}
t = time.time()
try:
    r = httpx.post(f"{config.nebius_base_url.rstrip('/')}/chat/completions", headers=h, json=payload, timeout=90)
    print("chat status:", r.status_code, f"{time.time()-t:.2f}s")
    d = r.json()
    msg = d["choices"][0]["message"] if "choices" in d else d
    print(json.dumps({k: msg.get(k) for k in ("content", "tool_calls")} if "choices" in d else d, indent=1)[:1500])
except Exception as e:
    print("chat err:", e)
