"""Static flow verification: register operations traced from driver code, compared with datasheet sequences."""

from pathlib import Path

import pytest

from embedforge.agent.events import RunSession
from embedforge.agent.tools import ToolBelt, check_agent_write
from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore, verify_project
from embedforge.truth.flow import FlowModel
from embedforge.truth.sequences import SequenceBook

DATASHEET = (
    "Register 0x7E is the CMD register. Writing 0xB6 to CMD triggers a soft reset. "
    "After a soft reset the device needs a start-up time of 2 ms before any other register access. "
    "The calibration coefficients start at register 0x31 and are read as one block of 21 bytes. "
    "Register 0x1B is PWR_CTRL; writing 0x33 enables pressure and temperature in normal mode. "
    "With SDO connected to GND the I2C address is 0x76. "
    "Initialisation: issue a soft reset, wait for the start-up time, read the calibration block, then set PWR_CTRL. "
    "Register 0x7E CMD: Bit 7..0 cmd, 0xB6 softreset. "
    "Register 0x1B PWR_CTRL: Bit 0 press_en, Bit 1 temp_en, Bit 5..4 mode 00 sleep mode 11 normal mode. "
    "Calibration: 0x32 NVM_PAR_T1<15:8> NVM_PAR_T1 16 Bit unsigned 0x31 NVM_PAR_T1<7:0>."
)

GOOD = r"""
#include "hardware/i2c.h"
#define ADDR          0x76   /* FACT-008 */
#define REG_CMD       0x7E   /* FACT-001 */
#define CMD_SOFTRESET 0xB6   /* FACT-002 */
#define REG_CALIB     0x31   /* FACT-004 */
#define CALIB_LEN     21     /* FACT-005 */
#define REG_PWR_CTRL  0x1B   /* FACT-006 */
#define PWR_NORMAL    0x33   /* FACT-007 */
#define STARTUP_MS    2      /* FACT-003 */
static uint8_t calib[CALIB_LEN];
static int write_reg(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return i2c_write_blocking(i2c0, ADDR, buf, 2, false);
}
static int read_regs(uint8_t reg, uint8_t *dst, size_t len) {
    i2c_write_blocking(i2c0, ADDR, &reg, 1, true);
    return i2c_read_blocking(i2c0, ADDR, dst, len, false);
}
bool bmp_init(void) {
    write_reg(REG_CMD, CMD_SOFTRESET);
    sleep_ms(STARTUP_MS);
    read_regs(REG_CALIB, calib, sizeof(calib));
    write_reg(REG_PWR_CTRL, PWR_NORMAL);
    return true;
}
int main(void) { bmp_init(); return 0; }
"""


def _project(tmp_path: Path, code: str = GOOD):
    p = Project("f1", tmp_path / "f1")
    store = SourceStore(p.root)
    src = store.add("https://example.com/bmp-like-datasheet.pdf", "datasheet", DATASHEET, kind="pdf")
    ledger = FactLedger(p.root, store)
    facts = {}
    for key, prop, val, unit, quote in [
        ("cmd", "cmd_reg", "0x7E", "", "Register 0x7E is the CMD register."),
        ("rst", "softreset_cmd", "0xB6", "", "Writing 0xB6 to CMD triggers a soft reset."),
        ("t", "startup_time", "2", "ms", "After a soft reset the device needs a start-up time of 2 ms"),
        ("cal", "calib_first_reg", "0x31", "", "The calibration coefficients start at register 0x31"),
        ("len", "calib_len", "21", "", "read as one block of 21 bytes"),
        ("pwr", "pwr_ctrl_reg", "0x1B", "", "Register 0x1B is PWR_CTRL"),
        ("norm", "pwr_ctrl_normal", "0x33", "", "writing 0x33 enables pressure and temperature in normal mode"),
        ("addr", "i2c_address", "0x76", "", "With SDO connected to GND the I2C address is 0x76."),
    ]:
        res = ledger.record("BMP", prop, val, src.id, quote, unit)
        assert res["ok"], res
        facts[key] = res["fact"]["id"]
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.c").write_text(code, encoding="utf-8")
    return p, store, ledger, src, facts


def _record(p, ledger, src, f, entry="bmp_init", steps=None):
    steps = steps or [{"op": "write", "reg": f["cmd"], "value": f["rst"]}, {"op": "delay_min", "value": f["t"]},
                      {"op": "read", "reg": f["cal"], "value": f["len"]}, {"op": "write", "reg": f["pwr"], "value": f["norm"]}]
    return SequenceBook(p.root).record(ledger, "BMP", "power_on_init", entry, steps, src.id,
                                       "Initialisation: issue a soft reset, wait for the start-up time, read the calibration block, then set PWR_CTRL.",
                                       device=f["addr"])


