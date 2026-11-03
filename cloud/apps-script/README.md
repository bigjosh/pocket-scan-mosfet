# Google Sheets scan receiver

Each HTTPS POST saves one scan to a new tab and adds an Index row plus two
charts. The spreadsheet stays private; possession of the production `/exec`
URL permits submissions. There is no scan-reading or deletion web endpoint.
Never commit or log that URL.

The current firmware uses **schema 5** when a compatible open-output current
baseline is applied, or **schema 4** for uncorrected readings. Both carry four
measured values per point and no target-quality classification. The scan stays
in 64 KiB of RAM, uploads automatically, then restarts. There is no offline
queue. Shunt-ADC saturation can upload completed partial results; zero-point
scans are not uploaded. Calibration is a separate Settings operation; a normal
scan performs no baseline file writes or reads.

## Create or update the receiver

1. Create a private Google Sheet. Open **Extensions → Apps Script**, replace
   `Code.gs` with [this file](Code.gs), and save.
2. On a new installation only, run `setupReceiver` and authorize it after
   reviewing Google's permission request. It creates Index and stores the
   bound workbook ID in the script property `POCKET_SCAN_SPREADSHEET_ID`.
   The web handler always opens that configured workbook.
3. Deploy a Web app: **Execute as Me**, access **Anyone**, including unsigned-in
   users. Workspace policy may restrict this. Use the production `/exec` URL,
   not the editor-only `/dev` URL.
4. Enter the URL on the Pico in **Settings → Advanced → Cloud**.
5. For updates, edit the existing deployment to use a **new version**, retaining
   its URL. Deploy schema-5 support before enabling corrected uploads on the Pico.

No new Google permissions are needed for the schema-5 update. Existing schemas
1–4 and their data/hashes/chart semantics remain supported. A foreign existing
sheet called Index is rejected rather than overwritten.

For automatic Index cleanup, run **`setupIndexMaintenance` once as the owner**
after saving the script. Authorize trigger management if Google requests it.
This reconciles existing rows and installs one change trigger for this workbook.
Re-running it is safe: duplicate matching triggers owned by the current user
are removed, while unrelated triggers are preserved. Reload the spreadsheet
to show **Pocket MOSFET → Refresh Index**.

