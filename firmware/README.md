# Pocket MOSFET scanner firmware

The application runs one RAM-only scan per boot on a Pico 2 W with a GeeekPi /
52Pi EP-0172 Plus kit. PWM uses nominal resistor math, optionally anchored to
measured source-relative zero. A compact open-output baseline corrects drain
current without a voltage feedback loop or target-quality stops. Gate current
always uses raw INA228 shunt readings, without baseline subtraction.

## Controls and flow

Home starts with Wi-Fi off. Physical **left GP15 / pin 20** is Settings/Cancel;
physical **right GP14 / pin 19** is Scan/OK. Touch buttons have the same roles.
Settings edits integer-microvolt VDS/VGS start, stop and positive step values.
Advanced contains source voltage, driver supply, INA averaging, Wi-Fi, cloud,
manual Debug and an explicit open-output calibration action.

Settings OK saves; Cancel discards the draft. Confirming calibration also saves
the draft before the sweep, so cancelling that sweep does not undo those saved
settings. Exiting Settings soft-restarts.
Settings and connection files use atomic JSON writes; scans never write files.

**Settings → Advanced → Debug** opens a manual monitor with both PWMs off.
Tap either target to enter the drain or gate voltage relative to GND.
**Keypad OK accepts the edited value and immediately applies both targets**;
OK on the main Debug page also applies both. Opening the keypad stops both
outputs, and keypad Cancel returns with them off. Invalid input or an apply
failure keeps both outputs off.

The line beneath each target shows red **OFF** while disabled. When enabled,
it shows the applied PWM duty percentage and 16-bit count at 100 kHz, read back
from the hardware after applying; no inactive preview is displayed. A large
ON/OFF badge in the header also shows the output state. Drive calculation still
uses the same nominal resistor math and compatible source-relative zero anchors
as a scan.

Both channels display **Actual VBUS (V to GND)** directly from their bus ADCs,
using freshly triggered hardware averaging. These are the voltages at IN+,
before each shunt, with no source-voltage or shunt-drop subtraction. Manual
targets use that same ground reference, without adding the configured source
voltage. Scan sweep settings still use VDS/VGS. Drain current uses a compatible saved baseline;
gate current is always raw and turns red above 1 µA magnitude. The manual
targets do not change the scan sweep settings or write flash. No readings are
retained as a scan, logged or uploaded; only the latest measurement is kept.
Wi-Fi stays paused while the monitor is open.

**CANCEL stops both outputs and returns to Advanced**. Leaving the monitor,
editing, sensor saturation or an acquisition fault stops its output drive.
PWM-off means raw PWM zero, not zero volts across the DUT: the drain bias and
configured source may remain energized. Settings still exits through a soft
restart. Manual debug does not add current compliance or stop on target error.

SCAN prepares the fixed RAM buffer, paints the chart, and starts acquisition.
It automatically uploads completed results and soft-restarts after the exact
acknowledgement. CANCEL immediately stops outputs, shows **RESTARTING**, discards
the local scan and restarts. DNS/TLS calls are synchronous and can delay input
polling while they execute. Cancel cannot retract an upload Google already saved.

The upload screen shows four stages: **Prepare data**, **Connect**, **Upload data**,
and **Confirm saved**. Each starts with a pending symbol, becomes a check after
success, or an X if that stage fails. The transfer percentage updates from actual
payload bytes accepted by the socket after each chunk; headers and redirects do
not count. At 100%, confirmation remains pending until the exact acknowledgement
arrives. Only changed symbols and percentage are redrawn. All four checks remain
visible for 600 ms before the automatic restart; Cancel is still active.

A nonempty shunt-ADC saturation stop uploads the completed partial
scan. A zero-point stop displays its reason; OK/Cancel discards and restarts.
I²C/read/configuration failures, malformed ADC data and conversion timeouts abort
acquisition. A bus-register full-scale value is recorded normally.
Upload failure displays:

> WiFi Error, Upload Failed
> Please reconnect WiFi in settings and redo scan.

OK/Cancel then restarts. There is no offline queue or retry after reboot.

## Voltage commands and readings

The physical circuit constants are in `model.py`:

- Nominal PWM rail and fixed drain bias: 3.3 V.
- Both LM358 feedback dividers: 68 kΩ / 10 kΩ, gain 7.8.
- Drain input: PWM through 1 kΩ, bias through 6.8 kΩ.
- Drain: `VD = 3.3 + 22.44 × dutyD`.
- Gate: `VG = 25.74 × dutyG`.
- Requested absolute terminal voltage is requested VDS/VGS plus configured VS.
  Invert the corresponding equation, then clamp duty to 0…1.

The drain bias is a circuit constant; it is not inferred from a changing source
setting. For the intended source tied to the same 3.3 V rail and without a
calibrated zero anchor, `dutyD = requested_VDS / 22.44`. A valid source-relative
zero instead uses `duty = captured_zero_duty + requested_relative_V / slope`,
where slope is 22.44 V for drain or 25.74 V for gate. The nominal slope remains
unchanged and duty is still clamped to 0…1. PWM is 100 kHz.

Driver supply and source are manual descriptions. Defaults are source 3.3 V,
driver supply 18.8 V, drain start 0.1 V. Existing version-3 settings are preserved.
The prototype's batteries were replaced on 2026-11-04 by an MTDELE XL6019
converter (B0FB3PVWV5), last set to **19.0 V**. Its input was subsequently moved
from Pico 3V3 to **Pico VBUS / pin 40** because the chip specifies a 5 V minimum.
The owner reported a steady 19 V output after the change and closed the case;
converter input margin and loaded regulation remain unverified. See the
[hardware guide](../prototype/README.md) before final assembly. Set
**Driver supply (V+) = 19.0 V** in Advanced before fresh
calibration; changing this setting makes a baseline captured at 18.8 V
incompatible. Gate current remains raw regardless of calibration.
The supply setting does not correct gain, establish output headroom, or measure
the rail. Actual outputs may be far from requested voltages under load.

Both sensors' VBUS pins connect to IN+ **before the shunt**. For each channel:

```text
terminal_V = bus_V - shunt_V
relative_V = terminal_V - configured_source_V
drain_A    = (drain_shunt_V - drain_baseline_V(drain_bus_V)) / drain_shunt_ohms
gate_A     = gate_shunt_V / 98.8
```

Drain uses an INA226 at `0x44` with 0.789 Ω (previously measured effective
scale). Gate uses an INA228 at `0x40` with its stock shunt replaced by a resistor
measured at **98.8 Ω**, and its ±163.84 mV range. The recommended nominal part
is 100 Ω, 1% metal-film; the old shunt must not remain in parallel. The fitted
gate scale gives 98.8 µV per µA, a 3.164 nA ADC step, and approximately
±1.6583 mA full scale. Resolution is not an accuracy guarantee. Ten identity
reads per channel on 2026-11-04 confirmed drain `0x2260` at `0x44` and gate
`0x2281` at `0x40`. Native digital sensor-protocol checks passed with both
PWMs low. Analog wiring is now connected and an unloaded gate sweep has been
measured. A subsequent user-observed 1.7 kΩ loaded scan looked clean and linear;
numerical loaded accuracy and fresh calibration residuals have not been
independently documented.
A missing or incompatible baseline uses zero subtraction for drain current and
is labeled uncorrected. Gate current remains raw even with a compatible baseline;
Home then shows **Id corrected | Ig raw**. Terminal voltages always subtract the raw shunt voltage, preserving
the real voltage drop even when current is corrected.

## Current baseline and source-relative zero

Use **Settings → Advanced → Calibrate** with both outputs open: disconnect the
DUT, test resistors and meter probes, and remove both shunt jumpers. Confirmation
saves the edited settings before starting. Calibration measures actual sensor
VBUS and signed shunt codes at 53 bulk PWM settings per channel, spaced
quadratically to give more coverage at low voltages. It also retains up to 12
already-measured zero-search points as knots, within the fixed capacity of 65
knots per channel; retaining them adds no measurements. Commanded voltage is
never used as the lookup coordinate. Each knot combines four hardware-average
blocks using the saved settling/averaging settings. Raw ADC saturation and
communication faults still stop both outputs.