def _flow_codes(p, ledger):
    rep = verify_project(p.root, ledger)
    # these tests are about order and timing; register meaning (and the rule that it must be recorded) is test_registers.py,
    # unchecked statuses and unbounded waits are test_robust.py
    rep.findings = [x for x in rep.findings if x.code not in ("REG_MAP_MISSING", "RESULT_IGNORED", "UNBOUNDED_WAIT")]
    rep.passed = not any(x.severity == "ERROR" for x in rep.findings)
    return rep, sorted({x.code for x in rep.findings if x.code.startswith("FLOW")})


# ------------------------------------------------------------------ tracing


def test_traces_through_helpers_macros_and_buffers():
    t = FlowModel({"src/main.c": GOOD}).trace("bmp_init")
    assert [e.describe() for e in t.events] == ["write 0x7E <- 0xB6", "delay 2000 us", "read 0x31 x21", "write 0x1B <- 0x33"]
    assert all(e.addr == 0x76 and e.guaranteed for e in t.ops())
    assert t.events[0].public()["via"].startswith("bmp_init:21 > write_reg")


@pytest.mark.parametrize("code, entry, expected", [
    ("""#include <Wire.h>
#define A 0x77
class S { public: bool begin() {
  Wire.beginTransmission(A); Wire.write(0x7E); Wire.write(0xB6); Wire.endTransmission();
  delay(10);
  Wire.beginTransmission(A); Wire.write((uint8_t)0x31); Wire.endTransmission(false); Wire.requestFrom(A, 21);
  return true; } };""", "begin", ["write 0x7E <- 0xB6", "delay 10000 us", "read 0x31 x21"]),
    ("""#define BMP (0x76 << 1)
void bmp_init(I2C_HandleTypeDef *h) { uint8_t v = 0xB6;
  HAL_I2C_Mem_Write(h, BMP, 0x7E, I2C_MEMADD_SIZE_8BIT, &v, 1, 100); HAL_Delay(5);
  uint8_t c[21]; HAL_I2C_Mem_Read(h, BMP, 0x31, I2C_MEMADD_SIZE_8BIT, c, 21, 100); }""", "bmp_init",
     ["write 0x7E <- 0xB6", "delay 5000 us", "read 0x31 x21"]),
    ("""static int bmp388_init(const struct device *dev) { const struct bmp388_config *cfg = dev->config;
  i2c_reg_write_byte_dt(&cfg->i2c, 0x7E, 0xB6); k_sleep(K_MSEC(2));
  uint8_t buf[21]; i2c_burst_read_dt(&cfg->i2c, 0x31, buf, sizeof(buf)); return 0; }""", "bmp388_init",
     ["write 0x7E <- 0xB6", "delay 2000 us", "read 0x31 x21"]),
    ("""#define BMP3_REG_CMD 0x7E
typedef struct { int8_t (*write)(uint8_t, const uint8_t *, uint32_t, void *); void (*delay_us)(uint32_t, void *); void *intf_ptr; } bmp3_dev;
int8_t bmp3_soft_reset(bmp3_dev *dev) { uint8_t cmd = 0xB6; dev->write(BMP3_REG_CMD, &cmd, 1, dev->intf_ptr); dev->delay_us(2000, dev->intf_ptr); return 0; }""",
     "bmp3_soft_reset", ["write 0x7E <- 0xB6", "delay 2000 us"]),
    ("""void init(void) { uint8_t b[2]; b[0] = 0x7E; b[1] = 0xB6; i2c_master_write_to_device(I2C_NUM_0, 0x76, b, 2, 100);
  vTaskDelay(pdMS_TO_TICKS(3)); }""", "init", ["write 0x7E <- 0xB6", "delay 2000 us (vTaskDelay(3) at 1000 Hz: at least 2 whole tick(s))"]),
])
def test_traces_in_each_hal_style(code, entry, expected):
    assert [e.describe() for e in FlowModel({"src/x.cpp": code}).trace(entry).events] == expected


@pytest.mark.parametrize("hz, call, waits", [
    (100, "vTaskDelay(pdMS_TO_TICKS(2))", 0),                     # 2 ms is 0 ticks at 100 Hz
    (100, "vTaskDelay(pdMS_TO_TICKS(2) + 1)", 0),                 # 1 tick: it can end at the very next tick
    (100, "vTaskDelay((2 + portTICK_PERIOD_MS - 1) / portTICK_PERIOD_MS + 1)", 10000),
    (1000, "vTaskDelay(pdMS_TO_TICKS(2) + 1)", 2000),
    (1000, "vTaskDelay(2 / portTICK_PERIOD_MS)", 1000),
])
def test_freertos_delays_count_whole_ticks_at_the_project_tick_rate(tmp_path, hz, call, waits):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("void init(void) { " + call + "; }", encoding="utf-8")
    (tmp_path / "sdkconfig.defaults").write_text(f"CONFIG_FREERTOS_HZ={hz}\n", encoding="utf-8")
    model = FlowModel.from_dir(tmp_path)
    [e] = model.trace("init").events
    assert e.delay_us == waits and not e.assumed
    # FreeRTOSConfig.h in the sources states it too; with nothing stated the rate is an assumption
    assert FlowModel({"include/FreeRTOSConfig.h": "#define configTICK_RATE_HZ ((TickType_t)250)"}).tick_hz == 250
    assert FlowModel({"src/main.c": "void init(void) { " + call + "; }"}).trace("init").events[0].assumed


