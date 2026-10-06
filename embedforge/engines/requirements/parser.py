"""Requirement parser extracting strictly explicit facts without invention."""

import re
from typing import List, Dict, Any, Optional
from embedforge.models.requirement import (
    RequirementSet, RequirementItem, RequirementCategory, InformationGap
)
from embedforge.engines.requirements.gap_detector import RequirementGapDetector

class RequirementParser:
    """Parses natural language requirements into structured explicit items and detects gaps."""

    def parse(self, user_request: str) -> RequirementSet:
        req_items: List[RequirementItem] = []
        item_counter = 1

        # Explicit regex patterns for extracting stated facts without hallucination
        patterns = [
            (
                RequirementCategory.INTERFACE,
                r"\b(I2C|SPI|UART|USART|CAN|USB|ADC|PWM|GPIO)\b",
                "bus_interface"
            ),
            (
                RequirementCategory.TIMING,
                r"\b(\d+(?:\.\d+)?)\s*(Hz|kHz|MHz|ms|us|seconds|sec)\b",
                "timing_spec"
            ),
            (
                RequirementCategory.POWER,
                r"\b(\d+(?:\.\d+)?)\s*(V|mV|mA|uA)\b",
                "voltage_or_current"
            ),
            (
                RequirementCategory.HARDWARE_PIN,
                r"\b(PA\d+|PB\d+|PC\d+|PD\d+|P\d+_\d+|pin\s*\d+|GPIO\d+)\b",
                "pin_reference"
            ),
            (
                RequirementCategory.CONSTRAINT,
                r"\b(0x[0-9A-Fa-f]{2,4})\b",
                "hex_address_or_reg"
            )
        ]

        extracted_facts: Dict[str, Any] = {}
        seen_facts = set()

        # Scan for explicit facts
        for cat, pat, fact_key in patterns:
            matches = re.finditer(pat, user_request, re.IGNORECASE)
            for m in matches:
                val = m.group(0)
                dedup_key = (cat, fact_key, val.upper())
                if dedup_key in seen_facts:
                    continue
                seen_facts.add(dedup_key)
                extracted_facts[fact_key] = val
                req_items.append(RequirementItem(
                    id=f"REQ-{item_counter:03d}",
                    category=cat,
                    description=f"Explicit {fact_key.replace('_', ' ')} specified: {val}",
                    explicit_facts={fact_key: val},
                    raw_source_phrase=m.group(0)
                ))
                item_counter += 1

        # Look for chip/sensor keywords explicitly mentioned in source text (e.g. MPU6050, BME280, STM32F401, ESP32)
        chip_pattern = r"\b([A-Z0-9]{3,}-[A-Z0-9]+|[A-Z]{2,}\d{2,}[A-Z0-9]*)\b"
        for m in re.finditer(chip_pattern, user_request):
            token = m.group(0)
            utoken = token.upper()
            if utoken in {"UART", "USART", "GPIO", "CAN", "PWM", "ADC", "DMA", "HAL", "SDK", "API"}:
                continue
            if re.match(r"^P[A-Z]\d+$", utoken) or re.match(r"^GPIO\d+$", utoken):
                continue
            dedup_key = (RequirementCategory.FUNCTIONAL, "target_component", utoken)
            if dedup_key in seen_facts:
                continue
            seen_facts.add(dedup_key)
            req_items.append(RequirementItem(
                id=f"REQ-{item_counter:03d}",
                category=RequirementCategory.FUNCTIONAL,
                description=f"Explicit target component identified: {token}",
                explicit_facts={"target_component": token},
                raw_source_phrase=token
            ))
            item_counter += 1

        # Always run gap detector to identify missing essential hardware parameters
        gap_detector = RequirementGapDetector()
        gaps = gap_detector.detect_gaps(user_request, req_items)

        return RequirementSet(
            id="REQ-001",
            source_text=user_request,
            requirements=req_items,
            information_gaps=gaps,
            status="PARSED"
        )
