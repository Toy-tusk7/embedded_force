"""Deterministic static behavior extraction data contracts."""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

class RegisterAccess(BaseModel):
    register_name: Optional[str] = None
    address_or_offset: str # e.g. "0x68", "0x19"
    access_type: str # "READ" | "WRITE" | "RMW"
    bitmask: Optional[str] = None
    value_written: Optional[str] = None
    file: str
    line: int
    context_snippet: str

class ProtocolCall(BaseModel):
    bus: str # "I2C" | "SPI" | "UART" | "GPIO"
    operation: str # "TRANSMIT" | "RECEIVE" | "INIT" | "SET_PIN"
    target_address: Optional[str] = None
    frequency_hz: Optional[int] = None
    timeout_ms: Optional[int] = None
    file: str
    line: int

class FunctionBehavior(BaseModel):
    name: str
    file: str
    start_line: int
    end_line: int
    calls: List[str] = Field(default_factory=list)
    register_accesses: List[RegisterAccess] = Field(default_factory=list)
    protocol_calls: List[ProtocolCall] = Field(default_factory=list)
    has_timeout: bool = False
    checks_return_code: bool = False

class BehaviorModel(BaseModel):
    id: str = "BEHAV-001"
    firmware_id: str
    functions: List[FunctionBehavior] = Field(default_factory=list)
    all_register_accesses: List[RegisterAccess] = Field(default_factory=list)
    all_protocol_calls: List[ProtocolCall] = Field(default_factory=list)
    peripherals_identified: List[str] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
