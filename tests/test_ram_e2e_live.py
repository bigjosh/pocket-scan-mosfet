"""Host-only live-helper checks using real UI, engine, packing and ACK checks."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from firmware.ram_cloud import RamCloudManager
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_offset_calibration import OffsetInstrument, calibrate, offset_settings
from test_ram_cloud import Store, Transport
from test_ram_ui import RamWifi
from test_ui import FakeDisplay, FakeInputs, MemoryStore


class Reboot(BaseException):
    pass


class AutoAck(Transport):
    def step(self):
        result = dict(self.expected)
        if self.fault:
            result.update(sha256="0" * 64, url="PRIVATE_SERVER_URL")
        return result


class LiveHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        engine, _ = calibrate(hardware)
        assert engine.state == "complete", engine.error
        cls.calibration = engine.calibration

    def setUp(self):
        self.now = 0
        self.hardware = OffsetInstrument()
        self.hardware.floors["drain"] = 3.58
        self.transport, self.cloud_store, self.ui_store = AutoAck(), Store(), MemoryStore()
        self.inputs = FakeInputs()
        self.saved = offset_settings(drain_start_uv=500000, drain_stop_uv=500000)
        self.original = copy.deepcopy(self.saved)
        self.buffer = PackedScanBuffer(capacity=32)
        self.cancel = False
        self.restarts = 0
        self.main = types.SimpleNamespace(
            App=App, RamCloudManager=RamCloudManager, model=model,
            time=types.SimpleNamespace(ticks_ms=lambda: self.now, ticks_diff=lambda a, b: a - b),
            gc=types.SimpleNamespace(mem_free=lambda: 123456), storage=types.SimpleNamespace(json=json))
        self.main.run = self.production_loop
        path = Path(__file__).resolve().parents[1] / "prototype" / "ram_e2e_live.py"
        spec = importlib.util.spec_from_file_location("ram_e2e_live_test", path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"main": self.main}):
            spec.loader.exec_module(self.helper)

    def production_loop(self):
        self.cloud = self.main.RamCloudManager(self.buffer, self.transport, self.cloud_store,
            device_id="face", random_bytes=lambda n: b"\x01" * n,
            utc_provider=lambda: "2026-11-02T00:00:00Z")
        wifi = RamWifi()
        engine = Engine(self.hardware)
        def restart():
            self.restarts += 1
            raise Reboot()
        try:
            self.app = self.main.App(FakeDisplay(), self.inputs, engine, self.saved,
                copy.deepcopy(self.calibration), self.ui_store, wifi=wifi, cloud=self.cloud,
                scan_buffer=self.buffer, restart=restart)
            for self.now in range(0, 120000, 20):
                self.hardware.now = self.now
                if self.cancel and engine.state == "scanning":
                    self.inputs.events = [("left",)]
                self.app.tick(self.now)
            raise AssertionError("Host loop failed to finish")
        finally:
            self.hardware.zero()
            self.cloud.pause()
            wifi.pause()

    def run_helper(self, reboot=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.open", side_effect=AssertionError("filesystem used")):
            if reboot:
                with self.assertRaises(Reboot):
                    self.helper.run()
            else:
                self.helper.run()
        self.assertIs(self.main.App, App)
        self.assertIs(self.main.RamCloudManager, RamCloudManager)
        self.assertEqual(self.original, self.saved)
        self.assertEqual(0, self.ui_store.writes)
        self.assertEqual([], self.cloud_store.writes)
        self.assertEqual({"drain": 0.0, "gate": 0.0}, self.hardware.duties)
        return output.getvalue()

    def test_actual_nine_points_and_exact_ack_logged_before_discard(self):
        output = self.run_helper(reboot=True)
        rows = [json.loads(line.split(" ", 1)[1]) for line in output.splitlines()
                if line.startswith("RAM_E2E_POINT ")]
        self.assertEqual(list(range(9)), [row["index"] for row in rows])
        for row in rows:
            self.assertAlmostEqual(max(0.28, 0.1 + (row["index"] % 3) * 0.5), row["vds"], places=6)
            self.assertAlmostEqual(row["index"] // 3 - 1, row["vgs"], places=6)
        ack = json.loads(next(line.split(" ", 1)[1] for line in output.splitlines()
                              if line.startswith("RAM_E2E_ACK ")))
        self.assertEqual((True, 9, 123, "complete"),
                         (ack["exact_match"], ack["point_count"], ack["sheet_id"], ack["cloud_state"]))
        self.assertEqual(64, len(ack["sha256"]))
        self.assertEqual(0, len(self.buffer))  # Production discard ran after ACK log.
        self.assertEqual(1, self.restarts)
        self.assertNotIn("RAM_E2E_RETURNED", output)
        begin = json.loads(next(line.split(" ", 1)[1] for line in output.splitlines()
                                if line.startswith("RAM_E2E_BEGIN ")))
        self.assertEqual((100000, 1100000, True),
                         (begin["drain_start_uv"], begin["drain_stop_uv"], begin["low_boundary_excluded"]))
        self.assertEqual(self.original["ina_average_count"], begin["ina_average_count"])
        self.assertEqual(model.acquisition_metadata(self.original), begin["acquisition"])
        self.assertEqual(3, begin["settings_version"])
        self.assertNotIn("batches", begin)

    def test_nondefault_saved_hardware_average_is_preserved_through_exact_ack(self):
        self.saved["ina_average_count"] = 128
        self.original = copy.deepcopy(self.saved)
        fixture_hardware = OffsetInstrument()
        fixture_hardware.floors["drain"] = 3.58
        calibration_engine, _ = calibrate(fixture_hardware, self.saved)
        self.assertEqual("complete", calibration_engine.state, calibration_engine.error)
        self.calibration = calibration_engine.calibration
        output = self.run_helper(reboot=True)
        begin = json.loads(next(line.split(" ", 1)[1] for line in output.splitlines()
                                if line.startswith("RAM_E2E_BEGIN ")))
        self.assertEqual(128, begin["ina_average_count"])
        self.assertEqual(128, begin["acquisition"]["ina_average_count"])

    def test_stale_calibration_stops_before_any_drive_or_upload(self):
        self.calibration = copy.deepcopy(self.calibration)
        self.calibration["metadata"].pop("acquisition_mode")
        # Production construction/validation fails closed before Home arms a scan.
        with self.assertRaises(ValueError):
            self.run_helper()
        self.assertEqual([], self.hardware.commands)
        self.assertEqual(0, self.transport.starts)

    def test_bad_ack_stops_without_retry_and_does_not_log_server_fields(self):
        self.transport.fault = True
        output = self.run_helper()
        self.assertIn("RAM_E2E_ABORT upload_error", output)
        self.assertIn("RAM_E2E_RETURNED", output)
        self.assertNotIn("PRIVATE_SERVER_URL", output)
        self.assertNotIn("RAM_E2E_ACK", output)
        self.assertEqual(0, self.restarts)
        self.assertEqual(1, self.transport.starts)

    def test_zero_point_partial_scan_shows_fault_before_upload(self):
        self.hardware.current["drain"] = 0.006
        output = self.run_helper()
        self.assertIn("RAM_E2E_ABORT scan_error", output)
        self.assertIn("No measured points.", self.app.notice)
        self.assertEqual(0, len(self.buffer))
        self.assertEqual("recording", self.cloud.state)
        self.assertEqual(0, self.transport.starts)
        self.assertEqual(0, self.restarts)

    def test_nonempty_partial_scan_stops_diagnostic_before_upload(self):
        original = self.hardware.read_pair

        def overrange_after_first_point():
            if len(self.buffer):
                self.hardware.current["drain"] = 0.006
            return original()

        self.hardware.read_pair = overrange_after_first_point
        output = self.run_helper()
        self.assertIn("RAM_E2E_ABORT partial_scan", output)
        self.assertEqual(1, len(self.buffer))
        self.assertEqual("preparing", self.cloud.state)
        self.assertEqual("partial", self.cloud.scan.outcome)
        self.assertEqual(0, self.transport.starts)
        self.assertEqual(0, self.restarts)

    def test_timeout_stops_without_retry(self):
        self.helper.TIMEOUT_MS = 1
        output = self.run_helper()
        self.assertIn("RAM_E2E_ABORT timeout", output)
        self.assertEqual(0, self.transport.starts)
        self.assertEqual(0, self.restarts)

    def test_physical_cancel_retains_production_discard_restart(self):
        self.cancel = True
        output = self.run_helper(reboot=True)
        self.assertIn("RAM_E2E_ABORT cancelled", output)
        self.assertNotIn("RAM_E2E_ACK", output)
        self.assertEqual(0, len(self.buffer))
        self.assertEqual(1, self.restarts)


if __name__ == "__main__":
    unittest.main()
