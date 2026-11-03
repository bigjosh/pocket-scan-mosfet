/** Pocket MOSFET scan receiver. Deploy as owner, with anonymous access.
 * The deployment URL is the upload capability. Never log requests or that URL.
 * Run setupReceiver manually from the bound spreadsheet before deployment.
 */
const PSM = Object.freeze({
  property: 'POCKET_SCAN_SPREADSHEET_ID',
  index: 'Index',
  format: 'pocket-scan-mosfet/1',
  maxBytes: 2 * 1024 * 1024,
  maxPoints: 4096,
  headerRow: 15,
  columns: ['index', 'curve', 'requested_vds', 'requested_vgs', 'vds', 'vgs',
    'id_a', 'ig_a', 'id_std_a', 'ig_std_a', 'drain_shunt_v', 'gate_shunt_v',
    'drain_shunt_std_v', 'gate_shunt_std_v', 'batches_completed',
    'drain_voltage_limited', 'gate_voltage_limited'],
  indexColumns: ['scan_id', 'sha256', 'point_count', 'sheet_id', 'created_utc',
    'received_utc', 'device_id', 'duration_ms', 'outcome', 'scan_url',
    'settings_json', 'calibration_sha256'],
});

// The first eight columns are shared. Keep legacy physical columns and body
// hashes intact; compact scans never invent missing shunt/deviation values.
const PSM_LAYOUTS = Object.freeze({
  legacy: Object.freeze({columns: PSM.columns, dataSchema: 2, rawEnd: 'Q', flaggedGate: false}),
  compact: Object.freeze({columns: Object.freeze([
    'index', 'curve', 'requested_vds', 'requested_vgs', 'vds', 'vgs', 'id_a', 'ig_a',
    'drain_voltage_limited', 'gate_voltage_limited', 'gate_off_target']),
    dataSchema: 3, rawEnd: 'K', flaggedGate: true}),
});

function scanLayout_(schemaVersion) {
  require_([1, 2, 3].includes(schemaVersion));
  return schemaVersion === 3 ? PSM_LAYOUTS.compact : PSM_LAYOUTS.legacy;
}

/** Read only the bounded header, including on a chartless 11-column tab. */
function savedLayout_(sheet) {
  const width = Math.min(sheet.getMaxColumns(), PSM.columns.length);
  require_(sheet.getMaxRows() >= PSM.headerRow && width >= PSM_LAYOUTS.compact.columns.length,
    'invalid_saved_scan');
  const header = sheet.getRange(PSM.headerRow, 1, 1, width).getValues()[0];
  for (const layout of [PSM_LAYOUTS.legacy, PSM_LAYOUTS.compact]) {
    if (header.length >= layout.columns.length && layout.columns.every((name, i) => header[i] === name)) return layout;
  }
  throw new ReceiverError('invalid_saved_scan');
}

class ReceiverError extends Error {
  constructor(code) { super(code); this.code = code; }
}

