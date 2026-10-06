"""Research and discovery data contracts."""

from enum import Enum
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class SourceType(str, Enum):
    DATASHEET = "DATASHEET"
    REFERENCE_MANUAL = "REFERENCE_MANUAL"
    APPLICATION_NOTE = "APPLICATION_NOTE"
    GITHUB_REPOSITORY = "GITHUB_REPOSITORY"
    COMMUNITY_FORUM = "COMMUNITY_FORUM"
    OTHER = "OTHER"

class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

class ResearchQuery(BaseModel):
    id: str
    target_gap_id: str
    query_text: str
    search_type: str = "DATASHEET_SEARCH"
    target_domains: List[str] = Field(default_factory=list)
    rationale: str

class ResearchSource(BaseModel):
    id: str
    url: str
    title: str
    snippet: str
    raw_content: Optional[str] = None
    source_type: SourceType = SourceType.OTHER
    trust_score: float = Field(ge=0.0, le=1.0, default=0.5)
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_notes: Optional[str] = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ResearchResult(BaseModel):
    id: str = "RES-001"
    gap_id: str
    query: ResearchQuery
    sources: List[ResearchSource] = Field(default_factory=list)
    gaps_remaining: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
