/* Host-only Apps Script receiver tests. No Google account, network or real data. */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const code = fs.readFileSync(path.join(__dirname, '..', 'cloud', 'apps-script', 'Code.gs'), 'utf8');

const columns = ['index', 'curve', 'requested_vds', 'requested_vgs', 'vds', 'vgs',
  'id_a', 'ig_a', 'id_std_a', 'ig_std_a', 'drain_shunt_v', 'gate_shunt_v',
  'drain_shunt_std_v', 'gate_shunt_std_v', 'batches_completed',
  'drain_voltage_limited', 'gate_voltage_limited'];
const compactColumns = ['index', 'curve', 'requested_vds', 'requested_vgs', 'vds', 'vgs',
  'id_a', 'ig_a', 'drain_voltage_limited', 'gate_voltage_limited', 'gate_off_target'];

function fixture() {
  return {
    schema_version: 1,
    scan_id: '3b8922c9b91bbe9f-aabbccddeeff00112233445566778899',
    device_id: 'synthetic-pico', created_utc: '2026-11-01T15:04:05Z',
    settings: {version: 1, drain_start_v: 0, drain_stop_v: 1, drain_step_v: 0.5,
      gate_start_v: 0, gate_stop_v: 0, gate_step_v: 0.5, driver_supply_v: 5,
      source_voltage_v: 0, batches: 10, samples_per_batch: 50, sample_pause_ms: 4, settle_ms: 200},
    calibration: {sha256: 'a'.repeat(64), metadata: {version: 2, label: 'synthetic calibration', identity: {drain: 0x44, gate: 0x41}}},
    columns: columns.slice(), outcome: 'complete', point_count: 3, duration_ms: 12000,
    points: [0, 1, 2].map(i => [i, 0, i * 0.5, 0, i * 0.5 - 0.001, 0.0002,
      -2e-7, 1e-8, 2e-8, 3e-9, 1e-6, -2e-6, 3e-7, 4e-7, 10, i === 2, false]),
  };
}

function partialFixture(count = 1) {
  const scan = fixture();
  scan.schema_version = 2;
  scan.outcome = 'partial';
  scan.point_count = count;
  scan.points = scan.points.slice(0, count);
  scan.fault = {channel: 'drain', kind: 'shunt_saturation', raw_code: 0x7FFF,
    sign: 1, limit_value: 0.0819175, limit_unit: 'V', measured_value: null,
    index: count, curve: 0, requested_vds: count * 0.5, requested_vgs: 0, phase: 'sample'};
  return scan;
}

function microvoltFixture() {
  const scan = fixture();
  scan.schema_version = 2;
  scan.fault = null;
  const settings = scan.settings;
  for (const key of Object.keys(settings)) if (key.endsWith('_v')) {
    settings[key.slice(0, -2) + '_uv'] = Math.round(settings[key] * 1000000);
    delete settings[key];
  }
  settings.version = 2;
  return scan;
}

function compactFixture(count = 3) {
  const scan = microvoltFixture();
  scan.schema_version = 3;
  scan.columns = compactColumns.slice();
  scan.points = scan.points.slice(0, count).map(row => row.slice(0, 8).concat(row[15], row[16], false));
  scan.point_count = count;
  if (count < 3) {
    scan.outcome = 'partial';
    scan.fault = partialFixture(count).fault;
  }
  return scan;
}

function triggeredFixture(count = 3, averageCount = 64) {
  const scan = compactFixture(count);
  scan.settings.version = 3;
  for (const field of ['batches', 'samples_per_batch', 'sample_pause_ms']) delete scan.settings[field];
  scan.settings.ina_average_count = averageCount;
  const shared = {acquisition_mode: 'ina226_triggered_v1', ina_average_count: averageCount,
    bus_conversion_us: 1100, shunt_conversion_us: 1100};
  scan.acquisition = {...shared, blocks_per_point: 1, point_variance: 'unavailable'};
  Object.assign(scan.calibration.metadata, shared, {calibration_repeat_blocks: 3});
  return scan;
}

function rawFixture(count = 3) {
  const scan = triggeredFixture(count);
  scan.schema_version = 4;
  delete scan.calibration;
  scan.columns = ['vds', 'vgs', 'id_a', 'ig_a'];
  scan.points = scan.points.map(row => row.slice(4, 8));
  scan.settings.settle_ms = 100;
  return scan;
}

function baselineFixture(count = 3) {
  const scan = rawFixture(count);
  scan.schema_version = 5;
  scan.current_correction = {version: 1, sha256: 'b'.repeat(64), lookup: 'linear_endpoint_hold',
    drain: {min_bus_uv: 3300000, max_bus_uv: 16000000, zero_duty_u16: 0,
      zero_residual_uv: 1250, zero_valid: true},
    gate: {min_bus_uv: 1250, max_bus_uv: 16000000, zero_duty_u16: 8410,
      zero_residual_uv: -2500, zero_valid: true}};
  return scan;
}

function drainBaselineFixture(count = 3) {
  const scan = baselineFixture(count);
  Object.assign(scan.acquisition, {acquisition_mode: 'ina226_ina228_triggered_v1',
    gate_bus_conversion_us: 1052, gate_shunt_conversion_us: 1052});
  Object.assign(scan.current_correction, {version: 2, applied_to: 'drain'});
  return scan;
}

function assertRaw(env, scan, ack) {
  assert.equal(ack.ok, true);
  assert.equal(ack.scan_id, scan.scan_id);
  assert.equal(ack.point_count, scan.point_count);
  assert.equal(ack.sha256, crypto.createHash('sha256').update(JSON.stringify(scan)).digest('hex'));
  const sheet = env.scans().find(item => item.id === ack.sheet_id);
  assert.ok(sheet);
  assert.equal(sheet.cell(4, 2), 'complete');
  assert.equal(sheet.cell(10, 2), scan.outcome);
  assert.deepEqual(JSON.parse(sheet.cell(11, 2)), scan.settings);
  assert.deepEqual(JSON.parse(sheet.cell(1, 5)), scan.acquisition);
  assert.equal(sheet.cell(12, 1), 'measurement_mode');
  assert.equal(sheet.cell(12, 2), 'uncorrected');
  assert.equal(sheet.cell(13, 1), 'scan_schema_version');
  assert.equal(sheet.cell(13, 2), 4);
  assert.deepEqual(JSON.parse(sheet.cell(14, 2)), scan.fault);
  assert.deepEqual(sheet.getRange(15, 1, 1, 8).getValues()[0], columns.slice(0, 8));
  const count = Math.floor((scan.settings.drain_stop_uv - scan.settings.drain_start_uv) / scan.settings.drain_step_uv) + 1;
  scan.points.forEach((row, i) => {
    const curve = Math.floor(i / count);
    assert.deepEqual(sheet.getRange(16 + i, 1, 1, 8).getValues()[0], [i, curve,
      (scan.settings.drain_start_uv + i % count * scan.settings.drain_step_uv) / 1e6,
      (scan.settings.gate_start_uv + curve * scan.settings.gate_step_uv) / 1e6, ...row]);
  });
  assert.equal(sheet.charts.length, 2);
  assert.equal(JSON.parse(sheet.metadata[0].getValue()).data_schema, 4);
  assert.match(sheet.cell(49, 19), /uncorrected Ig/);
  return sheet;
}

function assertPartial(env, scan, acknowledgement) {
  assert.equal(acknowledgement.ok, true);
  assert.equal(acknowledgement.scan_id, scan.scan_id);
  assert.equal(acknowledgement.point_count, scan.point_count);
  assert.equal(acknowledgement.sha256, crypto.createHash('sha256').update(JSON.stringify(scan)).digest('hex'));
  assert.equal(env.scans().length, 1);
  const sheet = env.scans()[0];
  assert.equal(acknowledgement.sheet_id, sheet.id);
  assert.equal(sheet.cell(4, 2), 'complete'); // Receipt is committed; acquisition is partial.
  assert.equal(sheet.cell(10, 2), 'partial');
  assert.equal(sheet.cell(14, 1), 'fault_json');
  assert.deepEqual(JSON.parse(sheet.cell(14, 2)), scan.fault);
  assert.equal(env.index().cell(2, 9), 'partial');
  assert.equal(env.index().getLastRow(), 2);
  assert.match(sheet.cell(2, 19), /^PARTIAL/);
  assert.equal(sheet.charts.length, scan.point_count ? 2 : 0);
  for (let i = 0; i < scan.point_count; i++) {
    assert.deepEqual(sheet.getRange(16 + i, 1, 1, scan.columns.length).getValues()[0], scan.points[i]);
  }
  assert.ok(Array.from({length: scan.columns.length}, (_, i) => sheet.cell(16 + scan.point_count, i + 1)).every(value => value === ''));
}

class FakeRange {
  constructor(sheet, row, col, rows = 1, cols = 1) {
    assert.ok(row >= 1 && col >= 1 && rows >= 1 && cols >= 1);
    assert.ok(row + rows - 1 <= sheet.rows && col + cols - 1 <= sheet.cols, 'range within allocated grid');
    Object.assign(this, {sheet, row, col, rows, cols});
  }
  getValues() {
    return Array.from({length: this.rows}, (_, r) => Array.from({length: this.cols}, (_, c) =>
      this.sheet.cell(this.row + r, this.col + c)));
  }
  getValue() { return this.getValues()[0][0]; }
  getFormulas() { return this.metadataCells(this.sheet.formulas); }
  getNotes() { return this.metadataCells(this.sheet.notes); }
  metadataCells(map) {
    return Array.from({length: this.rows}, (_, r) => Array.from({length: this.cols}, (_, c) =>
      map.get((this.row + r) + ',' + (this.col + c)) || ''));
  }
  getRow() { return this.row; }
  getColumn() { return this.col; }
  setValue(value) { return this.setValues([[value]]); }
  setFormula(value) {
    assert.equal(this.rows, 1); assert.equal(this.cols, 1);
    const key = this.row + ',' + this.col;
    this.sheet.env.mutate('setFormula:' + this.sheet.name + ':' + key, () => {
      this.sheet.cells.set(key, value);
      this.sheet.formulas.set(key, value);
    });
    return this;
  }
  setDataValidation(value) { this.validation = value; return this; }
  setBackground() { return this; }
  setFontColor() { return this; }
  setNumberFormat() { return this; }
  setFontWeight() { return this; }
  setValues(values) {
    assert.equal(values.length, this.rows);
    assert.ok(values.every(row => row.length === this.cols));
    this.sheet.env.mutate('setValues:' + this.sheet.name + ':' + this.row + ':' + this.rows, () => {
      for (let r = 0; r < this.rows; r++) for (let c = 0; c < this.cols; c++) {
        this.sheet.cells.set((this.row + r) + ',' + (this.col + c), values[r][c]);
      }
    });
    return this;
  }
}

class FakeChartBuilder {
  constructor(chart = null) {
    this.id = chart?.id ?? null;
    this.ranges = chart ? chart.ranges.slice() : [];
    this.options = chart ? {...chart.options} : {};
  }
  clearRanges() { this.ranges = []; return this; }
  addRange(range) { this.ranges.push(range); return this; }
  setChartType(type) { this.type = type; return this; }
  setNumHeaders(value) { this.headers = value; return this; }
  setTransposeRowsAndColumns(value) { this.transpose = value; return this; }
  setHiddenDimensionStrategy(value) { this.hidden = value; return this; }
  setPosition(...value) { this.position = value; return this; }
  setOption(key, value) { this.options[key] = value; return this; }
  build() {
    return Object.assign(new FakeChart(), this, {ranges: this.ranges.slice(), options: {...this.options}});
  }
}

class FakeChart {
  getChartId() { return this.id; }
  getOptions() { return {get: key => this.options[key]}; }
  getRanges() { return this.ranges.slice(); }
  modify() { return new FakeChartBuilder(this); }
}

class FakeSheet {
  constructor(env, name, id) { Object.assign(this, {env, name, id, rows: 1000, cols: 26, cells: new Map(), formulas: new Map(), notes: new Map(), metadata: [], charts: []}); }
  cell(row, col) { return this.cells.get(row + ',' + col) ?? ''; }
  getSheetId() { return this.id; }
  getParent() { return this.env.spreadsheet; }
  getName() { return this.name; }
  getMaxRows() { return this.rows; }
  getMaxColumns() { return this.cols; }
  getLastRow() {
    let result = 0;
    for (const [key, value] of this.cells) if (value !== '') result = Math.max(result, Number(key.split(',')[0]));
    return result;
  }
  getRange(...args) { return new FakeRange(this, ...args); }
  getDeveloperMetadata() { return this.metadata.slice(); }
  addDeveloperMetadata(key, value) {
    const sheet = this;
    this.env.mutate('chartMetadataCreate', () => this.metadata.push({key, value,
      getKey() { return this.key; }, getValue() { return this.value; },
      setValue(value) { sheet.env.mutate('chartMetadataUpdate', () => { this.value = value; }); return this; }}));
  }
  getCharts() { return this.charts.slice(); }
  newChart() { return new FakeChartBuilder(); }
  insertChart(chart) {
    this.env.mutate('insertChart', () => { chart.id = this.env.nextChartId++; this.charts.push(chart); });
  }
  updateChart(chart) {
    this.env.mutate('updateChart', () => { this.charts[this.charts.findIndex(item => item.id === chart.id)] = chart; });
  }
  removeChart(chart) {
    this.env.mutate('removeChart', () => { this.charts = this.charts.filter(item => item.id !== chart.id); });
  }
  setName(name) {
    assert.ok(name.length <= 100 && !/[\[\]*?:/\\]/.test(name));
    assert.ok(!this.env.spreadsheet.sheets.some(sheet => sheet !== this && sheet.name === name));
    this.env.mutate('rename:' + name, () => { this.name = name; });
    return this;
  }
  deleteRows(start, count) {
    assert.ok(start >= 1 && count >= 1 && start + count - 1 <= this.rows && count < this.rows);
    this.env.mutate('deleteRows:' + this.name, () => {
      for (const map of [this.cells, this.formulas, this.notes]) {
        const shifted = [];
        for (const [key, value] of map) {
          const [row, column] = key.split(',').map(Number);
          if (row >= start) {
            map.delete(key);
            if (row >= start + count) shifted.push([(row - count) + ',' + column, value]);
          }
        }
        for (const [key, value] of shifted) map.set(key, value);
      }
      this.rows -= count;
    });
  }
  deleteColumns(start, count) {
    assert.equal(start + count - 1, this.cols);
    this.env.mutate('deleteColumns:' + this.name, () => {
      for (const key of this.cells.keys()) if (Number(key.split(',')[1]) >= start) this.cells.delete(key);
      this.cols -= count;
    });
  }
  insertRowsAfter(after, count) {
    assert.equal(after, this.rows);
    this.env.mutate('insertRows:' + this.name, () => { this.rows += count; });
  }
  insertColumnsAfter(after, count) {
    assert.equal(after, this.cols);
    this.env.mutate('insertColumns:' + this.name, () => { this.cols += count; });
  }
}

