"""DM388 drivers for the verify benchmark: one shared driver core (init sequence, calibration, temperature conversion) on
top of a bus layer per framework, and the mutations (bugs) verify has to catch. `@F(key)@` stands for the FACT id the
ground truth recorded for `key`."""

from typing import Dict, List, Tuple

CORE = r"""
#define DM388_CHIP_ID_REG 0x00   /* @F(chip_id_reg)@ */
#define DM388_CHIP_ID     0x50   /* @F(chip_id)@ */
#define DM388_CMD_REG     0x7E   /* @F(cmd_reg)@ */
#define DM388_SOFTRESET   0xB6   /* @F(softreset)@ */
#define DM388_STARTUP_MS  2      /* @F(startup)@ */
#define DM388_CALIB_REG   0x31   /* @F(calib_reg)@ */
#define DM388_CALIB_LEN   21     /* @F(calib_len)@ */
#define DM388_OSR_REG     0x1C   /* @F(osr_reg)@ */
#define DM388_OSR_VALUE   0x0B   /* @F(osr_value)@ */
#define DM388_ODR_REG     0x1D   /* @F(odr_reg)@ */
#define DM388_ODR_VALUE   0x02   /* @F(odr_value)@ */
#define DM388_PWR_REG     0x1B   /* @F(pwr_reg)@ */
#define DM388_PWR_NORMAL  0x33   /* @F(pwr_normal)@ */
#define DM388_DATA_REG    0x04   /* @F(data_reg)@ */
#define DM388_DATA_LEN    6      /* @F(data_len)@ */

static float par_t1, par_t2, par_t3;

int dm388_init(void) {
    uint8_t id = 0;
    write_reg(DM388_CMD_REG, DM388_SOFTRESET);
    delay_ms(DM388_STARTUP_MS);
    if (read_regs(DM388_CHIP_ID_REG, &id, 1) != 0) return -1;
    if (id != DM388_CHIP_ID) return -2;
    uint8_t c[DM388_CALIB_LEN];
    if (read_regs(DM388_CALIB_REG, c, DM388_CALIB_LEN) != 0) return -1;
    uint16_t nvm_par_t1 = (uint16_t)(((uint16_t)c[1] << 8) | c[0]);
    uint16_t nvm_par_t2 = (uint16_t)(((uint16_t)c[3] << 8) | c[2]);
    int8_t nvm_par_t3 = (int8_t)c[4];
    par_t1 = (float)nvm_par_t1 / powf(2.0f, -8.0f);
    par_t2 = (float)nvm_par_t2 / powf(2.0f, 30.0f);
    par_t3 = (float)nvm_par_t3 / powf(2.0f, 48.0f);
    write_reg(DM388_OSR_REG, DM388_OSR_VALUE);
    write_reg(DM388_ODR_REG, DM388_ODR_VALUE);
    write_reg(DM388_PWR_REG, DM388_PWR_NORMAL);
    return 0;
}

float dm388_read_temperature(void) {
    uint8_t d[DM388_DATA_LEN];
    read_regs(DM388_DATA_REG, d, DM388_DATA_LEN);
    uint32_t uncomp_temp = ((uint32_t)d[5] << 16) | ((uint32_t)d[4] << 8) | (uint32_t)d[3];
    float partial_data1 = (float)uncomp_temp - par_t1;
    float partial_data2 = partial_data1 * par_t2;
    return partial_data2 + (partial_data1 * partial_data1) * par_t3;
}
"""

C_MAIN = r"""
int main(void) {
    if (dm388_init() != 0) return 1;
    float t = dm388_read_temperature();
    printf("T = %f\n", t);
    return 0;
}
"""

