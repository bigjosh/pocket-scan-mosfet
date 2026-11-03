"""Real UI/engine/manager proof that only an exact three-point ACK may pass."""
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from firmware.ram_cloud import RamCloudManager
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore
from test_ram_ui import RamWifi


class UploadSmokeTests(unittest.TestCase):
    def setUp(self):
        self.pins, self.messages, self.cleanup = {}, [], []
        pins = self.pins
        class Pin:
            OUT = 1
            def __init__(self, number, mode=None, value=None):
                self.number = number
                if value is not None:
                    pins[number] = value
            def value(self):
                return pins[self.number]
        self.main = SimpleNamespace(App=App, _scan_timing=SimpleNamespace(stop=lambda: None))
        path = Path(__file__).parents[1] / "prototype" / "direct_upload_smoke.py"
        spec = importlib.util.spec_from_file_location("test_direct_upload_helper", path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"main": self.main, "model": model,
                                      "machine": SimpleNamespace(Pin=Pin)}):
            spec.loader.exec_module(self.helper)
        self.helper.time = SimpleNamespace(ticks_ms=lambda: 0, ticks_diff=lambda a, b: a - b)
        self.saved = dict(model.DEFAULT_SETTINGS, drain_start_uv=0, drain_stop_uv=2000000,
                          drain_step_uv=100000, gate_start_uv=-200000,
                          gate_stop_uv=2000000, gate_step_uv=100000)

    def simulate(self, cause=None):
        owner = self
        class Transport:
            def close(self):
                pass
            def start(self, url, scan, scan_id, sha256, count):
                owner.upload_count = count
                self.ack = {"ok": True, "scan_id": scan_id, "sha256": sha256,
                            "point_count": count, "sheet_id": 1}
                if cause == "wrong_ack":
                    self.ack["point_count"] = 4
            def step(self):
                return self.ack
        self.upload_count = 0
        self.instrument = HostInstrument()
        self.buffer, self.store = PackedScanBuffer(capacity=4096), MemoryStore()
        self.store.files["cloud.json"] = {"version": 1, "enabled": True,
                                          "url": "https://script.google.com/macros/s/PRIVATE/exec"}
        cloud = RamCloudManager(self.buffer, Transport(), self.store,
                                device_id="dead", random_bytes=lambda n: b"a" * n)
        inputs, wifi = FakeInputs(), RamWifi()
        def run_main():
            try:
                app = self.main.App(FakeDisplay(), inputs, Engine(self.instrument), self.saved,
                                    store=self.store, cloud=cloud, wifi=wifi,
                                    scan_buffer=self.buffer, restart=lambda: None)
                for now in range(0, 200000, 20):
                    if cause == "cancel" and app.screen == "scan":
                        inputs.events = [("left",)]
                    if cause == "swallowed" and app.screen == "scan":
                        raise MemoryError("main caught this")
                    if cause == "timeout" and app.screen == "scan":
                        app.tick(180001)
                    app.tick(now)
                raise AssertionError("Helper did not stop")
            except Exception:
                # The production main.run catches ordinary faults; returning
                # normally must not be mistaken for the helper's exit marker.
                pass
            finally:
                self.instrument.zero()
                self.cleanup.append(True)
        self.main.run = run_main
        output = io.StringIO()
        with patch("sys.stdout", output):
            if cause is None:
                self.helper.run()
            else:
                with self.assertRaises(RuntimeError):
                    self.helper.run()
        return output.getvalue()

    def test_grid_override_is_exact_and_does_not_change_saved_settings(self):
        before = dict(self.saved)
        limited = self.helper.first_three_settings(self.saved)
        self.assertEqual(limited["drain_stop_uv"], 200000)
        self.assertEqual(limited["gate_stop_uv"], -200000)
        self.assertEqual(model.scan_point_count(limited), 3)
        self.assertEqual(self.saved, before)
        self.assertEqual({key for key in before if before[key] != limited[key]},
                         {"drain_stop_uv", "gate_stop_uv"})
        with self.assertRaises(ValueError):
            self.helper.first_three_settings(dict(self.saved, drain_stop_uv=100000))

    def test_exact_ack_after_three_actual_points_and_cleanup_is_only_success(self):
        output = self.simulate()
        self.assertEqual(output.count("DIRECT_UPLOAD_POINT"), 3)
        self.assertIn("DIRECT_UPLOAD_ACK", output)
        self.assertIn("DIRECT_UPLOAD_PASS", output)
        self.assertNotIn("PRIVATE", output)
        self.assertEqual(self.upload_count, 3)
        self.assertEqual(self.cleanup, [True])
        self.assertEqual(self.pins, {21: 0, 20: 0})
        self.assertEqual(len(self.buffer), 0)
        self.assertEqual(self.store.writes, 0)
        self.assertIs(self.main.App, App)

    def test_cancel_wrong_ack_timeout_and_swallowed_exception_cannot_pass(self):
        for cause in ("cancel", "wrong_ack", "timeout", "swallowed"):
            with self.subTest(cause=cause):
                self.setUp()
                output = self.simulate(cause)
                self.assertNotIn("DIRECT_UPLOAD_PASS", output)
                self.assertNotIn("DIRECT_UPLOAD_ACK", output)
                self.assertIn("DIRECT_UPLOAD_ABORT", output)
                self.assertEqual(self.cleanup, [True])
                self.assertEqual(self.pins, {21: 0, 20: 0})
                self.assertEqual(self.store.writes, 0)
                if cause != "wrong_ack":
                    self.assertEqual(self.upload_count, 0)


if __name__ == "__main__":
    unittest.main()
