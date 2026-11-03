"""Immutable RAM scan snapshots with bounded, repeatable schema-3 JSON reads.

Construction snapshots the small header before acquisition. Sealing stores a
bounded diagnostic, freezes measurements, and never builds the full payload.
prepare_step hashes at most 256 wire bytes per call; readers reproduce them.
This module has no filesystem or legacy archive dependency.
"""

import binascii
import hashlib
import io
import json
import math

try:
    from time import ticks_diff as _ticks_diff
except ImportError:
    def _ticks_diff(now, before):
        return now - before

try:
    from . import model
    from .calibration_json import dump_calibration
    from .ram_scan import PackedScanBuffer
except ImportError:
    import model
    from calibration_json import dump_calibration
    from ram_scan import PackedScanBuffer


COLUMNS = ("index", "curve", "requested_vds", "requested_vgs", "vds", "vgs",
           "id_a", "ig_a", "drain_voltage_limited", "gate_voltage_limited", "gate_off_target")
CHUNK_BYTES = 256
HEADER_BYTES = 4096
TRAILER_BYTES = 1024
MAX_WIRE_BYTES = 2 * 1024 * 1024


def _hex(value):
    return binascii.hexlify(value).decode("ascii")


def _digest_valid(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(character in "0123456789abcdef" for character in value))


def _safe_id(value, maximum=96):
    return (isinstance(value, str) and 1 <= len(value) <= maximum and
            all(character in "0123456789abcdefABCDEF-_" for character in value))


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid scan number")
    try:
        if not math.isfinite(value):
            raise ValueError("Invalid scan number")
    except OverflowError:
        raise ValueError("Invalid scan number")
    return value


class _HashWriter(io.IOBase):
    def __init__(self):
        self.hash = hashlib.sha256()

    def write(self, value):
        if isinstance(value, str):
            for offset in range(0, len(value), 64):
                self.hash.update(value[offset:offset + 64].encode("utf-8"))
        else:
            self.hash.update(value)
        return len(value)


def calibration_hash(calibration):
    if not isinstance(calibration, dict):
        raise ValueError("Invalid calibration fingerprint input")
    writer = _HashWriter()
    dump_calibration(calibration, writer)
    return _hex(writer.hash.digest())


class _ChunkWriter(io.IOBase):
    """Native MicroPython JSON stream protocol, bounded UTF-8 staging."""
    def __init__(self, maximum):
        self._maximum, self.count, self._used = maximum, 0, 0
        self._buffer = bytearray(CHUNK_BYTES)
        self._view = memoryview(self._buffer)
        self.chunks = []

    def _consume(self, value):
        self.count += len(value)
        if self.count > self._maximum:
            raise ValueError("Scan metadata exceeds size limit")
        offset = 0
        view = memoryview(value)
        while offset < len(view):
            take = min(CHUNK_BYTES - self._used, len(view) - offset)
            self._view[self._used:self._used + take] = view[offset:offset + take]
            self._used += take
            offset += take
            if self._used == CHUNK_BYTES:
                self.chunks.append(bytes(self._buffer))
                self._used = 0

    def write(self, value):
        if isinstance(value, str):
            for offset in range(0, len(value), 64):
                self._consume(value[offset:offset + 64].encode("utf-8"))
        else:
            self._consume(value)
        return len(value)

    def finish(self):
        if self._used:
            self.chunks.append(bytes(self._view[:self._used]))
            self._used = 0
        return tuple(self.chunks)


def _json_chunks(value, maximum):
    writer = _ChunkWriter(maximum)
    json.dump(value, writer)
    return writer.finish()


def _validate_utc(value):
    if value is None:
        return
    if (not isinstance(value, str) or not 20 <= len(value) <= 32 or
            value[4] != "-" or value[7] != "-" or value[10] != "T" or
            value[13] != ":" or value[16] != ":" or value[-1] != "Z" or
            any(character not in "0123456789" for character in
                value[:4] + value[5:7] + value[8:10] + value[11:13] + value[14:16] + value[17:19]) or
            (len(value) > 20 and (value[19] != "." or not value[20:-1] or
                                 any(character not in "0123456789" for character in value[20:-1])))):
        raise ValueError("Invalid scan creation time")