/** Manual setup only: active-document helpers are unavailable in web apps. */
function setupReceiver() {
  const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
  if (!spreadsheet) throw new Error('Open the bound spreadsheet before setup');
  const properties = PropertiesService.getScriptProperties();
  const oldId = properties.getProperty(PSM.property);
  if (oldId && oldId !== spreadsheet.getId()) {
    throw new Error('Receiver is already bound to another spreadsheet');
  }
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw new Error('Receiver is busy');
  try {
    ensureIndex_(spreadsheet);
    properties.setProperty(PSM.property, spreadsheet.getId());
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
}

/** Health only: never expose scans, workbook ID, configuration or credentials. */
function doGet() {
  return json_({ok: true, service: 'pocket-scan-mosfet', schema_version: 1,
    settings_versions: [1, 2, 3], scan_schema_versions: [1, 2, 3]});
}

function doPost(event) {
  let lock;
  let locked = false;
  try {
    const body = requestBody_(event);
    let scan;
    try { scan = JSON.parse(body); }
    catch (_) { throw new ReceiverError('invalid_json'); }
    validateScan_(scan);
    const digest = sha256_(body);
    const spreadsheetId = PropertiesService.getScriptProperties().getProperty(PSM.property);
    if (!spreadsheetId) throw new ReceiverError('receiver_not_configured');
    lock = LockService.getScriptLock();
    locked = lock.tryLock(5000);
    if (!locked) throw new ReceiverError('receiver_busy');
    const spreadsheet = SpreadsheetApp.openById(spreadsheetId);
    return json_(receive_(spreadsheet, scan, digest));
  } catch (error) {
    // Never return or log raw exceptions, request bodies, or capability URLs.
    return json_({ok: false, error: error instanceof ReceiverError ? error.code : 'storage_error'});
  } finally {
    if (locked) {
      try { lock.releaseLock(); } catch (_) { /* Runtime releases it on exit. */ }
    }
  }
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

function require_(condition, code) {
  if (!condition) throw new ReceiverError(code || 'invalid_scan');
}

function object_(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function number_(value, low, high, integer) {
  return typeof value === 'number' && Number.isFinite(value) && value >= low && value <= high &&
    (!integer || Number.isSafeInteger(value));
}

function utc_(value) {
  return typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/.test(value) &&
    Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
}

/** Count actual UTF-8 bytes without allocating a second multi-megabyte buffer. */
function utf8Length_(text) {
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x80) bytes++;
    else if (c < 0x800) bytes += 2;
    else if (c >= 0xd800 && c <= 0xdbff) {
      const next = text.charCodeAt(++i);
      require_(next >= 0xdc00 && next <= 0xdfff, 'invalid_request');
      bytes += 4;
    } else {
      require_(c < 0xdc00 || c > 0xdfff, 'invalid_request');
      bytes += 3;
    }
    require_(bytes <= PSM.maxBytes, 'payload_too_large');
  }
  return bytes;
}

function requestBody_(event) {
  require_(event && event.postData && typeof event.postData.contents === 'string', 'invalid_request');
  require_(typeof event.postData.type === 'string' &&
    event.postData.type.split(';')[0].trim().toLowerCase() === 'application/json', 'invalid_request');
  const body = event.postData.contents;
  require_(body.length > 0, 'invalid_request');
  require_(body.length <= PSM.maxBytes && !(event.contentLength > PSM.maxBytes), 'payload_too_large');
  utf8Length_(body);
  return body;
}

/** Keep each snapshot well below Sheets' single-cell limit, with bounded depth. */
function snapshot_(value) {
  require_(object_(value));
  let nodes = 0;
  function visit(item, depth) {
    require_(++nodes <= 2048 && depth <= 6);
    if (item === null || typeof item === 'boolean') return;
    if (typeof item === 'number') { require_(number_(item, -1e12, 1e12)); return; }
    if (typeof item === 'string') { require_(item.length <= 2048); return; }
    require_(Array.isArray(item) || object_(item));
    const keys = Object.keys(item);
    require_(keys.length <= 256);
    for (const key of keys) {
      require_(key.length <= 128 && key !== '__proto__' && key !== 'constructor' && key !== 'prototype');
      visit(item[key], depth + 1);
    }
  }
  visit(value, 0);
  require_(JSON.stringify(value).length <= 32768);
}

function validateSettings_(settings) {
  snapshot_(settings);
  require_([1, 2, 3].includes(settings.version));
  const integer = settings.version >= 2;
  const unit = integer ? '_uv' : '_v';
  const wrongUnit = integer ? '_v' : '_uv';
  const voltageFields = ['driver_supply', 'source_voltage', 'drain_start', 'drain_stop',
    'drain_step', 'gate_start', 'gate_stop', 'gate_step'];
  require_(voltageFields.every(key => !Object.prototype.hasOwnProperty.call(settings, key + wrongUnit)));
  const scale = integer ? 1000000 : 1;
  const supply = settings['driver_supply' + unit];
  const source = settings['source_voltage' + unit];
  require_(number_(supply, 2.1 * scale, 30 * scale, integer));
  require_(number_(source, 0, supply, integer));
  const geometry = integer ? {count: 1, drainCount: 0} : null;
  for (const role of ['drain', 'gate']) {
    const start = settings[role + '_start' + unit];
    const stop = settings[role + '_stop' + unit];
    const step = settings[role + '_step' + unit];
    require_(number_(start, -36 * scale, 36 * scale, integer) &&
      number_(stop, start, 36 * scale, integer) &&
      number_(step, integer ? 1 : Number.MIN_VALUE, 72 * scale, integer));
    const tolerance = integer ? 0 : 1e-5;
    require_(start + source >= -tolerance && stop + source <= supply + tolerance);
    if (integer) {
      const count = Math.floor((stop - start) / step) + 1;
      if (role === 'drain') geometry.drainCount = count;
      geometry.count *= count;
      require_(geometry.count <= PSM.maxPoints);
    }
  }
  // Legacy float32 sweeps may have a different planned count from JavaScript.
  // Their actual row count remains bounded, but never recompute their plan.
  const timing = [['settle_ms', settings.version === 3 ? 100 : 200, 10000]];
  if (settings.version === 3) {
    require_([1, 4, 16, 64, 128, 256, 512, 1024].includes(settings.ina_average_count));
    require_(['batches', 'samples_per_batch', 'sample_pause_ms'].every(key =>
      !Object.prototype.hasOwnProperty.call(settings, key)));
  } else {
    timing.push(['batches', 2, 32], ['samples_per_batch', 1, 500], ['sample_pause_ms', 4, 1000]);
    require_(!Object.prototype.hasOwnProperty.call(settings, 'ina_average_count'));
  }
  for (const [key, low, high] of timing) {
    require_(number_(settings[key], low, high, true));
  }
  return geometry;
}

/** Fixed conversion protocol; counts alone must not relabel older calibrations. */
function validateAcquisition_(acquisition, calibration, settings) {
  const shared = {acquisition_mode: 'ina226_triggered_v1',
    ina_average_count: settings.ina_average_count, bus_conversion_us: 1100, shunt_conversion_us: 1100};
  const expected = Object.assign({}, shared, {blocks_per_point: 1, point_variance: 'unavailable'});
  require_(object_(acquisition) && Object.keys(acquisition).length === Object.keys(expected).length &&
    Object.keys(expected).every(key => acquisition[key] === expected[key]), 'invalid_acquisition');
  require_(object_(calibration) && Object.keys(shared).every(key => calibration[key] === shared[key]) &&
    calibration.calibration_repeat_blocks === 3, 'invalid_calibration_acquisition');
}

/** A stopped point is a diagnostic, never a numeric measurement row. */
function validateFault_(fault, count, settings, geometry) {
  const keys = ['channel', 'kind', 'raw_code', 'sign', 'limit_value', 'limit_unit',
    'measured_value', 'index', 'curve', 'requested_vds', 'requested_vgs', 'phase'];
  require_(object_(fault) && Object.keys(fault).length === keys.length &&
    keys.every(key => Object.prototype.hasOwnProperty.call(fault, key)), 'invalid_fault');
  require_(['drain', 'gate'].includes(fault.channel) &&
    ['shunt_saturation', 'bus_saturation', 'current_limit', 'current_limit_alert'].includes(fault.kind) &&
    [-1, 1].includes(fault.sign), 'invalid_fault');
  require_(fault.index === count && number_(fault.index, 0, PSM.maxPoints - 1, true) &&
    number_(fault.curve, 0, PSM.maxPoints - 1, true) &&
    ['drive', 'park_drain', 'settle_gate', 'settle', 'sample'].includes(fault.phase), 'invalid_fault');
  if (geometry) require_(fault.curve === Math.floor(count / geometry.drainCount), 'invalid_fault');
  const source = settings.version >= 2 ? settings.source_voltage_uv / 1000000 : settings.source_voltage_v;
  const supply = settings.version >= 2 ? settings.driver_supply_uv / 1000000 : settings.driver_supply_v;
  for (const key of ['requested_vds', 'requested_vgs']) {
    require_(number_(fault[key], -36, 36) && fault[key] + source >= -1e-5 &&
      fault[key] + source <= supply + 1e-5, 'invalid_fault');
  }
  if (fault.kind === 'current_limit_alert') {
    require_(settings.version === 3 && fault.raw_code === null && fault.measured_value === null &&
      fault.limit_unit === 'A' && number_(fault.limit_value, Number.MIN_VALUE, 1), 'invalid_fault');
  } else if (fault.kind === 'current_limit') {
    require_(fault.raw_code === null && fault.limit_unit === 'A' &&
      number_(fault.limit_value, Number.MIN_VALUE, 1) &&
      number_(fault.measured_value, -1, 1) && fault.measured_value * fault.sign > fault.limit_value,
      'invalid_fault');
  } else {
    require_(fault.limit_unit === 'V' && fault.measured_value === null, 'invalid_fault');
    if (fault.kind === 'shunt_saturation') {
      require_(fault.raw_code === (fault.sign === 1 ? 0x7FFF : 0x8000) &&
        number_(fault.limit_value, 0.0819175 - 1e-6, 0.08192 + 1e-6), 'invalid_fault');
    } else {
      require_(fault.raw_code === 0x7FFF && fault.sign === 1 &&
        number_(fault.limit_value, 40.95875 - 1e-4, 40.95875 + 1e-4), 'invalid_fault');
    }
  }
}

function validateScan_(scan) {
  require_(object_(scan));
  const layout = scanLayout_(scan.schema_version);
  const keys = ['schema_version', 'scan_id', 'device_id', 'created_utc', 'settings',
    'calibration', 'columns', 'points', 'outcome', 'point_count', 'duration_ms'];
  if (scan.schema_version >= 2) keys.push('fault');
  if (object_(scan.settings) && scan.settings.version === 3) keys.push('acquisition');
  require_(Object.keys(scan).length === keys.length && keys.every(key => Object.prototype.hasOwnProperty.call(scan, key)));
  require_((scan.schema_version === 1 && scan.outcome === 'complete') ||
    (scan.schema_version >= 2 && ['complete', 'partial'].includes(scan.outcome)));
  require_(typeof scan.scan_id === 'string' && scan.scan_id.length <= 96 && /^[0-9a-f]+-[0-9a-f]+$/i.test(scan.scan_id));
  require_(typeof scan.device_id === 'string' && scan.device_id.length > 0 && scan.device_id.length <= 64);
  require_(scan.created_utc === null || utc_(scan.created_utc));
  require_(number_(scan.duration_ms, 0, Number.MAX_SAFE_INTEGER, true));
  const geometry = validateSettings_(scan.settings);
  if (scan.schema_version === 3) require_([2, 3].includes(scan.settings.version));
  if (scan.settings.version === 3) require_(scan.schema_version === 3);
  require_(object_(scan.calibration) && Object.keys(scan.calibration).length === 2 &&
    typeof scan.calibration.sha256 === 'string' && /^[0-9a-f]{64}$/i.test(scan.calibration.sha256));
  snapshot_(scan.calibration.metadata);
  if (scan.settings.version === 3) validateAcquisition_(scan.acquisition, scan.calibration.metadata, scan.settings);
  require_(Array.isArray(scan.columns) && scan.columns.length === layout.columns.length &&
    scan.columns.every((name, index) => name === layout.columns[index]), 'invalid_columns');
  require_(number_(scan.point_count, scan.outcome === 'partial' ? 0 : 1, PSM.maxPoints, true) && Array.isArray(scan.points) &&
    scan.points.length === scan.point_count);
  if (geometry) require_(scan.outcome === 'complete' ? scan.point_count === geometry.count :
    scan.point_count < geometry.count);
  if (scan.schema_version >= 2) {
    if (scan.outcome === 'partial') {
      validateFault_(scan.fault, scan.point_count, scan.settings, geometry);
    } else {
      require_(scan.fault === null);
    }
  }
  // Version 2 makes count and curve positions exact. Requested/measured rows
  // still use float32 volts, so do not compare them to exact grid voltages.
  validatePoints_(scan.points, scan.settings, geometry, layout);
}

function validatePoints_(points, settings, geometry, layout) {
  for (let index = 0; index < points.length; index++) {
    const row = points[index];
    require_(Array.isArray(row) && row.length === layout.columns.length, 'invalid_point');
    require_(row[0] === index && number_(row[1], 0, PSM.maxPoints - 1, true), 'invalid_point');
    if (geometry) require_(row[1] === Math.floor(index / geometry.drainCount), 'invalid_point');
    for (let col = 2; col < 6; col++) require_(number_(row[col], -36, 36), 'invalid_point');
    for (let col = 6; col < 8; col++) require_(number_(row[col], -1, 1), 'invalid_point');
    if (layout.flaggedGate) {
      for (let col = 8; col < 11; col++) require_(typeof row[col] === 'boolean', 'invalid_point');
    } else {
      for (let col = 8; col < 10; col++) require_(number_(row[col], 0, 1), 'invalid_point');
      for (let col = 10; col < 12; col++) require_(number_(row[col], -1, 1), 'invalid_point');
      for (let col = 12; col < 14; col++) require_(number_(row[col], 0, 1), 'invalid_point');
      require_(number_(row[14], 2, 32, true) && row[14] === settings.batches, 'invalid_point');
      require_(typeof row[15] === 'boolean' && typeof row[16] === 'boolean', 'invalid_point');
    }
  }
}

function sha256_(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8)
    .map(byte => ('0' + (byte & 255).toString(16)).slice(-2)).join('');
}

