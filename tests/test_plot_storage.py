"""Regression: repeated scans reuse fixed buffers on MicroPython's nonmoving GC."""
import unittest
from firmware.ui import PlotValues


class PlotStorageTests(unittest.TestCase):
    def test_full_capacity_and_reuse_do_not_replace_backing_storage(self):
        values = PlotValues("<f", 4096 * 2)
        buffer = values._data
        for index in range(values.capacity):
            values.append(index / 2)
        self.assertEqual(len(values), 4096 * 2)
        self.assertEqual(values[-1], (4096 * 2 - 1) / 2)
        with self.assertRaises(ValueError):
            values.append(0)
        values.clear()
        self.assertIs(buffer, values._data)
        with self.assertRaises(IndexError):
            unused = values[0]
        values.append(-1.25)
        self.assertEqual(values[0], -1.25)
        self.assertEqual(len(values), 1)

    def test_curve_storage_and_negative_index_boundaries(self):
        values = PlotValues("<H", 4096)
        for index in range(4096):
            values.append(index)
        self.assertEqual(values[-4096], 0)
        self.assertEqual(values[-1], 4095)
        for index in (-4097, 4096):
            with self.assertRaises(IndexError):
                unused = values[index]

    def test_limit_flags_fit_one_fixed_byte_per_point_and_reuse(self):
        values = PlotValues("<B", 4096)
        buffer = values._data
        self.assertEqual(len(buffer), 4096)
        for repeat in range(3):
            values.clear()
            for index in range(4096):
                values.append(index % 8)
            self.assertIs(buffer, values._data)
            self.assertEqual(values[4095], 7)
            self.assertEqual(values[4094], 6)


if __name__ == "__main__":
    unittest.main()
