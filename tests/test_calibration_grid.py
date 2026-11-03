"""Exact, allocation-bounded calibration targets for the supported rail range."""

import unittest
from unittest.mock import patch

from firmware import engine, model
from test_model_engine import FakeInstrument, run_engine, settings

POINT_SLOT_FACTORY = engine._calibration_slots


def reference_targets(limit):
    # The original selection rule, retained only as an independent test oracle.
    return ([0] + [value for value in range(50000, limit, 50000)
                   if value % 100000 == 0 or 800000 <= value <= 1800000] + [limit])


class CalibrationGridTests(unittest.TestCase):
    def test_full_grid_matches_original_for_standard_and_fractional_rails(self):
        for limit in (2100000, 5000000, 18800000, 30000000,
                      2100001, 4999999, 5050000, 18800001, 29999999):
            with self.subTest(limit=limit):
                actual = model.calibration_targets_uv({"driver_supply_uv": limit})
                expected = reference_targets(limit)
                self.assertNotIsInstance(actual, (list, tuple))
                self.assertEqual(len(expected), len(actual))
                self.assertEqual(expected, list(actual))
                self.assertEqual(limit, actual[-1])
                self.assertEqual(1, sum(value == limit for value in actual))
                self.assertTrue(all(type(value) is int for value in actual))
                self.assertEqual([actual[index] for index in range(len(actual))], expected)

    def test_index_boundaries_and_negative_indices(self):
        values = model.calibration_targets_uv({"driver_supply_uv": 18800000})
        self.assertEqual(0, values[-len(values)])
        self.assertEqual(700000, values[7])
        self.assertEqual(800000, values[8])
        self.assertEqual(850000, values[9])
        self.assertEqual(1800000, values[28])
        self.assertEqual(1900000, values[29])
        self.assertEqual(18700000, values[-2])
        for index in (-len(values) - 1, len(values)):
            with self.assertRaises(IndexError):
                values[index]
        for index in (1.0, "1", slice(0, 1)):
            with self.assertRaises(TypeError):
                values[index]

    def test_engine_retains_lazy_sequence_without_volts_list_wrapper(self):
        value = dict(model.DEFAULT_SETTINGS)
        value["driver_supply_uv"] = 18800000
        scanner = engine.Engine(object())
        with patch.object(model, "calibration_targets", side_effect=AssertionError("volts list")):
            scanner.start_calibration(value)
        self.assertIsInstance(scanner._targets, model.CalibrationTargets)
        self.assertEqual(199, scanner._total)
        self.assertEqual(18800000, scanner._targets[-1])
        self.assertEqual("calibrating", scanner.state)




