"""Open-fixture cancellation at nonzero voltage; requires explicit confirmation."""
if globals().get("_confirm_open_fixture_firmware_test") is not True:
    raise RuntimeError("Confirm open outputs and unbridged shunts before this test")
del _confirm_open_fixture_firmware_test

import json
import hashlib
import time
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
from ui import App
import storage


def calibration_hash():
    digest = hashlib.sha256()
    with open("calibration.json", "rb") as stream:
        while True:
            chunk = stream.read(512)
            if not chunk:
                break
            digest.update(chunk)
    return digest.digest()


instrument = Instrument()
try:
    settings = storage.load_settings()
    assert settings["driver_supply_uv"] == 5000000 and settings["source_voltage_uv"] == 0
    calibration = storage.load_json("calibration.json")
    app = App(Display(), Inputs(), Engine(instrument), settings, calibration)
    app.settings = dict(settings)
    for role in ("drain", "gate"):
        app.settings[role + "_start_uv"] = 1000000
        app.settings[role + "_stop_uv"] = 1000000
    app.render()
    app.action("right")
    started = time.ticks_ms()
    while app.engine.state == "scanning":
        app.tick(time.ticks_ms())
        if time.ticks_diff(time.ticks_ms(), started) > 30000:
            raise RuntimeError("One-point scan exceeded 30 seconds")
        time.sleep_ms(2)
    assert app.engine.state == "complete" and len(app.curves) == 1, app.engine.error
    measured = {key: app.latest[key] for key in ("vds", "vgs", "id_a", "ig_a", "batches_completed")}
    app.action("left")
    app.action("right")
    started = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), started) < 1800:
        app.tick(time.ticks_ms())
        time.sleep_ms(2)
    assert app.engine.state == "scanning", app.engine.error
    before = instrument.read_pair()
    assert all(abs(before[role]["terminal_v"] - 1.0) < 0.030 for role in ("drain", "gate"))
    started = time.ticks_ms()
    app.action("left")
    cancel_ms = time.ticks_diff(time.ticks_ms(), started)
    app.tick(time.ticks_ms())
    assert app.engine.state == "cancelled" and app.screen == "home"
    time.sleep_ms(250)
    after = instrument.read_pair()
    assert all(abs(after[role]["terminal_v"]) < 0.050 for role in ("drain", "gate"))
    # Exercise streaming persistence after the heap has seen an acquisition.
    storage.save_json_atomic("calibration.json", calibration)
    original = calibration_hash()
    app.settings = settings
    app.action("left")
    app.action("advanced")
    app.action("calibrate")
    app.action("right")
    app.tick(time.ticks_ms())
    app.action("left")
    app.tick(time.ticks_ms())
    assert app.engine.state == "cancelled" and app.screen == "advanced"
    assert calibration_hash() == original
    app.goto("home")
    app.render()
    print("CANCEL_TEST", json.dumps({"completed_point": measured, "before": before, "after": after,
          "cancel_command_ms": cancel_ms, "prior_calibration_preserved": True}))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