def _validate_fault(fault, count, drains, gates):
    keys = ("channel", "kind", "raw_code", "sign", "limit_value", "limit_unit",
            "measured_value", "index", "curve", "requested_vds", "requested_vgs", "phase")
    if not isinstance(fault, dict) or len(fault) != len(keys) or any(key not in fault for key in keys):
        raise ValueError("Invalid stopped scan diagnostic")
    if (fault["channel"] not in ("drain", "gate") or fault["kind"] not in
            ("shunt_saturation", "bus_saturation", "current_limit", "current_limit_alert") or
            fault["phase"] not in ("drive", "park_drain", "settle_gate", "settle", "sample")):
        raise ValueError("Invalid stopped scan diagnostic")
    for key in ("sign", "index", "curve"):
        if isinstance(fault[key], bool) or not isinstance(fault[key], int):
            raise ValueError("Invalid stopped scan diagnostic")
    if (fault["sign"] not in (-1, 1) or fault["index"] != count or
            not 0 <= count < len(drains) * len(gates) or fault["curve"] != count // len(drains)):
        raise ValueError("Invalid stopped scan position")
    for key, target_uv in (("requested_vds", drains[count % len(drains)]),
                           ("requested_vgs", gates[count // len(drains)])):
        target = model.volts_from_uv(target_uv)
        if abs(_finite(fault[key]) - target) > 1e-5 * max(1, abs(target)):
            raise ValueError("Invalid stopped scan target")
    limit = _finite(fault["limit_value"])
    if not 0 < limit <= 100:
        raise ValueError("Invalid stopped scan limit")
    if fault["kind"] == "current_limit_alert":
        if (fault["raw_code"] is not None or fault["limit_unit"] != "A" or
                fault["measured_value"] is not None or limit > 1):
            raise ValueError("Invalid latched current-limit alert")
    elif fault["kind"] == "current_limit":
        measured = _finite(fault["measured_value"])
        if (fault["raw_code"] is not None or fault["limit_unit"] != "A" or
                abs(measured) > 1 or limit > 1 or measured * fault["sign"] <= limit):
            raise ValueError("Invalid stopped scan current")
    else:
        code = fault["raw_code"]
        if (isinstance(code, bool) or not isinstance(code, int) or
                fault["limit_unit"] != "V" or fault["measured_value"] is not None):
            raise ValueError("Invalid stopped scan ADC code")
        if fault["kind"] == "shunt_saturation":
            expected = 32767 if fault["sign"] == 1 else 32768
            if code != expected or abs(limit - expected * 0.0000025) > 1e-6:
                raise ValueError("Invalid stopped scan shunt limit")
        elif code != 32767 or fault["sign"] != 1 or abs(limit - 40.95875) > 1e-4:
            raise ValueError("Invalid stopped scan bus limit")
    return {key: fault[key] for key in keys}


class RamScan:
    def __init__(self, buffer, settings, calibration, device_id, scan_id, created_utc, started_ms,
                 calibration_sha256=None):
        if not isinstance(buffer, PackedScanBuffer) or len(buffer) or buffer.frozen:
            raise ValueError("A new scan requires an empty writable RAM buffer")
        if not _safe_id(device_id, 64) or not _safe_id(scan_id):
            raise ValueError("Invalid scan identifier")
        _validate_utc(created_utc)
        if isinstance(started_ms, bool) or not isinstance(started_ms, int) or started_ms < 0:
            raise ValueError("Invalid scan start time")
        normalized = model.validate_settings(settings)
        self._settings = {key: normalized[key] for key in model.DEFAULT_SETTINGS}
        self._drains = model.sweep_uv(self._settings["drain_start_uv"], self._settings["drain_stop_uv"], self._settings["drain_step_uv"])
        self._gates = model.sweep_uv(self._settings["gate_start_uv"], self._settings["gate_stop_uv"], self._settings["gate_step_uv"])
        if len(self._drains) * len(self._gates) > buffer.capacity:
            raise ValueError("Scan exceeds RAM buffer capacity")
        if not isinstance(calibration, dict) or not isinstance(calibration.get("metadata", {}), dict):
            raise ValueError("Invalid calibration metadata")
        model.validate_calibration_acquisition(calibration.get("metadata", {}), self._settings)
        if calibration_sha256 is None:
            calibration_sha256 = calibration_hash(calibration)
        if not _digest_valid(calibration_sha256):
            raise ValueError("Invalid calibration fingerprint")
        header = {"schema_version": 3, "scan_id": scan_id, "device_id": device_id,
                  "created_utc": created_utc, "settings": self._settings,
                  "acquisition": model.acquisition_metadata(self._settings, blocks_per_point=1),
                  "calibration": {"sha256": calibration_sha256, "metadata": calibration.get("metadata", {})},
                  "columns": COLUMNS}
        chunks = _json_chunks(header, HEADER_BYTES)
        self._header = chunks[:-1] + (chunks[-1][:-1], b',"points":[')
        self._buffer, self._record_revision = buffer, buffer.revision
        self._device_id, self._scan_id, self._created_utc = device_id, scan_id, created_utc
        self._started_ms = started_ms
        # Allocate instance fields now, before the acquisition heap fragments.
        self._tail, self._fault, self._outcome, self._duration = (), None, None, None
        self._point_count, self._seal_revision, self._sealed = 0, None, False
        self._prepare_reader, self._prepare_hash, self._prepare_size = None, None, 0
        self._wire_size, self._sha256 = None, None

    @property
    def buffer(self):
        return self._buffer

    @property
    def settings(self):
        return dict(self._settings)

    @property
    def scan_id(self):
        return self._scan_id

    @property
    def device_id(self):
        return self._device_id

    @property
    def created_utc(self):
        return self._created_utc

    @property
    def point_count(self):
        return self._point_count if self._sealed else len(self._buffer)

    @property
    def outcome(self):
        return self._outcome

    @property
    def fault(self):
        return dict(self._fault) if self._fault is not None else None

    @property
    def wire_size(self):
        return self._wire_size

    @property
    def sha256(self):
        return self._sha256

    @property
    def sealed(self):
        return self._sealed

    def _check_recording(self):
        if self._sealed or self._buffer.frozen or self._buffer.revision != self._record_revision:
            raise ValueError("Scan buffer changed or is already complete")

    def _check_sealed(self):
        if not self._sealed:
            raise ValueError("Scan has not completed")
        if (not self._buffer.frozen or self._buffer.revision != self._seal_revision or
                len(self._buffer) != self._point_count):
            raise ValueError("Completed scan data changed")

    def append(self, row):
        self._check_recording()
        if not isinstance(row, dict):
            raise ValueError("Scan point must be an object")
        count = len(self._buffer)
        curve, drain_index = divmod(count, len(self._drains))
        if count >= len(self._drains) * len(self._gates):
            raise ValueError("Unexpected scan point")
        for key, expected in (("index", count), ("curve", curve),
                              ("acquisition_blocks_completed", 1)):
            actual = row.get(key)
            if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
                raise ValueError("Invalid scan point position or averaging")
        for key, target_uv in (("requested_vds", self._drains[drain_index]),
                               ("requested_vgs", self._gates[curve])):
            target = model.volts_from_uv(target_uv)
            if abs(_finite(row.get(key)) - target) > 1e-5 * max(1, abs(target)):
                raise ValueError("Invalid requested scan voltage")
        for key, maximum in (("vds", 36), ("vgs", 36), ("id_a", 1), ("ig_a", 1)):
            if abs(_finite(row.get(key))) > maximum:
                raise ValueError("Invalid scan measurement")
        self._buffer.append(row)
        self._record_revision = self._buffer.revision

    def seal(self, now_ms, fault=None):
        self._check_recording()
        count = len(self._buffer)
        expected = len(self._drains) * len(self._gates)
        if fault is None and count != expected:
            raise ValueError("Incomplete scan cannot be saved")
        saved_fault = _validate_fault(fault, count, self._drains, self._gates) if fault is not None else None
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise ValueError("Invalid scan finish time")
        duration = _ticks_diff(now_ms, self._started_ms)
        if not 0 <= duration <= 0xffffffff:
            raise ValueError("Invalid scan duration")
        outcome = "partial" if fault is not None else "complete"
        tail = _json_chunks({"outcome": outcome, "fault": saved_fault,
                             "point_count": count, "duration_ms": duration}, TRAILER_BYTES)
        tail = (b'],', tail[0][1:]) + tail[1:]
        self._buffer.freeze()
        self._seal_revision, self._point_count = self._buffer.revision, count
        self._tail, self._fault, self._outcome, self._duration = tail, saved_fault, outcome, duration
        self._sealed = True

    def prepare_step(self, maxbytes=CHUNK_BYTES):
        if isinstance(maxbytes, bool) or not isinstance(maxbytes, int) or not 1 <= maxbytes <= CHUNK_BYTES:
            raise ValueError("Preparation reads must be between 1 and 256 bytes")
        self._check_sealed()
        if self._sha256 is not None:
            return True
        try:
            if self._prepare_reader is None:
                self._prepare_hash = hashlib.sha256()
                self._prepare_reader = PayloadReader(self, _preparing=True)
                self._prepare_size = 0
            chunk = self._prepare_reader.read(maxbytes)
            if chunk:
                self._prepare_hash.update(chunk)
                self._prepare_size += len(chunk)
                if self._prepare_size > MAX_WIRE_BYTES:
                    raise ValueError("Scan payload exceeds upload limit")
                return False
            digest = _hex(self._prepare_hash.digest())
            self._prepare_reader.close()
            self._prepare_reader, self._prepare_hash = None, None
            self._wire_size, self._sha256 = self._prepare_size, digest
            return True
        except Exception:
            if self._prepare_reader is not None:
                self._prepare_reader.close()
            self._prepare_reader, self._prepare_hash, self._prepare_size = None, None, 0
            raise


class PayloadReader:
    def __init__(self, scan, _preparing=False):
        if not isinstance(scan, RamScan):
            raise ValueError("Invalid RAM scan source")
        scan._check_sealed()
        if not _preparing and scan.wire_size is None:
            raise ValueError("Scan payload is not prepared")
        self._scan, self.size = scan, scan.wire_size
        self._header_index, self._point_index, self._tail_index = 0, 0, 0
        self._piece, self._offset = b"", 0

    def _next_piece(self):
        scan = self._scan
        if self._header_index < len(scan._header):
            piece = scan._header[self._header_index]
            self._header_index += 1
            return piece
        if self._point_index < scan._point_count:
            index = self._point_index
            curve, drain_index = divmod(index, len(scan._drains))
            vds, vgs, id_a, ig_a, flags = scan.buffer.get(index)
            piece = ("%s[%d,%d,%s,%s,%.9g,%.9g,%.9g,%.9g,%s,%s,%s]" %
                     ("," if index else "", index, curve,
                      model.format_voltage_uv(scan._drains[drain_index]),
                      model.format_voltage_uv(scan._gates[curve]), vds, vgs, id_a, ig_a,
                      "true" if flags & 1 else "false", "true" if flags & 2 else "false",
                      "true" if flags & 4 else "false")).encode("ascii")
            if len(piece) > CHUNK_BYTES:
                raise ValueError("Scan point exceeds wire limit")
            self._point_index += 1
            return piece
        if self._tail_index < len(scan._tail):
            piece = scan._tail[self._tail_index]
            self._tail_index += 1
            return piece
        return None

    def read(self, size=1024):
        if self._scan is None:
            raise ValueError("Scan reader is closed")
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 1024:
            raise ValueError("Payload reads must be between 1 and 1024 bytes")
        self._scan._check_sealed()
        result = bytearray(size)
        used = 0
        while used < size:
            if self._offset == len(self._piece):
                piece = self._next_piece()
                if piece is None:
                    break
                self._piece, self._offset = piece, 0
                if not piece:
                    continue
            take = min(size - used, len(self._piece) - self._offset)
            result[used:used + take] = memoryview(self._piece)[self._offset:self._offset + take]
            self._offset += take
            used += take
        return bytes(memoryview(result)[:used])

    def close(self):
        self._scan, self._piece = None, b""