The small, fixed binary table holds bus microvolts, shunt nanovolts and PWM
codes. Its format version and SHA-256 detect unsupported or corrupted files;
incompatible settings/hardware disable correction. The mixed-sensor circuit
needs a new calibration; the old two-INA226 baseline loads without applying its
current correction or zero anchor. The unchanged PSB1 format and 1,720-byte
buffer now also fingerprint each sensor's conversion time and shunt range.
Linear interpolation estimates the drain baseline at its measured bus voltage. Outside
the measured interval the nearest endpoint is used, without a new scan stop;
accuracy beyond the calibrated interval is not established. The baseline includes
input bias, ADC offset and any fixture leakage, and may drift with temperature.
It does not determine the shunt's resistance. Recalibrate after changing hardware.
Both channels retain their measured tables and zero searches in the existing
binary format, but the gate-current table is never applied to scan readings.

Calibration also searches for each terminal equal to the configured source
voltage: **VDS = 0 / VGS = 0**, not zero volts to ground. It saves the closest
PWM and measured residual. A bracketed zero with residual at most 100 mV anchors
nominal drive math; a closest-only result is reported and its zero trim is not
used. The search is bounded to 12 refinement measurements per channel.
Emergency shutdown, boot and reset still force both PWM pins low, gate first;
the saved scan zero is not a hardware power-sequencing interlock.

Only a complete calibration atomically replaces `current-baseline.bin`.
Cancellation, acquisition failure or a failed staged write preserves the previous
file. The Settings path restarts after calibration so a scan begins from a fresh
heap. This file is distinct from the retired `calibration.bin` gain tables.

Each data point stores only measured VDS, VGS, Id and Ig. Requested coordinates
and curve indices are derived from the integer sweep grid. All points in a
commanded gate sweep connect on the chart; measured VDS sets X, and actual VGS
is retained even when it misses the command.
The live Ig label and value turn red when the measured current magnitude exceeds
1 µA, returning to their normal colors at or below 1 µA. This uses the reported
raw INA228 gate current, without baseline subtraction. It is a visual warning and
does not change acquisition or stopping behavior.

## Acquisition and stopping

For each voltage pair, the engine sets PWM, waits the configured settling
interval, triggers both sensors, polls each ready flag, and reads their bus/shunt
registers. Sensors measure concurrently after sequential I²C commands.
Default averaging is 64; choices are 1, 4, 16, 64, 128, 256, 512 and 1024.
Drain INA226 bus and shunt conversion times are each 1.1 ms. Gate INA228
uses the nearest available setting, 1.052 ms per conversion, with temperature
conversion disabled. A 64-average block nominally takes 140.8 ms on drain and
134.656 ms on gate; the engine waits for both, in addition to settling and
UI/I²C work. Both use reusable I²C buffers; INA228 measurements are 24-bit
register transfers containing 20-bit ADC codes.

Minimum settling defaults to 100 ms. Incremental rendering of the previous
point overlaps the next point's settling window. Drawing longer than that
minimum naturally extends it. Normal scans have no extra gate/parking calibration
measurements. A source/bias change requires updating configuration/constants and
repeating the baseline/zero calibration before using its correction again.

Current-driven stops use actual signed shunt-ADC endpoints only:

| Channel | Positive / negative endpoint | Shunt ADC step | Approximate current range |
|---|---|---|---|
| Drain INA226 | `0x7fff` / `0x8000` | 2.5 µV | ±104 mA at 0.789 Ω |
| Gate INA228 | `0x7ffff` / `0x80000` (20-bit codes) | 312.5 nV | ±1.6583 mA at 98.8 Ω |

Gate codes occupy register bits 23:4; the four low bits are reserved.
Drain endpoints represent +81.9175 mV / −81.920 mV; gate endpoints represent
+163.8396875 mV / −163.840 mV. There are no separate current-trip thresholds and no
programmed shunt-limit comparators. A full-scale result stops the outputs and
is excluded from the numeric measurements. Cancel and errors disable gate
drive before drain drive.

Bus-register full scale does not stop the scan, and measured voltage has no
range veto. Reserved bits or an invalid negative bus code indicate malformed
data and abort the read.

