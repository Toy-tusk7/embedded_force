---
name: datasheet-rules
description: How to turn a datasheet's sections into records verify can check (what to record for modes, timing, readout, recommended settings), and when a requirement is really not applicable
---

# Datasheet rules playbook

A claim ("implemented", "not applicable") is checked only by a second reader; a record is checked by verify on every
build. Prefer the record whenever the rule says something about values, order or timing.

## What each kind of section asks for
| Section is about | Record |
|---|---|
| Power-up, reset, start-up time | record_fact (time with unit) + record_sequence (order and delays) |
| A mode the code uses (normal, continuous, forced, sleep) | record_sequence for entering it; record_constraint for what must hold in it |
| Conversion / measurement time, output data rate, sampling period | record_constraint: e.g. when MODE == normal: conversion time <= sampling period |
| Data ready, status bits | record_registers with the status fields; wait or poll before reading (bounded) |
| Readout, shadowing, burst reads | record_registers bursts; record_layouts |
| Compensation / conversion formulas | record_formula (quote the datasheet or the vendor's reference code) |
| Recommended settings per use case | record_fact for the chosen row's values, citing the table row; use them in the code |

## Figures and large datasheets
On a 100-page datasheet, read by section: datasheet_outline, then read_section on the sections the code depends on
(verify lists them; a section they refer to, "see Sec. 3.11", becomes required too). Timing diagrams and mode
diagrams are drawn, so the text extraction loses them: read_section lists each section's figures, and
read_figure(source_id, 'Figure n') transcribes one from the stored PDF. A transcription is the vision model's reading,
not the datasheet's words: record the rule it shows (record_sequence for an order, record_constraint for a limit)
quoting the datasheet text that says the same.

## Registers wider than a byte
Some parts (TI's temperature sensors, current monitors) have 16-bit registers: the register map gives 4-hex-digit
values ('00h R 8000h Temp_Result', '01h R/W 0220h Configuration') and the field tables number bits up to 15. Record
them with width '16' and, once per part, byte_order_quote: the bus section's sentence on which byte of a register is
sent first ('most significant byte first, followed by the least significant byte'). verify then reads each two-byte
write as one 16-bit value, catches bytes put together in the wrong order and transfers of half a register, and asks
for a layout (signed or not) for every 16-bit measurement register the code converts. A conversion stated as
'One LSB equals 7.8125 m°C' is a formula on that layout: T = LSB * 7.8125 / 1000, with inputs={'LSB': '<layout>'}.

## Formulas in PDF text
PDF extraction loses exponents and fraction bars: "2osr_p" is 2^osr_p, "230" next to a coefficient is 2^30.
Write the rule with REGISTER.field names and the quoted numbers; unit factors (1000 for ms to us) are allowed.
Some PDFs garble the letters of an equation too (the BMP388's conversion time reads
"= 234μpp + pppppppppp_ppcc ∙ (392μpp + 2oossff_ss ∙ 2000μpp) + ..."): copy the line as read_section shows it, put
'…' over a garbled name if you like, and keep every number and the 2 before an exponent. A limit like that one is a
record_constraint: when PWR_CTRL.mode == normal: <conversion time from the formula> <= <sampling period from the ODR
table>, quoting the formula line and the table row.

## When "not applicable" is right
Only when the rule is about something the code does not use: another mode, an interrupt pin the code never enables,
the FIFO, board-level electrical rules (pull-up values, supply ramp). A rule that limits a value or a timing the code
does set always applies, even if it reads like a description ("the minimum achievable frequency is determined by the
maximum measurement time" constrains the data rate you choose).
