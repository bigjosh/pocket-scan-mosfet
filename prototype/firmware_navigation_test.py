"""Replay settings save/navigation and idle on Pico, with outputs held at zero."""
import time
import gc
import json
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
from ui import App
from diagnostics import record_error
import storage


class ObserveInputs:
    def __init__(self):
        self.real = Inputs()
    def poll(self, now_ms):
        self.real.poll(now_ms)
        return []  # This diagnostic dispatches only the two specified actions.


instrument = Instrument()
try:
    inputs = ObserveInputs()
    app = App(Display(), inputs, Engine(instrument), storage.load_settings(),
              storage.load_json("calibration.json"), error_logger=record_error)
    app.tick(time.ticks_ms())
    for cycle in range(20):
        app.action("left")
        app.tick(time.ticks_ms())
        assert app.screen == "settings"
        app.action("right")
        app.tick(time.ticks_ms())
        assert app.screen == "home", getattr(app, "notice", "not home")
    print("NAVIGATION_CYCLES_PASSED", 20)
    started = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), started) < 60000:
        app.tick(time.ticks_ms())
        time.sleep_ms(2)
    gc.collect()
    print("NAVIGATION_TEST", json.dumps({"settings_ok_cycles": 20,
          "idle_ms": time.ticks_diff(time.ticks_ms(), started),
          "screen": app.screen, "touch_errors": inputs.real.status["touch_error_count"],
          "free_heap_bytes": gc.mem_free()}))
except Exception as error:
    instrument.close()
    record_error("navigation reproduction", error)
    raise
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