function environment({configured = true, occupiedIndex = false} = {}) {
  const env = {
    properties: new Map(), operations: [], mutationCount: 0, failAt: null, failBefore: false,
    nextChartId: 1000,
    locked: false, forceBusy: false, opened: [], onMutation: null,
    lockWaits: [], onLock: null, triggers: [], menus: [], toasts: [], logs: [],
    mutate(name, fn) {
      const n = ++this.mutationCount;
      this.operations.push(name);
      if (this.failAt === n && this.failBefore) throw new Error('RAW_SECRET_DO_NOT_LEAK');
      fn();
      if (this.onMutation) this.onMutation(name);
      if (this.failAt === n && !this.failBefore) throw new Error('RAW_SECRET_DO_NOT_LEAK');
    },
    resetFault(failAt = null, failBefore = false) {
      this.operations = []; this.mutationCount = 0; this.failAt = failAt; this.failBefore = failBefore;
    },
  };
  env.spreadsheet = {
    sheets: [], nextId: 100,
    getId() { return 'TEST_SPREADSHEET_ONLY'; },
    toast(...args) { env.toasts.push(args); },
    getSheets() { return this.sheets.slice(); },
    getSheetByName(name) { return this.sheets.find(sheet => sheet.name === name) || null; },
    insertSheet(name) {
      assert.ok(!this.getSheetByName(name));
      const sheet = new FakeSheet(env, name, this.nextId++);
      env.mutate('create:' + name, () => this.sheets.push(sheet));
      return sheet;
    },
  };
  env.spreadsheet.sheets.push(new FakeSheet(env, 'My notes', 1));
  if (occupiedIndex) {
    const sheet = new FakeSheet(env, 'Index', 2);
    sheet.cells.set('1,1', 'Do not replace my unrelated index');
    env.spreadsheet.sheets.push(sheet);
  }
  const context = vm.createContext({
    SpreadsheetApp: {
      getActiveSpreadsheet() { return env.spreadsheet; },
      getUi() { return {createMenu(name) {
        const menu = {name, items: [], addItem(label, handler) { this.items.push([label, handler]); return this; },
          addToUi() { env.menus.push(this); }};
        return menu;
      }}; },
      openById(id) { assert.equal(id, 'TEST_SPREADSHEET_ONLY'); env.opened.push(id); return env.spreadsheet; },
      flush() { env.mutate('flush', () => {}); },
      newDataValidation() { return {
        requireFormulaSatisfied(value) { this.formula = value; return this; },
        setAllowInvalid(value) { this.invalid = value; return this; },
        setHelpText(value) { this.help = value; return this; }, build() { return this; }}; },
    },
    Charts: {ChartType: {SCATTER: 'SCATTER', LINE: 'LINE'}, ChartHiddenDimensionStrategy: {SHOW_BOTH: 'SHOW_BOTH'}},
    PropertiesService: {getScriptProperties() { return {
      getProperty(key) { return env.properties.get(key) || null; },
      setProperty(key, value) { env.mutate('property:' + key, () => env.properties.set(key, value)); },
    }; }},
    LockService: {getScriptLock() { return {
      tryLock(timeout) {
        env.lockWaits.push(timeout);
        if (env.locked || env.forceBusy) return false;
        env.locked = true;
        if (env.onLock) env.onLock();
        return true;
      },
      releaseLock() { env.locked = false; },
    }; }},
    ScriptApp: {
      EventType: {ON_CHANGE: 'ON_CHANGE'}, TriggerSource: {SPREADSHEETS: 'SPREADSHEETS'},
      getProjectTriggers() { return env.triggers.slice(); },
      newTrigger(handler) { return {
        forSpreadsheet(spreadsheet) { this.sourceId = spreadsheet.getId(); return this; },
        onChange() { this.eventType = 'ON_CHANGE'; return this; },
        create() {
          const trigger = fakeTrigger(handler, this.sourceId, this.eventType);
          env.mutate('createTrigger', () => env.triggers.push(trigger));
          return trigger;
        },
      }; },
      deleteTrigger(trigger) {
        env.mutate('deleteTrigger', () => { env.triggers = env.triggers.filter(item => item !== trigger); });
      },
    },
    console: {log(...args) { env.logs.push(args.join(' ')); }},
    ContentService: {MimeType: {JSON: 'application/json'}, createTextOutput(text) { return {
      text, setMimeType(type) { this.type = type; return this; },
    }; }},
    Utilities: {DigestAlgorithm: {SHA_256: 'sha256'}, Charset: {UTF_8: 'utf8'},
      computeDigest(algorithm, text, charset) { return [...crypto.createHash(algorithm).update(text, charset).digest()]; }},
  });
  vm.runInContext(code, context, {filename: 'Code.gs'});
  env.context = context;
  env.post = body => {
    const text = typeof body === 'string' ? body : JSON.stringify(body);
    return JSON.parse(context.doPost({postData: {contents: text, type: 'application/json; charset=utf-8'},
      contentLength: Buffer.byteLength(text, 'utf8')}).text);
  };
  env.index = () => env.spreadsheet.getSheetByName('Index');
  env.scans = () => env.spreadsheet.sheets.filter(sheet => sheet.name.startsWith('_psm_') || sheet.cell(1, 2) === 'pocket-scan-mosfet/1');
  if (configured) {
    if (occupiedIndex) env.properties.set('POCKET_SCAN_SPREADSHEET_ID', 'TEST_SPREADSHEET_ONLY');
    else context.setupReceiver();
  }
  env.resetFault();
  return env;
}

function fakeTrigger(handler = 'onScannerChange', sourceId = 'TEST_SPREADSHEET_ONLY',
    eventType = 'ON_CHANGE', source = 'SPREADSHEETS') {
  return {getHandlerFunction: () => handler, getTriggerSourceId: () => sourceId,
    getEventType: () => eventType, getTriggerSource: () => source};
}

function assertComplete(env, scan, acknowledgement) {
  const body = JSON.stringify(scan);
  assert.deepEqual(acknowledgement, {ok: true, scan_id: scan.scan_id,
    sha256: crypto.createHash('sha256').update(body).digest('hex'), point_count: scan.point_count,
    sheet_id: env.scans()[0].getSheetId()});
  assert.equal(env.scans().length, 1);
  const sheet = env.scans()[0];
  assert.equal(sheet.cell(4, 2), 'complete');
  assert.equal(sheet.cell(2, 2), scan.scan_id);
  assert.ok(sheet.rows >= 15 + scan.point_count);
  assert.equal(sheet.cols, 59);
  assert.equal(sheet.charts.length, 2);
  assert.equal(sheet.cell(48, 20), 'Ready');
  assert.deepEqual(JSON.parse(JSON.stringify(sheet.getRange(16, 1, scan.point_count, scan.columns.length).getValues())), scan.points);
  assert.equal(env.index().getLastRow(), 2);
  assert.equal(env.index().rows, 2);
  assert.equal(env.index().cols, 12);
  assert.equal(env.index().cell(2, 1), scan.scan_id);
  assert.match(sheet.name, /^2026-11-01 15-04-05 [0-9a-f]{12}$/);
  assert.equal(env.locked, false);
}

test('manual setup binds one fixed private spreadsheet; GET exposes only health', () => {
  const env = environment({configured: false});
  assert.deepEqual(env.post(fixture()), {ok: false, error: 'receiver_not_configured'});
  assert.equal(env.operations.length, 0);
  env.context.setupReceiver();
  const health = JSON.parse(env.context.doGet().text);
  assert.deepEqual(health, {ok: true, service: 'pocket-scan-mosfet', schema_version: 1,
    settings_versions: [1, 2, 3], scan_schema_versions: [1, 2, 3, 4, 5]});
  env.context.SpreadsheetApp.getActiveSpreadsheet = () => { throw new Error('web handler must not use active sheet'); };
  assertComplete(env, fixture(), env.post(fixture()));
  assert.equal(env.opened.length, 1);
});

test('complete upload preserves numeric/boolean cells and exact UTF-8 checksum', () => {
  const env = environment();
  const scan = fixture();
  scan.calibration.metadata.label = 'synthetic µA 漏電 😀';
  assertComplete(env, scan, env.post(scan));
});

test('same-body retry is read-only and returns the original sheet ID', () => {
  const env = environment();
  const scan = fixture();
  const first = env.post(scan);
  env.resetFault();
  const second = env.post(scan);
  assert.deepEqual(second, first);
  assert.equal(env.operations.length, 0);
  assertComplete(env, scan, second);
});

test('same scan ID with changed value or just changed wire whitespace is rejected', () => {
  const env = environment();
  const scan = fixture();
  env.post(scan);
  const original = env.scans()[0].cell(16, 7);
  scan.points[0][6] = 0.00001;
  assert.deepEqual(env.post(scan), {ok: false, error: 'scan_conflict'});
  assert.deepEqual(env.post(JSON.stringify(fixture(), null, 2)), {ok: false, error: 'scan_conflict'});
  assert.equal(env.scans()[0].cell(16, 7), original);
  assert.equal(env.index().getLastRow(), 2);
});

test('malformed payloads fail before spreadsheet access or writes', () => {
  const changes = [
    x => { x.schema_version = 2; }, x => { x.scan_id = '../scan'; },
    x => { x.scan_id = 'a'.repeat(96) + '-b'; }, x => { x.device_id = ''; },
    x => { x.created_utc = '2026-02-30T12:00:00Z'; }, x => { x.created_utc = '2026-11-01T12:00:00'; },
    x => { x.outcome = 'cancelled'; }, x => { x.point_count = 2; },
    x => { x.duration_ms = -1; }, x => { x.duration_ms = 1.5; },
    x => { x.points = []; x.point_count = 0; }, x => { x.calibration.sha256 = 'not a hash'; },
    x => { x.calibration.metadata = []; }, x => { x.calibration.metadata.deep = {a:{b:{c:{d:{e:{f:{g:1}}}}}}}; },
    x => { x.calibration.metadata.text = 'x'.repeat(2049); },
    x => { x.settings.batches = true; }, x => { x.settings.settle_ms = 1; },
    x => { x.settings.driver_supply_v = 31; }, x => { x.settings.source_voltage_v = -1; },
    x => { x.settings.drain_step_v = 0; }, x => { x.settings.drain_stop_v = 6; },
    x => { x.columns.reverse(); }, x => { x.points[0].pop(); },
    x => { x.points[0][0] = 1; }, x => { x.points[0][1] = -1; },
    x => { x.points[0][2] = 37; }, x => { x.points[0][6] = '0.001'; },
    x => { x.points[0][8] = -1e-8; }, x => { x.points[0][14] = 5; },
    x => { x.points[0][15] = 1; }, x => { x.extra = 'unexpected'; },
  ];
  for (const change of changes) {
    const env = environment(); const scan = fixture(); change(scan);
    const result = env.post(scan);
    assert.equal(result.ok, false, String(change));
    assert.equal(env.operations.length, 0, String(change));
    assert.equal(env.opened.length, 0, String(change));
  }
  const env = environment();
  assert.deepEqual(env.post('{'), {ok: false, error: 'invalid_json'});
  assert.deepEqual(env.post(JSON.stringify(fixture()).replace('12000', '1e999')), {ok: false, error: 'invalid_scan'});
});

test('UTF-8 byte limit, request MIME and invalid Unicode are enforced', () => {
  const env = environment();
  assert.deepEqual(env.post(' '.repeat(2 * 1024 * 1024 + 1)), {ok: false, error: 'payload_too_large'});
  const tooManyBytes = 'é'.repeat(1024 * 1024 + 1);
  // Omit the length hint so the actual UTF-8 accounting is exercised.
  let result = JSON.parse(env.context.doPost({postData: {type: 'application/json', contents: tooManyBytes}}).text);
  assert.deepEqual(result, {ok: false, error: 'payload_too_large'});
  result = JSON.parse(env.context.doPost({postData: {type: 'text/plain', contents: JSON.stringify(fixture())}}).text);
  assert.deepEqual(result, {ok: false, error: 'invalid_request'});
  assert.deepEqual(env.post('\ud800'), {ok: false, error: 'invalid_request'});
  assert.equal(env.operations.length, 0);
});

test('strings cannot become spreadsheet formulas; snapshots remain JSON', () => {
  const env = environment(); const scan = fixture();
  scan.device_id = ' \t=IMPORTXML("https://invalid.invalid", "//x")';
  scan.calibration.metadata.label = '=IMPORTXML("https://invalid.invalid", "//x")';
  scan.settings.annotation = '+1+1';
  assert.equal(env.post(scan).ok, true);
  assert.equal(env.scans()[0].cell(5, 2), "'" + scan.device_id);
  assert.equal(env.index().cell(2, 7), "'" + scan.device_id);
  assert.deepEqual(JSON.parse(env.scans()[0].cell(13, 2)), scan.calibration.metadata);
  for (const sheet of env.spreadsheet.sheets) for (const [key, value] of sheet.cells) {
    if (sheet !== env.index() && key === '3,4') continue; // Receiver-owned navigation, tested separately.
    if (Number(key.split(',')[1]) <= 17 && typeof value === 'string') {
      assert.equal(/^[\s\u0000-\u001f]*[=+\-@]/.test(value), false);
    }
  }
});

test('busy and concurrent requests make no duplicate scan or Index entry', () => {
  const env = environment(); env.forceBusy = true;
  assert.deepEqual(env.post(fixture()), {ok: false, error: 'receiver_busy'});
  assert.equal(env.operations.length, 0);
  env.forceBusy = false;
  let nested;
  env.onMutation = () => { env.onMutation = null; nested = env.post(fixture()); };
  const result = env.post(fixture());
  assert.deepEqual(nested, {ok: false, error: 'receiver_busy'});
  assertComplete(env, fixture(), result);
});

test('before/after failures at every mutation recover exactly one committed scan', () => {
  const baseline = environment(); baseline.post(fixture());
  const operations = baseline.operations.slice();
  assert.ok(operations.length > 10);
  for (const before of [true, false]) for (let failAt = 1; failAt <= operations.length; failAt++) {
    const env = environment(); env.resetFault(failAt, before);
    const first = env.post(fixture());
    // Presentation failures are repairable without withholding a committed scan ACK.
    if (!first.ok) assert.deepEqual(first, {ok: false, error: 'storage_error'}, `${before}/${failAt}/${operations[failAt - 1]}`);
    assert.equal(env.locked, false);
    // An Index row may exist before its acknowledgement, but never before
    // the complete point table has been written and flushed.
    if (env.index().getLastRow() === 2) {
      assert.equal(env.scans().length, 1);
      assert.ok(['ready', 'complete'].includes(env.scans()[0].cell(4, 2)));
      assert.deepEqual(JSON.parse(JSON.stringify(env.scans()[0].getRange(16, 1, 3, columns.length).getValues())), fixture().points);
    }
    env.resetFault();
    const retry = env.post(fixture());
    assertComplete(env, fixture(), retry);
  }
});

