"""Unit tests for the Requirement Engine and Gap Detector (Frozen Contract)."""

from embedforge.engines.requirements import RequirementParser, RequirementGapDetector
from embedforge.models.requirement import RequirementCategory

def test_requirement_parser_zero_invention():
    parser = RequirementParser()
    prompt = "Create a driver for MPU6050 over I2C at 100Hz with 3.3V power."
    req_set = parser.parse(prompt)

    # Must extract explicitly stated items
    extracted_cats = {r.category for r in req_set.requirements}
    assert RequirementCategory.FUNCTIONAL in extracted_cats
    assert RequirementCategory.INTERFACE in extracted_cats
    assert RequirementCategory.TIMING in extracted_cats
    assert RequirementCategory.POWER in extracted_cats

    # Gaps MUST be detected because I2C address and register map were NOT given in prompt
    gap_types = [g.gap_type for g in req_set.information_gaps]
    assert "MISSING_DEVICE_ADDRESS" in gap_types
    assert "MISSING_REGISTER_MAP" in gap_types

def test_requirement_parser_no_sensor_named():
    parser = RequirementParser()
    prompt = "Create a driver over SPI."
    req_set = parser.parse(prompt)
    gap_types = [g.gap_type for g in req_set.information_gaps]
    assert "MISSING_TARGET_DEVICE" in gap_types
