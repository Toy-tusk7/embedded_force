---
name: sensor-driver
description: How a production-grade driver for an I2C/SPI sensor (barometer, IMU, magnetometer, humidity...) is structured, and what the datasheet must be checked for at each step
---

# Sensor driver playbook

Follow this order; each step names what to record so verify can check it.

## 1. Power-up and identity
- Wait the datasheet's start-up time before the first bus access (record_fact with its unit).
- If the part has a soft reset, send it, then wait the reset time again.
- Read the chip ID and compare it with the datasheet value; retry a bounded number of times, then report failure.
- record_sequence for this order, with the delays as delay_min steps.

## 2. Calibration / trimming data
- Read it once, after reset, in one burst; check that the read succeeded before using a byte.
- record_layouts for every coefficient (bytes least significant first, signedness from the table) and
  record_formula for every conversion (the datasheet's or the vendor reference driver's, quoted).

## 3. Configuration: settings first, the mode last
- Most sensors start in sleep/standby. Write oversampling, filter, output data rate, interrupt and FIFO settings while
  the part is asleep, and select the operating mode (normal/continuous/forced) LAST. Changing settings while the part
  measures is either forbidden or flagged as a configuration error on many parts.
- Read the datasheet's timing rule for the mode you use (conversion time vs. sampling period, settling time) and
  record it with record_constraint (and the registers' reset values in record_registers): verify then checks the rule
  after every register write, so a valid final configuration written in an invalid order still fails.
- If the part has a configuration-error flag (e.g. ERR_REG.conf_err), read it after configuring and report it.

## 4. Choose the settings from the use case
- Datasheets usually have a table of recommended settings per application (weather station, drone, indoor
  navigation, handheld...). Pick the row that matches what the user asked for, use exactly those values, and say
  which row you chose and why. If the user did not say, ask_user or pick the general-purpose row and say so.

## 5. Reading data
- Before the first read, wait at least the maximum conversion time after selecting the mode, or poll the data-ready
  bit (bounded, with a timeout). In continuous mode, do not read faster than the output data rate.
- Read all data registers in one burst (shadowing: reading them separately can mix two measurements).
- Check the read status; on failure return an error instead of converting stale bytes.
- Convert exactly as the recorded formulas say, in the precision the reference uses.

## 6. Robustness (verify enforces these)
- Every bus call's status is checked; every wait on hardware is bounded; a failure path returns failure.
- No blocking forever in setup (e.g. waiting for a USB serial host): give it a timeout.
- Initialise every struct member and variable before use (cppcheck reports the rest).

## 7. Interface of the driver
- begin()/init() returns success or failure; read() returns success plus its outputs in documented units.
- Every hardware constant and every pin carries /* FACT-nnn */.
