"""Deterministic static analysis extracting actual code behavior independently of LLM."""

import re
from typing import List, Dict, Any
from embedforge.models.engineering import FirmwareFile
from embedforge.models.analysis import (
    BehaviorModel, FunctionBehavior, RegisterAccess, ProtocolCall
)
from embedforge.core.logger import logger

class StaticBehaviorAnalyzer:
    """Extracts actual firmware behavior (registers, I2C addresses, delays, timeouts) via deterministic static analysis."""

    def analyze_firmware(self, files: List[FirmwareFile], firmware_id: str = "FW-001") -> BehaviorModel:
        logger.log_event("ANALYSIS", f"Performing deterministic behavior analysis on {firmware_id}")

        all_registers: List[RegisterAccess] = []
        all_protocols: List[ProtocolCall] = []
        functions: List[FunctionBehavior] = []
        peripherals = set()

        # Step 1: Map #define macros in header files
        macro_map: Dict[str, str] = {}
        for f in files:
            for line in f.content.split("\n"):
                m = re.match(r"^\s*#define\s+([A-Za-z0-9_]+)\s+(0x[0-9A-Fa-f]+|\d+)", line)
                if m:
                    macro_map[m.group(1)] = m.group(2)

        # Step 2: Analyze functions and calls in C source files
        for f in files:
            lines = f.content.split("\n")
            current_func: FunctionBehavior = None

            for line_no, line in enumerate(lines, 1):
                clean_line = line.strip()
                if clean_line.startswith("extern") or clean_line.startswith("//") or clean_line.startswith("/*") or clean_line.startswith("*"):
                    continue

                # Detect function definition
                func_match = re.match(r"^[A-Za-z0-9_]+_Status_t\s+([A-Za-z0-9_]+)\s*\(", line) or re.match(r"^int\s+([A-Za-z0-9_]+)\s*\(", line)
                if func_match:
                    func_name = func_match.group(1)
                    current_func = FunctionBehavior(
                        name=func_name,
                        file=f.filename,
                        start_line=line_no,
                        end_line=line_no
                    )
                    functions.append(current_func)

                # Detect I2C writes: hal_i2c_write(addr, reg, val, timeout)
                i2c_write_m = re.search(r"hal_i2c_write\s*\(\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^)]+)\s*\)", line)
                if i2c_write_m:
                    raw_addr = i2c_write_m.group(1).strip()
                    raw_reg = i2c_write_m.group(2).strip()
                    raw_val = i2c_write_m.group(3).strip()
                    raw_timeout = i2c_write_m.group(4).strip()

                    resolved_addr = macro_map.get(raw_addr, raw_addr)
                    resolved_reg = macro_map.get(raw_reg, raw_reg)

                    reg_access = RegisterAccess(
                        register_name=raw_reg,
                        address_or_offset=resolved_reg,
                        access_type="WRITE",
                        value_written=raw_val,
                        file=f.filename,
                        line=line_no,
                        context_snippet=line.strip()
                    )
                    all_registers.append(reg_access)

                    proto = ProtocolCall(
                        bus="I2C",
                        operation="TRANSMIT",
                        target_address=resolved_addr,
                        timeout_ms=int(raw_timeout) if raw_timeout.isdigit() else None,
                        file=f.filename,
                        line=line_no
                    )
                    all_protocols.append(proto)
                    peripherals.add("I2C")

                    if current_func:
                        current_func.register_accesses.append(reg_access)
                        current_func.protocol_calls.append(proto)
                        if raw_timeout != "0":
                            current_func.has_timeout = True

                # Detect I2C reads: hal_i2c_read(addr, reg, buf, len, timeout)
                i2c_read_m = re.search(r"hal_i2c_read\s*\(\s*([^,]+)\s*,\s*([^,]+)\s*,", line)
                if i2c_read_m:
                    raw_addr = i2c_read_m.group(1).strip()
                    raw_reg = i2c_read_m.group(2).strip()
                    resolved_addr = macro_map.get(raw_addr, raw_addr)
                    resolved_reg = macro_map.get(raw_reg, raw_reg)

                    reg_access = RegisterAccess(
                        register_name=raw_reg,
                        address_or_offset=resolved_reg,
                        access_type="READ",
                        file=f.filename,
                        line=line_no,
                        context_snippet=line.strip()
                    )
                    all_registers.append(reg_access)

                    proto = ProtocolCall(
                        bus="I2C",
                        operation="RECEIVE",
                        target_address=resolved_addr,
                        file=f.filename,
                        line=line_no
                    )
                    all_protocols.append(proto)
                    peripherals.add("I2C")

                    if current_func:
                        current_func.register_accesses.append(reg_access)
                        current_func.protocol_calls.append(proto)

                # Detect PWM dead-time configuration (e.g. dead_time_ns, DEAD_TIME_NS, BKDT/BDTR writes)
                dt_match = re.search(r"\b(?:dead_?time(?:_ns)?|DTG)\s*[:=]\s*(\d+)", line, re.IGNORECASE) or re.search(r"#define\s+(?:ESC_)?DEAD_?TIME(?:_NS)?\s+(\d+)", line, re.IGNORECASE)
                if dt_match:
                    dt_val = int(dt_match.group(1))
                    proto = ProtocolCall(
                        bus="PWM",
                        operation="DEADTIME_CONFIG",
                        timeout_ms=dt_val,  # reuse timeout_ms field to carry dead-time nanoseconds
                        file=f.filename,
                        line=line_no
                    )
                    all_protocols.append(proto)
                    peripherals.add("PWM")

                # Detect PWM frequency configuration (e.g. PWM_FREQ_HZ 20000)
                freq_match = re.search(r"#define\s+(?:ESC_)?PWM_FREQ(?:UENCY)?(?:_HZ)?\s+(\d+)", line, re.IGNORECASE)
                if freq_match:
                    proto = ProtocolCall(
                        bus="PWM",
                        operation="PWM_CONFIG",
                        frequency_hz=int(freq_match.group(1)),
                        file=f.filename,
                        line=line_no
                    )
                    all_protocols.append(proto)
                    peripherals.add("PWM")

                # Detect GPIO Pin Alternate Function and channel mapping definitions
                pin_match = re.search(r"#define\s+(?:PIN_)?([A-Za-z0-9_]+)\s+([P][A-Z]\d+)", line)
                if pin_match:
                    proto = ProtocolCall(
                        bus="GPIO",
                        operation="PIN_MAP",
                        target_address=f"{pin_match.group(1)}={pin_match.group(2)}",
                        file=f.filename,
                        line=line_no
                    )
                    all_protocols.append(proto)
                    peripherals.add("GPIO")

        return BehaviorModel(
            id=f"BEHAV-{firmware_id}",
            firmware_id=firmware_id,
            functions=functions,
            all_register_accesses=all_registers,
            all_protocol_calls=all_protocols,
            peripherals_identified=list(peripherals)
        )
