"""Scan using saved calibration; count natural zero-edge samples.

No calibration/settings files are changed. Explicit setup confirmation is
required, as for the other bench drive diagnostics.
"""
fixture = globals().get("_confirmed_scan_fixture")
if fixture not in ("open", "2n7000"):
    raise RuntimeError("Confirm the open fixture or attached 2N7000 before this test")
del _confirmed_scan_fixture

import gc
import json
import time
import hashlib
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
from ui import App
from diagnostics import record_error
import model
import storage


def file_hash(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(512)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        if error.args[0] == 2:
            return None
        raise
    return digest.digest()


class ObservedInstrument:
    def __init__(self, hardware, calibration):
        self.hardware = hardware
        self.identity = hardware.identity
        self.engine = None
        self.calibration = calibration
        self.zero_readings = {role: {"count": 0, "minimum_v": 99.0,
                                    "maximum_v": -99.0, "below_old_boundary": 0}
                              for role in ("drain", "gate")}

    def set_duty(self, role, duty):
        self.hardware.set_duty(role, duty)

    def zero(self):
        self.hardware.zero()

    def read_pair(self):
        pair = self.hardware.read_pair()
        engine = self.engine
        if engine is not None and engine.state == "scanning" and engine._phase in ("settle", "sample"):
            for role in ("drain", "gate"):
                if engine._target[role] == 0.0 and engine._applied.get(role) == 0.0:
                    value = pair[role]["terminal_v"]
                    stats = self.zero_readings[role]
                    stats["count"] += 1
                    stats["minimum_v"] = min(stats["minimum_v"], value)
                    stats["maximum_v"] = max(stats["maximum_v"], value)
                    threshold = self.calibration["channels"][role]["points"][0]["terminal_v"] - model.CALIBRATION_EDGE_TOLERANCE_V
                    if value < threshold:
                        stats["below_old_boundary"] += 1
        return pair


hardware = Instrument()
try:
    saved_settings = storage.load_settings()
    assert saved_settings["driver_supply_uv"] == 5000000 and saved_settings["source_voltage_uv"] == 0
    calibration = storage.load_json("calibration.json")
    settings_before = file_hash("settings.json")
    calibration_before = file_hash("calibration.json")
    error_before = file_hash("last_error.log")
    # Verify the exact reported edge case on native MicroPython, independently
    # of whether today's physical samples happen to cross that same boundary.
    drain = calibration["channels"]["drain"]
    baseline = drain["points"][0]["shunt_v"]
    assert model.corrected_current(drain, 0.0013, baseline, zero_drive=True) == 0.0
    rejected_nonzero_drive = False
    try:
        model.corrected_current(drain, 0.0013, baseline)
    except ValueError:
        rejected_nonzero_drive = True
    assert rejected_nonzero_drive
    print("NATIVE_REGRESSION_PASSED", "1.3mV accepted only for explicit zero drive")
    instrument = ObservedInstrument(hardware, calibration)
    engine = Engine(instrument)
    instrument.engine = engine
    # Repeatable check; the user's stored settings survive. This particular
    # 2N7000 exceeded the 5mA limit at VGS2V, so keep its check below that point.
    settings = dict(model.DEFAULT_SETTINGS)
    if fixture == "2n7000":
        settings["gate_stop_uv"] = 1500000
    expected_points = model.scan_point_count(settings)
    app = App(Display(), Inputs(), engine, settings, calibration, error_logger=record_error)
    app.render()
    app.action("right")
    started = last_report = time.ticks_ms()
    last_point = 0
    while engine.state == "scanning":
        app.tick(time.ticks_ms())
        now = time.ticks_ms()
        if time.ticks_diff(now, started) > 600000:
            engine.cancel()
            raise RuntimeError("Scan exceeded ten-minute bound")
        if time.ticks_diff(now, last_report) >= 10000:
            print("PROGRESS", int(engine.progress * 100), time.ticks_diff(now, started) // 1000)
            last_report = now
        if len(app.curves) > last_point:
            last_point = len(app.curves)
            row = engine.latest
            print("POINT", json.dumps({key: row[key] for key in (
                "index", "vds", "vgs", "id_a", "ig_a", "id_std_a", "ig_std_a", "batches_completed")}))
        time.sleep_ms(2)
    assert engine.state == "complete", engine.error or engine.state
    assert len(app.curves) == expected_points
    assert settings_before == file_hash("settings.json")
    assert calibration_before == file_hash("calibration.json")
    assert error_before == file_hash("last_error.log")
    time.sleep_ms(250)
    idle = hardware.read_pair()
    assert all(abs(idle[role]["terminal_v"]) < 0.050 for role in ("drain", "gate"))
    print("ZERO_EDGE_SCAN", json.dumps({"fixture": fixture, "points": len(app.curves),
          "elapsed_ms": time.ticks_diff(time.ticks_ms(), started),
          "zero_readings": instrument.zero_readings, "idle": idle,
          "settings_and_calibration_preserved": True, "no_new_error_log": True,
          "touch_errors": app.inputs.status["touch_error_count"]}))
except Exception as error:
    hardware.close()
    record_error("zero-edge scan verification", error)
    raise
finally:
    hardware.close()
    print("OUTPUTS_DISABLED")