# framework -> (file name, bus layer before the core, entry code after it)
FRAMEWORKS: Dict[str, Tuple[str, str, str]] = {
    "pico_i2c": ("main.c", r"""
#include <stdio.h>
#include <math.h>
#include "pico/stdlib.h"
#include "hardware/i2c.h"
#define DM388_ADDR 0x76   /* @F(addr)@ */
static int write_reg(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return i2c_write_blocking(i2c0, DM388_ADDR, buf, 2, false) == 2 ? 0 : -1;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    if (i2c_write_blocking(i2c0, DM388_ADDR, &reg, 1, true) != 1) return -1;
    return i2c_read_blocking(i2c0, DM388_ADDR, buf, len, false) == (int)len ? 0 : -1;
}
static void delay_ms(uint32_t ms) { sleep_ms(ms); }
""", C_MAIN),

    "pico_spi": ("main.c", r"""
#include <stdio.h>
#include <math.h>
#include "pico/stdlib.h"
#include "hardware/spi.h"
#include "hardware/gpio.h"
#define DM388_CS 17
static int write_reg(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {(uint8_t)(reg & 0x7F), val};
    gpio_put(DM388_CS, 0);
    spi_write_blocking(spi0, buf, 2);
    gpio_put(DM388_CS, 1);
    return 0;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    uint8_t cmd = reg | 0x80;
    uint8_t tmp[32];
    gpio_put(DM388_CS, 0);
    spi_write_blocking(spi0, &cmd, 1);
    spi_read_blocking(spi0, 0, tmp, len + 1);   /* the first byte is the dummy byte */
    gpio_put(DM388_CS, 1);
    for (size_t i = 0; i < len; i++) buf[i] = tmp[i + 1];
    return 0;
}
static void delay_ms(uint32_t ms) { sleep_ms(ms); }
""", C_MAIN),

    "arduino_wire": ("main.cpp", r"""
#include <Arduino.h>
#include <Wire.h>
#include <math.h>
#define DM388_ADDR 0x76   /* @F(addr)@ */
static int write_reg(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(DM388_ADDR);
    Wire.write(reg);
    Wire.write(val);
    return Wire.endTransmission() == 0 ? 0 : -1;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    Wire.beginTransmission(DM388_ADDR);
    Wire.write(reg);
    if (Wire.endTransmission(false) != 0) return -1;
    if (Wire.requestFrom((uint8_t)DM388_ADDR, (uint8_t)len) != len) return -1;
    for (size_t i = 0; i < len; i++) buf[i] = Wire.read();
    return 0;
}
static void delay_ms(uint32_t ms) { delay(ms); }
""", r"""
void setup() {
    Serial.begin(115200);
    Wire.begin();
    dm388_init();
}
void loop() {
    Serial.println(dm388_read_temperature());
    delay(1000);
}
"""),

    "arduino_spi": ("main.cpp", r"""
#include <Arduino.h>
#include <SPI.h>
#include <math.h>
#define DM388_CS 10
static int write_reg(uint8_t reg, uint8_t val) {
    digitalWrite(DM388_CS, LOW);
    SPI.transfer(reg & 0x7F);
    SPI.transfer(val);
    digitalWrite(DM388_CS, HIGH);
    return 0;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    digitalWrite(DM388_CS, LOW);
    SPI.transfer(reg | 0x80);
    SPI.transfer(0);   /* dummy byte */
    for (size_t i = 0; i < len; i++) buf[i] = SPI.transfer(0);
    digitalWrite(DM388_CS, HIGH);
    return 0;
}
static void delay_ms(uint32_t ms) { delay(ms); }
""", r"""
void setup() {
    Serial.begin(115200);
    pinMode(DM388_CS, OUTPUT);
    digitalWrite(DM388_CS, HIGH);
    SPI.begin();
    dm388_init();
}
void loop() {
    Serial.println(dm388_read_temperature());
    delay(1000);
}
"""),

    "stm32_hal": ("main.c", r"""
#include <stdio.h>
#include <math.h>
#include "stm32f4xx_hal.h"
extern I2C_HandleTypeDef hi2c1;
#define DM388_ADDR 0x76   /* @F(addr)@ */
static int write_reg(uint8_t reg, uint8_t val) {
    return HAL_I2C_Mem_Write(&hi2c1, DM388_ADDR << 1, reg, I2C_MEMADD_SIZE_8BIT, &val, 1, 100) == HAL_OK ? 0 : -1;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    return HAL_I2C_Mem_Read(&hi2c1, DM388_ADDR << 1, reg, I2C_MEMADD_SIZE_8BIT, buf, len, 100) == HAL_OK ? 0 : -1;
}
static void delay_ms(uint32_t ms) { HAL_Delay(ms); }
""", C_MAIN),

    "esp_idf": ("main.c", r"""
#include <stdio.h>
#include <math.h>
#include "driver/i2c.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#define DM388_ADDR 0x76   /* @F(addr)@ */
static int write_reg(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return i2c_master_write_to_device(I2C_NUM_0, DM388_ADDR, buf, 2, pdMS_TO_TICKS(100)) == ESP_OK ? 0 : -1;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    return i2c_master_write_read_device(I2C_NUM_0, DM388_ADDR, &reg, 1, buf, len, pdMS_TO_TICKS(100)) == ESP_OK ? 0 : -1;
}
static void delay_ms(uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms) + 1); }   /* + 1: the current tick is already partly over */
""", r"""
void app_main(void) {
    if (dm388_init() != 0) return;
    printf("T = %f\n", dm388_read_temperature());
}
"""),

    "esp_idf_legacy": ("main.c", r"""
#include <stdio.h>
#include <math.h>
#include "driver/i2c.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#define DM388_ADDR 0x76   /* @F(addr)@ */
static int write_reg(uint8_t reg, uint8_t val) {
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (DM388_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_write_byte(cmd, val, true);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(I2C_NUM_0, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret == ESP_OK ? 0 : -1;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (DM388_ADDR << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (DM388_ADDR << 1) | I2C_MASTER_READ, true);
    i2c_master_read(cmd, buf, len, I2C_MASTER_LAST_NACK);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(I2C_NUM_0, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret == ESP_OK ? 0 : -1;
}
static void delay_ms(uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms) + 1); }   /* + 1: the current tick is already partly over */
""", r"""
void app_main(void) {
    if (dm388_init() != 0) return;
    printf("T = %f\n", dm388_read_temperature());
}
"""),

    "zephyr": ("main.c", r"""
#include <stdio.h>
#include <math.h>
#include <zephyr/kernel.h>
#include <zephyr/drivers/i2c.h>
static const struct i2c_dt_spec dm388 = I2C_DT_SPEC_GET(DT_NODELABEL(dm388));
static int write_reg(uint8_t reg, uint8_t val) { return i2c_reg_write_byte_dt(&dm388, reg, val); }
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) { return i2c_burst_read_dt(&dm388, reg, buf, len); }
static void delay_ms(uint32_t ms) { k_msleep(ms); }
""", C_MAIN),
}

