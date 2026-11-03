# Pocket MOSFET scanner

A MicroPython MOSFET curve tracer using a **Raspberry Pi Pico 2 W**, two
**INA226** sensors, and **LM358P** voltage drivers. It sweeps drain voltage
for each gate voltage, plots measured curves locally, and uploads each scan
to a new Google Sheets tab. This is a breadboard prototype.

## Direct measurement

The firmware calculates PWM from the resistor values, optionally anchored to
measured source-relative zero settings. An open-output calibration in Advanced
measures a small voltage-dependent current baseline for each INA226. There are
no iterative voltage corrections or target-error stops. Each point sets drain
drive, updates gate drive when starting a new curve, waits for settling,
triggers both INA226s, then records actual **VDS, VGS, drain current and gate
current**.

Terminal voltage is `VBUS - shunt voltage`; subtract the configured source
voltage for VDS/VGS. With a compatible baseline, current is signed shunt voltage
minus the interpolated open-output baseline, divided by resistance. Missing or
incompatible calibration leaves readings explicitly uncorrected. Source voltage
is a manual setting, not an independently measured value.

Unreachable voltage commands produce the measured point. All points in each
requested gate sweep connect on the display and Google chart, even when
measured gate voltage differs from its command. Actual VGS remains in the data.

## Workflow

- Physical left is **Settings/Cancel**; physical right is **Scan/OK**.
  Leaving Settings soft-restarts to Home.
- Scan acquires, plots, uploads automatically, then soft-restarts. Cancel stops
  outputs, discards RAM data, shows Restarting, and restarts.
- Upload shows a checklist for preparing data, connecting, transferring and
  confirming the save. Transfer percentage follows bytes sent; only the exact
  server acknowledgement marks the save complete.
- Up to **4,096 points** fit in **64 KiB** of preallocated RAM (four float32
  values per point). Scan cycles write no data to flash. Settings, up to 20
  Wi-Fi profiles and the upload URL persist separately.
- Advanced calibration requires both outputs open, with no DUT, test loads,
  probes or shunt jumpers. Its fixed binary tables and workspace are reserved
  at boot; only a completed calibration replaces the saved baseline. Cancel
  preserves the previous file and restarts.
- Settling defaults to **100 ms** and overlaps incremental screen updates.
  INA averaging defaults to **64**, with **1.1 ms bus + 1.1 ms shunt** per
  conversion pair; both sensors run concurrently after sequential triggers.
- The live **Ig** label and value turn red when **|Ig| > 1 µA**. This visual
  warning uses the reported current and does not stop the scan.
- Current-driven stops occur only when either INA226 shunt readback reaches
  **0x7fff (positive full scale) or 0x8000 (negative full scale)**. With the
  fitted scales, this is about **±104 mA drain / ±128 µA gate**. No separate
  current threshold or shunt comparator is programmed.
- Shunt-ADC saturation preserves completed points for partial upload. A stop before
  the first point shows the fault and does not upload an empty scan.
  Communication/configuration failures, malformed ADC data and
  conversion timeouts also stop acquisition.
- Wi-Fi/upload failure asks the user to reconnect in Settings and redo the
  scan. There is no offline queue. Cancel during upload may leave an already
  accepted Google tab.

## Hardware

| Function | Current prototype |
|---|---|
| Controller/UI | Pico 2 W, GeeekPi / 52Pi EP-0172 Plus kit |
| Drain/gate INA226 | 0x44 / 0x41; 3.3 V logic; VBUS tied to IN+ before shunt |
| I²C | GP18 SDA, GP19 SCL, 100 kHz; breakout pull-ups |
| Drain/gate PWM | GP20 / GP21, 100 kHz; 1 kΩ / 10 µF filter per channel |
| Feedback | 68 kΩ / 10 kΩ, nominal gain 7.8 |
| Source | Pico 3V3; 6.8 kΩ from that rail to drain IN1+ adds drain offset |
| LM358 supply | Two series 9 V batteries; negative at GND; positive via relay NO |
| Relay | VCC and ACTIVATE tied to 3V3; no software isolation control |
| Shunt scales | Drain 0.789 Ω (previously measured effective value); gate 638 Ω (provisional meter value) |
| Buttons | Left GP15/pin 20; right GP14/pin 19 |

