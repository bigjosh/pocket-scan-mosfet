"""Run on Pico via mpremote; every hardware PWM duty remains zero."""

import gc
import json
import os
import time
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
from ui import App
import model
import storage

instrument = Instrument()
report = {"identity": instrument.identity}
try:
    display = Display()
    inputs = Inputs()
    report["inputs"] = inputs.status
    app = App(display, inputs, Engine(instrument))
    timings = {}
    app.draft = dict(model.DEFAULT_SETTINGS)
    app.advanced_entry = dict(app.draft)
    app.edit_label, app.edit_value = "VGS step (V)", "0.5"
    for screen in ("home", "settings", "advanced", "edit", "confirm_calibration"):
        app.goto(screen)
        started = time.ticks_ms()
        app.render()
        timings[screen] = time.ticks_diff(time.ticks_ms(), started)
    report["screen_render_ms"] = timings
    # Synthetic zero values exercise display timing only, never drive hardware.
    app.latest = {"vds": 0.0, "vgs": 0.0, "id_a": 0.0, "ig_a": 0.0,
                  "batches_completed": 1}
    app.goto("scan")
    app.render()
    app._render_live()
    started = time.ticks_ms()
    app._render_live()
    report["warm_live_update_ms"] = time.ticks_diff(time.ticks_ms(), started)
    app.goto("edit")
    app.render()
    started = time.ticks_ms()
    app._render_edit_value()
    report["keypad_value_update_ms"] = time.ticks_diff(time.ticks_ms(), started)
    path = "firmware-smoke-test.json"
    if path in os.listdir():
        raise RuntimeError("Refusing to overwrite an existing smoke-test file")
    try:
        storage.save_json_atomic(path, {"generation": 1})
        storage.save_json_atomic(path, {"generation": 2})
        assert storage.load_json(path) == {"generation": 2}
        report["atomic_replace_roundtrip"] = True
    finally:
        if path in os.listdir():
            os.remove(path)
    totals = {"drain": [0.0, 0.0], "gate": [0.0, 0.0]}
    for unused in range(50):
        pair = instrument.read_pair()
        for role in totals:
            totals[role][0] += pair[role]["terminal_v"]
            totals[role][1] += pair[role]["shunt_v"]
        inputs.poll(time.ticks_ms())
        time.sleep_ms(4)
    report["zero_drive_means"] = {role: {"terminal_v": value[0] / 50,
                                              "shunt_v": value[1] / 50}
                                    for role, value in totals.items()}
    report["touch_error_count"] = inputs.status["touch_error_count"]
    app.goto("home")
    app.render()
    gc.collect()
    report["free_heap_bytes"] = gc.mem_free()
finally:
    instrument.close()
    report["outputs_disabled"] = True
print(json.dumps(report))
