"""Zero-drive timing diagnostics for the integration loop, not a calibration."""
import time
from time import ticks_diff as cached_ticks_diff
import json
import gc
from hardware import Instrument
from inputs import Inputs
from engine import Engine
import model


def measure(function, count=100):
    gc.collect()
    started = time.ticks_us()
    for unused in range(count):
        function()
    return time.ticks_diff(time.ticks_us(), started) / count


class SyntheticInstrument:
    identity = {}
    def __init__(self):
        self.duty = {"drain": 0.0, "gate": 0.0}
    def set_duty(self, role, value):
        self.duty[role] = value
    def zero(self):
        pass
    def read_pair(self):
        return {role: {"bus_v": value * model.PWM_FULL_SCALE_V,
                       "shunt_v": 0.0, "terminal_v": value * model.PWM_FULL_SCALE_V}
                for role, value in self.duty.items()}


instrument = Instrument()
try:
    inputs = Inputs()
    engine = Engine(SyntheticInstrument())
    engine.start_calibration(dict(model.DEFAULT_SETTINGS))
    virtual_ms = 0
    def engine_tick():
        global virtual_ms
        virtual_ms += 4
        engine.tick(virtual_ms)
    def touch_poll():
        inputs._last_touch_poll = None
        inputs.poll(time.ticks_ms())
    def repeated_import():
        from time import ticks_diff
        return ticks_diff(123, 120)
    def cached_import():
        return cached_ticks_diff(123, 120)
    print("TIMING_US", json.dumps({
        "read_pair": measure(instrument.read_pair),
        "touch_poll": measure(touch_poll),
        "engine_tick_synthetic": measure(engine_tick, 600),
        "sleep_ms_2": measure(lambda: time.sleep_ms(2)),
        "repeated_time_import": measure(repeated_import),
        "cached_time_function": measure(cached_import),
    }))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
