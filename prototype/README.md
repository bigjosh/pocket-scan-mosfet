# Prototype circuit and bench tools

The tracked drawings describe revision G: two LM358P drive channels, two INA226
sensors, a nominal 3.3 V source reference, and a ground-referenced series pair
of 9 V batteries feeding the LM358 through a relay module. This supersedes the
earlier arrangement with batteries stacked above VSYS. Earlier bench checks do
not establish complete hardware validation. The current
[firmware](../firmware/README.md) uses resistor-based drive math, raw voltage
readback and optional open-output current-baseline correction.

## Circuit reference

- [Overview](prototype-overview.svg), [drive channels](prototype-drivers.svg),
  and [power/readback](prototype-power-and-sensing.svg).
- Full schematic: [analog channels](full/full-01-analog.svg),
  [power and logic](full/full-02-power-logic.svg),
  [every net](full/full-03-connections.svg), and
  [pin orientation and parts](full/full-04-pinouts-parts.svg).
- [connections.json](full/connections.json) is generated from the same checked
  component/pin table as the connection sheet. Its electrical netlist describes
  the intended circuit, not an independently measured continuity test.

| Function | Current reference |
|---|---|
| Drain PWM | GP20, Pico physical pin 26; 100 kHz |
| Gate PWM | GP21, Pico physical pin 27; 100 kHz |
| INA226 bus | I2C1: GP18/SDA pin 24, GP19/SCL pin 25; 3.3 V logic |
| INA226 addresses | Drain 0x44 (A1 high/A0 low); gate 0x41 (A1 low/A0 high) |
| Source and drain bias reference | Pico 3V3(OUT), physical pin 36; configured manually |
| Ground | Pico physical pin 38; INA grounds, LM358 pin 4, battery negative |
| Driver supply | Two 9 V batteries in series; pack positive through K1 COM/NO to LM358 pin 8 |
| Relay module | VCC and ACTIVATE tied to 3.3 V; GND common; no GPIO control |
| Drain filter/bias | R1 = 1 kΩ, 5% fitted; C1 = 10 µF; R13 = 6.8 kΩ from 3.3 V to LM358 pin 3 |
| Gate filter | R6 = 1 kΩ nominal, not remeasured; C3 = 10 µF |
| Both feedback dividers | 68 kΩ output-to-inverting input; 10 kΩ inverting input-to-GND |
| Drain current scale | Nominal 1 Ω shunt; previously measured effective installed scale 0.789 Ω |
| Gate current scale | Nominal 680 Ω shunt; provisional 638 Ω in-circuit measurement |
| Supply decoupling | C5 = 100 nF fitted; C6 = 10 µF shown but installation unconfirmed |

The INA226 VBUS and IN+ connections are **before** each shunt; IN− connects to
the DUT terminal. Terminal voltage is `VBUS - raw shunt voltage`. Current is
`(shunt voltage - baseline(VBUS)) / shunt scale` when calibrated, otherwise the
raw shunt voltage divided by the same scale. Voltage reconstruction always uses
the raw shunt drop. Subtract the manually configured source voltage from the
measured drain/gate terminal voltages to obtain VDS/VGS.
Current acquisition uses a minimum 100 ms settling interval, followed by
triggered INA226 hardware averaging (64 samples by default). The saved
settling and averaging settings control the actual measurement duration.

Pico VBUS and VSYS have no scanner connections in this revision. The onboard
ADC3 VSYS divider does not measure the 3.3 V source reference. The device has
been configured with source = 3.3 V and driver rail = 18.8 V. These are also the
factory defaults in `firmware/model.py`. Measure the
actual rails and enter them in Settings. The fixed drain bias/PWM reference is
nominal 3.3 V; resistor and reference constants live in `model.py`.

The early 2026-11-02 zero-drive result in `connections.json` is historical.
The 2026-11-03 open-output check found the drain floor near 3.42 V with the
source configured as 3.3 V. The factory drain start is 0.1 V; an unattainable
command still records the actual measured voltage. Baseline calibration records
the closest source-relative zero PWM and its residual; an unreachable zero
cannot be corrected in software. A bracketed
zero within 100 mV anchors nominal drive math for scans. Boot and emergency
shutdown continue to force raw PWM low. Gain remains nominal and the actual VDS
is recorded. Run calibration with both outputs open and shunts unbridged; its
voltage-dependent current offsets are saved separately in `current-baseline.bin`.
Both channels stop on an actual full-scale INA226 shunt readback:
**0x7fff** in the positive direction or **0x8000** in the negative direction.
The corresponding current is about **±104 mA** with the 0.789 Ω drain scale
and **±128 µA** with the 638 Ω gate scale. There are no separate current-trip
thresholds or programmed shunt-limit comparators. The live Ig value and label
turn red above 1 µA magnitude as a visual warning; that does not stop acquisition.
Completed points remain available for partial upload; a fault before the first
point produces no upload.
Cancel, communication/configuration errors, conversion timeouts and malformed
ADC data still stop acquisition. Bus-register full scale is a normal recorded
value; its reserved high bit is a read fault.
There is no verified hardware current compliance. K1 switches analog power
while 3.3 V is present and remains energized through soft reset. Its internals,
current draw, suppression and power-transition behavior remain unverified.

