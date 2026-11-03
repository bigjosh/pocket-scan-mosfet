# Google Sheets scan receiver

This Apps Script accepts one finished pocket-scanner scan per HTTPS POST and
stores it in a private Google spreadsheet. Each scan has a data tab, normally two charts,
and one row in `Index`, including a link to that tab. There is no public
scan-reading endpoint. The device's upload URL is its only credential for this first pass.

`Code.gs` is ready for local testing and deployment. No endpoint or Google
credentials need to be embedded in the source. Keep each installation's private
configuration out of version control.

The schema 3 firmware holds one scan in RAM, using 17 bytes per point for four
float32 measurements and one flags byte, then streams JSON on upload. A completed
scan proceeds automatically to upload and soft-restarts after a matching
acknowledgement. **CANCEL discards and restarts** during acquisition or upload.
There is no offline scan queue or scan-data flash write. After a Wi-Fi/upload
error, OK or CANCEL discards the RAM scan and restarts the scanner; reconnect
from Settings and redo the scan.
Settings, calibration and connection configuration remain persistent.

The receiver also accepts historical schema 1/2 uploads and preserves their
original hashes, rows and chart layout. See the
[firmware guide](../../firmware/README.md) for device controls, the public CA
bundle, and historical binary archive retrieval.
Bench verification used a private workbook. Create a separate workbook for
your installation using the steps below.
Real bench scans have passed the POST, redirect and matching-acknowledgement
path with the Pico's full UI resident. Their stored measurements were checked
in the browser, and repeated identical uploads created no duplicate scan tabs.
The submission URL remains private and is not included here.

## Create and deploy

1. Create a Google spreadsheet. Leave its sharing private, or share it only with
   the people who should view the scans.
2. Open **Extensions → Apps Script**. Replace the initial `Code.gs` contents with
   this folder's `Code.gs`, then save.
3. Select `setupReceiver` in the script editor and run it manually. Authorize the
   script under the spreadsheet owner's account after reviewing Google's
   permission request. Google may request access to all Sheets even though this
   receiver uses only its configured workbook. This creates `Index` and stores
   the workbook ID in the script property `POCKET_SCAN_SPREADSHEET_ID`.
4. Choose **Deploy → New deployment → Web app**. Set **Execute as: Me** and allow
   access by **Anyone**, including users who are not signed in. In the manifest
   these settings are `USER_DEPLOYING` and `ANYONE_ANONYMOUS`. Workspace policy may
   prevent anonymous deployment; this must be checked on the actual account.
5. Copy the production URL ending in `/exec` into the scanner's cloud setting.
   The `/dev` test URL requires script-editor access and is unsuitable for the
   Pico. Treat the `/exec` URL as a secret; do not commit it, print it in device
   logs, or include it in screenshots.
   On the Pico, configure the published URL in **Settings → Advanced → Cloud**
   and commit the configuration. The current firmware uploads the RAM scan
   automatically after acquisition; it does not queue failed uploads for later retry.
6. For a code update, edit the existing deployment to use a new version, retaining
   its URL. To revoke a leaked URL, disable that deployment and configure a new
   deployment URL on the scanner.

The receiver always opens the configured workbook with `openById`. It never
accepts a destination workbook ID or an operation name from a request. Although
the script is bound to the workbook, active-document methods are unavailable
when it runs as a web app. `setupReceiver` is deliberately a manual operation.
An unrelated existing sheet named `Index` is rejected rather than overwritten.
Other pre-existing tabs are left alone.

