"""Evidence lineage builder tracking artifacts from requirements to verified patches."""

from typing import List, Dict
from embedforge.models.verification import Finding
from embedforge.models.evidence import EvidenceCase, LineageNode
from embedforge.core.logger import logger

class EvidenceEngine:
    """Maintains end-to-end artifact lineage and produces structured EvidenceCases."""

    def __init__(self):
        self.lineage_tree: Dict[str, LineageNode] = {}

    def record_node(self, artifact_id: str, artifact_type: str, parent_ids: List[str], summary: str) -> LineageNode:
        node = LineageNode(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            parent_ids=parent_ids,
            summary=summary
        )
        self.lineage_tree[artifact_id] = node
        return node

    def build_evidence_case(self, finding: Finding, case_id: str = "CASE-001") -> EvidenceCase:
        logger.log_event("EVIDENCE", f"Constructing evidence case {case_id} for Finding [{finding.id}]")

        provenance_trail = [
            finding.requirement_ref or "REQ-001",
            finding.specification_ref or "KNOW-001",
            finding.behavior_ref or "BEHAV-001",
            finding.id
        ]

        case = EvidenceCase(
            id=case_id,
            finding_id=finding.id,
            requirement_id=finding.requirement_ref,
            specification_id=finding.specification_ref,
            source_file=finding.file,
            line_number=finding.line,
            expected_claim=f"Approved specification expects '{finding.expected_value}'",
            observed_code_claim=f"Source code implements '{finding.actual_value}'",
            provenance_trail=provenance_trail,
            conclusion=(
                f"Definitive PROVEN_MISMATCH detected in {finding.file}:{finding.line}. "
                f"Source code value ({finding.actual_value}) conflicts with verified specification ({finding.expected_value}). "
                f"Targeted software patch required."
            ),
            requires_repair=True
        )

        return case