# bugs in the shared core: (key, what is wrong, text, replacement)
CORE_MUTATIONS: List[Tuple[str, str, str, str]] = [
    ("msb_shift", "temperature MSB shifted by 8 instead of 16",
     "((uint32_t)d[5] << 16)", "((uint32_t)d[5] << 8)"),
    ("data_byte_order", "temperature bytes assembled MSB first",
     "((uint32_t)d[5] << 16) | ((uint32_t)d[4] << 8) | (uint32_t)d[3]",
     "((uint32_t)d[3] << 16) | ((uint32_t)d[4] << 8) | (uint32_t)d[5]"),
    ("calib_byte_order", "NVM_PAR_T1 assembled big-endian",
     "(((uint16_t)c[1] << 8) | c[0])", "(((uint16_t)c[0] << 8) | c[1])"),
    ("calib_wrong_byte", "NVM_PAR_T3 read from the wrong calibration byte",
     "int8_t nvm_par_t3 = (int8_t)c[4];", "int8_t nvm_par_t3 = (int8_t)c[5];"),
    ("signedness", "signed NVM_PAR_T3 read as unsigned",
     "int8_t nvm_par_t3 = (int8_t)c[4];", "uint8_t nvm_par_t3 = c[4];"),
    ("exponent_sign", "PAR_T1 divided by 2^8 instead of 2^-8",
     "powf(2.0f, -8.0f)", "powf(2.0f, 8.0f)"),
    ("exponent_value", "PAR_T2 divided by 2^31 instead of 2^30",
     "powf(2.0f, 30.0f)", "powf(2.0f, 31.0f)"),
    ("formula_operator", "t_lin uses partial_data1 * partial_data2",
     "(partial_data1 * partial_data1) * par_t3", "(partial_data1 * partial_data2) * par_t3"),
    ("formula_term", "partial_data2 adds par_t2 instead of multiplying",
     "float partial_data2 = partial_data1 * par_t2;", "float partial_data2 = partial_data1 + par_t2;"),
    ("missing_delay", "no start-up delay after the soft reset",
     "    delay_ms(DM388_STARTUP_MS);\n", ""),
    ("short_delay", "1 ms start-up delay instead of 2 ms",
     "delay_ms(DM388_STARTUP_MS);", "delay_ms(1);"),
    ("missing_reset", "no soft reset",
     "    write_reg(DM388_CMD_REG, DM388_SOFTRESET);\n", ""),
    ("pwr_before_config", "measurement enabled before OSR/ODR are configured",
     "    write_reg(DM388_OSR_REG, DM388_OSR_VALUE);\n    write_reg(DM388_ODR_REG, DM388_ODR_VALUE);\n    write_reg(DM388_PWR_REG, DM388_PWR_NORMAL);\n",
     "    write_reg(DM388_PWR_REG, DM388_PWR_NORMAL);\n    write_reg(DM388_OSR_REG, DM388_OSR_VALUE);\n    write_reg(DM388_ODR_REG, DM388_ODR_VALUE);\n"),
    ("forced_mode", "PWR_CTRL written with forced mode instead of normal mode",
     "write_reg(DM388_PWR_REG, DM388_PWR_NORMAL);", "write_reg(DM388_PWR_REG, DM388_PWR_NORMAL & 0x1F);"),
    ("burst_split", "data registers read in two transactions",
     "    read_regs(DM388_DATA_REG, d, DM388_DATA_LEN);\n",
     "    read_regs(DM388_DATA_REG, d, 3);\n    read_regs(DM388_DATA_REG + 3, d + 3, 3);\n"),
    ("wrong_register", "ODR value written to register 0x1E",
     "write_reg(DM388_ODR_REG, DM388_ODR_VALUE);", "write_reg(DM388_ODR_REG + 1, DM388_ODR_VALUE);"),
    ("reserved_bit", "OSR written with reserved bit 7 set",
     "write_reg(DM388_OSR_REG, DM388_OSR_VALUE);", "write_reg(DM388_OSR_REG, DM388_OSR_VALUE | 0x80);"),
    ("wrong_chip_id", "chip ID compared with an unsourced 0x58",
     "if (id != DM388_CHIP_ID) return -2;", "if (id != 0x58) return -2;"),
    ("untagged_constant", "OSR value without its FACT tag",
     "#define DM388_OSR_VALUE   0x0B   /* @F(osr_value)@ */", "#define DM388_OSR_VALUE   0x0B"),
    ("no_chip_id_read", "chip ID compared without reading it",
     "    if (read_regs(DM388_CHIP_ID_REG, &id, 1) != 0) return -1;\n", ""),
]

