"""Unit tests for Stage 15: Physical Validation and Errata Debug Loop."""

from embedforge.engines.debug import PhysicalDebugEngine
from embedforge.models.debug import HumanObservation, DebugClassification
from embedforge.models.knowledge import KnowledgeBase

def test_hardware_dependent_ad0_pullup():
    engine = PhysicalDebugEngine()
    obs = HumanObservation(
        id="OBS-101",
        session_id="SESS-001",
        target_chip="MPU6050",
        symptom="Device NACKs on address 0x68 during hardware power-on",
        hardware_setup_notes="AD0 pin is tied to 3.3V power rail on breadboard",
        serial_log_snippet="[INIT_FAIL] WHO_AM_I probe at 0x68 failed with timeout"
    )
    diag = engine.diagnose_observation(obs, firmware_files=[], kb=KnowledgeBase())
    
    assert diag.classification == DebugClassification.HARDWARE_DEPENDENT
    assert diag.hardware_action_required
    assert not diag.software_patch_required
    assert "0x69" in diag.root_cause_explanation
    assert "AD0" in diag.recommended_action

def test_hardware_dependent_missing_pullups():
    engine = PhysicalDebugEngine()
    obs = HumanObservation(
        id="OBS-102",
        session_id="SESS-001",
        target_chip="MPU6050",
        symptom="I2C bus hangs on start",
        logic_analyzer_notes="SCL clock active, but SDA stuck low continuously with no pull-up"
    )
    diag = engine.diagnose_observation(obs, firmware_files=[], kb=KnowledgeBase())
    
    assert diag.classification == DebugClassification.HARDWARE_DEPENDENT
    assert "pull-up" in diag.recommended_action.lower()

def test_hardware_dependent_undervoltage():
    engine = PhysicalDebugEngine()
    obs = HumanObservation(
        id="OBS-103",
        session_id="SESS-001",
        target_chip="MPU6050",
        symptom="Chip brownout reset",
        measured_voltage=1.8 # Minimum is 2.375V
    )
    diag = engine.diagnose_observation(obs, firmware_files=[], kb=KnowledgeBase())
    
    assert diag.classification == DebugClassification.HARDWARE_DEPENDENT
    assert "undervoltage" in diag.root_cause_explanation.lower()

def test_unknown_classification_when_insufficient_evidence():
    engine = PhysicalDebugEngine()
    obs = HumanObservation(
        id="OBS-104",
        session_id="SESS-001",
        target_chip="MPU6050",
        symptom="LED is blinking red sometimes",
        hardware_setup_notes="Generic setup"
    )
    diag = engine.diagnose_observation(obs, firmware_files=[], kb=KnowledgeBase())
    
    # Must return UNKNOWN rather than hallucinating a cause
    assert diag.classification == DebugClassification.UNKNOWN
    assert not diag.software_patch_required