function literal_(value) {
  return typeof value === 'string' && /^[\s\u0000-\u001f]*[=+\-@]/.test(value) ? "'" + value : value;
}

function size_(sheet, rows, columns) {
  if (sheet.getMaxRows() < rows) sheet.insertRowsAfter(sheet.getMaxRows(), rows - sheet.getMaxRows());
  if (sheet.getMaxRows() > rows) sheet.deleteRows(rows + 1, sheet.getMaxRows() - rows);
  if (sheet.getMaxColumns() < columns) sheet.insertColumnsAfter(sheet.getMaxColumns(), columns - sheet.getMaxColumns());
  if (sheet.getMaxColumns() > columns) sheet.deleteColumns(columns + 1, sheet.getMaxColumns() - columns);
}

function ensureIndex_(spreadsheet) {
  let sheet = spreadsheet.getSheetByName(PSM.index);
  if (!sheet) sheet = spreadsheet.insertSheet(PSM.index);
  if (sheet.getLastRow() === 0) {
    size_(sheet, 1, PSM.indexColumns.length);
    sheet.getRange(1, 1, 1, PSM.indexColumns.length).setValues([PSM.indexColumns]);
    SpreadsheetApp.flush();
  } else {
    indexColumns_(sheet);
  }
  return sheet;
}