test('empty stage still reserves the original body hash before any cell is written', () => {
  const env = environment(); env.resetFault(1, false); // insertSheet succeeds, then execution ends.
  assert.equal(env.post(fixture()).ok, false);
  assert.equal(env.scans().length, 1);
  assert.equal(env.scans()[0].getLastRow(), 0);
  env.resetFault();
  const changed = fixture(); changed.duration_ms++;
  assert.deepEqual(env.post(changed), {ok: false, error: 'scan_conflict'});
  assertComplete(env, fixture(), env.post(fixture()));
});

test('unrelated existing Index is not overwritten', () => {
  const env = environment({occupiedIndex: true});
  assert.deepEqual(env.post(fixture()), {ok: false, error: 'index_conflict'});
  assert.equal(env.index().cell(1, 1), 'Do not replace my unrelated index');
  assert.equal(env.operations.length, 0);
});

test('unconfigured Index creation also recovers after each setup mutation', () => {
  const probe = environment({configured: false});
  probe.properties.set('POCKET_SCAN_SPREADSHEET_ID', 'TEST_SPREADSHEET_ONLY');
  probe.post(fixture());
  const operations = probe.operations.slice();
  const createScan = operations.findIndex(name => name.startsWith('create:_psm_'));
  for (let failAt = 1; failAt <= createScan; failAt++) {
    const env = environment({configured: false});
    env.properties.set('POCKET_SCAN_SPREADSHEET_ID', 'TEST_SPREADSHEET_ONLY');
    env.resetFault(failAt, false);
    assert.equal(env.post(fixture()).ok, false);
    env.resetFault();
    assertComplete(env, fixture(), env.post(fixture()));
  }
});

test('4096 point boundary uses bounded chart grids and batch writes', () => {
  const env = environment(); const scan = fixture();
  scan.point_count = 4096;
  scan.settings.drain_stop_v = 4.095; scan.settings.drain_step_v = 0.001;
  scan.points = Array.from({length: 4096}, (_, index) => {
    const row = fixture().points[0]; row[0] = index;
    row[5] = index % 2 ? 0.06 : 0;
    return row;
  });
  assertComplete(env, scan, env.post(scan));
  assert.ok(env.operations.filter(name => name.startsWith('setValues:')).length < 20);
  assert.equal(env.scans()[0].rows, 4111);
  assert.equal(env.scans()[0].cols, 59);
  assert.equal(env.context.chartPlan_(scan.points).offTargetRows.length, 2048);
  assert.equal(env.scans()[0].charts[0].ranges[0].cols, 3);
  const bad = fixture(); bad.points = Array(4097).fill(bad.points[0]); bad.point_count = 4097;
  assert.deepEqual(environment().post(bad), {ok: false, error: 'invalid_scan'});
});

test('unknown UTC receives a server timestamp and independent scans keep separate tabs', () => {
  const env = environment(); const first = fixture(); first.created_utc = null;
  const a = env.post(first);
  assert.equal(a.ok, true);
  assert.match(env.scans()[0].cell(7, 2), /^\d{4}-\d\d-\d\dT/);
  const second = fixture(); second.scan_id = '3b8922c9b91bbe9f-' + 'ff'.repeat(16);
  const b = env.post(second);
  assert.equal(b.ok, true);
  assert.notEqual(a.sheet_id, b.sheet_id);
  assert.equal(env.scans().length, 2);
  assert.equal(env.index().rows, 3);
});

test('owner-added Index annotations are preserved when appending another scan', () => {
  const env = environment();
  env.post(fixture());
  env.index().insertColumnsAfter(12, 1);
  env.index().getRange(1, 13).setValue('My notes');
  env.index().getRange(2, 13).setValue('Keep this annotation');
  const second = fixture(); second.scan_id = 'abcd-0123456789';
  assert.equal(env.post(second).ok, true);
  assert.equal(env.index().cell(2, 13), 'Keep this annotation');
});

const reorderedIndexHeaders = ['created_utc', 'scan_url', 'scan_id', 'sha256',
  'point_count', 'sheet_id', 'received_utc', 'device_id', 'duration_ms', 'outcome',
  'settings_json', 'calibration_sha256'];

function reorderIndex(env, headers) {
  const sheet = env.index();
  const rows = sheet.getRange(1, 1, sheet.getLastRow(), 12).getValues();
  const positions = headers.map(name => rows[0].indexOf(name));
  sheet.getRange(1, 1, rows.length, 12).setValues(rows.map(row => positions.map(position => row[position])));
}

test('existing scan retries remain read-only after required Index columns are reordered', () => {
  for (const headers of [reorderedIndexHeaders, reorderedIndexHeaders.slice().reverse()]) {
    const env = environment();
    const scan = fixture();
    const first = env.post(scan);
    reorderIndex(env, headers);
    const before = env.index().getRange(1, 1, 2, 12).getValues();
    env.resetFault();
    assert.deepEqual(env.post(scan), first);
    assert.equal(env.operations.length, 0);
    assert.deepEqual(env.index().getRange(1, 1, 2, 12).getValues(), before);
    assert.equal(env.scans().length, 1);
    const changed = fixture(); changed.duration_ms++;
    assert.deepEqual(env.post(changed), {ok: false, error: 'scan_conflict'});
    assert.equal(env.operations.length, 0);
  }
});

test('new Index entries follow reordered headers while preserving existing layout and owner notes', () => {
  const env = environment();
  env.post(fixture());
  reorderIndex(env, reorderedIndexHeaders);
  const index = env.index();
  index.insertColumnsAfter(12, 2);
  index.insertRowsAfter(index.rows, 8);
  index.getRange(1, 13, 2, 2).setValues([['Owner notes', 'Other'], ['Keep this note', 'Unchanged']]);
  index.notes.set('2,13', 'Preserve the cell note too');
  const before = index.getRange(1, 1, 2, 14).getValues();
  const second = fixture();
  second.scan_id = 'abcd-112233445566';
  second.created_utc = null;
  second.duration_ms = 4321;
  const result = env.post(second);
  assert.equal(result.ok, true);
  const sheet = env.scans().find(item => item.getSheetId() === result.sheet_id);
  const expected = {scan_id: second.scan_id, sha256: result.sha256,
    point_count: second.point_count, sheet_id: result.sheet_id, created_utc: '',
    received_utc: sheet.cell(7, 2), device_id: second.device_id,
    duration_ms: second.duration_ms, outcome: 'complete',
    scan_url: 'https://docs.google.com/spreadsheets/d/TEST_SPREADSHEET_ONLY/edit#gid=' + result.sheet_id,
    settings_json: JSON.stringify(second.settings), calibration_sha256: second.calibration.sha256};
  reorderedIndexHeaders.forEach((name, column) => assert.equal(index.cell(3, column + 1), expected[name], name));
  assert.deepEqual(index.getRange(1, 1, 2, 14).getValues(), before);
  assert.equal(index.notes.get('2,13'), 'Preserve the cell note too');
  assert.equal(index.rows, 10);
  assert.equal(index.cols, 14);
  env.resetFault();
  assert.deepEqual(env.post(second), result);
  assert.equal(env.operations.length, 0);
});

test('unknown missing duplicate and undersized Index headers still fail without mutation', () => {
  for (const mode of ['unknown', 'missing', 'duplicate', 'undersized']) {
    const env = environment();
    env.post(fixture());
    reorderIndex(env, reorderedIndexHeaders);
    if (mode === 'undersized') env.index().deleteColumns(12, 1);
    else env.index().getRange(1, 1).setValue(mode === 'unknown' ? 'unrecognized' :
      mode === 'missing' ? '' : reorderedIndexHeaders[1]);
    const cells = new Map(env.index().cells);
    env.resetFault();
    assert.deepEqual(env.post(fixture()), {ok: false, error: 'index_conflict'}, mode);
    assert.equal(env.operations.length, 0, mode);
    assert.deepEqual(env.index().cells, cells, mode);
    assert.equal(env.scans().length, 1);
  }
});

function maintenanceFixture(count = 3) {
  const env = environment();
  env.uploads = Array.from({length: count}, (_, i) => {
    const scan = fixture(); scan.scan_id = 'abcd-' + i.toString(16).padStart(8, '0');
    const ack = env.post(scan);
    assert.equal(ack.ok, true);
    return {scan, ack, sheet: env.scans().find(sheet => sheet.id === ack.sheet_id)};
  });
  env.removeTab = sheet => { env.spreadsheet.sheets = env.spreadsheet.sheets.filter(item => item !== sheet); };
  env.change = changeType => env.context.onScannerChange({changeType, source: env.spreadsheet});
  env.resetFault();
  return env;
}

test('deleting multiple tabs prunes bottom-up and preserves surviving reordered rows and annotations', () => {
  const env = maintenanceFixture(5);
  reorderIndex(env, reorderedIndexHeaders);
  const index = env.index();
  index.insertColumnsAfter(12, 2);
  index.getRange(1, 13, 1, 2).setValues([['Owner notes', 'Formula']]);
  for (let row = 2; row <= 6; row++) {
    index.getRange(row, 13).setValue('Note ' + row);
    index.getRange(row, 14).setFormula('=1+1');
    index.notes.set(row + ',13', 'Cell note ' + row);
  }
  const kept = [3, 5].map(row => index.getRange(row, 1, 1, 14).getValues()[0]);
  for (const i of [0, 2, 4]) env.removeTab(env.uploads[i].sheet);
  env.resetFault();
  const deletedRows = [];
  const originalDelete = index.deleteRows.bind(index);
  index.deleteRows = (row, count) => {
    assert.equal(env.locked, true);
    deletedRows.push(row); originalDelete(row, count);
  };
  const result = env.change('REMOVE_GRID');
  assert.equal(result.removed, 3);
  assert.equal(result.skipped, 0);
  assert.deepEqual(deletedRows, [6, 4, 2]);
  assert.deepEqual(index.getRange(2, 1, 2, 14).getValues(), kept);
  assert.equal(index.notes.get('2,13'), 'Cell note 3');
  assert.equal(index.notes.get('3,13'), 'Cell note 5');
  assert.equal(index.formulas.get('2,14'), '=1+1');
  assert.equal(index.formulas.get('3,14'), '=1+1');
  assert.equal(index.cols, 14);
  assert.equal(env.lockWaits.at(-1), 60000);
  assert.equal(env.locked, false);
  env.resetFault();
  assert.equal(env.change('REMOVE_GRID').removed, 0);
  assert.equal(env.operations.length, 0);
});

test('renames and sheet reordering preserve Index entries using stable IDs', () => {
  const env = maintenanceFixture();
  env.uploads[0].sheet.setName('My renamed transistor scan');
  env.spreadsheet.sheets.reverse();
  const before = new Map(env.index().cells);
  env.resetFault();
  assert.equal(env.change('REMOVE_GRID').removed, 0);
  assert.deepEqual(env.index().cells, before);
  assert.equal(env.operations.length, 0);
  assert.deepEqual(env.post(env.uploads[0].scan), env.uploads[0].ack);
});

test('change handler ignores unrelated events and foreign or missing sources', () => {
  const env = maintenanceFixture(1);
  env.removeTab(env.uploads[0].sheet);
  const waits = env.lockWaits.length;
  for (const changeType of ['EDIT', 'OTHER', 'INSERT_GRID', 'REMOVE_ROW']) env.change(changeType);
  for (const event of [undefined, {}, {changeType: 'REMOVE_GRID'},
    {changeType: 'REMOVE_GRID', source: {getId: () => 'FOREIGN'}}]) env.context.onScannerChange(event);
  assert.equal(env.index().getLastRow(), 2);
  assert.equal(env.lockWaits.length, waits);
  assert.equal(env.operations.length, 0);
  env.context.SpreadsheetApp.getActiveSpreadsheet = () => ({getId: () => 'FOREIGN'});
  assert.throws(() => env.context.refreshIndex(), /receiver_wrong_spreadsheet/);
  assert.throws(() => env.context.setupIndexMaintenance(), /receiver_wrong_spreadsheet/);
  assert.equal(env.operations.length, 0);
});

test('missing Index is not recreated and foreign Index is rejected unchanged', () => {
  const env = maintenanceFixture(1);
  env.removeTab(env.index());
  assert.equal(env.context.refreshIndex().indexMissing, true);
  assert.equal(env.change('REMOVE_GRID').indexMissing, true);
  assert.equal(env.index(), null);
  assert.match(env.toasts[0][0], /Index is missing/);
  assert.equal(env.operations.length, 0);
  const foreign = environment({occupiedIndex: true});
  const before = new Map(foreign.index().cells);
  assert.throws(() => foreign.context.refreshIndex(), /index_conflict/);
  assert.throws(() => foreign.context.onScannerChange({changeType: 'REMOVE_GRID', source: foreign.spreadsheet}), /index_conflict/);
  assert.deepEqual(foreign.index().cells, before);
  assert.equal(foreign.operations.length, 0);
  assert.equal(foreign.locked, false);
});

test('cleanup leaves malformed, formula-derived and unrelated rows unchanged', () => {
  const corruptions = [
    index => index.cells.set('2,1', 'Owner heading'),
    index => index.cells.set('2,2', 'not-a-digest'),
    index => index.cells.set('2,4', String(index.cell(2, 4))),
    index => index.cells.set('2,4', -1),
    index => index.cells.set('2,10', 'https://example.com/user-link'),
    index => index.formulas.set('2,4', '=100+1'),
  ];
  for (const corrupt of corruptions) {
    const env = maintenanceFixture(1);
    env.removeTab(env.uploads[0].sheet);
    corrupt(env.index());
    const before = new Map(env.index().cells);
    const result = env.context.refreshIndex();
    assert.equal(result.removed, 0);
    assert.equal(result.skipped, 1);
    assert.deepEqual(env.index().cells, before);
    assert.equal(env.operations.length, 0);
    assert.match(env.toasts[0][0], /unrecognized row/);
  }
});

test('owner formatting and annotations outside stable identity do not prevent cleanup', () => {
  const env = maintenanceFixture(1), index = env.index();
  env.removeTab(env.uploads[0].sheet);
  index.cells.set('2,5', new Date('2026-11-01T15:04:05Z'));
  index.cells.set('2,6', 'Sunday afternoon');
  index.cells.set('2,11', 'Owner annotated settings');
  index.formulas.set('2,8', '=12000');
  const result = env.context.refreshIndex();
  assert.equal(result.removed, 1);
  assert.equal(result.skipped, 0);
  assert.equal(index.getLastRow(), 1);
});

