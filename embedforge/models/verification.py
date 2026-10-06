"""Verification finding and evidence evaluation contracts."""

from enum import Enum
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class FindingClassification(str, Enum):
    PROVEN_MISMATCH = "PROVEN_MISMATCH"             # Direct violation of an approved spec fact
    INFERRED_ISSUE = "INFERRED_ISSUE"               # Missing defensive handling, missing timeout
    UNKNOWN = "UNKNOWN"                             # Insufficient evidence to determine
    HARDWARE_DEPENDENT = "HARDWARE_DEPENDENT"       # Physical environment / wiring / silicon variation

class Finding(BaseModel):
    id: str = "FIND-001"
    rule_id: str
    classification: FindingClassification
    title: str
    description: str
    requirement_ref: Optional[str] = None
    specification_ref: Optional[str] = None # KNOW-xxx
    behavior_ref: Optional[str] = None      # BEHAV-xxx
    file: str
    line: int
    expected_value: Any
    actual_value: Any
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class VerificationReport(BaseModel):
    id: str = "VERIFY-001"
    firmware_id: str
    behavior_id: str
    findings: List[Finding] = Field(default_factory=list)
    rules_evaluated: int = 0
    proven_mismatch_count: int = 0
    inferred_issue_count: int = 0
    hardware_dependent_count: int = 0
    passed: bool = True
    summary: str = ""
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
