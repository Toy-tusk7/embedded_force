"""Build engine and compiler artifact contracts."""

from enum import Enum
from typing import List, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class BuildStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"

class BuildDiagnostic(BaseModel):
    file: str
    line: int
    column: Optional[int] = None
    severity: str # "WARNING" | "ERROR"
    code: Optional[str] = None
    message: str

class BuildArtifact(BaseModel):
    id: str = "BUILD-001"
    firmware_id: str
    compiler: str = "arm-none-eabi-gcc"
    compiler_flags: List[str] = Field(default_factory=list)
    status: BuildStatus
    flash_size_bytes: int = 0
    ram_size_bytes: int = 0
    diagnostics: List[BuildDiagnostic] = Field(default_factory=list)
    raw_log: str
    output_bin_path: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def has_errors(self) -> bool:
        return any(d.severity == "ERROR" for d in self.diagnostics) or self.status != BuildStatus.SUCCESS