test('cleanup skips blank managed cells while preserving owner-only rows', () => {
  const env = maintenanceFixture(1), index = env.index();
  index.insertColumnsAfter(12, 1);
  index.insertRowsAfter(index.rows, 2);
  index.getRange(4, 13).setValue('Independent owner note');
  env.removeTab(env.uploads[0].sheet);
  const result = env.context.refreshIndex();
  assert.equal(result.removed, 1);
  assert.equal(result.skipped, 0);
  assert.equal(index.cell(3, 13), 'Independent owner note');
  assert.equal(index.getLastRow(), 3);
});

test('busy maintenance leaves rows and triggers untouched and snapshots after acquiring upload lock', () => {
  const env = maintenanceFixture(1);
  env.removeTab(env.uploads[0].sheet);
  env.forceBusy = true;
  for (const action of [() => env.change('REMOVE_GRID'), () => env.context.refreshIndex(),
    () => env.context.setupIndexMaintenance()]) assert.throws(action, /receiver_busy/);
  assert.equal(env.operations.length, 0);
  assert.equal(env.index().getLastRow(), 2);
  assert.equal(env.triggers.length, 0);
  env.forceBusy = false;
  env.onLock = () => { env.spreadsheet.sheets.push(env.uploads[0].sheet); };
  assert.equal(env.context.refreshIndex().removed, 0);
  assert.equal(env.operations.length, 0);
});

test('interrupted cleanup can retry before or after each committed deletion', () => {
  for (const before of [true, false]) for (const failAt of [1, 2, 3, 4]) {
    const env = maintenanceFixture(4);
    for (const i of [0, 1, 3]) env.removeTab(env.uploads[i].sheet);
    env.resetFault(failAt, before);
    assert.throws(() => env.context.refreshIndex());
    assert.equal(env.locked, false);
    env.resetFault();
    env.context.refreshIndex();
    assert.equal(env.index().getLastRow(), 2);
    assert.equal(env.index().cell(2, 1), env.uploads[2].scan.scan_id);
    assert.equal(env.scans().length, 1);
  }
});

test('upload recovery prunes deleted tabs after validation and an identical retry may recreate one', () => {
  const env = maintenanceFixture(2);
  env.removeTab(env.uploads[0].sheet);
  const invalid = fixture(); invalid.point_count = 99;
  assert.equal(env.post(invalid).ok, false);
  assert.equal(env.operations.length, 0);
  assert.equal(env.index().getLastRow(), 3);
  const retry = env.post(env.uploads[0].scan);
  assert.equal(retry.ok, true);
  assert.notEqual(retry.sheet_id, env.uploads[0].ack.sheet_id);
  assert.equal(env.index().getLastRow(), 3);
  assert.equal(env.index().cell(2, 1), env.uploads[1].scan.scan_id);
  assert.equal(env.index().cell(3, 1), env.uploads[0].scan.scan_id);
});

test('setup installs one matching change trigger and preserves unrelated triggers on repetition', () => {
  const env = maintenanceFixture(1);
  const unrelated = [fakeTrigger('differentHandler'), fakeTrigger('onScannerChange', 'FOREIGN'),
    fakeTrigger('onScannerChange', 'TEST_SPREADSHEET_ONLY', 'ON_EDIT'),
    fakeTrigger('onScannerChange', 'TEST_SPREADSHEET_ONLY', 'ON_CHANGE', 'CLOCK')];
  env.triggers.push(...unrelated);
  env.removeTab(env.uploads[0].sheet);
  const result = env.context.setupIndexMaintenance();
  assert.equal(result.removed, 1);
  assert.equal(env.triggers.length, 5);
  assert.ok(unrelated.every(trigger => env.triggers.includes(trigger)));
  assert.equal(env.locked, false);
  assert.match(env.logs[0], /removed 1/);
  assert.doesNotMatch(env.logs.join(' '), /TEST_SPREADSHEET_ONLY|https:|abcd-/);
  env.resetFault();
  env.context.setupIndexMaintenance();
  assert.equal(env.triggers.length, 5);
  assert.equal(env.operations.length, 0);
  const duplicate = fakeTrigger(); env.triggers.push(duplicate);
  env.context.setupIndexMaintenance();
  assert.equal(env.triggers.length, 5);
  assert.ok(!env.triggers.includes(duplicate));
  assert.ok(unrelated.every(trigger => env.triggers.includes(trigger)));
});

test('interrupted trigger creation retries without duplication and onOpen adds manual menu', () => {
  const env = maintenanceFixture(1);
  env.resetFault(1); // Trigger is created, then execution is interrupted.
  assert.throws(() => env.context.setupIndexMaintenance());
  assert.equal(env.triggers.length, 1);
  assert.equal(env.locked, false);
  env.resetFault();
  env.context.setupIndexMaintenance();
  assert.equal(env.triggers.length, 1);
  assert.equal(env.operations.length, 0);
  env.context.onOpen();
  assert.equal(env.menus.length, 1);
  assert.equal(env.menus[0].name, 'Pocket MOSFET');
  assert.deepEqual(env.menus[0].items, [['Refresh Index', 'refreshIndex']]);
});

test('float32 wire rounding does not impose exact requested-grid equality', () => {
  const env = environment(); const scan = fixture();
  scan.settings.source_voltage_v = 4.9000001;
  scan.settings.drain_start_v = 0; scan.settings.drain_stop_v = 0.100000001; scan.settings.drain_step_v = 0.05;
  scan.settings.gate_start_v = -4.9000001; scan.settings.gate_stop_v = -4.9000001;
  for (let i = 0; i < scan.points.length; i++) scan.points[i][2] = Math.fround(i * 0.05);
  assert.equal(env.post(scan).ok, true);
});

test('charts preserve measured X per curve and use formula-based absolute leakage maxima', () => {
  const env = environment(); const scan = fixture();
  scan.point_count = 4;
  scan.settings.drain_stop_v = 1; scan.settings.drain_step_v = 1;
  scan.settings.gate_stop_v = 1; scan.settings.gate_step_v = 1;
  scan.points = [0, 1, 2, 3].map(index => {
    const row = fixture().points[0].slice();
    row[0] = index; row[1] = index < 2 ? 0 : 1; row[3] = row[1];
    row[4] = [0.11, 0.95, 0.12, 0.95][index];
    row[5] = [0.001, 0.002, 0.99, 1.01][index];
    row[7] = [2e-8, -9e-8, -4e-8, 4e-8][index];
    return row;
  });
  assertComplete(env, scan, env.post(scan));
  const sheet = env.scans()[0];
  assert.equal(sheet.cell(2, 42), '=$E$16');
  assert.equal(sheet.cell(4, 42), '=$E$18');
  assert.match(sheet.cell(2, 43), /\$G\$16\*1E3/);
  assert.match(sheet.cell(1, 44), /VGS target.*\+2/);
  assert.match(sheet.cell(2, 35), /^=MAX\(ARRAYFORMULA\(IF\(\$B\$16:\$B\$17=\$AG2,ABS\(\$H\$16:\$H\$17\),-1\)\)\)$/);
  assert.match(sheet.cell(2, 36), /^=MATCH\(\$AI2,.*ABS.*0\)\+15$/);
  assert.equal(sheet.cell(2, 37), '=INDEX($F:$F,$AJ2)');
  assert.equal(sheet.cell(2, 38), '=$AI2*1E6');
  assert.equal(sheet.cell(2, 39), '=INDEX($E:$E,$AJ2)');
  assert.equal(sheet.cell(2, 40), '=INDEX($A:$A,$AJ2)');
  assert.equal(sheet.charts[0].ranges[0].cols, 4);
  assert.equal(sheet.charts[1].ranges[0].col, 37);
  assert.equal(sheet.charts[1].ranges[0].rows, 3);
  assert.equal(sheet.charts[0].type, 'LINE');
  assert.equal(sheet.charts[1].type, 'SCATTER');
  assert.ok(sheet.charts.every(chart => chart.headers === 1 && !chart.transpose));
  assert.ok(sheet.charts.every(chart => chart.options.pointSize > 0 && chart.options.lineWidth > 0));
  assert.ok(sheet.charts.every(chart => !Object.hasOwn(chart.options, 'chartArea')));
});

test('all 4096 gate curves remain accessible through bounded 16-series pages', () => {
  const env = environment(); const scan = fixture();
  scan.point_count = 4096;
  scan.settings.drain_start_v = scan.settings.drain_stop_v = 0.5;
  scan.settings.gate_stop_v = 4.095; scan.settings.gate_step_v = 0.001;
  scan.points = Array.from({length: 4096}, (_, index) => {
    const row = fixture().points[0].slice(); row[0] = row[1] = index;
    row[3] = row[5] = index / 1000; return row;
  });
  assertComplete(env, scan, env.post(scan));
  const sheet = env.scans()[0];
  assert.equal(sheet.rows, 4111); assert.equal(sheet.cols, 59);
  assert.equal(sheet.charts[0].ranges[0].cols, 18);
  assert.equal(sheet.charts[1].ranges[0].rows, 4097);
  assert.match(sheet.cell(4097, 33), /=\$B\$4111/);
  assert.match(sheet.cell(2, 43), /INT\(\$U\$1\)/);
  sheet.getRange(1, 21).setValue(256);
  env.resetFault();
  assert.equal(env.post(scan).ok, true);
  assert.equal(sheet.cell(1, 21), 256);
  assert.equal(env.operations.length, 0);
});

test('gate classification is inclusive at both 20mV edges and independent of limit flags', () => {
  const env = environment();
  for (const target of [1, -1, 0]) {
    for (const [measured, expected] of [[target, true], [target - 0.02, true],
      [target + 0.02, true], [target - 0.020001, false], [target + 0.020001, false]]) {
      const point = fixture().points[0].slice();
      point[3] = target; point[5] = measured;
      for (const flag of [false, true]) {
        point[15] = point[16] = flag;
        assert.equal(env.context.gateOnTarget_(point), expected, `${target} -> ${measured}, flag=${flag}`);
      }
    }
  }
});

test('drain chart keeps every measured point while separating both gate miss directions from line series', () => {
  const env = environment(); const scan = fixture();
  const gate = [1, 1, 0.94, 1, 1, 1.06, 1];
  scan.point_count = gate.length;
  scan.settings.drain_stop_v = 3;
  scan.settings.gate_start_v = scan.settings.gate_stop_v = 1;
  scan.points = gate.map((voltage, index) => {
    const row = fixture().points[0].slice();
    row[0] = index; row[1] = 0; row[2] = index * 0.5; row[3] = 1;
    row[4] = [0.01, 0.46, 1.1, 1.4, 2.2, 2.4, 2.9][index];
    row[5] = voltage; row[6] = (index + 1) * 0.0001;
    row[7] = index === 2 ? -0.000002 : 0.00000001;
    row[15] = row[16] = false;
    return row;
  });
  assertComplete(env, scan, env.post(scan));
  const sheet = env.scans()[0], plan = env.context.chartPlan_(scan.points);
  assert.deepEqual(Array.from(plan.offTargetRows), [18, 21]);
  assert.deepEqual(Array.from(plan.rows), [16, 17, 18, 19, 20, 21, 22]);
  const eligibleSegments = scan.points.slice(1).flatMap((point, index) =>
    env.context.gateOnTarget_(point) && env.context.gateOnTarget_(scan.points[index]) ? [[index, index + 1]] : []);
  assert.deepEqual(eligibleSegments, [[0, 1], [3, 4]]);
  for (let index = 0; index < gate.length; index++) {
    const sourceRow = 16 + index, helperRow = index + 2;
    assert.equal(sheet.cell(helperRow, 42), `=$E$${sourceRow}`);
    const line = sheet.cell(helperRow, 43), marker = sheet.cell(helperRow, 44);
    for (const formula of [line, marker]) {
      assert.ok(formula.includes(`$F$${sourceRow}>=$D$${sourceRow}-0.02`), formula);
      assert.ok(formula.includes(`$F$${sourceRow}<=$D$${sourceRow}+0.02`), formula);
      assert.ok(formula.includes(`$G$${sourceRow}*1E3`), formula);
      assert.ok(formula.includes('""'), formula);
    }
    assert.ok(!line.includes('NOT('));
    assert.ok(marker.includes('NOT(AND('));
    assert.ok(marker.includes('INT($U$1)=1'));
  }
  const chart = sheet.charts[0];
  assert.equal(chart.type, 'LINE');
  assert.equal(chart.options.interpolateNulls, false);
  assert.equal(chart.options.treatLabelsAsText, false);
  assert.equal(chart.options.useFirstColumnAsDomain, true);
  assert.equal(chart.options.series[1].lineWidth, 0);
  assert.equal(chart.options.series[1].color, '#D98B00');
  assert.ok(chart.options.series[1].pointSize > 0);
  // The worst leakage point is off-target but remains part of the full sweep.
  assert.match(sheet.cell(2, 35), /ABS\(\$H\$16:\$H\$22\)/);
  assert.equal(sheet.cell(2, 37), '=INDEX($F:$F,$AJ2)');
  assert.deepEqual(sheet.getRange(16, 1, scan.point_count, columns.length).getValues(), scan.points);
});

test('all-off-target curves retain marker data and the last partial page includes its misses', () => {
  const env = environment(); const scan = fixture();
  scan.point_count = 17;
  scan.settings.drain_start_v = scan.settings.drain_stop_v = 0.5;
  scan.settings.gate_start_v = 0; scan.settings.gate_stop_v = 1.6; scan.settings.gate_step_v = 0.1;
  scan.points = Array.from({length: 17}, (_, index) => {
    const row = fixture().points[0].slice();
    row[0] = row[1] = index; row[2] = 0.5; row[3] = index / 10;
    row[4] = 0.48 + index / 1000; row[5] = row[3] + 0.06;
    row[15] = row[16] = false;
    return row;
  });
  assertComplete(env, scan, env.post(scan));
  const sheet = env.scans()[0], plan = env.context.chartPlan_(scan.points);
  assert.equal(plan.pages, 2); assert.equal(plan.series, 16);
  assert.equal(plan.offTargetRows.length, 17);
  assert.equal(plan.rowPages[31], 1); assert.equal(plan.rowPages[32], 2);
  assert.equal(sheet.charts[0].ranges[0].cols, 18);
  assert.equal(sheet.charts[0].options.series[16].lineWidth, 0);
  assert.ok(sheet.cell(18, 59).includes('INT($U$1)=2'));
  assert.ok(sheet.cell(18, 59).includes('$G$32*1E3'));
  sheet.getRange(1, 21).setValue(2);
  env.resetFault();
  assert.equal(env.post(scan).ok, true);
  assert.equal(sheet.cell(1, 21), 2);
  assert.equal(env.operations.length, 0);
});

