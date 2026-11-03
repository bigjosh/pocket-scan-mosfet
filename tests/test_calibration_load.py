"""Bounded compatibility reader: no full calibration point-list JSON tree."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from firmware import model, storage
from prototype import calibration_json_legacy as calibration_load
from firmware.calibration_data import PackedPoints, MAX_CALIBRATION_POINTS
from test_model_engine import calibration, discovered_calibration


class ChunkedStream(io.StringIO):
    def __init__(self, text, chunk_limit=128):
        super().__init__(text)
        self.chunk_limit = chunk_limit
        self.requests = []

    def read(self, size=-1):
        if size < 0 or size > 128:
            raise AssertionError("Unbounded calibration read")
        self.requests.append(size)
        return super().read(min(size, self.chunk_limit))


def load(value, chunk_limit=128):
    text = value if isinstance(value, str) else json.dumps(value)
    return calibration_load.load_calibration(ChunkedStream(text, chunk_limit))


class CalibrationLoadTests(unittest.TestCase):
    def test_both_numeric_tables_are_reserved_before_first_read_and_reused(self):
        allocated, events = [], []
        def reserve(capacity):
            self.assertNotIn("read", events)
            events.append("reserve")
            result = PackedPoints(capacity)
            allocated.append(result)
            return result
        class Stream(ChunkedStream):
            def read(self, size=-1):
                self_outer.assertEqual(len(allocated), 2)
                events.append("read")
                return super().read(size)
        self_outer = self
        stream = Stream(json.dumps(calibration()))
        with patch.object(calibration_load, "PackedPoints", side_effect=reserve):
            value = calibration_load.load_calibration(stream)
        self.assertEqual(events[:2], ["reserve", "reserve"])
        self.assertIs(value["channels"]["drain"]["points"], allocated[0])
        self.assertIs(value["channels"]["gate"]["points"], allocated[1])

    def test_second_table_allocation_failure_happens_before_file_read(self):
        stream = ChunkedStream(json.dumps(calibration()))
        with patch.object(calibration_load, "PackedPoints",
                          side_effect=[PackedPoints(MAX_CALIBRATION_POINTS), MemoryError("second table")]):
            with self.assertRaisesRegex(MemoryError, "second table"):
                calibration_load.load_calibration(stream)
        self.assertEqual(stream.requests, [])

    def test_legacy_single_line_maps_load_as_packed_without_json_point_lists(self):
        saved = calibration()
        decoded = []
        original = json.loads
        def loads(text):
            value = original(text)
            decoded.append(value)
            self.assertNotIn('"points":', text)
            return value
        with patch.object(calibration_load.json, "loads", side_effect=loads):
            value = load(saved, chunk_limit=1)
        model.validate_calibration(value)
        self.assertFalse(any(isinstance(item, list) for item in decoded))
        for role in ("drain", "gate"):
            points = value["channels"][role]["points"]
            self.assertIsInstance(points, PackedPoints)
            self.assertEqual(len(points), 3)
            self.assertEqual(points.capacity, MAX_CALIBRATION_POINTS)
            self.assertEqual(points.value(1, "shunt_std_v"), 0.0)
            for index, point in enumerate(saved["channels"][role]["points"]):
                for key, number in point.items():
                    self.assertAlmostEqual(points.value(index, key), number, places=7)

    def test_reordered_keys_whitespace_escaped_metadata_and_endpoints(self):
        saved = calibration()
        saved["metadata"]["description"] = 'escaped "quote" \\ and } ] unicode μ'
        for channel in saved["channels"].values():
            points = channel["points"]
            points[0].update(zero_min_v=0, zero_max_v=0.01)
            points[-1].update(drive_v=2.99, drive_duty=0.1)
            channel["points"] = [dict(reversed(list(point.items()))) for point in points]
        saved = dict(reversed(list(saved.items())))
        for chunk in (1, 2, 7, 127, 128):
            value = load(json.dumps(saved, indent=2), chunk)
            self.assertEqual(value["metadata"], saved["metadata"])
            self.assertEqual(value["channels"]["drain"]["points"][0]["zero_max_v"], 0.01)
            self.assertEqual(value["channels"]["drain"]["points"][-1]["drive_v"], 2.99)

    def test_full_capacity_has_only_packed_retained_point_storage(self):
        saved = calibration()
        for channel in saved["channels"].values():
            channel["points"] = [{"terminal_v": index * 0.05, "shunt_v": index * 1e-8,
                                  "shunt_std_v": 1e-7, "duty": index / 400}
                                 for index in range(MAX_CALIBRATION_POINTS)]
        value = load(saved)
        model.validate_calibration(value)
        for channel in value["channels"].values():
            self.assertEqual(len(channel["points"]), MAX_CALIBRATION_POINTS)
            self.assertEqual(len(channel["points"]._values), MAX_CALIBRATION_POINTS * 4)

    def test_float64_endpoint_support_only_expands_for_float32_rounding(self):
        saved = discovered_calibration(3.3)
        for channel in saved["channels"].values():
            channel["points"][0].update(terminal_v=0.0033, zero_min_v=0.0033, zero_max_v=0.0033)
            channel["points"][-1]["terminal_v"] = 3.3
            channel["capability"].update(endpoint_min_v=3.3, endpoint_max_v=3.3)
        value = load(saved)
        model.validate_calibration(value)
        for channel in value["channels"].values():
            first, last = channel["points"][0], channel["points"][-1]
            self.assertLessEqual(first["zero_min_v"], first["terminal_v"])
            self.assertGreaterEqual(first["zero_max_v"], first["terminal_v"])
            self.assertLessEqual(channel["capability"]["endpoint_min_v"], last["terminal_v"])
            self.assertGreaterEqual(channel["capability"]["endpoint_max_v"], last["terminal_v"])
            self.assertLess(first["zero_max_v"] - first["zero_min_v"], 1e-8)
            self.assertLess(channel["capability"]["endpoint_max_v"] - channel["capability"]["endpoint_min_v"], 1e-6)
        for case in ("zero", "endpoint"):
            broken = json.loads(json.dumps(saved))
            channel = broken["channels"]["drain"]
            if case == "zero":
                channel["points"][0]["zero_min_v"] += 1e-10
            else:
                channel["capability"]["endpoint_max_v"] -= 1e-10
            with self.subTest(case=case), self.assertRaises(ValueError):
                load(broken)

    def test_float64_inverse_endpoint_keeps_its_offset_and_valid_duty_bound(self):
        saved = discovered_calibration(3.2)
        for channel in saved["channels"].values():
            last = channel["points"][-1]
            last["duty"] = 0.12
            last.update(drive_v=3.195, drive_duty=0.12)
        value = load(saved)
        model.validate_calibration(value)
        for channel in value["channels"].values():
            last = channel["points"][-1]
            self.assertAlmostEqual(last["terminal_v"] - last["drive_v"], 0.005)
            self.assertLessEqual(last["drive_duty"], last["duty"])
        saved["channels"]["drain"]["points"][-1]["drive_duty"] += 1e-10
        with self.assertRaisesRegex(ValueError, "endpoint duty"):
            load(saved)

    def test_truncation_bad_separators_and_trailing_data_are_rejected(self):
        text = json.dumps(calibration())
        for end in (0, 1, 5, len(text)//2, len(text)-2, len(text)-1):
            with self.subTest(end=end), self.assertRaises(ValueError):
                load(text[:end])
        for text in ('{"version":1,}', '{"channels":{"drain":{"points":[{},]}}}',
                     json.dumps(calibration()) + " false", '{"metadata":"unterminated}'):
            with self.subTest(text=text[-50:]), self.assertRaises(ValueError):
                load(text)

    def test_duplicate_critical_fields_are_rejected_at_every_structural_level(self):
        texts = ('{"version":1,"version":2}',
                 '{"channels":{},"channels":{}}',
                 '{"channels":{"drain":{},"drain":{}}}',
                 '{"channels":{"drain":{"points":[],"points":[]}}}',
                 '{"channels":{"drain":{"points":[{"terminal_v":0,"terminal_v":1}]}}}')
        for text in texts:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "Duplicate"):
                load(text)

    def test_invalid_point_fields_numbers_and_optional_positions_are_rejected(self):
        changes = (
            lambda points: points[0].update(terminal_v=True),
            lambda points: points[0].update(shunt_v=float("nan")),
            lambda points: points[0].update(duty=float("inf")),
            lambda points: points[0].update(duty=10 ** 400),
            lambda points: points[0].update(duty=1e300),
            lambda points: points[0].update(extra=1),
            lambda points: points[0].pop("duty"),
            lambda points: points[0].update(zero_min_v=0),
            lambda points: points[1].update(zero_min_v=0, zero_max_v=1),
            lambda points: points[0].update(drive_v=0, drive_duty=0),
            lambda points: points[-1].update(drive_v=3),
        )
        for change in changes:
            saved = calibration()
            change(saved["channels"]["drain"]["points"])
            with self.subTest(change=change), self.assertRaises(ValueError):
                load(saved)

    def test_size_count_and_nesting_bounds_fail_without_unbounded_reads(self):
        cases = []
        saved = calibration()
        saved["metadata"] = {"too_big": "x" * calibration_load.MAX_VALUE_CHARS}
        cases.append(saved)
        saved = calibration()
        saved["metadata"] = {"a": "x" * 6000}
        saved["extension1"] = "x" * 6000
        saved["extension2"] = "x" * 6000
        cases.append(saved)
        saved = calibration()
        saved["channels"]["drain"]["points"] *= 104
        cases.append(saved)
        cases.append('{"metadata":' + '[' * 40 + '0' + ']' * 40 + '}')
        cases.append('{"channels":{"drain":{"points":[{' + ' ' * 513 + '"duty":0}]}}}')
        for value in cases:
            with self.subTest(size=len(str(value))), self.assertRaises(ValueError):
                load(value)

    def test_generic_json_loader_does_not_dispatch_to_migration_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calibration.json"
            path.write_text(json.dumps(calibration()), encoding="utf-8")
            value = storage.load_json(str(path))
            self.assertIsInstance(value["channels"]["gate"]["points"], list)
            other = Path(folder) / "settings.json"
            other.write_text(json.dumps(calibration()), encoding="utf-8")
            self.assertIsInstance(storage.load_json(str(other))["channels"]["gate"]["points"], list)
            self.assertEqual(storage.load_json(str(Path(folder) / "missing" / "calibration.json"), "absent"), "absent")


if __name__ == "__main__":
    unittest.main()
