"""Schema-3 RAM snapshot identity, exact geometry and bounded replay checks."""

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
    result.update(drain_stop_uv=(count - 1) * 1000, drain_step_uv=1000, gate_stop_uv=0)
    return result


def calibration(config=None):
    config = settings() if config is None else config
    return {"version": 2, "metadata": {"driver_supply_v": 5.0, "pwm_hz": 100000,
                                      "acquisition_mode": model.ACQUISITION_MODE,
                                      "ina_average_count": config["ina_average_count"],
                                      "bus_conversion_us": model.INA_CONVERSION_US,
                                      "shunt_conversion_us": model.INA_CONVERSION_US,
                                      "calibration_repeat_blocks": model.CALIBRATION_REPEAT_BLOCKS,
                                      "label": "Gate \u03bcA / \u03a9", "detail": {"value": "original"}},
            "channels": {"drain": {"points": [{"terminal_v": 0.0, "shunt_v": -1e-6}]}}}


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


def new_scan(config=None, cal=None, **changes):
    config = settings() if config is None else config
    arguments = {"buffer": PackedScanBuffer(), "settings": config,
                 "calibration": calibration(config) if cal is None else cal,
                 "device_id": "abcd", "scan_id": "abcd-0000000000000001",
                 "created_utc": None, "started_ms": 10}
    arguments.update(changes)
    return ram_payload.RamScan(**arguments)


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
    def test_schema3_measured_float32_records_exact_targets_and_repeat_hash(self):
        config = settings(14)
        config.update(drain_stop_uv=1300000, drain_step_uv=100000)
        scan = new_scan(config, created_utc="2026-11-02T20:01:02Z")
        for index in range(14):
            scan.append(point(config, index))
        scan.seal(1010)
        prepare(scan, 31)
        expected = wire(scan)
        value = json.loads(expected)
        self.assertEqual(3, value["schema_version"])
        self.assertEqual(list(ram_payload.COLUMNS), value["columns"])
        self.assertEqual(11, len(value["columns"]))
        self.assertEqual(14, value["point_count"])
        self.assertEqual("complete", value["outcome"])
        self.assertIsNone(value["fault"])
        self.assertEqual(1000, value["duration_ms"])
        self.assertEqual("2026-11-02T20:01:02Z", value["created_utc"])
        self.assertEqual(config, value["settings"])
        self.assertEqual(model.acquisition_metadata(config), value["acquisition"])
        self.assertEqual("unavailable", value["acquisition"]["point_variance"])
        self.assertEqual(1.3, value["points"][-1][2])
        self.assertEqual([False, True, False], value["points"][0][-3:])
        for index, packed in enumerate(value["points"]):
            expected_row = point(config, index)
            for field, actual in zip(("vds", "vgs", "id_a", "ig_a"), packed[4:8]):
                f32 = struct.unpack("<f", struct.pack("<f", expected_row[field]))[0]
                self.assertEqual(float("%.9g" % f32), actual)
        for size in (1, 17, 256, 1024):
            self.assertEqual(expected, wire(scan, size))
        self.assertEqual(hashlib.sha256(expected).hexdigest(), scan.sha256)
        self.assertEqual(len(expected), scan.wire_size)
        self.assertTrue(scan.prepare_step())

    def test_snapshots_detach_inputs_metadata_and_calibration_points(self):
        class Points(list):
            pass
        config, cal = settings(), calibration()
        points = Points(cal["channels"]["drain"]["points"])
        points_ref = weakref.ref(points)
        cal["channels"]["drain"]["points"] = points
        digest = ram_payload.calibration_hash(cal)
        original = copy.deepcopy(config)
        scan = new_scan(config, cal)
        config["gate_stop_uv"] = 1000000
        cal["metadata"]["detail"]["value"] = "changed"
        scan.settings["gate_stop_uv"] = 2000000
        del points, cal
        gc.collect()
        self.assertIsNone(points_ref())
        scan.append(point(original))
        scan.seal(20)
        prepare(scan)
        value = json.loads(wire(scan))
        self.assertEqual(original, value["settings"])
        self.assertEqual("original", value["calibration"]["metadata"]["detail"]["value"])
        self.assertEqual(digest, value["calibration"]["sha256"])
        self.assertIsNone(value["created_utc"])
        for key, replacement in (("scan_id", "ffff"), ("created_utc", "2026-11-02T00:00:00Z"),
                                 ("sha256", "a" * 64), ("wire_size", 1)):
            with self.assertRaises(AttributeError):
                setattr(scan, key, replacement)

    def test_cached_calibration_fingerprint_skips_point_rehash(self):
        with patch.object(ram_payload, "calibration_hash", side_effect=AssertionError("unexpected hash")):
            scan = new_scan(calibration_sha256="a" * 64)
        scan.append(point(settings()))
        scan.seal(20)
        prepare(scan)
        self.assertEqual("a" * 64, json.loads(wire(scan))["calibration"]["sha256"])
        for invalid in ("A" * 64, "a" * 63, True, 123):
            with self.assertRaises(ValueError):
                new_scan(calibration_sha256=invalid)

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

    def test_partial_zero_and_mid_curve_positions_preserve_fault(self):
        config = settings(3)
        config.update(gate_stop_uv=1000000, gate_step_uv=500000)
        for count in (0, 4, 8):
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
        self.assertEqual([-1, -0.5], value["points"][0][2:4])
        self.assertEqual([0, -0.5], value["points"][1][2:4])
        self.assertEqual([-1, 0], value["points"][3][2:4])
        self.assertEqual(diagnostic, value["fault"])

    def test_record_position_averaging_requests_and_values_validate_before_append(self):
        for change in ({"index": 1}, {"curve": 1}, {"index": False}, {"acquisition_blocks_completed": 3},
                       {"acquisition_blocks_completed": 1.0}, {"requested_vgs": 1}, {"requested_vds": None},
                       {"id_a": 2}, {"vds": 37}, {"ig_a": float("nan")},
                       {"gate_voltage_limited": 1}):
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

    def test_legacy_or_mismatched_calibration_cannot_be_relabelled_as_triggered(self):
        for key, replacement in (("acquisition_mode", "continuous"), ("ina_average_count", 4),
                                 ("bus_conversion_us", 588), ("shunt_conversion_us", 588),
                                 ("calibration_repeat_blocks", 1)):
            candidate = calibration()
            candidate["metadata"][key] = replacement
            with self.assertRaises(ValueError):
                new_scan(cal=candidate)
        candidate = calibration()
        del candidate["metadata"]["acquisition_mode"]
        with self.assertRaises(ValueError):
            new_scan(cal=candidate)

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
        cal = calibration()
        cal["metadata"]["label"] = "x" * ram_payload.HEADER_BYTES
        with self.assertRaises(ValueError):
            new_scan(cal=cal)
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