function legacyChartVersion3(env, sheet) {
  const metadata = sheet.metadata.find(item => item.getKey() === 'POCKET_SCAN_CHARTS');
  const saved = JSON.parse(metadata.getValue());
  saved.version = 3;
  delete saved.lastColumn;
  metadata.setValue(JSON.stringify(saved));
  const drain = sheet.charts.find(chart => chart.id === saved.drain);
  drain.type = 'SCATTER';
  const markerColumn = drain.ranges[0].col + drain.ranges[0].cols - 1;
  for (const key of sheet.cells.keys()) if (Number(key.split(',')[1]) === markerColumn) sheet.cells.delete(key);
  drain.ranges[0].cols--;
  for (const map of [sheet.cells, sheet.formulas, sheet.notes]) {
    for (const key of map.keys()) if (Number(key.split(',')[1]) === 59) map.delete(key);
  }
  return saved;
}

test('version3 migration replaces only managed charts and preserves measurements annotations and idempotence', () => {
  const env = environment(); const scan = fixture();
  const ack = env.post(scan), sheet = env.scans()[0];
  const previous = legacyChartVersion3(env, sheet);
  sheet.insertColumnsAfter(sheet.cols, 60 - sheet.cols);
  sheet.getRange(5, 60).setValue('Keep owner annotation');
  sheet.notes.set('3,60', 'Keep owner note');
  // Same title is insufficient to claim a chart that uses owner-selected data.
  sheet.insertChart(sheet.newChart().setOption('title', 'Drain current vs VDS')
    .addRange(sheet.getRange(16, 5, 3, 2)).build());
  const ownerId = sheet.charts.at(-1).id;
  const raw = sheet.getRange(1, 1, 18, 17).getValues();
  env.resetFault(); env.context.addScanCharts();
  assert.deepEqual(sheet.getRange(1, 1, 18, 17).getValues(), raw);
  assert.equal(sheet.cell(5, 60), 'Keep owner annotation');
  assert.equal(sheet.notes.get('3,60'), 'Keep owner note');
  assert.equal(sheet.charts.length, 3);
  assert.ok(sheet.charts.some(chart => chart.id === ownerId));
  assert.ok(sheet.charts.every(chart => ![previous.drain, previous.leakage].includes(chart.id)));
  assert.equal(JSON.parse(sheet.metadata[0].getValue()).version, 4);
  assert.equal(JSON.parse(sheet.metadata[0].getValue()).state, 'complete');
  env.resetFault(); env.context.addScanCharts();
  assert.deepEqual(env.post(scan), ack);
  assert.equal(env.operations.length, 0);
});

test('version3 migration refuses newly claimed BG owner values formulas and notes without losing old charts', () => {
  for (const kind of ['cells', 'formulas', 'notes']) {
    const env = environment(); const scan = fixture();
    const ack = env.post(scan), sheet = env.scans()[0];
    const previous = legacyChartVersion3(env, sheet);
    const value = kind === 'formulas' ? '=IF(TRUE,"","Owner formula")' : 'Keep owner content';
    sheet[kind].set('2,59', value);
    const raw = sheet.getRange(1, 1, 18, 17).getValues();
    env.resetFault();
    assert.throws(() => env.context.addScanCharts(), /measurement data is unchanged/);
    assert.equal(sheet[kind].get('2,59'), value);
    assert.deepEqual(sheet.getRange(1, 1, 18, 17).getValues(), raw);
    assert.deepEqual(sheet.charts.map(chart => chart.id), [previous.drain, previous.leakage]);
    assert.equal(JSON.parse(sheet.metadata[0].getValue()).version, 3);
    assert.deepEqual(env.post(scan), ack);
    assert.equal(sheet[kind].get('2,59'), value);
  }
});

test('version3 migration recovers from every interrupted mutation without duplicate charts or raw changes', () => {
  const scan = fixture();
  const baseline = environment(); baseline.post(scan);
  legacyChartVersion3(baseline, baseline.scans()[0]);
  baseline.resetFault(); baseline.context.addScanCharts();
  const mutations = baseline.operations.length;
  for (const before of [true, false]) for (let at = 1; at <= mutations; at++) {
    const env = environment(); const ack = env.post(scan), sheet = env.scans()[0];
    legacyChartVersion3(env, sheet);
    const raw = sheet.getRange(1, 1, 18, 17).getValues();
    env.resetFault(at, before);
    try { env.context.addScanCharts(); } catch (_) { /* Simulate an interrupted chart-only migration. */ }
    env.resetFault(); env.context.addScanCharts();
    assert.deepEqual(sheet.getRange(1, 1, 18, 17).getValues(), raw, `${at}/${before}`);
    assert.equal(sheet.charts.length, 2, `${at}/${before}`);
    assert.equal(JSON.parse(sheet.metadata[0].getValue()).version, 4);
    assert.equal(JSON.parse(sheet.metadata[0].getValue()).state, 'complete');
    env.resetFault(); env.context.addScanCharts();
    assert.deepEqual(env.post(scan), ack);
    assert.equal(env.operations.length, 0, `${at}/${before}`);
  }
});

test('manual chart backfill preserves raw cells, extra annotations, and unrelated charts', () => {
  const env = environment(); const original = env.context.ensureScanCharts_;
  env.context.ensureScanCharts_ = () => { throw new Error('chart service unavailable'); };
  assert.equal(env.post(fixture()).ok, true);
  const sheet = env.scans()[0];
  const raw = sheet.getRange(1, 1, 18, 17).getValues();
  sheet.insertColumnsAfter(sheet.cols, 60 - sheet.cols);
  sheet.getRange(5, 60).setValue('Owner annotation');
  sheet.insertChart(sheet.newChart().setOption('title', 'Owner chart').addRange(sheet.getRange(16, 5, 3, 2)).build());
  const ownerId = sheet.charts[0].id;
  env.context.ensureScanCharts_ = original;
  env.context.addScanCharts();
  raw[2][3] = sheet.getRange(3, 4).getFormulas()[0][0]; // Only the new navigation cell may change.
  assert.deepEqual(sheet.getRange(1, 1, 18, 17).getValues(), raw);
  assert.equal(sheet.cell(5, 60), 'Owner annotation');
  assert.equal(sheet.cols, 60);
  assert.equal(sheet.charts.length, 3);
  assert.ok(sheet.charts.some(chart => chart.id === ownerId));
  env.resetFault(); env.context.addScanCharts();
  assert.equal(env.operations.length, 0);
});

test('existing content in the chart region is preserved and does not block scan acknowledgement', () => {
  const env = environment(); const original = env.context.ensureScanCharts_;
  env.context.ensureScanCharts_ = () => { throw new Error('not installed yet'); };
  const first = env.post(fixture()); const sheet = env.scans()[0];
  sheet.insertColumnsAfter(sheet.cols, 33 - sheet.cols);
  sheet.getRange(2, 33).setValue('Keep this owner note');
  env.context.ensureScanCharts_ = original;
  assert.deepEqual(env.post(fixture()), first);
  assert.equal(sheet.cell(2, 33), 'Keep this owner note');
  assert.equal(sheet.charts.length, 0);
  assert.throws(() => env.context.addScanCharts(), /measurement data is unchanged/);
});

test('blank-result owner formulas and cell notes prevent claiming the chart region', () => {
  for (const kind of ['formulas', 'notes']) {
    const env = environment(); const original = env.context.ensureScanCharts_;
    env.context.ensureScanCharts_ = () => { throw new Error('not installed yet'); };
    const first = env.post(fixture()); const sheet = env.scans()[0];
    sheet.insertColumnsAfter(sheet.cols, 33 - sheet.cols);
    const content = kind === 'formulas' ? '=IF(TRUE,"","Owner formula")' : 'Owner cell note';
    sheet[kind].set('2,33', content);
    assert.equal(sheet.cell(2, 33), '');
    env.context.ensureScanCharts_ = original;
    assert.deepEqual(env.post(fixture()), first);
    assert.equal(sheet[kind].get('2,33'), content);
    assert.equal(sheet.charts.length, 0);
    assert.throws(() => env.context.addScanCharts(), /measurement data is unchanged/);
  }
});

test('schema2 complete scans retain the original measurement and acknowledgement shape', () => {
  const env = environment(); const scan = fixture();
  scan.schema_version = 2; scan.fault = null;
  assertComplete(env, scan, env.post(scan));
  assert.equal(env.scans()[0].cell(14, 2), 'null');
});

test('schema2 retains float-runtime tolerance for completed counts near a step boundary', () => {
  const scan = fixture(); scan.schema_version = 2; scan.fault = null;
  scan.settings.drain_stop_v = 0.99999994; scan.settings.drain_step_v = 0.49999998;
  const env = environment();
  assertComplete(env, scan, env.post(scan));
});

test('native legacy 4092-point plan uploads unchanged despite JavaScript computing 4158', () => {
  const scan = fixture(); scan.schema_version = 2; scan.fault = null;
  Object.assign(scan.settings, {drain_stop_v: 1.55, drain_step_v: 0.025,
    gate_stop_v: 1.625, gate_step_v: 0.025});
  // RP2 float32: 62 drain points by 66 gate points. JavaScript used to reject
  // these settings as 63 * 66 before it even checked the actual valid rows.
  assert.equal((Math.floor(1.55 / 0.025 + 1e-9) + 1) *
    (Math.floor(1.625 / 0.025 + 1e-9) + 1), 4158);
  scan.point_count = 4092;
  scan.points = Array.from({length: scan.point_count}, (_, index) => {
    const row = fixture().points[0]; row[0] = index; row[1] = Math.floor(index / 62);
    row[2] = Math.fround(index % 62 * 0.025); row[3] = Math.fround(row[1] * 0.025);
    row[4] = row[2]; row[5] = row[3]; return row;
  });
  const body = JSON.stringify(scan);
  const env = environment(); const ack = env.post(body);
  assertComplete(env, scan, ack);
  env.resetFault(); assert.deepEqual(env.post(body), ack);
  assert.equal(env.operations.length, 0);
  const newer = microvoltFixture();
  Object.assign(newer.settings, {drain_stop_uv: 1550000, drain_step_uv: 25000,
    gate_stop_uv: 1625000, gate_step_uv: 25000});
  newer.point_count = scan.point_count; newer.points = scan.points;
  const rejected = environment();
  assert.deepEqual(rejected.post(newer), {ok: false, error: 'invalid_scan'});
  assert.equal(rejected.opened.length, 0);
});

test('microvolt settings preserve exact count, integer snapshots, rows and retry hash', () => {
  const scan = microvoltFixture();
  Object.assign(scan.settings, {drain_stop_uv: 1300000, drain_step_uv: 100000});
  scan.point_count = 14;
  scan.points = Array.from({length: 14}, (_, index) => {
    const row = fixture().points[0]; row[0] = index;
    row[2] = Math.fround(index / 10); row[4] = row[2] + 0.0002; return row;
  });
  const env = environment(); const ack = env.post(scan);
  assertComplete(env, scan, ack);
  assert.deepEqual(JSON.parse(env.scans()[0].cell(11, 2)), scan.settings);
  env.resetFault(); assert.deepEqual(env.post(scan), ack); assert.equal(env.operations.length, 0);
});

test('microvolt geometry accepts the exact 4096-point boundary and rejects extra or missing points', () => {
  const scan = microvoltFixture();
  Object.assign(scan.settings, {drain_stop_uv: 63, drain_step_uv: 1,
    gate_stop_uv: 63, gate_step_uv: 1});
  scan.point_count = 4096;
  scan.points = Array.from({length: 4096}, (_, index) => {
    const row = fixture().points[0]; row[0] = index; row[1] = Math.floor(index / 64);
    row[2] = Math.fround(index % 64 / 1000000); row[3] = Math.fround(row[1] / 1000000);
    row[4] = row[2]; row[5] = row[3]; return row;
  });
  const env = environment(); assertComplete(env, scan, env.post(scan));
  for (const mutate of [s => {s.points.pop(); s.point_count--;},
    s => {s.settings.drain_stop_uv++;}, s => {s.points[64][1] = 0;}]) {
    const bad = JSON.parse(JSON.stringify(scan)); mutate(bad); const rejected = environment();
    assert.equal(rejected.post(bad).ok, false);
    assert.equal(rejected.opened.length, 0);
  }
});

test('microvolt settings reject fractional, unsafe, absent and mixed-unit fields before writes', () => {
  const changes = [
    s => {s.settings.drain_step_uv = 0;}, s => {s.settings.drain_step_uv = 0.5;},
    s => {s.settings.gate_stop_uv = 1.5;}, s => {s.settings.source_voltage_uv = true;},
    s => {s.settings.driver_supply_uv = '5000000';},
    s => {s.settings.drain_step_uv = Number.MAX_SAFE_INTEGER + 1;},
    s => {delete s.settings.gate_step_uv;}, s => {s.settings.drain_start_v = 0;},
    s => {s.settings.driver_supply_v = 5;}, s => {s.settings.version = 1;},
    s => {s.settings.driver_supply_uv = 2099999;}, s => {s.settings.driver_supply_uv = 30000001;},
    s => {s.settings.source_voltage_uv = 5000001;}, s => {s.settings.drain_start_uv = -1;},
    s => {s.settings.drain_stop_uv = 5000001;},
  ];
  for (const change of changes) {
    const env = environment(); const scan = microvoltFixture(); change(scan);
    assert.equal(env.post(scan).ok, false, String(change));
    assert.equal(env.opened.length, 0, String(change));
  }
  const old = fixture(); old.settings.gate_step_uv = 500000;
  assert.equal(environment().post(old).ok, false);
});

test('microvolt partials enforce remaining plan and curve position without float equality', () => {
  for (const count of [0, 1, 3, 5]) {
    const scan = microvoltFixture(); scan.settings.gate_stop_uv = 500000;
    scan.outcome = 'partial'; scan.point_count = count;
    scan.points = Array.from({length: count}, (_, index) => {
      const row = fixture().points[0]; row[0] = index; row[1] = Math.floor(index / 3);
      row[2] = index % 3 * 0.5; row[3] = row[1] * 0.5; return row;
    });
    scan.fault = partialFixture().fault;
    Object.assign(scan.fault, {index: count, curve: Math.floor(count / 3),
      requested_vds: count % 3 * 0.5, requested_vgs: Math.floor(count / 3) * 0.5});
    const env = environment(); assertPartial(env, scan, env.post(scan));
    const bad = JSON.parse(JSON.stringify(scan)); bad.fault.curve++;
    assert.equal(environment().post(bad).ok, false);
  }
  const finished = microvoltFixture(); finished.outcome = 'partial';
  finished.fault = partialFixture(3).fault;
  assert.equal(environment().post(finished).ok, false);
  const negative = microvoltFixture();
  Object.assign(negative.settings, {source_voltage_uv: 1000000,
    gate_start_uv: -1000000, gate_stop_uv: -1000000});
  negative.points.forEach(row => {row[3] = -1; row[5] = -0.9999;});
  const env = environment(); assertComplete(env, negative, env.post(negative));
});

