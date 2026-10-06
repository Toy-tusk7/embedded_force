"""Information gap detector for embedded hardware requirements."""

import re
from typing import List, Set
from embedforge.models.requirement import RequirementItem, InformationGap

class RequirementGapDetector:
    """Detects missing embedded specifications and flags them as information gaps."""

    def detect_gaps(self, source_text: str, items: List[RequirementItem]) -> List[InformationGap]:
        gaps: List[InformationGap] = []
        gap_id = 1

        # Check explicit facts present
        explicit_keys: Set[str] = set()
        components_found: List[str] = []

        for item in items:
            for k, v in item.explicit_facts.items():
                explicit_keys.add(k)
                if k == "target_component":
                    components_found.append(v)

        # 1. Target Sensor/IC identification gap
        if not components_found:
            gaps.append(InformationGap(
                id=f"GAP-{gap_id:03d}",
                gap_type="MISSING_TARGET_DEVICE",
                target_subject="HARDWARE",
                target_property="sensor_or_ic_model",
                description="No specific sensor or peripheral IC model was named in the requirement.",
                criticality="CRITICAL",
                suggested_search_terms=["embedded sensor components"]
            ))
            gap_id += 1

        MCU_PREFIXES = ("STM32", "AT32", "ESP32", "RP2040", "ATMEGA", "ATTINY", "NRF52", "NRF53", "SAMD", "CH32", "PIC", "GD32", "MSP430", "LPC")
        GATE_DRIVER_PREFIXES = ("FD6288", "IR210", "DRV8", "A4988", "TMC22", "L298", "L293", "TC44", "MOSFET", "GATE", "BRIDGE", "ESC")

        # For each identified IC, detect peripheral-level engineering gaps
        for comp in components_found:
            ucomp = comp.upper()
            if re.match(r"^P[A-Z]\d+$", ucomp) or re.match(r"^GPIO\d+$", ucomp):
                continue
            is_mcu = any(p in ucomp for p in MCU_PREFIXES)
            is_gate_driver = any(p in ucomp for p in GATE_DRIVER_PREFIXES)

            # MCUs are host processors: they do not have I2C slave addresses or peripheral sensor register maps
            if is_mcu:
                continue

            # Discrete gate drivers & motor bridges: controlled directly via GPIO/PWM, no I2C/SPI slave address or register map
            if is_gate_driver:
                if "bus_interface" not in explicit_keys and not any(k in source_text.upper() for k in ("PWM", "GPIO")):
                    gaps.append(InformationGap(
                        id=f"GAP-{gap_id:03d}",
                        gap_type="MISSING_PWM_CONTROL",
                        target_subject=comp,
                        target_property="pwm_control_scheme",
                        description=f"Control interface (complementary PWM channels, dead-time) for gate driver {comp} is not specified.",
                        criticality="HIGH",
                        suggested_search_terms=[f"{comp} complementary PWM dead time pinout"]
                    ))
                    gap_id += 1
                continue

            # Standard digital peripheral / sensor (e.g. MPU6050, BME280)
            # Check bus interface
            if "bus_interface" not in explicit_keys:
                gaps.append(InformationGap(
                    id=f"GAP-{gap_id:03d}",
                    gap_type="MISSING_INTERFACE_PROTOCOL",
                    target_subject=comp,
                    target_property="bus_interface",
                    description=f"Communication bus interface (I2C, SPI, UART) for {comp} is not specified.",
                    criticality="HIGH",
                    suggested_search_terms=[f"{comp} datasheet communication interface"]
                ))
                gap_id += 1

            # Check I2C / SPI address or chip-select
            if "hex_address_or_reg" not in explicit_keys:
                gaps.append(InformationGap(
                    id=f"GAP-{gap_id:03d}",
                    gap_type="MISSING_DEVICE_ADDRESS",
                    target_subject=comp,
                    target_property="device_address",
                    description=f"Hardware device address / default I2C address for {comp} is missing.",
                    criticality="CRITICAL",
                    suggested_search_terms=[f"{comp} default I2C address register map datasheet"]
                ))
                gap_id += 1

            # Check Register Map & Initialization Sequence
            gaps.append(InformationGap(
                id=f"GAP-{gap_id:03d}",
                gap_type="MISSING_REGISTER_MAP",
                target_subject=comp,
                target_property="register_map",
                description=f"Register map, WHO_AM_I register, and power management registers for {comp} are unverified.",
                criticality="HIGH",
                suggested_search_terms=[f"{comp} register map power management WHO_AM_I reset"]
            ))
            gap_id += 1

            # Check Power-up Delay & Sampling limits
            if "timing_spec" not in explicit_keys:
                gaps.append(InformationGap(
                    id=f"GAP-{gap_id:03d}",
                    gap_type="MISSING_TIMING_SPEC",
                    target_subject=comp,
                    target_property="sampling_rate_and_delays",
                    description=f"Operating frequency, sampling rate, and reset delay timing for {comp} are not specified.",
                    criticality="MEDIUM",
                    suggested_search_terms=[f"{comp} max clock speed startup time delay"]
                ))
                gap_id += 1

        return gaps