# bugs in a framework's bus layer
BUS_MUTATIONS: Dict[str, List[Tuple[str, str, str, str]]] = {
    "pico_i2c": [("wrong_bus_address", "writes go to address 0x77",
                  "i2c_write_blocking(i2c0, DM388_ADDR, buf, 2, false)", "i2c_write_blocking(i2c0, DM388_ADDR + 1, buf, 2, false)")],
    "pico_spi": [("no_read_flag", "SPI read without bit 7 set", "uint8_t cmd = reg | 0x80;", "uint8_t cmd = reg;"),
                 ("dummy_byte_ignored", "the dummy byte is taken as data", "buf[i] = tmp[i + 1];", "buf[i] = tmp[i];")],
    "arduino_wire": [("wrong_bus_address", "writes go to address 0x77",
                      "    Wire.beginTransmission(DM388_ADDR);\n    Wire.write(reg);\n    Wire.write(val);",
                      "    Wire.beginTransmission(DM388_ADDR + 1);\n    Wire.write(reg);\n    Wire.write(val);")],
    "arduino_spi": [("no_read_flag", "SPI read without bit 7 set", "SPI.transfer(reg | 0x80);", "SPI.transfer(reg);"),
                    ("dummy_byte_ignored", "the dummy byte is taken as data", "    SPI.transfer(0);   /* dummy byte */\n", "")],
    "stm32_hal": [("hal_address_not_shifted", "HAL given the 7-bit address unshifted",
                   "HAL_I2C_Mem_Write(&hi2c1, DM388_ADDR << 1,", "HAL_I2C_Mem_Write(&hi2c1, DM388_ADDR,")],
    "esp_idf": [("wrong_bus_address", "writes go to address 0x77",
                 "i2c_master_write_to_device(I2C_NUM_0, DM388_ADDR, buf", "i2c_master_write_to_device(I2C_NUM_0, DM388_ADDR + 1, buf")],
    "esp_idf_legacy": [("read_as_write", "the read phase addresses the device for writing",
                        "(DM388_ADDR << 1) | I2C_MASTER_READ", "(DM388_ADDR << 1) | I2C_MASTER_WRITE")],
    "zephyr": [],
}


