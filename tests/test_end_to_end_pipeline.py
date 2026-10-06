"""End-to-end integration tests for EmbedForge pipeline with autonomous repair."""

from embedforge.pipeline import EmbedForgePipeline

def test_full_pipeline_nominal_flow():
    pipeline = EmbedForgePipeline(project_name="Test_Nominal_Flow")
    prompt = "Create an STM32 driver for MPU6050 over I2C at 100Hz with 30ms startup delay."
    results = pipeline.run(user_prompt=prompt, inject_initial_defect=None)

    assert results["verification_report"].passed
    assert len(results["firmware_files"]) >= 3
    assert len(results["approved_knowledge"].items) > 0
    assert "REQ-001" in results["lineage_tree"]
    assert "PLAN-001" in results["lineage_tree"]

def test_full_pipeline_defect_repair_and_reverification():
    pipeline = EmbedForgePipeline(project_name="Test_Defect_Repair_Flow")
    prompt = "Create an STM32 driver for MPU6050 over I2C at 100Hz with 30ms startup delay."
    # Injected Defect: WRONG_I2C_ADDRESS
    results = pipeline.run(user_prompt=prompt, inject_initial_defect="WRONG_I2C_ADDRESS")

    # The repair session must have executed and reverification must pass
    assert results["repair_session"] is not None
    assert results["repair_session"].success
    assert results["verification_report"].passed
    assert "FW-002" in results["lineage_tree"]
    assert "BUILD-FW-002" in results["lineage_tree"]


def test_full_pipeline_bldc_esc_flow():
    pipeline = EmbedForgePipeline(project_name="Test_BLDC_ESC_Flow")
    prompt = (
        "Develop initial firmware for custom 3-phase BLDC ESC on AT32F421G8U7 with FD6288Q gate driver. "
        "High-side pins: PA10, PA9, PA8. Low-side pins: PB1, PB0, PA7. Throttle: PA2. "
        "PWM frequency 20kHz center-aligned, 500ns dead-time."
    )
    results = pipeline.run(user_prompt=prompt, inject_initial_defect=None)

    assert results["verification_report"].passed
    assert len(results["firmware_files"]) >= 3
    assert any("bldc_esc" in f.filename for f in results["firmware_files"])
    assert results["plan"].bus_interface == "PWM"
    assert "TMR1" in results["plan"].hal_boundary.peripherals_used

