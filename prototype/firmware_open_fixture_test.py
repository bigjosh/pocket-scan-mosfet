"""Explicitly authorized open-fixture calibration + full default UI scan.

Invoke only after confirming both shunts unbridged and DUT/loads/probes removed:
mpremote ... exec '_confirm_open_fixture_firmware_test=True' run this_file.py
"""

if globals().get("_confirm_open_fixture_firmware_test") is not True:
    raise RuntimeError("Confirm open outputs and unbridged shunts before this test")
del _confirm_open_fixture_firmware_test

import json
import time
import gc
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
from ui import App
import model
import storage


def run_operation(app, label):
    started = last_report = time.ticks_ms()
    last_point = 0
    while app.engine.state in ("scanning", "calibrating"):
        app.tick(time.ticks_ms())
        now = time.ticks_ms()
        if time.ticks_diff(now, started) > 600000:
            app.engine.cancel()
            raise RuntimeError(label + " exceeded 10-minute bound")
        if time.ticks_diff(now, last_report) >= 10000:
            print("PROGRESS", label, int(app.engine.progress * 100),
                  time.ticks_diff(now, started) // 1000)
            last_report = now
        if len(app.curves) > last_point:
            last_point = len(app.curves)
            row = app.engine.latest
            print("POINT", json.dumps({key: row[key] for key in
                  ("index", "vds", "vgs", "id_a", "ig_a", "id_std_a", "ig_std_a", "batches_completed")}))
        time.sleep_ms(2)
    if app.engine.state != "complete":
        raise RuntimeError(label + " did not complete: " + str(app.engine.error or app.engine.state))
    return time.ticks_diff(time.ticks_ms(), started)


instrument = Instrument()
try:
    display, inputs = Display(), Inputs()
    app = App(display, inputs, Engine(instrument), dict(model.DEFAULT_SETTINGS))
    app.render()
    app.action("left")
    app.action("advanced")
    app.action("calibrate")
    assert app.screen == "confirm_calibration"
    app.action("right")
    calibration_ms = run_operation(app, "calibration")
    if app.calibration is None:
        raise RuntimeError("Completed calibration was not saved: " + app.notice)
    saved = storage.load_json("calibration.json")
    model.validate_calibration(saved, storage.load_settings())
    print("CALIBRATION_SAVED", json.dumps({role: {
        "points": len(saved["channels"][role]["points"]),
        "first": saved["channels"][role]["points"][0],
        "last": saved["channels"][role]["points"][-1]}
        for role in ("drain", "gate")}))
    app.goto("home")
    app.render()
    app.action("right")
    scan_ms = run_operation(app, "scan")
    assert app.screen == "complete"
    point_count = len(app.curves)  # Points already streamed individually above.
    # Exercise cancellation after real drive starts, without replacing calibration.
    app.action("right")
    assert app.screen == "home"
    app.action("right")
    deadline = time.ticks_add(time.ticks_ms(), 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        app.tick(time.ticks_ms())
        time.sleep_ms(2)
    app.action("left")
    app.tick(time.ticks_ms())
    assert app.engine.state == "cancelled" and app.screen == "home"
    time.sleep_ms(250)
    idle = instrument.read_pair()
    assert all(abs(idle[role]["terminal_v"]) < 0.050 for role in ("drain", "gate"))
    gc.collect()
    print("TEST_RESULT", json.dumps({"calibration_ms": calibration_ms, "scan_ms": scan_ms,
          "point_count": point_count, "cancel_verified": True, "idle": idle,
          "touch_errors": inputs.status["touch_error_count"], "free_heap_bytes": gc.mem_free()}))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
