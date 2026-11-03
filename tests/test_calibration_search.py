"""Binary interpolation matches the original linear arithmetic and bounds."""
import copy
import math
import random
import struct
import unittest
from unittest.mock import patch

from firmware import model
from firmware.calibration_data import PackedPoints, point_value
from test_model_engine import calibration


def linear_reference(points, voltage, field, edge_tolerance_v=0.0):
    """Frozen pre-search implementation, including endpoint/rounding order."""
    voltage = model._number(voltage, "Calibration voltage")
    drive_map = field == "drive_duty"
    coordinate = "drive_v" if drive_map else "terminal_v"
    value_field = "duty" if drive_map else field
    first_v = point_value(points, 0, coordinate, point_value(points, 0, "terminal_v"))
    last_v = point_value(points, -1, coordinate, point_value(points, -1, "terminal_v"))
    first_value = point_value(points, 0, field, point_value(points, 0, value_field))
    if voltage < first_v:
        if first_v - voltage <= edge_tolerance_v + 1e-9:
            return first_value
        raise ValueError("Voltage %.4f V is below calibration coverage" % voltage)
    if voltage > last_v:
        if voltage - last_v <= edge_tolerance_v + 1e-9:
            return point_value(points, -1, field, point_value(points, -1, value_field))
        raise ValueError("Voltage %.4f V is above calibration coverage" % voltage)
    low_value, lower_v = first_value, first_v
    for index in range(1, len(points)):
        upper_v = point_value(points, index, coordinate, point_value(points, index, "terminal_v"))
        high_value = point_value(points, index, field, point_value(points, index, value_field))
        if voltage <= upper_v:
            fraction = (voltage - lower_v) / (upper_v - lower_v)
            return low_value + fraction * (high_value - low_value)
        low_value, lower_v = high_value, upper_v
    return point_value(points, -1, field, point_value(points, -1, value_field))


def random_table(count, seed):
    rng = random.Random(seed)
    voltage, values = 0.003, []
    for index in range(count):
        if index:
            voltage += rng.uniform(0.025, 0.075)
        values.append({"terminal_v": voltage, "shunt_v": rng.uniform(-0.0001, 0.0001),
                       "shunt_std_v": 1e-8, "duty": index / 1000})
    return values


def packed_table(values, endpoint=False):
    packed = PackedPoints(len(values))
    for point in values:
        packed.append(point["terminal_v"], point["shunt_v"], point["shunt_std_v"], point["duty"])
    if endpoint:
        packed.set_drive_endpoint(packed.value(-1, "terminal_v") - 0.002,
                                  packed.value(-1, "duty") - 0.0001)
    return packed


