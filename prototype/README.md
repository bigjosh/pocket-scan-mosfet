# Prototype circuit and bench tools

The current prototype uses an [MTDELE XL6019 boost module, ASIN
B0FB3PVWV5](https://www.amazon.com/dp/B0FB3PVWV5) in place of its two 9 V
batteries. On 2026-11-04 the owner reported its output set to **19.0 V**, then
moved its input from Pico 3V3 to **Pico VBUS / physical pin 40**. The owner then
reported a steady **19 V output** and that the case was closed. The meter check's
load conditions were not recorded; converter input margin and loaded output
regulation remain unverified.
The MOSFET source and PWM/bias reference remain on Pico 3V3.

With the case closed, the owner subsequently scanned a **1.7 kΩ resistor
between source and drain up to 10 V** and described the curve as clean and
linear. This is a user-observed loaded check, not an independently captured
measurement. At an actual 10 V across that resistance, expected current is
5.88 mA and resistor dissipation is 58.8 mW. The actual endpoint readings and
resistor tolerance were not supplied, so the current scale is not yet verified.
The scanner does not directly measure the boost rail or the source rail.

The seller's 3 V input claim conflicts with [XLSEMI's XL6019 Rev 1.5
datasheet](https://www.xlsemi.com/datasheet/XL6019-EN.pdf): its operating input
range is **5–40 V**, so the former 3.3 V feed was outside specification. A working
unloaded output did not qualify startup or loaded regulation at that input.
The chip specifies 180 kHz and 5 A switching current; the latter is not a
19 V output-current rating. Nominal USB 5 V is at the specified lower boundary,
so the new VBUS feed needs input-voltage verification under load, including cable
and module losses. Moving the converter off 3V3 removed a load that previously
absorbed MOSFET source-return current; verify that the remaining source-rail
loads absorb that current during a loaded scan before final assembly.

The tracked drawings still describe revision H, before that supply change:
two LM358P drive channels, an INA226 drain
sensor and an INA228 gate sensor, a nominal 3.3 V source reference, and a ground-referenced series pair
of 9 V batteries feeding the LM358 through a relay module. This supersedes the
earlier arrangement with batteries stacked above VSYS. Earlier bench checks do
not establish complete hardware validation. The current
[firmware](../firmware/README.md) uses resistor-based drive math, raw voltage
readback, optional open-output drain-current correction, and raw INA228 gate current.

The INA228 gate module's stock shunt has been replaced with a resistor measured
at **98.8 Ω**. The recommended nominal part remains 100 Ω, 1% metal-film;
the stock shunt must not remain in parallel. Ten identity reads per channel on
2026-11-04 confirmed drain INA226 `0x2260` and gate INA228 `0x2281`.
The mixed firmware is installed and passes digital sensor-protocol checks with
both PWMs held low. Analog wiring is now connected, gate VBUS tied to IN+,
and an unloaded gate sweep passed with raw apparent current below 0.03 µA
magnitude. A user-observed 1.7 kΩ loaded sweep was clean and linear; numerical
loaded accuracy and fresh open-output calibration residuals have not been
independently documented. The initial
I²C-only results in the generated JSON remain historical.
Both address pins go to GND for gate address `0x40`; retain drain at `0x44`.
Recalibrate with open outputs after the change; the old baseline does not apply.

## Circuit reference

For accessible checks with the case closed, **Settings → Advanced → Debug**
shows independently editable drain/gate targets to GND, applied PWM and live raw
drain/gate VBUS ADC voltages to GND (at IN+, before each shunt), plus currents.
Entry and opening the editor keep PWM off. Keypad OK immediately applies both
targets; keypad Cancel returns with both off. Main-page OK also applies them,
and CANCEL stops and returns to Advanced. PWM lines show red OFF when disabled
or current duty when enabled, with a large ON/OFF badge. It uses the
same sensor scales and correction policy as scans, and does not record data.
The raw VBUS readouts still do not measure the boost or source
rails directly.
On 2026-11-05, the owner confirmed normal operation after the immediate-apply
and ON/OFF display update. This is a functional check, not a numerical accuracy
qualification; the firmware guide records the measured checks separately.

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
| Sensor bus | I2C1: GP18/SDA pin 24, GP19/SCL pin 25; 3.3 V logic |
| Sensor addresses | Drain INA226 0x44 (A1 high/A0 low); gate INA228 0x40 (A1 low/A0 low) |
| Source and drain bias reference | Pico 3V3(OUT), physical pin 36; configured manually |
| Ground | Pico physical pin 38; INA grounds and LM358 pin 4; previous battery negative connection was here |
| Driver supply | MTDELE XL6019 B0FB3PVWV5, fed from Pico VBUS / pin 40; last output setting 19.0 V; loaded verification and relay routing pending |
| Relay module | VCC and ACTIVATE tied to 3.3 V; GND common; no GPIO control |
| Drain filter/bias | R1 = 1 kΩ, 5% fitted; C1 = 10 µF; R13 = 6.8 kΩ from 3.3 V to LM358 pin 3 |
| Gate filter | R6 = 1 kΩ nominal, not remeasured; C3 = 10 µF |
| Both feedback dividers | 68 kΩ output-to-inverting input; 10 kΩ inverting input-to-GND |
| Drain current scale | Nominal 1 Ω shunt; previously measured effective installed scale 0.789 Ω |
| Gate current scale | 98.8 Ω measured replacement; 100 Ω, 1% recommended nominal part |
| Supply decoupling | C5 = 100 nF fitted; C6 = 10 µF shown but installation unconfirmed |

Both sensors' VBUS and IN+ connections are **before** each shunt; IN− connects to
the DUT terminal. Terminal voltage is `VBUS - raw shunt voltage`. Drain current is
`(shunt voltage - baseline(VBUS)) / shunt scale` when calibrated, otherwise the
raw shunt voltage divided by the same scale. Gate current always uses raw shunt
voltage divided by 98.8 Ω, with no baseline correction. Voltage reconstruction always uses
the raw shunt drop. Subtract the manually configured source voltage from the
measured drain/gate terminal voltages to obtain VDS/VGS.
Current acquisition uses a minimum 100 ms settling interval, followed by
triggered hardware averaging (64 samples by default). Drain conversions are
1.1 ms each for bus/shunt; gate conversions are 1.052 ms each, without temperature
conversion. The slower drain sets the nominal 140.8 ms block time at 64 averages.
The saved
settling and averaging settings control the actual measurement duration.

Pico VBUS now powers the boost input; VSYS has no external scanner connection. The onboard
ADC3 VSYS divider does not measure the 3.3 V source reference. The device has
previously been configured with source = 3.3 V and driver rail = 18.8 V. These
remain the factory defaults in `firmware/model.py`. The new converter setting is
19.0 V: enter **Driver supply (V+) = 19.0 V** in Advanced before fresh calibration.
The converter setting is a user report, not an automatic rail readback. Measure
the actual rails and enter them in Settings. The fixed drain bias/PWM reference is
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
Only the drain offset is subtracted during scans; both source-relative zero PWM
anchors remain available.
Both channels stop on an actual full-scale signed shunt readback. Drain uses
16-bit endpoints **0x7fff / 0x8000**; gate uses 20-bit endpoints
**0x7ffff / 0x80000** (register bits 23:4). The corresponding current is about
**±104 mA** with the 0.789 Ω drain scale and **±1.6583 mA** with the fitted
98.8 Ω gate shunt. The INA228 wide range is ±163.84 mV, giving 312.5 nV per
count or 3.164 nA with 98.8 Ω; this ADC resolution is not calibrated accuracy. There are no separate current-trip
thresholds or programmed shunt-limit comparators. The live Ig value and label
turn red above 1 µA magnitude as a visual warning; that does not stop acquisition.
Completed points remain available for partial upload; a fault before the first
point produces no upload.
Cancel, communication/configuration errors, conversion timeouts and malformed
ADC data still stop acquisition. Bus-register full scale is a normal recorded
value; invalid sign/reserved bits are read faults.
There is no verified hardware current compliance. In the revision-H battery
wiring, K1 switches analog power while 3.3 V is present and remains energized
through soft reset. Relay routing after the converter change has not been
independently recorded. Its internals, current draw, suppression and
power-transition behavior remain unverified.

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
Check the actual manufacturer/package for a different device. INA226/INA228 pin
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
[TI INA226](https://www.ti.com/lit/ds/symlink/ina226.pdf),
[TI INA228](https://www.ti.com/lit/ds/symlink/ina228.pdf), and
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

`manual_debug_smoke.py` tests the manual page on a fresh Pico interpreter with
the real LCD and production buffers, but synthetic sensor readings and drive
commands. It checks target editing, apply/cancel, fault shutdown, bounded
measurement storage and the Settings restart without driving hardware, writing
files or using Wi-Fi. Compile it separately as a temporary test module, remove
it afterward, then restart the normal application. `render_firmware_ui.py`
includes illustrative manual-page previews with outputs both off and on.

`baseline_live_check.py` performs a physical calibration with both outputs open,
the DUT, loads and probes removed, and both shunts unbridged. It writes a separate
staged binary, reloads it, then measures unloaded residual currents at seven
voltage pairs using sixteen fresh hardware-average blocks per pair. It reports
the corrected drain residual and raw gate current without an accuracy pass/fail
threshold. Only after those checks
finish does it atomically replace the committed baseline. Cancel, a read fault
or a timeout before that replacement preserves the previous baseline. This
helper drives both channels and writes the baseline, but leaves settings and
credentials unchanged and uploads no scan. Its fixture guard still requires the
previous 3.3 V source / 18.8 V driver settings. For the new 19.0 V converter,
use the firmware's Advanced calibration action; update and verify the helper's
fixture guard before using that bench utility again.
The 2026-11-03 check, using the previous two-INA226 circuit, measured maximum
averaged residuals of 0.198 µA drain and 0.094 µA gate; these are unloaded offset results, not per-point accuracy or gain
specifications. See the [validation details](../firmware/README.md#development-checks).
Local reports, device baselines and snapshots are not supplied in a clone.
