# Pocket MOSFET scanner firmware

MicroPython firmware for a Pico 2 W and GeeekPi EP-0172 Plus breadboard kit.
The current application runs **one RAM-only scan per boot**, uploads it, then
soft-restarts. Settings are a separate persistent path. See
[Install or update](#install-or-update) for the Windows build/deployment
procedure and [Development checks](#development-checks) for host verification.
The dated checks below distinguish current behavior from earlier firmware
and wiring; a result applies only to its stated configuration.

Paths under `output/`, including bench logs, screenshots, generated PDFs and
build manifests, are **local generated artifacts excluded from Git**. They
are not required to build or test a fresh clone. Historical evidence paths
identify the original development records, not files supplied with this repository.

## Controls and saved settings

- Home shows **SETTINGS** on the left and **SCAN** on the right. Startup
  notices appear inline. The radio stays off; Home neither connects Wi-Fi
  nor uploads an old queue.
- The physical buttons keep the same roles: **left Settings/Cancel**, **right
  Scan/OK**. The right button is inactive during acquisition and upload. The
  assembled prototype uses **left BTN1/GP15 (pin 20)** and **right BTN2/GP14
  (pin 19)**, confirmed by the user's physical button check.
- Settings edits the start, stop and positive step for **VDS** and **VGS**.
  Tap a value for the numeric keypad. Values are relative to the source.
  Settings OK saves; Cancel discards the draft. Either exit to Home
  **soft-restarts the device**. Subpages retain their existing OK/Cancel
  behavior until the Settings path exits.
  Entering Settings releases the 96 KiB upload-memory reserve for editing,
  calibration and atomic file saves; the next boot reserves it again before
  a scan. Settings OK avoids creating a fresh draft after committing its save.
- Advanced includes **Driver supply voltage (V+)** and **Source voltage (VS)**,
  **Averaging**, **Wi-Fi**, **Cloud**, and explicit open-output calibration.
  The new UI has no Storage page or saved-scan queue controls.
- **Settings → Advanced → Averaging** selects the INA226 hardware average
  count: **1, 4, 16, 64, 128, 256, 512 or 1024**. The page shows conversion
  time and whether recalibration is required. OK returns the choice to the Advanced draft;
  Cancel discards changes made on that page. Return to Settings and press OK
  to save. Changing the count requires a matching open-output calibration.
- Scan sweeps VDS for each VGS value, plots measured drain current versus
  measured VDS, and shows measured VDS, VGS, Id and Ig. Completed scans proceed
  automatically to upload; there is no keep/discard confirmation screen.
  A current-limit or ADC-overrange stop uploads a partial scan only if it
  contains completed measurements. A stop before the first point displays
  **SCAN STOPPED**, the channel and fault, and the signed current/limit when
  available. It does not upload an empty scan; OK or CANCEL discards and restarts.
- **CANCEL** during preparation, scanning, payload preparation, connection or
  upload stops the current operation, discards its local RAM data and restarts.
  After stopping outputs, it immediately clears the chart and paints
  **RESTARTING / Returning to Home**, before closing upload/Wi-Fi connections.
  Display failures do not prevent discard/restart; an output-shutdown failure
  takes the final hardware-cleanup path without waiting for a screen redraw.
  Scan/upload error screens present **CANCEL** and **OK**; either discards
  the RAM data and restarts.

Home **SCAN** first paints **Preparing scan**, validates the configuration,
prepares the RAM scan and disables Wi-Fi before arming acquisition. The scan
screen is painted before the first drive command. Repeated SCAN/OK presses
during preparation are ignored, and CANCEL is available between stages.
Display/radio calls remain synchronous; DNS/TLS calls can still delay input
polling while that call is executing.

Scan rendering updates only changed numbers, requested values, status colors
and progress regions. Static labels persist. Plot limits expand with extra
headroom instead of following every new extreme; axes and historical points
are repainted only when those bounds change. LCD line segments batch adjacent
pixels into horizontal or vertical runs without changing plotted geometry.

A completed point is retained and reported first. On the following loop tick,
the engine commands the next voltage before painting the previous point, so
screen work overlaps settling. The **100 ms minimum** starts after the drive
command and setup complete. Drawing that takes less than 100 ms leaves only
the remaining interval to wait; longer drawing naturally extends the window.
Ready polling during averaged conversion takes priority over further drawing.

## Per-point timing diagnostics

Normal scans emit one fixed `DOT_US` USB line per accepted measurement. All
time fields are integer microseconds. Two preallocated counter arrays and a
fixed output buffer are reused; timing adds no scan-file writes or fields to
the uploaded measurement data. `prototype/capture_dot_timings.py` passively
captures these lines without sending commands, resetting or starting a scan.

- `index` and `curve` identify the zero-based point and gate sweep. `wall`
  runs from the previous report boundary through this point's UI tick. The
  first window starts after preparation and the initial scan-screen paint.
  During acquisition, drawing is deferred into the following point's settling
  window: its cost therefore appears in the following report. The first report
  can have no actual LCD transfers, but `render` still includes the overhead
  of calls that find no drawing work. Final scan sealing includes its status paint.
- `drive`, `park_settle`, `gate_settle`, `drain_settle`, `convert`, `process`
  and `other` partition that elapsed wall time. `convert` includes triggering,
  readiness-poll latency and reading the resulting registers; it is not a
  direct measurement of the ADC's internal conversion time.
- `engine`, `i2c`, `input`, `record`, `render`, `sleep` and `emit` attribute
  execution time. **I2C is included in engine**; these work counters overlap
  the wall-time phases and must not be added to them. `record` includes RAM
  packing and, on the final point, scan sealing. `render` includes autoscaling,
  live readings and plotting. Remaining wall time covers dispatch, timing
  bookkeeping and other work outside those measured regions.
- `adc_blocks` counts successful paired triggers; `polls` counts readiness
  checks. Normally the first point of a gate sweep uses three blocks and an
  interior point uses one. Strict drain-parking corrections can add blocks.
- `backlog` counts accepted points still awaiting plotting after this tick.
  A report is an acquisition/UI-cycle measurement, not proof that the newest
  dot is already visible after an autoscale redraw.
- USB reporting is synchronous. Its formatting/write time appears as `emit`
  in the **next** point; the most recent cost is also retained in
  `_scan_timing.last_emit_us`. A slow USB reader can therefore be identified
  as overhead. Short writes or exceptions increment `dropped`/`failed` and do
  not abort acquisition. Cancel/fault stops timing without inventing a point.

Timing samples are not retained across soft restart. Capture the USB lines
while scanning to keep them on the computer; the latest fixed counter snapshot
is also available in RAM until restart. Settling, averaging, calibration and
current-limit behavior are unchanged by instrumentation.

The 2026-11-03 binary-lookup/gate-cache update was checked against the preceding
incremental-display firmware with the same first 100 saved-grid points,
100 ms settling and 64 INA226 averages. Ordinary points averaged **327.0 ms**
versus **341.8 ms** (4.3% less time); all 100, including four gate transitions,
averaged 349.3 versus 363.4 ms. Engine work excluding I²C fell from 71.5 to
50.4 ms across all points. Drawing varied between runs (132.4 versus 122.8 ms),
so these are observed end-to-end timings, not isolated algorithm timings.
Both captures completed without dropped or failed timing reports. Native
checks matched the former interpolation's exact float32 results and coverage
errors across 2,238 cases; standalone lookup batches were 11–22 times faster.
Evidence: `output/smoke-test/calibration-search-speed-comparison-20261103.json`
and `calibration-search-native-20261103.log`. Settings, calibration, credentials
and historic scan files were preserved; the benchmark was discarded locally
without uploading. `prototype/compare_scan_timings.py` compares complete
100-point captures and rejects mismatched point/curve/conversion counts.

Boot reserves a **68 KiB** packed scan buffer, shared by plotting and upload,
instead of the former 44 KiB plot arrays. Each of its 4096 records contains
four float32 values—actual VDS, VGS, Id and Ig—and one flags byte, **17 bytes
per point**. Currents remain in amperes with float32 precision; they are not
rounded to integer microamps. Requested voltages, point/curve indices and
averaging counts are derived from the integer grid/settings snapshot. The
new compact records do not retain raw shunt means or standard deviations.
A scan point is one hardware-averaged block; its variance is unavailable.
No per-point Python dictionaries remain in the scan buffer.

Once complete or validly stopped, the buffer is frozen while its bounded JSON
payload is prepared and sent. Settings/calibration metadata and the calibration
fingerprint remain in the upload snapshot; the live calibration map is released
before Wi-Fi connection. Scan rows use **wire schema version 3**, not a new
binary archive version. Existing archive files remain untouched and their
legacy readers/export tools remain available.

Normal Boot, Scan and Upload do not write configuration, scan data, sidecars,
receipts or flash error logs. Errors are reported over USB. Persistent settings,
calibration, Wi-Fi profiles and the receiver URL are changed only through
Settings actions.

The two advanced voltages are **manual descriptions of the wiring**, measured
relative to Pico GND. They do not control a supply or measure VSYS. Defaults
are `driver_supply_uv = 5000000` (5 V) and `source_voltage_uv = 0` (0 V). The requested
absolute terminal envelope is `0 .. min(V+, 36 V)`; at 5 V it is
0–5 V. A requested terminal voltage is `VS + VDS` or `VS + VGS`. This is a
command envelope, not a promise that the LM358 can reach the requested value.
Actual terminal voltages are measured at every point.

The revised prototype uses a nominal **3.3 V source reference** and a
**two-9-V battery supply, nominally 18 V above GND**, switched by a relay.
For that wiring, Source voltage (VS) is the shared offset used for both
VDS and VGS; it is not a separate gate-only offset. Enter the measured rail
values in Settings before recalibration. `source_voltage_uv = 3300000`
describes exactly 3.300000 V; the regulated rail is not a calibrated precision
reference. The source and driver voltages are still manual settings, and
updating this documentation does not change the values saved on the Pico.

At the 2026-11-03 development checkpoint, the configured values remain
**VS = 3.300 V and V+ = 18.800 V**; the user's meter read approximately
**3.28 V and 18.47 V** respectively. Firmware still subtracts the configured
3.300 V from both measured terminals, so these manual-rail differences affect
the interpreted VDS/VGS. No automatic rail correction or settings change was
made. Changing the declared source/supply requires the corresponding settings
and calibration compatibility checks; measure the rails with the fixture
disconnected before an explicit recalibration.

`settings.json` persists scan ranges and advanced settings on the Pico.
`calibration.bin` stores the two channels' calibration. Files are written
through a temporary file before replacement. Invalid committed settings are
reported rather than silently overwritten. Default sweeps are 0–2 V in
0.5 V steps for both axes. A stop is included only if an exact step reaches
it; the firmware does not add a shorter last step. Maximum scan size is
4096 points.

Settings version 3 retains all eight commanded voltage fields as **integer
microvolts**: `drain_start_uv`, `drain_stop_uv`, `drain_step_uv`,
`gate_start_uv`, `gate_stop_uv`, `gate_step_uv`, `driver_supply_uv` and
`source_voltage_uv`. The touchscreen still displays and accepts decimal volts;
entering `0.025` stores `25000`, and `2.000001` stores `2000001`. Nonzero digits
below one microvolt are rejected instead of silently rounded. Settings must
not mix the old `_v` fields with the new `_uv` fields.

Version 3 replaces `batches`, `samples_per_batch` and `sample_pause_ms` with
`ina_average_count`; new devices default to 64. Existing version 1/2 settings
migrate the old batches × samples product upward to the next supported count,
capped at 1024. Thus 2 × 2 becomes 4 and 10 × 50 becomes 512. The configured
settling delay remains unchanged. Mixed old/new acquisition knobs are rejected.

Normal boot calls `storage.load_settings(migrate=False)`: old settings are
normalized **in RAM**, without rewriting the file; version 1 numeric volts
are also converted to the nearest microvolt. An explicit Settings save commits version 3 through the
normal temporary-file replacement. Invalid files are reported inline; boot
may use defaults in RAM but does not overwrite the invalid file. Existing
calibration and scan-archive files are not rewritten by settings normalization.

Sweep lengths and terminal-envelope checks use integer arithmetic. Each axis
has `(stop_uv - start_uv) // step_uv + 1` points; their product must not exceed
4096. Thus 0–1.3 V in 0.1 V steps always has 14 points, including 1.3 V, on
both the Pico and the receiver. The drive layer converts each requested target
to volts only when using the measured calibration map. ADC readings, corrected
currents and displayed/plotted measurements retain their volts/amps units.

## Wi-Fi setup and saved networks

Use **Settings → Advanced → Wi-Fi** to find nearby networks or pick a saved
network. Selecting a recognized SSID offers **Use saved pw**; selecting a
new password-protected network offers a masked touchscreen keyboard with
uppercase/lowercase, symbols, backspace and an explicit reveal option.
Connection status and the assigned IP address appear on the scanner. These
indicate a local Wi-Fi connection, not verified cloud connectivity.

The scanner remembers up to **20 distinct SSIDs** in `wifi.json`. A successful
connection moves that profile to the front; adding a 21st removes the least
recently used profile. A new password replaces an SSID's saved password only
after connection succeeds and an IP address is obtained. Failed attempts,
timeouts and Cancel preserve the previous working credentials. **Forget**
removes the selected profile. Wi-Fi profile changes are independent of the
scan-settings draft and do not change calibration.

Credentials are stored in the Pico's ordinary filesystem, without encryption
at rest. Passwords are excluded from status messages, USB diagnostics and
the host verification reports; they are visible on-screen only when reveal
is explicitly selected. Do not copy `wifi.json` into source control or share
it with scan results. Firmware updates preserve the credential file.

Startup and Home do not attempt a connection. In Settings, network discovery
and explicit connection allow credentials to be tested and saved; they are
prohibited during measurement and calibration. Leaving Settings restarts into
a quiet Home.

Before acquisition, both Wi-Fi interfaces are deactivated and checked. Failure
to stop the radio prevents acquisition. Only after a scan has stopped its
outputs and prepared its payload does the Scan path call
`autoconnect(..., persist=False)` for the most recently saved network. Connection
polling is bounded; this automatic connection does not reorder or rewrite
`wifi.json`. No saved network, connection failure or timeout takes the upload
failure path below. Settings calibration keeps its own pause/restore lifecycle.

The first pass targets ordinary 2.4 GHz personal-password and open networks.
Enterprise authentication, browser login portals, hidden-network entry, a
phone setup website and Internet connectivity checks are not implemented.

## RAM scans and cloud upload

Use **Settings → Advanced → Cloud** to enter the published Google Apps Script
receiver URL. The URL editor is masked unless Show is selected. Editor OK
returns to the Cloud draft; Cloud OK saves `cloud.json` with upload enabled,
and Cancel discards edits. This page configures the receiver only: there is no
auto-upload toggle, queue, Retry or Storage control. A previously disabled
configuration must be explicitly saved here before it is used. The accepted
URL has the form `https://script.google.com/macros/s/<deployment>/exec`, with
no query or fragment. See the
[Google Sheets receiver instructions](../cloud/apps-script/README.md).

The URL authorizes submissions. Treat `cloud.json` as private, like
`wifi.json`; exclude both from source control and shared diagnostic reports.
No Google account password is stored on the Pico. A local Wi-Fi connection
alone does not confirm that a scan reached the receiver.

After a complete scan, the UI prepares the payload, connects Wi-Fi and uploads
automatically. A recognized current-limit or ADC-saturation trip stops the
outputs and automatically uploads only the fully completed averaged points,
plus the partial-scan diagnostic. If no point completed, the fault stays on
the scanner's **SCAN STOPPED** screen and no upload starts. The unfinished
point and conversions never become measurements.
Sensor communication failures, invalid readings, calibration errors and failed
output shutdown remain hard errors: no upload occurs, and OK restarts.

Success requires an acknowledgement matching the exact scan ID, body SHA-256
and point count; HTTP success alone is insufficient. After that acknowledgement,
the device discards local RAM data and restarts. Google Sheets receives a data
tab per scan and its Index link. The receiver must support wire schema 3.

Connection or upload failure shows exactly:

```text
WiFi Error, Upload Failed
Please reconnect WiFi in settings and redo scan.
```

Both **OK** and **CANCEL** discard the RAM scan and restart; there is no
persistent queue or automatic retry. Power loss or a
reset also loses the RAM scan. CANCEL during an upload discards local data,
but cannot retract a tab the receiver already accepted; acknowledgement loss
can leave a cloud tab even when the device reports failure or is cancelled.

## Historical local archive: legacy firmware and tools

The following archive/retention details describe the former persistent-scan
path. They are not used by current Home/Scan/Upload. Existing files in `scans/`
are neither reclaimed nor uploaded automatically, and the new UI hides Storage.
Keep existing data files and sidecars together for the retained archive tools.

### Local binary files and measurement provenance

`scans/` holds the local archive. `.writing` and `.draft` files are unaccepted;
only an explicit complete/stopped-scan OK renames a draft to `.pending`. An acknowledged
upload becomes `.sent`. Each completed file has a matching `.meta` sidecar with
its binary integrity hash, deterministic JSON-body hash, wire length and point
count. Acknowledgement order is added atomically before publishing `.sent`;
it is covered by the metadata hash and does not alter the upload body. Keep
the data file and sidecar together when backing up accepted scans.
The data files are binary, despite containing a small JSON metadata header.

The legacy writer uses binary version 2: a bounded header, **54 bytes per averaged point**,
a bounded JSON outcome/fault trailer, and a 12-byte `<III>` footer containing
point count, duration and trailer byte length. Existing version 1 files retain
their 8-byte count/duration footer and exact original JSON upload bytes. Both
formats remain readable and retryable. The point record is little-endian `<HH12fBB`:
two integer indices, twelve float32 measurements, a batch count and two packed
voltage-limit flags. At 4096 points, records occupy 221,184 bytes, plus the small
header/footer and sidecar. Requested and measured voltages, corrected currents,
raw shunt means, their between-batch standard deviations and the limit flags
are retained; individual raw conversions and batch-mean arrays are not archived.
Version 2 uploads use `schema_version: 2`, `outcome: "complete"` with `fault: null`,
or `outcome: "partial"` with a bounded diagnostic. Its index identifies the next,
unfinished point; requested drain/gate voltages, curve and acquisition phase give
context. Saturation retains the raw ADC code, sign and limit, with measured value
`null`; a current trip retains the signed raw current in amperes separately from
the completed-point measurements. Complete scans still require every requested
point; partial scans must stop before that count. Acknowledgement hashes cover
the outcome and diagnostic as well as the points.

The header records a stable device/random scan ID, settings snapshot, calibration
metadata and a SHA-256 of the streamed calibration-object serialization. That
calibration hash is not the binary file's integrity hash; retain
the matching calibration file separately if the full baseline curve is needed
later. Capture UTC is `null` when the Pico clock is not plausibly set. Network
clock setup before an upload cannot reconstruct an earlier unknown capture
time; the receiver records receipt time separately.

`UploadSource` verifies binary integrity, then streams the agreed JSON schema
in chunks of at most 1024 bytes. Float32 values use nine significant decimal
digits; reopening and retrying on the tested Pico runtime reproduce the same
JSON bytes. No second JSON scan file or full-scan JSON string is created on the
Pico. Float32 rounding is smaller than the INA226 quantization over the current
measurement range; shunt calibration and baseline drift still determine
measurement accuracy.

Header parsing, integrity hashing and header replay use 256-byte file reads;
the Pico never materializes the entire header or a decoded duplicate. Local
finalization also hashes the upload body in 256-byte chunks. The sidecar is
read and closed before opening the binary file, avoiding two concurrent
LittleFS file buffers. These bounds address a reproduced 1,881-byte header
allocation failure after the 68 kΩ calibration enlarged its metadata.
Wi-Fi resumes only after local finalization, with temporary objects collected
first. Failure notices/logs include fixed stage/category diagnostics such as
`[source/memory]`, without exposing receiver URLs or exception contents.

After copying a `.sent` or `.pending` file and its matching `.meta` sidecar to
the host, export the measurements to CSV from the repository root:

```powershell
python prototype/export_scan.py saved_scan.sent output.csv
```

Keep `saved_scan.meta` beside `saved_scan.sent`. The exporter verifies archive
integrity and writes the stored point columns and values, including requested
and measured voltages and voltage-limit flags.
For a partial scan it also writes `output.csv.metadata.json`, so even a zero-row
CSV retains its stop reason and provenance. `--metadata path.json` selects the
metadata path or requests metadata for a complete scan.

CSV export is valid across runtimes, but CPython and RP2 MicroPython can spell
the same float32 value differently with `%.9g`. Host-reconstructed JSON is
therefore not necessarily byte-identical to the Pico's stored wire hash. Do not
repost a scan under its existing ID using a different JSON formatter.
`prototype/cloud_receiver_check.py --scan` checks the reconstructed body hash
against the sidecar before any POST and declines a mismatch. The legacy queue/tooling provides authoritative retries for those accepted
scans; the new Home does not service that queue.

The queue holds at most **20 unacknowledged scans**. Space is checked before
starting a scan; a full queue or insufficient flash prevents acquisition.
Unacknowledged scans are never automatically evicted. Up to five acknowledged
local scans are retained, with older acknowledged files removed first; fewer
may remain when space is needed for a new scan. This is a working archive,
so copy scans elsewhere when long-term local retention matters. Retention
uses persistent acknowledgement order, so a reset or rollback of the Pico
clock cannot cause the newest uploaded scan to be selected as the oldest.

If the remaining queue or space limit still prevents a scan, pressing **SCAN**
opens **Local scan storage** before acquisition starts. The same screen is
available from **Advanced → Storage** and **Cloud → Storage**. It shows actual
free space, the uploaded bytes eligible for automatic reclamation, and whether
the saved scan settings will fit. Its calculation includes the configured
point count and the existing 32 KiB safety margin; it uses the same policy as
the acquisition preflight. Abandoned unaccepted drafts that the next scan
will reclaim are included in the preview and identified separately.

Accepted scans appear oldest first, with capture time when known, queue/upload
status, point count and local size. Tap one scan to review its full ID, then
press **OK** on the confirmation screen to delete only that local scan and its
sidecar. **CANCEL** on that screen keeps it. A queued scan has **no confirmed
cloud copy**; the confirmation warns that deletion may remove its only copy.
Deleting an uploaded local copy does not delete the Google Sheets tab.
Unreadable metadata is labelled and remains selectable by its filename ID.

The uploader stops before listing or deleting scans. Active drafts and a scan
currently being uploaded cannot be deleted directly. The list and capacity are
checked again after every deletion, so several individually confirmed deletions
can make room. Storage **OK** returns Home without starting a scan or saving
unrelated settings drafts; press **SCAN** separately when ready. Storage
**CANCEL** returns to the screen that opened it. Existing automatic uploaded-scan
reclamation and five-scan retention remain; queued scans are never silently
evicted.

## HTTPS trust

The transport requires certificate validation and the requested hostname.
`google_roots.pem` is a public trust bundle, not a credential, and must be
installed with the firmware. Certificate, clock or connection failures take
the RAM upload-failure path; there is no fallback to unverified HTTPS. Response redirects
are restricted to Google's `script.googleusercontent.com` host and use GET
without resending the scan body.

The bundle contains the four GTS roots R1–R4, downloaded over verified HTTPS
from Google's certificate repository:
[R1](https://pki.goog/repo/certs/gtsr1.pem),
[R2](https://pki.goog/repo/certs/gtsr2.pem),
[R3](https://pki.goog/repo/certs/gtsr3.pem), and
[R4](https://pki.goog/repo/certs/gtsr4.pem).
The concatenated file is 5,352 bytes; SHA-256 is
`6ba8f2f19e161f295a52b49e627796d7dbbeab3c7e60a46b07529e98e18536d7`.

### Historical HTTPS verification

These are earlier persistent-scan deployment results, not verification of the
current RAM workflow. The receiver used a private development workbook;
its identifier and deployment URL are intentionally omitted from this repository.
Real bench scans have passed certificate-verified POST, Google response
redirect and matching acknowledgement checks with the complete UI resident.
Three scans' stored measurements were inspected in the browser; repeated uploads
created no additional scan tabs. Four consecutive full-application upload
cycles passed. Wrong-hostname rejection was verified separately.

A fresh two-point scan also uploaded automatically through the normal
`main.py` boot path. Its measurements, 4,398 ms duration and completed Index
entry were verified in the browser. The workbook contained exactly three
scan entries and three data tabs after the repeated-upload checks.

## Calibration and averaging

Before selecting **Calibrate — DUT disconnected** in Advanced:

1. Disconnect the MOSFET and all test loads from both outputs.
2. Remove meter probes and both IN+–IN− shunt-bypass jumpers.
3. Leave the normal amplifier, shunt, VBUS-sense and ground wiring connected.
4. Set Driver supply voltage to the actual LM358 V+ supply and confirm the
   open-fixture prompt on screen. Confirmation also saves the settings draft.

Calibration is never automatic at startup. It first measures each open
output at zero PWM duty, including its actual voltage floor, then sweeps
upward from that floor to discover the channel's reachable ceiling. This
supports the drain's source-referenced floor as well as the gate's near-ground
floor. It saves measured terminal voltage, raw shunt baseline and PWM duty.
It replaces the saved calibration only after
successful completion. Cancelled or failed calibration does not publish a
partial curve. Repeated voltages at a saturation plateau are excluded from
the strictly increasing interpolation map. Each channel records whether it
reached a plateau, its permitted drive ceiling, or the declared rail target.
Probe duty is bounded by
the declared supply, nominal gain and LM358 input common-mode allowance;
at a 5 V supply it never exceeds about 17.8% PWM.
Recalibrate after changing a sensor, shunt, amplifier wiring or supply, and
when temperature or drift makes the open-output readings unreliable.

New calibration metadata includes exact `driver_supply_uv` and
`source_voltage_uv` integers alongside the existing volts values. Supply
compatibility uses the integer field when available; older calibrations use
their stored volts value. Measured calibration points and interpolation remain
in volts. New triggered acquisitions require metadata identifying
`acquisition_mode = ina226_triggered_v1`, the matching `ina_average_count`,
`bus_conversion_us = shunt_conversion_us = 1100`, and
`calibration_repeat_blocks = 3`. Old continuous-mode calibrations remain
readable and preserved on disk, but cannot drive a new scan. Home and Settings
stay available so the user can explicitly recalibrate with the DUT removed.

Calibration reserves fixed float32 arrays before driving and fills their
numeric cells in place. Each point retains terminal voltage, shunt baseline,
shunt scatter and duty: 16 bytes per point, or 6,368 bytes for both 199-point
tables at an 18.8 V rail. The largest supported 30 V grid reserves 311 points
per channel (9,952 bytes total). A separate populated count excludes unused
capacity after a channel reaches its ceiling; buffers never grow or shrink
during acquisition. Zero-floor support and the final inverse-drive endpoint
use small, separate fields. Targets are generated lazily. Sample sums and
calibration-block means also use fixed numeric arrays, and the live summary
updates one preallocated mapping. Scan result rows remain independent snapshots.

Calibration persistence uses **`calibration.bin` only**. Its versioned header
contains the two point counts and a bounded metadata length. The point tables
are little-endian four-float32 records, read directly into preallocated arrays.
The small metadata/endpoint envelope encodes floating-point bits exactly,
avoiding decimal round-trip changes. A SHA-256 trailer covers the header,
metadata and tables; truncated, corrupt, oversized and unsupported files fail
closed. Both numeric tables are allocated before decoding metadata. No list
of point dictionaries is constructed.

Boot loads and fully validates the calibration once, before constructing the
display, inputs and Wi-Fi objects, with PWM pins low. Home and scan preparation
then check settings compatibility and endpoint coverage without repeating the
table walk. Trust belongs to the App's exact read-only calibration object and
is discarded when that object is replaced, released or reloaded. The public
engine API still fully validates an untrusted calibration by default.

Scan interpolation uses binary search on those validated monotonic tables,
reading packed values directly without constructing point dictionaries. Exact
knots use the same preceding interval and arithmetic as before, preserving
native float32 results and the existing endpoint coverage tolerances. The
gate's target-to-duty lookup is cached for its drain sweep and reset whenever
an operation starts; drain drive and both measured-current baselines are
still looked up for every point. Calibration sweeps do not use this cache.

The old JSON reader is an explicit one-time migration tool under `prototype`,
not deployed firmware. [migrate_calibration_binary.py](../prototype/migrate_calibration_binary.py) converts the existing
map and verifies its canonical fingerprint after native binary reload. Only
after verification is the old JSON removed from the Pico. Boot never migrates
or falls back to JSON. The canonical JSON serializer remains solely for
fingerprinting/export compatibility; it is not the persistence format.

After cloud/Wi-Fi shutdown and garbage collection, the Settings path opens the
atomic-save temporary writer before driving, allocating its actual filesystem
buffer early. Completion writes, flushes and closes that writer before replacing
the committed file. Failure or cancellation aborts and closes it before reloading
the saved map. Entering Settings has already released the 96 KiB TLS reserve;
the following soft restart restores that reserve before scanning.

During explicit calibration, an already committed previous map leaves App,
engine and cloud-cache RAM while its file stays untouched. On cancellation or
failure, the candidate is released and a later UI tick reloads and validates
the committed map after acquisition/save frames have unwound. Wi-Fi resumes
after that attempt, provided output shutdown succeeded. A reload failure
leaves the file intact and reports a recoverable notice; scanning requires a
valid calibration. This avoids retaining two large maps during recalibration.

For each scan point, the engine applies voltages, waits for the configured
settling delay (at least 100 ms), triggers both INA226s, polls until both
report conversion-ready, and reads one averaged pair. Each chip averages the
selected number of bus/shunt conversions at **1.1 ms per conversion**; a 64-count
block takes nominally 140.8 ms, excluding settling and I²C overhead. The two
chips are triggered and read sequentially, so they are not perfectly simultaneous.
Polling remains cooperative, Cancel stays active, and conversion timeout is bounded.

Calibration measures **three hardware-averaged blocks per point** using the
same selected count. Its stored baseline scatter is the sample standard
deviation across those three blocks. Ordinary scan points use one block and
report variance as unavailable; zero is not substituted for an unknown deviation.
During settling, both sensors run continuously without averaging. Their
positive shunt-limit comparators latch individual-conversion excursions, which
the firmware checks during settling and while waiting for an averaged block.
This is software-polled protection, not a physical ALERT shutdown interlock.
The single comparator on each sensor is configured for positive current;
negative current is checked on the fresh pretrigger reference and completed
averages. A longer averaging interval can therefore delay detection of a
negative excursion. The averaged result cannot reveal every transient inside
its block. Absolute current thresholds, sensor saturation, voltage-envelope
and calibration-coverage guards remain in place.

Calibration normally uses 0.1 V increments above each channel's measured
zero-duty floor, refined to 0.05 V for increments from 0.8–1.8 V. Targets
are capped at the declared supply rail, and each channel finishes independently
when it reaches that rail, its drive bound or a measured plateau. The first
point also records `zero_min_v` and `zero_max_v`, the voltage range observed
while averaging at zero duty. Raw current, sensor-range and averaging-stability
checks remain active throughout that initial measurement.

The code interpolates between measured points. It never extrapolates the
baseline slope; a 5 mV edge allowance uses the nearest endpoint to absorb
small readback variation. A newly discovered ceiling also records the actual
minimum and maximum voltage observed while averaging its baseline. Within
that measured ceiling support, the endpoint baseline can be reused without
extrapolating its slope. This support is separate from the inverse drive map;
a noise peak is not treated as a reachable drive target. A scan needs a valid calibration covering its
actual measured voltages. Drive commands within 20 mV of a calibrated endpoint
use that endpoint's PWM duty, matching the settling tolerance and PWM step
size.
For example, a requested 3.000 V can use an endpoint measured at 2.985 V,
and the result records the actual 2.985 V. This command allowance does not
widen the 5 mV baseline allowance. Ordinary scan points apply the calibrated
PWM setting, wait for settling, then average the actual measurements. A stable
voltage above or below its request no longer aborts a scan or triggers drive
corrections and extra settling retries. Calibration and strict drain parking
retain their correction logic. Current/ADC limits, valid readback, voltage
envelope and baseline coverage checks remain active. The completed hardware block
is compared with the measured post-settle reference using the existing 40 mV
span guard. The hardware average does not expose the extrema of its individual
internal conversions; stability is checked against measured output, not target.

An undershoot at a known complete calibrated drive ceiling retains the
per-channel **VOLTAGE LIMITED** flag. Other target mismatches are preserved by
the requested and measured values without implying a proven hardware limit.
Several requested steps may consequently record almost identical voltages.
The display shows measured VDS/VGS and plots measured VDS versus corrected Id.
If averaged measured VGS falls outside the inclusive requested VGS ±100 mV
band, its real-time chart point is drawn as an isolated amber marker: no line
connects into it, out of it, or bridges across it. Adjacent in-band points in
the same requested gate sweep still connect. A drain mismatch alone does not
break the line because measured VDS is the x-axis. Every completed point is
still retained in RAM, including both measured voltages. The distinction uses
bit 2 of the packed record's flags and is also sent in wire schema 3, so the
cloud plot can keep the same gaps. This plotting tolerance is separate from
the 20 mV drive/calibration tolerance. Existing uploaded scans retain their
recorded classification; historical binary archives remain unchanged.

The version-1 table layout remains supported within its original coverage
when its acquisition metadata matches the current settings. Old continuous-
mode calibrations still require recalibration. A version-1 table's last point
does not establish a hardware ceiling; requesting above
that range requires a new open-output calibration. Baseline extrapolation
remains prohibited. Source parking must still be reachable: clipping is not
accepted as an arbitrary substitute for source parking.

A fresh version-2 calibration can establish a small positive drain floor when
component tolerances prevent reaching VS at zero PWM. This requires measured
`zero_min_v`/`zero_max_v`, the same exact saved `source_voltage_uv`, and a floor
no more than **0.5 V above VS**. Parking then uses that calibrated floor. Every
zero-duty drain reading must stay within **100 mV** of it and at or above
**VS − 5 mV**; crossing either bound still stops the scan. This wider parking
band applies only to the validated positive floor at actual zero drain duty.
Calibration's 20 mV target tolerance, the 40 mV measured-span guard and the
5 mV baseline edge allowance remain unchanged. Nonnegative VDS requests below
the floor retain the actual measured VDS
with the drain voltage-limited flag. Negative VDS requests are rejected in
this mode. Changing the source reference requires fresh calibration.

The above-source-floor path removes gate drive before parking drain on normal
completion or cancellation. Driver-level zero/close/fault cleanup also stops
gate before drain. Current trips remain active; this ordering provides no
hardware isolation or protection while the controller is stalled.

At actual zero PWM duty, a calibration with measured floor support may reuse
its first-point baseline for terminal readings from `zero_min_v - 5 mV` up to
the averaged first endpoint. This bounded allowance also works at a nonzero
floor such as 3.3 V. Both stored bounds are validated, including the existing
40 mV maximum measured span; a wide or incomplete floor range is rejected.

The existing near-ground allowance is retained: when the first zero-duty
endpoint is within 0–20 mV of GND, readings from -5 mV up to that endpoint
may reuse its baseline at zero duty. Older tables without measured floor
bounds additionally require an absolute target of zero to use this exception.
These allowances change current correction only; actual voltage readback
remains intact. Nonzero drive keeps the normal baseline coverage checks, and
ordinary source parking must still reach VS within 20 mV. The separately
validated positive zero-duty floor uses the 100 mV parking band and independent
VS − 5 mV lower bound described above; accepting a parked voltage does not
authorize a baseline-corrected sample at that voltage.
The upper-edge rules remain unchanged. Errors identify the channel.

For each channel, with sensor VBUS connected upstream of the shunt:

```text
Vshunt   = signed INA226 shunt code × 2.5 µV
Vbus     = INA226 bus code × 1.25 mV
Vterminal = Vbus − Vshunt
VDS/VGS  = Vterminal − configured VS
Icorrected = (Vshunt − interpolated open-output baseline(Vterminal)) / Rshunt
```

The terminal-voltage calculation always uses the raw shunt voltage. Baseline
correction applies to current only. Positive current flows from amplifier
IN+ toward DUT IN−; negative corrected readings are retained. The drain now
has a **nominal 1 Ω shunt, calibrated effective scale 0.789 Ω**
using a user-specified 9.98 kΩ reference on 2026-11-02. Gate remains a
**nominal 680 Ω shunt with a provisional 638 Ω in-circuit measurement**.
The drain result is an installed current scale, including the sense path
and sensor response; it is not an
exact four-wire resistor measurement. Its approximately 21% difference from
nominal cannot be attributed to the component alone from these readings.
Reference/DMM uncertainty and sensor gain errors remain. Averaging reduces
noise, not those systematic errors or baseline drift. The drain software
current limit remains **5 mA**; replacing the shunt does not raise it.

## Prototype wiring reference

This is the revised 2026-11-02 wiring: a 3.3 V source reference, a drain
offset resistor and a relay-switched battery supply. The MOSFET source is
connected to Pico **3V3(OUT), physical pin 36**, as confirmed by the user.
It shares this rail with the drain offset reference R13. The
configured driver-supply/source values must match the actual wiring; the
firmware does not measure or select those supply rails automatically.
The tracked schematic is split into four SVG sheets:
[analog channels](../prototype/full/full-01-analog.svg),
[power and logic](../prototype/full/full-02-power-logic.svg),
[connections](../prototype/full/full-03-connections.svg), and
[pinouts and parts](../prototype/full/full-04-pinouts-parts.svg).
The [connection manifest](../prototype/full/connections.json) names every net.
[draw_full_schematic.py](../prototype/draw_full_schematic.py) regenerates these
and the local PDF `output/pdf/pocket-scanner-full-schematic.pdf` using ReportLab
and Windows Arial fonts. The schematic's J1 assignment **1=D, 2=G, 3=S is a
fixture connector**, not the lead order of an arbitrary MOSFET package.
Verify the actual manufacturer's package drawing and viewing direction.

Pico physical pin numbers are the 40-pin header numbers.

| Function | Pico GPIO/name | Physical pin |
|---|---|---:|
| Drain PWM, 100 kHz | GP20 | 26 |
| Gate PWM, 100 kHz | GP21 | 27 |
| INA226 I2C1 SDA | GP18 | 24 |
| INA226 I2C1 SCL | GP19 | 25 |
| INA226 logic supply and I2C pull-ups | 3V3(OUT) | 36 |
| Source reference VS, drain offset reference, K1 module VCC and ACTIVATE | 3V3(OUT) | 36 |
| Common ground, battery negative and K1 module GND | GND | 38, or another GND pin |
| Display SPI0 clock / MOSI | GP2 / GP3 | 4 / 5 |
| Display CS / DC / RESET | GP5 / GP6 / GP7 | 7 / 9 / 10 |
| Touch I2C0 SDA / SCL | GP8 / GP9 | 11 / 12 |
| Touch RESET / INT | GP10 / GP11 | 14 / 15 |
| Left / right physical buttons | GP15 / GP14 | 20 / 19 |

Drain INA226 address is **0x44**; gate is **0x41**. Breakout I2C pull-ups go
to 3.3 V. Each board's voltage-sense **VBUS joins its IN+ and amplifier
output**, not Pico VBUS. IN− goes to the corresponding DUT terminal through
the board's shunt. ALE/ALERT is unused.

The drain PWM filter now uses the fitted **R1 = 1 kΩ, 5%** and **C1 = 10 µF**.
This replaces the temporary 1.1 kΩ resistor and the earlier incorrectly
fitted 10 kΩ resistor. The gate filter
remains **R6 = 1 kΩ nominal / C3 = 10 µF** and has not been remeasured.
Both LM358 feedback networks use 68 kΩ from output to inverting input and 10 kΩ from inverting
input to GND. **R4 stays connected to GND.** The new **R13, 6.8 kΩ, joins
drain IN1+ (LM358 pin 3) to Pico 3V3(OUT)**. It shares pin 3 with the drain
filter output. The gate filter and feedback network remain unchanged.

With these nominal ratios, before clipping and before each shunt's drop:

```text
Drain driver = Vreference + 6.8 × average drain PWM voltage
Gate driver  = 7.8 × average gate PWM voltage
```

With the MOSFET source on the same nominal 3.3 V reference, ideal zero-PWM
drain output is **3.3 V**, giving nominal **VDS = 0 V** before the shunt drop.
R1's 5% tolerance and the other resistor tolerances can put the actual floor
above or below VS, so the nominal equation does not replace calibration.
The firmware's bounded, calibrated above-source floor support remains
relevant if a fitted circuit's minimum drain output is slightly above VS;
it requires matching source metadata and permits at most a 0.5 V offset.
Zero gate PWM gives approximately **-3.3 V VGS**. Both upper limits are the measured reachable terminal voltage
minus VS. An 18 V supply is not an 18 V output guarantee: LM358 headroom,
loading and shunt drops reduce the available range. The drain offset branch
changes its nominal filter time constant to `(1 kΩ || 6.8 kΩ) × 10 µF`,
about **8.72 ms**, while the gate remains **10 ms nominal**. PWM remains 100 kHz.

For historical comparison, a 2026-11-02 zero-drive check with **1 kΩ, 5% R1**, outputs open,
probes removed and both shunts unbridged held both PWM pins low and averaged
40 INA226 readings. Drain measured **3.292975 V** (range
3.2912574-3.2937572 V); gate measured **0.020715 V** (range
0.019985-0.022495 V). Against the manually configured 3.3 V source, the
drain sat about **7 mV below VS** in that check. Later measurements and the
saved calibration have a higher floor; this early result does not establish
current zero-VDS coverage. The source rail was not measured by this check. See the
final 1 kΩ zero-drive readback (`output/smoke-test/source33-zero-1k-final-20261102.log`, local artifact).

For history, the earlier 2026-11-02 check with the **temporary 1.1 kΩ R1**
after reseating the sensors held both PWM pins low and averaged 40 INA226
readings. It measured drain **3.667564 V** (range 3.66625-3.67000 V) and
gate **0.008904 V**. Subtracting the manually configured 3.3 V source gives
an observed zero-drive VDS of about **+0.3676 V**; the actual source rail
was not measured by this check. Resistor tolerances and the reference rail
can change the ideal estimate. Those measurements belong to the replaced
resistor, not the current circuit. Neither zero-output check is a completed
drive/baseline calibration. See the
historical 1.1 kΩ readback (`output/smoke-test/source33-zero-1k1-reseated-20261102.log`, local artifact).

A later 2026-11-02 repeat, after the user installed **C5**, compared seven
identical PWM command states using the old continuous INA226 acquisition,
open outputs, the display active and Wi-Fi paused. Gate-dependent drain
droop decreased from **26.1–37.4 mV to 12.1–20.4 mV**, approximately a
45–55% reduction, but remained measurable. Terminal reading spans did not
improve consistently. The drain's zero-drive mean also changed from
**3.278288 V to 3.364192 V** between runs. Power was cycled to install C5,
and neither supply/reference rail was independently measured in these
logs, so the absolute shift cannot be attributed to C5. Identical PWM
readbacks produced changed gate voltages as well; the earlier calibration
and zero-drive observations therefore do not establish present coverage.
**C6 installation is still unconfirmed.** See the
C5 comparison and raw-log links (`output/smoke-test/source33-C5-comparison-20261102.md`, local artifact).
This comparison does not measure high-frequency oscillation or justify
relaxing the calibrated floor/baseline guards.

The user accepts **100 mV drain-voltage accuracy** as a hardware design
criterion for this prototype. Scans still record the measured terminal
voltage rather than substituting the requested value. This acceptance
criterion does not extend the voltage range supporting a valid baseline
correction or raise current limits. The separate positive-floor drain-parking
check now uses 100 mV with an independent VS − 5 mV lower bound. The physical
residual grid passed for VDS targets starting at 0.1 V. The current device
now saves **drain start = 0.1 V** (`drain_start_uv = 100000`), as selected by
the user. Cross-gate calibration of the zero-VDS baseline is deferred.
Requested 0.1 V does not force or round the recorded voltage to 0.1 V:
readback, plots and uploads retain the actual measured value.

Drain uses LM358 output pin 1, inverting pin 2 and non-inverting pin 3;
gate uses output pin 7, inverting pin 6 and non-inverting pin 5. LM358 pin 4
and both INA226 grounds connect to Pico GND. Neither Pico VBUS pin 40 nor
VSYS pin 39 supplies the analog rail or the MOSFET source in this
revision. The firmware does not use onboard ADC3 to measure this 3.3 V rail.

The two 9 V batteries, **B1 and B2**, are in series. Pack negative connects
to **GND**; pack positive passes through **K1**'s **COM and normally-open (NO)
contacts** to LM358 **V+ pin 8** and its local bypass capacitors. This makes
V+ nominally **18 V relative to GND**, not 18 V above the source. Battery
voltage changes with charge and load; measure it at LM358 pin 8 when setting
Driver supply voltage. Do not join battery positive to Pico VBUS, VSYS or
3V3.

**K1 is a relay module**, identified by the user as
[Amazon ASIN B0F3TM3RZ6](https://www.amazon.com/dp/B0F3TM3RZ6); the listing
identifies a Teyleten Robot module with an HFD4/3-S relay. The user confirmed
that its external **VCC and ACTIVATE terminals are tied together to Pico
3V3(OUT)**, and its **GND terminal connects to common GND**. This keeps the
module activated while 3.3 V is present, including through a soft reset;
there is no GPIO control. K1's external VCC/ACTIVATE/GND and COM/NO labels
describe terminal functions, not physical header-position numbers. Use the
module's printed labels.

The module's internal driver, suppression circuit and total current draw are
not yet verified. This is not a bare-coil connection, and the drawing does
not prescribe an extra flyback diode across the module supply. Internal
coil wiring and any existing suppression must be identified before adding
a component. The relay component's coil specification alone does not verify
the complete module's current consumption or circuit.
The [Hongfa HFD4 datasheet](https://source.hongfa.com/Uploads/Product/PDF/HFD4_en.pdf)
is the relay-component reference; its package pin numbers do not describe the
module's header order.

The new source offset and battery supply were calibrated open-output on
2026-11-02 with an entered driver rail of 18.8 V and source of 3.3 V. This
replaces the earlier 5 V map; the physical calibration results and limits are
recorded under Development checks below. Further resistor, source-reference
or supply changes still require fresh drive and baseline calibration.

Historically, after the 68 kΩ replacement, both drive maps and open-output
current baselines were recalibrated on 2026-11-02 using 10 batches of 50
samples. Measured unloaded
ceilings on the 5 V test supply were 3.633 V drain and 3.500 V gate. All 18
independent ascending/descending checks completed; a 2 V gate command repeatedly
measured 2.0243 V, outside the **then-current** chart's ±20 mV connection
band. The current plot band is ±100 mV. Calibration was saved and normal UI startup verified, preserving settings,
saved scans, shunt scales and current limits. See the
verification record (`output/smoke-test/gain-68k-final-verification-20261102.json`, local artifact).
Its averaged open-current residuals describe this unloaded check, not absolute
current accuracy or behavior under load.

The display is 480×320 landscape. The user confirmed it is upright/readable
and the on-screen SETTINGS and OK touch targets work. The physical button
check confirmed left GP15 and right GP14, as listed above.

## Install or update

Use the Pico's existing MicroPython firmware; this is a module upload, not a
UF2 reflash. The verified runtime is MicroPython **1.29.0**, board
`RPI_PICO2_W`, with `sys.implementation._mpy == 7942`. The RAM workflow reserves
its large buffers before application imports, so deploy precompiled `.mpy`
modules to avoid the source compiler's peak allocation during startup.

The build helper currently targets **Windows x64**. From a fresh clone's
repository root, use Python 3.12 to create a local environment, install the
hash-pinned compiler, then build and verify the public modules. No device is
accessed by these commands:

```powershell
py -3.12 -m venv .venv
$scannerPython = (Resolve-Path .venv/Scripts/python.exe).Path
& $scannerPython -m pip install --target .tools/mpy-cross --no-deps --only-binary=:all: --require-hashes -r .tools/mpy-cross-requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Compiler installation failed' }
& $scannerPython prototype/build_mpy.py
if ($LASTEXITCODE -ne 0) { throw 'Firmware build failed' }
& $scannerPython prototype/build_mpy.py --check
if ($LASTEXITCODE -ne 0) { throw 'Firmware build verification failed' }
```

[build_mpy.py](../prototype/build_mpy.py) requires compiler 1.29.0 and emits pure bytecode with
31-bit small integers, assertions and traceback line numbers retained
(`-O0 -msmall-int-bits=31 -X emit=bytecode -X source-lines`). It writes only
`output/firmware-mpy/` and never accesses the Pico. The manifest records source
and output hashes; rebuild after every firmware edit and require `--check`
to pass before transfer. If the Pico's MicroPython version changes, check
[bytecode compatibility](https://docs.micropython.org/en/v1.29.0/reference/mpyfiles.html)
before using these artifacts.

Disconnect the DUT during installation and initial UI checks. For the first
migration from source files, back up the existing public `.py` modules on the
Pico. After copying the compiled files below, remove only their corresponding
`.py` files listed under `modules[].source` in the manifest: a same-directory
`.py` file takes precedence over `.mpy` and would still invoke the compiler.
Keep `main.py` and any `boot.py` as source. Retain all device data, including
`settings.json`, `calibration.bin`, `wifi.json`, `cloud.json` and `scans/`.
An older device with only `calibration.json` requires the explicit one-time
conversion above before that file and its obsolete reader are removed.

The following commands **do access the Pico**. Install the development-tested
transfer tools in the same virtual environment. Select the intended device by
USB serial; the example ID below is this prototype, not a universal Pico ID.
Close other serial tools before connecting. A transfer interrupts the running
application, so first disconnect the DUT and preserve the device data listed above.

```powershell
& $scannerPython -m pip install mpremote==1.29.0 pyserial==3.5 platformdirs==4.14.3
if ($LASTEXITCODE -ne 0) { throw 'Transfer tool installation failed' }
& $scannerPython -m mpremote connect list
$scannerDevice = 'id:3B8922C9B91BBE9F'
& $scannerPython prototype/build_mpy.py --check
if ($LASTEXITCODE -ne 0) { throw 'Firmware build is stale' }
$scannerFiles = @((Get-ChildItem -LiteralPath output/firmware-mpy -Filter '*.mpy').FullName)
$scannerFiles += (Resolve-Path -LiteralPath firmware/main.py).Path
$scannerFiles += (Resolve-Path -LiteralPath firmware/google_roots.pem).Path
& $scannerPython -m mpremote connect $scannerDevice fs cp @scannerFiles :
if ($LASTEXITCODE -ne 0) { throw 'Firmware transfer failed' }
```

Read back the uploaded public files and compare them with the checked local
artifacts; the build helper's `--check` verifies local files only:

```powershell
$scannerVerify = Join-Path '.local' 'device-public-verification'
New-Item -ItemType Directory -Force -Path $scannerVerify | Out-Null
foreach ($scannerFile in $scannerFiles) {
    $scannerName = Split-Path -Leaf $scannerFile
    $scannerReadback = Join-Path $scannerVerify $scannerName
    & $scannerPython -m mpremote connect $scannerDevice fs cp ":$scannerName" $scannerReadback
    if ($LASTEXITCODE -ne 0) { throw "Readback failed: $scannerName" }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $scannerFile).Hash -ne
        (Get-FileHash -Algorithm SHA256 -LiteralPath $scannerReadback).Hash) {
        throw "Firmware hash mismatch: $scannerName"
    }
}
```

After removing any shadowing public `.py` modules as described above, enter
the USB REPL and press **Ctrl-D** for a soft restart:

```powershell
& $scannerPython -m mpremote connect $scannerDevice repl
```

`main.py` starts the UI with both PWM outputs initially zero. Neither scan nor
calibration starts at boot. Confirm the `Pocket MOSFET ready` marker and a
readable Home screen, then check Settings/Cancel and Scan/OK button roles.
Keep the DUT disconnected throughout installation checks. A missing calibration
leaves Settings available for explicit open-output calibration; do not copy
another instrument's calibration. The powered offset's transient behavior
through reset remains a hardware verification item.

## Present limits

The historical bench bring-up used a source at 0 V. The revised drain
offset targets VDS near zero at zero PWM when source and offset reference
both use 3.3 V and the LM358 is powered. Its behavior through startup,
shutdown and reset has not yet been measured. The firmware's controlled
soft-reset path drives both GPIOs low before restarting; zero gate PWM now
means a negative VGS, not gate equal to source.

The new relay switches the battery supply only. It does not disconnect the
DUT, enforce a current limit, or open on a firmware fault or soft reset.
Opening LM358 V+ is not equivalent to isolating the drain: an attached DUT
can feed an unpowered driver through its body diode and drain sense path.
If fitted, C6 also retains charge after the contacts open. Coil current loads the same
3.3 V rail used as the source reference; its rating, suppression and effect
on that rail still need verification. Positive drain current returns through
the DUT source into this rail. Until rail sinking behavior is verified, the
board and other loads must absorb that returned current. Retain the 5 mA
drain trip and check 3.3 V under load. The Pico's 3V3 output is a regulated
power rail with a shared current budget, not a precision reference; see the
[Pico 2 W datasheet](https://datasheets.raspberrypi.com/picow/pico-2-w-datasheet.pdf).
Do not treat software cancellation, current trips or this supply relay as
protection against transients, a stuck process or power loss. Keep the DUT
disconnected for the new supply/offset checks and recalibration.
Sampled trips currently use raw
apparent current limits of 5 mA drain and 100 µA gate. Stable LM358 clipping
is retained and flagged. Overcurrent, INA226 saturation, sensor failures,
unstable outputs and measurements outside baseline coverage remain faults.

Configuration and calibration persist through Settings. New scan data exists
only in RAM until upload and is discarded on restart. Existing archive files
remain untouched. There is no automatic V+ or source-voltage measurement and
no VSYS ADC read.

The retained legacy archive writer accepts software-averaged settings version
1/2 only and rejects triggered settings version 3 before creating a data file.
Its 17-column format requires historical batch statistics; current hardware
averages use the RAM scan format. Legacy readers preserve the original settings,
measurements and upload identity.

The current entry point reports faults over USB and does not write
`last_error.log` during Boot, Scan or Upload. A pre-existing log belongs to an
older run and is left intact; do not interpret it as the latest RAM-flow error.
The legacy diagnostic logger remains available to diagnostic/Settings callers.

## Development checks

From the repository root, run the hardware-free tests using the virtual
environment created above and a Node.js installation with the built-in test
runner (Node 18 or newer). The Python unit suites use the standard library;
serial transfer tools are needed only for device access.

```powershell
$scannerPython = (Resolve-Path .venv/Scripts/python.exe).Path
& $scannerPython -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }
node --test tests/test_apps_script.js
if ($LASTEXITCODE -ne 0) { throw 'Apps Script tests failed' }
& $scannerPython prototype/build_mpy.py --check
if ($LASTEXITCODE -ne 0) { throw 'Firmware artifacts need rebuilding' }
```

The build check requires a prior successful build; it never connects to USB.
Host tests do not establish analog accuracy, Pico heap behavior or TLS memory
fit. Device helpers in `prototype/` have different fixture and side-effect
requirements; read each helper before using it. In particular,
[display_benchmark.py](../prototype/display_benchmark.py) drives the saved
grid, while [early_scan_stop_diagnostic.py](../prototype/early_scan_stop_diagnostic.py)
drives up to three points and discards them. Neither is a host unit test.

### Current checkpoint, 2026-11-03

The initial repository snapshot passed **684 Python tests** with `-S`
(installed site-packages disabled) and **53 Node.js receiver tests**.
The earlier zero-point fault UI checkpoint passed **679 host tests** and a native real-LCD/RAM
smoke test with both PWM pins low. The native test checked the signed current
notice, both Cancel/OK exit paths, and zero finalizations or uploads.
Five subsequent focused host regressions hardened the three-point diagnostic's
success marker against swallowed errors, stale timing state and incomplete
cleanup; that helper-only change was not another device deployment.
The last verified build contains **20 compiled modules, 139,900 bytes**,
plus source `main.py` and the CA bundle. The manifest and current test commands
are authoritative when the source changes.

The saved grid at this checkpoint is VDS **0.1–2.0 V** and VGS **−0.2–2.0 V**,
both in 0.1 V increments, with **100 ms settling and 64 INA226 averages**.
An initial loaded attempt stopped before its first point on a signed raw
drain current of **−20.916 mA**, exceeding the unchanged 5 mA magnitude limit
during drain parking. The cause is **unresolved**; no wiring diagnosis is
established by that reading. After the user's wiring/probe checks, fresh
zero-drive readings were healthy and a bounded three-point scan passed.
Its last point measured VDS **0.351252 V**, VGS **−0.135138 V**, corrected
Id **0 A**, and Ig **−0.0790 µA**. The scan was discarded without upload.
A diagnostic-only full-row JSON allocation failed in an earlier attempt;
streaming the four scalar measurements passed without changing firmware.

The installed public firmware and 14 retained data files were checked,
temporary helpers were removed, and normal startup reached its ready marker
in **5.063 s**. This is a bounded check, not a full sweep or a loaded-current
accuracy specification. Local records: `output/smoke-test/dut-recheck-verification-20261103.json`,
`dut-corrected-wiring-three-point-streamed-20261103.log`,
`dut-recheck-startup-20261103.json`, and `empty-scan-fault-native-20261103.log`
in the same directory. The 100-point timing comparison above used an earlier
saved gate start of **−2.0 V**, not the latest −0.2 V start.

### Earlier binary calibration and memory checks, 2026-11-02

The binary-only calibration update reduced normal soft-reset-to-first-UI
time from **19.266 s** to **4.750–4.766 s** across three final restarts. The
converted file is **7,511 bytes**, down from **33,402 bytes** (77.5% smaller).
Its native canonical fingerprint is exactly unchanged, so no new electrical
calibration was needed. The JSON file, old reader and temporary conversion
tools were removed from the Pico; firmware has no JSON calibration fallback.
Settings, credentials and all ten historical scan files were unchanged.

Instrumented startup measured **297.632 ms** for binary loading, versus about
**14.5 s** for the previous JSON loads (dominated by calibration). Full table
validation ran once, taking **544.393 ms**; Home's compatibility check did not
repeat it. Fingerprinting still took about **1.68 s**. These profile stages
overlap and include wrapper overhead; the normal restart measurements above
are separate. Evidence: migration/profile (`output/smoke-test/calibration-binary-profile-verification-20261102.json`, local artifact),
normal restart timings (`output/smoke-test/calibration-binary-final-startup-20261102.json`, local artifact),
and installed/data verification (`output/smoke-test/calibration-binary-final-verification-20261102.json`, local artifact).

The final native memory check retained **622 synthetic calibration points**,
streamed the binary save into a non-writing sink with a maximum 256-byte
write, and verified the unchanged canonical fingerprint. It completed with
**29,296 bytes free after collection**, both full tables and the real UI
retained, and physical PWM pins low. No real calibration file was changed.
The separate **4096-record RAM/payload** check also passed with zero filesystem
attempts through its guarded production Python APIs and no real network calls.
Evidence: full calibration memory check (`output/smoke-test/calibration-binary-final-max-grid-native-20261102.log`, local artifact)
and RAM/payload check (`output/smoke-test/ram-binary-calibration-native-20261102.log`, local artifact).

The `test_ram*.py` suites cover packed records, immutable payload preparation,
receiver handoff and the new UI lifecycle. Host tests verify no filesystem
mutation through the Boot/Scan/Upload source APIs, a quiet Home, exact-ACK
restart, cancellation and failure behavior, and direct plotting from the
shared buffer. This is host verification, not native memory/TLS validation.
An earlier triggered-acquisition checkpoint passed **604 Python tests** and
**52 Apps Script tests**, with 19 bytecode modules (133,881 bytes). Those
counts describe that revision, not the current checkout. Direct register
tests on both physical INA226s passed all
eight averaging counts and confirmed that completed triggered registers stay
stable until retriggered. At 64 averages, observed ready times were about
134 ms drain and 140 ms gate, including sequential I2C setup/polling. See
`output/smoke-test/ina226-trigger-counts-20261102.log`. This register-level
check does not establish a completed scan/upload/reboot with the new firmware.
Boot fingerprint failures also leave Home and Settings reachable; acquisition
retries the fingerprint if the startup cache could not be prepared.

The CANCEL feedback update also passed a native LCD timing check with the
production engine armed but never driven: both PWM outputs stayed zero.
After the injected LEFT event entered handling, screen clearing began at
**4.106 ms**, the **RESTARTING** text finished drawing at **206.907 ms**, and
the observed restart callback ran at **284.413 ms** after RAM/radio cleanup.
This measures rendering/cleanup after event detection, not physical-button
debounce or a full reboot. Host regressions cover both physical and touch
CANCEL, cleanup/display failures, and no further acquisition/upload ticks.
See native CANCEL feedback check (`output/smoke-test/cancel-feedback-native-20261102.log`, local artifact).

The 2026-11-02 triggered-acquisition revision passed these native checks:

- The production driver read both physical INA226s at **64 averages**,
  with ready times of **133,868 µs drain / 140,442 µs gate** and stable
  completed registers. A pending 1,024-average measurement was cancelled
  with both PWM outputs zero; the trigger/wait/cancel test took **15,361 µs**,
  including an intentional 10 ms wait. A **synthetic alert-threshold test**
  exercised the latched positive-current alert and shutdown path, then
  restored the production thresholds. It did not produce or measure a real
  overcurrent excursion. All these tests kept PWM at zero. Evidence:
  production INA226 smoke test (`output/smoke-test/ina226-production-smoke-20261102.log`, local artifact).
- A full-UI calibration stress run used a **synthetic instrument**, virtual
  acquisition timing, 64 averages and three calibration blocks per point.
  It retained all **622 points (311 per channel)** at the largest supported
  30 V grid, painted 311 live updates, streamed the completed calibration
  into a non-writing sink and verified its fingerprint. It completed in
  **77,202 ms**, leaving **33,120 bytes free after collection with both
  tables retained**. This is a memory/lifecycle test, not physical calibration
  timing or a 30 V hardware test; physical PWM pins stayed low and production
  files were unchanged. Evidence:
  maximum-grid triggered calibration stress (`output/smoke-test/calibration-triggered-max-grid-native-20261102.log`, local artifact).
- The RAM diagnostic passed **4096 records** in the **69,632-byte**
  shared buffer and streamed **234,351 wire bytes**, with matching hashes,
  exact acknowledgement, discard, a zero-point partial result and four
  simulated upload failures. Its guard recorded **zero filesystem attempts**
  through the known production Python module APIs, after 27 interception
  self-checks. Native/C APIs outside that scope are not intercepted. This run
  reserved the production display pool and TLS region but used synthetic
  measurements and **no real network calls**. The lower-level payload still
  supports historical zero-point partials; current UI policy prevents uploading
  a newly empty scan. Physical PWM pins stayed low.
  Evidence:
  triggered RAM diagnostic (`output/smoke-test/ram-triggered-native-20261102.log`, local artifact).

The subsequent **physical open-output calibration passed with C5 installed**,
using the actual INA226s and LM358 drivers at **64 hardware averages** and
**three triggered blocks per calibration point**. The 10-second zero-drive
precheck passed, with drain/gate terminal spans of 3.755 mV / 5.012 mV.
The full UI calibration retained **160 drain points and 184 gate points**,
saved successfully, and preserved the drain-shunt calibration provenance.
It persisted the selected count of 64; other settings remained unchanged.
The diagnostic reported **234,143 ms elapsed**, including setup and the
zero-drive precheck, and **37,648 bytes free after collection**. Its lowest
periodically observed free-heap value was 4,688 bytes. Both PWM pins finished
low; the helper did not soft-reset.

| Channel | Measured terminal-to-GND coverage | Coverage minus entered 3.300 V source | Ceiling reason |
| --- | --- | --- | --- |
| Drain | 3.362505–18.181252 V | +0.062505–14.881252 V | Measured plateau |
| Gate | 0.008748–17.256230 V | −3.291252–13.956230 V | Configured drive ceiling |

The source rail was manually configured, not independently measured. These
unloaded endpoints establish the saved calibration's coverage, not loaded
output capability, current accuracy or stability across a reboot. Initial
scan verification used the 20 mV drain-floor guard; the subsequent separate
100 mV parking change does not extend baseline coverage. Evidence:
physical C5/64-average calibration (`output/smoke-test/source33-triggered-calibration-64-C5-20261102.log`, local artifact).

A **nine-point physical open-output scan, real TLS upload, exact
acknowledgement and automatic reboot to Home passed** with the 64-average
build. The RAM-only grid used VDS targets 0.1/0.6/1.1 V and VGS targets
−1/0/+1 V, deliberately excluding zero VDS. The receiver's nine rows,
acquisition metadata, drain curves and gate-leakage chart were independently
checked against the device log. Evidence:
native scan/upload/reboot (`output/smoke-test/ram-triggered-live-C5-20261102.log`, local artifact)
and spreadsheet verification (`output/smoke-test/ram-triggered-sheets-verification-20261103.json`, local artifact).

With the separate 100 mV parking guard, the **16-point open-output residual
grid passed** across VDS targets **0.1/1/5/10 V** and VGS targets
**−3/0/+3/+10 V**. Maximum absolute residuals were **3.638 µA drain and
0.465 µA gate**, with outputs low on completion. Each recorded point is one
64-average hardware block, so per-point standard deviation is unavailable.
The final build's exact-boundary refinement also passed a one-point check
at requested VDS 0.1 V / VGS +10 V, measuring VDS **0.063755 V** and VGS
**10.025007 V**, then finishing with outputs low. These open-output checks
do not establish loaded accuracy or zero-VDS coverage. Evidence:
complete residual grid (`output/smoke-test/source33-residuals-100mV-64-C5-20261102.log`, local artifact)
and final-build high-gate confirmation (`output/smoke-test/source33-final-100mV-highgate-20261102.log`, local artifact).

Before that parking change, the wider residual test stopped after 12 points
when the 20 mV floor check rejected the transition to VGS +10 V. A separate
raw diagnostic held drain PWM at zero while changing gate voltage: readings spanned
**35.000 mV**, then returned close to their initial level with gate off.
Subtracting the saved −5 µV floor baseline from each state's mean raw shunt
reading gives a maximum **1.743 µA** drain residual. That last calculation
is diagnostic constant-floor subtraction, not permission to extrapolate
the production baseline. The user selected a 0.1 V drain start for now and
deferred cross-gate zero-VDS baseline calibration; the passing residual grid
deliberately excluded zero VDS. See the
residual log (`output/smoke-test/source33-triggered-residuals-64-C5-20261102.log`, local artifact)
and zero-drive cross-gate diagnostic (`output/smoke-test/source33-zero-crossgate-C5-20261102.log`, local artifact).

At the end of the 2026-11-02 run, settings verification found exactly one change from the post-calibration
settings: **`drain_start_uv` changed from 0 to 100000**. All other ranges,
18.8 V driver rail, 3.3 V source, 200 ms settling delay and 64-average count
were retained. Calibration, Wi-Fi/cloud configuration and all ten historical
scan files retain their prior hashes. All **20 installed public firmware
files** match the verified build, and temporary diagnostic modules were
removed. With the final settings, startup reached the ready marker after
the first complete UI/input tick in **19.266 seconds**, then remained running
for the two-second observation without a fault or return to REPL; this last
startup check did not independently inspect the screen. See the
final settings, installation and preservation verification (`output/smoke-test/triggered-final-settings-preservation-verification-20261103.json`, local artifact)
and the earlier
physical evidence checks (`output/smoke-test/triggered-physical-verification-before-floor-20261103.json`, local artifact).

### Earlier software-averaging calibration checks

The following native memory and calibration checks exercised the preceding
software-averaging implementation. They establish the packed-storage behavior;
the current-build checks above supersede their simulated memory coverage,
while their physical calibration remains historical.
The modules were installed as bytecode. Native `import main` passed with
all fixed buffers reserved and **89,824 bytes free** after collection. The
source-only deployment had failed while compiling the UI (576-byte allocation);
precompiled modules remove that compiler peak without reducing buffers.

The native RAM diagnostic then passed all **4096 records**, 235,352 streamed
wire bytes with matching hashes, exact acknowledgement, discard, zero-point
partial results and four simulated upload failure paths. Its guard observed
zero filesystem accesses through the known production Python module APIs;
immutable native/C APIs and new local imports are outside that interception.
Independent before/after hashes confirm settings, calibration, credentials
and all ten historical scan files were unchanged. Both physical PWM pins
remained low. See `output/smoke-test/ram-native-bytecode-20261102.log` and
`source33-post-ram-manifest-20261102.log`.

That RAM diagnostic reserved the production display pool but did not instantiate
the display/inputs or perform a real network transfer.

A subsequent native calibration stress test instantiated the production
display, inputs and UI with a synthetic instrument, retaining all **398 points
(199 per channel)** at the 18.8 V grid. It completed **500 readings per point**,
**1,990 live chart paints**, full streamed save serialization into a non-writing
sink, and matching calibration fingerprints. After collection, **41,728 bytes
remained free with both completed tables retained**. The run took 827,960 ms;
both physical PWM pins stayed low, and settings/calibration files were untouched.
This verifies the packed acquisition and serialization lifecycle under the
full UI workload, including the stage where the previous dictionary-based
calibration exhausted a contiguous allocation. Evidence:
`output/smoke-test/calibration-packed-full-stress-20261102.log`.

The subsequent **physical open-output calibration passed** with the actual
INA226s and LM358 drivers: **10 batches of 10 readings per point**, retaining
161 drain points and 182 gate points. It saved successfully in **446,083 ms**
(about 7 minutes 26 seconds), with **42,400 bytes free after collection**.
The drain-shunt calibration provenance survived replacement, saved user
averaging stayed at 2 batches × 2 readings, and both PWM pins finished low.

| Channel | Measured terminal-to-GND coverage | Coverage minus entered 3.300 V source | Ceiling reason |
| --- | --- | --- | --- |
| Drain | 3.286731–18.219662 V | −0.013269–14.919662 V | Measured plateau |
| Gate | 0.021496–17.108904 V | −3.278504–13.808904 V | Configured drive ceiling |

The relative ranges are calculated from the manually entered source voltage;
the source rail was not independently measured by the scanner. These are
unloaded calibration endpoints, not guarantees of loaded output capability
or current accuracy. Independent residual checks, live compact TLS upload
and physical reset behavior still need verification. Evidence and the saved
calibration are `output/smoke-test/source33-packed-calibration-100-20261102.log`
and `source33-packed-calibration-100-20261102.json`; the JSON SHA-256 is
`a9465ca7f2b4363d2e862697a9c1df84121f77496280a860829c460bd60b5cf6`.

A fresh native bounded reload validated both tables and preserved the source,
averaging settings and shunt provenance. Its runtime fingerprint matches the
captured canonical serialization, while differing from the saved file's byte
hash: MicroPython's JSON float round trip changed 33 numeric fields in their
least-significant digits. The largest terminal change was 0.2 µV; the largest
baseline change corresponds to less than 0.000001 µA. Metadata, table lengths,
duties and endpoint support were unchanged. This verifies serialization, not
measurement accuracy. See
`output/smoke-test/source33-packed-roundtrip-verification-20261102.json`.

Display lookup metadata also has fixed capacity: 95 printable-ASCII glyph
slots and 96 raster-key slots. Raster lookup searches those slots instead of
growing a dictionary during acquisition. This removes a late 776-byte map
resize exposed by the binary-calibration memory test. Pixel buffers and FIFO
replacement are unchanged; failed raster generation leaves its slot invalid
so error rendering can retry safely.

The current boot layout reserves 68 KiB for scan records, a 48 KiB display
raster pool, a 96 KiB contiguous TLS region and the bounded acknowledgement
buffer before large application imports. There are no separate 44 KiB plot
arrays. The one-shot transport does not re-reserve its TLS region after upload;
the subsequent restart reconstructs the boot layout. MicroPython's heap does
not compact, so native testing must check contiguous allocations as well as
total free memory, including the largest supported calibration and 4096 points.

`prototype/render_firmware_ui.py` still renders the legacy UI fixture screens;
its curves are simulated. The new RAM-flow layout was separately checked with
a host rendering. Scripts named `firmware_*test.py` under `prototype` are device
diagnostics; a drive test requires an explicit, appropriate fixture. Old native
helpers may exercise the retained legacy App mode rather than the new main path.

## Historical implementation and verification records

The following memory, deployment and bench results concern prior persistent-
archive firmware. They explain earlier constraints and preserve their evidence;
they do not verify the current RAM-only workflow or authorize a hardware test.

In the legacy archive path, the calibration fingerprint was streamed when the
UI loaded its calibration and refreshed on replacement. Its cache retained the
same calibration object; in-place edits required `invalidate_calibration()`.
Each scan checked queue capacity and free space, walking acknowledged files
for reclamation only when space was insufficient. Preparation collected
temporary drawing objects after painting its screen and before archive open.
Archive headers were counted and streamed through a 256-byte buffer without
allocating a complete string and UTF-8 copy. This addressed a measured
1,304-byte contiguous-allocation failure on the RP2 heap.

The display reserves a 48 KiB raster pool and the UI reserves **44 KiB** for
4096 plot points: 32 KiB for measured VDS/current, 8 KiB for curve indices and
4 KiB for voltage-limit flags. Full results stream to the binary archive.
Boot allocates the three plot buffers and the display raster pool immediately
after the TLS/acknowledgement buffers and before the large application imports.
`App` and `Display` receive the existing bytearrays without copying them;
display hardware initialization still occurs after imports. Allocating the
32 KiB plot array or 48 KiB raster pool later in their constructors failed
after firmware growth fragmented the remaining heap.
These buffers are reused. MicroPython's heap does
not compact: abundant free RAM does not guarantee a large contiguous block
after a long acquisition. TLS testing exposed this even with more than
200 KiB free. Boot reserves a contiguous 96 KiB region before allocating the
UI and preallocates a 4 KiB acknowledgement buffer. Upload setup retains its
small objects while the reserve is held, then releases it for TLS. After a
response, the transport copies the bounded acknowledgement, closes network
objects, restores the reserve and only then decodes JSON.

On the tested MicroPython build, restoration attempted solely inside the
deeper upload call path failed even after collection. An outer `main.py`
loop reservation attempt before `App.tick` made repeated uploads succeed.
Preserve that tested ordering; the precise reason for the different allocation
outcomes has not been established. Memory failure keeps scans queued and
does not relax certificate checks. The HTTP parser also uses bounded tail
copies because native MicroPython rejected bytearray item/slice deletion.
Clock helpers are imported once; repeated imports measured about 4 ms each
on this firmware build.

Idle work must also avoid retaining fresh objects while the TLS region is
released. `Engine.tick` reuses its existing empty event queue when quiescent;
nonempty events still detach and deliver once. Following the partial-scan
update, reserve restoration failed; with this idle allocation removed, the
native retry acknowledged the exact ID/hash/count and restored all 98,304
bytes. This demonstrates the tested fix, without proving that one retained
object was the sole cause of the earlier heap layout failure. Evidence:
`output/smoke-test/partial-upload-idle-fix-20261102.log`.

### Drain shunt calibration, 2026-11-02

After replacement with a nominal 1 Ω part, two loaded up/down sweeps using the
user's 9.98 kΩ reference and a fresh open baseline fitted 0.789508 Ω and
0.789208 Ω. The combined effective scale is 0.789358 Ω, rounded to 0.789 Ω
in both `model.py` and the saved calibration. The free fitted intercept is
diagnostic only; it is not subtracted from measured current or folded into
the open baseline. Regression scatter does not include systematic uncertainty.

A 48-point drain baseline reaches 3.6347 V. The original gate calibration and
scan settings were preserved. Seven subsequent normal-engine points from
0.5–3.5 V agreed with measured VDS / 9980 Ω within 1.542 µA, using 10 batches
of 50 samples per point. The 5 mA drain trip is unchanged and uses the new
scale. All 294 host tests pass, all 15 installed public files match the
workspace, and normal UI startup passed. No calibration test was archived
or uploaded. Evidence: `output/smoke-test/drain-shunt-calibration-20261102.json`.

### Partial-scan verification, 2026-11-02

All 294 host tests and 30 Apps Script tests pass. The Pico exercised synthetic
drain saturation through the real decoder, engine, UI and archive with both
physical outputs held low. Completed points survived; unfinished averages did
not. OK accepted one-point and zero-point partial scans, CANCEL discarded only
the unaccepted draft, and no scan was queued before OK.

The labeled synthetic one-point scan reached the private workbook with its
fault diagnostic and two charts containing only the valid point. Its initial
acknowledgement and two consecutive exact replays passed ID/hash/count checks,
restoring the 96 KiB TLS reserve each time. Original scans were preserved;
the final queue has zero pending and four retained scans. Binary export kept
the fault in a companion metadata file. All 15 deployed public files match
the local sources, settings and calibration hashes are unchanged, and normal
UI startup passed. No real DUT overload was induced. Evidence:
`output/smoke-test/firmware-partial-verification-20261102.json`.

### Bench verification, 2026-11-01

- The initial cloud release passed 239 Python tests and 16 Apps Script receiver tests.
  The storage/chart update passes 269 Python tests and 24 receiver tests. Native
  storage tests exercised individual confirmations, repeated capacity checks,
  preservation of unselected scans and returning Home without acquisition.
  A full-UI replay of an existing scan passed verified TLS and exact ID/hash/count
  acknowledgement with the 96 KiB TLS reserve restored. No real scan was deleted.
  See `output/smoke-test/firmware-storage-charts-timing-verification-20261101.json`.
- At the earlier bring-up checkpoint, 163 host tests passed. All ten then-deployed Python files matched their local
  SHA-256 hashes, and the installed `main.py` booted successfully on COM20,
  USB serial `3B8922C9B91BBE9F`.
- Both channels completed 41-point calibration with 500 readings per point,
  ending at about 3.01 V. The bring-up run took 192 seconds.
- A real 25-point open-output scan completed in 137 seconds. Maximum absolute
  corrected readings were 0.0261 µA drain and 0.0116 µA gate. These are
  open-output residuals, not a loaded-current accuracy specification.
- With the final fixed buffers and lazy sweep setup, a further 500-reading
  point completed. Cancelling a subsequent scan took drain/gate from about
  1 V to 6.3/3.8 mV after settling. Cancelling calibration preserved the saved
  calibration, and streaming calibration persistence succeeded after scanning.
- On-device storage stress filled and reused all 4096 plot points three times.
  Display orientation/readability and the SETTINGS/OK touch targets were
  confirmed by the user; physical button ordering remains a manual check.
- The user found an unsupported `str.capitalize()` call on the invalid-voltage
  error path. Explicit channel labels now produce the intended range notice.
  Both drain/gate invalid ranges were tested on MicroPython, including return
  to Settings and preservation of saved values. Twenty SETTINGS/OK cycles
  followed by a one-minute idle test passed with no touch errors.
- A later near-zero coverage fault was fixed with the zero-drive-only baseline
  exception described above. Native MicroPython verified the reported 1.3 mV
  case and rejection when zero-drive permission is absent. With the user's
  2N7000G attached, a 20-point scan (VDS 0–2 V, VGS 0–1.5 V) completed in
  118 seconds, handling 487 drain readings below the former lower boundary.
  The VGS approximately 1.51 V curve reached 0.233 mA. A preceding scan to
  VGS 2 V correctly tripped the unchanged 5 mA drain limit; the DUT was
  connected, so those results are not open-output residual measurements.
- Expanded ceiling discovery completed with the DUT removed: 48 drain and
  47 gate calibration points, each using 500 readings. The saved endpoints
  were 3.638 V drain and 3.505 V gate; calibration took 249 seconds. Observed
  ceiling ranges were 3.624–3.645 V and 3.489–3.512 V respectively. A preceding
  fixed-duty probe showed stable means with roughly 20 mV peak-to-peak raw
  variation, motivating averaged settling checks and measured endpoint support.
- A subsequent open-output scan requested 0, 2.5 and 5 V on both axes.
  All nine points completed in 60 seconds; the five points containing a
  5 V request retained the correct channel limit flags. Measured ceilings
  were approximately 3.639 V drain and 3.505 V gate. Maximum absolute corrected
  open-output residuals were 0.0126 µA drain and 0.0114 µA gate, not an accuracy
  specification. Saved settings/calibration were preserved, no new error log
  was generated, and idle outputs returned to about 5.0/1.2 mV.
- The Averaging controls were tested on the Pico without driving either
  output: 3 batches × 7 samples saved and reloaded as integers, nested Cancel
  discarded edits, and out-of-range counts were rejected. The exact original
  settings bytes were restored afterward; calibration and the error log were
  preserved. Host tests also verify that the saved counts reach the engine.
- Wi-Fi discovery, join startup, cancellation and radio shutdown passed on
  the Pico. A synthetic 21-profile test verified the 20-profile limit, saved
  reuse, MRU ordering, reload and preservation after a failed replacement.
  Native touch-routing checks covered network and saved-list pagination,
  password keyboard modes, reveal/hide and Cancel without driving outputs.
  The updated UI booted successfully; settings and calibration hashes stayed
  unchanged. The user confirmed a real connection; a fresh MicroPython run
  reloaded the saved profiles and joined in 1.1 seconds. Deactivating both
  radio interfaces and resuming reconnected in 2.6 seconds, with PWM pins
  held low throughout. Details are
  in `firmware-wifi-verification-20261101.json` and the Wi-Fi diagnostic logs;
  these reports exclude credentials.
- Binary archive verification passed on MicroPython with synthetic two-point
  data and the real calibration object: 951 bytes locally and 1,400 JSON wire
  bytes, identical wire hash after reopening, cancelled-draft preservation,
  and mock-acknowledgement queue completion. The isolated test files were
  removed and outputs stayed disabled. See `firmware-archive-20261101.log`.
  Host tests cover 4096-point binary storage, malformed/corrupt files, queue and
  space limits, write failures, acknowledgement mismatches and stable retries.
- The native 4096-point synthetic stress test also passed: 222,031 bytes in
  the binary file and 568,237 streamed JSON bytes, with matching hash after
  reopening. The stress portion took about 205 seconds; files were isolated
  and removed afterward, with outputs disabled and no network used. See
  `firmware-archive-stress-20261101.log`.
- Native Cloud UI checks verified save-only-on-OK, Cancel preserving the prior
  queued scan, masked URL editing, configuration persistence and reuse of fixed
  plot buffers. These used synthetic data and no real upload credentials or
  network. See `firmware-cloud-ui-20261101.log`.
- The UI-resident TLS probe synchronized UTC, established certificate-verified
  connections to `script.google.com` and `script.googleusercontent.com`, and
  rejected a mismatched hostname with outputs held at zero. Earlier CA-load
  allocation failures motivated the reduced plot buffer and contiguous TLS
  reserve. See `firmware-cloud-tls-20261101.log`.
- Native HTTP parser checks passed fragmented Content-Length and chunked
  responses, the allowed response redirect and HTML rejection. See
  `firmware-http-parser-20261101.log`.
- Two real two-point bench scans reached the deployed private workbook and
  their voltage/current cells were checked in the browser. Subsequent identical
  retries created no extra tabs. Four consecutive full-UI upload cycles verified
  ID, point-count and body-hash acknowledgements, ending with no queued scans
  and the 96 KiB reserve restored. See `firmware-cloud-repeat-20261101.log`.
  The second bench scan's `duration_ms` was generated incorrectly by its
  diagnostic harness and is annotated in the sheet; do not use that field as
  an elapsed-time measurement. Its measurement cells were verified separately.
- A third fresh two-point scan was acquired and saved only after OK, with
  outputs parked and settings/calibration preserved; its recorded duration is
  4,398 ms. See `firmware-cloud-final-scan-20261101.log`. The normal `main.py`
  boot then uploaded it automatically. Browser inspection verified the exact
  measurement values, duration and complete Index entry, with exactly three
  scan entries/data tabs in the workbook after all repeated uploads.
- Final device checks found no pending scans, three retained local scans,
  automatic upload enabled and the 98,304-byte TLS reserve restored. All 15
  deployed public files matched their local hashes, calibration was unchanged,
  and normal UI startup was verified and left running. Settings and calibration
  were preserved within each real scan test. The final two-point archive also
  exported successfully to `final-scan.csv`; the host retry check rejects
  cross-runtime JSON spelling differences before posting. See
  `firmware-cloud-verification-20261101.json`.

Detailed results and calibration backups are under `output/smoke-test`,
including `firmware-validation-20261101.json` and
`firmware-ceiling-verification-20261101.json`, plus
`firmware-boot-20261101.json`. The UI is installed for automatic startup;
scan and calibration still require explicit user actions.

The 2026-11-02 scan-start update passed 314 host tests and a native test with
the full UI buffers, the real saved settings/calibration, and isolated copies
of all seven saved scans. Two starts began drawing Preparing within 1.6–1.9 ms
and reached the first logical drive at 1.66–1.69 s, after the scan screen was
painted. The earlier firmware waited 1.70 s before beginning its scan-screen
draw. These tests held both physical outputs low; they do not time a real
transistor measurement. CANCEL passed at all three preparation stages,
partial-result acceptance/discard passed, and an identical replay of an
already uploaded scan received a matching acknowledgement with the 96 KiB
TLS reserve restored. All 15 public device files matched their local hashes;
settings, calibration and all seven accepted scans were preserved.

An initial normal boot stopped safely with I2C EIO because drain address 0x44
was absent. After the user reseated its connections, both INA226s at 0x41 and
0x44 passed identity/configuration checks. Normal startup then verified both
sensors and the touchscreen, and the updated UI was left running. See
`output/smoke-test/firmware-startup-verification-20261102.json` for the timing,
compatibility checks and resolved hardware check.

The subsequent actual-voltage scan update passed 322 host tests. An eight-point
native simulation with the full UI and fixed buffers retained both gate
undershoot and overshoot, saved their actual values, issued no target-correction
commands, and drew only the three expected in-band line segments on a full
redraw. Physical outputs stayed low and all 12 existing scan files plus settings
and calibration were preserved. A separate native overrange test retained the
partial-result save/discard behavior; its verifier streams the payload to avoid
a diagnostic-only full-JSON allocation. All 15 installed public file hashes
matched, both sensors and touchscreen passed normal startup, and the updated
UI was left running. See
`output/smoke-test/firmware-actual-target-verification-20261102.json`.
