"""Schema-4 RAM snapshot identity, exact geometry and bounded replay checks."""

import copy
import gc
import hashlib
import json
import struct
import unittest
import weakref
from unittest.mock import patch

from firmware import model, ram_payload
from firmware.ram_scan import PackedScanBuffer


def settings(count=1):
    result = dict(model.DEFAULT_SETTINGS)
    result.update(drain_start_uv=0, drain_stop_uv=(count - 1) * 1000, drain_step_uv=1000,
                  gate_start_uv=0, gate_stop_uv=0, source_voltage_uv=0, driver_supply_uv=5000000)
    return result



def point(config, index=0):
    drains = model.sweep_uv(config["drain_start_uv"], config["drain_stop_uv"], config["drain_step_uv"])
    gates = model.sweep_uv(config["gate_start_uv"], config["gate_stop_uv"], config["gate_step_uv"])
    curve, drain_index = divmod(index, len(drains))
    vds, vgs = model.volts_from_uv(drains[drain_index]), model.volts_from_uv(gates[curve])
    return {"index": index, "curve": curve, "requested_vds": vds, "requested_vgs": vgs,
            "vds": vds + 0.000123456, "vgs": vgs + 0.00123456,
            "id_a": 0.000123456789, "ig_a": -1.23456789e-9,
            "acquisition_blocks_completed": 1, "drain_voltage_limited": False,
            "gate_voltage_limited": True, "discarded_statistics": [1, 2, 3]}


def fault(config, index=0):
    row = point(config, index)
    return {"channel": "gate", "kind": "shunt_saturation", "raw_code": 32767,
            "sign": 1, "limit_value": 0.0819175, "limit_unit": "V", "measured_value": None,
            "index": index, "curve": row["curve"], "requested_vds": row["requested_vds"],
            "requested_vgs": row["requested_vgs"], "phase": "sample"}


def new_scan(config=None, **changes):
    config = settings() if config is None else config
    arguments = {"buffer": PackedScanBuffer(), "settings": config,
                 "device_id": "abcd", "scan_id": "abcd-0000000000000001",
                 "created_utc": None, "started_ms": 10}
    arguments.update(changes)
    return ram_payload.RamScan(**arguments)


def correction_metadata():
    return {"version": 1, "sha256": "a" * 64, "lookup": "linear_endpoint_hold",
            "drain": {"min_bus_uv": 3300000, "max_bus_uv": 16000000,
                      "zero_duty_u16": 42, "zero_residual_uv": 2500, "zero_valid": True},
            "gate": {"min_bus_uv": 1250, "max_bus_uv": 16000000,
                     "zero_duty_u16": 8400, "zero_residual_uv": -1250, "zero_valid": True}}