_PWR_WRITE = "    write_reg(DM388_PWR_REG, DM388_PWR_NORMAL);\n"
_T3 = ("    int8_t nvm_par_t3 = (int8_t)c[4];\n", "    par_t3 = (float)nvm_par_t3 / powf(2.0f, 48.0f);\n")

# The same driver written with C idioms real drivers use: each correct variant must pass, and the bugs planted in it
# (the core bugs whose text it still contains, plus its own) must be caught.
# name -> (framework, [(text, replacement) that make the correct variant], [(key, what, text, replacement)])
IDIOMS: Dict[str, Tuple[str, List[Tuple[str, str]], List[Tuple[str, str, str, str]]]] = {
    "compound_literal": ("pico_i2c", [(
        "    uint8_t buf[2] = {reg, val};\n    return i2c_write_blocking(i2c0, DM388_ADDR, buf, 2, false) == 2 ? 0 : -1;",
        "    return i2c_write_blocking(i2c0, DM388_ADDR, (uint8_t[]){reg, val}, 2, false) == 2 ? 0 : -1;")], []),
    "element_address": ("pico_i2c", [
        ("    uint8_t buf[2] = {reg, val};\n    return i2c_write_blocking(i2c0, DM388_ADDR, buf, 2, false) == 2 ? 0 : -1;",
         "    uint8_t buf[2];\n    buf[0] = reg;\n    buf[1] = val;\n    return i2c_write_blocking(i2c0, DM388_ADDR, &buf[0], 2, false) == 2 ? 0 : -1;"),
        ("i2c_read_blocking(i2c0, DM388_ADDR, buf, len, false)", "i2c_read_blocking(i2c0, DM388_ADDR, &buf[0], len, false)")], []),
    "pointer_walk": ("pico_i2c", [(
        "    uint16_t nvm_par_t1 = (uint16_t)(((uint16_t)c[1] << 8) | c[0]);\n"
        "    uint16_t nvm_par_t2 = (uint16_t)(((uint16_t)c[3] << 8) | c[2]);\n    int8_t nvm_par_t3 = (int8_t)c[4];\n",
        "    const uint8_t *p = c;\n    uint16_t nvm_par_t1 = (uint16_t)(((uint16_t)p[1] << 8) | p[0]);\n    p += 2;\n"
        "    uint16_t nvm_par_t2 = (uint16_t)(((uint16_t)p[1] << 8) | p[0]);\n    p += 2;\n    int8_t nvm_par_t3 = (int8_t)*p;\n")],
        [("pointer_byte_order", "NVM_PAR_T2 assembled big-endian through the pointer",
          "uint16_t nvm_par_t2 = (uint16_t)(((uint16_t)p[1] << 8) | p[0]);", "uint16_t nvm_par_t2 = (uint16_t)(((uint16_t)p[0] << 8) | p[1]);")]),
    "post_increment": ("arduino_wire", [(
        "    for (size_t i = 0; i < len; i++) buf[i] = Wire.read();",
        "    size_t i = 0;\n    while (Wire.available()) buf[i++] = Wire.read();")], []),
    "enum_class": ("arduino_wire", [(
        "#define DM388_CMD_REG     0x7E   /* @F(cmd_reg)@ */",
        "enum class Dm388Reg : uint8_t { CMD = 0x7E /* @F(cmd_reg)@ */ };\n#define DM388_CMD_REG static_cast<uint8_t>(Dm388Reg::CMD)")],
        [("enum_wrong_register", "soft reset written to 0x7F", "CMD = 0x7E /* @F(cmd_reg)@ */", "CMD = 0x7E + 1 /* @F(cmd_reg)@ */")]),
    "switch_value": ("pico_i2c", [(
        _PWR_WRITE,
        "    write_reg(DM388_PWR_REG, pwr_value(3));\n"), (
        "int dm388_init(void) {",
        "static uint8_t pwr_value(int mode) {\n    switch (mode) {\n    case 0:\n        return 0x00;\n    case 3:\n"
        "        return DM388_PWR_NORMAL;\n    default:\n        return 0x00;\n    }\n}\n\nint dm388_init(void) {")],
        [("switch_forced_mode", "the switch maps normal to forced mode",
          "        return DM388_PWR_NORMAL;\n", "        return DM388_PWR_NORMAL & 0x1F;\n")]),
    "ternary_value": ("pico_i2c", [(
        _PWR_WRITE, "    int enable = 1;\n    write_reg(DM388_PWR_REG, enable ? DM388_PWR_NORMAL : 0x00);\n")],
        [("ternary_forced_mode", "the enabled branch writes forced mode",
          "enable ? DM388_PWR_NORMAL : 0x00", "enable ? (DM388_PWR_NORMAL & 0x1F) : 0x00")]),
    "typed_parameter": ("pico_i2c", [
        (_T3[0], ""), (_T3[1], "    par_t3 = scale_t3(c[4]);\n"),
        ("int dm388_init(void) {", "static float scale_t3(int8_t raw) { return (float)raw / powf(2.0f, 48.0f); }\n\nint dm388_init(void) {")],
        [("parameter_signedness", "the helper takes the signed coefficient as uint8_t",
          "static float scale_t3(int8_t raw)", "static float scale_t3(uint8_t raw)")]),
    "freertos_task": ("esp_idf", [(
        "void app_main(void) {\n    if (dm388_init() != 0) return;\n    printf(\"T = %f\\n\", dm388_read_temperature());\n}",
        "static void sensor_task(void *arg) {\n    for (;;) {\n        printf(\"T = %f\\n\", dm388_read_temperature());\n"
        "        vTaskDelay(pdMS_TO_TICKS(1000));\n    }\n}\n\nvoid app_main(void) {\n    if (dm388_init() != 0) return;\n"
        "    xTaskCreate(sensor_task, \"dm388\", 4096, NULL, 5, NULL);\n}")], []),
    "read_modify_write": ("pico_i2c", [(
        "    write_reg(DM388_OSR_REG, DM388_OSR_VALUE);\n",
        "    uint8_t osr = 0;\n    read_regs(DM388_OSR_REG, &osr, 1);\n    osr = (uint8_t)((osr & ~0x3F) | DM388_OSR_VALUE);\n"
        "    write_reg(DM388_OSR_REG, osr);\n")],
        [("rmw_reserved_bit", "the read-modify-write also sets reserved bit 7",
          "osr = (uint8_t)((osr & ~0x3F) | DM388_OSR_VALUE);", "osr = (uint8_t)((osr & ~0x3F) | DM388_OSR_VALUE | 0x80);"),
         ("rmw_wrong_value", "the read-modify-write sets OSR to 0x09 (temperature x2, pressure x2)",
          "osr = (uint8_t)((osr & ~0x3F) | DM388_OSR_VALUE);", "osr = (uint8_t)((osr & ~0x3F) | (DM388_OSR_VALUE & 0x0F) - 2);")]),
    "paired_write": ("pico_i2c", [(
        "    write_reg(DM388_OSR_REG, DM388_OSR_VALUE);\n    write_reg(DM388_ODR_REG, DM388_ODR_VALUE);\n",
        "    uint8_t cfg[4] = {DM388_OSR_REG, DM388_OSR_VALUE, DM388_ODR_REG, DM388_ODR_VALUE};   /* reg, data pairs */\n"
        "    i2c_write_blocking(i2c0, DM388_ADDR, cfg, 4, false);\n")],
        [("paired_wrong_value", "the paired write sets ODR to 0x03",
          "DM388_ODR_REG, DM388_ODR_VALUE};", "DM388_ODR_REG, DM388_ODR_VALUE + 1};")]),
    "freertos_100hz": ("esp_idf", [(   # sdkconfig.defaults sets 100 Hz: a tick is 10 ms
        "static void delay_ms(uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms) + 1); }   /* + 1: the current tick is already partly over */",
        "static void delay_ms(uint32_t ms) {   /* whole ticks, rounded up, + 1 for the tick already under way */\n"
        "    vTaskDelay((ms + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS + 1);\n}")],
        [("tick_rounds_to_zero", "vTaskDelay(pdMS_TO_TICKS(2)) waits 0 ticks at 100 Hz",
          "vTaskDelay((ms + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS + 1);", "vTaskDelay(pdMS_TO_TICKS(ms));"),
         ("one_partial_tick", "vTaskDelay(pdMS_TO_TICKS(2) + 1) is 1 tick at 100 Hz, which can end at once",
          "vTaskDelay((ms + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS + 1);", "vTaskDelay(pdMS_TO_TICKS(ms) + 1);")]),
    "two_files_same_names": ("pico_i2c", [
        ("#define DM388_ADDR 0x76   /* @F(addr)@ */", "#define DEV 0x76   /* @F(addr)@ */\n#define DM388_ADDR DEV")], []),
    "goto_cleanup": ("pico_i2c", [
        ("    if (read_regs(DM388_CHIP_ID_REG, &id, 1) != 0) return -1;\n    if (id != DM388_CHIP_ID) return -2;\n",
         "    if (read_regs(DM388_CHIP_ID_REG, &id, 1) != 0) goto fail;\n    if (id != DM388_CHIP_ID) goto fail;\n"),
        (_PWR_WRITE + "    return 0;\n}\n", _PWR_WRITE + "    return 0;\nfail:\n    return -1;\n}\n")], []),
}


