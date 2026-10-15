"""EmbedForge core utilities."""

from embedforge.core.config import config, EmbedForgeConfig
from embedforge.core.logger import logger, console
from embedforge.core.telemetry import tracer, LangSmithTracer

__all__ = ["config", "EmbedForgeConfig", "logger", "console", "tracer", "LangSmithTracer"]
