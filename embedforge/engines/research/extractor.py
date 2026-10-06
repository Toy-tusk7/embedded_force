"""Extracts structured candidate engineering facts from technical research sources.

Extracts hardware specifications:
- MCU Pin Alternate Function (AF) multiplexing tables
- Advanced Timer PWM & dead-time register architectures
- Gate Driver electrical timings, logic thresholds, and shoot-through protection
- Digital sensor bus addresses, register maps, and startup delays
"""

import re
from typing import List
from embedforge.models.research import ResearchSource
from embedforge.models.knowledge import KnowledgeItem, TrustLevel

class FactExtractor:
    """Extracts provenance-linked candidate facts from technical documentation snippets."""

    def extract_facts_from_source(self, source: ResearchSource, subject: str) -> List[KnowledgeItem]:
        facts: List[KnowledgeItem] = []
        text = f"{source.title} {source.snippet} {source.raw_content or ''}"
        idx = 1
        trust = TrustLevel.HIGH_OFFICIAL if source.trust_score >= 0.9 else TrustLevel.MEDIUM_REFERENCE

        def _add(prop: str, val: any, prov: str, unit: str = ""):
            nonlocal idx
            facts.append(KnowledgeItem(
                id=f"KNOW-{subject}-{idx:02d}",
                subject=subject,
                property_name=prop,
                value=val,
                unit=unit,
                source_id=source.id,
                source_url=source.url,
                source_type=source.source_type.value,
                location=source.title,
                approval_status=source.approval_status.value,
                trust_level=trust,
                provenance=prov[:200]
            ))
            idx += 1

        # ==================== 1. MCU Pin Alternate Function (AF) Mappings ====================
        # Form A: Sentence style ("PA8 is TMR1_CH1", "PA7 is TMR1_CH1C (TMR1_CH1N)", "PA2 -> TMR2_CH3")
        af_matches = re.finditer(
            r"\b(P[A-Z]\d+)\s+(?:is|->|mapped to|:|=)\s+([A-Z0-9_]+(?:\s*\([A-Z0-9_]+\))?)",
            text,
            re.IGNORECASE
        )
        for m in af_matches:
            pin = m.group(1).upper()
            func = m.group(2).strip()
            clean_func = func.split()[0].rstrip(",")
            _add(f"pin_af_{pin.lower()}", clean_func, m.group(0))

        # Form B: 2D Multi-Column Layout Table (e.g. "18   PA8   I/O   FT   TMR1_CH1 ...")
        table_matches = re.finditer(
            r"^\s*\d*\s*(P[A-Z]\d+)\s+[^\n\r]*?\b(TMR\d+_CH\d+[NC]?|TIM\d+_CH\d+[NC]?|USART\d+_[RT]X|SPI\d+_[A-Z]+|I2C\d+_[A-Z]+)\b",
            text,
            re.MULTILINE
        )
        for tm in table_matches:
            pin = tm.group(1).upper()
            func = tm.group(2).strip()
            # Avoid duplicate if already extracted
            if not any(f.property_name == f"pin_af_{pin.lower()}" for f in facts):
                _add(f"pin_af_{pin.lower()}", func, tm.group(0).strip())

        # Form C: GitHub BSP / C Header defines (e.g. "#define PIN_HI_A PA10", "#define PA8_AF TMR1_CH1")
        code_matches = re.finditer(
            r"#define\s+(?:PIN_)?([A-Za-z0-9_]+)\s+([P][A-Z]\d+)\b",
            text
        )
        for cm in code_matches:
            signal = cm.group(1).lower()
            pin = cm.group(2).upper()
            _add(f"pin_def_{signal}", pin, cm.group(0))

        # ==================== 2. Advanced Timer PWM & Dead-Time Architecture ====================
        # Dead-time register (e.g. TMR1_BKDT, TMR1_BRK, TIM1_BDTR)
        dt_reg_match = re.search(r"\b(TMR\d+_BKDT|TMR\d+_BRK|TIM\d+_BDTR|DTG(?:\[\d+:\d+\])?)\b", text, re.IGNORECASE)
        if dt_reg_match:
            _add("dead_time_reg", dt_reg_match.group(1), dt_reg_match.group(0))

        # Center-aligned PWM capability
        if re.search(r"\bcenter-aligned(?:\s+PWM)?\b", text, re.IGNORECASE):
            _add("pwm_mode", "CENTER_ALIGNED", "Supports center-aligned PWM.")

        # Complementary channels
        if re.search(r"\bcomplementary\s+channels\b", text, re.IGNORECASE):
            _add("has_complementary_outputs", True, "Advanced timer features complementary channels with dead-time generator.")

        # Timer / Bus Clock Speed
        tmr_clk_match = re.search(r"(?:timer clock is|running up to|maximum.*?clock is)\s*(\d+)\s*(MHz|kHz)", text, re.IGNORECASE)
        if tmr_clk_match:
            _add("max_timer_clock_mhz", int(tmr_clk_match.group(1)), tmr_clk_match.group(0), unit=tmr_clk_match.group(2))

        # ==================== 3. Gate Driver Electrical Timing & Logic ====================
        # Minimum dead-time requirement (e.g. 200ns)
        dead_time_match = re.search(r"(?:dead-time\s+is|dead-time\s+setting\s*>=|dead\s+time\s*>=|protection\s+dead-time\s+is)\s*(\d+)\s*ns", text, re.IGNORECASE)
        if dead_time_match:
            _add("min_dead_time_ns", int(dead_time_match.group(1)), dead_time_match.group(0), unit="ns")

        # Logic level (Active High vs Active Low)
        logic_match = re.search(r"\b(?:Active\s+HIGH|Active-High)\b", text, re.IGNORECASE)
        if logic_match:
            _add("input_logic", "ACTIVE_HIGH", logic_match.group(0))

        # Turn-on delay ton
        ton_match = re.search(r"turn-on\s+delay\s+ton\s*=\s*(\d+)\s*ns", text, re.IGNORECASE)
        if ton_match:
            _add("turn_on_delay_ns", int(ton_match.group(1)), ton_match.group(0), unit="ns")

        # Turn-off delay toff
        toff_match = re.search(r"turn-off\s+delay\s+toff\s*=\s*(\d+)\s*ns", text, re.IGNORECASE)
        if toff_match:
            _add("turn_off_delay_ns", int(toff_match.group(1)), toff_match.group(0), unit="ns")

        # Supply voltage
        vcc_match = re.search(r"(?:VCC:\s*(\d+)V\s+to\s+(\d+)V|(\d+)V\s*nominal)", text, re.IGNORECASE)
        if vcc_match:
            v_val = int(vcc_match.group(3) or vcc_match.group(1))
            _add("nominal_supply_voltage_v", v_val, vcc_match.group(0), unit="V")

        # ==================== 4. Digital Sensor Specifications ====================
        # I2C slave address (e.g., 0x68, 0x69, 0x76, 0x77)
        i2c_addr_match = re.search(r"\b(?:I2C\s+slave\s+address|device\s+address|slave\s+address|address)\s*(?:is|of|:)?\s*(0x[0-9A-Fa-f]{2})\b", text, re.IGNORECASE) or re.search(r"\b(0x68|0x69|0x76|0x77)\b", text)
        if i2c_addr_match:
            addr_val = i2c_addr_match.group(1) if i2c_addr_match.lastindex else i2c_addr_match.group(0)
            _add("i2c_primary_address", addr_val, i2c_addr_match.group(0))

        # WHO_AM_I register and ID value (e.g., WHO_AM_I: 0x75, id: 0x68 or 0x60)
        whoami_match = re.search(r"WHO_AM_I.*?0x([0-9A-Fa-f]{2})", text, re.IGNORECASE)
        if whoami_match:
            _add("who_am_i_reg", f"0x{whoami_match.group(1)}", whoami_match.group(0))

        # PWR_MGMT / Reset register
        pwr_match = re.search(r"(?:PWR_MGMT_1|power management|reset register).*?(0x[0-9A-Fa-f]{2})", text, re.IGNORECASE)
        if pwr_match:
            _add("pwr_mgmt_reg", pwr_match.group(1), pwr_match.group(0))

        # Max bus clock frequency (e.g., 400kHz)
        clock_match = re.search(r"(\d+)\s*(kHz|MHz)\s*(?:I2C|SPI|clock)?", text, re.IGNORECASE)
        if clock_match:
            _add("max_bus_clock_khz", int(clock_match.group(1)), clock_match.group(0), unit=clock_match.group(2))

        # Startup / reset delay (e.g., 30ms)
        delay_match = re.search(r"(?:startup time|startup delay|power-on reset|reset delay|delay)\s*(?:is|of|:)?\s*(\d+)\s*(ms|us)", text, re.IGNORECASE)
        if delay_match:
            _add("startup_delay_ms", int(delay_match.group(1)), delay_match.group(0), unit=delay_match.group(2))

        return facts