# other project files a variant needs (path relative to the project -> content)
EXTRA_FILES: Dict[str, Dict[str, str]] = {
    "esp_idf_legacy": {"sdkconfig.defaults": "CONFIG_FREERTOS_HZ=1000\n"},   # the plain esp_idf driver leaves the rate unstated
    "freertos_100hz": {"sdkconfig.defaults": "CONFIG_FREERTOS_HZ=100\n"},
    # another module of the project whose own DEV, write_reg and read_regs mean something else (a settings page in RAM)
    "two_files_same_names": {"src/zz_settings.c": r"""
#include <stdint.h>
#include <stddef.h>
#define DEV 0x01                                /* settings page in use */
static uint8_t page[16];
static int write_reg(uint8_t reg, uint8_t val) {
    uint8_t slot = (uint8_t)(reg & 0x0F);
    page[slot] = val;
    page[0] = (uint8_t)(page[0] + DEV);         /* change counter */
    return 0;
}
static int read_regs(uint8_t reg, uint8_t *buf, size_t len) {
    for (size_t i = 0; i < len; i++) {
        buf[i] = page[(reg + i) & 0x0F];
    }
    return 0;
}
int settings_set(uint8_t key, uint8_t value) { return write_reg(key, value); }
int settings_get(uint8_t key, uint8_t *value) { return read_regs(key, value, 1); }
"""},
}


