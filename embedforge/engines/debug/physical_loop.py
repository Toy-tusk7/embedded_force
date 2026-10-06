"""Physical hardware debug loop and errata investigation engine."""

import re
from typing import List, Optional
from embedforge.models.debug import (
    HumanObservation, DebugDiagnosis, DebugClassification, ErrataItem
)
from embedforge.models.engineering import FirmwareFile
from embedforge.models.knowledge import KnowledgeBase
from embedforge.engines.research.tavily_client import TavilyResearchClient
from embedforge.models.research import ResearchQuery
from embedforge.core.logger import logger

class PhysicalDebugEngine:
    """Investigates real physical hardware observations using Tavily errata search and deterministic logic."""

    def __init__(self, tavily_client: Optional[TavilyResearchClient] = None):
        self.tavily = tavily_client or TavilyResearchClient()

    def diagnose_observation(
        self,
        obs: HumanObservation,
        firmware_files: List[FirmwareFile],
        kb: KnowledgeBase
    ) -> DebugDiagnosis:
        logger.log_event("PHYSICAL_DEBUG", f"Investigating physical observation [{obs.id}] for chip '{obs.target_chip}'")

        symptom_lower = obs.symptom.lower()
        notes_lower = (obs.logic_analyzer_notes or "").lower() + " " + (obs.hardware_setup_notes or "").lower()
        log_lower = (obs.serial_log_snippet or "").lower()

        # 1. Check for Hardware-Dependent Pin / Electrical condition: AD0 pin state or missing pull-ups
        if "ad0" in notes_lower or "0x69" in log_lower or "address not responding" in symptom_lower or "nack" in symptom_lower:
            if "high" in notes_lower or "3.3v" in notes_lower or "0x69" in log_lower:
                return DebugDiagnosis(
                    id=f"DIAG-{obs.id}",
                    observation_id=obs.id,
                    classification=DebugClassification.HARDWARE_DEPENDENT,
                    root_cause_explanation=(
                        f"Physical hardware diagnosis for {obs.target_chip}: The AD0 (address select) pin is pulled HIGH (3.3V) on the physical PCB/breakout, "
                        f"which changes the internal I2C slave address from default 0x68 to 0x69."
                    ),
                    recommended_action=(
                        "Physical Action: Tie the AD0 pin to GND (0V) to select 0x68, or update firmware configuration to target secondary address 0x69."
                    ),
                    software_patch_required=False,
                    hardware_action_required=True,
                    confidence=0.95
                )

        if "pull-up" in notes_lower or "sda stuck low" in notes_lower or "bus hang" in symptom_lower:
            return DebugDiagnosis(
                id=f"DIAG-{obs.id}",
                observation_id=obs.id,
                classification=DebugClassification.HARDWARE_DEPENDENT,
                root_cause_explanation=(
                    f"I2C physical bus lockup detected on {obs.target_chip}. Missing or insufficient pull-up resistors (typically 2.2kΩ - 4.7kΩ) "
                    f"on SDA/SCL lines, or a slave device is holding SDA low."
                ),
                recommended_action=(
                    "Physical Action: Ensure external 4.7kΩ pull-up resistors are installed between SDA/SCL lines and VDD (3.3V). "
                    "Optionally toggle SCL line 9 times in firmware to clear slave bus lock."
                ),
                software_patch_required=False,
                hardware_action_required=True,
                confidence=0.92
            )

        # 2. Check for Voltage / Power-supply boundary violations
        if obs.measured_voltage is not None:
            if obs.measured_voltage < 2.37:
                return DebugDiagnosis(
                    id=f"DIAG-{obs.id}",
                    observation_id=obs.id,
                    classification=DebugClassification.HARDWARE_DEPENDENT,
                    root_cause_explanation=(
                        f"Undervoltage condition: Measured supply voltage ({obs.measured_voltage}V) is below the minimum operating threshold (2.375V) for {obs.target_chip}."
                    ),
                    recommended_action="Physical Action: Verify power supply rail and provide stable 3.3V to VDD pin.",
                    software_patch_required=False,
                    hardware_action_required=True,
                    confidence=0.98
                )

        # 3. Query Tavily for Silicon Errata if complex bus issue
        errata_items: List[ErrataItem] = []
        if "errata" in symptom_lower or "analog filter" in symptom_lower or "dma lock" in symptom_lower:
            query = ResearchQuery(
                id=f"QRY-ERRATA-{obs.id}",
                target_gap_id=obs.id,
                query_text=f"{obs.target_chip} silicon errata limitation I2C workaround",
                rationale="Search manufacturer errata for known silicon bug."
            )
            res = self.tavily.search_for_gap(query, max_results=2)
            for s in res.sources:
                errata_items.append(ErrataItem(
                    id=f"ERRATA-{abs(hash(s.url)) % 1000:03d}",
                    chip=obs.target_chip,
                    title=s.title,
                    description=s.snippet,
                    official_workaround="Follow manufacturer errata sequence for peripheral clock enable.",
                    source_url=s.url
                ))

            return DebugDiagnosis(
                id=f"DIAG-{obs.id}",
                observation_id=obs.id,
                classification=DebugClassification.SILICON_ERRATA,
                root_cause_explanation=f"Known silicon limitation identified for {obs.target_chip}.",
                recommended_action="Apply manufacturer recommended errata workaround in initialization sequence.",
                errata_references=errata_items,
                software_patch_required=True,
                hardware_action_required=False,
                confidence=0.88
            )

        # 4. Fallback when evidence is insufficient
        return DebugDiagnosis(
            id=f"DIAG-{obs.id}",
            observation_id=obs.id,
            classification=DebugClassification.UNKNOWN,
            root_cause_explanation="Insufficient physical telemetry or logs to determine definitive root cause without guessing.",
            recommended_action="Request logic analyzer capture or oscilloscope trace of SDA/SCL lines during power-on.",
            software_patch_required=False,
            hardware_action_required=False,
            confidence=0.50
        )
