"""Verify benchmark: does `verify` pass correct drivers and catch planted bugs?

No LLM is involved. The ground truth for the fictional DM388 (datasheet.md) is recorded through the same gates the agent
uses (FactLedger, RegisterBook, SequenceBook); then each framework's correct driver must verify with no ERROR, and each
mutant (a driver with one planted bug) must produce an ERROR the correct driver does not have."""

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from embedforge.agent.workspace import Project
from embedforge.truth import FactLedger, SourceStore, verify_project
from embedforge.truth.registers import RegisterBook
from embedforge.truth.sequences import SequenceBook
from embedforge.verifybench.drivers import FRAMEWORKS, IDIOMS, MUST_FAIL, MUST_FAIL_TRUTH, driver, extra_files, mutants

DATASHEET = (Path(__file__).parent / "datasheet.md").read_text(encoding="utf-8")
DEVICE = "DM388"

# key, property, value, unit, verbatim quote
FACTS = [
    ("addr", "i2c_address", "0x76", "", "The 7-bit I2C address of the DM388 is 0x76 when SDO is connected to GND."),
    ("spi_dummy", "spi_dummy_bytes", "1", "", "The DM388 sends 1 dummy byte before the register data of every SPI read."),
    ("chip_id_reg", "chip_id_reg", "0x00", "", "Table 3: Register 0x00 “CHIP_ID”"),
    ("chip_id", "chip_id", "0x50", "", "Chip identification code, fixed to 0x50"),
    ("cmd_reg", "cmd_reg", "0x7E", "", "write the softreset command 0xB6 to the CMD register 0x7E"),
    ("softreset", "softreset_cmd", "0xB6", "", "write the softreset command 0xB6 to the CMD register 0x7E"),
    ("startup", "startup_time", "2", "ms", "wait at least 2 ms before any other register access"),
    ("calib_reg", "calib_first_reg", "0x31", "", "The calibration data is read as one block of 21 bytes starting at register 0x31."),
    ("calib_len", "calib_len", "21", "", "The calibration data is read as one block of 21 bytes starting at register 0x31."),
    ("pwr_reg", "pwr_ctrl_reg", "0x1B", "", "Table 5: Register 0x1B “PWR_CTRL”"),
    ("pwr_normal", "pwr_ctrl_normal", "0x33", "", "Writing 0x33 to PWR_CTRL enables pressure and temperature measurement in normal mode."),
    ("osr_reg", "osr_reg", "0x1C", "", "Table 6: Register 0x1C “OSR”"),
    ("osr_value", "osr_setting", "0x0B", "", "The recommended OSR setting is 0x0B (pressure x8, temperature x2)."),
    ("odr_reg", "odr_reg", "0x1D", "", "Table 7: Register 0x1D “ODR”"),
    ("odr_value", "odr_setting", "0x02", "", "The ODR setting 0x02 selects an output data rate of 50 Hz."),
    ("data_reg", "data_first_reg", "0x04", "", "Data readout is done by starting a burst read of 6 bytes from register 0x04 to 0x09."),
    ("data_len", "data_len", "6", "", "Data readout is done by starting a burst read of 6 bytes from register 0x04 to 0x09."),
]


# the boards' wiring (an imported schematic's lines), for the pins the SPI drivers drive: key, property, value, line
BOARD = ("U1 pin 22 (GPIO17) -> net DM388_CS -> U2.6 (CSB)    [Pico board]\n"
         "J1 pin 10 (GPIO10) -> net DM388_CS -> U2.6 (CSB)    [Arduino board]\n")
PIN_FACTS = [
    ("cs_pico", "cs_pin_pico_board", "GPIO17", "U1 pin 22 (GPIO17) -> net DM388_CS -> U2.6 (CSB)"),
    ("cs_arduino", "cs_pin_arduino_board", "GPIO10", "J1 pin 10 (GPIO10) -> net DM388_CS -> U2.6 (CSB)"),
]


def _cut(start: str, end: str) -> str:
    i = DATASHEET.index(start)
    return DATASHEET[i:DATASHEET.index(end, i) + len(end)]


def _row(text: str) -> str:
    i = DATASHEET.index(text)
    return DATASHEET[DATASHEET.rfind("\n", 0, i) + 1:DATASHEET.find("\n", i)]