/** Map canonical field positions to the owner's current first-12-column order. */
function indexColumns_(index) {
  require_(index.getMaxColumns() >= PSM.indexColumns.length, 'index_conflict');
  const header = index.getRange(1, 1, 1, PSM.indexColumns.length).getValues()[0];
  const positions = PSM.indexColumns.map(name => header.indexOf(name));
  // Every expected name must occur exactly once; extra owner columns are
  // outside this region and are neither interpreted nor rewritten.
  require_(new Set(header).size === PSM.indexColumns.length &&
    positions.every(position => position >= 0), 'index_conflict');
  return positions;
}

function indexEntry_(index, scanId, positions = indexColumns_(index)) {
  let result = null;
  const end = index.getLastRow();
  for (let row = 2; row <= end; row += 128) {
    const values = index.getRange(row, 1, Math.min(128, end - row + 1), PSM.indexColumns.length).getValues();
    for (let offset = 0; offset < values.length; offset++) {
      if (values[offset][positions[0]] === scanId) {
        require_(result === null, 'index_conflict');
        result = {row: row + offset, values: positions.map(position => values[offset][position])};
      }
    }
  }
  return result;
}

function identity_(sheet) {
  if (sheet.getMaxRows() < 4 || sheet.getMaxColumns() < 2) return null;
  const rows = sheet.getRange(1, 1, 4, 2).getValues();
  if (rows[0][0] !== 'format' || rows[0][1] !== PSM.format) return null;
  return {scanId: rows[1][1], digest: rows[2][1], state: rows[3][1]};
}