def test_two_files_keep_their_own_macros_and_static_helpers():
    files = {
        "src/a.c": "#define DEV 0x76\nstatic void wr(uint8_t r, uint8_t v) { uint8_t b[2] = {r, v}; i2c_write_blocking(i2c0, DEV, b, 2, false); }\n"
                   "void a_init(void) { wr(0x7E, 0xB6); }",
        "src/b.c": "#define DEV 0x01\nstatic uint8_t page[4];\nstatic void wr(uint8_t r, uint8_t v) { page[r & 3] = v; page[0] = (uint8_t)(page[0] + DEV); }\n"
                   "void b_set(uint8_t k, uint8_t v) { wr(k, v); }",
    }
    [e] = FlowModel(files).trace("a_init").events
    assert (e.addr, e.describe()) == (0x76, "write 0x7E <- 0xB6")
    assert FlowModel(files).trace("b_set").events == []


def test_a_multi_register_write_is_read_the_way_the_register_map_allows():
    from embedforge.truth.flow import Event, writes_of
    e = Event("write", addr=0x76, reg=0x1C, data=[0x0B, 0x1D, 0x02])   # OSR=0x0B, then the pair ODR=0x02
    mapped = {0x1B, 0x1C, 0x1D}.__contains__
    assert writes_of(e, mapped) == [(0x1C, 0x0B), (0x1D, 0x02)]
    burst = Event("write", addr=0x76, reg=0x1B, data=[0x33, 0x0B, 0x02])   # PWR, OSR, ODR in a row
    assert writes_of(burst, mapped) == [(0x1B, 0x33), (0x1C, 0x0B), (0x1D, 0x02)]
    assert len(writes_of(e)) == 5   # without a map: both readings


def test_read_data_is_runtime_and_branches_are_conditional():
    code = """#define A 0x76
static void rd(uint8_t r, uint8_t *d, int n) { i2c_write_blocking(i2c0, A, &r, 1, true); i2c_read_blocking(i2c0, A, d, n, false); }
static void wr(uint8_t r, uint8_t v) { uint8_t b[] = {r, v}; i2c_write_blocking(i2c0, A, b, 2, false); }
bool init(void) { uint8_t id = 0; rd(0x00, &id, 1); if (id != 0x50) { return false; }
  if (id == 0x50) { sleep_ms(5); } wr(0x7E, 0xB6); for (int i = 0; i < 3; i++) { wr(0x1C, 0x05); } return true; }"""
    t = FlowModel({"a.c": code}).trace("init")
    # a counted loop runs its real number of times
    assert [e.describe() for e in t.events] == ["read 0x00 x1", "delay 5000 us", "write 0x7E <- 0xB6"] + ["write 0x1C <- 0x05"] * 3
    assert [e.guaranteed for e in t.events] == [True, False, True, True, True, True]   # the delay is inside a data-dependent if


