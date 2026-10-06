"""Unit tests for Hardware-Aware Research Planner, Deep Technical Extractor, and Verification Plugins."""

import pytest
from embedforge.models.requirement import InformationGap, RequirementSet
from embedforge.models.research import ResearchSource, SourceType, ApprovalStatus
from embedforge.models.knowledge import KnowledgeBase, KnowledgeItem
from embedforge.models.engineering import EngineeringPlan, HalBoundary
from embedforge.models.verification import FindingClassification
from embedforge.engines.research.planner import (
    ResearchPlanner, classify_component, get_official_domains, ComponentCategory
)
from embedforge.engines.research.extractor import FactExtractor
from embedforge.engines.engineering.generator import FirmwareGenerator
from embedforge.engines.analysis.ast_analyzer import StaticBehaviorAnalyzer
from embedforge.engines.verification.rules import VerificationEngine


def test_component_classification_and_domains():
    # 1. Host MCU
    assert classify_component("AT32F421G8U7") == ComponentCategory.HOST_MCU
    assert classify_component("STM32F401RE") == ComponentCategory.HOST_MCU
    mcu_domains = get_official_domains("AT32F421")
    assert "arterytek.com" in mcu_domains

    # 2. Discrete Gate Driver
    assert classify_component("FD6288Q") == ComponentCategory.GATE_DRIVER
    driver_domains = get_official_domains("FD6288Q")
    assert "fortiortech.com" in driver_domains

    # 3. Digital Peripheral Sensor
    assert classify_component("MPU6050") == ComponentCategory.DIGITAL_PERIPHERAL
    sensor_domains = get_official_domains("MPU6050")
    assert "invensense.tdk.com" in sensor_domains or "tdk.com" in sensor_domains


def test_research_planner_hardware_awareness():
    planner = ResearchPlanner()

    # Gap for MCU should query pinout/timer AF, not I2C address
    mcu_gap = InformationGap(
        id="GAP-MCU-01",
        gap_type="MISSING_PIN_MAPPING",
        target_subject="AT32F421G8U7",
        target_property="pin_mapping",
        description="Missing timer PWM pinout"
    )
    queries = planner.plan_queries([mcu_gap])
    assert len(queries) == 1
    assert "pinout" in queries[0].query_text.lower()
    assert "alternate function" in queries[0].query_text.lower()
    assert "arterytek.com" in queries[0].target_domains

    # Gap for Gate Driver should query dead time and propagation delay
    driver_gap = InformationGap(
        id="GAP-DRV-01",
        gap_type="MISSING_TIMING_SPEC",
        target_subject="FD6288Q",
        target_property="dead_time",
        description="Missing dead-time specs"
    )
    drv_queries = planner.plan_queries([driver_gap])
    assert len(drv_queries) == 1
    assert "dead time" in drv_queries[0].query_text.lower()
    assert "gate driver" in drv_queries[0].query_text.lower()
    assert "fortiortech.com" in drv_queries[0].target_domains