def build_truth(root: Path, facts_only: bool = False, formulas: bool = True) -> Dict[str, str]:
    """Record the DM388 ground truth into a project at `root`; returns key -> FACT id. Every record must pass its gate.
    facts_only: the facts but no register map and no sequence (what a careless run leaves behind). formulas=False: all
    of it but the conversion formulas."""
    p = Project("verifybench", root)
    store = SourceStore(p.root)
    src = store.add("https://example.com/dm388-datasheet.pdf", "DM388 datasheet", DATASHEET, kind="pdf")
    ledger = FactLedger(p.root, store)
    ids: Dict[str, str] = {}
    for key, prop, value, unit, quote in FACTS:
        r = ledger.record(DEVICE, prop, value, src.id, quote, unit)
        if not r.get("ok"):
            raise RuntimeError(f"ground truth fact {key} rejected: {r.get('error')}")
        ids[key] = r["fact"]["id"]
    board = store.add("user://board", "Imported schematic", BOARD, kind="user")
    for key, prop, value, quote in PIN_FACTS:
        r = ledger.record(DEVICE, prop, value, board.id, quote)
        if not r.get("ok"):
            raise RuntimeError(f"ground truth pin {key} rejected: {r.get('error')}")
        ids[key] = r["fact"]["id"]
    if facts_only:
        return ids
    book = RegisterBook(p.root)
    regs = [
        {"name": "CHIP_ID", "addr": "0x00", "id_value": "0x50", "quote": _cut("Table 3: Register 0x00", "fixed to 0x50 |"),
         "fields": [{"name": "chip_id", "bits": "7:0"}]},
        {"name": "STATUS", "addr": "0x03", "quote": _cut("Table 4: Register 0x03", "Temperature data ready |"),
         "fields": [{"name": "cmd_rdy", "bits": "4"}, {"name": "drdy_press", "bits": "5"}, {"name": "drdy_temp", "bits": "6"}]},
        {"name": "PWR_CTRL", "addr": "0x1B", "quote": _cut("Table 5: Register 0x1B", "11 normal mode |"),
         "fields": [{"name": "press_en", "bits": "0", "values": {"press_off": 0, "press_on": 1}},
                    {"name": "temp_en", "bits": "1", "values": {"temp_off": 0, "temp_on": 1}},
                    {"name": "mode", "bits": "5:4", "values": {"sleep": "00", "forced": ["01", "10"], "normal": "11"}}]},
        {"name": "OSR", "addr": "0x1C", "quote": _cut("Table 6: Register 0x1C", "| Bit 5..3 | osr_t |"),
         "fields": [{"name": "osr_p", "bits": "2:0"}, {"name": "osr_t", "bits": "5:3"}]},
        {"name": "ODR", "addr": "0x1D", "quote": _cut("Table 7: Register 0x1D", "allowed values 0..17 |"),
         "fields": [{"name": "odr_sel", "bits": "4:0"}]},
        {"name": "CMD", "addr": "0x7E", "quote": _cut("Table 8: Register 0x7E", "Triggers a reset |"),
         "fields": [{"name": "cmd", "bits": "7:0", "values": {"softreset": "0xB6"}}]},
    ]
    r = book.record_registers(store, ledger, DEVICE, src.id, regs)
    if r.get("rejected") or len(r.get("recorded") or []) != len(regs):
        raise RuntimeError(f"ground truth registers rejected: {r}")
    lay = []
    for name, addrs, signed in [("NVM_PAR_T1", ["0x31", "0x32"], False), ("NVM_PAR_T2", ["0x33", "0x34"], False),
                                ("NVM_PAR_T3", ["0x35"], True)]:
        rows = [_row(f"| {a} | {name}<") for a in reversed(addrs)]
        lay.append({"name": name, "bytes": addrs, "signed": signed, "quote": "\n".join(rows)})
    lay.append({"name": "UP", "bytes": ["0x04", "0x05", "0x06"], "quote": _cut("| Register | 0x06 DATA_2", "PRESS_XLSB_7_0 |")})
    lay.append({"name": "UT", "bytes": ["0x07", "0x08", "0x09"], "quote": _cut("| Register | 0x09 DATA_5", "TEMP_XLSB_7_0 |")})
    r = book.record_layouts(store, DEVICE, src.id, lay)
    if r.get("rejected") or len(r.get("recorded") or []) != len(lay):
        raise RuntimeError(f"ground truth layouts rejected: {r}")
    r = book.record_registers(store, ledger, DEVICE, src.id, [], bursts=[
        {"name": "data", "start": "0x04", "end": "0x09",
         "quote": "Shadowing only works if all data registers are read in a single burst read."}])
    if not r.get("bursts"):
        raise RuntimeError(f"ground truth burst rejected: {r}")
    for name, expr in ([("PAR_T1", "NVM_PAR_T1 / 2^-8"), ("PAR_T2", "NVM_PAR_T2 / 2^30"), ("PAR_T3", "NVM_PAR_T3 / 2^48")]
                       if formulas else []):
        r = book.record_formula(store, DEVICE, src.id, name, [f"{name} = {expr}"], f"{name} = {expr}")
        if not r.get("ok"):
            raise RuntimeError(f"ground truth formula {name} rejected: {r}")
    if formulas:
        q = _cut("partial_data1 = (float)(uncomp_temp", "* calib_data->par_t3;")
        r = book.record_formula(store, DEVICE, src.id, "T_LIN", [
            "partial_data1 = (float)(uncomp_temp - calib_data->par_t1)", "partial_data2 = (float)(partial_data1 * calib_data->par_t2)",
            "calib_data->t_lin = partial_data2 + (partial_data1 * partial_data1) * calib_data->par_t3"], q, "degC",
            inputs={"uncomp_temp": "UT"})
        if not r.get("ok"):
            raise RuntimeError(f"ground truth formula T_LIN rejected: {r}")
    steps = [{"op": "write", "reg": ids["cmd_reg"], "value": ids["softreset"]},
             {"op": "delay_min", "value": ids["startup"]},
             {"op": "read", "reg": ids["chip_id_reg"]},
             {"op": "read", "reg": ids["calib_reg"], "value": ids["calib_len"]},
             {"op": "write", "reg": ids["osr_reg"], "value": ids["osr_value"]},
             {"op": "write", "reg": ids["odr_reg"], "value": ids["odr_value"]},
             {"op": "write", "reg": ids["pwr_reg"], "fields": {"mode": "normal", "press_en": 1, "temp_en": 1}}]
    r = SequenceBook(p.root).record(ledger, DEVICE, "power_on_init", "dm388_init", steps, src.id,
                                    "Then read the chip ID, read the calibration data, configure OSR and ODR, and finally enable the "
                                    "measurement in PWR_CTRL.", device=ids["addr"])
    if not r.get("ok"):
        raise RuntimeError(f"ground truth sequence rejected: {r}")
    return ids


