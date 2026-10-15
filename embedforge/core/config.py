"""Core configuration management for EmbedForge."""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Automatically load .env if present
load_dotenv()

class EmbedForgeConfig(BaseModel):
    """System-wide configuration settings."""
    nebius_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("NEBIUS_API_KEY")
    )
    nebius_base_url: str = Field(
        default_factory=lambda: os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
    )
    nebius_model: str = Field(
        default_factory=lambda: os.getenv("NEBIUS_MODEL", "nvidia/Nemotron-3-Ultra-550b-a55b")
    )
    tavily_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("TAVILY_API_KEY")
    )
    # advanced (2 credits): the most relevant chunks of each page, better on technical documents; basic: 1 credit
    tavily_search_depth: str = Field(default_factory=lambda: os.getenv("TAVILY_SEARCH_DEPTH", "advanced"))
    # Tavily searches and maps one run may make: past it the agent works from the sources it has
    tavily_max_calls: int = Field(default_factory=lambda: int(os.getenv("EMBEDFORGE_TAVILY_MAX_CALLS", "12")))
    agent_model: str = Field(
        # Ultra is the only Nemotron that produced correct firmware in our evals (Nano: 0 of 4)
        default_factory=lambda: os.getenv("NEBIUS_AGENT_MODEL") or os.getenv("NEBIUS_MODEL", "nvidia/Nemotron-3-Ultra-550b-a55b")
    )
    # Optional second provider: NVIDIA API catalog / NIM (OpenAI-compatible), https://build.nvidia.com
    nvidia_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("NVIDIA_API_KEY")
    )
    nvidia_base_url: str = Field(
        default_factory=lambda: os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    # the NVIDIA API names models its own way (Ultra's id from its build.nvidia.com example; the IDE's model picker lists
    # the rest, e.g. nvidia/nemotron-3-super-120b-a12b)
    nvidia_model: str = Field(default_factory=lambda: os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"))
    # A self-hosted OpenAI-compatible server (vLLM / SGLang on a Nebius AI Cloud GPU VM, Ollama / llama.cpp locally):
    # used first when its base URL is set. The key is only needed if the server was started with one.
    selfhosted_base_url: str = Field(default_factory=lambda: os.getenv("SELFHOSTED_BASE_URL", "").strip())
    selfhosted_model: str = Field(default_factory=lambda: os.getenv("SELFHOSTED_MODEL", "nemotron-3-super"))
    selfhosted_api_key: str = Field(default_factory=lambda: os.getenv("SELFHOSTED_API_KEY", ""))
    # Anthropic Claude API (the SDK also reads ANTHROPIC_API_KEY itself; ANTHROPIC_AUTH_TOKEN works too)
    anthropic_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
    )
    anthropic_model: str = Field(
        default_factory=lambda: os.getenv("EMBEDFORGE_ANTHROPIC_MODEL", "claude-opus-5-5")
    )
    anthropic_effort: str = Field(   # low | medium | high | xhigh | max (Claude Opus 5.5 defaults to medium)
        default_factory=lambda: os.getenv("EMBEDFORGE_EFFORT", "high")
    )
    # Google Gemini API through its OpenAI-compatible endpoint (keys from Google AI Studio)
    gemini_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    )
    gemini_base_url: str = Field(
        default_factory=lambda: os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    )
    gemini_model: str = Field(   # on the free tier; gemini-3.1-pro-preview is paid-only
        default_factory=lambda: os.getenv("EMBEDFORGE_GEMINI_MODEL", "gemini-3.8-flash")
    )
    # Force one provider for every run: selfhosted | nebius | nvidia | anthropic | gemini (empty: the first configured,
    # in that order)
    llm_provider: str = Field(
        default_factory=lambda: os.getenv("EMBEDFORGE_PROVIDER", "").strip().lower()
    )
    agent_max_steps: int = Field(
        default_factory=lambda: int(os.getenv("EMBEDFORGE_MAX_STEPS", "60"))
    )
    # Sampling for the agent LLM; empty = per-model default (reasoning models loop and repeat when run near-greedy)
    agent_temperature: Optional[float] = Field(
        default_factory=lambda: float(os.environ["EMBEDFORGE_TEMPERATURE"]) if os.getenv("EMBEDFORGE_TEMPERATURE") else None
    )
    agent_top_p: Optional[float] = Field(
        default_factory=lambda: float(os.environ["EMBEDFORGE_TOP_P"]) if os.getenv("EMBEDFORGE_TOP_P") else None
    )
    workspace_root: Path = Field(
        default_factory=lambda: Path(os.getenv("EMBEDFORGE_WORKSPACE", Path.cwd()))
    )
    # a hosted IDE asks for this password (HTTP Basic auth); required when EMBEDFORGE_HOST is not this machine
    server_password: str = Field(default_factory=lambda: os.getenv("EMBEDFORGE_PASSWORD", ""))
    temperature: float = 0.2
    max_tokens: int = 4096
    bounded_search_limit: int = 5

    @property
    def has_nebius(self) -> bool:
        return bool(self.nebius_api_key and len(self.nebius_api_key.strip()) > 0)

    @property
    def has_nvidia(self) -> bool:
        return bool(self.nvidia_api_key and self.nvidia_api_key.strip())

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.strip())

    @property
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key and self.gemini_api_key.strip())

    @property
    def has_selfhosted(self) -> bool:
        return bool(self.selfhosted_base_url)

    @property
    def has_llm(self) -> bool:
        return self.has_selfhosted or self.has_anthropic or self.has_gemini or self.has_nebius or self.has_nvidia

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key and len(self.tavily_api_key.strip()) > 0)

config = EmbedForgeConfig()