@pytest.mark.parametrize("code", [
    # pico-sdk full-duplex SPI: rx[1] holds register 0x00 (rx[0] was clocked in during the command byte)
    """bool init(void) { uint8_t tx[2] = {0x80, 0}; uint8_t rx[2] = {0}; spi_write_read_blocking(spi0, tx, rx, 2);
  if (rx[1] != 0x50) return false; uint8_t w[2] = {0x1B, 0x33}; spi_write_blocking(spi0, w, 2); return true; }""",
    # command byte, then spi_read_blocking
    """bool init(void) { uint8_t r = 0x80; uint8_t id[1] = {0}; spi_write_blocking(spi0, &r, 1); spi_read_blocking(spi0, 0, id, 1);
  if (id[0] != 0x50) return false; uint8_t w[2] = {0x1B, 0x33}; spi_write_blocking(spi0, w, 2); return true; }""",
    # Zephyr: the byte lands in *value, and 0 is success
    """int init(const struct i2c_dt_spec *s) { uint8_t id = 0; if (i2c_reg_read_byte_dt(s, 0x00, &id)) return -5;
  if (id != 0x50) return -19; if (i2c_reg_write_byte_dt(s, 0x1B, 0x33)) return -5; return 0; }""",
    # STM32 HAL returns HAL_OK (0), not a byte count
    """int init(void) { uint8_t c[2] = {0x7E, 0xB6}; if (HAL_I2C_Master_Transmit(&hi2c1, 0x76 << 1, c, 2, 100)) return -1;
  uint8_t w[2] = {0x1B, 0x33}; HAL_I2C_Master_Transmit(&hi2c1, 0x76 << 1, w, 2, 100); return 0; }""",
    # an unknown vendor function handed an empty buffer fills it
    """bool init(void) { uint8_t id[1] = {0}; vendor_read(0x76, 0x00, id, 1); if (id[0] != 0x50) return false;
  uint8_t w[2] = {0x1B, 0x33}; i2c_write_blocking(i2c0, 0x76, w, 2, false); return true; }""",
])
def test_read_buffers_are_filled_so_an_id_check_does_not_end_the_trace(code):
    # before: the buffer kept its initializer, `id != 0x50` was always true and every later step looked missing
    events = [e.describe() for e in FlowModel({"a.c": code}).trace("init").events]
    assert "write 0x1B <- 0x33" in events, events


def test_spi_read_data_carries_its_register():
    code = """float t(void) { uint8_t tx[3] = {0x84, 0, 0}; uint8_t rx[3]; spi_write_read_blocking(spi0, tx, rx, 3);
  return (rx[2] << 8) | rx[1]; }"""
    w = FlowModel({"a.c": code}).run(["t"])
    assert sorted((s[0].reg, s[1]) for s in w.obs_shifts) == [(0x04, 0), (0x05, 8)]   # rx[1] is 0x04, rx[2] is 0x05
    assert [e.describe() for e in w.events if e.kind == "read"] == ["read 0x04 x2"]


def test_c_idioms_keep_their_values():
    code = """
enum class Reg : uint8_t { CTRL = 0xF4 };
static int16_t be16(const uint8_t *b) { return (int16_t)((b[0] << 8) | b[1]); }
static float scale(int8_t raw) { return raw * 2.0f; }
static uint8_t pick(int m) { switch (m) { case 0: return 0x00; case 3: return 0x27; default: return 0x01; } }
void init(void) {
    i2c_write_blocking(i2c0, 0x76, (uint8_t[]){static_cast<uint8_t>(Reg::CTRL), pick(3)}, 2, false);
    uint8_t buf[4] = {0xF5, 0xA0, 0, 0};
    i2c_write_blocking(i2c0, 0x76, &buf[0], 2, false);
    const uint8_t *p = buf;
    p += 1;
    uint8_t raw[2] = {0xFF, 0x38};
    uint8_t out[3]; size_t i = 0;
    out[i++] = 1; out[i++] = 2;
    printf("%d %f %d %d", be16(raw), scale(0xF0), *p, (int)i);
}"""
    w = FlowModel({"a.cpp": code}).run(["init"])
    assert [e.describe() for e in w.events if e.kind == "write"] == ["write 0xF4 <- 0x27", "write 0xF5 <- 0xA0"]
    printed = [v for v, _ in w.outputs]
    assert printed == [-200, -32.0, 0xA0, 2], printed   # signed return, int8_t parameter, pointer into buf, i++ twice


def test_read_modify_write_keeps_the_bits_it_decides():
    from embedforge.truth.flow import Part
    code = """void init(void) { uint8_t v = 0; i2c_write_blocking(i2c0, 0x76, (uint8_t[]){0x1C}, 1, true);
  i2c_read_blocking(i2c0, 0x76, &v, 1, false); v = (uint8_t)((v & ~0x3F) | 0x0B);
  i2c_write_blocking(i2c0, 0x76, (uint8_t[]){0x1C, v}, 2, false); }"""
    w = FlowModel({"a.c": code}).run(["init"])
    v = [e for e in w.events if e.kind == "write"][-1].data[0]
    assert isinstance(v, Part) and v.mask & 0xFF == 0x3F and v.value == 0x0B


@pytest.mark.parametrize("code, expected", [
    ("""void init(void) { digitalWrite(10, LOW); SPI.transfer(0x7E); SPI.transfer(0xB6); digitalWrite(10, HIGH); }""",
     ["write 0x7E <- 0xB6"]),
    ("""void init(void) { i2c_cmd_handle_t c = i2c_cmd_link_create(); i2c_master_start(c);
  i2c_master_write_byte(c, (0x76 << 1) | I2C_MASTER_WRITE, true); i2c_master_write_byte(c, 0x7E, true);
  i2c_master_write_byte(c, 0xB6, true); i2c_master_stop(c); i2c_master_cmd_begin(I2C_NUM_0, c, 100); i2c_cmd_link_delete(c); }""",
     ["write 0x7E <- 0xB6"]),
])
def test_arduino_spi_and_esp_idf_command_links_are_traced(code, expected):
    assert [e.describe() for e in FlowModel({"a.cpp": code}).trace("init").events] == expected


