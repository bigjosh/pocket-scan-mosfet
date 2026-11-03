# Pocket MOSFET scanner firmware

The application runs one RAM-only scan per boot on a Pico 2 W with a GeeekPi /
52Pi EP-0172 Plus kit. PWM uses nominal resistor math, optionally anchored to
measured source-relative zero. A compact open-output baseline corrects current
readings without a voltage feedback loop or target-quality stops.

## Controls and flow

Home starts with Wi-Fi off. Physical **left GP15 / pin 20** is Settings/Cancel;
physical **right GP14 / pin 19** is Scan/OK. Touch buttons have the same roles.
Settings edits integer-microvolt VDS/VGS start, stop and positive step values.
Advanced contains source voltage, driver supply, INA averaging, Wi-Fi, cloud and
an explicit open-output calibration action.

Settings OK saves; Cancel discards the draft. Confirming calibration also saves
the draft before the sweep, so cancelling that sweep does not undo those saved
settings. Exiting Settings soft-restarts.
Settings and connection files use atomic JSON writes; scans never write files.

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
The supply setting does not correct gain, establish output headroom, or measure
the rail. Actual outputs may be far from requested voltages under load.

INA226 VBUS connects to IN+ **before the shunt**. For each channel:

```text
terminal_V = bus_V - shunt_V
relative_V = terminal_V - configured_source_V
current_A  = (shunt_V - baseline_V(bus_V)) / shunt_ohms
```

Drain uses 0.789 Ω (previously measured effective scale); gate uses provisional
638 Ω. A missing or incompatible baseline uses zero subtraction and is labeled
uncorrected. Terminal voltages always subtract the raw shunt voltage, preserving
the real voltage drop even when current is corrected.

## Current baseline and source-relative zero

Use **Settings → Advanced → Calibrate** with both outputs open: disconnect the
DUT, test resistors and meter probes, and remove both shunt jumpers. Confirmation
saves the edited settings before starting. Calibration measures actual INA226
VBUS and signed shunt codes at 53 bulk PWM settings per channel, spaced
quadratically to give more coverage at low voltages. It also retains up to 12
already-measured zero-search points as knots, within the fixed capacity of 65
knots per channel; retaining them adds no measurements. Commanded voltage is
never used as the lookup coordinate. Each knot combines four hardware-average
blocks using the saved settling/averaging settings. Raw ADC saturation and
communication faults still stop both outputs.

The small, fixed binary table holds bus microvolts, shunt nanovolts and PWM
codes. Its format version and SHA-256 detect unsupported or corrupted files;
incompatible settings/hardware disable correction.
Linear interpolation estimates the baseline at the measured bus voltage. Outside
the measured interval the nearest endpoint is used, without a new scan stop;
accuracy beyond the calibrated interval is not established. The baseline includes
input bias, ADC offset and any fixture leakage, and may drift with temperature.
It does not determine the shunt's resistance. Recalibrate after changing hardware.

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
current, including baseline correction when active. It is a visual warning and
does not change acquisition or stopping behavior.

## Acquisition and stopping

For each voltage pair, the engine sets PWM, waits the configured settling
interval, triggers both INA226s, polls each ready flag, and reads their bus/shunt
registers. Sensors measure concurrently after sequential I²C commands.
Default averaging is 64; choices are 1, 4, 16, 64, 128, 256, 512 and 1024.
Bus and shunt conversion times are each 1.1 ms. A 64-average block nominally
takes 140.8 ms, in addition to settling and UI/I²C work.

Minimum settling defaults to 100 ms. Incremental rendering of the previous
point overlaps the next point's settling window. Drawing longer than that
minimum naturally extends it. Normal scans have no extra gate/parking calibration
measurements. A source/bias change requires updating configuration/constants and
repeating the baseline/zero calibration before using its correction again.

Current-driven stops use the actual INA226 shunt register readback only:
**0x7fff** is positive full scale and **0x8000** is negative full scale, for
both channels. At 2.5 µV per count these represent +81.9175 mV and −81.920 mV.
The fitted 0.789 Ω drain scale gives about **±104 mA**; the 638 Ω gate scale
gives about **±128 µA**. There are no separate current-trip thresholds and no
programmed shunt-limit comparators. A full-scale result stops the outputs and
is excluded from the numeric measurements. Cancel and errors disable gate
drive before drain drive.

Bus-register **0x7fff** does not stop the scan, and measured voltage has no
range veto. A reserved high bus-register bit still indicates malformed data
and aborts the read.

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
values with compact current-correction provenance, and Sheets labels the currents
accordingly. SHA-256 binds the exact streamed payload. The device
requires a matching scan ID, digest, count and Sheet ID before declaring success.
HTTPS certificate verification remains enabled using `google_roots.pem`.
Never replace that with an insecure TLS mode.

Settings, Wi-Fi profiles, cloud URL and the new binary baseline persist. Old
gain-calibration files and scan archives are not read by this firmware.
Historical cloud schemas 1–4 remain supported without rewriting their data.
Install the schema-5 receiver before sending corrected scans.

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

The 2026-11-03 current-baseline release passed **342 Python tests and 79 receiver
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
