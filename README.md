# Pocket MOSFET scanner

A MicroPython MOSFET curve tracer built around a **Raspberry Pi Pico 2 W**,
two **INA226** voltage/current sensors, and **LM358P** PWM-driven voltage
outputs. It sweeps drain-source voltage for each gate-source voltage, displays
the measured curves locally, and uploads each scan to its own Google Sheets tab.

This is an actively developed breadboard prototype. The firmware and bench
tests are working; the analog circuit and enclosure are not production-qualified.

## What it does

- Records actual **VDS, VGS, drain current, and gate leakage current** at each
  point. Requested voltages describe the sweep; the recorded voltages describe
  what the driver actually reached.
- Uses configurable INA226 hardware averaging, initially **64 conversions**,
  with **1.1 ms each for bus and shunt**. The two sensors operate concurrently
  after sequential triggers. The minimum settling interval is **100 ms**;
  incremental screen updates overlap settling.
- Keeps up to **4,096 points in 68 KiB of RAM**: four float32 measurements plus
  a flags byte per point. A normal scan cycle writes no scan data to flash.
- Applies voltage-dependent current-baseline correction using packed binary
  calibration tables, binary-search interpolation, and a cached gate drive
  within each drain sweep.
- Provides touchscreen and physical-button control: **left Settings/Cancel**,
  **right Scan/OK**. Scan ranges, advanced settings, calibration, and up to
  20 Wi-Fi profiles persist on the Pico.
- Uploads through a Google Apps Script receiver. Each scan tab gets drain-current
  curves and a gate-leakage chart. Points whose measured VGS misses its command
  by more than **100 mV** are not connected to the requested gate curve.

## Using the scanner

Power-up opens Home with Wi-Fi inactive. Settings provides scan ranges,
averaging, the manually entered supply/source voltages, Wi-Fi, cloud setup,
and explicit **Calibrate — DUT disconnected**. Leaving Settings for Home
soft-restarts the device.

Home **SCAN** acquires, plots, uploads automatically, and then soft-restarts.
**CANCEL** discards the current RAM scan and restarts. A current-limit or ADC
overrange stop preserves completed points for partial upload; a stop before
the first point shows the fault and does **not** upload an empty scan.
If Wi-Fi or upload fails, the device asks the user to reconnect in Settings
and redo the scan. There is no offline scan queue in this workflow. An upload
already accepted by Google may remain if Cancel is pressed during transfer.

See the [firmware guide](firmware/README.md) for the complete workflow,
calibration procedure, error behavior, and deployment instructions.

## Current prototype hardware

| Function | Connection / configuration |
|---|---|
| Controller and UI | Pico 2 W with GeeekPi / 52Pi EP-0172 Plus kit |
| Drain / gate INA226 | I²C addresses **0x44 / 0x41**, powered from Pico 3V3 |
| I²C | GP18 SDA, GP19 SCL, 100 kHz; breakout pull-ups to 3V3 |
| Drain / gate PWM | GP20 / GP21, 100 kHz, one 1 kΩ / 10 µF filter per channel |
| Drivers | LM358P, nominal 68 kΩ / 10 kΩ feedback per channel |
| Source reference | Pico 3V3; 6.8 kΩ from that rail to drain IN1+ adds the drain offset |
| Analog supply | Two 9 V batteries in series, negative at GND, positive through relay NO contacts to LM358 V+ |
| Relay module | VCC and ACTIVATE tied to 3V3; common GND; not a software shutdown interlock |
| Drain current scale | Nominal 1 Ω shunt; calibrated effective value **0.789 Ω** |
| Gate current scale | Nominal 680 Ω shunt; provisional measured value **638 Ω** |
| Current limits | **±5 mA drain**, **±100 µA gate**; positive transient monitoring also enabled |
| Physical buttons | Left GP15 / physical pin 20; right GP14 / physical pin 19 |

Each INA226 **VBUS connects to IN+**, on the amplifier side of its shunt.
The firmware reconstructs the terminal voltage as bus voltage minus the raw
shunt drop, before applying current-baseline correction. INA226 VBUS is a
voltage-sense input; it is not the Pico's USB VBUS supply connection.