def test_tasks_and_callbacks_are_walked_after_main():
    from embedforge.truth.registers import entry_points
    code = """static void sensor_task(void *arg) { read_data(); }
void read_data(void) { }
static void IRAM_ATTR drdy_isr(void *arg) { }
void app_main(void) { xTaskCreate(sensor_task, "s", 4096, NULL, 5, NULL); gpio_isr_handler_add(4, drdy_isr, NULL); }"""
    assert entry_points(FlowModel({"main.c": code})) == [["app_main", "sensor_task", "drdy_isr"]]


# ------------------------------------------------------------------ sequence checking


def test_correct_driver_matches_every_step(tmp_path):
    p, store, ledger, src, f = _project(tmp_path)
    assert _record(p, ledger, src, f)["ok"]
    rep, codes = _flow_codes(p, ledger)
    assert codes == [] and rep.passed, [x.public() for x in rep.findings]
    assert [s["status"] for s in rep.flows[0]["steps"]] == ["matched"] * 4
    assert rep.stats["sequence_steps_matched"] == 4


@pytest.mark.parametrize("mutate, code", [
    (lambda c: c.replace("    write_reg(REG_CMD, CMD_SOFTRESET);\n", ""), "FLOW_STEP_MISSING"),
    (lambda c: c.replace("sleep_ms(STARTUP_MS);", "sleep_ms(1);"), "FLOW_DELAY_SHORT"),
    (lambda c: c.replace("    sleep_ms(STARTUP_MS);\n", ""), "FLOW_DELAY_SHORT"),
    (lambda c: c.replace("#define PWR_NORMAL    0x33", "#define PWR_NORMAL    0x13"), "FLOW_VALUE_MISMATCH"),
    (lambda c: c.replace("    write_reg(REG_CMD, CMD_SOFTRESET);\n    sleep_ms(STARTUP_MS);\n    read_regs(REG_CALIB, calib, sizeof(calib));",
                         "    read_regs(REG_CALIB, calib, sizeof(calib));\n    write_reg(REG_CMD, CMD_SOFTRESET);\n    sleep_ms(STARTUP_MS);"), "FLOW_ORDER"),
    (lambda c: c.replace("read_regs(REG_CALIB, calib, sizeof(calib));", "read_regs(REG_CALIB, calib, 6);"), "FLOW_VALUE_MISMATCH"),
])
def test_flow_errors_are_reported(tmp_path, mutate, code):
    bad = mutate(GOOD)
    assert bad != GOOD
    p, store, ledger, src, f = _project(tmp_path, bad)
    assert _record(p, ledger, src, f)["ok"]
    rep, codes = _flow_codes(p, ledger)
    assert code in codes and not rep.passed, codes


def test_runtime_dependent_delay_is_a_warning_not_a_pass(tmp_path):
    p, store, ledger, src, f = _project(tmp_path, GOOD.replace("sleep_ms(STARTUP_MS);", "sleep_ms(get_wait());"))
    assert _record(p, ledger, src, f)["ok"]
    rep, codes = _flow_codes(p, ledger)
    assert codes == ["FLOW_DELAY_UNVERIFIABLE"] and rep.passed            # warning: honest "cannot tell"


def test_missing_entry_function_is_an_error(tmp_path):
    p, store, ledger, src, f = _project(tmp_path)
    assert _record(p, ledger, src, f, entry="bmp388_begin")["ok"]
    rep, codes = _flow_codes(p, ledger)
    assert codes == ["FLOW_ENTRY_MISSING"] and not rep.passed


def test_configured_device_without_a_sequence_is_an_error(tmp_path):
    p, store, ledger, src, f = _project(tmp_path)
    rep, codes = _flow_codes(p, ledger)
    assert codes == ["FLOW_UNSPECIFIED"] and not rep.passed


