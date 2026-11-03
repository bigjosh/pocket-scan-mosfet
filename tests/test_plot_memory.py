"""Early-allocated plot storage is reused without reducing point capacity."""

import unittest

from firmware import model
from firmware.ui import App, PlotValues
from test_ui import FakeDisplay, FakeInputs


class IdleEngine:
    state = "idle"


class PlotMemoryTests(unittest.TestCase):
    def test_app_wraps_exact_boot_buffers_without_copying(self):
        buffers = (bytearray(32 * 1024), bytearray(8 * 1024), bytearray(4 * 1024))
        app = App(FakeDisplay(), FakeInputs(), IdleEngine(), plot_buffers=buffers)
        self.assertIs(buffers[0], app.rows._data)
        self.assertIs(buffers[1], app.curves._data)
        self.assertIs(buffers[2], app.limit_flags._data)
        self.assertEqual(model.MAX_POINTS * 2, app.rows.capacity)
        self.assertEqual(model.MAX_POINTS, app.curves.capacity)
        self.assertEqual(model.MAX_POINTS, app.limit_flags.capacity)
        for index in range(model.MAX_POINTS):
            app.rows.append(float(index))
            app.rows.append(float(index) / 2)
            app.curves.append(index)
            app.limit_flags.append(index & 7)
        self.assertEqual(float(model.MAX_POINTS - 1) / 2, app.rows[-1])
        self.assertEqual(model.MAX_POINTS - 1, app.curves[-1])
        self.assertEqual(7, app.limit_flags[-1])
        app.clear_results()
        self.assertEqual(0, len(app.rows))
        self.assertIs(buffers[0], app.rows._data)

    def test_external_buffer_rejects_wrong_size_and_readonly_storage(self):
        for value in (bytearray(7), bytearray(9), bytes(8), memoryview(bytearray(8))):
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                PlotValues("<f", 2, value)
        with self.assertRaises(ValueError):
            App(FakeDisplay(), FakeInputs(), IdleEngine(), plot_buffers=(bytearray(1),))

    def test_buffer_capacity_bound_remains_enforced(self):
        raw = bytearray(8)
        values = PlotValues("<f", 2, raw)
        values.append(1.25)
        values.append(2.5)
        with self.assertRaises(ValueError):
            values.append(3.75)
        self.assertEqual([1.25, 2.5], [values[0], values[1]])
        self.assertIs(raw, values._data)


if __name__ == "__main__":
    unittest.main()