class CalibrationSearchTests(unittest.TestCase):
    def assert_matches_reference(self, points, voltage, field, tolerance):
        try:
            expected = linear_reference(points, voltage, field, tolerance)
        except ValueError as error:
            with self.assertRaises(ValueError) as caught:
                model.interpolate(points, voltage, field, tolerance)
            self.assertEqual(str(caught.exception), str(error))
        else:
            actual = model.interpolate(points, voltage, field, tolerance)
            # Compare bits as well as numeric equality, including signed zero.
            self.assertEqual(struct.pack("<d", actual), struct.pack("<d", expected),
                             (len(points), voltage, field, tolerance))

    def test_random_list_and_packed_tables_match_at_all_knots_and_random_intervals(self):
        rng = random.Random(55219)
        for count in (2, 3, 7, 31, 160, 184, 311):
            for endpoint in (False, True):
                source = random_table(count, count + int(endpoint))
                if endpoint:
                    source[-1].update(drive_v=source[-1]["terminal_v"] - 0.002,
                                      drive_duty=source[-1]["duty"] - 0.0001)
                for points in (source, packed_table(source, endpoint)):
                    for field, tolerance in (("shunt_v", 0.005), ("drive_duty", 0.020)):
                        coordinate = "drive_v" if field == "drive_duty" else "terminal_v"
                        knots = [point_value(points, i, coordinate, point_value(points, i, "terminal_v"))
                                 for i in range(count)]
                        queries = list(knots)
                        for knot in knots:
                            queries.extend((math.nextafter(knot, -math.inf),
                                            math.nextafter(knot, math.inf)))
                        queries.extend(rng.uniform(knots[0], knots[-1]) for _ in range(40))
                        queries.extend((knots[0] - tolerance, knots[-1] + tolerance,
                                        knots[0] - tolerance - 1e-6,
                                        knots[-1] + tolerance + 1e-6))
                        for query in queries:
                            self.assert_matches_reference(points, query, field, tolerance)

    def test_knots_preserve_linear_arithmetic_instead_of_returning_stored_value(self):
        # Cancellation makes the computed upper-knot result differ from the
        # stored value. This small example protects the original arithmetic.
        points = [{"terminal_v": 0.0, "shunt_v": 1.0},
                  {"terminal_v": 1.0, "shunt_v": 1e-20},
                  {"terminal_v": 2.0, "shunt_v": -1.0}]
        self.assertEqual(linear_reference(points, 1.0, "shunt_v"), 0.0)
        self.assertNotEqual(linear_reference(points, 1.0, "shunt_v"), points[1]["shunt_v"])
        self.assert_matches_reference(points, 1.0, "shunt_v", 0.0)

    def test_coordinate_probes_are_logarithmic_without_snapshots_or_iteration(self):
        points = packed_table(random_table(311, 991), endpoint=True)
        backing = points._values
        maximum = math.ceil(math.log2(len(points) - 1)) + 4
        original = model.point_value
        for field in ("shunt_v", "drive_duty"):
            calls = []

            def read(points, index, name, default=None):
                calls.append((index, name))
                return original(points, index, name, default)

            query = points.value(-1, "terminal_v") - 0.010
            with patch.object(PackedPoints, "__getitem__", side_effect=AssertionError("point snapshot")), \
                    patch.object(PackedPoints, "__iter__", side_effect=AssertionError("table iteration")), \
                    patch.object(model, "point_value", side_effect=read):
                result = model.interpolate(points, query, field)
            self.assertTrue(math.isfinite(result))
            if field == "shunt_v":
                coordinates = sum(name == "terminal_v" for _, name in calls)
                self.assertLessEqual(coordinates, maximum)
                self.assertEqual(sum(name == "shunt_v" for _, name in calls), 2)
            else:
                self.assertLessEqual(sum(name == "drive_v" for _, name in calls), maximum)
                self.assertLessEqual(len(calls), maximum * 2 + 4)
            self.assertIs(points._values, backing)
            self.assertEqual(len(points), 311)

    def test_present_drive_overrides_do_not_read_unused_fallback_fields(self):
        points = random_table(17, 57)
        for point in points:
            point["drive_v"] = point["terminal_v"]
            point["drive_duty"] = point["duty"]
        original, fields = model.point_value, []

        def read(points, index, name, default=None):
            fields.append(name)
            return original(points, index, name, default)

        with patch.object(model, "point_value", side_effect=read):
            actual = model.interpolate(points, points[10]["drive_v"], "drive_duty")
        self.assertEqual(actual, linear_reference(points, points[10]["drive_v"], "drive_duty"))
        self.assertNotIn("terminal_v", fields)
        self.assertNotIn("duty", fields)

    def test_baseline_and_drive_edges_keep_distinct_tolerances(self):
        for points in (random_table(8, 17), packed_table(random_table(8, 17), endpoint=True)):
            channel = {"shunt_ohm": model.GATE_SHUNT_OHM, "points": points}
            first = point_value(points, 0, "terminal_v")
            last = point_value(points, -1, "terminal_v")
            drive_last = point_value(points, -1, "drive_v", last)
            duty = point_value(points, -1, "drive_duty", point_value(points, -1, "duty"))
            self.assertEqual(model.duty_for_voltage(channel, drive_last + 0.020), duty)
            with self.assertRaisesRegex(ValueError, "above calibration coverage"):
                model.duty_for_voltage(channel, drive_last + 0.020001)
            raw = point_value(points, -1, "shunt_v")
            self.assertEqual(model.corrected_current(channel, last + 0.005, raw), 0.0)
            with self.assertRaisesRegex(ValueError, "above calibration coverage"):
                model.corrected_current(channel, last + 0.005001, raw)
            for tolerance in (0, 0.005, 0.020):
                for query in (first - tolerance - 1e-6, first - tolerance,
                              last + tolerance, last + tolerance + 1e-6):
                    self.assert_matches_reference(points, query, "shunt_v", tolerance)

    def test_monotonic_validation_and_saved_table_are_unchanged(self):
        original = calibration()
        before = copy.deepcopy(original)
        self.assertIs(model.validate_calibration(original), original)
        for voltage in (0, 0.5, 1.5, 2.5, 3):
            model.interpolate(original["channels"]["gate"]["points"], voltage, "shunt_v")
        self.assertEqual(original, before)
        for field in ("terminal_v", "duty", "drive_v", "drive_duty"):
            invalid = copy.deepcopy(original)
            points = invalid["channels"]["gate"]["points"]
            fallback = "terminal_v" if field == "drive_v" else "duty" if field == "drive_duty" else field
            points[1][field] = points[0][fallback]
            with self.subTest(field=field), self.assertRaises(ValueError):
                model.validate_calibration(invalid)

    def test_single_point_and_invalid_voltage_behavior_matches_reference(self):
        points = [{"terminal_v": 1.0, "shunt_v": -0.0, "duty": 0.1}]
        for field in ("shunt_v", "drive_duty"):
            for voltage in (0.999, 1.0, 1.001, 1.1):
                self.assert_matches_reference(points, voltage, field, 0.005)
            for voltage in (True, None, "1", math.nan, math.inf):
                self.assert_matches_reference(points, voltage, field, 0.005)


if __name__ == "__main__":
    unittest.main()
