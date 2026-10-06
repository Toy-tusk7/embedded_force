"""Physical hardware debugging and errata diagnostic data contracts."""

from enum import Enum
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class DebugClassification(str, Enum):
    SOFTWARE_MISMATCH = "SOFTWARE_MISMATCH"           # Fixable bug in driver or configuration
    HARDWARE_DEPENDENT = "HARDWARE_DEPENDENT"         # Physical wiring, floating pin, missing pull-ups
    SILICON_ERRATA = "SILICON_ERRATA"                 # Known manufacturer silicon limitation
    UNKNOWN = "UNKNOWN"                               # Insufficient physical evidence

class HumanObservation(BaseModel):
    id: str = "OBS-001"
    session_id: str
    target_chip: str
    symptom: str                                      # e.g., "Device NACKs on address 0x68" or "I2C bus hangs"
    measured_voltage: Optional[float] = None          # e.g., 3.3
    logic_analyzer_notes: Optional[str] = None        # e.g., "SCL clocking but SDA pulled low continuously"
    serial_log_snippet: Optional[str] = None          # e.g., "ERR_DEVICE_NOT_FOUND: 0x68"
    hardware_setup_notes: Optional[str] = None        # e.g., "GY-521 breakout connected via breadboard jumper wires"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class ErrataItem(BaseModel):
    id: str
    chip: str
    title: str
    description: str
    official_workaround: Optional[str] = None
    source_url: str
    trust_score: float = 0.95

class DebugDiagnosis(BaseModel):
    id: str = "DIAG-001"
    observation_id: str
    classification: DebugClassification
    root_cause_explanation: str
    recommended_action: str
    errata_references: List[ErrataItem] = Field(default_factory=list)
    software_patch_required: bool = False
    hardware_action_required: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.90)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
