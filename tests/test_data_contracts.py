"""Unit tests for all core Pydantic data contracts."""

import pytest
from embedforge.models.requirement import RequirementItem, RequirementCategory, InformationGap, RequirementSet
from embedforge.models.research import ResearchSource, SourceType, ApprovalStatus
from embedforge.models.knowledge import KnowledgeItem, TrustLevel, KnowledgeBase
from embedforge.models.verification import Finding, FindingClassification, VerificationReport

def test_requirement_set_contract():
    req = RequirementItem(
        id="REQ-001",
        category=RequirementCategory.INTERFACE,
        description="I2C bus specified",
        explicit_facts={"bus_interface": "I2C"},
        raw_source_phrase="I2C"
    )
    gap = InformationGap(
        id="GAP-001",
        gap_type="MISSING_DEVICE_ADDRESS",
        target_subject="MPU6050",
        target_property="device_address",
        description="I2C address missing"
    )
    rs = RequirementSet(
        source_text="Test prompt",
        requirements=[req],
        information_gaps=[gap]
    )
    assert len(rs.requirements) == 1
    assert len(rs.information_gaps) == 1
    assert rs.requirements[0].category == RequirementCategory.INTERFACE

def test_knowledge_base_contract():
    kb = KnowledgeBase()
    k_item = KnowledgeItem(
        id="KNOW-001",
        subject="MPU6050",
        property_name="i2c_primary_address",
        value="0x68",
        source_id="RES-001",
        source_url="https://invensense.tdk.com/datasheet.pdf",
        source_type="DATASHEET",
        provenance="I2C address is 0x68",
        trust_level=TrustLevel.HIGH_OFFICIAL
    )
    kb.items.append(k_item)
    assert kb.find_fact("MPU6050", "i2c_primary_address") is not None
    assert kb.find_fact("MPU6050", "i2c_primary_address").value == "0x68"

def test_verification_report_contract():
    finding = Finding(
        id="FIND-001",
        rule_id="RULE-001",
        classification=FindingClassification.PROVEN_MISMATCH,
        title="Mismatch detected",
        description="Address 0x77 != 0x68",
        file="mpu6050.h",
        line=10,
        expected_value="0x68",
        actual_value="0x77"
    )
    report = VerificationReport(
        firmware_id="FW-001",
        behavior_id="BEHAV-001",
        findings=[finding],
        proven_mismatch_count=1,
        passed=False
    )
    assert not report.passed
    assert report.proven_mismatch_count == 1
