"""Triggered acquisition timing and fault handling, without hardware access."""
import unittest
from pathlib import Path
import runpy

from firmware import model
from firmware.engine import Engine, OverrangeError
from test_model_engine import FakeInstrument, calibration, run_engine, settings


class TriggeredEngineTests(unittest.TestCase):
    def test_retired_profiler_fails_before_hardware_import_or_output_changes(self):
        path = Path(__file__).resolve().parents[1] / "prototype" / "firmware_sample_profile.py"
        with self.assertRaisesRegex(RuntimeError, "Retired sampling profiler"):
            runpy.run_path(str(path))

    def scanner(self, hardware, count=4):
        scanner = Engine(hardware)
        saved = calibration()
        saved["metadata"]["ina_average_count"] = count
        scanner.start_scan(settings(ina_average_count=count), saved)
        return scanner

    def tick(self, scanner, hardware, now):
        hardware.now = now
        return scanner.tick(now)

    def test_late_ready_poll_accepts_completed_registers_before_timeout(self):
        hardware = FakeInstrument()
        scanner = self.scanner(hardware, count=1)
        self.tick(scanner, hardware, 0)
        self.tick(scanner, hardware, 200)
        self.assertTrue(scanner._measurement_waiting)
        self.assertEqual(hardware.reads, 0)
        self.tick(scanner, hardware, 5000)  # Display stall, well past deadline.
        self.assertEqual(scanner.state, "scanning")
        self.assertEqual(scanner._phase, "settle_gate")
        self.assertEqual(hardware.reads, 1)

    def test_missing_completion_has_bounded_timeout_and_no_stale_read(self):
        class NeverReady(FakeInstrument):
            def poll_measurement(self):
                return False
        hardware = NeverReady()
        scanner = self.scanner(hardware, count=1024)
        events = run_engine(scanner, hardware, maximum_ms=4000)
        self.assertEqual(scanner.state, "error")
        self.assertIn("conversion timed out", scanner.error)
        self.assertEqual(hardware.reads, 0)
        self.assertTrue(scanner.shutdown_succeeded)
        self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_cancel_interrupts_long_average_without_waiting_or_reading(self):
        hardware = FakeInstrument()
        scanner = self.scanner(hardware, count=1024)
        self.tick(scanner, hardware, 0)
        self.tick(scanner, hardware, 200)
        self.assertTrue(scanner._measurement_waiting)
        scanner.cancel()
        self.assertEqual(scanner.state, "cancelled")
        self.assertTrue(scanner.shutdown_succeeded)
        self.assertEqual(hardware.reads, 0)
        self.assertEqual(hardware.duties, {"drain": 0.0, "gate": 0.0})
        self.tick(scanner, hardware, 10000)
        self.assertEqual(len(hardware.triggers), 1)
        self.assertEqual(hardware.reads, 0)

    def test_positive_alert_during_settle_or_average_is_partial_without_peak(self):
        for during_average in (False, True):
            class AlertHardware(FakeInstrument):
                def poll_alerts(self):
                    if not during_average and self.now >= 25:
                        raise OverrangeError("drain", "current_limit_alert", None, 1,
                                             model.DRAIN_CURRENT_LIMIT_A, "A")
                def poll_measurement(self):
                    raise OverrangeError("gate", "current_limit_alert", None, 1,
                                         model.GATE_CURRENT_LIMIT_A, "A")
            with self.subTest(during_average=during_average):
                hardware = AlertHardware()
                scanner = self.scanner(hardware, count=1024)
                events = run_engine(scanner, hardware)
                self.assertEqual(scanner.state, "partial")
                self.assertEqual(scanner.fault["kind"], "current_limit_alert")
                self.assertIsNone(scanner.fault["raw_code"])
                self.assertIsNone(scanner.fault["measured_value"])
                self.assertEqual(hardware.reads, 0)
                self.assertFalse(any(kind == "point" for kind, _ in events))
                self.assertTrue(scanner.shutdown_succeeded)

    def test_negative_completed_average_still_checks_absolute_current_limit(self):
        class NegativeCurrent(FakeInstrument):
            def read_pair(self):
                if len(self.triggers) == 3:
                    self.current["drain"] = -0.006
                return super().read_pair()
        hardware = NegativeCurrent()
        scanner = self.scanner(hardware)
        events = run_engine(scanner, hardware)
        self.assertEqual(scanner.state, "partial")
        self.assertEqual(scanner.fault["kind"], "current_limit")
        self.assertEqual(scanner.fault["sign"], -1)
        self.assertLess(scanner.fault["measured_value"], -0.005)
        self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_calibration_retains_three_real_blocks_and_between_block_scatter(self):
        class BlockCurrent(FakeInstrument):
            def read_pair(self):
                self.current["gate"] = (len(self.triggers) - 1) * 2e-6 if self.triggers else 0
                return super().read_pair()
        hardware = BlockCurrent()
        scanner = Engine(hardware)
        scanner.start_calibration(settings())
        for now in range(1000):
            self.tick(scanner, hardware, now)
            if scanner._index:
                break
        self.assertEqual(scanner._index, 1)
        self.assertEqual(len(hardware.triggers), 3)
        self.assertEqual(scanner.latest["acquisition_blocks_completed"], 3)
        self.assertAlmostEqual(scanner.latest["ig_a"], 2e-6, delta=1e-12)
        self.assertAlmostEqual(scanner.latest["ig_std_a"], 2e-6, delta=1e-12)
        point = scanner.calibration["channels"]["gate"]["points"][0]
        self.assertAlmostEqual(point["shunt_std_v"], 2e-6 * model.GATE_SHUNT_OHM, delta=1e-9)
        scanner.cancel()


if __name__ == "__main__":
    unittest.main()
