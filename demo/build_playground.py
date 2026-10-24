"""Build the Verify Playground: a static site that runs EmbedForge's verify in the visitor's browser (Pyodide).

Each demo project is a driver Nemotron wrote and verified, stripped to what verify reads: the sources, the records
(facts, register map, layouts, formulas, rules, sequences, each with its short verbatim datasheet quote) and the list
of sources. The datasheets' full text and the user's board files are left out; verify does not need them.

The bugs offered in the page are checked here: the delivered driver must pass, and every bug must make verify report
an error the driver does not have, so the page never shows a dud. With --compile each bug is also built with
PlatformIO under the strict warning set and checked with cppcheck, to show what the toolchain alone says.

  python demo/build_playground.py [--compile] [--out demo/dist]
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from embedforge.truth import FactLedger, SourceStore, mutate, verify_project  # noqa: E402

HERE = Path(__file__).resolve().parent
RECORDS = ("platformio.ini", "ledger.json", "registers.json", "sequences.json", "requirements.json", "records_retired.json",
           "sources/index.json", "sources/reads.json")
CODE_DIRS = ("src", "include", "lib")

PROJECTS: List[Dict[str, Any]] = [
    {"key": "tmp117", "id": "20261020-180358-f3b1", "chip": "RP2350B",
     "title": "TI TMP117", "kind": "temperature sensor · 16-bit registers",
     "story": "Written cold by Nemotron 3 Ultra from the task alone: it found TI's datasheet with Tavily, quoted every "
              "register, field, formula and timing it used, and passed verify with 0 errors and 0 warnings.",
     "mutation": "20 of 24", "keep_user_text": True,
     "presets": [
         {"title": "Bytes of the temperature swapped", "file": "src/tmp117.cpp",
          "old": "value = (static_cast<uint16_t>(msb) << 8) | lsb;", "new": "value = (static_cast<uint16_t>(lsb) << 8) | msb;",
          "why": "TI sends the most significant byte first; read the other way round, every temperature is wrong."},
         {"title": "Temperature read as unsigned", "file": "src/tmp117.cpp",
          "old": "int16_t LSB = static_cast<int16_t>(raw);", "new": "uint16_t LSB = raw;",
          "why": "The result is two's complement: below 0 °C an unsigned read gives about +500 °C."},
         {"title": "Only half of the configuration written", "file": "src/tmp117.cpp",
          "old": "    _wire->write(static_cast<uint8_t>(value & 0xFF)); // LSB\n", "new": "",
          "why": "A 16-bit register needs both bytes in one transfer."},
         {"title": "Conversion cycle set to 4 s instead of 1 s", "file": "src/tmp117.cpp",
          "old": "config |= (0b100 << 7);  // CONV = 1s", "new": "config |= (0b101 << 7);  // CONV = 1s",
          "why": "One bit in the CONV field: the code still says '1s', the chip would convert every 4 s."},
         {"title": "Device ID check inverted", "file": "src/tmp117.cpp",
          "old": "if (device_id != TMP117_DEVICE_ID_VALUE) {", "new": "if (device_id == TMP117_DEVICE_ID_VALUE) {",
          "why": "On a real TMP117 the driver would give up before configuring it."},
         {"title": "Soft-reset wait cut to 1 ms", "file": "src/tmp117.cpp",
          "old": "delay(TMP117_SOFT_RESET_DELAY_MS);", "new": "delay(1);",
          "why": "TI: the software reset takes 2 ms; talking to the chip earlier is undefined."},
         {"title": "Build not sized for the board's 2 MB flash", "file": "platformio.ini",
          "old": "board_upload.maximum_size = 2097152\n", "new": "",
          "why": "The task said '2 MB flash'; the build would assume the board's default 16 MB."},
     ]},
    {"key": "bmp388", "id": "20261016-002347-cc5f", "chip": "RP2350B",
     "title": "Bosch BMP388", "kind": "pressure sensor · 8-bit registers",
     "story": "Written by Nemotron 3 Ultra for a drone use case. Verify caught real bugs in its first versions (the "
              "first preset is one of them); this is the fixed, verified driver.",
     "mutation": "48 of 49", "keep_user_text": False,
     "presets": [
         {"title": "Nemotron's real bug: normal mode before oversampling", "file": "src/bmp388.cpp",
          "old": "    // Configure oversampling (SEQ-001 step 5)",
          "new": "    if (!_writeRegister(BMP388_REG_PWR_CTRL, BMP388_PWR_CTRL_PRESS_EN | BMP388_PWR_CTRL_TEMP_EN | "
                 "BMP388_PWR_CTRL_MODE_NORMAL)) {\n        return false;\n    }\n\n    // Configure oversampling (SEQ-001 step 5)",
          "why": "Switching normal mode on first runs x8 oversampling at the default 5 ms period, shorter than the "
                 "conversion takes: the datasheet's timing rule breaks until the period is set."},
         {"title": "Calibration multiplied instead of divided", "file": "src/bmp388.cpp",
          "old": "_calib.par_t1 = (double)_nvm_calib.nvm_par_t1 / 0.00390625;",
          "new": "_calib.par_t1 = (double)_nvm_calib.nvm_par_t1 * 0.00390625;",
          "why": "An earlier Nemotron version had this: every temperature comes out wrong."},
         {"title": "Calibration coefficient declared signed", "file": "include/bmp388.h",
          "old": "        uint16_t nvm_par_t2;", "new": "        int16_t nvm_par_t2;",
          "why": "Bosch's table gives NVM_PAR_T2 as unsigned; an earlier Nemotron version declared it signed, which "
                 "breaks the temperature for half of all chips' calibration values."},
         {"title": "Calibration bytes in the wrong order", "file": "src/bmp388.cpp",
          "old": "_nvm_calib.nvm_par_t1 = (uint16_t)((calib_data[1] << 8) | calib_data[0]);",
          "new": "_nvm_calib.nvm_par_t1 = (uint16_t)((calib_data[0] << 8) | calib_data[1]);",
          "why": "The datasheet lists the least significant byte at the lower address."},
         {"title": "Chip ID check inverted", "file": "src/bmp388.cpp",
          "old": "if (chip_id != BMP388_CHIP_ID) {", "new": "if (chip_id == BMP388_CHIP_ID) {",
          "why": "On a real BMP388 the driver would refuse the chip."},
     ]},
]


def strip_project(pid: str, keep_user_text: bool) -> Dict[str, str]:
    """Relative path -> text of what verify reads; datasheet text emptied, board files left out."""
    root = REPO / "workspace" / "projects" / pid
    files: Dict[str, str] = {}
    for d in CODE_DIRS:
        for p in sorted((root / d).rglob("*")) if (root / d).is_dir() else []:
            if p.is_file() and p.suffix.lower() in (".c", ".cpp", ".h", ".hpp", ".ino"):
                files[str(p.relative_to(root)).replace("\\", "/")] = p.read_text(encoding="utf-8")
    for rel in RECORDS:
        if (root / rel).exists():
            files[rel] = (root / rel).read_text(encoding="utf-8")
    index = json.loads(files.get("sources/index.json", "[]"))
    for s in index:
        user = s.get("tier") == "USER" and str(s.get("url", "")).startswith("user://conversation")
        txt = root / "sources" / f"{s['id']}.txt"
        files[f"sources/{s['id']}.txt"] = txt.read_text(encoding="utf-8") if (user and keep_user_text and txt.exists()) else ""
    return files


def materialise(files: Dict[str, str], where: Path) -> Path:
    if where.exists():
        shutil.rmtree(where)
    for rel, text in files.items():
        p = where / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return where


def errors(root: Path, chip: str) -> List[str]:
    rep = verify_project(root, FactLedger(root, SourceStore(root)), pico=True, chip=chip)
    return [f"{f.code} {f.file}:{f.line}" for f in rep.findings if f.severity == "ERROR"]


def apply(files: Dict[str, str], preset: Dict[str, Any]) -> Dict[str, str]:
    out = dict(files)
    text = out[preset["file"]]
    if "old" in preset:
        assert text.count(preset["old"]) == 1, f"{preset['title']}: the text to change is not in {preset['file']} exactly once"
        out[preset["file"]] = text.replace(preset["old"], preset["new"], 1)
    else:   # a planted mutation: one whole line
        lines = text.split("\n")
        lines[preset["line"] - 1] = preset["after"]
        out[preset["file"]] = "\n".join(lines)
    return out


def preset_key(project: Dict[str, Any], p: Dict[str, Any]) -> str:
    return json.dumps([project["id"], p["file"], p.get("old"), p.get("new"), p.get("line"), p.get("after")])


def compile_results(project: Dict[str, Any], files: Dict[str, str], presets: List[Dict[str, Any]], cache: Dict[str, Any]) -> None:
    """Build each bug with the strict warnings and run cppcheck on it (a copy of the project with its build cache)."""
    from embedforge.toolchain import pio

    def key(p: Dict[str, Any]) -> str:
        return preset_key(project, p)
    todo = [p for p in presets if key(p) not in cache]
    if not todo:
        return
    live = REPO / "workspace" / "projects" / project["id"]
    work = Path(tempfile.mkdtemp(prefix="playground-build-")) / "b"
    shutil.copytree(live, work, ignore=shutil.ignore_patterns("builds", "conversation.json", "trace.jsonl", "artifacts"))

    def run(fs: Dict[str, str]) -> Dict[str, Any]:
        for rel in [r for r in fs if r.split("/")[0] in CODE_DIRS or r == "platformio.ini"]:
            (work / rel).write_text(fs[rel], encoding="utf-8")
        b = pio.build(work)
        warns = {(d.file.split("/")[-1], d.line, d.message) for d in b.diagnostics if d.severity == "warning"}
        c = pio.check(work) if b.ok else {"defects": []}
        bugs = {(d["id"], d["file"], d["line"]) for d in c.get("defects") or [] if not str(d["id"]).startswith("misra")}
        return {"ok": b.ok, "warnings": warns, "cppcheck": bugs,
                "first_error": next((f"{d.file.split('/')[-1]}:{d.line}: {d.message}" for d in b.diagnostics if d.severity == "error"), "")}
    base = run(files)
    for p in todo:
        r = run(apply(files, p))
        new_w, new_c = sorted(r["warnings"] - base["warnings"]), sorted(r["cppcheck"] - base["cppcheck"])
        cache[key(p)] = {"compiles": r["ok"], "first_error": r["first_error"],
                         "warning": f"{new_w[0][0]}:{new_w[0][1]}: {new_w[0][2]}" if new_w else "",
                         "cppcheck": f"{new_c[0][0]} at {new_c[0][1]}:{new_c[0][2]}" if new_c else ""}
        print(f"  compiled: {p['title'][:60]:60} compiles={r['ok']} warning={bool(new_w)} cppcheck={bool(new_c)}", flush=True)
    run(files)   # leave the copy as delivered
    shutil.rmtree(work.parent, ignore_errors=True)
    for p in presets:
        p["toolchain"] = cache.get(key(p))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "dist"))
    ap.add_argument("--compile", action="store_true", help="build every bug with the strict warnings and cppcheck")
    args = ap.parse_args()
    out = Path(args.out)
    cache_path = HERE / "toolchain_results.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    tmp = Path(tempfile.mkdtemp(prefix="playground-"))
    bundle = []
    for proj in PROJECTS:
        files = strip_project(proj["id"], proj["keep_user_text"])
        root = materialise(files, tmp / proj["key"])
        base = errors(root, proj["chip"])
        assert not base, f"{proj['key']}: the delivered driver must pass verify here, got {base}"
        presets = []
        for p in proj["presets"]:   # the stories: each must be caught
            found = sorted(set(errors(materialise(apply(files, p), tmp / proj["key"]), proj["chip"])))
            assert found, f"{proj['key']}: verify does not catch '{p['title']}'"
            presets.append({**p, "story": True, "expect": [e.split(" ")[0] for e in found]})
        # more planted faults: the mutation test's own selection, the ones verify catches here
        root = materialise(files, tmp / proj["key"])
        cands = mutate._select(mutate.candidates(root, mutate.write_functions(root)), 6, 60)
        told = {(p["file"], files[p["file"]][:files[p["file"]].find(p["old"])].count("\n") + 1) for p in presets if p.get("old")}
        for m in cands:
            if (m.file, m.line) in told:
                continue
            p = {"title": f"{m.what[0].upper()}{m.what[1:]} (line {m.line})", "file": m.file, "line": m.line, "after": m.after,
                 "before": m.before, "why": "", "operator": m.operator}
            found = sorted(set(errors(materialise(apply(files, p), tmp / proj["key"]), proj["chip"])))
            if found:
                presets.append({**p, "story": False, "expect": [e.split(" ")[0] for e in found]})
        if args.compile:
            compile_results(proj, files, presets, cache)
        else:   # what an earlier --compile measured, if anything
            for p in presets:
                p["toolchain"] = cache.get(preset_key(proj, p))
        print(f"{proj['key']}: delivered driver passes; {sum(p['story'] for p in presets)} story bugs, "
              f"{sum(not p['story'] for p in presets)} planted faults, all caught by verify", flush=True)
        bundle.append({k: v for k, v in proj.items() if k not in ("presets", "keep_user_text")} | {"files": files, "presets": presets})
    if args.compile:
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    # four flat files, so the Space can be filled by dragging them into its web uploader. The package goes as text
    # (path -> source): a Hugging Face Space refuses binary files pushed without Git LFS
    code = {str(p.relative_to(REPO)).replace("\\", "/"): p.read_text(encoding="utf-8")
            for p in sorted((REPO / "embedforge").rglob("*.py")) if "__pycache__" not in p.parts}
    (out / "embedforge.json").write_text(json.dumps(code), encoding="utf-8")
    (out / "projects.json").write_text(json.dumps(bundle), encoding="utf-8")
    shutil.copy(HERE / "playground" / "index.html", out / "index.html")
    shutil.copy(HERE / "playground" / "README.md", out / "README.md")
    print(f"built {out} ({(out / 'embedforge.json').stat().st_size // 1024} KB code, "
          f"{(out / 'projects.json').stat().st_size // 1024} KB projects)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