These checks stop software-driven acquisition; they do not provide hardware
current compliance or disconnect the DUT. The relay follows 3V3 and remains on
through a soft reset. The 3V3 rail must absorb returned source current. ADC
range does not establish the source rail's or amplifier's current capability.

## Memory and transport

Boot preallocates **64 KiB** for 4,096 records of four float32 values (16 bytes
each), the **48 KiB** display raster pool, and a **96 KiB** TLS reserve.
The current baseline and candidate sweep each reserve a fixed **1,720 bytes**
before UI imports. Calibration fills those buffers rather than appending sample
objects or materializing voltage lists. The table writes flash only at completion;
the explicit confirmation also saves settings before the sweep begins.
Plotting and upload share the scan buffer; no per-point dictionaries accumulate.
Settings releases the TLS reserve and exits through restart.

Schema **4** uploads retain uncorrected readings. Schema **5** uses the same four
values with compact current-correction provenance. Metadata version 2 explicitly
sets `applied_to: "drain"`: Sheets labels Id as baseline-corrected and Ig as raw.
Historical metadata version 1 still means both currents were corrected; old
scans retain that meaning. SHA-256 binds the exact streamed payload. The device
requires a matching scan ID, digest, count and Sheet ID before declaring success.
HTTPS certificate verification remains enabled using `google_roots.pem`.
Never replace that with an insecure TLS mode.

Settings, Wi-Fi profiles, cloud URL and the new binary baseline persist. Old
gain-calibration files and scan archives are not read by this firmware.
Historical cloud schemas 1–4 remain supported without rewriting their data.
Deploy the current `cloud/apps-script/Code.gs`, including schema-5
`current_correction.version = 2` support for drain-only correction, before
sending corrected scans. Schema-5 support alone in an older receiver is not
sufficient for the current correction metadata.

## Timing logs

`DOT_US` lines on USB report wall time and drive, settling, conversion,
processing, rendering, I²C and loop-sleep counters. Counter arrays and their
formatting buffer are reused. Work counters overlap phase counters; do not add
them all together. `adc_blocks` is one paired trigger per accepted point.
Rendering generally appears in the next point's settle window. Timing itself
does not write flash.

`prototype/capture_dot_timings.py` passively captures logs without interrupting
a scan. Previous calibrated-firmware timing captures are historical comparisons,
not measurements of this direct path.

## Install or update

The supported target is MicroPython **1.29.0 RPI_PICO2_W**, `_mpy = 7942`.
Use the pinned Windows x64 compiler:

```text
python -m pip install --target .tools/mpy-cross --no-deps --only-binary=:all: --require-hashes -r .tools/mpy-cross-requirements.txt
python prototype/build_mpy.py
python prototype/build_mpy.py --check
```

The hash manifest in `output/firmware-mpy/manifest.json` identifies all current
modules. A stale build or unexpected output inventory fails verification.
`main.py` stays source; other application modules are compiled.

1. Create a private Google Sheet and deploy the schema-5 Apps Script receiver
   using the [receiver setup guide](../cloud/apps-script/README.md). For an
   existing installation, update its deployment while keeping the same URL.
2. Stop the Pico application with both PWM pins low. Back up device settings,
   Wi-Fi/cloud configuration and any old data without printing credentials.
3. Transfer current compiled modules, source `main.py`, and the public CA bundle.
   Matching source `.py` modules shadow `.mpy`, so remove their obsolete copies.
4. Remove retired public calibration/archive modules. Preserve `boot.py`,
   configuration, `current-baseline.bin` and unrelated user files. Retired gain
   calibration data may be backed up and removed; it is not used.
5. Verify transferred hashes, soft-restart, and check the USB Ready message.
   Test actual output behavior separately; a successful import is not an
   electrical verification.
6. On a new installation, enter the measured source/supply voltages in Advanced,
   configure Wi-Fi and the receiver URL, then run Calibrate with both outputs
   open. Keep device configuration and baseline files private; they are not
   included in the repository.

Do not use old calibration-era bench probes with this firmware. They are
preserved in Git's initial commit for historical investigation.

## Development checks