test('new scan tabs expose a same-tab View charts link without overwriting acquisition metadata', () => {
  for (const make of [fixture, microvoltFixture, compactFixture, triggeredFixture, rawFixture]) {
    const env = environment(), scan = make();
    const ack = env.post(scan); assert.equal(ack.ok, true);
    const sheet = env.scans()[0];
    const formula = '=HYPERLINK("https://docs.google.com/spreadsheets/d/TEST_SPREADSHEET_ONLY/edit#gid=' + sheet.id + '&range=S1:AB45","View charts")';
    assert.equal(sheet.getRange(3, 4).getFormulas()[0][0], formula);
    assert.equal(sheet.cell(3, 2), ack.sha256);
    if (scan.acquisition) assert.deepEqual(JSON.parse(sheet.cell(1, 5)), scan.acquisition);
    const chartIds = sheet.charts.map(chart => chart.id);
    env.resetFault(); assert.deepEqual(env.post(scan), ack); env.context.addScanCharts();
    assert.equal(env.operations.length, 0);
    assert.deepEqual(sheet.charts.map(chart => chart.id), chartIds);
  }
});

test('chart backfill and receive retry add missing navigation even for already complete charts', () => {
  for (const retryUpload of [false, true]) {
    const env = environment(), scan = rawFixture();
    const ack = env.post(scan), sheet = env.scans()[0];
    const formula = sheet.getRange(3, 4).getFormulas()[0][0];
    sheet.cells.delete('3,4'); sheet.formulas.delete('3,4'); // Tab written by the previous receiver.
    const before = [...sheet.cells], chartIds = sheet.charts.map(chart => chart.id);
    const metadata = sheet.metadata.map(item => item.value);
    const index = [...env.index().cells];
    env.resetFault();
    if (retryUpload) assert.deepEqual(env.post(scan), ack);
    else env.context.addScanCharts();
    assert.equal(sheet.getRange(3, 4).getFormulas()[0][0], formula);
    assert.deepEqual([...sheet.cells].filter(([key]) => key !== '3,4'), before);
    assert.deepEqual(sheet.charts.map(chart => chart.id), chartIds);
    assert.deepEqual(sheet.metadata.map(item => item.value), metadata);
    assert.deepEqual([...env.index().cells], index);
    assert.equal(env.operations.length, 1);
    env.resetFault(); env.context.addScanCharts(); assert.deepEqual(env.post(scan), ack);
    assert.equal(env.operations.length, 0);
  }
});

test('View charts navigation preserves owner values formulas blank-result formulas and notes', () => {
  const cases = [
    {value: 'Operator notes'}, {value: 'View charts'},
    {value: 0}, {value: false},
    {value: '', formula: '=""'},
    {value: 'Other destination', formula: '=HYPERLINK("https://example.invalid","Other destination")'},
    {value: '', note: 'Reserved for owner comments'},
  ];
  for (const contents of cases) {
    const env = environment(), scan = rawFixture();
    const ack = env.post(scan), sheet = env.scans()[0];
    sheet.cells.set('3,4', contents.value); sheet.formulas.delete('3,4');
    if (contents.formula) sheet.formulas.set('3,4', contents.formula);
    if (contents.note) sheet.notes.set('3,4', contents.note);
    env.resetFault(); env.context.addScanCharts(); assert.deepEqual(env.post(scan), ack);
    assert.equal(sheet.cell(3,4), contents.value);
    assert.equal(sheet.formulas.get('3,4'), contents.formula);
    assert.equal(sheet.notes.get('3,4'), contents.note);
    assert.equal(env.operations.length, 0);
  }
});

test('schema5 preserves corrected currents and raw-derived voltages with compact baseline provenance', () => {
  for (const count of [1, 3]) {
    const env = environment(), scan = baselineFixture(count);
    scan.points[0] = [0.123, -0.456, -2e-6, 3e-7];
    const ack = env.post(scan);
    assert.equal(ack.ok, true);
    assert.equal(ack.sha256, crypto.createHash('sha256').update(JSON.stringify(scan)).digest('hex'));
    const sheet = env.scans()[0];
    assert.equal(sheet.cell(12, 2), 'baseline_corrected');
    assert.equal(sheet.cell(13, 2), 5);
    assert.equal(sheet.cell(2, 4), 'current_correction_json');
    assert.deepEqual(JSON.parse(sheet.cell(2, 5)), scan.current_correction);
    assert.deepEqual(sheet.getRange(16, 5, 1, 4).getValues()[0], scan.points[0]);
    assert.equal(env.index().cell(2, 12), scan.current_correction.sha256);
    assert.equal(sheet.charts.length, 2);
    assert.equal(JSON.parse(sheet.metadata[0].getValue()).data_schema, 5);
    assert.match(sheet.cell(47, 19), /open-output baseline correction; voltages retain raw shunt drop/);
    assert.match(sheet.cell(49, 19), /baseline-corrected Ig/);
    assert.equal(sheet.charts[0].ranges[0].cols, 2);
    assert.match(sheet.cell(2, 43), /,TRUE\)/);
    assert.deepEqual(JSON.parse(sheet.cell(14, 2)), scan.fault);
    env.resetFault(); assert.deepEqual(env.post(scan), ack); env.context.addScanCharts();
    assert.equal(env.operations.length, 0);
    const conflicting = structuredClone(scan); conflicting.current_correction.sha256 = 'c'.repeat(64);
    assert.deepEqual(env.post(conflicting), {ok: false, error: 'scan_conflict'});
  }
});

test('schema5 rejects missing invalid or mislabeled correction metadata before spreadsheet access', () => {
  const changes = [s => {delete s.current_correction;}, s => {s.schema_version = 4;},
    s => {s.current_correction.version = true;}, s => {s.current_correction.sha256 = 'z'.repeat(64);},
    s => {s.current_correction.lookup = 'linear_extrapolation';},
    s => {s.current_correction.drain.zero_duty_u16 = 65536;},
    s => {s.current_correction.gate.zero_valid = 1;},
    s => {s.current_correction.gate.zero_residual_uv = null;},
    s => {s.current_correction.gate.zero_residual_uv = 42000001;},
    s => {s.current_correction.gate.min_bus_uv = 17000000;},
    s => {s.current_correction.drain.max_bus_uv = 40958751;},
    s => {s.current_correction.gate.zero_duty_u16 = 1.5;},
    s => {s.current_correction.table = [];}, s => {s.current_correction.drain.extra = 0;},
    s => {s.points=[];s.point_count=0;s.outcome='partial';s.fault=partialFixture(0).fault;}];
  for (const change of changes) {
    const env = environment(), scan = baselineFixture(); change(scan);
    assert.equal(env.post(scan).ok, false, String(change));
    assert.equal(env.opened.length, 0); assert.equal(env.operations.length, 0);
  }
});

test('schema5 chart repair retains earlier raw labels and all prior measurement data', () => {
  const env = environment(), raw = rawFixture();
  const rawAck = env.post(raw), rawSheet = assertRaw(env, raw, rawAck);
  const before = {cells: [...rawSheet.cells], metadata: rawSheet.metadata.map(m=>m.value),
    chartIds: rawSheet.charts.map(c=>c.id)};
  const scan = baselineFixture(); scan.scan_id = 'abcd-555555';
  assert.equal(env.post(scan).ok, true);
  const sheet = env.scans().at(-1);
  sheet.charts = []; sheet.metadata = [];
  sheet.deleteColumns(9, sheet.cols - 8);
  env.context.addScanCharts();
  assert.equal(sheet.charts.length, 2);
  assert.match(sheet.cell(49, 19), /baseline-corrected Ig/);
  assert.deepEqual({cells: [...rawSheet.cells], metadata: rawSheet.metadata.map(m=>m.value),
    chartIds: rawSheet.charts.map(c=>c.id)}, before);
  sheet.getRange(2, 5).setValue('{}');
  assert.throws(() => env.context.addScanCharts(), /Some scan charts need attention/);
});

test('schema5 interrupted receipt retries preserve the original baseline metadata and exact body hash', () => {
  const scan = baselineFixture(1), completed = environment();
  assert.equal(completed.post(scan).ok, true);
  for (let at = 1; at <= completed.operations.length; at++) for (const before of [true, false]) {
    const env = environment(); env.resetFault(at, before); env.post(scan); env.resetFault();
    const ack = env.post(scan);
    assert.equal(ack.ok, true, 'mutation ' + at);
    assert.equal(env.scans().length, 1);
    assert.deepEqual(JSON.parse(env.scans()[0].cell(2, 5)), scan.current_correction);
    assert.equal(env.index().cell(2, 12), scan.current_correction.sha256);
    env.resetFault(); assert.deepEqual(env.post(scan), ack);
    assert.equal(env.operations.length, 0);
  }
});

test('schema4 expands implicit grid without correction flags or calibration and keeps exact receipt', () => {
  const env = environment(), scan = rawFixture();
  scan.points = [[37, -39, 2, -3], [0.125, 5, -0.1, 1e-7], [0.125, -5, 0, -1e-7]];
  const ack = env.post(scan), sheet = assertRaw(env, scan, ack);
  assert.equal(env.index().cell(2, 12), '');
  assert.ok(sheet.getRange(15, 9, 4, 9).getValues().flat().every(value => value === ''));
  assert.match(sheet.cell(50, 19), /A:H\./);
  assert.equal(sheet.charts[0].ranges[0].cols, 2); // Domain plus one gate curve, no quality series.
  assert.deepEqual(JSON.parse(JSON.stringify(sheet.charts[0].options.series)), {});
  assert.match(sheet.cell(2, 43), /,TRUE\)/);
  assert.doesNotMatch(sheet.cell(2, 43), /\$F\$|\$K\$|0\.02|0\.1/);
  assert.equal(sheet.cell(2, 44), '');
  assert.equal(sheet.cell(2, 42), '=$E$16');
  assert.match(sheet.cell(2, 35), /ABS\(\$H\$16:\$H\$18\)/);
  env.resetFault(); assert.deepEqual(env.post(scan), ack);
  env.context.addScanCharts(); assert.equal(env.operations.length, 0);
  const changed = structuredClone(scan); changed.points[0][0]++;
  assert.deepEqual(env.post(changed), {ok: false, error: 'scan_conflict'});
  assert.equal(env.operations.length, 0);
});

test('schema4 rejects malformed encoding metadata and empty scans before any spreadsheet access', () => {
  const mutations = [s => {s.calibration = triggeredFixture().calibration;},
    s => {s.columns.push('gate_off_target');}, s => {s.points[0].push(false);},
    s => {s.points[0].pop();}, s => {s.points[0][0] = null;},
    s => {s.points[0][0] = true;}, s => {s.points[0][0] = '1';},
    s => {s.points[0][0] = 3.5e38;}, s => {s.points[0][0] = -3.5e38;},
    s => {delete s.acquisition;}, s => {s.acquisition.blocks_per_point = 3;},
    s => {s.acquisition.ina_average_count = 4;}, s => {s.acquisition.point_variance = 0;},
    s => {s.settings = microvoltFixture().settings;},
    s => {s.points.pop();s.point_count--;}, s => {s.fault = partialFixture().fault;},
    s => {s.points = [];s.point_count = 0;},
    s => {s.points = [];s.point_count = 0;s.outcome = 'partial';s.fault = partialFixture(0).fault;}];
  for (const mutate of mutations) {
    const env = environment(), scan = rawFixture(); mutate(scan);
    assert.equal(env.post(scan).ok, false, String(mutate));
    assert.equal(env.opened.length, 0); assert.equal(env.operations.length, 0);
  }
});

test('schema4 accepts nine-digit decimal encodings of the largest finite float32', () => {
  const env = environment(), scan = rawFixture();
  scan.points[0] = [3.40282347e38, -3.40282347e38, 1.40129846e-45, -1.40129846e-45];
  assertRaw(env, scan, env.post(scan));
});

test('schema4 retains typed nonempty partial stops without fabricating their interrupted measurement', () => {
  const faults = [partialFixture().fault,
    {...partialFixture().fault, channel: 'gate', raw_code: 32768, sign: -1, limit_value: 0.08192},
    {...partialFixture().fault, kind: 'bus_saturation', raw_code: 32767, limit_value: 40.95875},
    {...partialFixture().fault, kind: 'current_limit', raw_code: null, limit_value: 0.1,
      limit_unit: 'A', measured_value: 0.101},
    {...partialFixture().fault, channel: 'gate', kind: 'current_limit_alert', raw_code: null,
      limit_value: 0.001, limit_unit: 'A', measured_value: null, sign: -1}];
  for (const diagnostic of faults) {
    const env = environment(), scan = rawFixture(1); scan.fault = diagnostic;
    const sheet = assertRaw(env, scan, env.post(scan));
    assert.match(sheet.cell(2, 19), /^PARTIAL/);
    assert.ok(sheet.getRange(17, 1, 1, 8).getValues()[0].every(v => v === ''));
  }
});

test('schema4 exact4096 geometry reconstructs signed gates and nondivisible stop without wire targets', () => {
  const env = environment(), scan = rawFixture();
  Object.assign(scan.settings, {source_voltage_uv: 3300000, driver_supply_uv: 18800000,
    drain_start_uv: 100000, drain_stop_uv: 735000, drain_step_uv: 10000,
    gate_start_uv: -1000000, gate_stop_uv: -370000, gate_step_uv: 10000});
  scan.point_count = 4096;
  scan.points = Array.from({length: 4096}, (_, i) => [i % 64 * 0.001, -2, 1e-5, -1e-6]);
  const sheet = assertRaw(env, scan, env.post(scan));
  assert.equal(sheet.cell(16, 3), 0.1); assert.equal(sheet.cell(16, 4), -1);
  assert.equal(sheet.cell(4111, 3), 0.73); assert.equal(sheet.cell(4111, 4), -0.37);
  assert.equal(sheet.charts[0].ranges[0].cols, 17);
});

test('schema4 complete and partial receipts recover every interrupted mutation exactly once', () => {
  for (const scan of [rawFixture(), rawFixture(1)]) {
    const baseline = environment(); assertRaw(baseline, scan, baseline.post(scan));
    const mutations = baseline.operations.length;
    for (let at = 1; at <= mutations; at++) for (const before of [true, false]) {
      const env = environment(); env.resetFault(at, before); env.post(scan); env.resetFault();
      const ack = env.post(scan); assertRaw(env, scan, ack);
      assert.equal(env.scans().length, 1); assert.equal(env.index().getLastRow(), 2);
      env.resetFault(); assert.deepEqual(env.post(scan), ack); assert.equal(env.operations.length, 0);
    }
  }
});

