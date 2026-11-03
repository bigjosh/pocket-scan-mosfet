"""Native actual-voltage plot/handoff check; physical outputs always low.

Injects synthetic averaged blocks into the real Engine/App. Captures accepted
rows in RAM only; no PWM, I2C, network, configuration writes or scan files.
Preload main and a display before running this diagnostic.
"""
import main
import json
from ui import CURVES

OFFSETS = (0.0, 0.0, -0.15, 0.0, 0.0, 0.15, 0.0, 0.0)


class ZeroInstrument:
    identity = {'synthetic_test': True}

    def __init__(self):
        self.engine = None
        self.commands = []
        self.zero()

    def zero(self):
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
        self.duties = {'drain': 0.0, 'gate': 0.0}

    def set_duty(self, role, fraction):
        assert role in self.duties and 0 <= fraction <= 1
        # Record logical commands only; never construct PWM or write high.
        self.commands.append((role, fraction))
        self.duties[role] = fraction
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)

    def begin_measurement(self, count):
        pass

    def poll_measurement(self):
        return True

    def poll_alerts(self):
        pass

    def read_pair(self):
        index = self.engine._index
        pair = {}
        for role, resistance in (('drain', main.model.DRAIN_SHUNT_OHM),
                                 ('gate', main.model.GATE_SHUNT_OHM)):
            voltage = self.duties[role] * main.model.PWM_FULL_SCALE_V
            if role == 'gate' and self.duties[role] > 0:
                voltage += OFFSETS[index]
            current = 0.00005 * (index + 1) if role == 'drain' else 0.0
            shunt = current * resistance
            pair[role] = {'terminal_v': voltage, 'shunt_v': shunt,
                          'bus_v': voltage + shunt}
        return pair


class Inputs:
    def poll(self, now):
        return []


class Store:
    def load_json(self, path):
        assert path == '__actual_target_config'
        return None

    def save_json_atomic(self, *unused):
        raise AssertionError('No configuration writes in this diagnostic')

    save_settings = save_json_atomic


class MemoryCapture:
    """Diagnostic-only handoff capture; never serializes invented legacy rows."""
    def __init__(self):
        self.rows, self.accepted, self.fault = [], [], None

    @property
    def status(self):
        return {"state": "disabled", "message": "Diagnostic RAM capture only",
                "pending_count": 0, "saved_count": len(self.accepted),
                "configured": False, "enabled": False}

    def pause(self):
        return True

    def tick(self, *args, **kwargs):
        pass

    def begin_scan(self, settings, calibration, now):
        main.model.validate_scan_calibration(calibration, settings)
        self.rows, self.fault = [], None
        return True

    def record_point(self, row):
        assert row["acquisition_blocks_completed"] == 1
        assert row["id_std_a"] is None and row["ig_std_a"] is None
        self.rows.append(dict(row))

    def finish_scan(self, now, fault=None):
        self.fault = fault

    def accept_scan(self):
        self.accepted.append((self.rows, self.fault))
        self.rows, self.fault = [], None

    def discard_scan(self):
        self.rows, self.fault = [], None


class TraceDisplay:
    def __init__(self, real):
        self.real, self.lines = real, []

    def __getattr__(self, name):
        return getattr(self.real, name)

    def line(self, x0, y0, x1, y1, color):
        if color in CURVES:
            self.lines.append((x0, y0, x1, y1, color))
        self.real.line(x0, y0, x1, y1, color)


app = None
instrument = ZeroInstrument()
try:
    settings = dict(main.model.DEFAULT_SETTINGS)
    settings.update(drain_start_uv=125000, drain_stop_uv=1000000, drain_step_uv=125000,
                    gate_start_uv=1000000, gate_stop_uv=1000000, gate_step_uv=125000,
                    driver_supply_uv=5000000, source_voltage_uv=0,
                    ina_average_count=4)
    calibration = {'version': 1, 'metadata': {'driver_supply_v': 5.0,
        'pwm_hz': main.model.PWM_HZ, 'synthetic_test': True}, 'channels': {
        role: {'shunt_ohm': resistance, 'points': [
            {'terminal_v': voltage, 'duty': voltage / main.model.PWM_FULL_SCALE_V,
             'shunt_v': 0.0} for voltage in (0.0, 3.0)]}
        for role, resistance in (('drain', main.model.DRAIN_SHUNT_OHM),
                                 ('gate', main.model.GATE_SHUNT_OHM))}}
    calibration["metadata"].update(
        acquisition_mode=main.model.ACQUISITION_MODE, ina_average_count=4,
        bus_conversion_us=1100, shunt_conversion_us=1100,
        calibration_repeat_blocks=main.model.CALIBRATION_REPEAT_BLOCKS)
    engine = main.Engine(instrument, clock_ms=lambda: engine._last_clock)
    instrument.engine = engine
    cloud = MemoryCapture()
    trace = TraceDisplay(display)
    app = main.App(trace, Inputs(), engine, settings, calibration,
                   store=Store(), cloud=cloud, plot_buffers=main._plot_buffers)
    app.render()
    app.action('right')
    for now in range(0, 30000, 20):
        app.tick(now)
        if app.screen not in ('preparing', 'scan'):
            break
    assert app.screen == 'complete', app.notice if app.screen == 'notice' else engine.error
    assert engine.state == 'complete' and engine.shutdown_succeeded
    assert app._archive_ready and len(app.curves) == 8
    assert [app.limit_flags[i] for i in range(8)] == [0, 0, 4, 0, 0, 4, 0, 0]
    assert app.limited_points == 0
    assert len([c for c in instrument.commands if c[0] == 'gate']) == 1
    assert len([c for c in instrument.commands if c[0] == 'drain']) == 9
    # Force a complete redraw in the same four-point chunks as the real UI.
    app._plot_axes()
    app._plot_cursor, trace.lines = 0, []
    while app._plot_cursor < len(app.curves):
        app._draw_points()
    expected = []
    for index in (1, 4, 7):
        x0, y0 = app._xy(index - 1)
        x1, y1 = app._xy(index)
        expected.append((x0, y0, x1, y1, CURVES[0]))
    assert trace.lines == expected, 'Unexpected connection across off-target gate point'
    assert len(cloud.rows) == 8 and not cloud.accepted
    app.action('right')
    assert app.screen == 'home' and len(cloud.accepted) == 1
    rows, fault = cloud.accepted[0]
    assert fault is None and len(rows) == 8
    for index, row in enumerate(rows):
        assert abs(row['requested_vgs'] - 1.0) < 1e-6
        assert abs(row['vds'] - (index + 1) * 0.125) < 1e-6
        assert abs(row['vgs'] - (1.0 + OFFSETS[index])) < 1e-6
        assert abs(row['id_a'] - 0.00005 * (index + 1)) < 1e-8
        assert not row['drain_voltage_limited'] and not row['gate_voltage_limited']
    print('ACTUAL_TARGET_NATIVE', json.dumps({
        'completed_points': 8, 'gate_undershoot_and_overshoot_retained': True,
        'no_target_correction_commands': True, 'redraw_line_segments': len(trace.lines),
        'no_lines_to_from_or_across_off_target_points': True,
        'accepted_capture_preserves_actual_values': True,
        'physical_outputs_always_low': True, 'no_files_or_network': True}))
finally:
    instrument.zero()
    if app is not None:
        app.cloud.discard_scan()
    print('OUTPUTS_DISABLED')