Google documents the [deployment identity/access settings](https://developers.google.com/apps-script/manifest/web-app-api-executable),
[web-app request body and production/test URLs](https://developers.google.com/apps-script/guides/web),
and [bound-script restrictions](https://developers.google.com/apps-script/guides/bound).

## Access model

The workbook remains private while the anonymous web app runs with its owner's
permission to write it. Possession of the URL permits a caller to submit scans;
the URL does not identify or authenticate an individual scanner. This is the
user-selected prototype access model. The handler exposes only validated scan
creation and idempotent retries, with no scan listing, download, arbitrary cell
editing, or delete operation. `GET` returns fixed service health only.

Keep the deployment URL private and restrict who can edit the bound script.
Incoming requests, raw exceptions and URLs are never logged or echoed. A public
URL can still be used to submit bogus well-formed scans or consume the owner's
quotas. If needed later, a separately revocable device token can be checked in
the JSON body without changing workbook sharing or adding Google login on the
Pico. No Google account or service key is stored on the device.
The submitted device ID, calibration fingerprint and measurement values are
caller-provided data, not proof of the sender's identity or measurement accuracy.

## Scan charts

Each completed scan gets two plots to the right of its raw data, starting
at column S:

- **Drain current vs VDS:** measured terminal VDS on the horizontal axis and
  baseline-corrected drain current in mA on the vertical axis. Each gate sweep
  has a separate colored series. Legend labels identify its requested VGS;
  the actual measured VGS remains in the data table. Every measured drain point
  retains its own X coordinate, including repeated or voltage-limited points.
  For schema 3, a point joins its gate curve only when its retained
  `gate_off_target` flag is false. The Pico computes this flag from measured
  VGS and the inclusive **±100 mV** band around requested VGS; the receiver does
  not reclassify float32 boundary decisions using JavaScript float64. Legacy
  schema 1/2 charts retain their original ±20 mV measured-VGS band formulas.
  Previously uploaded schema 3 scans retain their recorded classification;
  the wider band applies to scans acquired with the updated firmware and needs
  no receiver redeployment.
  Off-target points appear as isolated
  amber squares, with no incoming line, outgoing line, or bridge across the gap.
  A drain-voltage target miss alone does not break the curve: the horizontal
  coordinate is the measured VDS.
- **Maximum |Ig| vs VGS:** one point per gate sweep, using the largest absolute
  baseline-corrected gate current across that drain sweep, in microamps. Its X
  coordinate is the measured VGS at that same point. A tie selects the first
  recorded point. This displays either leakage polarity as a positive magnitude;
  signed currents remain in the raw table.

The drain chart shows up to 16 gate curves at once. Change the whole-number page
in **U1** to see subsequent curves. All gate sweeps remain accessible; paging
bounds both chart clutter and spreadsheet size for a 4096-point scan. A shared
marker-only series shows the off-target points on that page. The drain plot uses
a line chart with a continuous numeric X axis and null interpolation disabled;
[Google Sheets supports these gaps for line charts](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/charts#BasicChartSpec).
The leakage scatter chart includes all gate sweeps. Visible formula helpers in
**AG:BG** reference the raw values rather than duplicating calculated results.
Raw data stays in **A:K** for schema 3 and **A:Q** for legacy schemas 1/2.

No bulk chart migration is needed for schema 3. Run **addScanCharts** in the
Apps Script editor only to add missing charts or retry interrupted creation.
It recognizes each tab's complete column contract and reads that table's width;
existing schema 1/2 data, formulas and completed chart IDs remain unchanged.
It is an owner/editor action, not a web API. Existing measurement cells, checksums,
Index rows and their annotations are preserved. An occupied presentation region
is rejected rather than overwritten; unrelated charts are preserved. Upgrading
older charts also checks the newly used **BG** column for owner values, formulas,
and notes before making changes. Clear or relocate that content yourself before
retrying if the area is occupied. A valid page selection is preserved.

Charts are created after the measurements are committed. If Google's chart service
fails, the scan still receives its normal acknowledgement; its data is safe.
The owned chart area shows **Needs retry: run addScanCharts** when possible.
Re-running the helper or retrying the same upload repairs incomplete charts
without creating duplicate scans or charts. Successfully completed retries are
read-only.

## Request and acknowledgement

Send `Content-Type: application/json` with a UTF-8 JSON body no larger than
**2,097,152 bytes (2 MiB)**. The required top-level fields are:

| Field | Value |
| --- | --- |
| `schema_version` | Integer `3` for compact RAM scans; legacy `1` and `2` remain accepted unchanged |
| `scan_id` | Stable `hex-hex` identifier, at most 96 characters |
| `device_id` | Nonempty string, at most 64 characters |
| `created_utc` | ISO UTC ending in `Z`, optionally fractional seconds, or `null` |
| `settings` | Snapshot including `version: 3` for triggered INA226 acquisition; schema 3 also accepts historical version 2, and schemas 1/2 accept historical versions 1/2 |
| `acquisition` | Required only with settings version 3: fixed conversion protocol, hardware averaging count, one block per scan point and unavailable point variance |
| `calibration` | `{ "sha256": "64 hex digits", "metadata": { ... } }` |
| `columns` | Exact ordered column list below |
| `points` | One array per completed averaged point, up to 4096 points |
| `outcome` | `"complete"` or, with schema 2/3, `"partial"` |
| `point_count` | Integer equal to the number of point arrays |
| `duration_ms` | Nonnegative safe integer |
| `fault` | Schema 2/3: `null` for complete scans; the overrange diagnostic for partial scans |

An ADC saturation or software current trip stops both outputs. A partial scan
contains only fully averaged points completed before that stop. Its interrupted
point is never represented as a numeric maximum, infinity, or fabricated current
or VDS. The current firmware automatically uploads a partial scan when it has
at least one completed point. CANCEL discards the local data and restarts.
A stop before the first point shows a device error instead of uploading;
OK or CANCEL returns to Home through a restart. For historical clients and
diagnostic tools, the receiver still accepts `point_count: 0`, an empty point
array and a partial-scan fault. A complete scan must contain at least one point.

The `fault` object has exactly these fields:

- `channel`: `drain` or `gate`.
- `kind`: `shunt_saturation`, `bus_saturation`, `current_limit`, or
  `current_limit_alert` (settings version 3).
- `raw_code`: the ADC saturation code, or `null` for a current trip/alert.
- `sign`: `1` or `-1`; `limit_value` is the positive magnitude of the bound,
  and `limit_unit` is `V` for ADC saturation or `A` for a current trip.
- `measured_value`: `null` for saturation or a latched hardware current alert;
  otherwise the signed raw current that exceeded the software limit. An alert
  proves that its threshold was crossed but does not measure the transient peak.
  These diagnostics are not averaged points.
- `index`, `curve`, `requested_vds`, `requested_vgs`, and `phase`: the interrupted
  scan position. `index` equals the number of completed points.

The tab and Index both identify `outcome: partial`; **B14** preserves the fault
JSON and a visible banner above the plots identifies the interrupted target.
Plots use retained measurements only. Leakage maxima on partial scans cover
the recorded points, not the unmeasured remainder of the sweep. A zero-point
diagnostic gets no empty or invented chart. The sheet's internal receipt state
may still say `complete`: that means its upload was committed, not that the
requested voltage sweep finished.

Schema 3 has exactly these 11 columns:

```text
index, curve, requested_vds, requested_vgs, vds, vgs,
id_a, ig_a, drain_voltage_limited, gate_voltage_limited, gate_off_target
```

The first four fields derive from row position and the integer settings. The
four measurements retain float32 volts/amperes. The last three fields are JSON
booleans decoded from RAM flag bits 0, 1 and 2 respectively. `gate_off_target`
controls chart line gaps independently of the two voltage-limit flags. Raw
shunt voltages, current/shunt deviations and per-point batch counts are absent;
the receiver never fills those omitted fields with zeros. Averaging parameters
remain in the settings snapshot.

Historical schemas 1/2 retain exactly these 17 columns:

```text
index, curve, requested_vds, requested_vgs, vds, vgs,
id_a, ig_a, id_std_a, ig_std_a,
drain_shunt_v, gate_shunt_v, drain_shunt_std_v, gate_shunt_std_v,
batches_completed, drain_voltage_limited, gate_voltage_limited
```

Indices are zero-based and consecutive. Currents and their standard deviations
are in amperes; voltages and shunt standard deviations are in volts. `curve` and
`batches_completed` are integers; the last two columns are JSON booleans. Requested
and measured values remain separate, including at voltage-limited points.
Float32 storage/serialization rounding is accepted; the receiver does not demand
exact requested-grid floating-point equality.

New scans use **settings version 3**. The eight voltage settings are integer
microvolts: `drain_start_uv`, `drain_stop_uv`, `drain_step_uv`, `gate_start_uv`,
`gate_stop_uv`, `gate_step_uv`, `driver_supply_uv`, and `source_voltage_uv`.
Fractions, strings, booleans and mixed `_v`/`_uv` settings are rejected.
`ina_average_count` is one of **1, 4, 16, 64, 128, 256, 512, 1024**; the
default is 64. `settle_ms` is the settling interval, from 100 to 10,000 ms
for settings version 3. Historical settings versions 1 and 2 retain their
200 ms minimum. The former `batches`, `samples_per_batch` and `sample_pause_ms`
fields are absent.
Each axis has exactly
`floor((stop_uv - start_uv) / step_uv) + 1` points; the total must not exceed 4096.
A complete scan must contain that exact count. A partial scan must stop before
the end, and its fault and rows must identify the correct integer curve position.
The measurement columns, including requested voltages, remain in **volts and
amperes**, and do not require exact float32 equality with the settings grid.

After settling, both INA226s are triggered and polled until their complete
hardware-averaged conversions are ready. Each bus and shunt conversion uses
1100 microseconds. A scan point retains one averaged block from each sensor.
Its top-level `acquisition` object is:

```json
{"acquisition_mode":"ina226_triggered_v1","ina_average_count":64,"bus_conversion_us":1100,"shunt_conversion_us":1100,"blocks_per_point":1,"point_variance":"unavailable"}
```

The count matches the settings snapshot. It describes a fresh conversion at
that point, not repeated reads of one continuous-conversion result. A single
hardware block has no measured between-block variance, so no standard deviation
is fabricated. The receiver stores this object in **D1:E1**, preserving existing
measurement and chart ranges. The calibration snapshot must identify the same
mode, averaging count and conversion times, plus `calibration_repeat_blocks: 3`.
Calibration measures three independent blocks per point to estimate baseline
spread. Earlier continuous-mode calibrations cannot be relabeled as this method;
the device requires a fresh open-output calibration before scanning.

Historical settings version 2 remains accepted with its original Pico batch
averaging fields and integer microvolt settings, without an acquisition header.
Historical settings version 1 remains accepted in its original voltage units.
It is checked for valid fields and the voltage envelope, but its planned count
is never recomputed: RP2 float32 and JavaScript float64 can disagree about how
many steps fit. The recorded row count remains bounded to 4096. Original settings
snapshots, request bytes and hashes are preserved, so old queued scans can retry.
The health response advertises `settings_versions: [1, 2, 3]` and
`scan_schema_versions: [1, 2, 3]`.

Each settings version uses its documented voltage, timing and averaging bounds.
Snapshot objects are bounded to depth 6, 2048 total nodes, 256 children per object
or array, 2048 characters per string, and 32768 serialized characters. Snapshots
are preserved as JSON text. Standalone metadata strings are escaped if they could
be interpreted as spreadsheet formulas. Point cells must be finite numbers with
broad integrity bounds: voltages ±36 V, currents/shunts ±1 in their units, and
standard deviations 0–1. These are upload validation limits, not hardware limits;
the firmware's much lower current trips remain responsible for acquisition.

Successful response:

```json
{"ok":true,"scan_id":"devicehex-randomhex","sha256":"64 lowercase hex digits","point_count":25,"sheet_id":123456789}
```

The example ID above illustrates the names of the two hex components; actual
IDs must contain hexadecimal digits only, separated by one hyphen.

`sha256` is the SHA-256 of the **exact original UTF-8 request body**, not a
reformatted JSON object. Retries must send the same serialized bytes. The Pico
must validate `ok`, `scan_id`, `sha256`, `point_count`, and a nonnegative integer
`sheet_id` before treating the scan as uploaded. A schema 3 client may release
its RAM results after this matching acknowledgement. An error response has the fixed shape:

```json
{"ok":false,"error":"receiver_busy"}
```

Historical archive firmware stores 54-byte binary point records with a metadata header and a
matching sidecar. Its uploader verifies binary integrity, emits float32 values
with nine significant decimal digits, and reconstructs the same JSON bytes for
retries on the tested Pico runtime. The sidecar's wire hash is distinct from the
binary-file hash. No permanent second JSON copy is needed on the device. When
copying a local scan, retain both the data file and sidecar. Use
`python prototype/export_scan.py saved_scan.sent output.csv` from the repository
root to export its measurements; keep `saved_scan.meta` beside the input.

CPython and RP2 MicroPython can format the same float32 value differently with
`%.9g`. CSV values remain valid, but JSON rebuilt on a host may not match the
stored wire hash and must not be posted under the existing scan ID.
`prototype/cloud_receiver_check.py --scan` compares that hash before any POST
and declines a mismatched reconstruction. The historical Pico queue is the
authoritative retry path for its accepted scans. RAM-only firmware does not
create or automatically replay this archive format. See the
[binary retrieval guide](../../firmware/README.md#local-binary-files-and-measurement-provenance).

Historical PMSC version 2 files use a bounded outcome/fault JSON trailer
and a count/duration/trailer-length footer. Existing PMSC version 1 files retain
their original wire bytes and acknowledgement hashes. Neither the binary record
layout nor the measurement columns changed. Settings version 2 is independent of
the scan schema and PMSC versions. Its integer plan is verified by both the
acquisition runtime and receiver; historical settings version 1 uses the
recorded count across float32/float64 rounding boundaries.

Validation/configuration errors include `invalid_request`, `invalid_json`,
`payload_too_large`, `invalid_scan`, `invalid_columns`, `invalid_point`, and
`receiver_not_configured`. `scan_conflict` means the ID already reserves different
body bytes. `index_conflict` or `storage_conflict` requires inspecting receiver
metadata. At the protocol level, `receiver_busy` and transient `storage_error`
can be retried using the exact same body and ID. The RAM-only firmware's chosen
failure policy does not queue or retry them. No exception text is returned.

Google's ContentService redirects its response to a one-time
`script.googleusercontent.com` URL. The client must follow the response redirect
and read the JSON acknowledgement. Do not forward the original POST body to the
response URL. A timeout or redirect failure may occur **after** the spreadsheet
write succeeds, so an HTTP status alone is not proof of upload completion.
[ContentService redirect documentation](https://developers.google.com/apps-script/guides/content).

**CANCEL is local only.** Closing the Pico's connection cannot undo a Sheets
write already performed by Apps Script. After cancellation or a missing
acknowledgement, a scan tab may already exist; the RAM-only firmware still
discards on the user's exit/reset action. Redoing a scan creates a new scan ID
and may therefore produce another tab. There is no remote delete/abort API.

## Interrupted uploads and duplicate prevention

A script lock serializes deduplication and writes across callers. The receiver
uses `getScriptLock`; document locks are unavailable in web-app execution.
[LockService documentation](https://developers.google.com/apps-script/reference/lock/lock-service).

1. Reserve a staging tab named `_psm_<scan-ID hash prefix>_<full body hash>`.
   The full body hash in its name also protects the reservation if execution
   stops immediately after tab creation, before the first metadata write.
2. Write metadata, the column header, and all points using batch `setValues`
   calls. Allocate only the rows and columns actually needed. Flush those writes
   before marking the tab `ready`.
3. Rename it to a readable UTC timestamp plus a short scan-ID hash. The full ID
   and body hash remain in metadata; an existing name never gets overwritten.
4. Write and flush its single `Index` row, then mark/flush `complete`. Add the
   derived charts and return the acknowledgement, even if chart creation fails.

There is no transaction spanning these Google service calls. Recovery is built
around the persistent staging name and state markers: a same-body retry rewrites
an incomplete stage, or finishes indexing an already-ready tab. A completed retry
returns the existing sheet ID without rewriting its data. Different bytes for
the same ID are rejected. The Index never advertises a partially written data
table. A tab left in `staging` or `ready` is recoverable by retrying the same local
scan; do not delete its reservation while that retry is pending.

Historical archive firmware used a bounded flash queue, delayed retries and
explicit Storage deletion. Schema 3 RAM firmware has no such queue: power loss,
discard and reset lose an unconfirmed in-memory scan. A workbook receipt is
established by a matching acknowledgement, not by RAM being cleared or Wi-Fi
being connected. Receiver idempotence remains available to compatible callers
and preserves all existing uploaded scan IDs/hashes.

Keep the first 15 rows and the raw data columns of managed scan tabs intact:
11 columns for schema 3, 17 for legacy schemas 1/2. The first
12 columns of `Index` may be reordered, provided each required header remains
present exactly once and its data moves with it. The receiver follows those
header names when reading or appending; missing, renamed or duplicate headers
are rejected. Owner-added Index columns after that region are preserved. The upload
hash records the original received data; it does not recalculate itself if a
person later edits cells. Preserve a spreadsheet export when original
measurements must remain reproducible after the RAM scan is released. Move to a new workbook per batch or period when navigation
becomes cumbersome, rather than filling one workbook indefinitely.

## Local tests

From the repository root:

```powershell
node --test tests/test_apps_script.js
```

The Node VM uses fake Apps Script APIs and synthetic scan data. It checks schema
rejection, numeric/boolean preservation, UTF-8 limits and hashing, formula-safe
metadata, identical/conflicting retries, concurrent entry, Index preservation,
and failures before and after every mutation/flush in an upload. It also covers
the 4096-point boundary and recovery from an empty reserved staging tab. These
tests do not access Google, the Pico, passwords, or real scan data. Chart tests
also cover measured X coordinates, signed leakage magnitudes and tied peaks,
4096 gate curves with paging, owner annotations, and repair without duplicates.
The receiver suite contains 53 tests, including reordered Index
headers, mapped appends, preservation of owner notes, partial and zero-point
diagnostics, fault validation, and recovery after every interrupted write.
Integer tests cover exact endpoints and the 4096-point boundary, invalid/mixed
units, complete/partial positions, and the native legacy 4092-point plan that
JavaScript previously miscounted as 4158.
Schema 3 tests cover the 11-column contract, authoritative device gate flags,
4096-point and zero-point cases, all interrupted upload mutations, mixed-schema
workbooks, unchanged legacy chart IDs/hashes, and bounded chartless-tab repair.
Triggered-acquisition tests cover every supported hardware averaging count,
calibration metadata binding, hardware current alerts without invented peaks,
the 100 ms settings-v3 settling boundary, and the unchanged 200 ms minimum for
historical settings.

## Live verification

The dated entries below refer to local evidence under `output/smoke-test`.
Those artifacts are not required for deployment and may be absent from a fresh
clone. Older entries describe the firmware and storage workflow of their date.

On 2026-11-03 at 00:36 Eastern, receiver version 9 was deployed at the
unchanged upload address. Settings version 3 now accepts `settle_ms` from
100 to 10,000 ms; historical settings versions 1 and 2 retain the 200 ms
minimum. All 53 receiver tests passed, including the boundary values and
unchanged chart/retry behavior. The published version's source matched local
`Code.gs` after newline normalization, and certificate-verified HTTPS health
passed. Existing access permissions were retained; deployment verification
did not upload scans or modify historical tabs. See
`output/smoke-test/receiver-settle-verification-20261103.json` and
`output/smoke-test/receiver-settle-version9-20261103.png`.

On 2026-11-02 at 22:44 Eastern (2026-11-03 UTC), receiver version 8 was
deployed at the unchanged upload address with settings version 3 and triggered
INA226 acquisition metadata support. The deployed version's source matched
local `Code.gs` after newline normalization. Certificate-verified HTTPS health
advertised settings versions 1, 2 and 3 and scan schemas 1, 2 and 3. All 52
receiver tests and 13 RAM payload tests passed, including legacy receipts,
new metadata binding, threshold alerts without invented peaks, and interrupted
upload recovery. Deployment retained existing identity/access permissions and
did not upload a scan, backfill charts or modify historical tabs. See
`output/smoke-test/receiver-triggered-verification-20261103.json` and
`output/smoke-test/receiver-triggered-version8-20261103.png`.
Native acquisition/upload verification remains a separate device check.

On 2026-11-02, receiver version 7 was deployed at the unchanged upload address
with compact scan schema 3 support. The saved editor source matched local
`Code.gs` after newline normalization. Certificate-verified HTTPS health
advertised scan schemas 1, 2 and 3 and settings versions 1 and 2. Existing
deployment identity/access settings were retained; no new authorization was
requested. No scan upload, chart backfill or scan-data rewrite was run during
this deployment verification. See
`output/smoke-test/receiver-schema3-verification-20261102.json` and the adjacent
version-7 screenshot, which omits the private upload address. Actual compact
scan upload verification is a separate device check.

On 2026-11-02, receiver version 6 was deployed at the unchanged upload address
with integer microvolt settings support. Its editor source matched local
`Code.gs` after newline normalization, and certificate-verified HTTPS health
advertised settings versions 1 and 2. The 41 host tests passed. Deployment
changed only receiver code; no chart backfill or scan-data rewrite was run.
See `output/smoke-test/receiver-microvolts-verification-20261102.json` and
`output/smoke-test/receiver-microvolts-tests-20261102.log`.

On 2026-11-02, receiver version 5 was deployed at the unchanged upload address.
All seven existing scan tabs were upgraded to chart layout version 4. An
owner-only audit compared raw values, formulas and notes plus the complete
Index before and after migration. They were unchanged, and a second backfill
preserved every managed chart ID. The zero-point partial scan retained its
diagnostic without creating a chart. The receiver passed certificate-verified
HTTPS health checking after deployment.

A temporary synthetic native Sheets chart verified both gate-miss directions,
isolated amber markers, no bridging across missing points, irregular measured
VDS spacing and vertical segments at repeated VDS. Its evaluated helper cells
matched the expected values. The temporary tab and test functions were removed
before deployment. The saved production editor source matched `Code.gs` exactly
after normalizing line endings. See
`output/smoke-test/sheets-gate-gap-verification-20261102.json` and the associated
migration and 36-test logs.

On 2026-11-02, receiver version 4 was deployed at the unchanged upload address.
The tab `TEST partial overrange 2026-11-02` contains one synthetic completed
point, an explicit partial banner and a separate saturation diagnostic. Both
charts exclude the fault point. The Pico verified the exact ID/hash/count
acknowledgement and two consecutive replays; browser inspection found no
duplicate tab or Index row. Three original scans and their owner annotations
were preserved. See `output/smoke-test/firmware-partial-verification-20261102.json`
and `output/smoke-test/browser-partial-verification-20261102.json`.

The chart update was deployed at the existing upload address. Chrome inspection
verified two rendered charts on each of the three existing scan tabs, with no
duplicate data tabs or charts and both diagnostic timing notes preserved. A
native exact-body replay passed after adding support for the owner's reordered
Index columns. Chart layout must remain automatic: custom `chartArea` dimensions
rendered blank interiors in native Sheets despite correctly evaluated source
formulas. See `output/smoke-test/browser-chart-verification-20261101.json` and
`output/smoke-test/firmware-storage-cloud-replay-20261101.log`.

The deployed private workbook contains three inspected real two-point bench
scans. Four consecutive full-application upload cycles completed verified TLS,
Google's response redirect and exact ID/hash/count acknowledgement checks;
replaying the same bodies produced no extra tabs. The evidence is in
`output/smoke-test/firmware-cloud-repeat-20261101.log` at the repository root.
Native tests also verified hostname rejection and a 4096-point binary archive
that produced 568,237 JSON bytes with an unchanged wire hash on reopening.

The second bench scan has a sheet annotation identifying an incorrect
`duration_ms` generated by its diagnostic harness. Its voltage/current data
were verified independently; that duration must not be treated as measured
scan elapsed time. The third fresh scan uploaded automatically through normal
`main.py` startup. Browser inspection verified its exact measurements, two-point
count, 4,398 ms duration and completed Index entry. Exactly three Index entries
and three scan data tabs remained after the idempotent retries.
The final device check found zero pending and three retained scans, with
automatic upload enabled and the 96 KiB TLS reserve restored. All 15 installed
public files matched their local hashes, and the UI was left running. The
final two-point archive was also exported to CSV. The consolidated evidence is
`output/smoke-test/firmware-cloud-verification-20261101.json`.