def prepare(scan, budget=256):
    for unused in range(2 * ram_payload.MAX_WIRE_BYTES // budget + 2):
        before = scan._prepare_size
        if scan.prepare_step(budget):
            return
        assert 0 < scan._prepare_size - before <= budget
    raise AssertionError("Payload preparation did not finish")


def wire(scan, size=1024):
    reader = ram_payload.PayloadReader(scan)
    try:
        parts = []
        while True:
            piece = reader.read(size)
            if not piece:
                break
            assert len(piece) <= size
            parts.append(piece)
        result = b"".join(parts)
        assert reader.size == len(result)
        return result
    finally:
        reader.close()


class RamPayloadTests(unittest.TestCase):
    def test_schema5_uses_actual_binary_baseline_fingerprint_without_retaining_table(self):
        from firmware.current_baseline import Baseline
        config = settings()
        baseline = Baseline()
        baseline.begin(config)
        for role in (0, 1):
            baseline.add_knot(role, 0, 1000, 0)
            baseline.add_knot(role, 5000000, 5000000, 20000)
        baseline.finish()
        metadata = baseline.metadata(config)
        self.assertEqual(metadata["sha256"], hashlib.sha256(baseline._data).hexdigest())
        scan = new_scan(config, current_correction=metadata)
        scan.append(point(config))
        scan.seal(20)
        prepare(scan)
        self.assertEqual(json.loads(wire(scan))["current_correction"], metadata)
        self.assertFalse(any(value is baseline or value is baseline._data for value in vars(scan).values()))

    def test_schema5_snapshots_correction_once_and_preserves_four_measured_values(self):
        config = settings(14)
        correction = correction_metadata()
        expected = copy.deepcopy(correction)
        with patch("builtins.open", side_effect=AssertionError("baseline reread")):
            scan = new_scan(config, current_correction=correction)
            correction["drain"]["zero_duty_u16"] = 9999
            correction["sha256"] = "b" * 64
            for index in range(14):
                scan.append(point(config, index))
            scan.seal(1010)
            prepare(scan, 31)
            body = wire(scan)
            self.assertEqual(body, wire(scan, 17))
        value = json.loads(body)
        self.assertEqual(5, value["schema_version"])
        self.assertEqual(expected, value["current_correction"])
        self.assertEqual(4, len(value["points"][0]))
        self.assertNotIn("calibration", value)
        self.assertEqual(hashlib.sha256(body).hexdigest(), scan.sha256)
        self.assertTrue(all(len(chunk) <= 256 for chunk in scan._header))
        self.assertLess(sum(map(len, scan._header)), 2048)

    def test_schema5_rejects_unbounded_or_malformed_metadata_before_acquisition(self):
        for path, value in (("version", True), ("version", 2), ("sha256", "z" * 64),
                            ("lookup", "extrapolate"), ("gate", None),
                            ("gate.zero_valid", 1), ("gate.zero_duty_u16", 65536),
                            ("gate.zero_residual_uv", float("nan")),
                            ("gate.zero_residual_uv", 42000001),
                            ("drain.min_bus_uv", 17000000), ("drain.max_bus_uv", False)):
            correction = correction_metadata()
            parts = path.split(".")
            target = correction if len(parts) == 1 else correction[parts[0]]
            target[parts[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                new_scan(current_correction=correction)
        correction = correction_metadata()
        correction["table"] = [0] * 4096
        with self.assertRaises(ValueError):
            new_scan(current_correction=correction)

    def test_schema4_measured_float32_records_implicit_grid_and_repeat_hash(self):
        config = settings(14)
        config.update(drain_stop_uv=1300000, drain_step_uv=100000)
        scan = new_scan(config, created_utc="2026-11-02T20:01:02Z")
        for index in range(14):
            scan.append(point(config, index))
        scan.seal(1010)
        prepare(scan, 31)
        expected = wire(scan)
        value = json.loads(expected)
        self.assertEqual(4, value["schema_version"])
        self.assertEqual(list(ram_payload.COLUMNS), value["columns"])
        self.assertEqual(4, len(value["columns"]))
        self.assertEqual(14, value["point_count"])
        self.assertEqual("complete", value["outcome"])
        self.assertIsNone(value["fault"])
        self.assertEqual(1000, value["duration_ms"])
        self.assertEqual("2026-11-02T20:01:02Z", value["created_utc"])
        self.assertEqual(config, value["settings"])
        self.assertEqual(model.acquisition_metadata(config), value["acquisition"])
        self.assertEqual("unavailable", value["acquisition"]["point_variance"])
        self.assertNotIn("calibration", value)
        for index, packed in enumerate(value["points"]):
            expected_row = point(config, index)
            for field, actual in zip(("vds", "vgs", "id_a", "ig_a"), packed):
                f32 = struct.unpack("<f", struct.pack("<f", expected_row[field]))[0]
                self.assertEqual(float("%.9g" % f32), actual)
        for size in (1, 17, 256, 1024):
            self.assertEqual(expected, wire(scan, size))
        self.assertEqual(hashlib.sha256(expected).hexdigest(), scan.sha256)
        self.assertEqual(len(expected), scan.wire_size)
        self.assertTrue(scan.prepare_step())

    def test_snapshot_detaches_settings_and_has_no_calibration_dependency(self):
        config = settings()
        original = dict(config)
        scan = new_scan(config)
        config["gate_stop_uv"] = 1000000
        scan.settings["gate_stop_uv"] = 2000000
        scan.append(point(original))
        scan.seal(20)
        prepare(scan)
        value = json.loads(wire(scan))
        self.assertEqual(original, value["settings"])
        self.assertNotIn("calibration", value)
        self.assertFalse(hasattr(ram_payload, "calibration_hash"))
        self.assertIsNone(value["created_utc"])
        for key in ("scan_id", "created_utc", "sha256", "wire_size"):
            with self.assertRaises(AttributeError):
                setattr(scan, key, "replacement")

    def test_full4096_records_remain_bounded_without_files_or_full_json_dumps(self):
        config = settings(model.MAX_POINTS)
        with patch("builtins.open", side_effect=AssertionError("filesystem access")), \
                patch.object(ram_payload.json, "dumps", side_effect=AssertionError("full JSON allocation")):
            scan = new_scan(config)
            backing = scan.buffer._data
            for index in range(model.MAX_POINTS):
                scan.append(point(config, index))
            scan.seal(2000)
            prepare(scan)
            first = wire(scan, 256)
            self.assertEqual(first, wire(scan, 1024))
        self.assertIs(backing, scan.buffer._data)
        self.assertLess(len(first), ram_payload.MAX_WIRE_BYTES)
        self.assertEqual(4096, len(json.loads(first)["points"]))
        self.assertEqual(hashlib.sha256(first).hexdigest(), scan.sha256)
        self.assertTrue(all(len(chunk) <= 256 for chunk in scan._header + scan._tail))
        self.assertNotIn("archive", ram_payload.__dict__)

    def test_nonempty_partial_mid_curve_positions_preserve_fault(self):
        config = settings(3)
        config.update(gate_stop_uv=1000000, gate_step_uv=500000)
        for count in (1, 4, 8):
            with self.subTest(count=count):
                scan = new_scan(config)
                for index in range(count):
                    scan.append(point(config, index))
                diagnostic = fault(config, count)
                expected = dict(diagnostic)
                scan.seal(100, diagnostic)
                diagnostic["index"] = 999
                scan.fault["index"] = 888
                prepare(scan)
                value = json.loads(wire(scan))
                self.assertEqual("partial", value["outcome"])
                self.assertEqual(count, value["point_count"])
                self.assertEqual(expected, value["fault"])
                self.assertEqual(count, len(value["points"]))

    def test_complete_count_and_partial_positions_are_exact(self):
        config = settings(14)
        config.update(drain_stop_uv=1300000, drain_step_uv=100000)
        scan = new_scan(config)
        for index in range(13):
            scan.append(point(config, index))
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            scan.seal(20)
        self.assertFalse(scan.buffer.frozen)
        for change in ({"index": 12}, {"curve": 1}, {"requested_vds": 1.2},
                       {"kind": "other"}, {"raw_code": True}, {"extra": "private"},
                       {"phase": "elsewhere"}, {"sign": 0}, {"limit_value": float("nan")}):
            diagnostic = fault(config, 13)
            diagnostic.update(change)
            with self.assertRaises(ValueError):
                scan.seal(20, diagnostic)
            self.assertFalse(scan.sealed)
        scan.append(point(config, 13))
        scan.seal(20)
        with self.assertRaises(ValueError):
            scan.seal(30)

    def test_signed_grid_and_current_limit_diagnostic(self):
        config = settings()
        config.update(source_voltage_uv=1000000, drain_start_uv=-1000000,
                      drain_stop_uv=1000000, drain_step_uv=1000000,
                      gate_start_uv=-500000, gate_stop_uv=500000, gate_step_uv=500000)
        scan = new_scan(config)
        for index in range(4):
            scan.append(point(config, index))
        diagnostic = fault(config, 4)
        diagnostic.update(kind="current_limit", raw_code=None, sign=-1,
                          limit_value=0.005, limit_unit="A", measured_value=-0.006)
        scan.seal(30, diagnostic)
        prepare(scan)
        value = json.loads(wire(scan))
        self.assertEqual(-1000000, value["settings"]["drain_start_uv"])
        self.assertEqual(-500000, value["settings"]["gate_start_uv"])
        self.assertEqual(4, len(value["points"][0]))
        self.assertEqual(diagnostic, value["fault"])

    def test_record_position_averaging_requests_and_values_validate_before_append(self):
        for change in ({"index": 1}, {"curve": 1}, {"index": False}, {"acquisition_blocks_completed": 3},
                       {"acquisition_blocks_completed": 1.0}, {"requested_vgs": 1}, {"requested_vds": None},
                       {"ig_a": float("nan")}, {"vds": float("inf")}):
            scan = new_scan()
            row = point(settings())
            row.update(change)
            with self.assertRaises(ValueError):
                scan.append(row)
            self.assertEqual(0, len(scan.buffer))
        scan = new_scan()
        scan.buffer.append(point(settings()))
        with self.assertRaisesRegex(ValueError, "changed"):
            scan.seal(20)

    def test_current_alert_retains_threshold_and_sign_without_invented_peak(self):
        for sign in (-1, 1):
            config = settings(2)
            scan = new_scan(config)
            scan.append(point(config))
            diagnostic = fault(config, 1)
            diagnostic.update(kind="current_limit_alert", raw_code=None, sign=sign,
                              limit_value=0.0001, limit_unit="A", measured_value=None)
            scan.seal(30, diagnostic)
            prepare(scan)
            self.assertEqual(diagnostic, json.loads(wire(scan))["fault"])
            for key, invalid in (("measured_value", 0), ("raw_code", 0), ("sign", 0),
                                 ("limit_unit", "V"), ("limit_value", 0)):
                candidate = new_scan(config)
                candidate.append(point(config))
                bad = dict(diagnostic)
                bad[key] = invalid
                with self.assertRaises(ValueError):
                    candidate.seal(30, bad)

    def test_zero_points_cannot_upload_even_with_a_hardware_fault(self):
        config = settings(2)
        scan = new_scan(config)
        with self.assertRaisesRegex(ValueError, "at least one"):
            scan.seal(30, fault(config))
        self.assertFalse(scan.buffer.frozen)

    def test_finite_out_of_target_or_large_values_are_recorded_unchanged(self):
        scan = new_scan()
        row = point(settings())
        row.update(vds=37, vgs=-39, id_a=2, ig_a=-3)
        scan.append(row)
        scan.seal(20)
        prepare(scan)
        self.assertEqual([[37, -39, 2, -3]], json.loads(wire(scan))["points"])

    def test_extreme_finite_float32_encoding_remains_finite_on_wire(self):
        scan = new_scan()
        row = point(settings())
        maximum = struct.unpack('<f', bytes.fromhex('ffff7f7f'))[0]
        row.update(vds=maximum, vgs=-maximum, id_a=maximum, ig_a=-maximum)
        scan.append(row)
        scan.seal(20)
        prepare(scan)
        expected = float('%.9g' % maximum)
        self.assertEqual([[expected, -expected, expected, -expected]], json.loads(wire(scan))["points"])

    def test_freeze_close_and_generation_checks_prevent_changed_replay(self):
        scan = new_scan()
        scan.append(point(settings()))
        with self.assertRaises(ValueError):
            ram_payload.PayloadReader(scan)
        scan.seal(20)
        with self.assertRaises(ValueError):
            ram_payload.PayloadReader(scan)
        prepare(scan)
        reader = ram_payload.PayloadReader(scan)
        reader.read(7)
        for action in (lambda: scan.buffer.append(point(settings())), scan.buffer.clear,
                       lambda: scan.append(point(settings()))):
            with self.assertRaises(ValueError):
                action()
        reader.close()
        self.assertEqual(1, len(scan.buffer))
        self.assertTrue(scan.buffer.frozen)
        with self.assertRaises(ValueError):
            reader.read(7)
        reader = ram_payload.PayloadReader(scan)
        scan.buffer.unfreeze()
        scan.buffer.freeze()
        with self.assertRaisesRegex(ValueError, "changed"):
            reader.read(7)
        with self.assertRaises(ValueError):
            scan.prepare_step()
        reader.close()

    def test_prepare_failure_restarts_hash_pass_without_losing_measurements(self):
        scan = new_scan()
        scan.append(point(settings()))
        scan.seal(20)
        self.assertFalse(scan.prepare_step(17))
        with patch.object(ram_payload.PayloadReader, "read", side_effect=MemoryError("temporary")):
            with self.assertRaises(MemoryError):
                scan.prepare_step(17)
        self.assertIsNone(scan.sha256)
        self.assertIsNone(scan.wire_size)
        self.assertIsNone(scan._prepare_reader)
        self.assertEqual(1, len(scan.buffer))
        prepare(scan)
        self.assertEqual(hashlib.sha256(wire(scan)).hexdigest(), scan.sha256)

    def test_bad_limits_identifiers_time_and_header_size_fail_without_freezing(self):
        for changes in ({"device_id": "not valid"}, {"scan_id": "x" * 97}, {"started_ms": True},
                        {"created_utc": "bad"}, {"buffer": PackedScanBuffer(capacity=1), "settings": settings(2)}):
            with self.assertRaises(ValueError):
                new_scan(**changes)
        scan = new_scan()
        scan.append(point(settings()))
        for now in (9, 0x10000000a, True):
            with self.assertRaises(ValueError):
                scan.seal(now)
            self.assertFalse(scan.buffer.frozen)
        scan.seal(20)
        for budget in (0, 257, True, 1.0):
            with self.assertRaises(ValueError):
                scan.prepare_step(budget)
        prepare(scan)
        reader = ram_payload.PayloadReader(scan)
        for size in (0, 1025, True, 1.0):
            with self.assertRaises(ValueError):
                reader.read(size)
        reader.close()


if __name__ == "__main__":
    unittest.main()