function receive_(spreadsheet, scan, digest) {
  const layout = scanLayout_(scan.schema_version);
  const width = layout.columns.length;
  const index = ensureIndex_(spreadsheet);
  const indexPositions = indexColumns_(index);
  const entry = indexEntry_(index, scan.scan_id, indexPositions);
  if (entry) require_(entry.values[1] === digest, 'scan_conflict');
  // The full BODY digest in a staging name reserves the upload even if the
  // execution stops immediately after insertSheet, before its first cell write.
  const prefix = '_psm_' + sha256_(scan.scan_id).slice(0, 16) + '_';
  const stagingName = prefix + digest;
  let sheet = null;
  let identity = null;
  for (const candidate of spreadsheet.getSheets()) {
    if (candidate.getSheetId() === index.getSheetId()) continue;
    const name = candidate.getName();
    const found = identity_(candidate);
    if (name.startsWith(prefix) || (found && found.scanId === scan.scan_id)) {
      require_(!sheet, 'storage_conflict');
      if (name.startsWith(prefix)) require_(name === stagingName, 'scan_conflict');
      if (found) require_(found.scanId === scan.scan_id && found.digest === digest, 'scan_conflict');
      else require_(candidate.getLastRow() === 0, 'storage_conflict');
      sheet = candidate;
      identity = found;
    }
  }
  if (entry) {
    require_(sheet && entry.values[2] === scan.point_count && entry.values[3] === sheet.getSheetId(), 'storage_conflict');
  }
  if (!sheet) sheet = spreadsheet.insertSheet(stagingName);
  let received = identity ? sheet.getRange(7, 2).getValue() : null;
  if (!utc_(received)) received = new Date().toISOString();
  const state = identity ? identity.state : 'staging';
  require_(['staging', 'ready', 'complete'].includes(state), 'storage_conflict');
  if (state === 'staging') {
    size_(sheet, PSM.headerRow + scan.point_count, width);
    const metadata = [['format', PSM.format], ['scan_id', scan.scan_id], ['sha256', digest],
      ['state', 'staging'], ['device_id', scan.device_id], ['created_utc', scan.created_utc || ''],
      ['received_utc', received], ['point_count', scan.point_count], ['duration_ms', scan.duration_ms],
      ['outcome', scan.outcome], ['settings_json', JSON.stringify(scan.settings)],
      ['calibration_sha256', scan.calibration.sha256],
      ['calibration_metadata_json', JSON.stringify(scan.calibration.metadata)],
      scan.schema_version >= 2 ? ['fault_json', JSON.stringify(scan.fault)] : []];
    const header = metadata.map(row => Array.from({length: width}, (_, i) => literal_(row[i] === undefined ? '' : row[i])));
    if (scan.settings.version === 3) {
      // New metadata uses otherwise empty cells without moving legacy data or chart ranges.
      header[0][3] = 'acquisition_json';
      header[0][4] = JSON.stringify(scan.acquisition);
    }
    header.push(layout.columns);
    sheet.getRange(1, 1, PSM.headerRow, width).setValues(header);
    if (scan.point_count) sheet.getRange(PSM.headerRow + 1, 1, scan.point_count, width).setValues(scan.points);
    SpreadsheetApp.flush();
    sheet.getRange(4, 2).setValue('ready');
    SpreadsheetApp.flush();
  } else {
    require_(sheet.getRange(8, 2).getValue() === scan.point_count &&
      sheet.getMaxRows() >= PSM.headerRow + scan.point_count, 'storage_conflict');
    require_(sheet.getMaxColumns() >= width, 'storage_conflict');
    const columns = sheet.getRange(PSM.headerRow, 1, 1, width).getValues()[0];
    require_(columns.every((name, i) => name === layout.columns[i]), 'storage_conflict');
  }
  if (sheet.getName() === stagingName) {
    const time = (scan.created_utc || received).slice(0, 19).replace('T', ' ').replace(/:/g, '-');
    let finalName = time + ' ' + sha256_(scan.scan_id).slice(0, 12);
    const collision = spreadsheet.getSheetByName(finalName);
    if (collision && collision.getSheetId() !== sheet.getSheetId()) finalName = time + ' ' + sha256_(scan.scan_id);
    sheet.setName(finalName);
    SpreadsheetApp.flush();
  }
  if (!entry) {
    const row = index.getLastRow() + 1;
    // A fresh receiver Index is exact-sized. On later appends, preserve any
    // owner-added columns/rows instead of deleting human annotations.
    if (index.getMaxRows() < row) index.insertRowsAfter(index.getMaxRows(), row - index.getMaxRows());
    const link = 'https://docs.google.com/spreadsheets/d/' + spreadsheet.getId() + '/edit#gid=' + sheet.getSheetId();
    const values = [scan.scan_id, digest, scan.point_count, sheet.getSheetId(), scan.created_utc || '',
      received, scan.device_id, scan.duration_ms, scan.outcome, link, JSON.stringify(scan.settings), scan.calibration.sha256];
    const ordered = Array(values.length);
    indexPositions.forEach((position, canonical) => { ordered[position] = literal_(values[canonical]); });
    index.getRange(row, 1, 1, ordered.length).setValues([ordered]);
    SpreadsheetApp.flush();
  }
  if (state !== 'complete') {
    sheet.getRange(4, 2).setValue('complete');
    SpreadsheetApp.flush();
  }
  // Charts are derived presentation. A chart-service failure must never undo
  // or withhold acknowledgement of an already committed measurement record.
  try { ensureScanCharts_(sheet, scan.points, scan.fault || null, layout); }
  catch (_) { chartRetryNotice_(sheet); }
  return {ok: true, scan_id: scan.scan_id, sha256: digest, point_count: scan.point_count, sheet_id: sheet.getSheetId()};
}

const PSM_CHARTS = Object.freeze({
  key: 'POCKET_SCAN_CHARTS', version: 4, seriesPerPage: 16,
  summaryColumn: 33, drainColumn: 42, lastColumn: 59, minRows: 50,
  gateToleranceV: 0.020, offTargetColor: '#D98B00',
  drainTitle: 'Drain current vs VDS', leakageTitle: 'Maximum |Ig| vs VGS',
  colors: ['#007E87', '#C75C0B', '#6854A3', '#2670B3', '#AB4064', '#50742D',
    '#985B26', '#4A7C8C', '#9B489E', '#5368A0', '#A05B60', '#5B8261',
    '#7654A3', '#87652D', '#267F9B', '#8D4B76'],
});

