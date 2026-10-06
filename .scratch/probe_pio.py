from pathlib import Path
from embedforge.toolchain import pio

print("pio available:", pio.is_available())
for q in ["stm32f401", "esp32 devkit", "rp2040 pico", "nrf52840"]:
    try:
        bs = pio.list_boards(q, limit=3)
        print(q, "->", [(b["id"], b["mcu"], b["platform"], b["frameworks"]) for b in bs])
    except Exception as e:
        print(q, "ERR", e)

r = pio.build(Path(".scratch/pio_probe"))
print(r.ok, r.artifacts, r.flash_used, r.flash_total, r.duration_s, r.error)
