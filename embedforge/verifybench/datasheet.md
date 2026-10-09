# DM388 digital barometric pressure sensor

This datasheet describes the DM388, a fictional part written for the EmbedForge verify benchmark. Its register interface
is modelled on BMP388-class barometers so that real driver styles apply to it.

## 1. Interfaces

The 7-bit I2C address of the DM388 is 0x76 when SDO is connected to GND.

In SPI mode a register read is started by setting bit 7 of the register address. The DM388 sends 1 dummy byte
before the register data of every SPI read.

## 2. Power-on sequence

After power-on, write the softreset command 0xB6 to the CMD register 0x7E and wait at least 2 ms before any other
register access. Then read the chip ID, read the calibration data, configure OSR and ODR, and finally enable the
measurement in PWR_CTRL.

## 3. Register description

### Table 3: Register 0x00 “CHIP_ID”

|  | Name | Description |
|---|---|---|
| Bit 7..0 | chip_id | Chip identification code, fixed to 0x50 |

### Table 4: Register 0x03 “STATUS”

|  | Name | Description |
|---|---|---|
| Bit 4 | cmd_rdy | Command decoder ready |
| Bit 5 | drdy_press | Pressure data ready |
| Bit 6 | drdy_temp | Temperature data ready |

### Table 5: Register 0x1B “PWR_CTRL”

|  | Name | Description |
|---|---|---|
| Bit 0 | press_en | 0 press_off 1 press_on |
| Bit 1 | temp_en | 0 temp_off 1 temp_on |
| Bit 5..4 | mode | 00 sleep mode 01/10 forced mode 11 normal mode |

Writing 0x33 to PWR_CTRL enables pressure and temperature measurement in normal mode.

### Table 6: Register 0x1C “OSR”

|  | Name | Description |
|---|---|---|
| Bit 2..0 | osr_p | 000 x1 001 x2 010 x4 011 x8 100 x16 101 x32 |
| Bit 5..3 | osr_t | 000 x1 001 x2 010 x4 011 x8 100 x16 101 x32 |

The recommended OSR setting is 0x0B (pressure x8, temperature x2).

### Table 7: Register 0x1D “ODR”

|  | Name | Description |
|---|---|---|
| Bit 4..0 | odr_sel | Output data rate 200 Hz / 2^odr_sel, allowed values 0..17 |

The ODR setting 0x02 selects an output data rate of 50 Hz.

### Table 8: Register 0x7E “CMD”

|  | Name | Description |
|---|---|---|
| Bit 7..0 | cmd | 0xB6 softreset Triggers a reset |

## 4. Data registers

### Table 9: Pressure data registers

| Register | 0x06 DATA_2 | 0x05 DATA_1 | 0x04 DATA_0 |
|---|---|---|---|
| name | PRESS_MSB_23_16 | PRESS_LSB_15_8 | PRESS_XLSB_7_0 |

### Table 10: Temperature data registers

| Register | 0x09 DATA_5 | 0x08 DATA_4 | 0x07 DATA_3 |
|---|---|---|---|
| name | TEMP_MSB_23_16 | TEMP_LSB_15_8 | TEMP_XLSB_7_0 |

Data readout is done by starting a burst read of 6 bytes from register 0x04 to 0x09. Shadowing only works if all data
registers are read in a single burst read.

## 5. Calibration coefficients

The calibration data is read as one block of 21 bytes starting at register 0x31.

### Table 11: Trimming coefficients

| Address | Register content | Coefficient | Size | Sign |
|---|---|---|---|---|
| 0x32 | NVM_PAR_T1<15:8> | NVM_PAR_T1 | 16 Bit | unsigned |
| 0x31 | NVM_PAR_T1<7:0> | NVM_PAR_T1 | 16 Bit | unsigned |
| 0x34 | NVM_PAR_T2<15:8> | NVM_PAR_T2 | 16 Bit | unsigned |
| 0x33 | NVM_PAR_T2<7:0> | NVM_PAR_T2 | 16 Bit | unsigned |
| 0x35 | NVM_PAR_T3<7:0> | NVM_PAR_T3 | 8 Bit | signed |

The coefficients are converted to floating point numbers:

`PAR_T1 = NVM_PAR_T1 / 2^-8`
`PAR_T2 = NVM_PAR_T2 / 2^30`
`PAR_T3 = NVM_PAR_T3 / 2^48`

## 6. Temperature compensation

partial_data1 = (float)(uncomp_temp - calib_data->par_t1); partial_data2 = (float)(partial_data1 * calib_data->par_t2);
calib_data->t_lin = partial_data2 + (partial_data1 * partial_data1) * calib_data->par_t3;