The firmware's factory defaults remain **5 V driver supply / 0 V source**.
They do not describe the current battery-powered, source-biased prototype.
Enter the actual wiring's voltages and perform open-output calibration before
using a new assembly. These settings describe supplies; they do not measure
or regulate them. Reachable voltages depend on the rail, LM358 headroom, load,
and saved calibration. Start the present prototype's VDS sweep at **0.1 V**;
zero-VDS cross-gate baseline coverage has not been established.

The [hardware guide](prototype/README.md) links the full schematic and pin
tables. **Fixture connector numbers are not transistor lead numbers.** In
particular, the onsemi 2N7000G is **Source–Gate–Drain**, left to right with its
flat marked face toward you and leads down; see the linked manufacturer drawing
in the hardware guide.

## Repository map

| Path | Contents |
|---|---|
| [firmware/](firmware/README.md) | Pico application, acquisition, calibration, UI, Wi-Fi, HTTPS, and legacy archive readers |
| [cloud/apps-script/](cloud/apps-script/README.md) | Google Sheets receiver and deployment instructions |
| [prototype/](prototype/README.md) | Schematics, their generators, host utilities, and bench diagnostics |
| [tests/](tests/) | Host Python tests and Node.js Apps Script tests |
| [enclosure/](enclosure/README.md) | Parametric housing generator; physical fit remains untested |

`output/` contains local generated artifacts and measurements and is ignored.
Historical documentation may refer to files there which are not included in
a clone. Downloaded dependencies under `.tools/` are also ignored, except for
the pinned MicroPython compiler requirements file. Credentials, device settings,
calibration files, and private deployment state are deliberately untracked.

## Host checks and firmware build

Run commands from the repository root. The host suite is exercised with
**Python 3.12** and Node.js's built-in test runner; it requires no Pico,
Google account, or third-party Python packages:

```sh
python -m unittest discover -s tests
node --test tests/test_apps_script.js
```

The deployed target is **MicroPython 1.29.0 for RPI_PICO2_W**, with the matching
portable `.mpy` format. The current build helper expects the pinned **Windows
x64** compiler at `.tools/mpy-cross/mpy_cross/mpy-cross.exe`:

```sh
python -m pip install --target .tools/mpy-cross --no-deps --only-binary=:all: --require-hashes -r .tools/mpy-cross-requirements.txt
python prototype/build_mpy.py
python prototype/build_mpy.py --check
```

This creates 20 compiled modules and a hash manifest under
`output/firmware-mpy/`; it does not access a device. Deployment additionally
requires source `firmware/main.py` and the public `firmware/google_roots.pem`
trust bundle. Matching `.py` modules on a Pico shadow `.mpy` files, so follow
the [deployment procedure](firmware/README.md) and preserve device data.
Do not run arbitrary bench scripts as installation steps: some drive outputs,
replace calibration, or depend on a particular fixture.

## Cloud setup

Create a private Google Sheet and deploy [Code.gs](cloud/apps-script/Code.gs)
as described in the [receiver guide](cloud/apps-script/README.md). Configure
Wi-Fi and the resulting `/exec` URL from the Pico's Advanced settings.

The deployment URL is the prototype's submission credential: anyone possessing
it can submit scans. Keep it private. The receiver does not expose scan reading
or deletion, and the spreadsheet's own sharing controls determine who can view
results. The Pico validates HTTPS certificates and the upload acknowledgement.

## Development status

The latest bench checks include the binary calibration lookup equivalence,
100-point timing comparisons, empty-scan fault handling, and a three-point
scan with the connected 2N7000G. On the recorded 100-point comparison, ordinary
points averaged about **327 ms** with 64 averages and 100 ms settling. Timing
depends on chart redraws, gate transitions, and measured values.

An intermittent reverse-current report during zero-duty parking was observed
and subsequently absent in fresh reads and the short scan; its physical cause
has **not** been established. The corrected fault UI now exposes such a stop
instead of uploading zero points. Software limits and a relay that follows
3V3 do not provide hardware DUT isolation through every reset or power event.
The source rail is not a calibrated voltage reference, and the gate-current
scale remains provisional. These are prototype constraints, not settled
production specifications.