Defaults describe this circuit: source **3.3 V**, driver supply **18.8 V**, and
drain sweep starting at **0.1 V**. Saved settings remain authoritative.
The supply setting describes the hardware; it does not measure or regulate it.
Output swing depends on the LM358 supply and load.

The nominal circuit equations are `VD = 3.3 + 22.44 × dutyD` and
`VG = 25.74 × dutyG`, in volts relative to GND. Without a valid zero capture,
duty is computed by inverting these equations. With one, duty starts at the
captured source-relative zero and adds requested VDS/22.44 or VGS/25.74.
Both paths clamp duty to 0…1. Changing resistors requires changing the constants
in [model.py](firmware/model.py) and repeating calibration.

See the [hardware guide](prototype/README.md) for schematics and pin tables.
**Fixture D/G/S numbers are not transistor lead numbers:** the onsemi 2N7000G
is **Source–Gate–Drain** with its flat face toward you and leads down.

## Repository and development

| Path | Purpose |
|---|---|
| [firmware/](firmware/README.md) | Acquisition, UI, settings, Wi-Fi and HTTPS |
| [cloud/apps-script/](cloud/apps-script/README.md) | Google Sheets receiver; raw and baseline-corrected readings |
| [prototype/](prototype/README.md) | Circuit drawings, generators and host utilities |
| [tests/](tests/) | Hardware-free Python and Node regression tests |
| [enclosure/](enclosure/README.md) | Housing generator; physical fit untested |

Run from the repository root:

```sh
python -m unittest discover -s tests
node --test tests/test_apps_script.js
python -m pip install --target .tools/mpy-cross --no-deps --only-binary=:all: --require-hashes -r .tools/mpy-cross-requirements.txt
python prototype/build_mpy.py
python prototype/build_mpy.py --check
```

The compiler targets **MicroPython 1.29.0 / RPI_PICO2_W**. See the
[firmware guide](firmware/README.md) before deployment. Build products, private
configuration and bench captures under `output/`, `.tools/` and `.local/`
are not committed. The initial commit preserves the former calibration
firmware and historical diagnostics.

Create a private Google Sheet and follow the [receiver setup](cloud/apps-script/README.md).
Its `/exec` URL is the submission credential; keep it private. The Pico verifies
HTTPS certificates and the exact upload acknowledgement. Spreadsheet sharing
controls who can read results.

For a new device, follow the [firmware installation steps](firmware/README.md#install-or-update),
enter its measured source and supply voltages in Advanced, configure Wi-Fi and
the receiver URL, then calibrate with both outputs open. Device credentials,
baseline files and bench captures are not supplied in this public repository.

## Prototype limitations

The source rail is not a precision reference. Baseline correction removes the
measured open-output offset; temperature changes, altered wiring and component
changes can invalidate it. It does not calibrate shunt resistance or amplifier
gain. A source-relative zero below the hardware output floor remains unreachable.
The relay follows 3V3 through soft resets and cannot provide fault isolation.
The 3V3 load must absorb returned source current; the ADC range does not establish
the source rail's or LM358's current capability. C5 is fitted; C6 installation is
unconfirmed.

An intermittent reverse-current report appeared during earlier loaded testing
and later disappeared; its physical cause remains unresolved. Bench results
from the previous calibrated firmware do not establish direct-drive accuracy.
The 2026-11-03 baseline release passed **342 Python tests and 79 receiver tests**,
a native 4,096-point RAM/streaming check, an unloaded baseline check, and a real
three-point corrected Google upload with an exact acknowledgement. Normal
startup was about **2.25 seconds**. See the [validation details](firmware/README.md#development-checks)
for the measured residuals and their limits. Full-range analog qualification
remains outstanding.
