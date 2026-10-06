"""Evidence case and lineage traceability contracts."""

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class LineageNode(BaseModel):
    artifact_id: str # e.g. "REQ-001", "RES-001", "KNOW-001", "PLAN-001", "FW-001"
    artifact_type: str
    parent_ids: List[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

class EvidenceCase(BaseModel):
    id: str = "CASE-001"
    finding_id: str
    requirement_id: Optional[str] = None
    specification_id: Optional[str] = None # KNOW-xxx
    source_file: str
    line_number: int
    expected_claim: str
    observed_code_claim: str
    provenance_trail: List[str] = Field(default_factory=list) # List of artifact IDs
    conclusion: str
    requires_repair: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
