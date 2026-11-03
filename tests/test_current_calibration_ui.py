"""Calibration UI lifecycle: no drive before confirmation, atomic-save handoff."""

import unittest

from firmware import model
from firmware.engine import Engine
from firmware.current_baseline import Baseline
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_ram_ui import RamCloud, RamWifi
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore


class Result:
    def __init__(self, events):
        self.events = events
        self.error = None
        self.summary = {
            "drain": {"zero_valid": False, "zero_residual_uv": 114000,
                      "zero_duty_u16": 0},
            "gate": {"zero_valid": True, "zero_residual_uv": -1250,
                     "zero_duty_u16": 8411},
        }

    def metadata(self, settings):
        self.events.append("summary")
        return self.summary

    def save(self, path):
        self.events.append(("save", path))
        if self.error:
            raise self.error


class Controller:
    def __init__(self, instrument, settings, events):
        self.instrument, self.settings, self.events = instrument, settings, events
        self.state, self.progress = "idle", 0.0
        self.channel, self.phase = "drain", "drive"
        self.error, self.outcome = None, None
        self.shutdown_succeeded = False
        self.result = Result(events)
        self.ticks = 0

    def start(self):
        self.events.append("start")
        self.state = "running"

    def tick(self, now_ms):
        self.ticks += 1
        self.events.append("tick")
        if self.outcome in ("complete", "error"):
            self.instrument.zero()
            self.shutdown_succeeded = True
            self.state = self.outcome
            self.progress = 1.0
            return
        self.instrument.set_duty("gate", 0.1)
        self.progress = 0.25
        self.phase = "convert"

    def cancel(self):
        self.events.append("cancel")
        self.instrument.zero()
        self.shutdown_succeeded = True
        self.state = "cancelled"


