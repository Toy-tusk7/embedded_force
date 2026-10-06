"""Engineering planner defining HAL boundaries and memory/timing budgets."""

import re
from typing import Dict, Any, List
from embedforge.models.requirement import RequirementSet
from embedforge.models.knowledge import KnowledgeBase
from embedforge.models.engineering import EngineeringPlan, HalBoundary
from embedforge.runtime.nemotron_client import NemotronClient
from embedforge.core.logger import logger

class EngineeringPlanner:
    """Constructs a deterministic engineering plan and HAL boundaries from approved knowledge."""

    def __init__(self, nemotron: NemotronClient = None):
        self.nemotron = nemotron or NemotronClient()

    def create_plan(
        self,
        req_set: RequirementSet,
        kb: KnowledgeBase,
        target_mcu: str = "STM32F401RE",
        target_sensor: str = "MPU6050"
    ) -> EngineeringPlan:
        # Dynamically detect hardware targets if default values were supplied
        src_upper = req_set.source_text.upper()
        if target_mcu == "STM32F401RE" and "AT32" in src_upper:
            target_mcu = "AT32F421"
        if target_sensor == "MPU6050":
            if "FD6288" in src_upper or "ESC" in src_upper or "BLDC" in src_upper:
                target_sensor = "FD6288Q_BLDC"
            elif "BME280" in src_upper:
                target_sensor = "BME280"

        logger.log_event("PLANNING", f"Synthesizing engineering plan for {target_sensor} on {target_mcu}")

        is_esc = any(k in target_sensor.upper() or k in target_mcu.upper() for k in ("BLDC", "ESC", "FD6288", "MOTOR"))

        if is_esc:
            hal_boundary = HalBoundary(
                module_name="bldc_esc_driver",
                target_mcu=target_mcu,
                peripherals_used=["TMR1", "GPIO"],
                abstracted_functions=[
                    "bldc_esc_init()",
                    "bldc_esc_set_duty(uint8_t phase, uint16_t duty)",
                    "bldc_esc_disable_all()"
                ],
                pin_mappings={
                    "HI_A": "PA10",
                    "HI_B": "PA9",
                    "HI_C": "PA8",
                    "LI_A": "PB1",
                    "LI_B": "PB0",
                    "LI_C": "PA7",
                    "THROTTLE": "PA2"
                }
            )
            plan = EngineeringPlan(
                id="PLAN-001",
                target_mcu=target_mcu,
                target_sensor=target_sensor,
                bus_interface="PWM",
                hal_boundary=hal_boundary,
                memory_budget_bytes=64 * 1024,
                clock_frequency_hz=120_000_000,
                timing_constraints_ms={
                    "dead_time_ns": 500.0,
                    "pwm_freq_hz": 20000.0
                },
                architecture_summary=(
                    f"Layered Embedded Motor Control Architecture:\n"
                    f"1. Application Layer (main.c): High-frequency commutation and throttle polling.\n"
                    f"2. Gate Driver Layer (bldc_esc_driver.c/.h): 3-phase half-bridge gate control.\n"
                    f"3. Hardware Timer Layer (TMR1): Advanced complementary PWM with hardware dead-time generator."
                ),
                implementation_steps=[
                    "Configure GPIO alternate function mux for high-side and low-side pins.",
                    "Initialize Advanced Timer TMR1 center-aligned counter at 20 kHz.",
                    "Configure hardware dead-time generator (BKDT/BDTR) for shoot-through safety.",
                    "Enable complementary channel outputs and break input protection.",
                    "Implement throttle input capture on PA2."
                ]
            )
            return plan

        # Look up approved facts from KnowledgeBase for sensor targets
        addr_fact = kb.find_fact(target_sensor, "i2c_primary_address")
        i2c_addr = addr_fact.value if addr_fact else "0x68"

        whoami_fact = kb.find_fact(target_sensor, "who_am_i_reg")
        whoami_reg = whoami_fact.value if whoami_fact else "0x75"

        delay_fact = kb.find_fact(target_sensor, "startup_delay_ms")
        startup_ms = delay_fact.value if delay_fact else 30

        hal_boundary = HalBoundary(
            module_name=f"{target_sensor.lower()}_driver",
            target_mcu=target_mcu,
            peripherals_used=["I2C1", "USART2", "GPIO"],
            abstracted_functions=[
                f"{target_sensor.lower()}_i2c_write(uint8_t reg, uint8_t val)",
                f"{target_sensor.lower()}_i2c_read(uint8_t reg, uint8_t *data, uint16_t len)",
                f"{target_sensor.lower()}_delay_ms(uint32_t ms)"
            ],
            pin_mappings={
                "I2C1_SCL": "PB8",
                "I2C1_SDA": "PB9",
                "USART2_TX": "PA2",
                "USART2_RX": "PA3"
            }
        )

        plan = EngineeringPlan(
            id="PLAN-001",
            target_mcu=target_mcu,
            target_sensor=target_sensor,
            bus_interface="I2C",
            hal_boundary=hal_boundary,
            memory_budget_bytes=128 * 1024,
            clock_frequency_hz=84_000_000,
            timing_constraints_ms={
                "startup_delay_ms": float(startup_ms),
                "i2c_timeout_ms": 100.0,
                "sample_interval_ms": 10.0 # 100 Hz
            },
            architecture_summary=(
                f"Layered Embedded Architecture:\n"
                f"1. Application Layer (main.c): Periodic telemetry loop at 100Hz.\n"
                f"2. Device Driver Layer ({target_sensor.lower()}.c/.h): Register ops (Addr={i2c_addr}, WHO_AM_I={whoami_reg}).\n"
                f"3. Hardware Abstraction Layer (hal_i2c.c/.h): {target_mcu} I2C1 register/HAL mappings."
            ),
            implementation_steps=[
                "Define register definitions and bitmasks matching approved datasheet.",
                "Implement hardware abstraction layer (HAL) for I2C and UART.",
                "Implement driver init, wake-up sequence, and who-am-i verification.",
                "Implement raw sensor reading function with error-checking.",
                "Implement main polling loop with defensive timeout checks."
            ]
        )

        return plan
