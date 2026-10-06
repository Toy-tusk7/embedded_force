"""Research engine package integrating Tavily discovery and extraction."""

from embedforge.engines.research.planner import ResearchPlanner
from embedforge.engines.research.classifier import SourceClassifier
from embedforge.engines.research.tavily_client import TavilyResearchClient
from embedforge.engines.research.extractor import FactExtractor

__all__ = [
    "ResearchPlanner",
    "SourceClassifier",
    "TavilyResearchClient",
    "FactExtractor"
]
