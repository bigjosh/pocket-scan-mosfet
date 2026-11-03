"""A cleanup marker or stale timing result must never certify a native scan."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_ram_ui import RamCloud, RamWifi
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore, fixture_calibration


class EarlyStopDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.pins, self.errors = [], []
        self.discard_failure = False
        owner = self

        class Pin:
            OUT = 1

            def __init__(self, number, mode, value):
                owner.pins.append((number, value))

        self.main = types.SimpleNamespace(
            App=App, Pin=Pin,
            time=types.SimpleNamespace(ticks_ms=lambda: self.now, ticks_diff=lambda a, b: a - b),
            _scan_timing=types.SimpleNamespace(last_index=99, stop=lambda: None))
        self.main.run = self.production_loop
        path = Path(__file__).resolve().parents[1] / "prototype" / "early_scan_stop_diagnostic.py"
        spec = importlib.util.spec_from_file_location("early_scan_stop_diagnostic_test", path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, main=self.main):
            spec.loader.exec_module(self.helper)

    def production_loop(self):
        self.now = 0
        self.instrument = HostInstrument()
        self.engine = Engine(self.instrument)
        self.buffer = PackedScanBuffer(capacity=8)
        self.cloud = RamCloud(self.buffer)
        self.store = MemoryStore()
        config = dict(model.DEFAULT_SETTINGS, drain_start_uv=100000, drain_stop_uv=400000,
                      drain_step_uv=100000, gate_start_uv=1000000, gate_stop_uv=1000000,
                      ina_average_count=4)
        if self.discard_failure:
            def fail_discard():
                raise OSError("synthetic discard failure")
            self.cloud.discard_scan = fail_discard
        try:
            self.app = self.main.App(FakeDisplay(), FakeInputs(), self.engine, config,
                fixture_calibration(), self.store, wifi=RamWifi(), cloud=self.cloud,
                scan_buffer=self.buffer, restart=lambda: None)
            for self.now in range(0, 60000, 20):
                self.app.tick(self.now)
            raise AssertionError("Diagnostic did not stop")
        except Exception as error:
            # Match production main.run: ordinary faults are logged and
            # swallowed, while DiagnosticDone (BaseException) unwinds it.
            self.errors.append(error)
            print("Scanner error", type(error).__name__)
        finally:
            self.instrument.zero()
            self.cloud.pause()

    def run_helper(self, fail_scalar=None):
        output = io.StringIO()
        normal_print = print

        def checked_print(*args, **kwargs):
            if len(args) > 1 and args[:2] == ("EARLY_STOP_MEASURED", fail_scalar):
                raise MemoryError("synthetic scalar logging failure")
            return normal_print(*args, **kwargs)

        with contextlib.redirect_stdout(output), patch("builtins.print", checked_print), \
                patch("builtins.open", side_effect=AssertionError("Filesystem access")):
            self.helper.run()
        self.assertIs(self.main.App, App)
        self.assertEqual([(21, 0), (20, 0)], self.pins[-2:])
        self.assertEqual({"gate": 0.0, "drain": 0.0}, self.instrument.duties)
        self.assertEqual(0, self.store.writes)
        self.assertNotIn("finish", self.cloud.calls)
        self.assertFalse(any(call.startswith("tick:") for call in self.cloud.calls))
        return output.getvalue()

    def test_stale_timing_index_does_not_pass_at_home_and_three_points_are_required(self):
        output = self.run_helper()
        self.assertEqual(99, self.main._scan_timing.last_index)  # Deliberately never refreshed.
        self.assertEqual(3, self.cloud.calls.count("point"))
        self.assertEqual(3, self.engine._index)
        self.assertEqual("cancelled", self.engine.state)
        self.assertEqual(0, len(self.buffer))
        self.assertEqual([], self.errors)
        self.assertIn("EARLY_STOP_PASS points=3", output)
        self.assertIn("EARLY_STOP_END passed=1", output)
        ready = output.index("EARLY_STOP_THREE_POINTS_OK")
        self.assertLess(output.index("EARLY_STOP_BEGIN"), ready)
        for key in ("vds", "vgs", "id_a", "ig_a"):
            self.assertLess(output.index("EARLY_STOP_MEASURED " + key), ready)

    def test_swallowed_scalar_logging_exception_has_no_success_marker(self):
        output = self.run_helper(fail_scalar="ig_a")
        self.assertEqual(3, self.cloud.calls.count("point"))
        self.assertIsInstance(self.errors[0], MemoryError)
        self.assertNotIn("EARLY_STOP_THREE_POINTS_OK", output)
        self.assertNotIn("EARLY_STOP_PASS", output)
        self.assertIn("EARLY_STOP_END passed=0", output)

    def test_cancel_cleanup_failure_cannot_pass_even_after_all_measurements_print(self):
        self.discard_failure = True
        output = self.run_helper()
        self.assertEqual(3, len(self.buffer))
        self.assertEqual("recording", self.cloud.state)
        self.assertTrue(self.engine.shutdown_succeeded)
        self.assertIn("EARLY_STOP_THREE_POINTS_OK", output)
        self.assertIsInstance(self.errors[0], AssertionError)
        self.assertNotIn("EARLY_STOP_PASS", output)
        self.assertIn("EARLY_STOP_END passed=0", output)

    def test_previous_success_does_not_survive_a_second_failed_run(self):
        self.assertIn("EARLY_STOP_PASS", self.run_helper())
        output = self.run_helper(fail_scalar="vds")
        self.assertNotIn("EARLY_STOP_PASS", output)
        self.assertIn("EARLY_STOP_END passed=0", output)

    def test_swallowed_restart_callback_error_after_cleanup_still_cannot_pass(self):
        with patch.object(self.helper, "stop", side_effect=MemoryError("synthetic callback failure")):
            output = self.run_helper()
        self.assertEqual(0, len(self.buffer))
        self.assertTrue(self.engine.shutdown_succeeded)
        self.assertIn("EARLY_STOP_THREE_POINTS_OK", output)
        self.assertIsInstance(self.errors[0], MemoryError)
        self.assertNotIn("EARLY_STOP_PASS", output)
        self.assertIn("EARLY_STOP_END passed=0", output)


if __name__ == "__main__":
    unittest.main()