def test_deep_extractor_mcu_and_gate_driver_specs():
    extractor = FactExtractor()

    # Datasheet snippet for AT32F421
    at32_source = ResearchSource(
        id="SRC-AT32-TEST",
        url="https://www.arterytek.com/download/DS/DS_AT32F421_V2.00_EN.pdf",
        title="AT32F421 Datasheet",
        snippet=(
            "AT32F421 Cortex-M4. Advanced Timer TMR1 features complementary channels with dead-time generator. "
            "Pin Alternate Functions: PA8 is TMR1_CH1, PA9 is TMR1_CH2, PA10 is TMR1_CH3, "
            "PA7 is TMR1_CH1C (TMR1_CH1N), PB0 is TMR1_CH2C (TMR1_CH2N), PB1 is TMR1_CH3C (TMR1_CH3N). "
            "PA2 is TMR2_CH3. Dead-time register is TMR1_BKDT. Supports center-aligned PWM. Maximum timer clock is 120MHz."
        ),
        source_type=SourceType.DATASHEET,
        trust_score=0.99,
        approval_status=ApprovalStatus.APPROVED
    )

    facts = extractor.extract_facts_from_source(at32_source, subject="AT32F421")
    fact_map = {f.property_name: f.value for f in facts}

    assert fact_map.get("pin_af_pa8") == "TMR1_CH1"
    assert fact_map.get("pin_af_pa9") == "TMR1_CH2"
    assert fact_map.get("pin_af_pa10") == "TMR1_CH3"
    assert fact_map.get("pin_af_pa7") == "TMR1_CH1C"
    assert fact_map.get("pin_af_pb0") == "TMR1_CH2C"
    assert fact_map.get("pin_af_pb1") == "TMR1_CH3C"
    assert fact_map.get("pin_af_pa2") == "TMR2_CH3"
    assert fact_map.get("dead_time_reg") == "TMR1_BKDT"
    assert fact_map.get("pwm_mode") == "CENTER_ALIGNED"
    assert fact_map.get("max_timer_clock_mhz") == 120

    # Datasheet snippet for FD6288Q
    fd6288_source = ResearchSource(
        id="SRC-FD6288-TEST",
        url="https://www.fortiortech.com/datasheet/FD6288Q_Datasheet_EN.pdf",
        title="FD6288Q Datasheet",
        snippet=(
            "FD6288Q 3-phase gate driver. Inputs HIN/LIN are Active HIGH. "
            "Internal shoot-through protection dead-time is 200ns typical. "
            "Turn-on delay ton=550ns, turn-off delay toff=500ns. Supply voltage 12V nominal."
        ),
        source_type=SourceType.DATASHEET,
        trust_score=0.99,
        approval_status=ApprovalStatus.APPROVED
    )

    drv_facts = extractor.extract_facts_from_source(fd6288_source, subject="FD6288Q")
    drv_map = {f.property_name: f.value for f in drv_facts}

    assert drv_map.get("min_dead_time_ns") == 200
    assert drv_map.get("input_logic") == "ACTIVE_HIGH"
    assert drv_map.get("turn_on_delay_ns") == 550
    assert drv_map.get("turn_off_delay_ns") == 500
    assert drv_map.get("nominal_supply_voltage_v") == 12


def test_shoot_through_safety_verification_plugin():
    kb = KnowledgeBase()
    # Add FD6288Q minimum dead time fact (200ns)
    kb.items.append(KnowledgeItem(
        id="KNOW-DT-01",
        subject="FD6288Q",
        property_name="min_dead_time_ns",
        value=200,
        unit="ns",
        source_id="SRC-001",
        source_url="https://www.fortiortech.com/datasheet/FD6288Q.pdf",
        source_type="DATASHEET",
        provenance="Minimum dead-time is 200ns"
    ))

    req_set = RequirementSet(source_text="AT32F421 + FD6288Q 3-phase BLDC ESC")
    plan = EngineeringPlan(
        id="PLAN-ESC-01",
        target_mcu="AT32F421",
        target_sensor="FD6288Q_BLDC",
        bus_interface="PWM",
        hal_boundary=HalBoundary(
            module_name="ESC_HAL",
            target_mcu="AT32F421",
            peripherals_used=["TMR1", "GPIO"],
            abstracted_functions=["bldc_esc_init"],
            pin_mappings={"HI_A": "PA10", "LI_A": "PB1"}
        ),
        architecture_summary="Complementary center-aligned PWM with hardware dead-time"
    )

    generator = FirmwareGenerator()
    analyzer = StaticBehaviorAnalyzer()
    verifier = VerificationEngine()

    # Case 1: Safe Firmware (500ns dead-time >= 200ns min)
    safe_files = generator.generate_firmware(plan, kb)
    safe_behavior = analyzer.analyze_firmware(safe_files)
    safe_report = verifier.verify(req_set, kb, safe_behavior, target_sensor="FD6288Q_BLDC", target_mcu="AT32F421")

    assert safe_report.passed
    assert safe_report.proven_mismatch_count == 0

    # Case 2: Hazardous Firmware (Inadequate 100ns dead-time < 200ns min -> Shoot-Through Hazard)
    hazard_files = generator.generate_firmware(plan, kb, inject_defect="INADEQUATE_DEAD_TIME")
    hazard_behavior = analyzer.analyze_firmware(hazard_files)
    hazard_report = verifier.verify(req_set, kb, hazard_behavior, target_sensor="FD6288Q_BLDC", target_mcu="AT32F421")

    assert not hazard_report.passed
    assert hazard_report.proven_mismatch_count >= 1
    mismatch_findings = [f for f in hazard_report.findings if f.classification == FindingClassification.PROVEN_MISMATCH]
    assert any("Shoot-Through Hazard" in f.title for f in mismatch_findings)


