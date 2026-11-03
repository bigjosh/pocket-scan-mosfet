"""Binary calibration integrity, bounded allocation, and atomic replacement."""
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from firmware import calibration_binary as codec, calibration_json, storage
from firmware.calibration_data import PackedPoints


def fixture(count=3, table_type=PackedPoints):
    channels = {}
    for role, shunt in (("drain", 0.78935784123), ("gate", 638.0)):
        points = table_type(count)
        for index in range(count):
            points.append(index * 0.05, -0.0000123 + index * 0.0000001,
                          0.000000234, index * 0.001)
        points.set_zero_bounds(-0.000123456789, 0.000987654321)
        points.set_drive_endpoint(points.value(-1, "terminal_v") - 0.00000123456789,
                                  points.value(-1, "duty") - 0.000000123456789)
        channels[role] = {"shunt_ohm": shunt, "points": points,
                          "capability": {"complete": True, "max_duty": 0.3141592653589793}}
    return {"version": 2, "metadata": {
        "source_voltage_uv": 3300000, "driver_supply_v": 18.8,
        "acquisition_mode": "ina226_triggered_v1", "ina_average_count": 64,
        "bus_conversion_us": 1100, "shunt_conversion_us": 1100,
        "calibration_repeat_blocks": 3,
        "provenance": {"resistance": 0.78935784123, "offset": -0.0,
                       "small": 1.23456789e-30, "reference_ohm": 9980},
        "extras": [True, None, "\u03bcA / \u03a9", {"tag": ["f", "literal"]}]},
        "channels": channels, "operator_note": "Retain unknown root fields"}


def canonical(value):
    out = io.StringIO()
    calibration_json.dump_calibration(value, out)
    return out.getvalue()


def encode(value):
    out = io.BytesIO()
    codec.dump_calibration(value, out)
    return out.getvalue()


def envelope_bytes(value):
    out = io.StringIO()
    codec._typed_dump(value, out, [0])
    return out.getvalue().encode()


def with_metadata(original, metadata):
    _, dcount, gcount, old_size = struct.unpack(codec.HEADER_FORMAT, original[:codec.HEADER_BYTES])
    raw = (struct.pack(codec.HEADER_FORMAT, codec.MAGIC, dcount, gcount, len(metadata)) +
           metadata + original[codec.HEADER_BYTES + old_size:-32])
    return raw + hashlib.sha256(raw).digest()


