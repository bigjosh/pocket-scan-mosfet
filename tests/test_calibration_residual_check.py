"""Read-only residual diagnostic using production averaging with host hardware."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from test_offset_calibration import OffsetInstrument, calibrate, offset_settings


class ResidualCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.29
        scanner, _ = calibrate(hardware)
        assert scanner.state == "complete", scanner.error
        cls.calibration = scanner.calibration

    def setUp(self):
        self.settings = offset_settings()
        self.saved = copy.deepcopy(self.settings)
        self.hardware = OffsetInstrument()
        self.hardware.floors["drain"] = 3.29
        self.now = 0
        self.order, self.pins = [], []
        def load_settings(**kwargs):
            self.assertEqual(kwargs, {"migrate": False})
            self.order.append("settings")
            return self.settings
        def load_calibration():
            self.order.append("calibration")
            return self.calibration
        def instrument():
            self.assertEqual(self.order, ["settings", "calibration"])
            self.order.append("hardware")
            return self.hardware
        def close():
            self.order.append("close")
            self.hardware.zero()
        self.hardware.close = close
        def sleep_ms(delay):
            self.now += delay
            self.hardware.now = self.now
        owner = self
        class Pin:
            OUT = 1
            def __init__(self, number, mode, value):
                owner.pins.append((number, value))
        self.main = types.SimpleNamespace(
            model=model, Engine=Engine, Instrument=instrument, Pin=Pin,
            gc=types.SimpleNamespace(collect=lambda: None),
            storage=types.SimpleNamespace(load_settings=load_settings, load_calibration=load_calibration, json=json),
            time=types.SimpleNamespace(ticks_ms=lambda: self.now,
                ticks_diff=lambda a, b: a-b, sleep_ms=sleep_ms))
        path = Path(__file__).resolve().parents[1] / "prototype" / "calibration_residual_check.py"
        spec = importlib.util.spec_from_file_location("calibration_residual_check_test", path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"main": self.main}):
            spec.loader.exec_module(self.helper)

    def execute(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.open", side_effect=AssertionError("unexpected file access")):
            self.helper.run()
        self.assertEqual(self.settings, self.saved)
        self.assertEqual(self.pins[-2:], [(21, 0), (20, 0)])
        self.assertEqual(self.hardware.duties, {"drain": 0.0, "gate": 0.0})
        return [(line.split(" ", 1)[0], json.loads(line.split(" ", 1)[1]))
                for line in output.getvalue().splitlines()]

    def test_full_grid_reports_single_hardware_block_residuals_and_closes_before_completion(self):
        self.hardware.current.update(drain=0.000003, gate=0.0000004)
        records = self.execute()
        points = [value for name, value in records if name == "CAL_RESIDUAL_POINT"]
        self.assertEqual(len(points), 16)
        self.assertTrue(all(point["ina_average_count"] == self.settings["ina_average_count"] for point in points))
        self.assertTrue(all(point["acquisition_blocks_completed"] == 1 for point in points))
        for point in points:
            self.assertAlmostEqual(point["id_residual_ua"], 3, delta=0.03)
            self.assertAlmostEqual(point["ig_residual_ua"], 0.4, delta=0.0001)
            self.assertIsNone(point["id_std_ua"])
            self.assertIsNone(point["ig_std_ua"])
            self.assertEqual(point["point_variance"], "unavailable")
        self.assertEqual(records[-1][0], "CAL_RESIDUAL_COMPLETE")
        self.assertEqual(records[-1][1]["points"], 16)
        self.assertEqual(self.order[-1], "close")

    def test_grid_stays_within_calibration_and_above_positive_drain_floor(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        scanner, _ = calibrate(hardware)
        values, floor = self.helper.grid(self.settings, scanner.calibration)
        self.assertGreater(values["drain"][0], 280000)
        self.assertAlmostEqual(floor, 3.58, places=5)
        for role in ("drain", "gate"):
            points = scanner.calibration["channels"][role]["points"]
            for target in values[role]:
                terminal = model.volts_from_uv(target + self.settings["source_voltage_uv"])
                self.assertGreaterEqual(terminal, points[0]["terminal_v"])
                self.assertLessEqual(terminal, points[-1]["terminal_v"])

    def test_ram_minimum_override_excludes_low_boundary_without_changing_saved_settings(self):
        self.helper.MIN_DRAIN_TARGET_UV = 100000
        records = self.execute()
        begin = records[0][1]
        self.assertEqual(begin["minimum_drain_target_uv"], 100000)
        self.assertTrue(begin["low_boundary_excluded"])
        points = [value for name, value in records if name == "CAL_RESIDUAL_POINT"]
        self.assertEqual(sorted({point["requested_vds_uv"] for point in points}),
                         [100000, 1000000, 5000000, 10000000])
        self.assertEqual(self.settings, self.saved)

    def test_invalid_minimum_override_is_rejected_before_hardware(self):
        for value in (-1, 0.1, True):
            self.helper.MIN_DRAIN_TARGET_UV = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.helper.grid(self.settings, self.calibration)
        self.assertEqual(self.hardware.commands, [])

    def test_current_trip_aborts_without_completion_and_still_forces_gate_first_low(self):
        self.hardware.current["drain"] = model.DRAIN_CURRENT_LIMIT_A * 2
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "current limit"):
            self.helper.run()
        self.assertIn("CAL_RESIDUAL_ABORT", output.getvalue())
        self.assertNotIn("CAL_RESIDUAL_COMPLETE", output.getvalue())
        self.assertEqual(self.pins[-2:], [(21, 0), (20, 0)])
        self.assertEqual(self.hardware.duties, {"drain": 0.0, "gate": 0.0})

    def test_invalid_calibration_fails_before_hardware_and_never_changes_settings(self):
        self.main.storage.load_calibration = lambda: None
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            self.helper.run()
        self.assertNotIn("hardware", self.order)
        self.assertEqual(self.hardware.commands, [])
        self.assertEqual(self.settings, self.saved)
        self.assertEqual(self.pins, [(21, 0), (20, 0)])

    def test_timeout_never_emits_complete(self):
        self.helper.TIMEOUT_MS = 1
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "timed out"):
            self.helper.run()
        self.assertNotIn("CAL_RESIDUAL_COMPLETE", output.getvalue())
        self.assertEqual(self.pins[-2:], [(21, 0), (20, 0)])

    def test_close_failure_still_forces_both_pins_low_without_success_marker(self):
        def failed_close():
            raise OSError("synthetic PWM cleanup failure")
        self.hardware.close = failed_close
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(OSError, "cleanup"):
            self.helper.run()
        self.assertNotIn("CAL_RESIDUAL_COMPLETE", output.getvalue())
        self.assertEqual(self.pins[-2:], [(21, 0), (20, 0)])


if __name__ == "__main__":
    unittest.main()
