"""Explicit open-output verification of expanded calibration and clipped scans.

Pass _confirm_open_fixture_firmware_test=True and _voltage_limit_test_mode as
"calibrate" or "scan" through mpremote exec before running this script.
Only successful calibration replaces calibration.json. Scan settings are RAM
overrides; the user's saved scan settings remain unchanged.
"""
if globals().get("_confirm_open_fixture_firmware_test") is not True:
    raise RuntimeError("Confirm open outputs and unbridged shunts before this test")
mode = globals().get("_voltage_limit_test_mode")
if mode not in ("calibrate", "scan"):
    raise RuntimeError("Choose calibrate or scan verification mode")
del _confirm_open_fixture_firmware_test
del _voltage_limit_test_mode

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


instrument = Instrument()
try:
    settings = storage.load_settings()
    assert settings["driver_supply_uv"] == 5000000 and settings["source_voltage_uv"] == 0
    original_settings = settings
    settings_before = file_hash("settings.json")
    calibration_before = file_hash("calibration.json")
    error_before = file_hash("last_error.log")
    settings = dict(settings)
    if mode == "scan":
        settings.update(drain_start_uv=0, drain_stop_uv=5000000, drain_step_uv=2500000,
                        gate_start_uv=0, gate_stop_uv=5000000, gate_step_uv=2500000)
    calibration = storage.load_json("calibration.json")
    app = App(Display(), Inputs(), Engine(instrument), settings, calibration,
              error_logger=record_error)
    app.render()
    if mode == "calibrate":
        # App owns the previous map until successful replacement; do not keep
        # an extra reference alive after it commits on the noncompacting heap.
        calibration = None
        gc.collect()
        # Start through the actual Advanced confirmation and persistence path.
        app.action("left")
        app.action("advanced")
        app.action("calibrate")
        assert app.screen == "confirm_calibration"
        app.action("right")
    else:
        model.validate_scan_calibration(calibration, settings)
        app.action("right")
    started = last_report = time.ticks_ms()
    last_point = 0
    limited_points = 0
    maximum = {"vds": -99.0, "vgs": -99.0, "id_a": 0.0, "ig_a": 0.0}
    while app.engine.state in ("scanning", "calibrating"):
        app.tick(time.ticks_ms())
        now = time.ticks_ms()
        if time.ticks_diff(now, started) > 600000:
            app.engine.cancel()
            raise RuntimeError(mode + " verification exceeded ten minutes")
        if time.ticks_diff(now, last_report) >= 10000:
            print("PROGRESS", mode, int(app.engine.progress * 100),
                  time.ticks_diff(now, started) // 1000)
            last_report = now
        if len(app.curves) > last_point:
            last_point = len(app.curves)
            row = app.engine.latest
            for key in maximum:
                maximum[key] = max(maximum[key], abs(row[key]))
            limited = row["drain_voltage_limited"] or row["gate_voltage_limited"]
            limited_points += int(limited)
            for role, key in (("drain", "vds"), ("gate", "vgs")):
                requested = row["requested_" + key]
                if requested == 5.0:
                    assert row[role + "_voltage_limited"], role + " missing clipping flag"
                    assert row[key] < requested - model.VOLTAGE_TOLERANCE_V
                elif requested == 0.0:
                    assert not row[role + "_voltage_limited"], role + " zero target falsely limited"
            print("POINT", json.dumps({key: row[key] for key in (
                "index", "requested_vds", "requested_vgs", "vds", "vgs",
                "id_a", "ig_a", "id_std_a", "ig_std_a", "batches_completed",
                "drain_voltage_limited", "gate_voltage_limited")}))
        time.sleep_ms(2)
    assert app.engine.state == "complete", app.engine.error or app.engine.state
    assert storage.load_settings() == original_settings, "Saved scan settings changed"
    if mode == "scan":
        assert settings_before == file_hash("settings.json"), "Scan rewrote settings"
    assert error_before == file_hash("last_error.log"), "Verification generated a new error log"
    if mode == "calibrate":
        saved = storage.load_json("calibration.json")
        model.validate_calibration(saved, settings)
        assert saved["version"] == 2
        for role in ("drain", "gate"):
            channel = saved["channels"][role]
            assert channel["capability"]["complete"] is True
            print("CALIBRATION_CHANNEL", role, json.dumps({
                "points": len(channel["points"]), "first": channel["points"][0],
                "last": channel["points"][-1], "capability": channel["capability"]}))
    else:
        assert len(app.curves) == 9
        assert limited_points == 5, "Expected drain-only, gate-only and combined clipping"
        assert calibration_before == file_hash("calibration.json")
        assert app.screen == "complete"
        # Both completion controls must retain the existing Home behavior.
        app.action("right")
        assert app.screen == "home"
    time.sleep_ms(250)
    idle = instrument.read_pair()
    assert all(abs(idle[role]["terminal_v"]) < 0.050 for role in ("drain", "gate"))
    gc.collect()
    print("VOLTAGE_LIMIT_TEST", json.dumps({"mode": mode,
          "elapsed_ms": time.ticks_diff(time.ticks_ms(), started),
          "points": last_point, "limited_points": limited_points, "maximum": maximum,
          "settings_preserved": True, "no_new_error_log": True,
          "idle": idle, "touch_errors": app.inputs.status["touch_error_count"],
          "free_heap_bytes": gc.mem_free()}))
except Exception as error:
    instrument.close()
    record_error("voltage-limit " + mode + " verification", error)
    raise
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
