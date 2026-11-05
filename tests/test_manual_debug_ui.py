"""Manual-page lifecycle with the real cooperative controller; no hardware."""

import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine, OverrangeError
from firmware.manual_debug import ManualDebug
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App, RED, INK
from test_ram_ui import RamCloud, RamWifi
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore


class Display(FakeDisplay):
    def __init__(self):
        super().__init__()
        self.draws, self.fills = [], 0
        self.error = None

    def fill(self, color):
        self.fills += 1
        super().fill(color)

    def text(self, text, x, y, color, bg=None, scale=1):
        if self.error:
            raise self.error
        self.draws.append((str(text), x, y, color))
        super().text(text, x, y, color, bg, scale)


class Instrument(HostInstrument):
    def __init__(self):
        super().__init__()
        self.zero_error = False
        self.gate_current = 0.5e-6

    def zero(self):
        if self.zero_error:
            raise OSError("Cannot zero PWM")
        super().zero()

    def read_pair(self):
        values = super().read_pair()
        gate = values["gate"]
        gate["shunt_v"] = self.gate_current * model.GATE_SHUNT_OHM
        gate["bus_v"] = gate["terminal_v"] + gate["shunt_v"]
        return values


class ManualDebugUITests(unittest.TestCase):
    def setUp(self):
        self.display, self.inputs = Display(), FakeInputs()
        self.instrument, self.store = Instrument(), MemoryStore()
        self.engine = Engine(self.instrument)
        self.buffer = PackedScanBuffer(capacity=32)
        self.cloud, self.wifi = RamCloud(self.buffer), RamWifi()
        self.restarts, self.constructed = [], []
        self.app = App(self.display, self.inputs, self.engine, model.DEFAULT_SETTINGS,
                       store=self.store, cloud=self.cloud, wifi=self.wifi,
                       scan_buffer=self.buffer, manual_factory=self.factory,
                       restart=lambda: self.restarts.append(True))
        self.now = 0
        self.tick()

    def factory(self, instrument, settings, baseline=None):
        self.constructed.append(dict(settings))
        return ManualDebug(instrument, settings, baseline)

    def tick(self, action=None, ms=20):
        self.now += ms
        self.inputs.events = [(action,)] if action else []
        self.app.tick(self.now)

    def touch(self, action):
        for x, y, width, height, target in self.app.hits:
            if target == action:
                self.now += 20
                self.inputs.events = [("touch", x + width // 2, y + height // 2)]
                self.app.tick(self.now)
                return
        self.fail("No touch target %r on %s" % (action, self.app.screen))

    def advanced(self):
        self.tick("left")
        self.touch("advanced")

    def open(self):
        self.advanced()
        self.touch("manual")
        self.assertEqual(self.app.screen, "manual")

    def edit(self, channel, value):
        self.touch(("manual_edit", channel))
        self.touch(("key", "CLEAR"))
        for char in value:
            self.touch(("key", char))
        self.tick("right")

    def test_entry_is_lazy_off_and_both_channels_show_red_off(self):
        self.assertEqual(self.constructed, [])
        self.advanced()
        self.assertEqual(self.constructed, [])
        self.touch("manual")
        controller = self.app._manual
        self.assertFalse(controller.outputs_enabled)
        self.assertEqual(controller.targets_uv, [0, 0])
        self.assertTrue(all(value == 0 for _, value in self.instrument.commands))
        self.assertEqual(self.instrument.duties, {"drain": 0, "gate": 0})
        self.assertEqual(controller.duty_u16, [0, 0])
        self.assertIn("Drain target (V to GND)", self.display.labels)
        self.assertIn("Gate target (V to GND)", self.display.labels)
        self.assertFalse(any("PWM " in text or "Next PWM" in text for text in self.display.labels))
        self.assertIn(("OFF", 16, 119, RED), self.display.draws)
        self.assertIn(("OFF", 248, 119, RED), self.display.draws)
        self.assertTrue(any(text == "OFF" and y == 12 for text, x, y, color in self.display.draws))
        self.assertTrue(any("OFF | OK applies targets" in text for text in self.display.labels))
        self.assertEqual(self.store.writes, 0)

    def test_target_keypad_ok_is_ram_only_and_immediately_applies_both_targets(self):
        self.open()
        original = dict(self.app.draft)
        self.edit(0, "5")
        self.edit(1, "2.5")
        controller = self.app._manual
        self.assertEqual(controller.targets_uv, [5000000, 2500000])
        self.assertEqual(self.app.draft, original)
        self.assertTrue(controller.outputs_enabled)
        preview = list(controller.duty_u16)
        self.assertEqual(preview, [int(model.drive_duty("drain", 5) * 65535 + .5),
                                   int(model.drive_duty("gate", 2.5) * 65535 + .5)])
        self.assertEqual(self.instrument.commands[-2:],
                         [("drain", preview[0] / 65535), ("gate", preview[1] / 65535)])
        self.assertEqual(self.app.screen, "manual")
        self.assertIn("ON", self.display.labels)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(len(self.buffer), 0)

    def test_edit_stops_before_keypad_and_cancel_does_not_apply(self):
        self.open()
        self.tick("right")
        self.touch(("manual_edit", 1))
        self.assertEqual(self.app.screen, "edit")
        self.assertFalse(self.app._manual.outputs_enabled)
        self.assertTrue(all(value == 0 for value in self.instrument.duties.values()))
        self.touch(("key", "2"))
        self.tick("left")
        self.assertEqual(self.app.screen, "manual")
        self.assertEqual(self.app._manual.targets_uv, [0, 0])
        self.assertFalse(self.app._manual.outputs_enabled)

    def test_physical_and_touch_cancel_stop_then_return_advanced(self):
        for use_touch in (False, True):
            if self.app.screen == "home":
                self.open()
            else:
                self.touch("manual")
            self.touch("right")
            self.assertTrue(self.app._manual.outputs_enabled)
            self.touch("left") if use_touch else self.tick("left")
            self.assertEqual(self.app.screen, "advanced")
            self.assertFalse(self.app._manual.outputs_enabled)
            self.assertTrue(all(value == 0 for value in self.instrument.duties.values()))
        self.tick("right")
        self.tick("left")
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.store.writes, 0)

    def test_debug_uses_draft_source_supply_and_averaging_without_saving(self):
        self.advanced()
        self.app.draft.update(source_voltage_uv=3200000, driver_supply_uv=12000000,
                              ina_average_count=4)
        self.touch("manual")
        controller = self.app._manual
        self.assertEqual(controller.settings, self.app.draft)
        self.assertEqual(controller.duty_u16[1], 0)
        for _ in range(12):
            self.tick()
        self.assertIn(4, self.instrument.measurements)
        self.assertEqual(self.store.writes, 0)

    def test_radio_paused_through_manual_and_keypad_resumes_only_after_safe_exit(self):
        self.open()
        self.wifi.calls.clear()
        for _ in range(15):
            self.tick()
        self.touch(("manual_edit", 1))
        self.tick()
        self.tick("left")
        self.assertEqual(self.wifi.calls, [])
        self.wifi.resume_check = lambda: self.assertTrue(all(v == 0 for v in self.instrument.duties.values()))
        self.tick("left")
        self.assertIn(("resume",), self.wifi.calls)
        self.assertNotIn("begin", self.cloud.calls)
        self.assertNotIn("point", self.cloud.calls)

    def test_new_page_resets_manual_targets_and_never_changes_scan_settings(self):
        self.open()
        self.edit(0, "2")
        self.tick("left")
        self.touch("manual")
        self.assertEqual(self.app._manual.targets_uv, [0, 0])
        self.assertEqual(self.app.draft, self.app.settings)

    def test_live_values_reuse_latest_no_history_and_red_ig_both_signs(self):
        self.instrument.gate_current = 100e-6
        self.open()
        for _ in range(14):
            self.tick()
        row_id = id(self.app._manual.latest)
        self.assertIsNotNone(self.app._manual.latest)
        # Display native bus voltages despite the configured source and a
        # nonzero shunt drop; targets and readback both use ground-relative volts.
        pair = self.instrument.read_pair()
        for role, x in (("drain", 16), ("gate", 248)):
            shown = next(text for text, px, py, _ in reversed(self.display.draws)
                         if (px, py) == (x, 160))
            self.assertEqual(shown, "%.3f" % pair[role]["bus_v"])
        self.assertIn("Actual VBUS (V to GND)", self.display.labels)
        fills = self.display.fills
        for current in (1.01e-6, -1.01e-6, 0.5e-6):
            self.instrument.gate_current = current
            for _ in range(15):
                self.tick()
            self.assertEqual(id(self.app._manual.latest), row_id)
            color = next(color for text, x, y, color in reversed(self.display.draws) if (x, y) == (248, 204))
            self.assertEqual(color, RED if abs(current) > 1e-6 else INK)
        self.assertEqual(self.display.fills, fills)
        self.assertEqual(len(self.app.rows), 0)
        self.assertEqual(len(self.buffer), 0)
        self.assertEqual(len(self.app._manual_cache), 6)

    def test_live_render_is_throttled_to_200ms(self):
        self.open()
        rendered = self.app._manual_render_ms
        for _ in range(9):
            self.tick()
        self.assertEqual(self.app._manual_render_ms, rendered)
        self.tick()
        self.assertEqual(self.app._manual_render_ms - rendered, 200)

    def test_start_apply_and_editor_return_wait_for_fresh_loop_timestamp(self):
        # Production start/apply read ticks_ms after the UI captured now_ms.
        # Reproduce a one-ms delay and ensure no same-turn tick moves backwards.
        with patch("firmware.manual_debug._ticks_ms", lambda: self.now + 1):
            self.open()
            self.assertEqual(self.app._manual.state, "monitoring")
            self.assertEqual(self.app.screen, "manual")
            self.tick()
            self.tick("right")
            self.assertTrue(self.app._manual.outputs_enabled)
            self.assertEqual(self.app._manual.state, "monitoring")
            self.tick()
            self.touch(("manual_edit", 1))
            self.tick("left")
            self.assertEqual(self.app.screen, "manual")
            self.assertEqual(self.app._manual.state, "monitoring")
            self.assertFalse(self.app._manual.outputs_enabled)
            self.tick()
            self.assertIsNone(self.app._manual.error)
            self.touch(("manual_edit", 1))
            self.app.edit_value = "5"
            self.tick("right")
            self.assertEqual(self.app.screen, "manual")
            self.assertTrue(self.app._manual.outputs_enabled)
            self.assertEqual(self.app._manual.state, "monitoring")
            self.tick()
            self.assertIsNone(self.app._manual.error)

    def test_touch_keypad_ok_applies_and_pwm_labels_use_actual_applied_codes(self):
        self.open()
        self.touch(("manual_edit", 1))
        self.app.edit_value = "5"
        self.touch("right")
        self.assertTrue(self.app._manual.outputs_enabled)
        # Hardware PWM may quantize the requested u16 code; display readback.
        self.app._manual.applied_duty_u16[:] = [1234, 9876]
        self.app.dirty = True
        self.tick()
        for code in (1234, 9876):
            self.assertIn("PWM %.2f%% %d/65535" % (code * 100 / 65535, code), self.display.labels)
        self.assertFalse(any("Next PWM" in text for text in self.display.labels))
        self.assertTrue(any(text == "ON" and y == 12 for text, x, y, color in self.display.draws))

    def test_keypad_ok_apply_failure_stops_before_error_screen(self):
        self.open()
        self.touch(("manual_edit", 1))
        self.app.edit_value = "5"
        original = self.instrument.set_duty
        def failed_gate(role, value):
            if role == "gate" and value > 0:
                raise OSError("PWM apply failed")
            original(role, value)
        self.instrument.set_duty = failed_gate
        self.tick("right")
        self.assertEqual(self.app.screen, "manual_error")
        self.assertTrue(self.app._outputs_safe)
        self.assertFalse(self.app._manual.outputs_enabled)
        self.assertTrue(all(v == 0 for v in self.instrument.duties.values()))
        self.assertIn("Outputs OFF. Return to Advanced.", self.display.labels)

    def test_saturation_and_sensor_failure_stop_before_error_screen(self):
        for error in (OverrangeError("gate", "shunt_saturation", 0x7ffff, 1, 0.1638396875, "V"),
                      OSError("Sensor missing")):
            if self.app.screen == "home":
                self.open()
            else:
                self.touch("manual")
            self.tick("right")
            self.instrument.error = error
            for _ in range(14):
                self.tick()
                if self.app.screen == "manual_error":
                    break
            self.assertEqual(self.app.screen, "manual_error")
            self.assertTrue(self.app._outputs_safe)
            self.assertTrue(all(value == 0 for value in self.instrument.duties.values()))
            self.assertIn("Outputs OFF. Return to Advanced.", self.display.labels)
            self.instrument.error = None
            self.tick("right")
            self.assertEqual(self.app.screen, "advanced")

    def test_notice_and_arbitrary_navigation_stop_outputs(self):
        self.open()
        self.tick("right")
        self.app.message("Test notice", "advanced")
        self.assertFalse(self.app._manual.outputs_enabled)
        self.assertTrue(all(v == 0 for v in self.instrument.duties.values()))
        self.tick("right")
        self.touch("manual")
        self.tick("right")
        self.app.goto("settings")
        self.assertFalse(self.app._manual.outputs_enabled)
        self.assertTrue(all(v == 0 for v in self.instrument.duties.values()))

    def test_restart_stops_outputs_and_failed_shutdown_never_claims_off(self):
        self.open()
        self.tick("right")
        self.instrument.zero_error = True
        self.tick("left")
        self.assertEqual(self.restarts, [True])
        self.assertFalse(self.app._outputs_safe)
        self.assertNotEqual(self.app.screen, "manual_error")
        self.assertNotEqual(self.app.screen, "advanced")

    def test_restart_from_live_page_stops_and_display_failure_also_stops(self):
        self.open()
        self.tick("right")
        self.app._request_restart()
        self.assertTrue(all(v == 0 for v in self.instrument.duties.values()))
        self.assertEqual(self.app.screen, "restarting")

    def test_display_failure_stops_before_propagating(self):
        self.open()
        self.tick("right")
        self.display.error = MemoryError("draw")
        self.app.dirty = True
        with self.assertRaises(MemoryError):
            self.tick()
        self.assertTrue(all(v == 0 for v in self.instrument.duties.values()))

    def test_invalid_manual_target_returns_to_keypad_without_partial_edit_or_drive(self):
        self.open()
        self.touch(("manual_edit", 1))
        self.app.edit_value = "100"
        self.tick("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.app._manual.targets_uv, [0, 0])
        self.assertFalse(self.app._manual.outputs_enabled)
        self.tick("right")
        self.assertEqual(self.app.screen, "edit")
        self.tick("left")
        self.assertEqual(self.app.screen, "manual")
        self.assertEqual(self.store.writes, 0)


if __name__ == "__main__":
    unittest.main()