def test_record_sequence_only_accepts_facts_and_verbatim_order(tmp_path):
    p, store, ledger, src, f = _project(tmp_path)
    book = SequenceBook(p.root)
    bad_fact = book.record(ledger, "BMP", "init", "bmp_init", [{"op": "write", "reg": "FACT-999"}], src.id, DATASHEET[:60])
    assert not bad_fact["ok"] and "FACT-999" in bad_fact["error"]
    bad_quote = book.record(ledger, "BMP", "init", "bmp_init", [{"op": "write", "reg": f["cmd"]}], src.id, "reset first, then wait 5 ms")
    assert not bad_quote["ok"] and "verbatim" in bad_quote["error"]
    retyped = book.record(ledger, "BMP", "init", "bmp_init", [{"op": "write", "reg": f["cmd"]}], src.id, DATASHEET[:40] + " and then more")
    assert "It matches the source up to " in retyped["error"] and "then the quote has 'and then more'" in retyped["error"], retyped
    no_unit =book.record(ledger, "BMP", "init", "bmp_init", [{"op": "delay_min", "value": f["len"]}], src.id, DATASHEET[:60])
    assert not no_unit["ok"] and "time unit" in no_unit["error"]
    first = _record(p, ledger, src, f)
    again = _record(p, ledger, src, f)
    assert first["ok"] and again.get("replaced") and again["sequence"]["id"] == first["sequence"]["id"]


# GOOD with every status checked (the whole agent verify judges robustness too); same line numbers
GOOD_CHECKED = (GOOD.replace("    i2c_write_blocking(i2c0, ADDR, &reg, 1, true);", "    if (i2c_write_blocking(i2c0, ADDR, &reg, 1, true) != 1) return -1;")
                .replace("    write_reg(REG_CMD, CMD_SOFTRESET);", "    if (write_reg(REG_CMD, CMD_SOFTRESET) < 0) return false;")
                .replace("    read_regs(REG_CALIB, calib, sizeof(calib));", "    if (read_regs(REG_CALIB, calib, sizeof(calib)) < 0) return false;")
                .replace("    write_reg(REG_PWR_CTRL, PWR_NORMAL);", "    if (write_reg(REG_PWR_CTRL, PWR_NORMAL) < 0) return false;")
                .replace("int main(void) { bmp_init(); return 0; }", "int main(void) { return bmp_init() ? 0 : 1; }"))


def test_record_sequence_tool_and_stale_verify_after_new_facts(tmp_path, monkeypatch):
    from embedforge.toolchain import pio
    from embedforge.toolchain.pio import BuildResult
    p, store, ledger, src, f = _project(tmp_path, GOOD_CHECKED)
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    res = belt.call("record_sequence", {"subject": "BMP", "name": "power_on_init", "entry": "bmp_init", "device": f["addr"],
                                        "source_id": "SRC001", "order_quote": "issue a soft reset, wait for the start-up time",
                                        "steps": [{"op": "write", "reg": f["cmd"], "value": f["rst"]}, {"op": "delay_min", "value": f["t"]}]})
    assert res["ok"] and "verify traces" in res["next"]
    monkeypatch.setattr(pio, "build", lambda root, on_line=None: BuildResult(ok=True))
    belt.build()
    v = belt.verify()
    assert not v["passed"] and v["findings"][0]["code"] == "REG_MAP_MISSING"   # a configured device needs its register map
    assert belt.call("record_layouts", {"device": "BMP", "source_id": "SRC-001", "layouts": [
        {"name": "NVM_PAR_T1", "bytes": ["0x31", "0x32"], "quote": "0x32 NVM_PAR_T1<15:8> NVM_PAR_T1 16 Bit unsigned 0x31 NVM_PAR_T1<7:0>"}]})["ok"]
    r = belt.call("record_registers", {"device": "BMP", "source_id": "SRC-001", "registers": [
        {"name": "CMD", "addr": "0x7E", "quote": "Register 0x7E CMD: Bit 7..0 cmd, 0xB6 softreset.",
         "fields": [{"name": "cmd", "bits": "7:0", "values": {"softreset": "0xB6"}}]},
        {"name": "PWR_CTRL", "addr": "0x1B", "quote": "Register 0x1B PWR_CTRL: Bit 0 press_en, Bit 1 temp_en, Bit 5..4 mode 00 sleep mode 11 normal mode.",
         "fields": [{"name": "press_en", "bits": "0"}, {"name": "temp_en", "bits": "1"}, {"name": "mode", "bits": "5:4", "values": {"sleep": "00", "normal": "11"}}]}]})
    assert r["ok"] and "rejected" not in r, r
    v = belt.verify()
    assert v["passed"] and v["flows"][0]["steps"] == ["1. write: matched (src/main.c:21)", "2. delay_min: matched"]
    assert belt.finish("success", "done")["ok"]
    belt.ended = None
    ledger.record("BMP", "chip_id_reg", "0x31", src.id, "start at register 0x31")       # a new fact after verify
    assert "changed after the last verify" in belt.finish("success", "done")["error"]


# ------------------------------------------------------------------ truth holes


@pytest.mark.parametrize("path", ["CMakeLists.txt", "src/CMakeLists.txt", "src/memmap.ld", "cmake/x.cmake", "src/link.lds".replace(".lds", ".ld"), "tools/a.rb"])
def test_build_and_linker_scripts_are_not_agent_writable(path):
    assert check_agent_write(path, "") is not None