test('schema4 backfill preserves historical schema1-3 data formulas chart IDs and hashes', () => {
  const env = environment();
  const previous = [fixture(), microvoltFixture(), compactFixture(), triggeredFixture()];
  previous.forEach((scan, i) => {scan.scan_id = 'abcd-000' + i; assert.equal(env.post(scan).ok, true);});
  const old = env.scans().slice();
  const snapshot = sheet => ({cells: [...sheet.cells], notes: [...sheet.notes],
    metadata: sheet.metadata.map(m => m.value), chartIds: sheet.charts.map(c => c.id)});
  const originals = old.map(snapshot);
  const scan = rawFixture(); scan.scan_id = 'abcd-4444';
  const ack = env.post(scan), sheet = assertRaw(env, scan, ack);
  sheet.charts = []; sheet.metadata = [];
  sheet.deleteColumns(9, sheet.cols - 8);
  env.context.addScanCharts(); assertRaw(env, scan, ack);
  old.forEach((historical, i) => assert.deepEqual(snapshot(historical), originals[i]));
  env.resetFault(); env.context.addScanCharts(); assert.deepEqual(env.post(scan), ack);
  assert.equal(env.operations.length, 0);
  for (const [r, c, bad] of [[13, 2, 3], [12, 2, 'corrected'], [15, 8, 'flags'], [16, 1, 99]]) {
    const original = sheet.cell(r, c); sheet.getRange(r, c).setValue(bad);
    assert.throws(() => env.context.addScanCharts(), /Some scan charts need attention/);
    sheet.getRange(r, c).setValue(original);
  }
});

test('schema3 writes only its eleven columns and preserves exact body hash on read-only retry', () => {
  const env = environment(); const scan = compactFixture();
  const ack = env.post(scan);
  assertComplete(env, scan, ack);
  const sheet = env.scans()[0];
  assert.deepEqual(sheet.getRange(15, 1, 1, 11).getValues()[0], compactColumns);
  assert.ok(sheet.getRange(15, 12, 4, 6).getValues().flat().every(value => value === ''));
  assert.equal(JSON.parse(sheet.metadata[0].getValue()).data_schema, 3);
  assert.match(sheet.cell(50, 19), /A:K\./);
  assert.equal(sheet.cell(16, 9), false); assert.equal(sheet.cell(18, 9), true);
  assert.equal(sheet.cell(16, 10), false); assert.equal(sheet.cell(16, 11), false);
  env.resetFault(); assert.deepEqual(env.post(scan), ack); assert.equal(env.operations.length, 0);
  const changed = compactFixture(); changed.points[0][10] = true;
  assert.deepEqual(env.post(changed), {ok: false, error: 'scan_conflict'});
  assert.equal(env.operations.length, 0);
});

test('triggered scans preserve acquisition metadata and eleven-column charts for every hardware count', () => {
  for (const count of [1, 4, 16, 64, 128, 256, 512, 1024]) {
    const env = environment(); const scan = triggeredFixture(3, count);
    const ack = env.post(scan);
    assertComplete(env, scan, ack);
    const sheet = env.scans()[0];
    assert.equal(sheet.cell(1, 4), 'acquisition_json');
    assert.deepEqual(JSON.parse(sheet.cell(1, 5)), scan.acquisition);
    assert.deepEqual(JSON.parse(sheet.cell(11, 2)), scan.settings);
    assert.deepEqual(JSON.parse(sheet.cell(13, 2)), scan.calibration.metadata);
    assert.deepEqual(sheet.getRange(15, 1, 1, 11).getValues()[0], compactColumns);
    env.resetFault(); assert.deepEqual(env.post(scan), ack);
    env.context.addScanCharts();
    assert.equal(env.operations.length, 0);
  }
});

test('triggered payload rejects legacy knobs, relabelled calibration and fabricated statistics before writes', () => {
  const changes = [
    s => {delete s.acquisition;}, s => {s.schema_version = 2;},
    s => {s.settings.version = 2;}, s => {s.settings.ina_average_count = 2;},
    s => {s.settings.ina_average_count = true;}, s => {s.settings.batches = 1;},
    s => {s.settings.samples_per_batch = 64;}, s => {s.settings.sample_pause_ms = 4;},
    s => {s.acquisition.extra = 1;}, s => {s.acquisition.blocks_per_point = 3;},
    s => {s.acquisition.point_variance = 0;}, s => {s.acquisition.ina_average_count = 4;},
    s => {s.acquisition.acquisition_mode = 'continuous';},
    s => {s.acquisition.bus_conversion_us = 588;}, s => {s.acquisition.shunt_conversion_us = 588;},
    s => {delete s.calibration.metadata.acquisition_mode;},
    s => {s.calibration.metadata.ina_average_count = 4;},
    s => {s.calibration.metadata.calibration_repeat_blocks = 1;},
    s => {s.calibration.metadata.bus_conversion_us = 588;},
  ];
  for (const change of changes) {
    const env = environment(); const scan = triggeredFixture(); change(scan);
    assert.equal(env.post(scan).ok, false, String(change));
    assert.equal(env.operations.length, 0, String(change));
    assert.equal(env.opened.length, 0, String(change));
  }
});

test('triggered settings accept 100ms settling while historical settings retain 200ms minimum', () => {
  for (const settle of [100, 101, 199, 200, 10000]) {
    const env = environment(); const scan = triggeredFixture();
    scan.settings.settle_ms = settle;
    assertComplete(env, scan, env.post(scan));
    assert.equal(JSON.parse(env.scans()[0].cell(11, 2)).settle_ms, settle);
    env.resetFault(); env.context.addScanCharts();
    assert.equal(env.operations.length, 0);
  }
  for (const settle of [0, 99, 100.5, 10001, true, '100']) {
    const env = environment(); const scan = triggeredFixture();
    scan.settings.settle_ms = settle;
    assert.equal(env.post(scan).ok, false, String(settle));
    assert.equal(env.operations.length, 0);
    assert.equal(env.opened.length, 0);
  }
  for (const make of [fixture, microvoltFixture, compactFixture]) {
    for (const settle of [100, 199]) {
      const env = environment(); const scan = make();
      scan.settings.settle_ms = settle;
      assert.equal(env.post(scan).ok, false);
      assert.equal(env.operations.length, 0);
      assert.equal(env.opened.length, 0);
    }
    const env = environment(); const scan = make();
    assertComplete(env, scan, env.post(scan));
  }
});

test('latched current alerts preserve either polarity without claiming a measured peak', () => {
  for (const sign of [-1, 1]) {
    const env = environment(); const scan = triggeredFixture(1);
    Object.assign(scan.fault, {kind: 'current_limit_alert', raw_code: null, sign,
      limit_value: 0.0001, limit_unit: 'A', measured_value: null});
    assertPartial(env, scan, env.post(scan));
    assert.deepEqual(JSON.parse(env.scans()[0].cell(1, 5)), scan.acquisition);
    for (const [key, invalid] of [['raw_code', 0], ['measured_value', 0], ['sign', 0],
      ['limit_unit', 'V'], ['limit_value', 0]]) {
      const bad = JSON.parse(JSON.stringify(scan)); bad.fault[key] = invalid;
      const fresh = environment(); assert.equal(fresh.post(bad).ok, false);
      assert.equal(fresh.operations.length, 0);
    }
  }
});

test('triggered receipt survives every interrupted write and does not alter prior compact tabs', () => {
  const scan = triggeredFixture(1);
  const baseline = environment(); assertPartial(baseline, scan, baseline.post(scan));
  for (const before of [true, false]) for (let at = 1; at <= baseline.operations.length; at++) {
    const env = environment(); env.resetFault(at, before);
    env.post(scan); env.resetFault();
    assertPartial(env, scan, env.post(scan));
    assert.deepEqual(JSON.parse(env.scans()[0].cell(1, 5)), scan.acquisition);
    env.resetFault(); assertPartial(env, scan, env.post(scan));
    assert.equal(env.operations.length, 0);
  }
  const env = environment(); const previous = compactFixture();
  env.post(previous); const sheet = env.scans()[0];
  const cells = [...sheet.cells.entries()], chartIds = sheet.charts.map(c => c.id);
  scan.scan_id = 'abcd-444444'; env.post(scan); env.context.addScanCharts();
  assert.deepEqual([...sheet.cells.entries()], cells);
  assert.deepEqual(sheet.charts.map(c => c.id), chartIds);
});

test('schema3 device gate flags control both line gaps and markers independently of float64 boundaries', () => {
  const env = environment(); const scan = compactFixture();
  // The bit is authoritative. Float32 classification can differ from a
  // reconstructed float64 comparison at the inclusive 20 mV boundary.
  scan.points[0][5] = 0; scan.points[0][10] = true;
  scan.points[1][5] = 0.020000001; scan.points[1][10] = false;
  scan.points[2][5] = -0.020000001; scan.points[2][10] = true;
  scan.points.forEach(row => {row[8] = row[9] = false;});
  assertComplete(env, scan, env.post(scan));
  const layout = env.context.scanLayout_(3);
  const plan = env.context.chartPlan_(scan.points, layout);
  assert.deepEqual(JSON.parse(JSON.stringify(plan.offTargetRows)), [16, 18]);
  assert.equal(env.context.gateOnTarget_(scan.points[1], layout), true);
  const sheet = env.scans()[0];
  assert.match(sheet.cell(2, 43), /NOT\(\$K\$16\)/);
  assert.match(sheet.cell(2, 44), /NOT\(NOT\(\$K\$16\)\)/);
  assert.doesNotMatch(sheet.cell(2, 43), /0\.02/);
  assert.equal(sheet.cell(2, 42), '=$E$16');
  assert.equal(sheet.cell(2, 37), '=INDEX($F:$F,$AJ2)');
  assert.match(sheet.cell(2, 35), /ABS\(\$H\$16:\$H\$18\)/);
  assert.equal(sheet.charts[0].options.interpolateNulls, false);
  assert.equal(sheet.charts[0].options.series[1].lineWidth, 0);
  assert.equal(sheet.charts[0].options.series[1].visibleInLegend, true);
});

test('schema3 rejects legacy settings, malformed columns/flags and wrong geometry before access', () => {
  const changes = [
    s => {s.settings = fixture().settings;}, s => {s.schema_version = 2;},
    s => {s.schema_version = 4;}, s => {s.columns = columns.slice();},
    s => {s.columns[10] = 'gate_voltage_limited';}, s => {s.columns.pop();},
    s => {s.points[0].push(0);}, s => {s.points[0].pop();},
    s => {s.points[0][8] = 0;}, s => {s.points[0][9] = 'false';},
    s => {s.points[0][10] = 4;}, s => {s.points[0][10] = null;},
    s => {s.points[0][7] = null;}, s => {s.points[0][6] = 1.001;},
    s => {s.points[0][4] = 36.001;}, s => {s.points[0][1] = 1;},
    s => {s.points.pop();s.point_count--;}, s => {s.settings.drain_step_uv = 100000;},
    s => {delete s.fault;}, s => {s.fault = partialFixture().fault;},
  ];
  for (const change of changes) {
    const env = environment(); const scan = compactFixture(); change(scan);
    assert.equal(env.post(scan).ok, false, String(change));
    assert.equal(env.opened.length, 0, String(change));
    assert.equal(env.operations.length, 0, String(change));
  }
});

test('schema3 accepts exact4096 and fractional float32 requested volts without invented extras', () => {
  const env = environment(); const scan = compactFixture();
  Object.assign(scan.settings, {drain_stop_uv: 630000, drain_step_uv: 10000,
    gate_stop_uv: 630000, gate_step_uv: 10000});
  scan.point_count = 4096;
  const first = scan.points[0];
  scan.points = Array.from({length: 4096}, (_, index) => {
    const row = first.slice(); row[0] = index; row[1] = Math.floor(index / 64);
    row[2] = Math.fround(index % 64 * 0.01); row[3] = Math.fround(row[1] * 0.01);
    row[4] = row[2]; row[5] = row[3]; row[10] = index % 2 === 1; return row;
  });
  assertComplete(env, scan, env.post(scan));
  const sheet = env.scans()[0]; assert.equal(sheet.rows, 4111);
  assert.equal(sheet.charts[0].ranges[0].cols, 18);
  assert.equal(env.context.chartPlan_(scan.points, env.context.scanLayout_(3)).offTargetRows.length, 2048);
  assert.ok(env.operations.filter(name => name.startsWith('setValues:')).length < 20);
  const bad = JSON.parse(JSON.stringify(scan)); bad.settings.gate_stop_uv = 640000;
  assert.deepEqual(environment().post(bad), {ok: false, error: 'invalid_scan'});
});

test('schema3 complete partial and zero-point receipts recover every interrupted mutation', () => {
  for (const count of [0, 1, 3]) {
    const scan = compactFixture(count); const baseline = environment();
    const verify = count === 3 ? assertComplete : assertPartial;
    verify(baseline, scan, baseline.post(scan));
    assert.equal(JSON.parse(baseline.scans()[0].metadata[0].getValue()).data_schema, 3);
    for (const before of [true, false]) for (let at = 1; at <= baseline.operations.length; at++) {
      const env = environment(); env.resetFault(at, before);
      env.post(scan); env.resetFault(); verify(env, scan, env.post(scan));
      env.resetFault(); verify(env, scan, env.post(scan));
      assert.equal(env.operations.length, 0);
    }
  }
  for (const change of [s => {s.fault.index++;}, s => {s.fault.curve++;},
    s => {s.point_count = 3;s.points = compactFixture().points;s.fault.index = 3;}]) {
    const scan = compactFixture(1); change(scan);
    assert.equal(environment().post(scan).ok, false);
  }
});