```text
python -m unittest discover -s tests
node --test tests/test_apps_script.js
python prototype/build_mpy.py --check
```

Host tests cover direct circuit equations, trigger/settle progression, retained
ADC stops, cancellation, UI/reset behavior, settings persistence,
streaming payload/ACK checks and old/new receiver compatibility. They do not
establish physical gain, source-rail stability or current compliance.

The initial 2026-11-04 mixed-sensor update passed **363 Python tests and 82 receiver
tests**. That build contained 15 compiled modules totaling 90,863 bytes.
New checks exercise native 20-bit gate readings through current correction and
RAM upload, both saturation signs, fractional-code rounding, and legacy
baseline rejection. Receiver version 14 was deployed with the same endpoint
and permissions. The gate shunt is now confirmed at 98.8 Ω; ten I²C identity
reads per channel verified both sensors at their intended addresses. The
mixed-sensor firmware is installed on the Pico. With both PWMs held low,
the native 64-average check completed conversion-ready polling (drain
136,734 µs; gate 140,126 µs), stable trigger-register checks and diagnostic
readback. Cancellation during 1,024-average conversion and return to continuous
mode also passed. The former baseline loaded with `compatible=False`.
Only I²C was connected for this digital check, so voltage readings from the
unconnected analog inputs have no circuit-validation meaning. No voltage sweep
or fresh physical calibration was performed in that digital check.

The native synthetic calibration/memory check passed with 984 simulated reads,
46 drain knots and 53 gate knots. Free heap was 65,040 bytes at the start,
64,048 after calibration and 63,520 at completion. It exercised 20,000 lookup
pairs and streamed 4,096 records twice (155,238 bytes each) with identical
SHA-256 `9694ba2e54bbfa9d1c087165c7213f80886bac8c47c915b94e539f58824c6524`.
The test used simulated inputs and an in-memory save; it performed no physical
calibration, baseline flash write or Wi-Fi operation. All 16 deployed public
files were verified, 14 preserved files were unchanged, and the three temporary
test helpers were removed. Normal boot reached Ready in 2,812 ms without a
fault. These are memory and installation checks, not analog validation.

After the analog wiring was connected on 2026-11-04, with VBUS on IN+, the
DUT disconnected and drain PWM held at zero, six gate readings used sixteen
fresh 64-average blocks each. No baseline subtraction was applied and no
calibration file was written:

| Measured gate voltage to GND | Mean apparent Ig |
|---|---|
| 0.0177 V | -0.0279 µA |
| 3.1124 V | -0.0047 µA |
| 4.8012 V | -0.0057 µA |
| 9.7921 V | -0.0055 µA |
| 14.7811 V | -0.0047 µA |
| 0.0177 V, return check | -0.0257 µA |

This checks unloaded offset, not current-gain accuracy. Both outputs were
returned to zero and the normal application reached Ready in 2.281 seconds.
Raw captures are local generated bench artifacts, not supplied with a clone.
The owner subsequently reported a clean, linear scan with a 1.7 kΩ resistor
between drain and source, sweeping up to 10 V with the case closed. No numeric
endpoint data or independent source/boost rail measurements were supplied.
Expected current at an actual VDS of 10 V is 5.88 mA (58.8 mW in the resistor).
This qualitative loaded check does not yet verify the existing 0.789 Ω drain
current scale. Fresh mixed-sensor baseline residuals and numerical accuracy
checks have not been independently documented.

The subsequent drain-only correction update passed **366 Python tests and 87
receiver tests**. That build contained 15 compiled modules totaling
90,909 bytes. With the production scan/display/TLS buffers reserved, a native
synthetic check rejected any gate-baseline lookup, verified both gate-current
signs, drain subtraction and the retained gate zero anchor, then streamed the
1,297-byte schema-5 payload twice with identical hashes. Free heap was 88,352
bytes before setup and 83,024 bytes after completion, with no
physical drive, calibration save or network upload. All 16 public files were
verified and 14 retained data/configuration files stayed unchanged. The test
helper was removed and normal boot reached Ready in 2,204 ms with outputs low.
Receiver version 15 is deployed at the same endpoint with unchanged permissions
and triggers. Source-hash verification and HTTPS health checks passed; its chart
labels distinguish corrected Id from raw Ig while retaining historical semantics.

