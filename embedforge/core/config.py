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
        default_factory=lambda: os.getenv("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1")
    )
    nebius_model: str = Field(
        default_factory=lambda: os.getenv("NEBIUS_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
    )
    tavily_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("TAVILY_API_KEY")
    )
    agent_model: str = Field(
        default_factory=lambda: os.getenv("NEBIUS_AGENT_MODEL") or os.getenv("NEBIUS_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
    )
    # Optional second provider: NVIDIA API catalog / NIM (OpenAI-compatible), https://build.nvidia.com
    nvidia_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("NVIDIA_API_KEY")
    )
    nvidia_base_url: str = Field(
        default_factory=lambda: os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
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
    def has_llm(self) -> bool:
        return self.has_nebius or self.has_nvidia

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key and len(self.tavily_api_key.strip()) > 0)

config = EmbedForgeConfig()
