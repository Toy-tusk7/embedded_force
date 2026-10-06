"""Repair planning, patching, and reverification contracts."""

from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class PatchAction(BaseModel):
    file_path: str
    line_start: int
    line_end: int
    old_content: str
    new_content: str
    explanation: str

class PatchPlan(BaseModel):
    id: str = "PATCH-001"
    evidence_case_id: str
    finding_id: str
    target_files: List[str] = Field(default_factory=list)
    actions: List[PatchAction] = Field(default_factory=list)
    rationale: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "PROPOSED" # PROPOSED, APPLIED, REVERIFIED_PASS, REVERIFIED_FAIL

class RepairSession(BaseModel):
    id: str = "REPAIR-001"
    case_id: str
    patch_plan: PatchPlan
    rebuild_id: Optional[str] = None
    reverify_id: Optional[str] = None
    success: bool = False
    notes: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
