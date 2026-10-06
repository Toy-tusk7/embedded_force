"""Firmware code generator producing structured, buildable C embedded source files."""

from typing import List, Optional
from embedforge.models.engineering import EngineeringPlan, FirmwareFile, FileType
from embedforge.models.knowledge import KnowledgeBase
from embedforge.core.logger import logger

class FirmwareGenerator:
    """Generates modular, HAL-separated C firmware adhering strictly to approved knowledge."""

    def generate_firmware(
        self,
        plan: EngineeringPlan,
        kb: KnowledgeBase,
        inject_defect: Optional[str] = None
    ) -> List[FirmwareFile]:
        """Generates C headers and source files for the target embedded project."""
        logger.log_event("GENERATION", f"Generating C firmware for {plan.target_sensor} on {plan.target_mcu} (Defect: {inject_defect or 'None'})")

        sensor_upper = plan.target_sensor.upper()
        mcu_upper = plan.target_mcu.upper()

        is_esc = any(k in sensor_upper or k in mcu_upper for k in ("BLDC", "ESC", "FD6288", "MOTOR", "AT32"))

        if is_esc:
            return self._generate_bldc_esc_firmware(plan, kb, inject_defect)
        else:
            return self._generate_sensor_firmware(plan, kb, inject_defect)

    def _generate_sensor_firmware(
        self,
        plan: EngineeringPlan,
        kb: KnowledgeBase,
        inject_defect: Optional[str] = None
    ) -> List[FirmwareFile]:
        sensor = plan.target_sensor.upper()
        sensor_lower = plan.target_sensor.lower()

        # Retrieve facts from knowledge base
        addr_fact = kb.find_fact(sensor, "i2c_primary_address")
        i2c_addr = addr_fact.value if addr_fact else "0x68"

        whoami_fact = kb.find_fact(sensor, "who_am_i_reg")
        whoami_reg = whoami_fact.value if whoami_fact else "0x75"

        pwr_fact = kb.find_fact(sensor, "pwr_mgmt_reg")
        pwr_reg = pwr_fact.value if pwr_fact else "0x6B"

        # Apply defect injection if specified for evaluation benchmark
        if inject_defect == "WRONG_I2C_ADDRESS":
            i2c_addr = "0x77" # Injected mismatch defect!
        elif inject_defect == "WRONG_WHO_AM_I_REG":
            whoami_reg = "0x00" # Injected mismatch defect!

        files: List[FirmwareFile] = []

        # 1. Header file: include/mpu6050.h
        header_code = f"""/**
 * @file {sensor_lower}.h
 * @brief EmbedForge Generated Hardware Driver for {sensor}
 * @provenance Auto-generated from Knowledge Base (KNOW-001)
 */

#ifndef {sensor}_H
#define {sensor}_H

#include <stdint.h>
#include <stdbool.h>

#define {sensor}_I2C_ADDR         {i2c_addr}
#define {sensor}_REG_SMPLRT_DIV    0x19
#define {sensor}_REG_PWR_MGMT_1    {pwr_reg}
#define {sensor}_REG_WHO_AM_I      {whoami_reg}
#define {sensor}_REG_ACCEL_XOUT_H  0x3B

typedef struct {{
    int16_t accel_x;
    int16_t accel_y;
    int16_t accel_z;
    int16_t gyro_x;
    int16_t gyro_y;
    int16_t gyro_z;
    int16_t temp;
}} {sensor}_RawData_t;

typedef enum {{
    {sensor}_OK = 0,
    {sensor}_ERR_I2C_TIMEOUT = -1,
    {sensor}_ERR_DEVICE_NOT_FOUND = -2,
    {sensor}_ERR_INVALID_PARAM = -3
}} {sensor}_Status_t;

// Driver API Prototypes
{sensor}_Status_t {sensor_lower}_init(void);
{sensor}_Status_t {sensor_lower}_read_raw({sensor}_RawData_t *data);
{sensor}_Status_t {sensor_lower}_check_id(void);

#endif // {sensor}_H
"""
        files.append(FirmwareFile(
            filename=f"{sensor_lower}.h",
            relative_path=f"include/{sensor_lower}.h",
            content=header_code,
            file_type=FileType.C_HEADER
        ))

        # 2. Source file: src/mpu6050.c
        timeout_code = "100" if inject_defect != "MISSING_TIMEOUT" else "0"
        src_code = f"""/**
 * @file {sensor_lower}.c
 * @brief EmbedForge Driver Implementation for {sensor}
 */

#include "{sensor_lower}.h"

// Mock HAL I2C declarations for verification and static analysis
extern int hal_i2c_write(uint8_t addr, uint8_t reg, uint8_t val, uint32_t timeout_ms);
extern int hal_i2c_read(uint8_t addr, uint8_t reg, uint8_t *buf, uint16_t len, uint32_t timeout_ms);
extern void hal_delay_ms(uint32_t ms);

{sensor}_Status_t {sensor_lower}_init(void) {{
    uint8_t id = 0;
    
    // Reset/Wakeup device: Clear sleep bit in PWR_MGMT_1
    if (hal_i2c_write({sensor}_I2C_ADDR, {sensor}_REG_PWR_MGMT_1, 0x00, {timeout_code}) != 0) {{
        return {sensor}_ERR_I2C_TIMEOUT;
    }}
    hal_delay_ms({plan.timing_constraints_ms.get('startup_delay_ms', 30.0):.0f});

    // Verify Device WHO_AM_I identity
    if ({sensor_lower}_check_id() != {sensor}_OK) {{
        return {sensor}_ERR_DEVICE_NOT_FOUND;
    }}

    return {sensor}_OK;
}}

{sensor}_Status_t {sensor_lower}_check_id(void) {{
    uint8_t id = 0;
    if (hal_i2c_read({sensor}_I2C_ADDR, {sensor}_REG_WHO_AM_I, &id, 1, {timeout_code}) != 0) {{
        return {sensor}_ERR_I2C_TIMEOUT;
    }}
    if (id != 0x68) {{
        return {sensor}_ERR_DEVICE_NOT_FOUND;
    }}
    return {sensor}_OK;
}}

{sensor}_Status_t {sensor_lower}_read_raw({sensor}_RawData_t *data) {{
    uint8_t buffer[14];
    if (data == 0) return {sensor}_ERR_INVALID_PARAM;

    if (hal_i2c_read({sensor}_I2C_ADDR, {sensor}_REG_ACCEL_XOUT_H, buffer, 14, {timeout_code}) != 0) {{
        return {sensor}_ERR_I2C_TIMEOUT;
    }}

    data->accel_x = (int16_t)((buffer[0] << 8) | buffer[1]);
    data->accel_y = (int16_t)((buffer[2] << 8) | buffer[3]);
    data->accel_z = (int16_t)((buffer[4] << 8) | buffer[5]);
    data->temp    = (int16_t)((buffer[6] << 8) | buffer[7]);
    data->gyro_x  = (int16_t)((buffer[8] << 8) | buffer[9]);
    data->gyro_y  = (int16_t)((buffer[10] << 8) | buffer[11]);
    data->gyro_z  = (int16_t)((buffer[12] << 8) | buffer[13]);

    return {sensor}_OK;
}}
"""
        files.append(FirmwareFile(
            filename=f"{sensor_lower}.c",
            relative_path=f"src/{sensor_lower}.c",
            content=src_code,
            file_type=FileType.C_SOURCE
        ))

        # 3. Main Application file: src/main.c
        main_code = f"""/**
 * @file main.c
 * @brief EmbedForge Application Entry Point
 */

#include "{sensor_lower}.h"

int main(void) {{
    {sensor}_RawData_t imu_data;
    
    // Initialize sensor driver
    if ({sensor_lower}_init() != {sensor}_OK) {{
        // Handle init failure
        while(1);
    }}

    while(1) {{
        if ({sensor_lower}_read_raw(&imu_data) == {sensor}_OK) {{
            // Data successfully acquired
        }}
    }}
    return 0;
}}
"""
        files.append(FirmwareFile(
            filename="main.c",
            relative_path="src/main.c",
            content=main_code,
            file_type=FileType.C_SOURCE
        ))

        return files

    def _generate_bldc_esc_firmware(
        self,
        plan: EngineeringPlan,
        kb: KnowledgeBase,
        inject_defect: Optional[str] = None
    ) -> List[FirmwareFile]:
        """Generates 3-phase BLDC ESC firmware driver with Advanced Timer complementary PWM."""
        files: List[FirmwareFile] = []

        dead_time_ns = 500
        if inject_defect == "INADEQUATE_DEAD_TIME":
            dead_time_ns = 100 # Injected shoot-through risk defect!

        pwm_freq_hz = 20000
        if inject_defect == "EXCESSIVE_PWM_FREQ":
            pwm_freq_hz = 150000

        header_code = f"""/**
 * @file bldc_esc_driver.h
 * @brief 3-Phase BLDC ESC Motor Control Driver for AT32F421 & FD6288Q
 * @provenance Auto-generated from Knowledge Base (KNOW-001)
 */

#ifndef BLDC_ESC_DRIVER_H
#define BLDC_ESC_DRIVER_H

#include <stdint.h>
#include <stdbool.h>

// PWM Timing Specifications
#define ESC_PWM_FREQ_HZ         {pwm_freq_hz}
#define ESC_DEADTIME_NS         {dead_time_ns}

// Gate Control Pin Mappings (AT32F421G8U7 -> FD6288Q)
#define PIN_HI_A                PA10
#define PIN_HI_B                PA9
#define PIN_HI_C                PA8
#define PIN_LI_A                PB1
#define PIN_LI_B                PB0
#define PIN_LI_C                PA7
#define PIN_THROTTLE            PA2

typedef enum {{
    ESC_OK = 0,
    ESC_ERR_COMMUTATION = -1,
    ESC_ERR_HARDWARE_FAULT = -2
}} ESC_Status_t;

// Driver API
ESC_Status_t bldc_esc_init(void);
void bldc_esc_set_duty(uint8_t phase, uint16_t duty);
void bldc_esc_disable_all(void);

#endif // BLDC_ESC_DRIVER_H
"""
        files.append(FirmwareFile(
            filename="bldc_esc_driver.h",
            relative_path="include/bldc_esc_driver.h",
            content=header_code,
            file_type=FileType.C_HEADER
        ))

        src_code = f"""/**
 * @file bldc_esc_driver.c
 * @brief 3-Phase BLDC ESC Driver Implementation with Advanced Timer Complementary PWM
 */

#include "bldc_esc_driver.h"

// Hardware Timer register mock / CMSIS hooks for AT32F421 TMR1
extern void tmr1_pwm_init(uint32_t freq_hz, uint32_t dead_time_ns);
extern void tmr1_set_channel_duty(uint8_t channel, uint16_t duty);

ESC_Status_t bldc_esc_init(void) {{
    // Initialize TMR1 complementary center-aligned PWM with dead-time generator
    tmr1_pwm_init(ESC_PWM_FREQ_HZ, ESC_DEADTIME_NS);
    return ESC_OK;
}}

void bldc_esc_set_duty(uint8_t phase, uint16_t duty) {{
    tmr1_set_channel_duty(phase, duty);
}}

void bldc_esc_disable_all(void) {{
    tmr1_set_channel_duty(1, 0);
    tmr1_set_channel_duty(2, 0);
    tmr1_set_channel_duty(3, 0);
}}
"""
        files.append(FirmwareFile(
            filename="bldc_esc_driver.c",
            relative_path="src/bldc_esc_driver.c",
            content=src_code,
            file_type=FileType.C_SOURCE
        ))

        main_code = """/**
 * @file main.c
 * @brief ESC Application Entry Point
 */

#include "bldc_esc_driver.h"

int main(void) {
    if (bldc_esc_init() != ESC_OK) {
        while(1);
    }

    while(1) {
        // Main ESC commutation and throttle loop
    }
    return 0;
}
"""
        files.append(FirmwareFile(
            filename="main.c",
            relative_path="src/main.c",
            content=main_code,
            file_type=FileType.C_SOURCE
        ))

        return files
