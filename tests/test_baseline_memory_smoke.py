"""Run the native helper's wiring on the host without machine or file access.

This does not substitute for native heap testing. It checks that the helper
reaches its real UI/calibrator path and restores the binary writer after errors.
"""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import current_baseline, engine, model, ram_scan, ui
from test_ui import FakeDisplay


class SyntheticInstrument:
    def __init__(self):
        self.drain = self.gate = 0.0
        self.commands = self.zeroes = self.reads = 0

    def set_duty(self, role, value):
        self.commands += 1
        setattr(self, role, value)

    def zero(self):
        self.zeroes += 1
        self.drain = self.gate = 0.0

    def poll_alerts(self):
        pass

    def begin_measurement(self, average_count):
        pass

    def poll_measurement(self):
        return True


class NoFiles:
    def __init__(self):
        self.writes = 0


class Inputs:
    def poll(self, now):
        return ()


class Restart:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


class BaselineMemorySmokeTests(unittest.TestCase):
    def setUp(self):
        self.pins, self.stops, self.payloads = [], [], []
        self.main = types.SimpleNamespace(
            _baseline_bytes=bytearray(current_baseline.BUFFER_BYTES),
            _baseline_workspace=bytearray(current_baseline.WORKSPACE_BYTES),
            _scan_bytes=bytearray(65536), _tls_reserved=True,
            _scan_timing=types.SimpleNamespace(stop=lambda: self.stops.append(True)))
        self.smoke = types.SimpleNamespace(
            SyntheticInstrument=SyntheticInstrument, NoFiles=NoFiles,
            SmokeDisplay=FakeDisplay, SyntheticInputs=Inputs, RestartCounter=Restart,
            _pins_low=lambda: self.pins.append(True), _heap=lambda: 123456,
            _full_payload=self.full_payload)
        aliases = {"main": self.main, "current_baseline": current_baseline,
                   "direct_memory_smoke": self.smoke, "model": model,
                   "engine": engine, "ram_scan": ram_scan, "ui": ui}
        path = Path(__file__).resolve().parents[1] / "prototype" / "baseline_memory_smoke.py"
        spec = importlib.util.spec_from_file_location("_baseline_smoke_host", path)
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, aliases):
            spec.loader.exec_module(self.module)

    def full_payload(self, buffer, display, store, app, current_correction=None):
        self.assertEqual(app.screen, "calibration_done")
        self.assertEqual(len(current_correction["sha256"]), 64)
        self.assertTrue(app._calibration.shutdown_succeeded)
        self.assertEqual(store.writes, 0)
        self.assertIs(app._calibration.result._data, self.main._baseline_workspace)
        self.payloads.append(current_correction)

    def test_complete_helper_runs_without_files_and_restores_writer(self):
        original = current_baseline.Baseline.save
        with patch("builtins.open", side_effect=AssertionError("unexpected file access")):
            self.module.run()
        self.assertIs(current_baseline.Baseline.save, original)
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(len(self.pins), 2)
        self.assertEqual(self.stops, [True])

    def test_failure_restores_writer_and_runs_pin_cleanup(self):
        original = current_baseline.Baseline.save
        with patch.object(self.module.SyntheticInstrument, "read_codes_into",
                          side_effect=OSError("synthetic read failure")):
            with patch("builtins.open", side_effect=AssertionError("unexpected file access")):
                with self.assertRaisesRegex(AssertionError, "synthetic read failure"):
                    self.module.run()
        self.assertIs(current_baseline.Baseline.save, original)
        self.assertEqual(self.payloads, [])
        self.assertEqual(len(self.pins), 2)
        self.assertEqual(self.stops, [True])


if __name__ == "__main__":
    unittest.main()
