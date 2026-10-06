"""Knowledge Engine data contracts."""

from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class TrustLevel(str, Enum):
    HIGH_OFFICIAL = "HIGH_OFFICIAL"       # Manufacturer Datasheet, Reference Manual
    MEDIUM_REFERENCE = "MEDIUM_REFERENCE" # Official SDK, Verified GitHub HAL
    LOW_COMMUNITY = "LOW_COMMUNITY"       # Forums, unverified blog posts

class KnowledgeItem(BaseModel):
    id: str = "KNOW-001"
    subject: str                      # e.g., "MPU6050", "STM32F401RE"
    property_name: str                # e.g., "i2c_primary_address", "who_am_i_reg"
    value: Any                        # e.g., "0x68", "0x75"
    unit: Optional[str] = None        # e.g., "Hz", "ms", "V", "bytes"
    conditions: Dict[str, Any] = Field(default_factory=dict) # e.g. {"AD0_pin": "LOW"}
    source_id: str                    # RES-xxx
    source_url: str
    source_type: str                  # DATASHEET, etc.
    location: Optional[str] = None    # "Section 7.1, Page 34"
    approval_status: str = "APPROVED" # Human approved
    trust_level: TrustLevel = TrustLevel.HIGH_OFFICIAL
    provenance: str                   # Exact quoted snippet or table row
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class KnowledgeConflict(BaseModel):
    id: str
    subject: str
    property_name: str
    claim_a: KnowledgeItem
    claim_b: KnowledgeItem
    resolution_status: str = "UNRESOLVED"
    resolution_note: Optional[str] = None

class KnowledgeBase(BaseModel):
    items: List[KnowledgeItem] = Field(default_factory=list)
    conflicts: List[KnowledgeConflict] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def find_fact(self, subject: str, property_name: str) -> Optional[KnowledgeItem]:
        for item in self.items:
            if item.subject.lower() == subject.lower() and item.property_name.lower() == property_name.lower():
                return item
        return None
