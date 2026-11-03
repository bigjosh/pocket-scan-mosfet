"""Binary archive transaction, integrity, streaming and capacity checks."""

import hashlib
import copy
import json
import io
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from firmware import archive, model


def settings(count=1):
    # Explicit historical settings: these rows contain measured software-batch
    # variances that today's single hardware block does not provide.
    return {"version": 2, "drain_start_uv": 0,
            "drain_stop_uv": (count - 1) * 1000, "drain_step_uv": 1000,
            "gate_start_uv": 0, "gate_stop_uv": 0, "gate_step_uv": 500000,
            "driver_supply_uv": 5000000, "source_voltage_uv": 0,
            "batches": 10, "samples_per_batch": 50,
            "sample_pause_ms": 4, "settle_ms": 200}


def legacy_settings(count=1):
    # Keep historical snapshots independent of today's active defaults.
    return {"version": 1, "drain_start_v": 0.0,
            "drain_stop_v": (count - 1) * 0.001, "drain_step_v": 0.001,
            "gate_start_v": 0.0, "gate_stop_v": 0.0, "gate_step_v": 0.5,
            "driver_supply_v": 5.0, "source_voltage_v": 0.0,
            "batches": 10, "samples_per_batch": 50,
            "sample_pause_ms": 4, "settle_ms": 200}


def row(index=0):
    value = {name: 0.0 for name in archive.COLUMNS}
    value.update(index=index, curve=0, requested_vds=index * 0.001,
                 requested_vgs=0.0, vds=index * 0.001 + 0.000123456,
                 vgs=0.00123456, id_a=0.000190123456, ig_a=-1.23456789e-8,
                 id_std_a=1.23e-7, ig_std_a=2.45e-10,
                 drain_shunt_v=0.00138, gate_shunt_v=0.00456789,
                 drain_shunt_std_v=2.5e-6, gate_shunt_std_v=1.75e-6,
                 batches_completed=10, drain_voltage_limited=False,
                 gate_voltage_limited=True)
    return value


def fault(index=0):
    return {"channel": "gate", "kind": "shunt_saturation", "raw_code": 32767,
            "sign": 1, "limit_value": 0.0819175, "limit_unit": "V", "measured_value": None,
            "index": index, "curve": 0, "requested_vds": index * 0.001,
            "requested_vgs": 0.0, "phase": "sample"}


CALIBRATION = {"version": 2, "metadata": {"driver_supply_v": 5.0, "pwm_hz": 100000},
               "channels": {"placeholder": {"points": [{"terminal_v": 0.0}]}}}