class CalibrationPointStorageTests(unittest.TestCase):
    def test_full_sweeps_fill_reserved_slots_without_growing_lists(self):
        for rail, expected_count in ((5000000, 61), (18800000, 199), (30000000, 311)):
            with self.subTest(rail=rail):
                hardware = FakeInstrument()
                scanner = engine.Engine(hardware)
                value = settings(driver_supply_uv=rail)
                allocated = []

                def allocate(count):
                    points = POINT_SLOT_FACTORY(count)
                    allocated.append(points)
                    return points

                # An ideal 40 V transfer range lets all three configured rails
                # finish at their final target, including the 30 V test.
                with patch.object(model, "PWM_FULL_SCALE_V", 40.0), \
                        patch.object(engine, "_calibration_slots", allocate):
                    scanner.start_calibration(value)
                    self.assertEqual([expected_count, expected_count], [points.capacity for points in allocated])
                    self.assertEqual({"drain": 0, "gate": 0}, scanner._cal_counts)
                    self.assertEqual([], hardware.commands)
                    events = run_engine(scanner, hardware, maximum_ms=300000)
                    self.assertEqual("complete", scanner.state, scanner.error)
                    model.validate_calibration(scanner.calibration, value)
                self.assertEqual(1, sum(kind == "calibrated" for kind, _ in events))
                for offset, role in enumerate(("drain", "gate")):
                    points = scanner.calibration["channels"][role]["points"]
                    self.assertIs(allocated[offset], points)
                    self.assertEqual(expected_count, len(points))
                    self.assertEqual(expected_count, scanner._cal_counts[role])
                    self.assertNotIn(None, points)
                    self.assertAlmostEqual(rail / 1000000, points[-1]["terminal_v"], delta=0.000002)

    def test_independent_early_plateaus_limit_count_and_preserve_inverse_map(self):
        hardware = FakeInstrument()
        hardware.voltage_ceiling = {"drain": 3.5, "gate": 3.7}
        scanner = engine.Engine(hardware)
        allocated = []

        def allocate(count):
            points = POINT_SLOT_FACTORY(count)
            allocated.append(points)
            return points

        with patch.object(engine, "_calibration_slots", allocate):
            scanner.start_calibration(settings(driver_supply_uv=18800000))
            reserved_count = scanner._total
            events = run_engine(scanner, hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        self.assertEqual(1, sum(kind == "calibrated" for kind, _ in events))
        model.validate_calibration(scanner.calibration, scanner.settings)
        for offset, role in enumerate(("drain", "gate")):
            channel = scanner.calibration["channels"][role]
            points = channel["points"]
            self.assertIs(allocated[offset], points)
            self.assertEqual(scanner._cal_counts[role], len(points))
            self.assertLess(len(points), reserved_count)
            self.assertNotIn(None, points)
            self.assertEqual("plateau", channel["capability"]["limit_reason"])
            self.assertLess(points[-1]["drive_duty"], points[-1]["duty"])
            self.assertAlmostEqual(hardware.voltage_ceiling[role], points[-1]["terminal_v"])
            self.assertTrue(all(after["terminal_v"] > before["terminal_v"] and
                                after["duty"] > before["duty"]
                                for before, after in zip(points, list(points)[1:])))
        self.assertLess(scanner._cal_counts["drain"], scanner._cal_counts["gate"])

    def test_slot_allocation_failure_keeps_first_start_and_saved_calibration_untouched(self):
        for existing in (False, True):
            for fail_on in (1, 2):
                with self.subTest(existing=existing, fail_on=fail_on):
                    hardware = FakeInstrument()
                    scanner = engine.Engine(hardware)
                    saved = {"saved": True}
                    if existing:
                        scanner.calibration = saved
                    calls = []

                    def allocate(count):
                        calls.append(count)
                        if len(calls) == fail_on:
                            raise MemoryError("calibration slot allocation")
                        return POINT_SLOT_FACTORY(count)

                    with patch.object(engine, "_calibration_slots", allocate):
                        with self.assertRaises(MemoryError):
                            scanner.start_calibration(settings(driver_supply_uv=18800000))
                    self.assertEqual("idle", scanner.state)
                    self.assertEqual([], hardware.commands)
                    self.assertEqual(0, hardware.reads)
                    self.assertEqual(0, hardware.zeroes)
                    if existing:
                        self.assertIs(saved, scanner.calibration)
                    else:
                        self.assertFalse(hasattr(scanner, "calibration"))
                    scanner.start_calibration(settings())
                    self.assertEqual("calibrating", scanner.state)
                    self.assertEqual([], hardware.commands)

    def test_drive_ceiling_limits_populated_count_without_resizing_buffer(self):
        hardware = FakeInstrument()
        hardware.voltage_gain = {"drain": 0.7, "gate": 0.9}
        scanner = engine.Engine(hardware)
        with patch.object(engine, "_calibration_slots", lambda count: POINT_SLOT_FACTORY(count)):
            scanner.start_calibration(settings())
            reserved_count = scanner._total
            run_engine(scanner, hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        model.validate_calibration(scanner.calibration, scanner.settings)
        for role in ("drain", "gate"):
            channel = scanner.calibration["channels"][role]
            self.assertEqual("drive_ceiling", channel["capability"]["limit_reason"])
            self.assertLess(len(channel["points"]), reserved_count)
            self.assertEqual(scanner._cal_counts[role], len(channel["points"]))
            self.assertNotIn(None, channel["points"])
            self.assertAlmostEqual(scanner._cal_duty_max, channel["points"][-1]["duty"], delta=1.2e-7)


if __name__ == "__main__":
    unittest.main()
