"""Knowledge Engine managing provenance-linked facts and conflict preservation."""

from typing import List, Optional, Dict, Any
from datetime import datetime
from embedforge.models.knowledge import KnowledgeItem, KnowledgeConflict, KnowledgeBase, TrustLevel
from embedforge.models.research import ResearchSource, ApprovalStatus
from embedforge.engines.research.extractor import FactExtractor
from embedforge.core.logger import logger

class KnowledgeEngine:
    """Stores approved engineering facts and manages conflicts with strict provenance."""

    def __init__(self):
        self.kb = KnowledgeBase()
        self.extractor = FactExtractor()

    def ingest_approved_sources(self, approved_sources: List[ResearchSource], target_subject: str) -> KnowledgeBase:
        """Extracts facts from approved sources only and adds them to the KnowledgeBase."""
        for src in approved_sources:
            if src.approval_status != ApprovalStatus.APPROVED:
                logger.log_event("KNOW_SKIP", f"Skipping unapproved resource {src.id} ({src.url})")
                continue

            facts = self.extractor.extract_facts_from_source(src, target_subject)
            for fact in facts:
                self.add_fact(fact)

        return self.kb

    def add_fact(self, item: KnowledgeItem) -> None:
        """Adds a fact while checking for conflicts with existing facts."""
        existing = self.kb.find_fact(item.subject, item.property_name)
        if existing:
            # Check if values conflict
            if str(existing.value).lower() != str(item.value).lower():
                conflict_id = f"CONF-{len(self.kb.conflicts)+1:03d}"
                conflict = KnowledgeConflict(
                    id=conflict_id,
                    subject=item.subject,
                    property_name=item.property_name,
                    claim_a=existing,
                    claim_b=item,
                    resolution_status="UNRESOLVED",
                    resolution_note=f"Conflict detected between {existing.source_type} ({existing.value}) and {item.source_type} ({item.value}). Preserving both."
                )
                self.kb.conflicts.append(conflict)
                logger.log_event("KNOW_CONFLICT", f"Conflict on {item.subject}.{item.property_name}: '{existing.value}' vs '{item.value}'")
                return

        self.kb.items.append(item)
        logger.log_provenance(item.id, item.source_url, f"{item.subject}.{item.property_name} = {item.value}")

    def query_fact(self, subject: str, property_name: str) -> Optional[KnowledgeItem]:
        return self.kb.find_fact(subject, property_name)
