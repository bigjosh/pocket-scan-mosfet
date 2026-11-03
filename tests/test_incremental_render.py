"""Bounded LCD updates preserve measured points and commanded gate curves."""
import unittest

from firmware import model
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App, AMBER, BG, GRID, INK, MUTED, TEAL
from test_ui import FakeDisplay, FakeInputs, MemoryStore


class RecordingDisplay(FakeDisplay):
    def __init__(self):
        super().__init__()
        self.calls = []

    def fill(self, color):
        self.calls.append(("fill", color))
        super().fill(color)

    def text(self, text, x, y, color, bg=None, scale=1):
        self.calls.append(("text", str(text), x, y, color, scale))
        super().text(text, x, y, color, bg, scale)

    def fill_rect(self, *args):
        self.calls.append(("fill_rect",) + args)

    def rect(self, *args):
        self.calls.append(("rect",) + args)

    def line(self, *args):
        self.calls.append(("line",) + args)


class IdleEngine:
    state = "idle"
    progress = 0.0


class IncrementalRenderTests(unittest.TestCase):
    def setUp(self):
        self.display, self.engine = RecordingDisplay(), IdleEngine()
        settings = dict(model.DEFAULT_SETTINGS, drain_start_uv=0, drain_stop_uv=1000000,
                        drain_step_uv=100000)
        self.buffer = PackedScanBuffer(capacity=128)
        self.app = App(self.display, FakeInputs(), self.engine, settings, store=MemoryStore(),
                       scan_buffer=self.buffer, restart=lambda: None)
        self.app.latest = self.row()
        self.app.goto("scan")
        self.app.render()
        self.display.calls.clear()

    @staticmethod
    def row(**changes):
        value = {"vds": 0.5, "vgs": 1.0, "id_a": 0.1e-6, "ig_a": 0.0,
                 "requested_vds": 0.5, "requested_vgs": 1.0, "curve": 0}
        value.update(changes)
        return value

    def test_unchanged_live_panel_performs_no_lcd_work(self):
        self.app._render_live()
        self.app._render_live()
        self.assertEqual([], self.display.calls)

    def test_shorter_value_clears_its_field_and_leaves_other_fields_alone(self):
        self.app.latest = self.row(vds=12.0)
        self.app._render_live()
        self.display.calls.clear()
        self.app.latest = self.row(vds=0.2)
        self.app._render_live()
        self.assertEqual([("fill_rect", 338, 67, 142, 16, BG),
                          ("text", "0.200", 338, 67, INK, 2)], self.display.calls)


    def test_missing_request_erases_old_text(self):
        del self.app.latest["requested_vgs"]
        self.app._render_live()
        self.assertEqual([("fill_rect", 338, 128, 142, 8, BG)], self.display.calls)

    def test_progress_only_updates_changed_strip_and_can_shrink(self):
        self.engine.progress = 0.5
        self.app._render_live()
        self.assertIn(("fill_rect", 16, 263, 224, 4, TEAL), self.display.calls)
        self.display.calls.clear()
        self.engine.progress = 0.25
        self.app._render_live()
        self.assertIn(("fill_rect", 128, 263, 112, 4, GRID), self.display.calls)
        self.assertNotIn(("fill_rect", 16, 263, 448, 4, GRID), self.display.calls)

    def test_full_render_and_clear_reuse_cache_storage_and_refresh_fields(self):
        caches = (self.app._live_values, self.app._live_label_colors,
                  self.app._live_requests, self.app._live_misc)
        self.app.render()
        self.assertIn(("text", "0.500", 338, 67, INK, 2), self.display.calls)
        self.app.clear_results()
        for before, after in zip(caches, (self.app._live_values, self.app._live_label_colors,
                                          self.app._live_requests, self.app._live_misc)):
            self.assertIs(before, after)
            self.assertTrue(all(value is None for value in after))
        self.display.calls.clear()
        self.app._render_live()
        self.assertTrue(any(call[:2] == ("text", "--") for call in self.display.calls))



    def test_add_point_never_draws_and_dirty_axes_are_consumed_with_four_points(self):
        for index in range(9):
            row = self.row(vds=0.1 * index, id_a=(index + 1) * 10e-6)
            self.buffer.append(row)
            self.app._add_point(row)
        self.assertEqual([], self.display.calls)
        self.assertTrue(self.app._plot_axes_dirty)
        self.assertEqual(0, self.app._plot_cursor)
        self.app._draw_points()
        self.assertFalse(self.app._plot_axes_dirty)
        self.assertEqual(4, self.app._plot_cursor)
        self.assertIn(("fill_rect", 0, 42, 330, 200, BG), self.display.calls)
        self.display.calls.clear()
        self.app._draw_points()
        self.assertEqual(8, self.app._plot_cursor)
        self.assertNotIn(("fill_rect", 0, 42, 330, 200, BG), self.display.calls)
        self.assertAlmostEqual(90e-6, self.app.rows[-1], places=10)

    def test_current_ramp_uses_geometric_headroom_and_few_rescales(self):
        rescales = 0
        for current in range(1, 101):
            row = self.row(id_a=current * 1e-6)
            self.buffer.append(row)
            self.app._add_point(row)
            if self.app._plot_axes_dirty:
                rescales += 1
                self.assertGreaterEqual(self.app._plot_ymax, current * 1.5 - 1e-9)
                self.app._plot_axes_dirty = False
        self.assertLessEqual(rescales, 9)
        self.assertEqual([], self.display.calls)

    def test_endpoint_scatter_does_not_redraw_for_each_millivolt(self):
        rescales = 0
        for index in range(1, 101):
            measured = 1.0 + index / 1000
            row = self.row(vds=measured)
            self.buffer.append(row)
            self.app._add_point(row)
            if self.app._plot_axes_dirty:
                rescales += 1
                self.app._plot_axes_dirty = False
            self.assertAlmostEqual(measured, self.app.rows[-2], places=6)
        self.assertLessEqual(rescales, 2)
        self.assertEqual([], self.display.calls)


if __name__ == "__main__":
    unittest.main()
