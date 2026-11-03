"""Exercise the native helper's real App/RAM path with a host LCD substitute."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

from firmware import ui
from firmware.ram_scan import PackedScanBuffer
from test_ui import FakeDisplay


class EmptyScanFaultNativeSmokeTests(unittest.TestCase):
    def load_helper(self, fail_render=False):
        pins = []

        class Pin:
            OUT = 1

            def __init__(self, number, mode, value):
                self.number, self.level = number, value
                pins.append((number, value))

            def value(self):
                return self.level

        class Display(FakeDisplay):
            def __init__(self, raster_pool):
                super().__init__()

            def text(self, *args, **kwargs):
                if fail_render:
                    raise OSError("display unavailable")
                return super().text(*args, **kwargs)

        main = types.SimpleNamespace(
            App=ui.App, Pin=Pin, Display=Display, PackedScanBuffer=PackedScanBuffer,
            _scan_bytes=bytearray(4096 * 17), _display_buffer=bytearray(1),
            _scan_timing=types.SimpleNamespace(stop=Mock()),
            tls_memory=types.SimpleNamespace(available=lambda: True),
            gc=types.SimpleNamespace(collect=lambda: None),
            time=types.SimpleNamespace(ticks_ms=lambda: 100), BG=ui.BG, INK=ui.INK,
            Instrument=Mock(side_effect=AssertionError("Physical hardware is forbidden")))
        path = Path(__file__).resolve().parents[1] / "prototype" / "empty_scan_fault_native_smoke.py"
        spec = importlib.util.spec_from_file_location("empty_scan_fault_native_smoke_test", path)
        helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, main=main):
            spec.loader.exec_module(helper)
        return helper, main, pins

    def test_real_app_seal_draws_all_fault_labels_and_both_exits_discard_without_upload(self):
        helper, main, pins = self.load_helper()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                patch("builtins.open", side_effect=AssertionError("File access forbidden")):
            helper.run()
        text = output.getvalue()
        self.assertIn("EMPTY_SCAN_FAULT_SCREEN button=left labels=127", text)
        self.assertIn("EMPTY_SCAN_FAULT_SCREEN button=right labels=127", text)
        self.assertIn("EMPTY_SCAN_FAULT_PASS exits=2 finish=0 upload=0 files_written=0 outputs_low=1", text)
        main.Instrument.assert_not_called()
        self.assertEqual([(21, 0), (20, 0), (21, 0), (20, 0)], pins)

    def test_render_failure_still_forces_gate_then_drain_low(self):
        helper, main, pins = self.load_helper(fail_render=True)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(OSError, "display unavailable"):
            helper.run()
        main.Instrument.assert_not_called()
        self.assertEqual([(21, 0), (20, 0)], pins[-2:])


if __name__ == "__main__":
    unittest.main()
