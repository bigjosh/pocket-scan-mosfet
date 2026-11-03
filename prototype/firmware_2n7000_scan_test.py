"""Loaded 2N7000 scan using saved calibration, with no persistent changes.

Explicitly confirm the attached transistor before running. This script never
calibrates. The saved user's settings remain intact; test ranges live in RAM.
"""
if globals().get("_confirmed_scan_fixture") != "2n7000":
    raise RuntimeError("Confirm the attached 2N7000 before this test")
del _confirmed_scan_fixture

import gc
import hashlib
import json
import time
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
    before = {name: file_hash(name) for name in ("settings.json", "calibration.json", "last_error.log")}
    settings = dict(settings)
    # This transistor previously exceeded 5 mA at VGS=2 V. Keep its check
    # below that region while exercising reachable and clipped drain requests.
    settings.update(drain_start_uv=0, drain_stop_uv=5000000, drain_step_uv=1000000,
                    gate_start_uv=0, gate_stop_uv=1500000, gate_step_uv=500000,
                    batches=10, samples_per_batch=50)
    calibration = storage.load_json("calibration.json")
    model.validate_scan_calibration(calibration, settings)
    engine = Engine(instrument)
    app = App(Display(), Inputs(), engine, settings, calibration, error_logger=record_error)
    app.render()
    app.action("right")
    expected = 24
    started = last_report = time.ticks_ms()
    last_point = limited_points = 0
    maximum = {"id_a": 0.0, "ig_a": 0.0, "vds": 0.0, "vgs": 0.0}
    while engine.state == "scanning":
        app.tick(time.ticks_ms())
        now = time.ticks_ms()
        if time.ticks_diff(now, started) > 600000:
            engine.cancel()
            raise RuntimeError("Loaded scan exceeded ten-minute bound")
        if time.ticks_diff(now, last_report) >= 10000:
            print("PROGRESS", int(engine.progress * 100), time.ticks_diff(now, started) // 1000)
            last_report = now
        if len(app.curves) > last_point:
            last_point = len(app.curves)
            row = engine.latest
            limited_points += int(row["drain_voltage_limited"] or row["gate_voltage_limited"])
            for key in maximum:
                maximum[key] = max(maximum[key], abs(row[key]))
            print("POINT", json.dumps({key: row[key] for key in (
                "index", "curve", "requested_vds", "requested_vgs", "vds", "vgs",
                "id_a", "ig_a", "id_std_a", "ig_std_a", "batches_completed",
                "drain_voltage_limited", "gate_voltage_limited")}))
    assert engine.state == "complete", engine.error or engine.state
    assert len(app.curves) == expected
    assert all(before[name] == file_hash(name) for name in before)
    time.sleep_ms(250)
    idle = instrument.read_pair()
    assert all(abs(idle[role]["terminal_v"]) < 0.050 for role in ("drain", "gate"))
    gc.collect()
    print("LOADED_SCAN", json.dumps({"fixture": "2n7000", "points": last_point,
          "limited_points": limited_points, "maximum": maximum,
          "elapsed_ms": time.ticks_diff(time.ticks_ms(), started), "idle": idle,
          "settings_and_calibration_preserved": True, "no_new_error_log": True,
          "touch_errors": app.inputs.status["touch_error_count"],
          "free_heap_bytes": gc.mem_free()}))
except Exception as error:
    instrument.close()
    record_error("loaded 2N7000 scan verification", error)
    raise
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