## Fixture numbers are not MOSFET lead numbers

**J1 is the scanner fixture: 1 = Drain, 2 = Gate, 3 = Source.** For the
**onsemi 2N7000G** TO-92 package, looking at the **flat marked face with leads
pointing down**, left to right is **1 = Source, 2 = Gate, 3 = Drain**. Thus:

| Scanner fixture | onsemi 2N7000G lead |
|---|---|
| J1.1 Drain | Pin 3, right |
| J1.2 Gate | Pin 2, middle |
| J1.3 Source | Pin 1, left |

This mapping follows the manufacturer's
[2N7000G datasheet, pages 1 and 4 (Mouser mirror)](https://www.mouser.com/datasheet/2/308/2N7000-D-33553.pdf).
Check the actual manufacturer/package for a different device. INA226 pin
numbers in the drawings likewise identify the **DGS-10 IC**, not positions on
an unidentified breakout header. Use the breakout's signal labels and
continuity tracing. Relay labels identify module terminal functions.

## Regenerate the drawings

From the repository root, using Python 3:

```text
python -m pip install reportlab
python prototype/draw_sketch.py
python prototype/draw_full_schematic.py
```

`draw_sketch.py` uses only the standard library and writes the three overview
SVGs. `draw_full_schematic.py` requires ReportLab and currently uses Windows
Arial fonts at `C:\Windows\Fonts\arial.ttf` and `arialbd.ttf`. It checks that
every declared terminal has exactly one net/NC assignment, writes four SVGs
and `full/connections.json`, and renders an A3 landscape PDF at
`output/pdf/pocket-scanner-full-schematic.pdf`. SVGs and the connection JSON
are tracked; PDF, PNG previews and bench outputs are generated local files.
This pin-assignment check is not circuit simulation or physical verification.

Optional PDF previews require Poppler's `pdftoppm` on PATH:

```text
pdftoppm -png -scale-to 2000 output/pdf/pocket-scanner-full-schematic.pdf prototype/full/print-page
```

Primary pin references: [TI LM358](https://www.ti.com/lit/gpn/lm358),
[TI INA226](https://www.ti.com/lit/ds/symlink/ina226.pdf), and
[Raspberry Pi Pico 2 W](https://datasheets.raspberrypi.com/picow/pico-2-w-datasheet.pdf).

## Bench scripts

The current tools build firmware, regenerate drawings, capture timing and
inspect device state. Old calibration and archive/UI probes were retired with
the direct-drive rewrite; Git's initial commit preserves them. Do not replay
those probes against the direct firmware.

Read each tool's docstring before using it on hardware. A native probe may
drive outputs or interrupt an active scan. Physical bench helpers target this
prototype's wiring, settings and connected services; they are not a general
bring-up sequence for another scanner. Review their assumptions before adapting
them to another device. Hardware-free tests live in
`tests/`. `build_mpy.py` builds and verifies bytecode without accessing a
device; `capture_dot_timings.py` passively captures serial timing lines.
`baseline_memory_smoke.py` exercises the new calibration UI, fixed buffers and
corrected payload on the Pico using synthetic measurements and a temporary
in-memory replacement for binary saving. It drives no PWM and writes no files;
run it only on a fresh interpreter and restart normally afterward.

`baseline_live_check.py` performs a physical calibration with both outputs open,
the DUT, loads and probes removed, and both shunts unbridged. It writes a separate
staged binary, reloads it, then measures unloaded residual currents at seven
voltage pairs using sixteen fresh hardware-average blocks per pair. It reports
the residuals without an accuracy pass/fail threshold. Only after those checks
finish does it atomically replace the committed baseline. Cancel, a read fault
or a timeout before that replacement preserves the previous baseline. This
helper drives both channels and writes the baseline, but leaves settings and
credentials unchanged and uploads no scan. Run on a fresh interpreter with the
present 3.3 V source / 18.8 V driver settings, then soft-reset normally afterward.
The 2026-11-03 check measured maximum averaged residuals of 0.198 µA drain and
0.094 µA gate; these are unloaded offset results, not per-point accuracy or gain
specifications. See the [validation details](../firmware/README.md#development-checks).
Local reports, device baselines and snapshots are not supplied in a clone.