def test_multi_column_table_and_code_header_extraction():
    extractor = FactExtractor()

    # Case A: Multi-column 2D layout table extracted from datasheet
    table_text = """
    Table 5. AT32F421 Pin Multiplexing and Alternate Function Mapping
    Pin Name   Type   I/O   Alternate Function 1   Alternate Function 2   Additional
    PA8        I/O    FT    TMR1_CH1               MCO                    WKUP1
    PA9        I/O    FT    TMR1_CH2               USART1_TX              -
    PA10       I/O    FT    TMR1_CH3               USART1_RX              -
    PB0        I/O    FT    TMR1_CH2N              -                      ADC1_IN8
    PB1        I/O    FT    TMR1_CH3N              -                      ADC1_IN9
    PA7        I/O    FT    TMR1_CH1N              -                      ADC1_IN7
    PA2        I/O    FT    TMR2_CH3               USART2_TX              ADC1_IN2
    """
    table_source = ResearchSource(
        id="SRC-TBL-01",
        url="https://www.arterytek.com/DS_AT32F421.pdf",
        title="AT32F421 Pinout Table",
        snippet=table_text,
        source_type=SourceType.DATASHEET,
        trust_score=0.99,
        approval_status=ApprovalStatus.APPROVED
    )
    facts = extractor.extract_facts_from_source(table_source, subject="AT32F421")
    fact_map = {f.property_name: f.value for f in facts}

    assert fact_map.get("pin_af_pa8") == "TMR1_CH1"
    assert fact_map.get("pin_af_pa9") == "TMR1_CH2"
    assert fact_map.get("pin_af_pa10") == "TMR1_CH3"
    assert fact_map.get("pin_af_pa7") == "TMR1_CH1N"
    assert fact_map.get("pin_af_pa2") == "TMR2_CH3"

    # Case B: Official GitHub BSP C header file defines
    header_text = """
    #ifndef AT32F421_ESC_BSP_H
    #define AT32F421_ESC_BSP_H

    #define PIN_HI_A        PA10
    #define PIN_HI_B        PA9
    #define PIN_HI_C        PA8
    #define PIN_LI_A        PB1
    #define PIN_LI_B        PB0
    #define PIN_LI_C        PA7
    #define PIN_THROTTLE    PA2
    #define ESC_DEADTIME_NS 500
    #define ESC_PWM_FREQ_HZ 20000

    #endif
    """
    code_source = ResearchSource(
        id="SRC-GH-BSP-01",
        url="https://github.com/ArteryTek/AT32F421_Firmware_Library/bsp.h",
        title="Official AT32 BSP Header",
        snippet=header_text,
        source_type=SourceType.GITHUB_REPOSITORY,
        trust_score=0.95,
        approval_status=ApprovalStatus.APPROVED
    )
    code_facts = extractor.extract_facts_from_source(code_source, subject="AT32F421")
    code_map = {f.property_name: f.value for f in code_facts}

    assert code_map.get("pin_def_hi_a") == "PA10"
    assert code_map.get("pin_def_hi_b") == "PA9"
    assert code_map.get("pin_def_hi_c") == "PA8"
    assert code_map.get("pin_def_li_a") == "PB1"
    assert code_map.get("pin_def_li_b") == "PB0"
    assert code_map.get("pin_def_li_c") == "PA7"
    assert code_map.get("pin_def_throttle") == "PA2"

