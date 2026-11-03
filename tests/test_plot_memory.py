"""Plot coordinates share the fixed 64 KiB scan buffer without copies."""
import unittest
from firmware import model
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App, PackedPlotView
from test_ui import FakeDisplay, FakeInputs

class IdleEngine:
    state = "idle"

class PlotMemoryTests(unittest.TestCase):
    def test_full_capacity_views_share_exact_boot_buffer(self):
        raw = bytearray(64 * 1024)
        buffer = PackedScanBuffer(raw)
        app = App(FakeDisplay(), FakeInputs(), IdleEngine(), scan_buffer=buffer, restart=lambda: None)
        app._ram_drain_count = 64
        self.assertIs(buffer._data, raw)
        self.assertIsInstance(app.rows, PackedPlotView)
        self.assertFalse(hasattr(app, "limit_flags"))
        for index in range(model.MAX_POINTS):
            buffer.append({"vds": index / 1000, "vgs": 1., "id_a": index * 1e-6, "ig_a": 0.})
        self.assertEqual(len(raw), 65536)
        self.assertEqual(len(app.rows), 8192)
        self.assertEqual(len(app.curves), 4096)
        self.assertAlmostEqual(app.rows[-1], 4095e-6, places=8)
        self.assertEqual(app.curves[-1], 63)
        with self.assertRaises(ValueError):
            buffer.append({"vds": 1., "vgs": 1., "id_a": 0., "ig_a": 0.})
        buffer.clear()
        app.clear_results()
        self.assertEqual(len(app.rows), 0)
        self.assertIs(buffer._data, raw)

    def test_app_requires_shared_buffer_and_restart_callback(self):
        with self.assertRaises(ValueError):
            App(FakeDisplay(), FakeInputs(), IdleEngine())
        with self.assertRaises(ValueError):
            App(FakeDisplay(), FakeInputs(), IdleEngine(), scan_buffer=PackedScanBuffer())

if __name__ == "__main__":
    unittest.main()