See Google's [web app guide](https://developers.google.com/apps-script/guides/web)
and [deployment settings](https://developers.google.com/apps-script/manifest/web-app-api-executable).
The submission URL is a capability, not proof of device identity; anyone with
it can submit well-formed invented readings or consume quotas. Sheet sharing
controls who can read results. Keep the workbook and script editors restricted
to intended users.

## Schemas 4 and 5

POST UTF-8 JSON, `Content-Type: application/json`, at most 2 MiB.

| Field | Value |
|---|---|
| schema_version | 5 for baseline-corrected currents; 4 for raw currents |
| scan_id, device_id | Device-generated identifiers |
| created_utc | ISO UTC timestamp or null when capture time is unknown |
| settings | Version-3 integer-microvolt sweep settings |
| acquisition | Triggered INA226 mode, averaging count and conversion times |
| current_correction | Schema 5 only: version, baseline SHA-256, lookup policy and per-channel voltage ranges/zero anchors |
| columns | `["vds","vgs","id_a","ig_a"]` |
| points | Arrays of four finite measured values, in volts and amperes; schema 5 currents already have their open-output baseline subtracted |
| outcome | Complete or stopped, using the firmware's outcome token |
| fault | Null for complete; typed shunt-saturation fault for a stopped partial scan |
| point_count | Number of completed records, 1…4096 |
| duration_ms | Acquisition duration |

The exact accepted tokens and bounded fault contract are implemented in
`Code.gs` and exercised by `tests/test_apps_script.js`. Point count and grid
consistency, finite encoding and input structure are checked. Measured voltage
error and arbitrary magnitude bounds do not reject schema-4/5 points.

Schema 5's `current_correction` is a bounded provenance record, not the full
baseline table:

```text
version: 1
sha256: SHA-256 of the complete saved binary baseline, including its checksum trailer
lookup: "linear_endpoint_hold"
drain / gate:
  min_bus_uv, max_bus_uv
  zero_duty_u16, zero_residual_uv, zero_valid
```

Lookup interpolates the open-output shunt baseline against actual VBUS and
holds the nearest endpoint outside the measured range. Current is
`(raw_shunt_voltage - baseline_shunt_voltage) / shunt_resistance`. Voltages
remain derived from **raw** shunt drop, then subtract the configured source
voltage. The receiver stores supplied readings without subtracting the baseline
again. `zero_residual_uv` is terminal-to-source voltage at the saved zero PWM;
`zero_valid=false` means that closest measured value was not accepted as a zero
anchor. Settings contain the compatible source, supply and acquisition values.
Raw schema-4 scans must omit `current_correction`; corrected schema-5 scans must
provide it. This prevents historical raw data being relabeled as corrected.

The current firmware stops either current channel when its shunt readback is
**0x7fff or 0x8000**, the positive/negative ADC full-scale codes. This is about
±104 mA with the 0.789 Ω drain scale and ±128 µA with the 638 Ω gate scale;
there is no separate current threshold or programmed shunt comparator. The
interrupted full-scale point is not uploaded as a fabricated numeric maximum.
Bus-register full scale is recorded normally. Cancel, communication and
configuration errors, malformed ADC data and conversion timeouts retain their
existing abort behavior. The receiver still accepts earlier bus-saturation and
current-trip diagnostics; no receiver deployment is needed for this
stopping-policy change.

Index, curve number and requested VDS/VGS are derived from the integer settings
grid, including nondivisible final intervals. The table uses **A:H**:

```text
index | curve | requested_vds | requested_vgs | vds | vgs | id_a | ig_a
```

Metadata identifies `measurement_mode = uncorrected` for schema 4 or
`baseline_corrected` for schema 5, and records the actual schema version.
Schema-5 correction metadata is in D2:E2, beside acquisition metadata in D1:E1.
The historical Index calibration-hash column is blank for schema 4 and contains
the binary baseline hash for schema 5. No full calibration snapshot is retained
in the new payload.

The receiver acknowledges `ok`, `scan_id`, the SHA-256 of the exact submitted
bytes, `point_count`, and `sheet_id`. The Pico requires all of them to match.
Retrying the identical scan ID/body is idempotent; a conflicting body is
rejected. A partially written tab is not treated as a completed acknowledged
scan. HTTPS certificate checking and payload integrity are independent of
measurement-quality checks and remain enabled.

## Deleting scans

Delete a scan tab normally in Google Sheets. The installed change trigger
removes its Index row by matching the saved numeric `sheet_id` against the
tabs that still exist. Renaming or rearranging tabs does not remove entries.
Deleting the Index row also removes any notes on that deleted scan's row;
surviving rows, formulas, notes and owner-added columns stay aligned.

The handler waits up to 60 seconds for the same script lock used by uploads.
If automatic cleanup is missed, use **Pocket MOSFET → Refresh Index**; the
next valid upload also performs cleanup. Deletions made by scripts or APIs
do not fire Google's change trigger, so these recovery paths cover them too.
Cleanup never recreates a missing Index, clears an unrecognized row, or
rebuilds the sheet. It respects reordered required columns and refuses a
foreign or malformed Index header. Preserve each row's `scan_id`, `sha256`,
numeric `sheet_id` and `scan_url` for automatic recognition.

Deletion is not a permanent upload block: after its Index row is removed,
retrying the same scan can create a new tab. The current Pico keeps no queue
after a successful upload. A person can still delete a tab while an upload
is writing it; that upload may fail because script locks do not block manual
spreadsheet edits.

## Charts

Click **View charts** in **D3**, beside the scan metadata, to jump to the
charts on the same tab. Charts start at column S, with formula helpers in AG:BG:

- **Drain current vs VDS** uses actual measured VDS on X and signed drain
  current in mA on Y. Each requested gate sweep has a separate colored curve.
  Every schema-4/5 point connects within its curve, including off-target gate
  readings and repeated drain voltages. Actual VGS remains in the table.
- **Maximum |Ig| vs VGS** selects the largest absolute gate current in each
  drain sweep (µA). X is actual VGS at that same point; ties use the first
  recorded point. Signed values remain in the table. Chart annotations identify
  schema-4 currents as uncorrected and schema-5 currents as baseline-corrected.

The drain plot displays up to 16 gate curves per page. Change the whole-number
page in **U1** for later sweeps. The leakage chart includes all recorded sweeps.
A partial scan includes only completed measurements.

Chart creation follows data commit. If it fails, the scan can still receive its
normal acknowledgement; the chart area requests retry. The owner may run
`addScanCharts` to repair missing/incomplete charts. It preserves measurement
cells, hashes, Index rows, existing complete chart IDs, and unrelated content;
occupied presentation areas are rejected rather than overwritten.
It also adds the navigation link to existing managed tabs, even when their
charts are already complete. D3 is left untouched if it contains user text,
another formula or a note; in that case, scroll right to column S.

Schemas 1/2 retain their original ±20 mV gate classification. Schema 3 retains
its recorded gate-off-target flag and disconnected markers. Existing tabs are
not reinterpreted using schema-4/5 plotting rules. Historical protocol notes are
in [legacy-schemas.md](legacy-schemas.md).

## Verification

```text
node --test tests/test_apps_script.js
python prototype/cloud_receiver_check.py
```

The receiver check reads the ignored local deployment URL and verifies TLS and
health without printing it. Health advertises scan schemas **[1,2,3,4,5]** and
settings versions **[1,2,3]**. Its optional upload check needs an explicitly
supplied JSON payload; normal health checking creates no scan.

Host tests cover old/new schemas, complete and nonempty partial records,
exact retry/conflict behavior, interrupted writes, formula/ chart repair and
historical-tab preservation. Index maintenance tests cover reordered columns,
deleted and renamed tabs, surviving annotations, foreign/malformed rows,
interrupted cleanup, busy locks and idempotent trigger setup. Schema-5 tests
cover bounded correction provenance, raw/corrected labels, legacy preservation
and interrupted upload recovery. Live deployment and an actual Pico upload
are separate checks.