/** Owner-only editor action. It never changes raw measurements/checksums. */
function addScanCharts() {
  const id = PropertiesService.getScriptProperties().getProperty(PSM.property);
  if (!id) throw new Error('Run setupReceiver first');
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) throw new Error('Receiver is busy; try again');
  let failed = 0;
  try {
    for (const sheet of SpreadsheetApp.openById(id).getSheets()) {
      const identity = identity_(sheet);
      if (!identity || identity.state !== 'complete') continue;
      try {
        const count = sheet.getRange(8, 2).getValue();
        require_(number_(count, 0, PSM.maxPoints, true), 'invalid_saved_scan');
        const layout = savedLayout_(sheet);
        const partial = sheet.getRange(10, 2).getValue() === 'partial';
        const fault = partial ? JSON.parse(sheet.getRange(14, 2).getValue()) : null;
        require_(count > 0 || partial, 'invalid_saved_scan');
        const settings = JSON.parse(sheet.getRange(11, 2).getValue());
        const geometry = validateSettings_(settings);
        if (layout.flaggedGate) {
          require_([2, 3].includes(settings.version) && (partial ? count < geometry.count : count === geometry.count), 'invalid_saved_scan');
        }
        if (settings.version === 3) {
          require_(layout.flaggedGate && sheet.getRange(1, 4).getValue() === 'acquisition_json', 'invalid_saved_scan');
          validateAcquisition_(JSON.parse(sheet.getRange(1, 5).getValue()),
            JSON.parse(sheet.getRange(13, 2).getValue()), settings);
        }
        if (partial) validateFault_(fault, count, settings, geometry);
        require_(sheet.getMaxRows() >= PSM.headerRow + count, 'invalid_saved_scan');
        const points = count ? sheet.getRange(PSM.headerRow + 1, 1, count, layout.columns.length).getValues() : [];
        if (layout.flaggedGate) validatePoints_(points, settings, geometry, layout);
        ensureScanCharts_(sheet, points, fault, layout);
      } catch (_) { failed++; chartRetryNotice_(sheet); }
    }
  } finally { lock.releaseLock(); }
  if (failed) throw new Error('Some scan charts need attention; measurement data is unchanged');
}

function chartMetadata_(sheet) {
  const found = sheet.getDeveloperMetadata().filter(item => item.getKey() === PSM_CHARTS.key);
  require_(found.length <= 1, 'chart_layout_conflict');
  return found.length ? found[0] : null;
}

function chartRetryNotice_(sheet) {
  try {
    if (chartMetadata_(sheet)) sheet.getRange(48, 19, 1, 2).setValues([['Charts', 'Needs retry: run addScanCharts']]);
  } catch (_) { /* Preserve the committed scan even if presentation is unavailable. */ }
}

function gateOnTarget_(point, layout = PSM_LAYOUTS.legacy) {
  // Compact RAM records preserve the device's classification bit, including
  // float32 edge decisions. Never reclassify it using JavaScript float64.
  if (layout.flaggedGate) return point[10] === false;
  // The inclusive band matches the Pico's plot classification. Compare the
  // endpoints directly so subtraction rounding does not exclude the boundary.
  return point[5] >= point[3] - PSM_CHARTS.gateToleranceV &&
    point[5] <= point[3] + PSM_CHARTS.gateToleranceV;
}

function gateInBandFormula_(sourceRow, layout = PSM_LAYOUTS.legacy) {
  if (layout.flaggedGate) return 'NOT($K$' + sourceRow + ')';
  return 'AND($F$' + sourceRow + '>=$D$' + sourceRow + '-' + PSM_CHARTS.gateToleranceV +
    ',$F$' + sourceRow + '<=$D$' + sourceRow + '+' + PSM_CHARTS.gateToleranceV + ')';
}

function chartPlan_(points, layout = PSM_LAYOUTS.legacy) {
  require_(Array.isArray(points) && points.length >= 1 && points.length <= PSM.maxPoints, 'invalid_saved_scan');
  const byCurve = new Map();
  const offTargetRows = [];
  points.forEach((point, index) => {
    require_(Array.isArray(point) && point.length === layout.columns.length && point[0] === index &&
      number_(point[1], 0, PSM.maxPoints - 1, true) &&
      point.slice(2, 8).every(value => typeof value === 'number' && Number.isFinite(value)), 'invalid_saved_scan');
    if (!byCurve.has(point[1])) byCurve.set(point[1], {id: point[1], target: point[3], rows: []});
    byCurve.get(point[1]).rows.push(PSM.headerRow + 1 + index);
    if (layout.flaggedGate) require_(point.slice(8).every(value => typeof value === 'boolean'), 'invalid_saved_scan');
    if (!gateOnTarget_(point, layout)) offTargetRows.push(PSM.headerRow + 1 + index);
  });
  const groups = [...byCurve.values()].sort((a, b) => a.target - b.target || a.id - b.id);
  const series = Math.min(PSM_CHARTS.seriesPerPage, groups.length);
  const rowPages = {};
  groups.forEach((group, index) => group.rows.forEach(row => {
    rowPages[row] = Math.floor(index / PSM_CHARTS.seriesPerPage) + 1;
  }));
  return {groups, series, rows: groups.flatMap(group => group.rows), rowPages, offTargetRows,
    pages: Math.ceil(groups.length / PSM_CHARTS.seriesPerPage)};
}

function requireEmptyChartRange_(range) {
  for (const cells of [range.getValues(), range.getFormulas(), range.getNotes()]) {
    require_(!cells.some(row => row.some(value => value !== '')), 'chart_layout_conflict');
  }
}

function growChartArea_(sheet, rows) {
  if (sheet.getMaxRows() < rows) sheet.insertRowsAfter(sheet.getMaxRows(), rows - sheet.getMaxRows());
  if (sheet.getMaxColumns() < PSM_CHARTS.lastColumn) {
    sheet.insertColumnsAfter(sheet.getMaxColumns(), PSM_CHARTS.lastColumn - sheet.getMaxColumns());
  }
}