The 2026-11-05 manual Debug update, with immediate keypad apply, clear ON/OFF
indication, ground-relative targets and raw VBUS display, passed **403 Python
tests**, including 37 controller/UI tests covering applied PWM readback,
edit/cancel shutdown, sensor faults, fresh averaging
and clock rollover. The installed build contains 16 compiled modules totaling
97,818 bytes. `prototype/manual_debug_smoke.py` exercised the actual LCD and
production buffers with synthetic sensors: 4,162 readings, no scan records or
file writes, and 160,928 bytes free both before and after the sustained run.
It also checked keypad/apply/cancel, fault recovery and the Settings restart.
Four subsequent physical measurements for this installation used PWM zero
throughout. All 17 public files were
hash-verified, 14 retained data/configuration files stayed unchanged, and normal
boot reached Ready in 2,266 ms. The temporary test helper was removed.

An earlier open-output diagnostic exercised the actual Debug-page apply path
at 5 V targets: both PWM pins showed digital activity at the expected roughly
7.6% drain / 20.1% gate duties, with 100 kHz configured. A separate direct 50%
hold was measured by the owner at about 1.65 V DC on both GPIOs. Earlier
diagnostics also logged drain VBUS near 18.8 V with PWM off. After the
immediate-apply and clear OFF-indicator update, the owner confirmed everything
was working normally. No new numeric drain-output meter comparison was
recorded, so this confirms operation without establishing analog accuracy or
the cause of that earlier reading.

The following 2026-11-03 results used the previous two-INA226 circuit; they
do not validate the new INA228 gate path or its 98.8 Ω shunt. The current-baseline release passed **342 Python tests and 79 receiver
tests**. The build including the Ig visual warning contains 15 compiled modules
totaling 88,883 bytes; the generated manifest is authoritative for later builds.
Receiver version 13 supports schemas 1–5, automatic Index cleanup when a scan
tab is deleted, and the Refresh Index menu.

On the Pico, `prototype/baseline_memory_smoke.py` completed a synthetic
calibration using the real UI/display and production buffers, including the
96 KiB TLS reserve. It then exercised 20,000 lookup pairs and streamed 4,096
corrected records twice with matching hashes. Binary saving was replaced by an
in-memory check; this test made no physical measurements, network connections
or file writes. It verifies the native memory path rather than analog accuracy.

The open-output physical check saved and reloaded a 1,720-byte baseline with
43 drain knots and 48 gate knots. Seven voltage pairs, each averaging 16
independent hardware-average blocks, gave maximum absolute residuals of
**0.198 µA drain and 0.094 µA gate**. This is an unloaded offset check, not a
calibrated current-source/gain test or a per-point accuracy specification. The
drain still has about 3.17 µA per raw ADC count.

The gate source-relative zero was captured at PWM code 8546/65535, with a
3.392 mV residual relative to the configured 3.3 V source. The drain's lowest
captured terminal was 3.423753 V, so its closest zero is recorded as unreachable
and is not used as a trim. A simultaneous paired check read about 3.415 V there.
The initial coarse table missed the gate's sharp bias transition around 3.3 V;
retaining the zero-search readings reduced that location's averaged residual
from about 1.9 µA to 0.005 µA.

A real three-point scan uploaded through the production Wi-Fi/HTTPS path with
the exact scan ID, payload SHA-256 and count acknowledged. The resulting sheet
used schema 5, recorded the matching baseline fingerprint, and contained both
charts. Deployed firmware hashes matched the build, and retained settings,
connection profiles and historical data were unchanged. Earlier direct-firmware
checks also covered scan cancellation and upload-progress transitions.

After installing the Ig warning, one normal soft boot reached the first complete
UI/input tick in **2.250 seconds**, with no fault markers. This is USB-observed
readiness; Wi-Fi connection and an independent visual screen check were not
part of that timing. Private captures under ignored `output/` are not included
in a clone. These prototype checks do not establish full-range analog accuracy,
temperature stability or safe hardware power sequencing.
