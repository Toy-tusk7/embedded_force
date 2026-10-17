---
name: rp2-arduino-pico
description: RP2040 / RP2350 with the Arduino-Pico (earlephilhower) core - I2C/SPI/UART instances and their pins, Wire pin setup, USB serial, timing calls
---

# RP2040 / RP2350 with Arduino-Pico

## Peripheral instances and pins
- `Wire` is I2C0 and `Wire1` is I2C1. I2C0 SDA is on a GPIO with number % 4 == 0 and SCL on % 4 == 1; I2C1 uses
  % 4 == 2 (SDA) and % 4 == 3 (SCL). verify checks this (PIN_CAPABILITY), so take the pins from the schematic
  (query_board / record_board_pins) and choose the instance that reaches them.
- `SPI` is SPI0 and `SPI1` is SPI1; `Serial1` is UART0 and `Serial2` is UART1. Each pin can carry only one
  instance's function.
- RP2350A has GPIO0-29, RP2350B GPIO0-47.

## Wire setup
- Call `Wire.setSDA(pin)` and `Wire.setSCL(pin)` BEFORE `Wire.begin()`; `Wire.begin(sda, scl)` is not this core's API.
- A register read: `beginTransmission(addr)`, `write(reg)`, `endTransmission(false)` (repeated start; check that it
  returns 0), then `requestFrom(addr, n)` and check it returns n before reading the bytes.
- Tag each pin call with its schematic fact: `Wire.setSDA(16);   /* FACT-nnn */`.

## USB serial
- `Serial` is USB CDC. `while (!Serial)` hangs forever without a PC: bound it,
  e.g. `uint32_t t0 = millis(); while (!Serial && millis() - t0 < 2000) {}`.

## Timing
- `delay(ms)` and `delayMicroseconds(us)` block; `millis()` / `micros()` for timeouts. Datasheet delays are minimums:
  round up, never down.
