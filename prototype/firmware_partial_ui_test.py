"""Synthetic partial-result UI check; all physical pins remain low.

Uses the real Instrument decoder, Engine and App with injected averaged raw
codes and a RAM-only handoff capture. No PWM, I2C, network or file operations.
This diagnostic cannot produce upload fixtures; legacy batch archives describe
an acquisition method that is no longer used by the scanner.
"""
import main
import json

if "display" not in globals():
    display = main.Display(raster_pool=main._display_buffer)


class ZeroInstrument(main.Instrument):
    def __init__(self):
        # Intentionally do not create PWM peripherals or an I2C controller.
        self._pwms, self._closed = {}, False
        self._measurement_pending, self._ready_mask = False, 0
        self.identity = {"synthetic_test": True}
        self.engine, self.stop_after, self.zeroes = None, 0, 0
        self.zero()

    def set_duty(self, role, fraction):
        assert role in ("drain", "gate") and 0 <= fraction <= 1
        # Logical 0/1/2 mV requests exercise normal sweep bookkeeping only.
        # No requested fraction is ever passed to a PWM or GPIO high command.
        self.zero()

    def zero(self):
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
        self.zeroes += 1

    def begin_measurement(self, count):
        self._measurement_pending, self._ready_mask = True, 3

    def poll_measurement(self):
        return True

    def poll_alerts(self):
        pass

    def _read16(self, address, register):
        assert address in (0x44, 0x41) and register in (1, 2)
        if address == 0x44 and register == 1 and self.engine._index >= self.stop_after:
            return 0x7fff
        return 0


class Inputs:
    def poll(self, unused):
        return []


class Store:
    def load_json(self, path):
        assert path == "__partial_memory_config"
        return None

    def save_json_atomic(self, *unused):
        raise AssertionError("Synthetic test must not save settings")

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


def run_case(count, accept):
    global now
    instrument.stop_after = count
    accepted = len(capture.accepted)
    app.action("right")
    for unused in range(8):
        if app.screen != "preparing":
            break
        now += 20
        app.tick(now)
    assert engine.state == "scanning"
    for unused in range(1000):
        now += 20
        app.tick(now)
        if engine.state != "scanning":
            break
    assert engine.state == "partial" and app.screen == "partial"
    assert engine.shutdown_succeeded and app._outputs_safe and app._archive_ready
    assert len(app.curves) == count and engine.fault["index"] == count
    assert engine.fault["channel"] == "drain" and engine.fault["kind"] == "shunt_saturation"
    assert len(capture.accepted) == accepted
    if count:
        assert app.latest["vds"] == app.latest["vgs"] == app.latest["id_a"] == app.latest["ig_a"] == 0
    else:
        assert app.latest is None
    assert any(hit[4] == "left" and hit[0] < 240 and hit[1] >= 270 for hit in app.hits)
    assert any(hit[4] == "right" and hit[0] >= 240 and hit[1] >= 270 for hit in app.hits)
    app.action("right" if accept else "left")
    assert app.screen == "home" and len(capture.accepted) == accepted + int(accept)
    if accept:
        rows, fault = capture.accepted[-1]
        assert len(rows) == count and fault["index"] == count
        assert fault["measured_value"] is None
    assert not capture.rows


app, instrument, now = None, None, 0
try:
    settings = dict(main.model.DEFAULT_SETTINGS)
    settings.update(drain_start_uv=0, drain_stop_uv=2000, drain_step_uv=1000,
                    gate_start_uv=0, gate_stop_uv=0, gate_step_uv=1000,
                    driver_supply_uv=5000000, source_voltage_uv=0, ina_average_count=4)
    calibration = {"version": 1, "metadata": {"driver_supply_v": 5.0,
        "pwm_hz": main.model.PWM_HZ, "synthetic_test": True, "synthetic_clock": True,
        "label": "Synthetic overrange test - outputs held low"}, "channels": {
        role: {"shunt_ohm": shunt, "points": [
            {"terminal_v": voltage, "duty": voltage / main.model.PWM_FULL_SCALE_V, "shunt_v": 0.0}
            for voltage in (0.0, 3.0)]} for role, shunt in
        (("drain", main.model.DRAIN_SHUNT_OHM), ("gate", main.model.GATE_SHUNT_OHM))}}
    calibration["metadata"].update(
        acquisition_mode=main.model.ACQUISITION_MODE, ina_average_count=4,
        bus_conversion_us=1100, shunt_conversion_us=1100,
        calibration_repeat_blocks=main.model.CALIBRATION_REPEAT_BLOCKS)
    instrument = ZeroInstrument()
    engine = main.Engine(instrument, clock_ms=lambda: engine._last_clock)
    instrument.engine = engine
    capture = MemoryCapture()
    app = main.App(display, Inputs(), engine, settings, calibration, store=Store(),
                   cloud=capture, plot_buffers=main._plot_buffers)
    run_case(1, True)
    run_case(0, False)
    run_case(0, True)
    assert len(capture.accepted) == 2
    print("PARTIAL_UI_NATIVE", json.dumps({"synthetic_test": True,
        "one_point_partial_captured": True, "zero_point_partial_captured": True,
        "cancel_discards_only_unaccepted": True, "not_accepted_before_ok": True,
        "completed_measurements_only": True, "footer_mapping_verified": True,
        "physical_outputs_always_low": True, "no_files_or_network": True}))
finally:
    main.Pin(21, main.Pin.OUT, value=0)
    main.Pin(20, main.Pin.OUT, value=0)
    if instrument is not None:
        instrument.zero()
    if app is not None:
        app.cloud.discard_scan()
    print("OUTPUTS_DISABLED")
