# Prototype circuit and bench tools

The tracked drawings describe revision G: two LM358P drive channels, two INA226
sensors, a nominal 3.3 V source reference, and a ground-referenced series pair
of 9 V batteries feeding the LM358 through a relay module. This supersedes the
earlier arrangement with batteries stacked above VSYS. Recorded bench checks
and open-output calibration exist; they do not establish complete hardware
validation. See the [firmware wiring and development record](../firmware/README.md).

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
| Drain current scale | Nominal 1 Ω shunt; calibrated effective installed scale 0.789 Ω |
| Gate current scale | Nominal 680 Ω shunt; provisional 638 Ω in-circuit measurement |
| Supply decoupling | C5 = 100 nF fitted; C6 = 10 µF shown but installation unconfirmed |

The INA226 VBUS and IN+ connections are **before** each shunt; IN− connects to
the DUT terminal. Terminal voltage is `VBUS - raw shunt voltage`. Current is
`(shunt voltage - calibrated open-output baseline) / shunt scale`. Baseline
correction applies to current only. Subtract the manually configured source
voltage from the measured drain/gate terminal voltages to obtain VDS/VGS.
Current acquisition uses a minimum 100 ms settling interval, followed by
triggered INA226 hardware averaging (64 samples by default). The saved
settling and averaging settings control the actual measurement duration.

Pico VBUS and VSYS have no scanner connections in this revision. The onboard
ADC3 VSYS divider does not measure the 3.3 V source reference. The device has
been configured with source = 3.3 V and driver rail = 18.8 V for recorded
calibration; measure the actual rails and enter them in Settings. Factory
defaults in `firmware/model.py` remain a 5 V driver rail and 0 V source, so a
fresh settings file must be configured for this hardware before calibration.

The early 2026-11-02 zero-drive result in `connections.json` is historical.
Later readings/calibration have a higher drain floor; that early reading does
not establish current zero-VDS coverage. The saved device drain start was
changed to 0.1 V. Recalibrate after resistor, supply, source-reference or
acquisition changes. The 5 mA drain and 100 µA gate limits are software checks;
there is no verified hardware current compliance. K1 switches analog power
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

This folder also contains historical diagnostics, calibration tools, UI
replays and native MicroPython checks. Some scripts run on the Pico, drive
outputs, or replace saved calibration/settings; this is not a batch test
suite. Read an individual script's entry point and docstring before using it
on hardware. Earlier scripts may assume superseded wiring or acquisition
settings. Use the [firmware README](../firmware/README.md) for the current
setup and [host tests](../tests) for automated regression checks.

`build_mpy.py` builds/checks public firmware bytecode without connecting to the
Pico; its docstring describes the pinned `mpy-cross` dependency and local
output. `export_scan.py` provides a host-side scan export tool. Local reports,
raw logs and calibration snapshots live under ignored `output/`; links to
them in development records refer to the original bench workspace, not files
available in a fresh checkout.
