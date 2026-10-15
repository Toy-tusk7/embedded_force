"""Deterministic engines used by the agent: the requirement parser (explicit facts and gaps in a user request)."""

from embedforge.engines.requirements import RequirementParser, RequirementGapDetector

__all__ = ["RequirementParser", "RequirementGapDetector"]
