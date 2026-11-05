"""Mixed sensor codes through current correction and the RAM upload payload."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from firmware import current_baseline, model, ram_payload
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from tests.test_hardware import MachineFake


def scan_settings():
    return dict(model.DEFAULT_SETTINGS, drain_start_uv=1000000,
                drain_stop_uv=1000000, gate_start_uv=1000000,
                gate_stop_uv=1000000, ina_average_count=1)


class RecordingBaseline(current_baseline.Baseline):
    def __init__(self):
        super().__init__()
        self.lookups = []

    def offset_nv(self, role, bus_uv):
        if role == 1 or role == "gate":
            raise AssertionError("A gate current must not look up its baseline")
        self.lookups.append((role, bus_uv))
        return super().offset_nv(role, bus_uv)


class MixedReadbackTests(unittest.TestCase):
    def setUp(self):
        self.fake = MachineFake()
        spec = importlib.util.spec_from_file_location(
            "firmware.hardware", Path(__file__).resolve().parents[1] / "firmware/hardware.py")
        hardware = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"machine": self.fake.module}):
            spec.loader.exec_module(hardware)
        self.instrument = hardware.Instrument()
        self.settings = scan_settings()

    def tearDown(self):
        self.instrument.close()

    def baseline(self, maximum_gate_uv=20000000):
        table = RecordingBaseline()
        table.begin(self.settings, self.instrument.identity)
        table.add_knot(0, 0, 2500, 0)
        table.add_knot(0, 20000000, 2500, 60000)
        table.add_knot(1, 0, 0, 0)
        table.add_knot(1, 3300000, 100000, 8400)
        table.add_knot(1, maximum_gate_uv, 500000, 60000)
        table.finish()
        return table

    def acquire(self, gate_code, gate_bus_code=16896, baseline=None):
        self.fake.set_codes(0x44, bus=3000, shunt=400)
        self.fake.set_codes(0x40, bus=gate_bus_code, shunt=gate_code)
        engine = Engine(self.instrument, baseline=baseline)
        engine.start_scan(self.settings)
        rows = []
        for now in range(1000):
            if engine._measurement_waiting:
                self.fake.set_ready(0x44)
                self.fake.set_ready(0x40)
            rows.extend(value for event, value in engine.tick(now) if event == "point")
            if engine.state != "scanning":
                break
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertEqual(len(rows), 1)
        self.assertTrue(engine.shutdown_succeeded)
        self.assertTrue(all(pwm.duty == 0 for pwm in self.fake.pwms.values()))
        return rows[0]

    def test_drain_corrected_and_positive_gate_raw_without_gate_lookup(self):
        baseline = self.baseline()
        # 640 INA228 codes = 200 uV; stored gate offset is deliberately nonzero.
        row = self.acquire(640, baseline=baseline)
        self.assertEqual(baseline.lookups, [(0, 3750000)])
        self.assertAlmostEqual(row["ig_a"], 0.0002 / model.GATE_SHUNT_OHM, places=13)
        self.assertAlmostEqual(row["id_a"], 0.0009975 / model.DRAIN_SHUNT_OHM, places=12)
        # Baseline correction changes current only: terminal voltage uses the
        # full raw drop, and neither voltage is replaced by its requested value.
        self.assertAlmostEqual(row["vgs"], -0.0002, places=12)
        self.assertAlmostEqual(row["vds"], 0.449, places=12)
        self.assertEqual(row["requested_vgs"], 1.0)

    def test_fractional_gate_bus_lsb_remains_measured_voltage_without_lookup(self):
        baseline = self.baseline()
        row = self.acquire(640, gate_bus_code=16897, baseline=baseline)
        # Native 16897 * 195.3125 uV = 3300195.3125 uV.
        self.assertAlmostEqual(row["vgs"], 3.3001953125 - 0.0002 - 3.3, places=12)
        self.assertEqual(baseline.lookups, [(0, 3750000)])

    def test_positive_and_negative_100_microvolt_readback_without_correction(self):
        for sign in (1, -1):
            with self.subTest(sign=sign):
                row = self.acquire(sign * 320)
                self.assertEqual(model.GATE_SHUNT_OHM, 98.8)
                self.assertAlmostEqual(row["ig_a"], sign * 0.0001 / model.GATE_SHUNT_OHM, places=13)
                self.assertAlmostEqual(row["vgs"], -sign * 0.0001, places=12)

    def test_negative_gate_current_is_raw_while_drain_correction_is_active(self):
        baseline = self.baseline()
        row = self.acquire(-320, baseline=baseline)
        self.assertAlmostEqual(row["ig_a"], -0.0001 / model.GATE_SHUNT_OHM, places=13)
        self.assertAlmostEqual(row["id_a"], 0.0009975 / model.DRAIN_SHUNT_OHM, places=12)
        self.assertAlmostEqual(row["vgs"], 0.0001, places=12)
        self.assertEqual(baseline.lookups, [(0, 3750000)])

    def test_gate_zero_pwm_anchor_remains_in_use_with_raw_gate_current(self):
        baseline = self.baseline()
        baseline.set_zero(1, 9000, 0, True)
        baseline.finish()
        row = self.acquire(320, baseline=baseline)
        # Requested VGS is +1 V: retain the captured source-relative zero
        # duty and advance it using the nominal gate-driver slope.
        expected = int((9000 / 65535 + 1 / model.GATE_PWM_FULL_SCALE_V) * 65535 + 0.5)
        gate_drives = [event[2] for event in self.fake.events
                       if event[0] == "duty" and event[1] == 21 and event[2]]
        self.assertEqual(gate_drives, [expected])
        self.assertAlmostEqual(row["ig_a"], 0.0001 / model.GATE_SHUNT_OHM, places=13)
        self.assertAlmostEqual(row["vgs"], -0.0001, places=12)
        self.assertEqual(baseline.lookups, [(0, 3750000)])

    def test_high_gate_baseline_metadata_survives_schema5_payload(self):
        baseline = self.baseline(maximum_gate_uv=102399805)
        metadata = baseline.metadata(self.settings)
        row = self.acquire(640, baseline=baseline)
        scan = ram_payload.RamScan(PackedScanBuffer(), self.settings,
                                   "abcd", "abcd-01234567", None, 0,
                                   current_correction=metadata)
        scan.append(row)
        scan.seal(200)
        for _ in range(100):
            if scan.prepare_step():
                break
        else:
            self.fail("Payload preparation did not finish")
        reader = ram_payload.PayloadReader(scan)
        chunks = []
        try:
            while True:
                chunk = reader.read(256)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            reader.close()
        payload = json.loads(b"".join(chunks))
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(payload["current_correction"], metadata)
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["applied_to"], "drain")
        self.assertEqual(payload["current_correction"]["gate"]["max_bus_uv"], 102399805)
        self.assertEqual(payload["acquisition"]["acquisition_mode"], "ina226_ina228_triggered_v1")
        self.assertAlmostEqual(payload["points"][0][3], 0.0002 / model.GATE_SHUNT_OHM, places=12)
        self.assertAlmostEqual(payload["points"][0][1], -0.0002, places=10)


if __name__ == "__main__":
    unittest.main()