function ensureScanCharts_(sheet, points, fault = null, layout = PSM_LAYOUTS.legacy) {
  require_(points.length > 0 || fault, 'invalid_saved_scan');
  const empty = points.length === 0;
  const ready = empty ? 'No completed points to plot' : 'Ready';
  const banner = fault ? 'PARTIAL — ' + points.length + ' recorded points. ' + fault.channel + ' ' +
    fault.kind.replace(/_/g, ' ') + ' at requested VDS ' + fault.requested_vds.toFixed(3) +
    ' V / VGS ' + fault.requested_vgs.toFixed(3) + ' V. Overrange point excluded.' : '';
  let metadata = chartMetadata_(sheet);
  let saved = metadata ? JSON.parse(metadata.getValue()) : null;
  let charts = sheet.getCharts();
  // Existing version-4 legacy metadata is intentionally unchanged. Schema3
  // tags only its own new metadata and keeps the same presentation region.
  const matchingDataSchema = saved && (layout.flaggedGate ? saved.data_schema === 3 :
    saved.data_schema === undefined || saved.data_schema === 2);
  if (saved && matchingDataSchema && saved.version === PSM_CHARTS.version && saved.state === 'complete' &&
      ((empty && saved.empty === true) || (!empty &&
        ['drain', 'leakage'].every(role => charts.some(chart => chart.getChartId() === saved[role]))))) {
    if (sheet.getRange(48, 20).getValue() !== ready) sheet.getRange(48, 20).setValue(ready);
    if (fault && sheet.getRange(2, 19).getValue() !== banner) sheet.getRange(2, 19).setValue(banner);
    return;
  }
  const plan = empty ? null : chartPlan_(points, layout);
  const rows = Math.max(PSM_CHARTS.minRows, PSM.headerRow + points.length, plan ? plan.groups.length + 1 : 0);
  if (metadata && saved.version !== PSM_CHARTS.version) {
    require_(Number.isInteger(saved.version) && saved.version >= 1 && saved.version < PSM_CHARTS.version,
      'chart_layout_conflict');
    // Earlier layouts owned only through BF. Claim BG only if it is empty;
    // owner values, even blank-result formulas or notes, must survive migration.
    // Check before changing metadata, charts, page selection or source formulas.
    if (sheet.getMaxColumns() >= PSM_CHARTS.lastColumn) {
      requireEmptyChartRange_(sheet.getRange(1, PSM_CHARTS.lastColumn,
        Math.min(rows, sheet.getMaxRows()), 1));
    }
  }
  if (!metadata) {
    // Claim only an empty presentation region. Existing owner annotations are
    // never silently overwritten during backfill.
    const width = Math.min(sheet.getMaxColumns(), PSM_CHARTS.lastColumn) - 18;
    if (width > 0) {
      const region = sheet.getRange(1, 19, Math.min(rows, sheet.getMaxRows()), width);
      requireEmptyChartRange_(region);
    }
    const initial = {version: PSM_CHARTS.version, state: 'building'};
    if (layout.flaggedGate) initial.data_schema = 3;
    sheet.addDeveloperMetadata(PSM_CHARTS.key, JSON.stringify(initial));
    metadata = chartMetadata_(sheet);
  }
  growChartArea_(sheet, rows);
  saved = {version: PSM_CHARTS.version, state: 'building'};
  if (layout.flaggedGate) saved.data_schema = 3;
  metadata.setValue(JSON.stringify(saved));
  if (fault) sheet.getRange(2, 19).setValue(banner).setFontColor('#C75C0B');
  if (empty) {
    sheet.getRange(1, 19).setValue('Scan stopped before the first completed point');
    sheet.getRange(48, 19, 1, 2).setValues([['Charts', ready]]);
    SpreadsheetApp.flush();
    saved.state = 'complete'; saved.empty = true;
    metadata.setValue(JSON.stringify(saved));
    return;
  }
  sheet.getRange(1, 19).setValue('Drain curve page');
  const page = sheet.getRange(1, 21);
  if (!number_(page.getValue(), 1, plan.pages, true)) page.setValue(1);
  page.setDataValidation(SpreadsheetApp.newDataValidation().requireFormulaSatisfied(
    '=AND(ISNUMBER(U1),U1=INT(U1),U1>=1,U1<=' + plan.pages + ')')
    .setAllowInvalid(false).setHelpText('Choose a whole-number page; every gate curve is included.').build());
  page.setBackground('#E5F3F4').setFontColor('#006B73').setNumberFormat('0');
  sheet.getRange(1, 22).setFormula('="of "&MAX(1,ROUNDUP(COUNTA($AG$2:$AG$' + (plan.groups.length + 1) + ')/16,0))');
  sheet.getRange(47, 19).setValue(layout.flaggedGate ?
    'Measured axes; VGS labels are requests. Device gate-off-target flags create isolated amber points. 16 gate curves/page.' :
    'Measured axes; VGS labels are requests. Gate misses outside +/-20 mV are isolated amber points. 16 gate curves/page.');
  sheet.getRange(49, 19).setValue('Leakage is maximum absolute baseline-corrected Ig among recorded points in each drain sweep.');
  sheet.getRange(50, 19).setValue('Chart source formulas are in columns AG:BG. All measured points remain in A:' + layout.rawEnd + '.');

  const summary = [['Curve', 'Requested VGS (V)', 'Maximum |Ig| (A)', 'Peak source row',
    'Measured VGS at peak (V)', 'Maximum |Ig| (uA)', 'Measured VDS at peak (V)', 'Peak point index']];
  plan.groups.forEach((group, index) => {
    const row = index + 2, first = Math.min(...group.rows), last = Math.max(...group.rows);
    const current = '$H$' + first + ':$H$' + last, curve = '$B$' + first + ':$B$' + last;
    const absolute = 'ARRAYFORMULA(IF(' + curve + '=$AG' + row + ',ABS(' + current + '),-1))';
    summary.push(['=$B$' + first, '=$D$' + first, '=MAX(' + absolute + ')',
      '=MATCH($AI' + row + ',' + absolute + ',0)+' + (first - 1),
      '=INDEX($F:$F,$AJ' + row + ')', '=$AI' + row + '*1E6',
      '=INDEX($E:$E,$AJ' + row + ')', '=INDEX($A:$A,$AJ' + row + ')']);
  });
  sheet.getRange(1, PSM_CHARTS.summaryColumn, summary.length, 8).setValues(summary);
  const gateEnd = plan.groups.length + 1;
  const drain = [['Measured VDS (V)']];
  for (let series = 0; series < plan.series; series++) {
    drain[0].push('=IFERROR("VGS target "&TEXT(INDEX($AH$2:$AH$' + gateEnd +
      ',(INT($U$1)-1)*16+' + (series + 1) + '),"0.###")&" V","")');
  }
  drain[0].push('Gate off target');
  for (const sourceRow of plan.rows) {
    const row = ['=$E$' + sourceRow];
    const inBand = gateInBandFormula_(sourceRow, layout);
    for (let series = 0; series < plan.series; series++) {
      row.push('=IFERROR(IF(AND($B$' + sourceRow + '=INDEX($AG$2:$AG$' + gateEnd +
        ',(INT($U$1)-1)*16+' + (series + 1) + '),' + inBand + '),$G$' + sourceRow + '*1E3,""),"")');
    }
    // Membership is tied to the sorted gate group's page, not an INDEX range
    // that could run past the end of a partially populated final page.
    row.push('=IFERROR(IF(AND(INT($U$1)=' + plan.rowPages[sourceRow] + ',NOT(' + inBand +
      ')),$G$' + sourceRow + '*1E3,""),"")');
    drain.push(row);
  }
  sheet.getRange(1, PSM_CHARTS.drainColumn, drain.length, plan.series + 2).setValues(drain);
  sheet.getRange(1, PSM_CHARTS.summaryColumn, 1, 8).setFontWeight('bold');
  sheet.getRange(1, PSM_CHARTS.drainColumn, 1, plan.series + 2).setFontWeight('bold');
  SpreadsheetApp.flush();

  const specifications = [
    {role: 'drain', title: PSM_CHARTS.drainTitle, column: PSM_CHARTS.drainColumn,
      rows: drain.length, columns: plan.series + 2, anchor: 3, type: Charts.ChartType.LINE,
      x: 'Measured VDS (V)', y: 'Drain current Id (mA)'},
    {role: 'leakage', title: PSM_CHARTS.leakageTitle, column: 37,
      rows: summary.length, columns: 2, anchor: 25, type: Charts.ChartType.SCATTER,
      x: 'Measured VGS at peak |Ig| (V)', y: 'Maximum |Ig| (uA)'},
  ];
  for (const spec of specifications) {
    charts = sheet.getCharts();
    // Recover an insertion interrupted before the new ID was saved. Match
    // both our title and our reserved source range, never unrelated charts.
    const owned = charts.filter(chart => chart.getOptions().get('title') === spec.title &&
      chart.getRanges().some(range => range.getColumn() === spec.column && range.getRow() === 1));
    // Rebuild managed charts so unsupported legacy layout options cannot
    // survive modify(). Insert first, then remove only our older versions.
    let builder = sheet.newChart();
    builder = builder.setChartType(spec.type)
      .addRange(sheet.getRange(1, spec.column, spec.rows, spec.columns))
      .setNumHeaders(1).setTransposeRowsAndColumns(false)
      .setHiddenDimensionStrategy(Charts.ChartHiddenDimensionStrategy.SHOW_BOTH)
      .setPosition(spec.anchor, 19, 0, 0)
      .setOption('title', spec.title).setOption('width', 900).setOption('height', 420)
      .setOption('fontName', 'Arial').setOption('backgroundColor', '#FFFFFF')
      .setOption('titleTextStyle', {color: '#12354A', fontSize: 18})
      .setOption('hAxis', {title: spec.x}).setOption('vAxis', {title: spec.y})
      .setOption('pointSize', 4).setOption('lineWidth', 1.5)
      .setOption('colors', PSM_CHARTS.colors)
      .setOption('legend', {position: spec.role === 'drain' ? 'right' : 'none', textStyle: {fontSize: 11}});
    if (spec.role === 'drain') {
      // Scatter charts do not support interpolateNulls. A numeric-domain LINE
      // chart preserves measured X spacing while leaving gaps at missing Ys.
      // The extra series shows each excluded point without connecting it.
      const series = {};
      series[plan.series] = {lineWidth: 0, pointSize: 4, pointShape: 'square',
        color: PSM_CHARTS.offTargetColor, visibleInLegend: plan.offTargetRows.length > 0};
      builder = builder.setOption('interpolateNulls', false)
        .setOption('treatLabelsAsText', false).setOption('useFirstColumnAsDomain', true)
        .setOption('series', series);
    }
    // Native Sheets leaves the plot blank with custom chartArea options,
    // including pixel dimensions. Its automatic layout renders correctly.
    sheet.insertChart(builder.build());
    for (const previous of owned) sheet.removeChart(previous);
    const managed = sheet.getCharts().find(chart => chart.getOptions().get('title') === spec.title &&
      chart.getRanges().some(range => range.getColumn() === spec.column && range.getRow() === 1));
    require_(managed, 'chart_not_created');
    saved[spec.role] = managed.getChartId();
    metadata.setValue(JSON.stringify(saved));
  }
  sheet.getRange(48, 19, 1, 2).setValues([['Charts', 'Ready']]);
  SpreadsheetApp.flush();
  saved.state = 'complete';
  metadata.setValue(JSON.stringify(saved));
}