def extra_files(framework: str) -> Dict[str, str]:
    return EXTRA_FILES.get(framework, {})


# Code verify cannot check must not pass: drivers verified against facts only (no sequence, no register map), or that
# talk to the bus through calls the analysis does not model. name -> (framework, edits); each must FAIL verify.
MUST_FAIL: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {
    "i2c_without_records": ("pico_i2c", []),
    "spi_without_records": ("pico_spi", []),
    "arduino_spi_without_records": ("arduino_spi", []),
    "unmodelled_bus_call": ("pico_i2c", [("i2c_write_blocking(i2c0, DM388_ADDR, buf, 2, false) == 2",
                                          "i2c_write_burst_blocking(i2c0, DM388_ADDR, buf, 2) == 2")]),
    "unmodelled_spi_call": ("zephyr", [
        ("static int write_reg(uint8_t reg, uint8_t val) { return i2c_reg_write_byte_dt(&dm388, reg, val); }",
         "static const struct spi_dt_spec bus = SPI_DT_SPEC_GET(DT_NODELABEL(dm388), SPI_WORD_SET(8), 0);\n"
         "static int write_reg(uint8_t reg, uint8_t val) {\n    uint8_t tx[2] = {reg & 0x7F, val};\n"
         "    struct spi_buf b = {.buf = tx, .len = 2};\n    struct spi_buf_set s = {.buffers = &b, .count = 1};\n"
         "    return spi_write_dt(&bus, &s);\n}")]),
}


