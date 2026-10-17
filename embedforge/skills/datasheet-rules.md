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

## Formulas in PDF text
PDF extraction loses exponents and fraction bars: "2osr_p" is 2^osr_p, "230" next to a coefficient is 2^30.
Write the rule with REGISTER.field names and the quoted numbers; unit factors (1000 for ms to us) are allowed.

## When "not applicable" is right
Only when the rule is about something the code does not use: another mode, an interrupt pin the code never enables,
the FIFO, board-level electrical rules (pull-up values, supply ramp). A rule that limits a value or a timing the code
does set always applies, even if it reads like a description ("the minimum achievable frequency is determined by the
maximum measurement time" constrains the data rate you choose).