test('mixed workbook keeps legacy layouts and hashes untouched while repairing compact charts', () => {
  const env = environment(); const first = fixture(), second = microvoltFixture();
  second.scan_id = 'abcd-222222';
  const acks = [env.post(first), env.post(second)];
  const legacy = env.scans().slice();
  const before = legacy.map(sheet => ({cells: [...sheet.cells], notes: [...sheet.notes],
    metadata: sheet.metadata.map(item => item.value), chartIds: sheet.charts.map(item => item.id)}));
  const compact = compactFixture(); compact.scan_id = 'abcd-333333';
  const ack = env.post(compact), sheet = env.scans().at(-1);
  sheet.getRange(15, 12).setValue('Owner extra column');
  sheet.getRange(16, 12).setValue('Owner value'); sheet.notes.set('16,12', 'Owner note');
  const raw = sheet.getRange(1, 1, 18, 18).getValues();
  sheet.charts = []; sheet.metadata = [];
  // Free only the owned presentation area for a manual rebuild.
  for (const map of [sheet.cells, sheet.formulas, sheet.notes]) {
    for (const key of map.keys()) if (Number(key.split(',')[1]) >= 19) map.delete(key);
  }
  env.resetFault(); env.context.addScanCharts();
  assert.deepEqual(sheet.getRange(1, 1, 18, 18).getValues(), raw);
  assert.equal(sheet.notes.get('16,12'), 'Owner note');
  assert.match(sheet.cell(2, 43), /NOT\(\$K\$16\)/);
  legacy.forEach((item, index) => {
    assert.deepEqual({cells: [...item.cells], notes: [...item.notes],
      metadata: item.metadata.map(meta => meta.value), chartIds: item.charts.map(chart => chart.id)}, before[index]);
  });
  env.resetFault(); env.context.addScanCharts();
  assert.deepEqual(env.post(first), acks[0]); assert.deepEqual(env.post(second), acks[1]);
  assert.deepEqual(env.post(compact), ack); assert.equal(env.operations.length, 0);
});

test('schema3 manual backfill handles chartless eleven-column tabs and rejects corrupt compact data', () => {
  const env = environment(); const scan = compactFixture(); env.post(scan);
  const sheet = env.scans()[0]; sheet.charts = []; sheet.metadata = [];
  sheet.deleteColumns(12, sheet.cols - 11);
  assert.equal(sheet.cols, 11);
  env.context.addScanCharts(); assert.equal(sheet.charts.length, 2);
  assert.equal(JSON.parse(sheet.metadata[0].getValue()).data_schema, 3);
  const raw = sheet.getRange(1, 1, 18, 11).getValues();
  for (const [row, col, value] of [[15, 11, 'wrong_header'], [16, 11, 4], [16, 2, 7], [8, 2, 4097]]) {
    const previous = sheet.cell(row, col); sheet.getRange(row, col).setValue(value);
    assert.throws(() => env.context.addScanCharts(), /Some scan charts need attention/);
    sheet.getRange(row, col).setValue(previous);
    assert.deepEqual(sheet.getRange(1, 1, 18, 11).getValues(), raw);
  }
});

test('partial overrange keeps only completed measurements and separately records each fault kind', () => {
  const faults = [
    {},
    {sign: -1, raw_code: 0x8000, limit_value: 0.08192},
    {channel: 'gate', kind: 'bus_saturation', raw_code: 0x7FFF, limit_value: 40.95875},
    {kind: 'current_limit', raw_code: null, limit_unit: 'A', limit_value: 0.005, measured_value: 0.006},
    {channel: 'gate', kind: 'current_limit', raw_code: null, sign: -1,
      limit_unit: 'A', limit_value: 0.0001, measured_value: -0.00011},
  ];
  for (const changes of faults) {
    const env = environment(); const scan = partialFixture(); Object.assign(scan.fault, changes);
    assertPartial(env, scan, env.post(scan));
    env.resetFault();
    assertPartial(env, scan, env.post(scan));
    assert.equal(env.operations.length, 0);
    env.context.addScanCharts();
    assert.equal(env.operations.length, 0);
  }
});

test('zero-point partial is a saveable diagnostic with no invented rows or empty charts', () => {
  const env = environment(); const scan = partialFixture(0);
  scan.fault.phase = 'settle';
  assertPartial(env, scan, env.post(scan));
  assert.equal(env.scans()[0].cell(48, 20), 'No completed points to plot');
  env.resetFault();
  env.context.addScanCharts();
  assertPartial(env, scan, env.post(scan));
  assert.equal(env.operations.length, 0);
});

test('partial payload rejects invalid fault metadata and incomplete numeric rows before writes', () => {
  const changes = [
    s => {s.fault = null;}, s => {delete s.fault;}, s => {s.schema_version = 1;},
    s => {s.fault.channel = 'source';}, s => {s.fault.kind = 'i2c_failure';},
    s => {s.fault.sign = 0;}, s => {s.fault.raw_code = 0;},
    s => {s.fault.sign = -1;}, s => {s.fault.index = 0;},
    s => {s.fault.phase = 'unknown';}, s => {s.fault.curve = -1;},
    s => {s.fault.limit_unit = 'A';}, s => {s.fault.limit_value = -0.08192;},
    s => {s.fault.measured_value = 0.08192;}, s => {s.fault.extra = 'not allowed';},
    s => {s.fault.requested_vgs = 50;}, s => {s.fault.requested_vds = -1;},
    s => {s.fault.kind = 'current_limit';s.fault.raw_code = null;s.fault.limit_unit='A';
      s.fault.limit_value=0.005;s.fault.measured_value=0.004;},
    s => {s.points[0][6]='MAX';}, s => {s.points[0][14]=1;},
    s => {s.outcome='complete';},
  ];
  for (const change of changes) {
    const env = environment(); const scan = partialFixture(); change(scan);
    assert.equal(env.post(scan).ok, false, String(change));
    assert.equal(env.operations.length, 0, String(change));
    assert.equal(env.opened.length, 0, String(change));
  }
  const complete = fixture(); complete.schema_version=2;complete.fault=null;
  complete.points=[];complete.point_count=0;
  assert.equal(environment().post(complete).ok,false);
});

test('partial and zero-point receipts recover across every interrupted mutation without duplicates', () => {
  for (const count of [0, 1]) {
    const scan = partialFixture(count); const baseline = environment();
    assertPartial(baseline, scan, baseline.post(scan));
    for (const before of [true, false]) for (let at = 1; at <= baseline.operations.length; at++) {
      const env = environment(); env.resetFault(at, before);
      env.post(scan); env.resetFault();
      assertPartial(env, scan, env.post(scan));
      env.resetFault(); assertPartial(env, scan, env.post(scan));
      assert.equal(env.operations.length, 0);
    }
  }
});


test('mixed INA226/INA228 scans preserve per-channel timing and both charts', () => {
  for (const corrected of [false, true]) {
    const scan = corrected ? baselineFixture() : rawFixture();
    Object.assign(scan.acquisition, {acquisition_mode: 'ina226_ina228_triggered_v1',
      gate_bus_conversion_us: 1052, gate_shunt_conversion_us: 1052});
    const env = environment();
    assert.equal(env.post(scan).ok, true);
    const sheet = env.scans()[0];
    assert.deepEqual(JSON.parse(sheet.cell(1, 5)), scan.acquisition);
    assert.equal(sheet.charts.length, 2);
    env.context.addScanCharts();
    assert.equal(sheet.charts.length, 2);
  }
});

test('mixed gate saturation accepts 20-bit endpoints and rejects wrong channel or scale', () => {
  for (const sign of [1, -1]) {
    const scan = rawFixture(1);
    Object.assign(scan.acquisition, {acquisition_mode: 'ina226_ina228_triggered_v1',
      gate_bus_conversion_us: 1052, gate_shunt_conversion_us: 1052});
    Object.assign(scan.fault, {channel: 'gate', sign, raw_code: sign > 0 ? 0x7ffff : 0x80000,
      limit_value: (sign > 0 ? 0x7ffff : 0x80000) * 0.0000003125});
    const env = environment();
    assert.equal(env.post(scan).ok, true);
    env.context.addScanCharts();
    assert.equal(env.scans()[0].charts.length, 2);
    for (const change of [s => {s.fault.channel = 'drain';},
      s => {s.fault.raw_code = sign > 0 ? 0x7fff : 0x8000;},
      s => {s.fault.limit_value = 0.08192;},
      s => {delete s.acquisition.gate_bus_conversion_us;},
      s => {s.acquisition.gate_shunt_conversion_us = 1100;},
      s => {s.acquisition.acquisition_mode = 'ina226_triggered_v1';}]) {
      const bad = JSON.parse(JSON.stringify(scan)); change(bad);
      const rejected = environment();
      assert.equal(rejected.post(bad).ok, false);
      assert.equal(rejected.operations.length, 0);
    }
  }
});


test('baseline provenance accepts the INA228 positive bus-code ceiling only on gate', () => {
  const scan = baselineFixture();
  Object.assign(scan.acquisition, {acquisition_mode: 'ina226_ina228_triggered_v1',
    gate_bus_conversion_us: 1052, gate_shunt_conversion_us: 1052});
  scan.current_correction.gate.max_bus_uv = 102399805;
  assert.equal(environment().post(scan).ok, true);
  for (const change of [s => {s.current_correction.gate.max_bus_uv++;},
    s => {s.current_correction.drain.max_bus_uv = 102399805;}]) {
    const bad = JSON.parse(JSON.stringify(scan)); change(bad);
    assert.equal(environment().post(bad).ok, false);
  }
});

test('drain-only correction records scope and raw Ig without changing submitted measurements', () => {
  for (const count of [1, 3]) {
    const env = environment(), scan = drainBaselineFixture(count);
    scan.points[0] = [0.123, -0.456, -2e-6, 7.8e-6];
    const ack = env.post(scan), sheet = env.scans()[0];
    assert.equal(ack.ok, true);
    assert.equal(ack.sha256, crypto.createHash('sha256').update(JSON.stringify(scan)).digest('hex'));
    assert.equal(sheet.cell(12, 2), 'drain_baseline_corrected_gate_raw');
    assert.deepEqual(JSON.parse(sheet.cell(2, 5)), scan.current_correction);
    assert.deepEqual(sheet.getRange(16, 5, 1, 4).getValues()[0], scan.points[0]);
    assert.equal(env.index().cell(2, 12), scan.current_correction.sha256);
    assert.match(sheet.cell(47, 19), /Drain current uses open-output baseline correction; gate current is raw/);
    assert.match(sheet.cell(49, 19), /maximum absolute raw Ig/);
    assert.equal(sheet.charts[0].options.vAxis.title, 'Baseline-corrected Id (mA)');
    assert.equal(sheet.charts[1].options.vAxis.title, 'Maximum raw |Ig| (uA)');
    assert.equal(JSON.parse(sheet.metadata[0].value).current_correction_applied_to, 'drain');
    env.resetFault(); assert.deepEqual(env.post(scan), ack); env.context.addScanCharts();
    assert.equal(env.operations.length, 0);
    const conflict = structuredClone(scan);
    conflict.current_correction.version = 1; delete conflict.current_correction.applied_to;
    assert.deepEqual(env.post(conflict), {ok: false, error: 'scan_conflict'});
  }
});

test('correction scope rejects missing, unsupported, extra and version-conflicting metadata before writes', () => {
  for (const change of [s => {delete s.current_correction.applied_to;},
    s => {s.current_correction.applied_to = 'gate';},
    s => {s.current_correction.applied_to = 'both';},
    s => {s.current_correction.applied_to = ['drain'];},
    s => {s.current_correction.version = 1;},
    s => {s.current_correction.version = 3;},
    s => {s.current_correction.extra = false;},
    s => {delete s.current_correction.gate;},
    s => {s.current_correction.gate.applied_to = false;},
    s => {s.schema_version = 4;}]) {
    const env = environment(), scan = drainBaselineFixture(); change(scan);
    assert.equal(env.post(scan).ok, false, String(change));
    assert.equal(env.opened.length, 0); assert.equal(env.operations.length, 0);
  }
});

test('drain-only chart repair preserves historical raw and both-corrected tabs', () => {
  const env = environment(), old = [];
  for (const [i, scan] of [rawFixture(), baselineFixture()].entries()) {
    scan.scan_id = 'abcd-' + (1000 + i);
    assert.equal(env.post(scan).ok, true);
    const sheet = env.scans().at(-1);
    old.push({sheet, before: {cells: [...sheet.cells], metadata: sheet.metadata.map(m=>m.value),
      chartIds: sheet.charts.map(c=>c.id)}});
  }
  const scan = drainBaselineFixture();
  assert.equal(env.post(scan).ok, true);
  const sheet = env.scans().at(-1), raw = sheet.getRange(1, 1, 18, 8).getValues();
  sheet.charts = []; sheet.metadata = []; sheet.deleteColumns(9, sheet.cols - 8);
  env.context.addScanCharts();
  assert.equal(sheet.charts.length, 2);
  assert.match(sheet.cell(49, 19), /raw Ig/);
  assert.equal(sheet.charts[1].options.vAxis.title, 'Maximum raw |Ig| (uA)');
  // D3 is the intentionally restored navigation link.
  raw[2][3] = sheet.cell(3, 4);
  assert.deepEqual(sheet.getRange(1, 1, 18, 8).getValues(), raw);
  for (const {sheet, before} of old) assert.deepEqual({cells: [...sheet.cells],
    metadata: sheet.metadata.map(m=>m.value), chartIds: sheet.charts.map(c=>c.id)}, before);
  const metadata = JSON.parse(sheet.metadata[0].value); delete metadata.current_correction_applied_to;
  sheet.metadata[0].setValue(JSON.stringify(metadata));
  sheet.getRange(49, 19).setValue('Incorrect old scope');
  env.context.addScanCharts();
  assert.equal(sheet.charts.length, 2);
  assert.match(sheet.cell(49, 19), /raw Ig/);
});

test('saved correction scope must agree with measurement-mode label before chart repair', () => {
  for (const scan of [baselineFixture(), drainBaselineFixture()]) {
    const env = environment(); assert.equal(env.post(scan).ok, true);
    const sheet = env.scans()[0], data = sheet.getRange(16, 1, 3, 8).getValues();
    const oldCharts = sheet.charts.map(c=>c.id);
    sheet.getRange(12, 2).setValue(scan.current_correction.version === 1 ?
      'drain_baseline_corrected_gate_raw' : 'baseline_corrected');
    assert.throws(() => env.context.addScanCharts(), /Some scan charts need attention/);
    assert.deepEqual(sheet.charts.map(c=>c.id), oldCharts);
    assert.deepEqual(sheet.getRange(16, 1, 3, 8).getValues(), data);
  }
});

test('drain-only receipt recovers interruption with exact scope and raw gate labels', () => {
  const scan = drainBaselineFixture(1), complete = environment();
  assert.equal(complete.post(scan).ok, true);
  for (let at = 1; at <= complete.operations.length; at++) for (const before of [true, false]) {
    const env = environment(); env.resetFault(at, before); env.post(scan); env.resetFault();
    const ack = env.post(scan), sheet = env.scans()[0];
    assert.equal(ack.ok, true, 'mutation ' + at);
    assert.equal(env.scans().length, 1);
    assert.equal(sheet.cell(12, 2), 'drain_baseline_corrected_gate_raw');
    assert.deepEqual(JSON.parse(sheet.cell(2, 5)), scan.current_correction);
    assert.match(sheet.cell(49, 19), /raw Ig/);
    assert.equal(sheet.charts.length, 2);
    env.resetFault(); assert.deepEqual(env.post(scan), ack);
    assert.equal(env.operations.length, 0);
  }
});