def driver(framework: str) -> Tuple[str, str]:
    """(file name, source with @F(key)@ placeholders) of the correct driver for a framework or idiom variant."""
    if framework in IDIOMS or framework in MUST_FAIL:
        base, edits = IDIOMS[framework][:2] if framework in IDIOMS else MUST_FAIL[framework]
        name, src = driver(base)
        for old, new in edits:
            assert old in src, f"{framework}: {old!r} not in the {base} driver"
            src = src.replace(old, new, 1)
        return name, src
    name, bus, entry = FRAMEWORKS[framework]
    return name, bus + CORE + entry


# core mutations that are not bugs in a variant: there the changed code still does what the datasheet requires
HARMLESS = {"freertos_100hz": {"short_delay"}}   # 1 ms rounds up to 2 ticks at 100 Hz: at least 10 ms


def mutants(framework: str) -> List[Tuple[str, str, str]]:
    """(key, what is wrong, source) for every bug planted in the driver (core bugs an idiom variant rewrote are skipped)."""
    _name, src = driver(framework)
    base = IDIOMS[framework][0] if framework in IDIOMS else framework
    own = IDIOMS[framework][2] if framework in IDIOMS else []
    out = []
    for key, what, old, new in CORE_MUTATIONS + BUS_MUTATIONS.get(base, []) + own:
        if key in HARMLESS.get(framework, ()):
            continue
        if old not in src:
            assert framework in IDIOMS and (key, what, old, new) not in own, f"{framework}/{key}: {old!r} not in the driver"
            continue
        out.append((key, what, src.replace(old, new, 1)))
    return out
