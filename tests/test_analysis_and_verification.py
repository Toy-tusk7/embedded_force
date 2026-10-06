"""Unit tests for Static Behavior Analysis and Deterministic Verification."""

from embedforge.engines.engineering import EngineeringPlanner, FirmwareGenerator
from embedforge.engines.build import BuildEngine
from embedforge.engines.analysis import StaticBehaviorAnalyzer
from embedforge.engines.verification import VerificationEngine
from embedforge.models.requirement import RequirementSet
from embedforge.models.knowledge import KnowledgeBase, KnowledgeItem, TrustLevel
from embedforge.models.verification import FindingClassification

def test_static_analysis_and_verification_pass():
    kb = KnowledgeBase()
    kb.items.append(KnowledgeItem(
        id="KNOW-001",
        subject="MPU6050",
        property_name="i2c_primary_address",
        value="0x68",
        source_id="RES-001",
        source_url="https://invensense.tdk.com/datasheet.pdf",
        source_type="DATASHEET",
        provenance="I2C address is 0x68"
    ))
    kb.items.append(KnowledgeItem(
        id="KNOW-002",
        subject="MPU6050",
        property_name="who_am_i_reg",
        value="0x75",
        source_id="RES-001",
        source_url="https://invensense.tdk.com/datasheet.pdf",
        source_type="DATASHEET",
        provenance="WHO_AM_I register is 0x75"
    ))

    req_set = RequirementSet(source_text="Test requirement")
    planner = EngineeringPlanner()
    plan = planner.create_plan(req_set, kb)

    generator = FirmwareGenerator()
    files = generator.generate_firmware(plan, kb)

    builder = BuildEngine()
    build_art = builder.build_firmware(files)
    assert not build_art.has_errors

    analyzer = StaticBehaviorAnalyzer()
    behavior = analyzer.analyze_firmware(files)
    assert len(behavior.all_register_accesses) > 0

    verifier = VerificationEngine()
    report = verifier.verify(req_set, kb, behavior, target_sensor="MPU6050")
    assert report.passed
    assert report.proven_mismatch_count == 0

def test_defect_injection_detection():
    kb = KnowledgeBase()
    kb.items.append(KnowledgeItem(
        id="KNOW-001",
        subject="MPU6050",
        property_name="i2c_primary_address",
        value="0x68",
        source_id="RES-001",
        source_url="https://invensense.tdk.com/datasheet.pdf",
        source_type="DATASHEET",
        provenance="I2C address is 0x68"
    ))

    req_set = RequirementSet(source_text="Test requirement")
    planner = EngineeringPlanner()
    plan = planner.create_plan(req_set, kb)

    # Injected Defect: WRONG_I2C_ADDRESS
    generator = FirmwareGenerator()
    defect_files = generator.generate_firmware(plan, kb, inject_defect="WRONG_I2C_ADDRESS")

    analyzer = StaticBehaviorAnalyzer()
    behavior = analyzer.analyze_firmware(defect_files)

    verifier = VerificationEngine()
    report = verifier.verify(req_set, kb, behavior, target_sensor="MPU6050")

    # MUST detect the proven mismatch!
    assert not report.passed
    assert report.proven_mismatch_count >= 1
    mismatch_findings = [f for f in report.findings if f.classification == FindingClassification.PROVEN_MISMATCH]
    assert len(mismatch_findings) >= 1
    assert mismatch_findings[0].actual_value == "0x77"
    assert mismatch_findings[0].expected_value == "0x68"