def test_linker_and_generated_flash_files_change_the_build_hash(tmp_path):
    p = Project("h1", tmp_path / "h1")
    store = SourceStore(p.root)
    belt = ToolBelt(p, RunSession("r", p.id), store, FactLedger(p.root, store))
    (p.root / "src").mkdir(exist_ok=True)
    (p.root / "src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    h0 = belt.source_hash()
    (p.root / "src" / "memmap.ld").write_text("MEMORY { FLASH : ORIGIN = 0x10000000, LENGTH = 2M }\n", encoding="utf-8")
    h1 = belt.source_hash()
    cfg = p.root / "betaflight" / "configs" / "CUST" / "B"
    cfg.mkdir(parents=True)
    (cfg / "config.mk").write_text("FLASH_SIZE = 2048\n", encoding="utf-8")
    h2 = belt.source_hash()
    assert len({h0, h1, h2}) == 3


# ------------------------------------------------------------------ patterns learned from real drivers (corpus)
def test_function_pointer_stored_by_the_caller_is_followed():
    code = """typedef struct { int (*write)(uint8_t, const uint8_t *, uint32_t, void *); void *intf; } dev_t;
static int my_write(uint8_t reg, const uint8_t *data, uint32_t len, void *intf) {
    uint8_t buf[2] = {reg, data[0]}; return i2c_write_blocking(i2c0, 0x76, buf, 2, false); }
static int soft_reset(dev_t *dev) { uint8_t cmd = 0xB6; return dev->write(0x7E, &cmd, 1, dev->intf); }
static dev_t sensor;
void init(void) { sensor.write = &my_write; soft_reset(&sensor); }"""
    t = FlowModel({"a.c": code}).trace("init")
    assert [e.describe() for e in t.events] == ["write 0x7E <- 0xB6"] and t.events[0].addr == 0x76
    assert "my_write" in t.events[0].chain


def test_const_vtable_is_followed():
    code = """struct bus_io { int (*write)(const void *bus, uint8_t reg, uint8_t val); };
static int reg_write_i2c(const void *bus, uint8_t reg, uint8_t val) { return i2c_reg_write_byte(bus, 0x76, reg, val); }
const struct bus_io bus_io_i2c = { .write = reg_write_i2c, };
int init(const struct cfg *cfg) { return cfg->bus_io->write(&cfg->bus, 0x7E, 0xB6); }"""
    assert [e.describe() for e in FlowModel({"a.c": code}).trace("init").events] == ["write 0x7E <- 0xB6"]


VENDOR_STYLE = r'''#include <Arduino.h>
typedef int8_t (*read_fptr_t)(uint8_t reg, uint8_t *data, uint32_t len, void *intf_ptr);
struct quant { double par_t1; double t_lin; };
struct calib { struct quant quantized; };
struct dev_t { read_fptr_t read; void *intf_ptr; struct calib calib_data; };
static uint8_t addr = 0x76;
static struct dev_t dev;
static int8_t rd(uint8_t reg, uint8_t *data, uint32_t len, void *intf_ptr) { return 0; }
static int8_t null_ptr_check(const struct dev_t *d)
{
    if ((d == NULL) || (d->read == NULL) || (d->intf_ptr == NULL) || !d->read) {
        return -1;
    }
    return 0;
}
static void parse_calib(struct dev_t *d)
{
    struct quant *q = &d->calib_data.quantized;
    q->par_t1 = 256.0;
}
static double compensate(struct calib *c) { return c->quantized.par_t1 * 2; }
void setup()
{
    dev.read = rd;
    dev.intf_ptr = &addr;
    if (null_ptr_check(&dev) != 0) {
        return;
    }
    parse_calib(&dev);
}
void loop()
{
    double t = compensate(&dev.calib_data);
    Serial.println(t);
}
'''


def test_vendor_style_drivers_keep_their_values(tmp_path):
    """Bosch's BMP3 API: a pointer declared to a nested member (struct quant *q = &d->calib_data.quantized) writes into
    that member, and a callback or an object's address is never NULL, so init's failure path is not taken and the
    calibration survives into loop()."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text(VENDOR_STYLE, encoding="utf-8")
    pw = FlowModel.from_dir(tmp_path).run(["setup", "loop"], probe={0: 0})
    vals = {(v[2], v[0]) for v in pw.values}
    assert ("dev.calib_data.quantized.par_t1", 256.0) in vals and ("printed by println()", 512.0) in vals, sorted(vals, key=str)
    assert not any(v[0] == -1 for v in pw.values)   # the null check is decided: no failure path
    # C++: a struct's name is a type without 'struct'
    cpp = VENDOR_STYLE.replace("struct quant *q =", "quant *q =")
    (tmp_path / "src" / "main.cpp").write_text(cpp, encoding="utf-8")
    pw = FlowModel.from_dir(tmp_path).run(["setup", "loop"], probe={0: 0})
    assert ("printed by println()", 512.0) in {(v[2], v[0]) for v in pw.values}


def test_struct_members_value_initialised_in_the_constructor_keep_their_fields(tmp_path):
    """The rewritten BMP388 driver: BMP388(...) : _calib{}, _nvm_calib() {} — every _nvm_calib.x = ... was lost, and all
    16 calibration and compensation formulas were reported as never computed."""
    src = r'''#include <Arduino.h>
struct nvm_t { uint16_t t1; };
struct calib_t { double t1; };
class Sensor {
public:
    Sensor(uint8_t addr) : _addr(addr), _count(), _calib{}, _nvm() {}
    void load() { _nvm.t1 = 1000; _calib.t1 = (double)_nvm.t1 / 0.5; }
    double value() { return _calib.t1 + _count; }
private:
    uint8_t _addr;
    int _count;
    calib_t _calib;
    nvm_t _nvm;
};
Sensor s(0x76);
void setup() { s.load(); }
void loop() { double v = s.value(); Serial.println(v); }
'''
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text(src, encoding="utf-8")
    pw = FlowModel.from_dir(tmp_path).run(["setup", "loop"], probe={0: 0})
    vals = {(v[2], v[0]) for v in pw.values}
    assert ("s._nvm.t1", 1000) in vals and ("printed by println()", 2000.0) in vals, sorted(vals, key=str)


def test_values_changed_on_an_uncertain_path_become_unknown_not_wrong():
    code = """void init(int intf) { uint8_t reg = 0x7E; if (intf == 0) { reg = reg | 0x80; }
  uint8_t b[2] = {reg, 0xB6}; i2c_write_blocking(i2c0, 0x76, b, 2, false); }"""
    e = FlowModel({"a.c": code}).trace("init").events[0]
    assert e.kind == "write" and not isinstance(e.reg, int)            # neither 0x7E nor 0xFE is claimed


def test_return_on_an_uncertain_path_does_not_end_the_trace():
    code = """static bool calib(void) { uint8_t c[21]; if (HAL_I2C_Mem_Read(h, 0xEC, 0x88, 1, c, 21, 10) == HAL_OK) { return true; } return false; }
bool init(void) { if (!calib()) { return false; } uint8_t v = 0x33; HAL_I2C_Mem_Write(h, 0xEC, 0xF4, 1, &v, 1, 10); return true; }"""
    assert [e.describe() for e in FlowModel({"a.c": code}).trace("init").events] == ["read 0x88 x21", "write 0xF4 <- 0x33"]


def test_preprocessor_branches_on_sdk_macros_are_walked_as_conditional():
    code = """#define REG_CONFIG _u(0xF5)
int main(void) {
#if !defined(i2c_default)
    puts("no i2c"); return 0;
#else
    uint8_t b[2] = {REG_CONFIG, 0x94}; i2c_write_blocking(i2c_default, 0x76, b, 2, false);
#endif
    return 0; }"""
    ev = FlowModel({"a.c": code}).trace("main").events
    assert [e.describe() for e in ev] == ["write 0xF5 <- 0x94"] and not ev[0].guaranteed

def test_finish_rejects_claims_the_state_does_not_support(tmp_path):
    p, store, ledger, src, f = _project(tmp_path)
    belt = ToolBelt(p, RunSession("r", p.id), store, ledger)
    err = belt.finish("blocked", "Driver skeleton built and verified, but compensation is not done.")["error"]
    assert "verified" in err and "built" in err
    assert belt.finish("blocked", "Tested on the hardware, works fine.").get("error")
    ok = belt.finish("blocked", "Not built yet: the compensation formulas are missing; nothing was verified.")
    assert ok.get("ok"), ok


def test_first_class_member_after_public_is_a_constant():
    """TI TMP117 cold run: `public: static constexpr uint8_t DEFAULT_ADDRESS = 0b1001000;` was the class's first member,
    lost as a constant, so the I2C address the constructor's default argument names stayed unknown."""
    m = FlowModel({"include/t.h": "class T { public:\n static constexpr uint8_t ADDR = 0b1001000;\n"
                                  " T(uint8_t a = ADDR) : _a(a) {}\n void go();\n private: uint8_t _a; };",
                   "src/t.cpp": '#include "t.h"\n#include <Wire.h>\nvoid T::go() { Wire.beginTransmission(_a); Wire.write(1); '
                                "Wire.endTransmission(); }\nT t;\nvoid setup() { t.go(); }"})
    assert "ADDR" in m.consts
    w = m.run(["setup"])
    assert [e.addr for e in w.events if e.kind == "write"] == [0x48]
