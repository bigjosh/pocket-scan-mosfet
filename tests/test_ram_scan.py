"""Fixed-memory measured scan records and exact gate target classification."""

import math
import struct
import unittest
from unittest.mock import patch

from firmware import model, ram_scan


def point(**changes):
    row = {"vds": 1.234567, "vgs": 2.125, "id_a": 0.00123456789,
           "ig_a": -1.23456789e-9, "requested_vgs": 2.125,
           "drain_voltage_limited": False, "gate_voltage_limited": False}
    row.update(changes)
    return row


class PackedScanBufferTests(unittest.TestCase):
    def test_record_layout_and_boot_buffer_are_exactly_17_bytes_per_point(self):
        self.assertEqual(17, struct.calcsize(ram_scan.RECORD_FORMAT))
        self.assertEqual(17, ram_scan.RECORD_BYTES)
        self.assertEqual(69632, ram_scan.BUFFER_BYTES)
        backing = bytearray(ram_scan.BUFFER_BYTES)
        values = ram_scan.PackedScanBuffer(backing)
        self.assertIs(backing, values._data)
        row = point(drain_voltage_limited=True, gate_voltage_limited=True, vgs=2.3)
        values.append(row)
        expected = struct.pack("<4fB", row["vds"], row["vgs"], row["id_a"], row["ig_a"], 7)
        self.assertEqual(expected, backing[:17])
        self.assertEqual(struct.unpack("<4fB", expected), values.get(0))
        self.assertEqual(values.get(0), values.get_tuple(0))
        self.assertEqual(values.get(0), values[0])

    def test_4096_points_full_boundary_and_clear_reuse_without_allocating(self):
        backing = bytearray(ram_scan.BUFFER_BYTES)
        values = ram_scan.PackedScanBuffer(backing)
        row = point()
        with patch("firmware.ram_scan.bytearray", side_effect=AssertionError("new backing allocation"), create=True), \
                patch.object(ram_scan.struct, "pack", side_effect=AssertionError("temporary packed records")):
            for index in range(model.MAX_POINTS):
                row["vds"] = index / 1000
                values.append(row)
            self.assertEqual(4096, len(values))
            self.assertEqual(4096, values.count)
            before = bytes(backing)
            with self.assertRaisesRegex(ValueError, "full"):
                values.append(row)
            self.assertEqual(4096, values.count)
            self.assertEqual(before, backing)
            self.assertIs(backing, values._data)
            self.assertEqual(69632, len(backing))
            self.assertEqual(0.0, values[0][0])
            self.assertAlmostEqual(4.095, values[-1][0], places=6)
            values.clear()
            self.assertEqual(0, len(values))
            self.assertEqual(before, backing)  # clear does not zero/copy 69 KiB.
            with self.assertRaises(IndexError):
                values.get(0)
            row["vds"] = -0.25
            values.append(row)
            self.assertEqual(-0.25, values[0][0])
            self.assertIs(backing, values._data)

    def test_constructor_rejects_invalid_capacity_or_nonexact_writable_buffer(self):
        for capacity in (0, -1, model.MAX_POINTS + 1, True, 1.0, "1"):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                ram_scan.PackedScanBuffer(capacity=capacity)
        for backing in (bytearray(16), bytearray(18), bytes(17), memoryview(bytearray(17))):
            with self.subTest(backing=type(backing).__name__), self.assertRaises(ValueError):
                ram_scan.PackedScanBuffer(backing, capacity=1)

    def test_negative_index_and_type_boundaries(self):
        values = ram_scan.PackedScanBuffer(capacity=2)
        for index in (0, -1):
            with self.assertRaises(IndexError):
                values.get(index)
        values.append(point(vds=1))
        values.append(point(vds=2))
        self.assertEqual(1, values[-2][0])
        self.assertEqual(2, values[-1][0])
        for index in (-3, 2):
            with self.assertRaises(IndexError):
                values.get(index)
        for index in (True, 0.0, "0", slice(0, 1)):
            with self.assertRaises(TypeError):
                values.get(index)

    def test_invalid_values_reject_before_count_or_storage_changes(self):
        for field in ("vds", "vgs", "id_a", "ig_a", "requested_vgs"):
            for invalid in (float("nan"), float("inf"), -float("inf"), 3.5e38, -3.5e38,
                            10 ** 1000, True, None, "1.0"):
                with self.subTest(field=field, invalid_type=type(invalid).__name__):
                    values = ram_scan.PackedScanBuffer(capacity=2)
                    values.append(point())
                    before = bytes(values._data)
                    with self.assertRaises(ValueError):
                        values.append(point(**{field: invalid}))
                    self.assertEqual(1, values.count)
                    self.assertEqual(before, values._data)

    def test_missing_values_and_invalid_flag_types_do_not_publish_a_point(self):
        for field in ("vds", "vgs", "id_a", "ig_a", "requested_vgs"):
            values = ram_scan.PackedScanBuffer(capacity=1)
            row = point()
            del row[field]
            with self.assertRaises(ValueError):
                values.append(row)
            self.assertEqual(0, values.count)
        for field in ("drain_voltage_limited", "gate_voltage_limited"):
            for invalid in (0, 1, None, "true", [], float("nan")):
                values = ram_scan.PackedScanBuffer(capacity=1)
                with self.assertRaises(ValueError):
                    values.append(point(**{field: invalid}))
                self.assertEqual(0, values.count)
        for row in (None, [], "point", 1):
            with self.assertRaises(ValueError):
                ram_scan.PackedScanBuffer(capacity=1).append(row)

    def test_gate_flag_preserves_inclusive_float_boundaries(self):
        for target in (0.0, 0.1, 1.0, -1.0, 18.8):
            lower = target - model.GATE_PLOT_TOLERANCE_V
            upper = target + model.GATE_PLOT_TOLERANCE_V
            for measured, expected in ((lower, 0), (upper, 0), (target, 0),
                                       (math.nextafter(lower, -math.inf), 4),
                                       (math.nextafter(upper, math.inf), 4)):
                with self.subTest(target=target, measured=measured):
                    values = ram_scan.PackedScanBuffer(capacity=1)
                    values.append(point(vgs=measured, requested_vgs=target))
                    self.assertEqual(expected, values[0][4])

    def test_gate_plot_band_accepts_50_and_100_mv_but_rejects_one_uv_beyond(self):
        for target in (-1.0, 0.0, 1.0, 2.125, 18.8):
            for offset_uv, expected in ((-100001, 4), (-100000, 0), (-50000, 0),
                                        (50000, 0), (100000, 0), (100001, 4)):
                with self.subTest(target=target, offset_uv=offset_uv):
                    values = ram_scan.PackedScanBuffer(capacity=1)
                    values.append(point(vgs=target + offset_uv / 1000000,
                                        requested_vgs=target))
                    self.assertEqual(expected, values[0][4])

    def test_limit_flags_are_independent_and_optional_flags_default_false(self):
        for drain in (False, True):
            for gate in (False, True):
                for off_target in (False, True):
                    values = ram_scan.PackedScanBuffer(capacity=1)
                    values.append(point(drain_voltage_limited=drain, gate_voltage_limited=gate,
                                        vgs=2.3 if off_target else 2.125))
                    self.assertEqual(int(drain) | (int(gate) << 1) | (int(off_target) << 2), values[0][4])
        row = point()
        del row["drain_voltage_limited"], row["gate_voltage_limited"]
        values = ram_scan.PackedScanBuffer(capacity=1)
        values.append(row)
        self.assertEqual(0, values[0][4])

    def test_float32_currents_retain_submicroamp_resolution_and_input_is_not_retained(self):
        values = ram_scan.PackedScanBuffer(capacity=3)
        row = point()
        for current in (1.23456789e-12, -1.23456789e-9, 0.000123456789):
            row["id_a"], row["ig_a"] = current, -current
            values.append(row)
            result = values[-1]
            self.assertNotEqual(0, result[2])
            self.assertAlmostEqual(current, result[2], delta=abs(current) * 1e-7)
            self.assertAlmostEqual(-current, result[3], delta=abs(current) * 1e-7)
        row.clear()
        self.assertAlmostEqual(1.23456789e-12, values[0][2], delta=1e-19)

    def test_pack_failure_cannot_advance_visible_count(self):
        values = ram_scan.PackedScanBuffer(capacity=2)
        values.append(point())
        first = values[0]
        with patch.object(ram_scan.struct, "pack_into", side_effect=MemoryError("pack failed")):
            with self.assertRaises(MemoryError):
                values.append(point())
        self.assertEqual(1, len(values))
        self.assertEqual(first, values[0])


if __name__ == "__main__":
    unittest.main()
