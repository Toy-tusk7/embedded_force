"""RP2350 Betaflight pin-capability rules (tables from Betaflight 2026.6.2 src/platform/PICO)."""

from embedforge.truth.betaflight_rp2350 import check_config

# The user's RP2354B wiring as it would appear in a Betaflight config (all legal).
GOOD = """
#define FC_TARGET_MCU RP2350B
#define SPI0_SCK_PIN PA18
#define SPI0_SDI_PIN PA20
#define SPI0_SDO_PIN PA19
#define GYRO_1_CS_PIN PA14
#define SPI1_SCK_PIN PA42
#define SPI1_SDI_PIN PA44
#define SPI1_SDO_PIN PA43
#define SDCARD_SPI_CS_PIN PA46
#define I2C0_SDA_PIN PA16
#define I2C0_SCL_PIN PA17
#define ADC_CURR_PIN PA40
#define ADC_VBAT_PIN PA41
#define MOTOR1_PIN PA28
#define MOTOR2_PIN PA29
#define MOTOR3_PIN PA30
#define MOTOR4_PIN PA31
#define LED_STRIP_PIN PA23
#define GYRO_2_CS_PIN NONE
"""


def codes(text, target="RP2350B"):
    return [(f["code"], f["line"]) for f in check_config(text, target)]


def test_users_wiring_is_legal_on_rp2350b():
    assert check_config(GOOD, "RP2350B") == []


def test_wrong_spi_pin_is_caught_with_legal_options():
    f = check_config("#define SPI0_SCK_PIN PA19\n", "RP2350B")
    assert f and "SPI0 SCK" in f[0]["message"] and "PA18" in f[0]["suggestion"]


def test_i2c_sda_scl_swapped():
    assert codes("#define I2C0_SDA_PIN PA17\n#define I2C0_SCL_PIN PA16\n") == [("BF_PIN_CAPABILITY", 1), ("BF_PIN_CAPABILITY", 2)]


def test_adc_pins_depend_on_package():
    assert codes("#define ADC_VBAT_PIN PA41\n", "RP2350B") == []
    assert codes("#define ADC_VBAT_PIN PA41\n", "RP2350A")          # A has only GPIO0-29
    assert codes("#define ADC_VBAT_PIN PA26\n", "RP2350B")          # B's ADC is GPIO40-47


def test_motor_limits():
    five = "".join(f"#define MOTOR{i}_PIN PA{i}\n" for i in range(1, 6))
    assert ("BF_PIN_CAPABILITY", 5) in codes(five)
    assert codes("#define MOTOR1_PIN PA2\n#define MOTOR2_PIN PA40\n")    # spans both PIO windows


def test_non_rp2350_targets_are_not_checked():
    assert check_config("#define SPI0_SCK_PIN PA19\n", "STM32F405") == []
