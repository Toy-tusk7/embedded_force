"""Requirement data contracts."""

from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class RequirementCategory(str, Enum):
    FUNCTIONAL = "FUNCTIONAL"
    INTERFACE = "INTERFACE"
    TIMING = "TIMING"
    CONSTRAINT = "CONSTRAINT"
    POWER = "POWER"
    HARDWARE_PIN = "HARDWARE_PIN"

class RequirementItem(BaseModel):
    id: str
    category: RequirementCategory
    description: str
    explicit_facts: Dict[str, Any] = Field(default_factory=dict)
    raw_source_phrase: str

class InformationGap(BaseModel):
    id: str
    gap_type: str
    target_subject: str
    target_property: str
    description: str
    criticality: str = "HIGH" # HIGH, MEDIUM, LOW
    suggested_search_terms: List[str] = Field(default_factory=list)

class RequirementSet(BaseModel):
    id: str = "REQ-001"
    source_text: str
    requirements: List[RequirementItem] = Field(default_factory=list)
    information_gaps: List[InformationGap] = Field(default_factory=list)
    status: str = "EXTRACTED"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
