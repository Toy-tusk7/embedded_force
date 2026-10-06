"""Engineering planning and firmware artifact contracts."""

from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class FileType(str, Enum):
    C_HEADER = "C_HEADER"
    C_SOURCE = "C_SOURCE"
    LINKER_SCRIPT = "LINKER_SCRIPT"
    MAKEFILE = "MAKEFILE"
    DOC = "DOC"

class FirmwareFile(BaseModel):
    filename: str
    relative_path: str
    content: str
    file_type: FileType
    version: str = "v1.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class HalBoundary(BaseModel):
    module_name: str
    target_mcu: str
    peripherals_used: List[str] = Field(default_factory=list)
    abstracted_functions: List[str] = Field(default_factory=list)
    pin_mappings: Dict[str, str] = Field(default_factory=dict)

class EngineeringPlan(BaseModel):
    id: str = "PLAN-001"
    target_mcu: str
    target_sensor: str
    bus_interface: str # e.g. "I2C", "SPI", "UART"
    hal_boundary: HalBoundary
    memory_budget_bytes: int = 128 * 1024
    clock_frequency_hz: int = 84_000_000
    timing_constraints_ms: Dict[str, float] = Field(default_factory=dict)
    architecture_summary: str
    implementation_steps: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
