"""Compiled-Pico manual-page regression with real LCD and synthetic sensors.

Imports production boot reserves first; never constructs physical PWM, writes
files or connects Wi-Fi. Run separately on a fresh interpreter. All APPLY
commands below are captured by FakeInstrument, not sent to the hardware.
"""
import main
import gc
import time
import model
import manual_debug
from engine import Engine
from ram_scan import PackedScanBuffer
from ui import App


class NoFiles:
    def save_settings(self, *args):
        raise AssertionError("Manual page must not save")


class Inputs:
    def __init__(self):
        self.event = None

    def poll(self, now):
        event, self.event = self.event, None
        return ((event,),) if event is not None else ()


class FakeInstrument:
    def __init__(self):
        self.drain = self.gate = 0.0
        self.reads = self.zeroes = 0
        self.fail = False

    def zero(self):
        self.drain = self.gate = 0.0
        self.zeroes += 1

    def set_duty(self, role, value):
        if role == "drain":
            self.drain = value
        else:
            self.gate = value

    def poll_alerts(self):
        pass

    def begin_measurement(self, count):
        assert count == 64

    def poll_measurement(self):
        return True

    def read_codes_into(self, codes):
        if self.fail:
            raise OSError("Synthetic sensor failure")
        self.reads += 1
        codes[0] = int((3.3 + 22.44 * self.drain) / .00125)
        codes[1] = 100 + self.reads % 8
        codes[2] = int(25.74 * self.gate / .0001953125)
        codes[3] = 400 if self.reads % 2 else -400


def run():
    instrument, inputs = FakeInstrument(), Inputs()
    restarts = [0]
    def restart():
        restarts[0] += 1
    display = main.Display(raster_pool=main._display_buffer)
    buffer = PackedScanBuffer(main._scan_bytes)
    app = App(display, inputs, Engine(instrument), dict(model.DEFAULT_SETTINGS),
              store=NoFiles(), scan_buffer=buffer, restart=restart)
    app.tick(time.ticks_ms())
    inputs.event = "left"
    app.tick(time.ticks_ms())
    assert app.screen == "settings"
    app.action("advanced")
    app.tick(time.ticks_ms())
    # Real clock/real LCD: opening, applying and editing cannot receive a stale
    # tick timestamp and falsely report that time moved backwards.
    app.action("manual")
    app.tick(time.ticks_ms())
    controller = app._manual
    assert app.screen == "manual" and not controller.outputs_enabled
    for _ in range(5):
        time.sleep_ms(30)
        app.tick(time.ticks_ms())
    assert controller.latest is not None and not controller.error
    assert len(controller.latest) == 4
    assert controller.latest["drain_bus_v"] == controller._codes[0] * .00125
    assert controller.latest["gate_bus_v"] == controller._codes[2] * .0001953125
    controller.set_targets(5000000, 2000000)
    inputs.event = "right"
    app.tick(time.ticks_ms())
    for _ in range(5):
        time.sleep_ms(30)
        app.tick(time.ticks_ms())
    assert controller.outputs_enabled and not controller.error
    assert instrument.drain > 0 and instrument.gate > 0
    app.action(("manual_edit", 1))
    app.tick(time.ticks_ms())
    assert app.screen == "edit" and instrument.drain == instrument.gate == 0
    app.edit_value = "1.25"
    inputs.event = "right"
    app.tick(time.ticks_ms())
    assert app.screen == "manual" and controller.outputs_enabled
    assert controller.targets_uv[1] == 1250000
    assert controller.duty_u16[1] == int(model.drive_duty("gate", 1.25) * 65535 + .5)
    assert controller.applied_duty_u16 == controller.duty_u16
    assert instrument.drain > 0 and instrument.gate > 0
    # Cancel from the keypad must not restore a previously active output.
    app.action(("manual_edit", 1))
    assert instrument.drain == instrument.gate == 0
    inputs.event = "left"
    app.tick(time.ticks_ms())
    assert app.screen == "manual" and not controller.outputs_enabled
    assert controller.applied_duty_u16 == [0, 0]
    # Speed up a long acquisition-memory check, keeping the production I/O
    # conversion and rendering paths but supplying a deterministic test clock.
    clock = manual_debug._ticks_ms
    try:
        manual_debug._ticks_ms = None
        controller.apply()
        now = 0
        for _ in range(30):
            now += 20
            app.tick(now)
        gc.collect()
        before = gc.mem_free()
        values, codes = controller.latest, controller._codes
        for _ in range(8192):
            now += 20
            controller.tick(now)
            assert controller.latest is values and controller._codes is codes
        assert controller.sample_revision >= 4096 and not controller.error
        # Repeated physical LCD updates exercise formatting/raster allocations.
        for _ in range(100):
            now += 200
            app.tick(now)
        gc.collect()
        after = gc.mem_free()
        assert after >= before - 2048, "Manual page retained growing heap"
        assert len(buffer) == 0
        instrument.fail = True
        for _ in range(3):
            now += 20
            app.tick(now)
        assert app.screen == "manual_error"
        assert instrument.drain == instrument.gate == 0
        app.action("right")
        assert app.screen == "advanced"
        instrument.fail = False
        app.action("manual")
        app.action("right")
        app.action("left")
        assert app.screen == "advanced" and instrument.drain == instrument.gate == 0
        app.action("right")
        app.action("left")
        app.tick(now + 20)
        assert restarts[0] == 1 and app.screen == "restarting"
        print("MANUAL_DEBUG_NATIVE_PASS", controller.sample_revision, before, after)
    finally:
        manual_debug._ticks_ms = clock
        instrument.zero()
