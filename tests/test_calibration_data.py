"""Packed calibration capacity, independent snapshots and numerical fidelity."""
import unittest
from unittest.mock import patch

from firmware.calibration_data import PackedPoints, MAX_CALIBRATION_POINTS, float_buffer
from firmware import calibration_data
from firmware import model


class PackedPointTests(unittest.TestCase):
    def test_constructor_uses_known_length_without_full_sized_temporary(self):
        original = calibration_data.array
        initializers = []
        def allocate(typecode, initializer):
            self.assertIsInstance(initializer, range)
            self.assertEqual(311 * 4, len(initializer))
            initializers.append(initializer)
            return original(typecode, initializer)
        with patch.object(calibration_data, "array", allocate):
            values = float_buffer(311 * 4)
        self.assertEqual(1, len(initializers))
        self.assertEqual(311 * 4, len(values))
        self.assertTrue(all(value == 0.0 for value in values))
        values[0], values[-1] = 1.25, -2.5
        self.assertEqual((1.25, -2.5), (values[0], values[-1]))

    def test_fixed_numeric_buffer_count_sidecars_and_snapshot_ownership(self):
        points = PackedPoints(3)
        buffer = points._values
        self.assertEqual(48, len(buffer) * buffer.itemsize)
        self.assertEqual(0, len(points))
        with self.assertRaisesRegex(TypeError, "integer"):
            points[:]
        points.append(3.3, 0.000001, 0.00000001, 0)
        points.set_zero_bounds(3.29, 3.31)
        first = points[0]
        first["terminal_v"] = 50
        points.append(4.0, 0.000002, 0.00000002, 0.03)
        points.replace(-1, 4.001, 0.0000021, 0.00000003, 0.04)
        points.set_drive_endpoint(4.0, 0.03)
        self.assertIs(buffer, points._values)
        self.assertEqual(2, len(points))
        self.assertAlmostEqual(3.3, points.value(0, "terminal_v"), delta=0.000001)
        self.assertEqual(3.29, points.value(0, "zero_min_v"))
        self.assertIsNone(points.value(1, "zero_min_v"))
        self.assertEqual(4.0, points.value(-1, "drive_v"))
        self.assertEqual(0.03, points.value(-1, "drive_duty"))
        self.assertNotIn("drive_v", points[0])
        with self.assertRaisesRegex(ValueError, "endpoint"):
            points.append(5, 0, 0, 0.05)

    def test_capacity_is_bounded_and_never_grows(self):
        for invalid in (0, -1, MAX_CALIBRATION_POINTS + 1, True, 3.5):
            with self.assertRaises(ValueError):
                PackedPoints(invalid)
        points = PackedPoints(MAX_CALIBRATION_POINTS)
        buffer = points._values
        for index in range(MAX_CALIBRATION_POINTS):
            points.append(index / 10, 0, 0, index / 1000)
        with self.assertRaisesRegex(ValueError, "capacity"):
            points.append(31.1, 0, 0, 0.311)
        self.assertIs(buffer, points._values)
        self.assertEqual(MAX_CALIBRATION_POINTS, len(points))
        self.assertEqual(4976, len(buffer) * buffer.itemsize)

    def test_hot_interpolation_uses_no_point_snapshots_and_preserves_microamps(self):
        points = PackedPoints(31)
        reference = []
        for index in range(31):
            v = float(index)
            shunt = 0.000030 + v * 0.0000123
            duty = v / 40
            points.append(v, shunt, 0.000000002, duty)
            reference.append({"terminal_v": v, "shunt_v": shunt, "duty": duty})
        channel = {"points": points, "shunt_ohm": model.DRAIN_SHUNT_OHM}
        with patch.object(PackedPoints, "__getitem__", side_effect=AssertionError("point snapshot")):
            for index in range(601):
                voltage = index * 0.05
                baseline = model.interpolate(reference, voltage, "shunt_v")
                measured = baseline + 0.000001 * model.DRAIN_SHUNT_OHM
                actual = model.corrected_current(channel, voltage, measured)
                self.assertAlmostEqual(0.000001, actual, delta=0.000000001)
                self.assertAlmostEqual(voltage / 40, model.duty_for_voltage(channel, voltage), delta=0.00000012)


if __name__ == "__main__":
    unittest.main()