def open_bytes(path):
    with open(path, "rb") as stream:
        return stream.read()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.counter = 0
        self.free = 4 * 1024 * 1024
        self.folder = os.path.join(self.temp.name, "scans")
        self.archive = archive.Archive(self.folder, "abcd", random_bytes=self.random,
                                       free_bytes=lambda: self.free)

    def random(self, count):
        self.counter += 1
        return self.counter.to_bytes(count, "big")

    def complete(self, count=1, accept=True):
        scan_id = self.archive.begin_scan(settings(count), CALIBRATION, 10)
        for index in range(count):
            self.archive.record_point(row(index))
        info = self.archive.finish_scan(1010)
        if accept:
            self.assertEqual(scan_id, self.archive.accept_scan())
        return info

    def wire(self, path, chunk=1024):
        source = archive.UploadSource(path)
        try:
            pieces = []
            while True:
                part = source.read(chunk)
                if not part:
                    break
                self.assertLessEqual(len(part), chunk)
                pieces.append(part)
            payload = b"".join(pieces)
            self.assertEqual(source.size, len(payload))
            return payload
        finally:
            source.close()

    def test_only_accept_queues_complete_scan(self):
        self.archive.begin_scan(settings(2), CALIBRATION, 0)
        self.archive.record_point(row())
        self.assertEqual(0, self.archive.pending_count)
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            self.archive.finish_scan(10)
        with self.assertRaisesRegex(RuntimeError, "completed"):
            self.archive.accept_scan()
        self.archive.record_point(row(1))
        self.archive.finish_scan(20)
        self.assertEqual(0, self.archive.pending_count)
        scan_id = self.archive.accept_scan()
        self.assertEqual(1, self.archive.pending_count)
        self.assertEqual(scan_id, self.archive.accept_scan())

    def test_partial_zero_or_completed_points_requires_explicit_accept_and_replays(self):
        for count in (0, 2):
            with self.subTest(count=count):
                scan_id = self.archive.begin_scan(settings(4), CALIBRATION, 10)
                for index in range(count):
                    self.archive.record_point(row(index))
                info = self.archive.finish_scan(100, fault(count))
                self.assertEqual(0, self.archive.pending_count)
                self.archive.accept_scan()
                entry = self.archive.next_pending()
                payload = self.wire(entry["path"], 13)
                value = json.loads(payload)
                self.assertEqual(2, value["schema_version"])
                self.assertEqual("partial", value["outcome"])
                self.assertEqual(fault(count), value["fault"])
                self.assertEqual(count, value["point_count"])
                self.assertEqual(count, len(value["points"]))
                self.assertEqual(info["sha256"], hashlib.sha256(payload).hexdigest())
                reopened = archive.Archive(self.folder, "abcd")
                self.assertEqual(payload, self.wire(reopened.next_pending()["path"], 1024))
                self.assertEqual("partial", reopened.list_scans()[0]["outcome"])
                self.assertFalse(reopened.list_scans()[0]["damaged"])
                reopened.delete_scan(scan_id)

    def test_partial_cancel_never_queues_or_reclaims_accepted_data(self):
        prior = self.complete()["scan_id"]
        before = open_bytes(self.archive._path(prior, ".pending"))
        self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.archive.finish_scan(20, fault())
        self.archive.discard_scan()
        self.assertEqual(1, self.archive.pending_count)
        self.assertEqual(before, open_bytes(self.archive._path(prior, ".pending")))
        self.assertEqual([], self.archive._names(".draft"))

    def test_partial_fault_validation_rejects_unbounded_or_inconsistent_metadata(self):
        for change in ({"extra": "untrusted"}, {"index": 1}, {"curve": 1},
                       {"requested_vgs": 1}, {"requested_vds": float("nan")},
                       {"raw_code": True}, {"sign": 0}, {"phase": "other"},
                       {"kind": "current_limit"}, {"measured_value": 0.001},
                       {"raw_code": 32766}, {"sign": -1}, {"limit_value": 0.02},
                       {"limit_unit": "A"}):
            self.archive.begin_scan(settings(), CALIBRATION, 0)
            value = fault()
            value.update(change)
            with self.assertRaisesRegex(RuntimeError, "finalized"):
                self.archive.finish_scan(10, value)
            with self.assertRaises(RuntimeError):
                self.archive.accept_scan()
            self.archive.discard_scan()
        self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.archive.record_point(row())
        with self.assertRaises(RuntimeError):
            self.archive.finish_scan(10, fault(1))

    def test_current_limit_fault_preserves_signed_raw_current_separately(self):
        self.archive.begin_scan(settings(), CALIBRATION, 0)
        value = fault()
        value.update(kind="current_limit", raw_code=None, limit_value=0.0001,
                     limit_unit="A", measured_value=-0.00011, sign=-1)
        self.archive.finish_scan(10, value)
        self.archive.accept_scan()
        result = json.loads(self.wire(self.archive.next_pending()["path"]))
        self.assertEqual(value, result["fault"])
        self.assertEqual([], result["points"])

    def test_v1_file_retains_original_wire_bytes_and_sidecar_hash(self):
        # Independent construction of the old format, including its exact JSON
        # suffix. Future format changes must never rewrite queued v1 payloads.
        scan_id = "abcd-0000000000000001"
        header = json.dumps({"schema_version": 1, "scan_id": scan_id,
                             "device_id": "abcd", "created_utc": None,
                             "settings": legacy_settings(), "calibration": {"sha256": "a" * 64, "metadata": {}}}).encode()
        values = row()
        record = struct.pack(archive.RECORD_FORMAT, 0, 0,
                             *[values[key] for key in archive.COLUMNS[2:14]], 10, 2)
        binary = struct.pack(archive.PREFIX_FORMAT, archive.MAGIC_V1, len(header)) + header + record + struct.pack("<II", 1, 1000)
        unpacked = struct.unpack(archive.RECORD_FORMAT, record)
        point = "[0,0," + ",".join("%.9g" % value for value in unpacked[2:14]) + ",10,false,true]"
        payload = (header[:-1] + b',"columns":' + archive._COLUMNS_JSON + b',"points":[' + point.encode() +
                   b'],"outcome":"complete","point_count":1,"duration_ms":1000}')
        info = {"format_version": 1, "scan_id": scan_id, "point_count": 1,
                "sha256": hashlib.sha256(payload).hexdigest(), "wire_size": len(payload),
                "binary_sha256": hashlib.sha256(binary).hexdigest()}
        info["metadata_sha256"] = archive._metadata_hash(info)
        path = self.archive._path(scan_id, ".pending")
        with open(path, "wb") as stream:
            stream.write(binary)
        with open(self.archive._path(scan_id, ".meta"), "w") as stream:
            json.dump(info, stream)
        self.assertEqual(payload, self.wire(path, 1))
        self.assertEqual(info["sha256"], self.archive.next_pending()["sha256"])
        self.assertEqual("complete", self.archive.list_scans()[0]["outcome"])
        self.archive.mark_sent(scan_id)
        self.assertEqual(payload, self.wire(self.archive._path(scan_id, ".sent")))

    def test_reader_does_not_recompute_exact_cross_runtime_sweep_endpoint_count(self):
        import io
        value = legacy_settings()
        value.update(drain_stop_v=0.3, drain_step_v=0.1)
        # A float32 runtime can omit the nominal .3 endpoint while a float64
        # reader derives four points. Saved count is finalization's authority.
        for version in (1, 2):
            trailer = b'{"outcome":"complete","fault":null}' if version == 2 else b""
            footer = struct.pack("<III", 3, 10, len(trailer)) if version == 2 else struct.pack("<II", 3, 10)
            content = b"\0" * (3 * archive.RECORD_BYTES) + trailer + footer
            result = archive._read_tail(io.BytesIO(content), len(content), 0, version, value)
            self.assertEqual(3, result[0])
            self.assertEqual("complete", result[4])

    def test_new_snapshot_uses_integer_microvolts_even_for_legacy_active_settings(self):
        previous = legacy_settings(3)
        before = dict(previous)
        self.archive.begin_scan(previous, CALIBRATION, 0)
        for index in range(3):
            self.archive.record_point(row(index))
        self.archive.finish_scan(10)
        self.archive.accept_scan()
        value = json.loads(self.wire(self.archive.next_pending()["path"]))
        self.assertEqual(before, previous)
        self.assertEqual(settings(3), value["settings"])
        self.assertEqual(2, value["settings"]["version"])
        self.assertEqual(8, sum(key.endswith("_uv") for key in value["settings"]))
        self.assertTrue(all(type(item) is int for key, item in value["settings"].items()
                            if key.endswith("_uv")))
        self.assertFalse(any(key.endswith("_v") for key in value["settings"]))

    def test_historical_writer_rejects_triggered_settings_before_creating_data(self):
        with self.assertRaisesRegex(RuntimeError, "could not be started"):
            self.archive.begin_scan(dict(model.DEFAULT_SETTINGS), CALIBRATION, 0)
        self.assertEqual([], os.listdir(self.folder))
        self.assertIsNone(self.archive._stream)
        self.assertIsNone(self.archive._current)

    def test_integer_snapshot_rejects_a_missing_exact_endpoint(self):
        value = settings()
        value.update(drain_stop_uv=300000, drain_step_uv=100000)
        trailer = b'{"outcome":"complete","fault":null}'
        footer = struct.pack("<III", 3, 10, len(trailer))
        content = b"\0" * (3 * archive.RECORD_BYTES) + trailer + footer
        with self.assertRaisesRegex(ValueError, "Incomplete scan"):
            archive._read_tail(io.BytesIO(content), len(content), 0, 2, value)

    def test_legacy_boundary_and_submicrovolt_archives_keep_original_wire_and_hash(self):
        # Both binary formats used v1 float settings. A Pico accepted this
        # 4092-point grid although exact decimal geometry would have 4158.
        boundary = legacy_settings()
        boundary.update(drain_stop_v=1.55, drain_step_v=0.025,
                        gate_stop_v=1.625, gate_step_v=0.025)
        tiny = legacy_settings()
        tiny.update(drain_stop_v=0.0000002, drain_step_v=0.0000001)
        for version in (1, 2):
            for snapshot, count, width in ((boundary, 4092, 62), (tiny, 3, 3)):
                with self.subTest(binary_version=version, count=count):
                    scan_id = "abcd-%016x" % (version * 10000 + count)
                    header = json.dumps({"schema_version": version, "scan_id": scan_id,
                                         "device_id": "abcd", "created_utc": None,
                                         "settings": snapshot,
                                         "calibration": {"sha256": "a" * 64, "metadata": {}}}).encode()
                    records, points = [], []
                    for index in range(count):
                        point = row(index)
                        point.update(curve=index // width,
                                     requested_vds=(index % width) * snapshot["drain_step_v"],
                                     requested_vgs=(index // width) * snapshot["gate_step_v"])
                        record = struct.pack(archive.RECORD_FORMAT, index, point["curve"],
                                             *[point[key] for key in archive.COLUMNS[2:14]], 10, 2)
                        records.append(record)
                        unpacked = struct.unpack(archive.RECORD_FORMAT, record)
                        points.append(("[%d,%d," % (index, point["curve"]) +
                                       ",".join("%.9g" % item for item in unpacked[2:14]) +
                                       ",10,false,true]").encode())
                    trailer = b'{"outcome":"complete","fault":null}' if version == 2 else b""
                    footer = (struct.pack("<III", count, 10, len(trailer)) if version == 2 else
                              struct.pack("<II", count, 10))
                    magic = archive.MAGIC if version == 2 else archive.MAGIC_V1
                    binary = (struct.pack(archive.PREFIX_FORMAT, magic, len(header)) + header +
                              b"".join(records) + trailer + footer)
                    payload = (header[:-1] + b',"columns":' + archive._COLUMNS_JSON +
                               b',"points":[' + b",".join(points) +
                               b'],"outcome":"complete"' +
                               (b',"fault":null' if version == 2 else b"") +
                               (',"point_count":%d,"duration_ms":10}' % count).encode())
                    info = {"format_version": 1, "scan_id": scan_id, "point_count": count,
                            "sha256": hashlib.sha256(payload).hexdigest(), "wire_size": len(payload),
                            "binary_sha256": hashlib.sha256(binary).hexdigest()}
                    info["metadata_sha256"] = archive._metadata_hash(info)
                    path = self.archive._path(scan_id, ".pending")
                    with open(path, "wb") as stream:
                        stream.write(binary)
                    meta_path = self.archive._path(scan_id, ".meta")
                    with open(meta_path, "w") as stream:
                        json.dump(info, stream)
                    meta_before = open_bytes(meta_path)
                    self.assertEqual(payload, self.wire(path))
                    reopened = archive.Archive(self.folder, "abcd")
                    listing = [item for item in reopened.list_scans() if item["scan_id"] == scan_id][0]
                    self.assertFalse(listing["damaged"])
                    self.assertEqual(count, listing["point_count"])
                    self.assertEqual(binary, open_bytes(path))
                    self.assertEqual(meta_before, open_bytes(meta_path))
                    reopened.mark_sent(scan_id)
                    self.assertEqual(payload, self.wire(reopened._path(scan_id, ".sent")))

    def test_binary_wire_schema_and_float32_accuracy(self):
        info = self.complete(2)
        entry = self.archive.next_pending()
        payload = self.wire(entry["path"], 17)
        self.assertEqual(info["sha256"], hashlib.sha256(payload).hexdigest())
        decoded = json.loads(payload)
        self.assertEqual(archive.COLUMNS, decoded["columns"])
        self.assertEqual("complete", decoded["outcome"])
        self.assertEqual(2, decoded["point_count"])
        self.assertEqual(1000, decoded["duration_ms"])
        self.assertIsNone(decoded["created_utc"])
        self.assertEqual(archive.calibration_hash(CALIBRATION), decoded["calibration"]["sha256"])
        for index, result in enumerate(decoded["points"]):
            original = row(index)
            for column, value in zip(archive.COLUMNS, result):
                if isinstance(original[column], bool):
                    self.assertIs(value, original[column])
                elif column in ("index", "curve", "batches_completed"):
                    self.assertEqual(original[column], value)
                else:
                    # Tens of nanovolts here, well below 2.5 uV shunt/1.25 mV bus LSB.
                    self.assertAlmostEqual(original[column], value, delta=1e-9)
        self.assertLessEqual(os.stat(entry["path"]).st_size,
                             2 * archive.RECORD_BYTES + archive.HEADER_BYTES + 20)

    def test_wire_is_identical_across_reopen_chunk_sizes_and_reboot(self):
        self.complete(4)
        first = self.archive.next_pending()
        payload = self.wire(first["path"], 1)
        reopened = archive.Archive(self.folder, "abcd")
        second = reopened.next_pending()
        self.assertEqual(first, second)
        self.assertEqual(payload, self.wire(second["path"], 1024))
        self.assertEqual(1, reopened.pending_count)

    def test_large_header_reads_and_wire_pieces_are_bounded_without_retaining_header(self):
        calibration = copy.deepcopy(CALIBRATION)
        calibration["metadata"]["notes"] = ["Measured baseline \u03bcA and voltage map " + str(i)
                                             for i in range(65)]
        scan_id = self.archive.begin_scan(settings(2), calibration, 0)
        self.archive.record_point(row())
        info = self.archive.finish_scan(10, fault(1))
        self.archive.accept_scan()
        path = self.archive._path(scan_id, ".pending")
        binary = open_bytes(path)
        length = struct.unpack(archive.PREFIX_FORMAT, binary[:archive.PREFIX_BYTES])[1]
        self.assertGreater(length, 1800)  # The native failure was a ~1.9 KB read.
        self.assertLessEqual(length, archive.HEADER_BYTES)
        header_reads = []
        real_open = open
        class LimitedFile:
            def __init__(self, stream):
                self.stream = stream
            def read(self, count=-1):
                offset = self.stream.tell()
                if archive.PREFIX_BYTES <= offset < archive.PREFIX_BYTES + length:
                    if not 0 < count <= archive.HEADER_CHUNK_BYTES:
                        raise AssertionError("Whole-header allocation")
                    header_reads.append(count)
                return self.stream.read(count)
            def seek(self, offset):
                return self.stream.seek(offset)
            def close(self):
                self.stream.close()
        with patch("firmware.archive.open", lambda path, mode: LimitedFile(real_open(path, mode)), create=True):
            source = archive.UploadSource(path)
            try:
                self.assertFalse(hasattr(source, "_header"))
                self.assertEqual(length, source._header_length)
                payload = bytearray()
                while True:
                    part = source.read(17)
                    if not part:
                        break
                    payload.extend(part)
                    self.assertLessEqual(len(source._piece), archive.TRAILER_BYTES)
            finally:
                source.close()
        self.assertGreater(len(header_reads), 10)
        expected_prefix = (binary[archive.PREFIX_BYTES:archive.PREFIX_BYTES + length - 1] +
                           b',"columns":' + archive._COLUMNS_JSON + b',"points":[')
        self.assertTrue(payload.startswith(expected_prefix))
        self.assertEqual(info["wire_size"], len(payload))
        self.assertEqual(info["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(bytes(payload), self.wire(path, 1024))
        value = json.loads(payload)
        self.assertEqual(calibration["metadata"], value["calibration"]["metadata"])
        self.assertEqual("partial", value["outcome"])
        self.assertEqual(fault(1), value["fault"])

    def test_native_header_readinto_stops_at_boundary_and_rejects_short_input(self):
        encoded = json.dumps({"text": "\u03bc" * 300}).encode()
        stream = io.BytesIO(encoded + b"RECORD_BYTES_MUST_NOT_REACH_JSON")
        reader = archive._HeaderReader(stream, len(encoded))
        target, parts = bytearray(13), []
        while True:
            count = reader.readinto(target)
            if not count:
                break
            parts.append(bytes(target[:count]))
        self.assertEqual(encoded, b"".join(parts))
        self.assertEqual(len(encoded), stream.tell())
        self.assertEqual(0, reader.remaining)
        short = archive._HeaderReader(io.BytesIO(b"{}"), 5)
        self.assertEqual(2, short.readinto(target))
        with self.assertRaisesRegex(ValueError, "Truncated"):
            short.readinto(target)

    def test_header_boundary_trailing_json_and_truncation_are_rejected(self):
        info = self.complete()
        path = self.archive._path(info["scan_id"], ".pending")
        original = open_bytes(path)
        magic, length = struct.unpack(archive.PREFIX_FORMAT, original[:archive.PREFIX_BYTES])
        header = original[archive.PREFIX_BYTES:archive.PREFIX_BYTES + length]
        tail = original[archive.PREFIX_BYTES + length:]
        cases = (
            struct.pack(archive.PREFIX_FORMAT, magic, length + 1) + header + tail,
            struct.pack(archive.PREFIX_FORMAT, magic, length) + header[:-5],
            struct.pack(archive.PREFIX_FORMAT, magic, length + 2) + header + b"{}" + tail,
            struct.pack(archive.PREFIX_FORMAT, magic, length + 1) + header + b" " + tail,
            struct.pack(archive.PREFIX_FORMAT, magic, archive.HEADER_BYTES + 1) + header + tail,
        )
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                with open(path, "wb") as stream:
                    stream.write(data)
                # No sidecar check: the parser itself must reject bad boundaries.
                with self.assertRaisesRegex(RuntimeError, "invalid or damaged"):
                    archive.UploadSource(path, verify=False)

    def test_finalize_reports_fixed_memory_stage_and_resets_for_next_scan(self):
        self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.archive.record_point(row())
        with patch.object(archive.json, "load", side_effect=MemoryError("private detail")):
            with self.assertRaisesRegex(RuntimeError, "finalized") as caught:
                self.archive.finish_scan(10)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(("source", "memory"), self.archive.last_finalize_error)
        self.archive.discard_scan()
        self.archive.begin_scan(settings(), CALIBRATION, 20)
        self.assertIsNone(self.archive.last_finalize_error)
        self.archive.record_point(row())
        self.archive.finish_scan(30)
        self.assertIsNone(self.archive.last_finalize_error)

    def test_source_memory_and_storage_errors_retain_only_fixed_categories(self):
        info = self.complete()
        path = self.archive._path(info["scan_id"], ".pending")
        with patch.object(archive.json, "load", side_effect=MemoryError("private detail")):
            with self.assertRaisesRegex(MemoryError, "Saved scan needs memory") as caught:
                archive.UploadSource(path)
            self.assertNotIn("private", str(caught.exception))
        with patch("firmware.archive.open", side_effect=OSError("private path"), create=True):
            with self.assertRaises(archive.ArchiveReadError) as caught:
                archive.UploadSource(path)
            self.assertEqual("storage", caught.exception.kind)
            self.assertNotIn("private", str(caught.exception))

    def test_verified_source_never_opens_binary_and_sidecar_together(self):
        info = self.complete(2)
        path = self.archive._path(info["scan_id"], ".pending")
        real_open, active, opened = open, [], []
        class OneFile:
            def __init__(self, path, mode):
                if active:
                    raise AssertionError("Two LittleFS caches retained simultaneously")
                self.stream = real_open(path, mode)
                self.path = path
                active.append(path)
                opened.append(path)
            def read(self, count=-1):
                return self.stream.read(count)
            def seek(self, offset):
                return self.stream.seek(offset)
            def close(self):
                if not self.stream.closed:
                    self.stream.close()
                    active.remove(self.path)
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                self.close()
        with patch("firmware.archive.open", OneFile, create=True), \
                patch("firmware.storage.open", OneFile, create=True):
            source = archive.UploadSource(path)
            try:
                self.assertEqual(2, source.point_count)
                self.assertEqual([path], active)
                digest, size = hashlib.sha256(), 0
                while True:
                    piece = source.read(256)
                    if not piece:
                        break
                    digest.update(piece)
                    size += len(piece)
                self.assertEqual(info["sha256"], digest.hexdigest())
                self.assertEqual(info["wire_size"], size)
            finally:
                source.close()
        self.assertEqual([], active)
        self.assertEqual([path.rsplit(".", 1)[0] + ".meta", path], opened)

    def test_full_4096_point_scan_stays_small_and_wire_under_limit(self):
        info = self.complete(4096)
        entry = self.archive.next_pending()
        self.assertLess(os.stat(entry["path"]).st_size, 226000)
        self.assertLess(info["wire_size"], 2 * 1024 * 1024)
        source = archive.UploadSource(entry["path"])
        count = 0
        try:
            while True:
                piece = source.read()
                if not piece:
                    break
                count += len(piece)
        finally:
            source.close()
        self.assertEqual(info["wire_size"], count)

    def test_corruption_truncation_bad_magic_and_metadata_rejected_before_read(self):
        for mode in ("byte", "truncated", "magic", "metadata"):
            with self.subTest(mode=mode):
                info = self.complete()
                path = self.archive._path(info["scan_id"], ".pending")
                if mode == "metadata":
                    meta = path.rsplit(".", 1)[0] + ".meta"
                    with open(meta) as stream:
                        value = json.load(stream)
                    value["wire_size"] += 1
                    with open(meta, "w") as stream:
                        json.dump(value, stream)
                else:
                    with open(path, "rb") as stream:
                        data = bytearray(stream.read())
                    if mode == "truncated":
                        data.pop()
                    else:
                        data[0 if mode == "magic" else -20] ^= 1
                    with open(path, "wb") as stream:
                        stream.write(data)
                with self.assertRaisesRegex(RuntimeError, "invalid or damaged"):
                    archive.UploadSource(path)
        self.assertEqual(4, self.archive.pending_count)

    def test_bad_record_flags_rejected_even_with_recomputed_integrity(self):
        info = self.complete()
        path = self.archive._path(info["scan_id"], ".pending")
        with open(path, "rb") as stream:
            data = bytearray(stream.read())
        header_length = struct.unpack(archive.PREFIX_FORMAT, data[:archive.PREFIX_BYTES])[1]
        data[archive.PREFIX_BYTES + header_length + archive.RECORD_BYTES - 1] = 8
        with open(path, "wb") as stream:
            stream.write(data)
        meta = self.archive._path(info["scan_id"], ".meta")
        info["binary_sha256"] = hashlib.sha256(data).hexdigest()
        info["metadata_sha256"] = archive._metadata_hash(info)
        with open(meta, "w") as stream:
            json.dump(info, stream)
        with self.assertRaisesRegex(RuntimeError, "invalid or damaged"):
            archive.UploadSource(path)

    def test_no_space_prevents_draft_and_does_not_remove_pending(self):
        self.complete()
        pending = self.archive.next_pending()
        self.free = 0
        with self.assertRaisesRegex(RuntimeError, "flash space"):
            self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertIsNone(self.archive._current)
        self.assertTrue(os.path.exists(pending["path"]))
        self.assertEqual([], self.archive._names(".writing"))

    def test_roomy_begin_uses_no_full_capacity_preview_or_sent_metadata_walk(self):
        sent = self.complete()["scan_id"]
        self.archive.mark_sent(sent)
        with patch.object(self.archive, "scan_capacity", side_effect=AssertionError("redundant preview")), \
                patch.object(self.archive, "_reclaimable_sent", side_effect=AssertionError("unneeded metadata walk")):
            self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertTrue(os.path.exists(self.archive._path(sent, ".sent")))
        self.archive.discard_scan()

    def test_begin_rechecks_space_after_successful_read_only_preview(self):
        self.assertTrue(self.archive.scan_capacity(settings())["can_start"])
        self.free = 0
        with self.assertRaises(archive.ArchiveCapacityError) as stopped:
            self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertEqual("space", stopped.exception.reason)
        self.assertIsNone(self.archive._current)
        self.assertEqual([], os.listdir(self.folder))

    def test_begin_rechecks_queue_after_successful_preview_before_any_cleanup(self):
        self.assertTrue(self.archive.scan_capacity(settings())["can_start"])
        for index in range(archive.MAX_PENDING):
            with open(self.archive._path("abcd-%04x" % index, ".pending"), "wb") as stream:
                stream.write(b"accepted data")
        with patch.object(self.archive, "_reclaim_unaccepted", side_effect=AssertionError("queue must win")):
            with self.assertRaises(archive.ArchiveCapacityError) as stopped:
                self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertEqual("queue", stopped.exception.reason)
        self.assertIsNone(self.archive._current)
        self.assertEqual(archive.MAX_PENDING, self.archive.pending_count)

    def test_insufficient_total_space_does_not_needlessly_reclaim_sent_scan(self):
        sent = self.complete()["scan_id"]
        self.archive.mark_sent(sent)
        before = {name: open_bytes(os.path.join(self.folder, name)) for name in os.listdir(self.folder)}
        self.free = 0
        with self.assertRaises(archive.ArchiveCapacityError):
            self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})

    def test_cached_calibration_keeps_streamed_hash_and_replacement_refreshes_it(self):
        first = copy.deepcopy(CALIBRATION)
        second = copy.deepcopy(first)
        second["metadata"]["label"] = "replacement calibration"
        original = archive.calibration_hash
        with patch.object(archive, "calibration_hash", wraps=original) as hashed:
            with patch.object(archive.json, "dumps", side_effect=AssertionError("do not buffer calibration JSON")):
                digest = self.archive.prepare_calibration(first)
            self.assertEqual(original(first), digest)
            self.assertEqual(digest, self.archive.prepare_calibration(first))
            self.archive.begin_scan(settings(), first, 0)
            self.archive.record_point(row())
            self.archive.finish_scan(10)
            self.archive.accept_scan()
            old = self.archive.next_pending()
            old_wire = self.wire(old["path"])
            self.assertEqual(digest, json.loads(old_wire)["calibration"]["sha256"])
            self.assertEqual(1, hashed.call_count)
            # No explicit cache hook is needed to recognize a replacement.
            self.archive.begin_scan(settings(), second, 20)
            self.assertEqual(2, hashed.call_count)
            self.assertEqual(original(second), self.archive._calibration_sha)
            self.assertNotEqual(digest, self.archive._calibration_sha)
            self.assertEqual(old_wire, self.wire(old["path"]))
            self.archive.discard_scan()

    def test_streamed_header_matches_previous_dumps_bytes_including_non_ascii(self):
        value = copy.deepcopy(CALIBRATION)
        value["metadata"].update(label="Gate \u03bcA / \u03a9 / \u6e2c\u5b9a / \U0001f50b", comment="x" * 400)
        expected_digest = self.archive.prepare_calibration(value)
        with patch.object(archive.json, "dumps", side_effect=AssertionError("full header serialization")):
            scan_id = self.archive.begin_scan(settings(), value, 0)
        path = self.archive._path(scan_id, ".writing")
        data = open_bytes(path)
        magic, count = struct.unpack(archive.PREFIX_FORMAT, data[:archive.PREFIX_BYTES])
        header = {"schema_version": 2, "scan_id": scan_id, "device_id": "abcd", "created_utc": None,
                  "settings": model.validate_archive_settings(settings()),
                  "calibration": {"sha256": expected_digest, "metadata": value["metadata"]}}
        previous = json.dumps(header).encode("utf-8")
        self.assertEqual(archive.MAGIC, magic)
        self.assertEqual(len(previous), count)
        self.assertEqual(previous, data[archive.PREFIX_BYTES:])
        self.archive.record_point(row())
        self.archive.finish_scan(10)
        self.archive.accept_scan()
        decoded = json.loads(self.wire(self.archive.next_pending()["path"]))
        self.assertEqual(value["metadata"], decoded["calibration"]["metadata"])

    def test_header_writer_bounds_utf8_chunks_and_counts_bytes_not_characters(self):
        text = ("a\u03bc\u6e2c\U0001f50b" * 83) + "end"
        expected = text.encode("utf-8")
        counter = archive._HeaderWriter()
        self.assertEqual(len(text), counter.write(text))
        self.assertEqual(len(expected), counter.count)
        self.assertIsNone(counter._buffer)
        chunks = []
        writer = archive._HeaderWriter(lambda part: chunks.append(bytes(part)))
        self.assertEqual(len(text), writer.write(text))
        writer.finish()
        self.assertEqual(len(expected), writer.count)
        self.assertEqual(expected, b"".join(chunks))
        self.assertTrue(all(0 < len(chunk) <= 256 for chunk in chunks))
        self.assertEqual(256, len(writer._buffer))
        writer.finish()
        self.assertEqual(expected, b"".join(chunks))

    def test_oversized_header_is_rejected_before_creating_a_working_file(self):
        accepted = self.complete()["scan_id"]
        before = {name: open_bytes(os.path.join(self.folder, name)) for name in os.listdir(self.folder)}
        value = copy.deepcopy(CALIBRATION)
        value["metadata"]["label"] = "x" * archive.HEADER_BYTES
        with self.assertRaisesRegex(RuntimeError, "could not be started"):
            self.archive.begin_scan(settings(), value, 0)
        self.assertIsNone(self.archive._stream)
        self.assertIsNone(self.archive._current)
        self.assertEqual([], self.archive._names(".writing"))
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})
        self.assertEqual(accepted, self.archive.next_pending()["scan_id"])

    def test_short_streamed_header_write_closes_and_can_discard_only_new_draft(self):
        accepted = self.complete()["scan_id"]
        before = {name: open_bytes(os.path.join(self.folder, name)) for name in os.listdir(self.folder)}
        real_open = open
        streams = []
        class ShortHeader:
            def __init__(self, path, mode):
                self.stream, self.calls = real_open(path, mode), 0
                self.closed = False
            def write(self, data):
                self.calls += 1
                if self.calls == 2:
                    self.stream.write(data[:-1])
                    return len(data) - 1
                return self.stream.write(data)
            def close(self):
                self.closed = True
                self.stream.close()
        def opened(path, mode):
            if path.endswith(".writing"):
                stream = ShortHeader(path, mode)
                streams.append(stream)
                return stream
            return real_open(path, mode)
        with patch("firmware.archive.open", opened, create=True):
            with self.assertRaisesRegex(RuntimeError, "could not be started"):
                self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertEqual(1, len(streams))
        self.assertTrue(streams[0].closed)
        self.assertEqual("error", self.archive.state)
        self.assertIsNone(self.archive._stream)
        with self.assertRaises(RuntimeError):
            self.archive.accept_scan()
        self.archive.discard_scan()
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})
        self.assertEqual(accepted, self.archive.next_pending()["scan_id"])

    def test_explicit_calibration_invalidation_and_failed_replacement_are_safe(self):
        value = copy.deepcopy(CALIBRATION)
        before = self.archive.prepare_calibration(value)
        with patch.object(archive, "calibration_hash", side_effect=ValueError("bad serialization")):
            with self.assertRaises(ValueError):
                self.archive.prepare_calibration(copy.deepcopy(value))
            self.assertEqual(before, self.archive.prepare_calibration(value))
        self.archive.invalidate_calibration()
        value["metadata"]["label"] = "explicitly edited calibration"
        after = self.archive.prepare_calibration(value)
        self.assertNotEqual(before, after)
        self.assertIs(value, self.archive._calibration_object)
        self.assertIsNone(self.archive.prepare_calibration(None))
        self.assertIsNone(self.archive._calibration_object)
        self.assertIsNone(self.archive._calibration_sha)

    def test_capacity_is_read_only_exact_and_matches_boundary(self):
        accepted = self.complete()["scan_id"]
        draft = self.complete(accept=False)["scan_id"]
        before = {name: open_bytes(os.path.join(self.folder, name))
                  for name in os.listdir(self.folder)}
        required = (archive.PREFIX_BYTES + archive.HEADER_BYTES +
                    4096 * archive.RECORD_BYTES + archive.TRAILER_BYTES + archive.FOOTER_BYTES + archive.RESERVE_BYTES)
        self.free = required
        with patch.object(archive.os, "remove", side_effect=AssertionError("read-only")):
            value = self.archive.scan_capacity(settings(4096))
        self.assertEqual(required, value["required_bytes"])
        self.assertEqual(required, value["actual_free_bytes"])
        self.assertEqual(required, value["free_bytes"])
        self.assertEqual(0, value["reclaimable_bytes"])
        self.assertEqual(1, value["accepted_count"])
        self.assertEqual(1, value["pending_count"])
        self.assertEqual(archive.MAX_PENDING, value["pending_limit"])
        self.assertTrue(value["can_start"])
        self.assertTrue(value["can_start_after_reclaim"])
        self.assertIsNone(value["reason"])
        self.free -= 1
        self.assertEqual("space", self.archive.scan_capacity(settings(4096))["reason"])
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})
        self.assertIn(accepted + ".pending", before)
        self.assertIn(draft + ".draft", before)

    def test_capacity_counts_only_sent_reclamation_and_begin_reuses_it(self):
        pending = self.complete()["scan_id"]
        sent = self.complete()["scan_id"]
        self.archive.mark_sent(sent)
        sent_size = sum(os.stat(self.archive._path(sent, suffix)).st_size
                        for suffix in (".sent", ".meta"))
        required = self.archive.scan_capacity(settings())["required_bytes"]
        initial_used = sum(os.stat(os.path.join(self.folder, name)).st_size
                           for name in os.listdir(self.folder))
        def available():
            used = sum(os.stat(os.path.join(self.folder, name)).st_size
                       for name in os.listdir(self.folder))
            return required - sent_size + initial_used - used
        self.archive._free_bytes = available
        capacity = self.archive.scan_capacity(settings())
        self.assertTrue(capacity["can_start"])
        self.assertLess(capacity["free_bytes"], required)
        self.assertEqual(sent_size, capacity["reclaimable_bytes"])
        self.assertTrue(os.path.exists(self.archive._path(sent, ".sent")))
        self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertFalse(os.path.exists(self.archive._path(sent, ".sent")))
        self.assertFalse(os.path.exists(self.archive._path(sent, ".meta")))
        self.assertTrue(os.path.exists(self.archive._path(pending, ".pending")))
        self.archive.discard_scan()

    def test_capacity_includes_stale_unaccepted_files_once_and_begin_can_reclaim_them(self):
        stale = self.complete(accept=False)["scan_id"]
        # Interrupted transitions can leave both suffixes and a temporary
        # sidecar. Metadata must not be counted once for each data suffix.
        for source, target in ((".draft", ".writing"), (".meta", ".meta.tmp")):
            with open(self.archive._path(stale, target), "wb") as stream:
                stream.write(open_bytes(self.archive._path(stale, source)))
        required = self.archive.scan_capacity(settings())["required_bytes"]
        stale_bytes = sum(os.stat(os.path.join(self.folder, name)).st_size
                          for name in os.listdir(self.folder))
        def available():
            used = sum(os.stat(os.path.join(self.folder, name)).st_size
                       for name in os.listdir(self.folder))
            return required - used
        reopened = archive.Archive(self.folder, "abcd", random_bytes=self.random,
                                   free_bytes=available)
        before = {name: open_bytes(os.path.join(self.folder, name))
                  for name in os.listdir(self.folder)}
        with patch.object(archive.os, "remove", side_effect=AssertionError("read-only")):
            capacity = reopened.scan_capacity(settings())
        self.assertLess(capacity["actual_free_bytes"], required)
        self.assertEqual(0, capacity["reclaimable_bytes"])
        self.assertEqual(stale_bytes, capacity["reclaimable_unaccepted_bytes"])
        self.assertTrue(capacity["can_start"])
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})
        reopened.begin_scan(settings(), CALIBRATION, 0)
        self.assertFalse(any(name.startswith(stale + ".") for name in os.listdir(self.folder)))
        reopened.discard_scan()

    def test_unaccepted_capacity_and_cleanup_protect_current_and_accepted_collisions(self):
        accepted = self.complete()["scan_id"]
        current = self.complete(accept=False)["scan_id"]
        for suffix in (".draft", ".writing"):
            with open(self.archive._path(accepted, suffix), "wb") as stream:
                stream.write(b"ambiguous accepted identity")
        before = {name: open_bytes(os.path.join(self.folder, name))
                  for name in os.listdir(self.folder)}
        self.assertEqual(0, self.archive.scan_capacity(settings())["reclaimable_unaccepted_bytes"])
        self.archive._reclaim_unaccepted()
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})
        self.assertEqual(current, self.archive._current)

    def test_queue_capacity_precedes_space_and_selected_delete_reopens_slot(self):
        ids = [self.complete()["scan_id"] for unused in range(archive.MAX_PENDING)]
        self.free = 0
        capacity = self.archive.scan_capacity(settings())
        self.assertEqual("queue", capacity["reason"])
        self.assertFalse(capacity["can_start"])
        self.assertEqual(archive.MAX_PENDING, capacity["accepted_count"])
        self.free = 4 * 1024 * 1024
        self.assertTrue(self.archive.delete_scan(ids[7]))
        reopened = archive.Archive(self.folder, "abcd", free_bytes=lambda: self.free)
        capacity = reopened.scan_capacity(settings())
        self.assertEqual(19, capacity["pending_count"])
        self.assertTrue(capacity["can_start"])
        self.assertEqual(set(ids) - {ids[7]}, {item["scan_id"] for item in reopened.list_scans()})

    def test_capacity_does_not_offer_damaged_sent_or_shared_metadata_for_reclaim(self):
        valid = self.complete()["scan_id"]
        broken = self.complete()["scan_id"]
        self.archive.mark_sent(valid)
        self.archive.mark_sent(broken)
        with open(self.archive._path(broken, ".meta"), "w") as stream:
            stream.write("damaged")
        expected = self.archive._accepted_size(valid, ".sent")
        self.assertEqual(expected, self.archive.scan_capacity(settings())["reclaimable_bytes"])
        with open(self.archive._path(valid, ".pending"), "wb") as stream:
            stream.write(open_bytes(self.archive._path(valid, ".sent")))
        self.assertEqual(0, self.archive.scan_capacity(settings())["reclaimable_bytes"])
        self.assertTrue(os.path.exists(self.archive._path(broken, ".sent")))

    def test_list_scans_oldest_first_survives_reboot_without_reading_point_bodies(self):
        self.archive._utc_provider = lambda: "2026-11-01T12:34:56Z"
        first = self.complete(2)["scan_id"]
        second = self.complete()["scan_id"]
        self.archive.mark_sent(first)
        os.utime(self.archive._path(first, ".sent"), (2000, 2000))
        os.utime(self.archive._path(second, ".pending"), (1000, 1000))
        current = self.complete(accept=False)["scan_id"]
        reopened = archive.Archive(self.folder, "abcd")
        reads = []
        real_open = open
        class BoundedReader:
            def __init__(self, stream):
                self.stream = stream
            def read(self, count=-1):
                self.assert_bounded(count)
                reads.append(count)
                return self.stream.read(count)
            @staticmethod
            def assert_bounded(count):
                if not 1 <= count <= archive.HEADER_BYTES:
                    raise AssertionError("Unbounded archive listing read")
            def seek(self, offset):
                return self.stream.seek(offset)
            def __enter__(self):
                return self
            def __exit__(self, *unused):
                self.stream.close()
        with patch("firmware.archive.open", lambda path, mode: BoundedReader(real_open(path, mode)), create=True):
            entries = reopened.list_scans()
        self.assertEqual([second, first], [item["scan_id"] for item in entries])
        self.assertEqual(["pending", "sent"], [item["status"] for item in entries])
        self.assertEqual([1, 2], [item["point_count"] for item in entries])
        self.assertTrue(all(not item["damaged"] for item in entries))
        self.assertTrue(all(item["created_utc"] == "2026-11-01T12:34:56Z" for item in entries))
        self.assertGreater(len(reads), 8)  # Headers now arrive in <=256-byte chunks.
        self.assertLessEqual(max(reads), archive.HEADER_CHUNK_BYTES)
        self.assertEqual(self.archive._accepted_size(first, ".sent"), entries[1]["size_bytes"])
        self.assertTrue(os.path.exists(self.archive._path(current, ".draft")))

    def test_list_marks_corrupt_items_without_hiding_other_accepted_scans(self):
        good = self.complete()["scan_id"]
        bad_ids = []
        for mode in ("identity", "header", "truncated", "metadata", "missing_metadata"):
            info = self.complete()
            scan_id = info["scan_id"]
            bad_ids.append(scan_id)
            path = self.archive._path(scan_id, ".pending")
            if mode == "identity":
                info["scan_id"] = good
                info["metadata_sha256"] = archive._metadata_hash(info)
                with open(self.archive._path(scan_id, ".meta"), "w") as stream:
                    json.dump(info, stream)
            elif mode == "metadata":
                with open(self.archive._path(scan_id, ".meta"), "w") as stream:
                    stream.write("{" * 1025)
            elif mode == "missing_metadata":
                os.remove(self.archive._path(scan_id, ".meta"))
            else:
                data = open_bytes(path)
                with open(path, "wb") as stream:
                    stream.write(data[:-1] if mode == "truncated" else b"broken" + data[6:])
        entries = {item["scan_id"]: item for item in self.archive.list_scans()}
        self.assertFalse(entries[good]["damaged"])
        self.assertTrue(all(entries[scan_id]["damaged"] for scan_id in bad_ids))
        self.assertTrue(all(entries[scan_id]["point_count"] is None for scan_id in bad_ids))
        self.assertEqual(6, self.archive.pending_count)

    def test_selected_delete_preserves_other_pending_sent_and_current_draft(self):
        removed = self.complete()["scan_id"]
        pending = self.complete()["scan_id"]
        sent = self.complete()["scan_id"]
        self.archive.mark_sent(sent)
        draft = self.complete(accept=False)["scan_id"]
        before = {name: open_bytes(os.path.join(self.folder, name))
                  for name in os.listdir(self.folder) if not name.startswith(removed + ".")}
        self.assertTrue(self.archive.delete_scan(removed))
        self.assertEqual(before, {name: open_bytes(os.path.join(self.folder, name))
                                  for name in os.listdir(self.folder)})
        self.assertEqual(draft, self.archive._current)
        self.assertEqual(1, self.archive.pending_count)
        self.assertTrue(self.archive.delete_scan(sent))
        self.assertTrue(os.path.exists(self.archive._path(pending, ".pending")))
        self.assertTrue(os.path.exists(self.archive._path(draft, ".draft")))

    def test_delete_uses_filename_identity_even_if_metadata_names_another_scan(self):
        victim = self.complete()["scan_id"]
        selected = self.complete()
        selected_id = selected["scan_id"]
        victim_bytes = self.wire(self.archive._path(victim, ".pending"))
        selected["scan_id"] = victim
        selected["metadata_sha256"] = archive._metadata_hash(selected)
        with open(self.archive._path(selected_id, ".meta"), "w") as stream:
            json.dump(selected, stream)
        self.assertTrue(self.archive.delete_scan(selected_id))
        self.assertEqual(victim_bytes, self.wire(self.archive._path(victim, ".pending")))
        self.assertFalse(os.path.exists(self.archive._path(selected_id, ".pending")))

    def test_delete_rejects_paths_missing_drafts_and_ambiguous_accepted_ids(self):
        accepted = self.complete()["scan_id"]
        draft = self.complete(accept=False)["scan_id"]
        for scan_id in (None, "", "../" + accepted, accepted + "/../x", "missing", draft):
            with self.subTest(scan_id=scan_id):
                with self.assertRaisesRegex(RuntimeError, "Selected scan could not be deleted"):
                    self.archive.delete_scan(scan_id)
        with open(self.archive._path(accepted, ".sent"), "wb") as stream:
            stream.write(open_bytes(self.archive._path(accepted, ".pending")))
        with self.assertRaises(RuntimeError):
            self.archive.delete_scan(accepted)
        self.assertTrue(os.path.exists(self.archive._path(accepted, ".pending")))
        self.assertTrue(os.path.exists(self.archive._path(accepted, ".sent")))
        self.assertTrue(os.path.exists(self.archive._path(draft, ".draft")))

    def test_delete_failure_preserves_unselected_files_and_payload_failure_keeps_sidecar(self):
        selected = self.complete()["scan_id"]
        other = self.complete()["scan_id"]
        other_path = self.archive._path(other, ".pending")
        other_bytes = self.wire(other_path)
        real_remove = archive.os.remove
        def fail_payload(path):
            if path == self.archive._path(selected, ".pending"):
                raise OSError("delete failed")
            return real_remove(path)
        with patch.object(archive.os, "remove", side_effect=fail_payload):
            with self.assertRaises(RuntimeError):
                self.archive.delete_scan(selected)
        self.assertTrue(os.path.exists(self.archive._path(selected, ".meta")))
        self.assertTrue(os.path.exists(self.archive._path(selected, ".pending")))
        with patch.object(archive, "_sync", side_effect=OSError("sync failed")):
            with self.assertRaises(RuntimeError):
                self.archive.delete_scan(selected)
        self.assertEqual(other_bytes, self.wire(other_path))
        self.assertEqual([other], self.archive._names(".pending"))

    def test_queue_limit_and_sent_retention_never_delete_unacknowledged(self):
        for unused in range(20):
            self.complete()
        with self.assertRaisesRegex(RuntimeError, "queue is full"):
            self.archive.begin_scan(settings(), CALIBRATION, 0)
        self.assertEqual(20, self.archive.pending_count)
        for unused in range(7):
            self.archive.mark_sent(self.archive.next_pending()["scan_id"])
        self.assertEqual(13, self.archive.pending_count)
        self.assertEqual(5, len(self.archive._names(".sent")))

    def test_acknowledgement_order_survives_reboot_and_clock_rollback(self):
        ids = []
        for index in range(6):
            info = self.complete()
            scan_id = info["scan_id"]
            ids.append(scan_id)
            pending = self.archive._path(scan_id, ".pending")
            stamp = 2000000000 + index if index < 5 else 1000
            os.utime(pending, (stamp, stamp))
            if index == 5:
                self.archive = archive.Archive(self.folder, "abcd", random_bytes=self.random,
                                               free_bytes=lambda: self.free)
            self.archive.mark_sent(scan_id)
            saved = archive._metadata(self.archive._path(scan_id, ".meta"))
            self.assertEqual(index + 1, saved["ack_order"])
            self.assertEqual(info["sha256"], saved["sha256"])
            self.assertEqual(info["wire_size"], saved["wire_size"])
            self.assertEqual(info["binary_sha256"], saved["binary_sha256"])
        self.assertEqual(ids[1:], self.archive._ordered(".sent"))
        self.assertTrue(os.path.exists(self.archive._path(ids[-1], ".sent")))
        self.assertFalse(os.path.exists(self.archive._path(ids[0], ".sent")))

    def test_legacy_sent_metadata_keeps_hash_and_is_older_than_new_ack(self):
        old = self.complete()
        self.assertNotIn("ack_order", old)
        archive._replace(self.archive._path(old["scan_id"], ".pending"),
                         self.archive._path(old["scan_id"], ".sent"))
        old_path = self.archive._path(old["scan_id"], ".sent")
        os.utime(old_path, (2000000000, 2000000000))
        new = self.complete()
        new_path = self.archive._path(new["scan_id"], ".pending")
        os.utime(new_path, (1000, 1000))
        self.archive.mark_sent(new["scan_id"])
        self.assertEqual([old["scan_id"], new["scan_id"]], self.archive._ordered(".sent"))
        self.assertEqual(old["metadata_sha256"], archive._metadata(
            self.archive._path(old["scan_id"], ".meta"))["metadata_sha256"])

    def test_failed_ack_metadata_commit_preserves_pending_and_retry_bytes(self):
        info = self.complete()
        path = self.archive._path(info["scan_id"], ".pending")
        payload = self.wire(path)
        with patch.object(archive.storage, "save_json_atomic", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                self.archive.mark_sent(info["scan_id"])
        self.assertEqual(1, self.archive.pending_count)
        self.assertEqual(payload, self.wire(path))
        # A rename failure after metadata publication is equally retryable.
        with patch.object(archive, "_replace", side_effect=OSError("rename failed")):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                self.archive.mark_sent(info["scan_id"])
        self.assertEqual(1, self.archive.pending_count)
        self.assertEqual(payload, self.wire(path))
        self.archive.mark_sent(info["scan_id"])
        self.assertEqual(0, self.archive.pending_count)

    def test_ack_metadata_sync_failure_leaves_pending_and_valid_sidecar(self):
        info = self.complete()
        path = self.archive._path(info["scan_id"], ".pending")
        payload = self.wire(path)
        # The second sync in save_json_atomic follows metadata replacement.
        with patch.object(archive.storage.os, "sync", create=True,
                          side_effect=[None, OSError("sync after metadata rename")]):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement"):
                self.archive.mark_sent(info["scan_id"])
        self.assertEqual(1, self.archive.pending_count)
        self.assertEqual(payload, self.wire(path))
        self.assertEqual(1, archive._metadata(self.archive._path(info["scan_id"], ".meta"))["ack_order"])

    def test_ack_order_is_validated_and_covered_by_metadata_hash(self):
        info = self.complete()
        path = self.archive._path(info["scan_id"], ".meta")
        for order in (0, -1, True, 1.5, 0x80000000):
            value = dict(info, ack_order=order)
            with open(path, "w") as stream:
                json.dump(value, stream)
            with self.assertRaises(ValueError):
                archive._metadata(path)
        value = dict(info, ack_order=1)
        with open(path, "w") as stream:
            json.dump(value, stream)
        with self.assertRaises(ValueError):
            archive._metadata(path)

    def test_reboot_reclaims_only_unaccepted_drafts_before_new_scan(self):
        accepted = self.complete()["scan_id"]
        unaccepted = self.complete(accept=False)["scan_id"]
        reopened = archive.Archive(self.folder, "abcd", random_bytes=lambda count: b"x" * count,
                                   free_bytes=lambda: self.free)
        self.assertEqual(1, reopened.pending_count)
        reopened.begin_scan(settings(), CALIBRATION, 0)
        self.assertFalse(os.path.exists(self.archive._path(unaccepted, ".draft")))
        self.assertTrue(os.path.exists(self.archive._path(accepted, ".pending")))
        reopened.discard_scan()

    def test_write_failure_cannot_be_finished_or_queued(self):
        self.archive.begin_scan(settings(), CALIBRATION, 0)
        stream = self.archive._stream
        class FailingWrite:
            def write(self, data):
                stream.write(data[:4])
                raise OSError("disk full")
            def close(self):
                stream.close()
        self.archive._stream = FailingWrite()
        with self.assertRaisesRegex(RuntimeError, "could not be written"):
            self.archive.record_point(row())
        self.assertEqual("error", self.archive.state)
        with self.assertRaises(RuntimeError):
            self.archive.finish_scan(10)
        with self.assertRaises(RuntimeError):
            self.archive.accept_scan()
        self.archive.discard_scan()
        self.assertEqual([], os.listdir(self.folder))

    def test_accept_sync_failure_never_lets_cancel_delete_pending(self):
        info = self.complete(accept=False)
        with patch.object(archive, "_sync", side_effect=OSError("sync")):
            with self.assertRaises(RuntimeError):
                self.archive.accept_scan()
        self.archive.discard_scan()
        self.assertTrue(os.path.exists(self.archive._path(info["scan_id"], ".pending")))
        self.assertEqual(1, self.archive.pending_count)

    def test_invalid_samples_never_become_records(self):
        for change in ({"index": 2}, {"id_a": float("nan")}, {"ig_std_a": -1},
                       {"batches_completed": 9}, {"gate_voltage_limited": 1}, {"curve": 5000}):
            self.archive.begin_scan(settings(), CALIBRATION, 0)
            value = row()
            value.update(change)
            with self.assertRaisesRegex(RuntimeError, "could not be written"):
                self.archive.record_point(value)
            self.archive.discard_scan()


if __name__ == "__main__":
    unittest.main()