class CurrentCalibrationUITests(unittest.TestCase):
    def setUp(self):
        self.display, self.inputs = FakeDisplay(), FakeInputs()
        self.instrument, self.store = HostInstrument(), MemoryStore()
        self.engine = Engine(self.instrument)
        self.buffer = PackedScanBuffer(capacity=32)
        self.cloud, self.wifi = RamCloud(self.buffer), RamWifi()
        self.events, self.restarts = [], []
        self.factory_error = None
        self.controller = None
        self.app = App(self.display, self.inputs, self.engine, model.DEFAULT_SETTINGS,
                       store=self.store, cloud=self.cloud, wifi=self.wifi,
                       scan_buffer=self.buffer,
                       restart=lambda: self.restarts.append(True),
                       calibration_factory=self.factory,
                       calibration_path="test-current-baseline.bin")
        self.now = 0
        self.tick()

    def factory(self, instrument, settings):
        self.events.append("factory")
        if self.factory_error:
            raise self.factory_error
        self.controller = Controller(instrument, settings, self.events)
        return self.controller

    def tick(self, action=None):
        self.now += 20
        self.inputs.events = [(action,)] if action else []
        self.app.tick(self.now)

    def action(self, action):
        self.app.action(action)
        self.tick()

    def confirm_screen(self):
        self.tick("left")
        self.action("advanced")
        self.action("calibrate")
        self.assertEqual(self.app.screen, "calibration_confirm")

    def start(self):
        self.confirm_screen()
        self.tick("right")
        self.assertEqual(self.app.screen, "calibration")
        self.tick()  # Factory/start; deliberately no first measurement yet.
        self.assertIsNotNone(self.controller)

    def test_confirmation_is_read_only_and_left_returns_to_advanced(self):
        self.confirm_screen()
        self.assertIn("Both outputs must be open.", self.display.labels)
        self.assertIn("Leave BOTH shunts unbridged.", self.display.labels)
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.store.writes, 0)
        self.tick("left")
        self.assertEqual(self.app.screen, "advanced")
        self.assertEqual(self.restarts, [])

    def test_start_saves_draft_and_prepares_before_any_output(self):
        self.confirm_screen()
        self.app.draft["source_voltage_uv"] = 3200000
        self.tick("right")
        self.assertEqual(self.events, [])
        self.assertEqual(self.store.writes, 0)
        self.assertIn("CALIBRATING", self.display.labels)
        self.tick()
        self.assertEqual(self.events, ["factory", "start"])
        self.assertEqual(self.store.settings["source_voltage_uv"], 3200000)
        self.assertEqual(self.controller.settings, self.store.settings)
        self.assertEqual(self.instrument.commands, [])
        self.assertTrue(self.app._wifi_paused)
        self.tick()
        self.assertEqual(self.controller.ticks, 1)
        self.assertTrue(self.instrument.commands)

    def test_cancel_preparing_does_not_save_or_construct(self):
        self.confirm_screen()
        self.tick("right")
        self.tick("left")
        self.assertEqual(self.events, [])
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.restarts, [True])
        self.assertIn("RESTARTING", self.display.labels)

    def test_settings_save_failure_prevents_controller_and_drive(self):
        self.confirm_screen()
        self.store.settings_error = OSError("settings write failed")
        self.tick("right")
        self.tick()
        self.assertEqual(self.app.screen, "calibration_error")
        self.assertEqual(self.events, [])
        self.assertEqual(self.instrument.commands, [])
        self.assertIn("settings write failed", self.display.labels)
        self.tick("right")
        self.assertEqual(self.restarts, [True])

    def test_workspace_allocation_failure_is_stopped_and_recoverable(self):
        self.factory_error = MemoryError("workspace")
        self.confirm_screen()
        self.tick("right")
        self.tick()
        self.assertEqual(self.app.screen, "calibration_error")
        self.assertEqual(self.instrument.commands, [])
        self.assertTrue(self.app._outputs_safe)
        self.assertEqual(self.events, ["factory"])
        self.tick("left")
        self.assertEqual(self.restarts, [True])

    def test_cancel_running_stops_and_restarts_without_saving_baseline(self):
        self.start()
        self.tick()
        self.tick("left")
        self.assertEqual(self.controller.state, "cancelled")
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
        self.assertFalse(any(isinstance(e, tuple) and e[0] == "save" for e in self.events))
        self.assertEqual(self.restarts, [True])
        self.assertIn("RESTARTING", self.display.labels)

    def test_right_during_sweep_does_not_skip_or_commit(self):
        self.start()
        self.tick("right")
        self.assertEqual(self.controller.state, "running")
        self.assertEqual(self.app.screen, "calibration")
        self.assertFalse(any(isinstance(e, tuple) for e in self.events))
        self.assertEqual(self.restarts, [])

    def test_success_prepares_summary_before_save_and_restarts_on_ok(self):
        self.start()
        self.controller.outcome = "complete"
        self.tick()
        self.assertEqual(self.events[-2:], ["summary", ("save", "test-current-baseline.bin")])
        self.assertEqual(self.app.screen, "calibration_done")
        self.assertIn("Closest only; zero trim not used", self.display.labels)
        self.assertIn("Gate zero residual: -0.00125 V", self.display.labels)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
        self.assertEqual(self.restarts, [])
        self.tick()
        self.assertEqual(self.events.count(("save", "test-current-baseline.bin")), 1)
        self.tick("right")
        self.assertEqual(self.restarts, [True])

    def test_sweep_error_never_calls_binary_save(self):
        self.start()
        self.controller.outcome, self.controller.error = "error", "drain shunt ADC saturated"
        self.tick()
        self.assertEqual(self.app.screen, "calibration_error")
        self.assertIn("drain shunt ADC saturated", self.display.labels)
        self.assertFalse(any(isinstance(e, tuple) for e in self.events))
        self.assertTrue(self.app._outputs_safe)

    def test_binary_save_failure_does_not_activate_candidate(self):
        old_baseline = self.engine.baseline
        self.start()
        self.controller.outcome = "complete"
        self.controller.result.error = OSError("baseline storage failed")
        self.tick()
        self.assertEqual(self.app.screen, "calibration_error")
        self.assertIs(self.engine.baseline, old_baseline)
        self.assertTrue(self.app._outputs_safe)

    def test_progress_paints_bounded_fields_without_full_screen_redraw(self):
        self.start()
        self.tick()
        before_hits = self.app.hits
        for _ in range(20):
            self.tick()
        self.assertIs(self.app.hits, before_hits)
        self.assertEqual(len(self.app.hits), 1)  # CANCEL only while busy.
        self.assertIs(self.app._calibration, self.controller)
        self.assertEqual(self.app._calibration_percent, 25)
        self.assertFalse(hasattr(self.app, "calibration_samples"))

    def make_baseline(self):
        self.instrument.identity = {
            "drain": {"address": 0x44, "die": 0x2260},
            "gate": {"address": 0x41, "die": 0x2260},
        }
        baseline = Baseline()
        baseline.begin(self.app.settings, self.instrument.identity)
        for role in range(2):
            baseline.add_knot(role, 0, 1000, 0)
            baseline.add_knot(role, 20000000, 2000, 65535)
            baseline.set_zero(role, 10000, 1000, True)
        baseline.finish()
        self.engine.baseline = baseline
        return baseline

    def test_home_status_and_upload_metadata_share_baseline_eligibility(self):
        baseline = self.make_baseline()

        class CorrectionCloud(RamCloud):
            def begin_scan(self, settings, now_ms, current_correction=None):
                self.correction = current_correction
                return super().begin_scan(settings, now_ms)

        self.cloud = self.app.cloud = CorrectionCloud(self.buffer)
        self.app.dirty = True
        self.tick()
        self.assertIn("Currents: baseline corrected", self.display.labels)
        self.tick("right")
        self.tick()
        self.assertEqual(self.cloud.correction["sha256"], baseline.sha256)
        self.assertEqual(self.cloud.correction, self.engine.current_correction(self.app.settings))

    def test_settings_or_sensor_identity_change_clearly_marks_raw(self):
        self.make_baseline()
        self.tick("left")
        self.action("advanced")
        self.assertIn("Currents: baseline corrected", self.display.labels)
        self.app.draft["ina_average_count"] = 256
        self.app.dirty = True
        self.tick()
        self.assertIn("RAW currents | Recalibration needed", self.display.labels)
        self.app.draft["ina_average_count"] = self.app.settings["ina_average_count"]
        self.instrument.identity["gate"]["address"] = 0x40
        self.app.dirty = True
        self.tick()
        self.assertIn("RAW currents | Recalibration needed", self.display.labels)
        self.assertIsNone(self.engine.current_correction(self.app.settings))


if __name__ == "__main__":
    unittest.main()
