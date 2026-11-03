"""Bounded packed-table JSON saves and fingerprints, without hardware."""
import gc
import hashlib
import io
import json
import unittest
import weakref
from unittest.mock import patch

from firmware import calibration_json, ram_payload
from firmware.calibration_data import PackedPoints


def points(count=3, cls=PackedPoints):
    table = cls(max(1, count))
    for index in range(count):
        table.append(index * 0.05, -0.00001 + index * 0.0000001,
                     0.0000002 + index * 0.00000001, index * 0.001)
    if count:
        table.set_zero_bounds(-0.001, 0.001)
    if count > 1:
        table.set_drive_endpoint((count - 1) * 0.05 - 0.0001,
                                 (count - 1) * 0.001 - 0.0001)
    return table


def calibration(table):
    return {"version": 2, "metadata": {
        "driver_supply_uv": 18800000, "source_voltage_uv": 3300000,
        "batches": 10, "samples_per_batch": 50,
        "label": "Gate \u03bcA / \u03a9", "nested": {"owner": [True, None, "note"]},
        "drain_shunt_calibration": {"effective_shunt_ohm": 0.789, "reference_ohm": 9980}},
        "channels": {"drain": {"shunt_ohm": 0.789, "points": table,
                    "capability": {"complete": True, "max_duty": 0.5},
                    "annotation": "Retain this"},
                     "gate": {"shunt_ohm": 638.0, "points": [
                         {"terminal_v": 0.0, "shunt_v": 0.0012, "note": "legacy"}]}},
        "operator_note": ["Do not drop extras", 42]}


def serialize(value):
    output = io.StringIO()
    calibration_json.dump_calibration(value, output)
    return output.getvalue()


class CalibrationJsonTests(unittest.TestCase):
    def test_packed_table_keeps_schema_optional_fields_metadata_and_other_channels(self):
        table = points()
        value = calibration(table)
        expected = calibration([table[index] for index in range(len(table))])
        actual = json.loads(serialize(value))
        self.assertEqual(expected, actual)
        self.assertEqual(2, actual["version"])
        rows = actual["channels"]["drain"]["points"]
        self.assertIn("zero_min_v", rows[0])
        self.assertNotIn("zero_min_v", rows[1])
        self.assertIn("drive_duty", rows[-1])
        self.assertNotIn("drive_duty", rows[0])

    def test_legacy_json_bytes_and_fingerprint_are_unchanged(self):
        table = points()
        value = calibration([table[index] for index in range(len(table))])
        for version in (1, 2):
            value["version"] = version
            expected = json.dumps(value)
            self.assertEqual(expected, serialize(value))
            self.assertEqual(hashlib.sha256(expected.encode()).hexdigest(),
                             ram_payload.calibration_hash(value))

    def test_file_writer_and_hash_use_identical_streamed_packed_json(self):
        value = calibration(points(199))
        wire = serialize(value).encode("utf-8")
        self.assertEqual(hashlib.sha256(wire).hexdigest(), ram_payload.calibration_hash(value))
        self.assertEqual(wire, serialize(value).encode("utf-8"))

    def test_no_table_conversion_and_only_one_point_dictionary_is_in_flight(self):
        class TrackedPoint(dict):
            pass

        class TrackedPoints(PackedPoints):
            def __init__(self, capacity):
                super().__init__(capacity)
                self.previous = None
                self.reads = 0

            def __iter__(self):
                raise AssertionError("Writer must index, never expand the table")

            def __getitem__(self, index):
                if not isinstance(index, int):
                    raise AssertionError("Writer must not slice the table")
                gc.collect()
                if self.previous is not None:
                    if self.previous() is not None:
                        raise AssertionError("Previous point dictionary is still retained")
                point = TrackedPoint(super().__getitem__(index))
                self.previous = weakref.ref(point)
                self.reads += 1
                return point

        table = points(311, TrackedPoints)
        value = calibration(table)
        real_dump = json.dump
        def checked_dump(item, stream):
            self.assertIsNot(item, table)
            self.assertNotIsInstance(item, TrackedPoint)
            real_dump(item, stream)

        with patch.object(calibration_json.json, "dump", checked_dump):
            output = serialize(value)
        self.assertEqual(311, table.reads)
        self.assertEqual(311, len(json.loads(output)["channels"]["drain"]["points"]))

    def test_packed_json_is_independent_of_all_dictionary_insertion_orders(self):
        def reversed_objects(value):
            if isinstance(value, dict):
                return {key: reversed_objects(value[key]) for key in reversed(list(value))}
            if isinstance(value, list):
                return [reversed_objects(item) for item in value]
            return value

        value = calibration(points())
        reordered = reversed_objects(value)
        self.assertEqual(serialize(value), serialize(reordered))
        self.assertEqual(ram_payload.calibration_hash(value), ram_payload.calibration_hash(reordered))

    def test_packed_file_hash_survives_bounded_loader_round_trip(self):
        from prototype.calibration_json_legacy import load_calibration

        value = calibration(points(199))
        value["channels"]["gate"]["points"] = points(199)
        original = serialize(value)
        loaded = load_calibration(io.StringIO(original))
        self.assertIsInstance(loaded["channels"]["drain"]["points"], PackedPoints)
        self.assertEqual(original, serialize(loaded))
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(),
                         ram_payload.calibration_hash(loaded))

    def test_stream_failure_propagates_without_mutating_input(self):
        table = points()
        value = calibration(table)
        before = serialize(value)

        class FailedStream:
            def write(self, unused):
                raise OSError("Synthetic full flash")

        with self.assertRaisesRegex(OSError, "Synthetic full flash"):
            calibration_json.dump_calibration(value, FailedStream())
        self.assertEqual(before, serialize(value))

    def test_two_packed_channels_and_empty_table_have_valid_json(self):
        value = calibration(points(0))
        value["channels"]["gate"]["points"] = points()
        actual = json.loads(serialize(value))
        self.assertEqual([], actual["channels"]["drain"]["points"])
        self.assertEqual(3, len(actual["channels"]["gate"]["points"]))

    def test_invalid_root_is_rejected_before_writing(self):
        output = io.StringIO()
        with self.assertRaises(ValueError):
            calibration_json.dump_calibration([], output)
        self.assertEqual("", output.getvalue())


if __name__ == "__main__":
    unittest.main()
