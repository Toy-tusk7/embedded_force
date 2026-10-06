"""Evaluation fixture definitions with injected defects for benchmark scorecard."""

from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class BenchmarkScenario(BaseModel):
    id: str
    name: str
    description: str
    prompt: str
    injected_defect: Optional[str] = None
    expected_finding_classification: str # PROVEN_MISMATCH | INFERRED_ISSUE | HARDWARE_DEPENDENT | NONE
    expected_repaired_file: Optional[str] = None

BENCHMARK_SUITE: List[BenchmarkScenario] = [
    BenchmarkScenario(
        id="BENCH-01",
        name="Wrong I2C Slave Address (Silicon Mismatch)",
        description="Firmware uses address 0x77 instead of verified datasheet address 0x68.",
        prompt="Create an STM32 driver for MPU6050 accelerometer over I2C at 100Hz with 30ms startup delay.",
        injected_defect="WRONG_I2C_ADDRESS",
        expected_finding_classification="PROVEN_MISMATCH",
        expected_repaired_file="mpu6050.h"
    ),
    BenchmarkScenario(
        id="BENCH-02",
        name="Wrong WHO_AM_I Register Address",
        description="Firmware reads register 0x00 instead of 0x75 to verify device identity.",
        prompt="Create an STM32 driver for MPU6050 accelerometer over I2C at 100Hz with 30ms startup delay.",
        injected_defect="WRONG_WHO_AM_I_REG",
        expected_finding_classification="PROVEN_MISMATCH",
        expected_repaired_file="mpu6050.h"
    ),
    BenchmarkScenario(
        id="BENCH-03",
        name="Missing Bus Timeout (Defensive Coding)",
        description="I2C calls omit timeout parameter, risking infinite bus lockup.",
        prompt="Create an STM32 driver for MPU6050 accelerometer over I2C at 100Hz with 30ms startup delay.",
        injected_defect="MISSING_TIMEOUT",
        expected_finding_classification="INFERRED_ISSUE",
        expected_repaired_file=None
    ),
    BenchmarkScenario(
        id="BENCH-04",
        name="Nominal Conformance Case",
        description="Firmware perfectly conforms to approved datasheet with 0 defects.",
        prompt="Create an STM32 driver for MPU6050 accelerometer over I2C at 100Hz with 30ms startup delay.",
        injected_defect=None,
        expected_finding_classification="NONE",
        expected_repaired_file=None
    )
]