@dataclass
class Case:
    framework: str
    mutation: str            # '' for the correct driver
    what: str
    passed: bool
    errors: List[str] = field(default_factory=list)   # 'CODE file:line message'
    caught: Optional[bool] = None                     # mutants: an ERROR the correct driver does not have


def _fill(src: str, ids: Dict[str, str]) -> str:
    return re.sub(r"@F\((\w+)\)@", lambda m: ids[m.group(1)], src)


def _verify(template: Path, work: Path, file_name: str, code: str, extra: Optional[Dict[str, str]] = None) -> List[str]:
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(template, work)
    (work / "src").mkdir(exist_ok=True)
    (work / "src" / file_name).write_text(code, encoding="utf-8")
    for rel, text in (extra or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text, encoding="utf-8")
    store = SourceStore(work)
    rep = verify_project(work, FactLedger(work, store))
    return [f"{f.code} {f.file}:{f.line} {f.message[:160]}" for f in rep.findings if f.severity == "ERROR"]


def run(frameworks: Optional[List[str]] = None, with_mutants: bool = True, workdir: Optional[Path] = None) -> List[Case]:
    base = Path(workdir or tempfile.mkdtemp(prefix="verifybench-"))
    template, bare, no_formulas = base / "truth", base / "facts_only", base / "no_formulas"
    for t in (template, bare, no_formulas):
        if t.exists():
            shutil.rmtree(t)
    ids = build_truth(template)
    build_truth(bare, facts_only=True)
    build_truth(no_formulas, formulas=False)
    truths = {"facts_only": bare, "no_formulas": no_formulas}
    cases: List[Case] = []
    names = frameworks or list(FRAMEWORKS) + list(IDIOMS) + list(MUST_FAIL)
    for fw in [n for n in names if n in MUST_FAIL]:   # unchecked code must not pass
        name, src = driver(fw)
        errors = _verify(truths[MUST_FAIL_TRUTH.get(fw, "facts_only")], base / "run", name, _fill(src, ids))
        cases.append(Case(fw, "must_fail", "verify cannot check this code, so it must not pass", not errors, errors, caught=bool(errors)))
    for fw in [n for n in names if n not in MUST_FAIL]:
        name, src = driver(fw)
        extra = extra_files(fw)
        errors = _verify(template, base / "run", name, _fill(src, ids), extra)
        cases.append(Case(fw, "", "correct driver", not errors, errors))
        if not with_mutants:
            continue
        base_codes = {e.split(" ", 1)[0] for e in errors}
        for key, what, msrc in mutants(fw):
            merr = _verify(template, base / "run", name, _fill(msrc, ids), extra)
            new = [e for e in merr if e.split(" ", 1)[0] not in base_codes]
            cases.append(Case(fw, key, what, not merr, merr, caught=bool(new)))
    return cases


def summary(cases: List[Case]) -> Dict[str, object]:
    correct = [c for c in cases if not c.mutation]
    muts = [c for c in cases if c.mutation and c.mutation != "must_fail"]
    blocked = [c for c in cases if c.mutation == "must_fail"]
    return {
        "correct_passed": sum(c.passed for c in correct), "correct_total": len(correct),
        "false_errors": {c.framework: c.errors for c in correct if not c.passed},
        "caught": sum(bool(c.caught) for c in muts), "mutants": len(muts),
        "missed": [f"{c.framework}/{c.mutation}: {c.what}" for c in muts if not c.caught],
        "unchecked_blocked": sum(bool(c.caught) for c in blocked), "unchecked_total": len(blocked),
        "unchecked_passed": [c.framework for c in blocked if not c.caught],
    }