class CalibrationBinaryTests(unittest.TestCase):
    def test_exact_logical_fingerprint_and_wire_bytes_survive_two_round_trips(self):
        value = fixture()
        before = canonical(value)
        first = encode(value)
        loaded = codec.load_calibration(io.BytesIO(first))
        self.assertEqual(before, canonical(loaded))
        second = encode(loaded)
        self.assertEqual(first, second)
        self.assertEqual(before, canonical(codec.load_calibration(io.BytesIO(second))))
        self.assertEqual(struct.pack("<d", -0.0), struct.pack("<d", loaded["metadata"]["provenance"]["offset"]))

    def test_full_311_point_tables_stream_bounded_without_mapping_snapshots(self):
        class NumericOnly(PackedPoints):
            def __getitem__(self, unused):
                raise AssertionError("No point mapping allocation in binary codec")
        class BoundedSink(io.BytesIO):
            def write(self, data):
                self.assert_bytes(data)
                return super().write(data)
            def assert_bytes(self, data):
                if not isinstance(data, bytes) or len(data) > 256:
                    raise AssertionError("Unbounded or nonbytes write")
        out = BoundedSink()
        codec.dump_calibration(fixture(311, NumericOnly), out)
        loaded = codec.load_calibration(io.BytesIO(out.getvalue()))
        self.assertEqual([311, 311], [len(loaded["channels"][role]["points"]) for role in codec.ROLES])
        self.assertLess(len(out.getvalue()), 13000)

    def test_buffers_are_both_reserved_before_metadata_is_read(self):
        wire = encode(fixture(311))
        events = []
        class ObservedStream(io.BytesIO):
            def read(self, count=-1):
                events.append(("read", count))
                return super().read(count)
        def reserve(count):
            events.append(("reserve", count))
            return PackedPoints(count)
        with patch.object(codec, "PackedPoints", reserve):
            loaded = codec.load_calibration(ObservedStream(wire))
        self.assertEqual([("read", 16), ("reserve", 311), ("reserve", 311)], events[:3])
        self.assertEqual(311, len(loaded["channels"]["gate"]["points"]))

    def test_second_reservation_failure_does_not_read_metadata(self):
        stream = io.BytesIO(encode(fixture()))
        with patch.object(codec, "PackedPoints", side_effect=[PackedPoints(3), MemoryError("reserve")]):
            with self.assertRaises(MemoryError):
                codec.load_calibration(stream)
        self.assertEqual(codec.HEADER_BYTES, stream.tell())

    def test_every_truncation_and_each_corrupt_region_fails(self):
        wire = encode(fixture(2))
        for cut in range(len(wire)):
            with self.subTest(cut=cut), self.assertRaises((ValueError, OSError)):
                codec.load_calibration(io.BytesIO(wire[:cut]))
        for offset in (0, 8, 12, 20, len(wire) - 40, len(wire) - 1):
            damaged = bytearray(wire)
            damaged[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                codec.load_calibration(io.BytesIO(damaged))
        with self.assertRaises(ValueError):
            codec.load_calibration(io.BytesIO(wire + b"extra"))

    def test_invalid_header_bounds_fail_before_allocation(self):
        for drain, gate, size in ((0, 2, 1), (1, 2, 1), (312, 2, 1), (2, 312, 1), (2, 2, 0), (2, 2, 8193)):
            header = struct.pack(codec.HEADER_FORMAT, codec.MAGIC, drain, gate, size)
            with patch.object(codec, "PackedPoints", side_effect=AssertionError("Must not allocate")):
                with self.subTest(drain=drain, gate=gate, size=size), self.assertRaises(ValueError):
                    codec.load_calibration(io.BytesIO(header))

    def test_valid_digest_does_not_allow_invalid_metadata(self):
        wire = encode(fixture())
        envelope, unused = codec._envelope(fixture())
        bad_metadata = [b'{}', b'[' * 50 + b']' * 50,
                        b'["d",[["calibration",0],["calibration",1]]]',
                        b'["f","000000000000f87f"]', b'["f","incorrect"]', b'["bogus",[]]']
        for metadata in bad_metadata:
            with self.subTest(metadata=metadata[:60]), self.assertRaises(ValueError):
                codec.load_calibration(io.BytesIO(with_metadata(wire, metadata)))
        for mutate in (
                lambda item: item.update(extra=True),
                lambda item: item["calibration"].update(version=True),
                lambda item: item["endpoints"]["drain"].update(unknown=0.1),
                lambda item: item["endpoints"]["drain"].pop("zero_max_v"),
                lambda item: item["endpoints"]["drain"].update(zero_min_v=3.0),
                lambda item: item["calibration"]["channels"]["gate"].update(points=[])):
            envelope, unused = codec._envelope(fixture())
            mutate(envelope)
            with self.assertRaises(ValueError):
                codec.load_calibration(io.BytesIO(with_metadata(wire, envelope_bytes(envelope))))

    def test_valid_digest_does_not_allow_invalid_table_values(self):
        original = encode(fixture())
        _, _, _, size = struct.unpack(codec.HEADER_FORMAT, original[:16])
        for relative, number in ((0, float("nan")), (4, float("inf")), (8, -1.0), (12, 2.0), (16, -1.0)):
            raw = bytearray(original[:-32])
            struct.pack_into("<f", raw, 16 + size + relative, number)
            wire = bytes(raw) + hashlib.sha256(raw).digest()
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                codec.load_calibration(io.BytesIO(wire))

    def test_short_write_readinto_and_real_io_failures_propagate(self):
        class ShortWriter:
            def write(self, data):
                return len(data) - 1
        class ShortReader(io.BytesIO):
            def readinto(self, target):
                return super().readinto(target) - 1
        with self.assertRaises(OSError):
            codec.dump_calibration(fixture(), ShortWriter())
        with self.assertRaises(ValueError):
            codec.load_calibration(ShortReader(encode(fixture())))

    def test_big_endian_fallback_matches_little_endian_format(self):
        value = fixture()
        wire = encode(value)
        with patch.object(codec, "_LITTLE_ENDIAN", False):
            self.assertEqual(wire, encode(value))
            self.assertEqual(canonical(value), canonical(codec.load_calibration(io.BytesIO(wire))))

    def test_legacy_maps_and_excessive_metadata_rejected_without_writing(self):
        for mutate in (lambda value: value["channels"]["drain"].update(points=[{}] * 3),
                       lambda value: value["metadata"].update(huge="x" * 8192),
                       lambda value: value.update(version=True)):
            value, stream = fixture(), io.BytesIO()
            mutate(value)
            with self.assertRaises(ValueError):
                codec.dump_calibration(value, stream)
            self.assertEqual(b"", stream.getvalue())

    def test_metadata_writer_accepts_native_bytes_and_utf8_text(self):
        out, digest = io.BytesIO(), hashlib.sha256()
        writer = codec._MetadataWriter(out, digest)
        self.assertEqual(4, writer.write(b"test"))
        self.assertEqual(80, writer.write("\u03a9" * 80))
        self.assertEqual(164, writer.count)
        self.assertEqual(hashlib.sha256(out.getvalue()).digest(), digest.digest())


class BinaryStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / "calibration.bin")

    def test_binary_only_missing_defaults_corruption_fails_and_json_is_untouched(self):
        old_json = Path(self.folder.name) / "calibration.json"
        old_json.write_text('{"historical":true}')
        self.assertEqual("absent", storage.load_calibration("absent", path=self.path))
        Path(self.path).write_bytes(b"bad")
        with self.assertRaises(ValueError):
            storage.load_calibration("absent", path=self.path)
        value = fixture()
        storage.save_calibration_atomic(value, path=self.path)
        self.assertEqual(canonical(value), canonical(storage.load_calibration(path=self.path)))
        self.assertEqual('{"historical":true}', old_json.read_text())
        self.assertEqual({"historical": True}, storage.load_json(str(old_json)))

    def test_preopen_abort_commit_and_stale_tmp_preserve_ownership(self):
        storage.save_calibration_atomic(fixture(), path=self.path)
        before = Path(self.path).read_bytes()
        Path(self.path + ".tmp").write_bytes(b"stale")
        writer = storage.begin_calibration_atomic(path=self.path)
        self.assertEqual(b"", Path(self.path + ".tmp").read_bytes())
        writer.abort()
        writer.abort()
        self.assertEqual(before, Path(self.path).read_bytes())
        self.assertFalse(Path(self.path + ".tmp").exists())
        with self.assertRaises(ValueError):
            writer.commit(fixture())
        writer = storage.begin_calibration_atomic(path=self.path)
        writer.commit(fixture(4))
        self.assertEqual(4, len(storage.load_calibration(path=self.path)["channels"]["drain"]["points"]))
        with self.assertRaises(ValueError):
            writer.commit(fixture())

    def test_flush_close_rename_and_sync_failures_leave_committed_file(self):
        storage.save_calibration_atomic(fixture(), path=self.path)
        before = Path(self.path).read_bytes()
        class FailedStream:
            def __init__(self, real, stage):
                self.real, self.stage = real, stage
            def write(self, data):
                return self.real.write(data)
            def flush(self):
                if self.stage == "flush":
                    raise OSError("flush fault")
                return self.real.flush()
            def close(self):
                self.real.close()
                if self.stage == "close":
                    raise OSError("close fault")
        for stage in ("flush", "close", "rename", "sync"):
            writer = storage.begin_calibration_atomic(path=self.path)
            if stage in ("flush", "close"):
                writer._stream = FailedStream(writer._stream, stage)
                context = patch.object(storage.os, "sync", create=True)
            else:
                target = "replace" if stage == "rename" else "sync"
                context = patch.object(storage.os, target, side_effect=OSError(stage + " fault"), create=True)
            with context, self.subTest(stage=stage), self.assertRaises(OSError):
                writer.commit(fixture(4))
            self.assertEqual(before, Path(self.path).read_bytes())
            self.assertFalse(Path(self.path + ".tmp").exists())

    def test_postrename_sync_failure_keeps_new_valid_commit(self):
        storage.save_calibration_atomic(fixture(), path=self.path)
        with patch.object(storage.os, "sync", side_effect=[None, OSError("after commit")], create=True):
            with self.assertRaises(OSError):
                storage.save_calibration_atomic(fixture(4), path=self.path)
        self.assertEqual(4, len(storage.load_calibration(path=self.path)["channels"]["drain"]["points"]))
        self.assertFalse(Path(self.path + ".tmp").exists())

    def test_permission_and_read_errors_are_not_treated_as_missing(self):
        with patch("builtins.open", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                storage.load_calibration(path=self.path)
        class FaultedStream(io.BytesIO):
            def read(self, unused=-1):
                raise OSError(2, "read failed after open")
        with patch("builtins.open", return_value=FaultedStream()):
            with self.assertRaises(OSError):
                storage.load_calibration(path=self.path)

    def test_codec_import_occurs_before_preopen_and_not_during_commit(self):
        original_import = builtins.__import__
        events = []
        def track_import(name, *args, **kwargs):
            if name == "calibration_binary":
                events.append("import")
                self.assertFalse(Path(self.path + ".tmp").exists())
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=track_import):
            writer = storage.begin_calibration_atomic(path=self.path)
        self.assertEqual(["import"], events)
        with patch("builtins.__import__", side_effect=AssertionError("Late codec import")):
            writer.commit(fixture())

    def test_codec_import_failure_does_not_open_or_change_any_file(self):
        Path(self.path).write_bytes(b"committed")
        original_import = builtins.__import__
        def failed_import(name, *args, **kwargs):
            if name == "calibration_binary":
                raise MemoryError("codec reservation")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=failed_import):
            with self.assertRaises(MemoryError):
                storage.begin_calibration_atomic(path=self.path)
        self.assertEqual(b"committed", Path(self.path).read_bytes())
        self.assertFalse(Path(self.path + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
