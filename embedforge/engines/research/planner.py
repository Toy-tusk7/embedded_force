"""Hardware-aware research planner generating bounded, targeted queries for Tavily.

Restricts queries strictly to official manufacturer channels and GitHub repositories.
Classifies components into Host MCUs, Discrete Gate Drivers, and Digital Peripherals
to avoid querying for non-existent properties (e.g. I2C addresses on MCUs or gate drivers).
"""

from enum import Enum
import re
from typing import List, Dict
from embedforge.models.requirement import InformationGap
from embedforge.models.research import ResearchQuery

class ComponentCategory(str, Enum):
    HOST_MCU = "HOST_MCU"
    GATE_DRIVER = "GATE_DRIVER"
    DIGITAL_PERIPHERAL = "DIGITAL_PERIPHERAL"

# Primary high-authority semiconductor & sensor manufacturer domains
DEFAULT_MANUFACTURER_DOMAINS = [
    "st.com",
    "ti.com",
    "microchip.com",
    "invensense.tdk.com",
    "tdk.com",
    "bosch-sensortec.com",
    "nordicsemi.com",
    "nxp.com",
    "analog.com",
    "espressif.com",
    "arterytek.com",
    "arterychip.com",
    "fortiortech.com",
    "fd-ic.com",
    "github.com",
]

PART_OFFICIAL_DOMAINS: Dict[str, List[str]] = {
    "AT32": ["arterytek.com", "arterychip.com", "github.com"],
    "STM32": ["st.com", "github.com"],
    "ESP32": ["espressif.com", "github.com"],
    "ESP8266": ["espressif.com", "github.com"],
    "RP2040": ["raspberrypi.com", "github.com"],
    "RP2350": ["raspberrypi.com", "github.com"],
    "NRF52": ["nordicsemi.com", "github.com"],
    "NRF53": ["nordicsemi.com", "github.com"],
    "CH32": ["wch-ic.com", "github.com"],
    "FD6288": ["fortiortech.com", "fd-ic.com", "arterytek.com", "github.com"],
    "DRV8301": ["ti.com", "github.com"],
    "DRV8302": ["ti.com", "github.com"],
    "MPU6050": ["invensense.tdk.com", "tdk.com", "st.com", "ti.com"],
    "MPU6000": ["invensense.tdk.com", "tdk.com", "st.com", "ti.com"],
    "BME280": ["bosch-sensortec.com", "github.com"],
    "BMP280": ["bosch-sensortec.com", "github.com"],
    "BMI270": ["bosch-sensortec.com", "github.com"],
    "LIS3DH": ["st.com", "github.com"],
    "INA219": ["ti.com", "github.com"],
    "ADS1115": ["ti.com", "github.com"],
}

MCU_PREFIXES = ("AT32", "STM32", "ESP32", "ESP8266", "RP2040", "RP2350", "NRF52", "NRF53", "CH32", "SAMD", "LPC", "EFM32")
GATE_DRIVER_PREFIXES = ("FD6288", "DRV8301", "DRV8302", "IR2101", "IR2104", "EG2133", "EG2113", "FAN7888", "HIP4086")


def classify_component(part_name: str) -> ComponentCategory:
    """Classifies a silicon part name into its functional hardware category."""
    p_upper = re.sub(r"[^A-Za-z0-9]", "", part_name.upper())
    for m in MCU_PREFIXES:
        if m in p_upper:
            return ComponentCategory.HOST_MCU
    for g in GATE_DRIVER_PREFIXES:
        if g in p_upper:
            return ComponentCategory.GATE_DRIVER
    return ComponentCategory.DIGITAL_PERIPHERAL


def get_official_domains(part_name: str) -> List[str]:
    """Resolves strict official manufacturer and code repository domains for a part."""
    p_upper = re.sub(r"[^A-Za-z0-9]", "", part_name.upper())
    for prefix, domains in PART_OFFICIAL_DOMAINS.items():
        if prefix in p_upper:
            return domains
    return DEFAULT_MANUFACTURER_DOMAINS


class ResearchPlanner:
    """Plans targeted, bounded queries to resolve specific hardware information gaps."""

    def plan_queries(self, gaps: List[InformationGap]) -> List[ResearchQuery]:
        queries: List[ResearchQuery] = []

        for gap in gaps:
            subj = gap.target_subject
            prop = gap.target_property
            cat = classify_component(subj)
            domains = get_official_domains(subj)

            if cat == ComponentCategory.HOST_MCU:
                # MCUs: query pinout, timer AF matrix, reference manual, or CMSIS/HAL driver
                if gap.gap_type in ("MISSING_INTERFACE_PROTOCOL", "MISSING_PIN_MAPPING"):
                    q_text = f"{subj} datasheet pinout alternate function timer PWM channels"
                    search_type = "DATASHEET_SEARCH"
                elif gap.gap_type in ("MISSING_TIMING_SPEC", "MISSING_CLOCK_TREE"):
                    q_text = f"{subj} datasheet maximum clock frequency APB timer clock tree"
                    search_type = "TIMING_SPEC_SEARCH"
                else:
                    q_text = f"{subj} reference manual advanced timer complementary PWM dead time"
                    search_type = "REFERENCE_MANUAL_SEARCH"

            elif cat == ComponentCategory.GATE_DRIVER:
                # Gate drivers: query input thresholds, dead-time minimum, propagation delay
                q_text = f"{subj} 3-phase gate driver datasheet dead time propagation delay logic threshold"
                search_type = "DATASHEET_SEARCH"

            else:
                # Digital Peripherals / Sensors: register maps, I2C/SPI protocols
                if gap.gap_type == "MISSING_DEVICE_ADDRESS":
                    q_text = f"{subj} datasheet I2C address slave address WHO_AM_I register map"
                    search_type = "DATASHEET_SEARCH"
                elif gap.gap_type == "MISSING_REGISTER_MAP":
                    q_text = f"{subj} register map power management register descriptions datasheet"
                    search_type = "REGISTER_MAP_SEARCH"
                elif gap.gap_type == "MISSING_TIMING_SPEC":
                    q_text = f"{subj} datasheet startup time reset delay maximum clock frequency"
                    search_type = "TIMING_SPEC_SEARCH"
                elif gap.gap_type == "MISSING_INTERFACE_PROTOCOL":
                    q_text = f"{subj} pinout communication bus I2C SPI interface datasheet"
                    search_type = "INTERFACE_SEARCH"
                else:
                    q_text = f"{subj} {prop} embedded hardware datasheet"
                    search_type = "GENERAL_HARDWARE_SEARCH"

            queries.append(ResearchQuery(
                id=f"QRY-{gap.id}",
                target_gap_id=gap.id,
                query_text=q_text,
                search_type=search_type,
                target_domains=domains,
                rationale=f"Resolve {gap.gap_type} for {subj} ({cat.value}) strictly via official channels."
            ))

        return queries
