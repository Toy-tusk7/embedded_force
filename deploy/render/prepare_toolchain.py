"""Render build step: install the RP2350 toolchain and compile the Arduino-Pico framework once, so a judge's first
build on the small free instance (0.1 CPU, 512 MB) only compiles their driver.

PlatformIO's core directory and build cache live inside the service's own directory (PLATFORMIO_CORE_DIR,
PLATFORMIO_BUILD_CACHE_DIR in render.yaml), so what this step installs and compiles is deployed with the service.
The sample project uses the same platformio.ini init_project writes for an RP2350B board, so the framework objects
in the cache match the agent's builds. cppcheck (tool-cppcheck) is installed by one `pio check`.

  python deploy/render/prepare_toolchain.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from embedforge.toolchain import pio  # noqa: E402

INI = """[env:weact_rp2350b]
platform = https://github.com/maxgerhardt/platform-raspberrypi.git
board = weact_rp2350b
framework = arduino
monitor_speed = 115200
board_upload.maximum_size = 2097152
"""
MAIN = """#include <Arduino.h>
#include <Wire.h>
#include <SPI.h>
void setup() { Serial.begin(115200); Wire.begin(); SPI.begin(); }
void loop() { delay(1000); }
"""


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="toolchain-")) / "sample"
    (work / "src").mkdir(parents=True)
    (work / "platformio.ini").write_text(INI, encoding="utf-8")
    (work / "src" / "main.cpp").write_text(MAIN, encoding="utf-8")
    print(f"PlatformIO core: {pio.core_dir()}", flush=True)
    b = pio.build(work, on_line=lambda ln: print(ln, flush=True), timeout_s=3600)
    print(f"sample build ok={b.ok} flash={b.flash_used}/{b.flash_total} {b.error}", flush=True)
    c = pio.check(work, timeout_s=1800)
    print(f"cppcheck installed: {c.get('ok')} {c.get('error', '')}", flush=True)
    shutil.rmtree(work.parent, ignore_errors=True)
    return 0 if b.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
