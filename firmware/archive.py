"""Compact binary scan archive with deterministic streaming JSON uploads."""

import os
import json
import math
import hashlib
import binascii
import struct
import io

try:
    from time import ticks_diff as _ticks_diff
except ImportError:
    def _ticks_diff(now, before):
        return now - before

try:
    from . import model, storage
except ImportError:
    import model, storage

COLUMNS = ["index", "curve", "requested_vds", "requested_vgs", "vds", "vgs",
           "id_a", "ig_a", "id_std_a", "ig_std_a", "drain_shunt_v",
           "gate_shunt_v", "drain_shunt_std_v", "gate_shunt_std_v",
           "batches_completed", "drain_voltage_limited", "gate_voltage_limited"]
MAX_PENDING = 20
KEEP_SENT = 5
MAGIC_V1 = b"PMSC\x01\x00\x00\x00"
MAGIC = b"PMSC\x02\x00\x00\x00"
PREFIX_FORMAT = "<8sI"
PREFIX_BYTES = 12
RECORD_FORMAT = "<HH12fBB"
RECORD_BYTES = 54
FOOTER_FORMAT = "<III"
FOOTER_BYTES = 12
TRAILER_BYTES = 1024
WIRE_POINT_BYTES = 500
MAX_WIRE_BYTES = 2 * 1024 * 1024
HEADER_BYTES = 4096
HEADER_CHUNK_BYTES = 256
RESERVE_BYTES = 32768
_COLUMNS_JSON = ('["' + '","'.join(COLUMNS) + '"]').encode("ascii")


class ArchiveCapacityError(RuntimeError):
    """A fresh queue/space check blocked acquisition before a draft opened."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Upload queue is full; upload saved scans first" if reason == "queue"
                         else "Not enough flash space for this scan")


class ArchiveReadError(RuntimeError):
    """A fixed category survives wrapping without exposing file contents."""
    def __init__(self, kind):
        self.kind = kind
        super().__init__("Saved scan is invalid or damaged")


def _error_kind(error):
    if isinstance(error, MemoryError):
        return "memory"
    if isinstance(error, OSError):
        return "storage"
    if isinstance(error, ArchiveReadError):
        return error.kind
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return "invalid"
    return "other"


def _hex(value):
    return binascii.hexlify(value).decode()


def _sync():
    if hasattr(os, "sync"):
        os.sync()


def _replace(source, target):
    getattr(os, "replace", os.rename)(source, target)


def _safe_id(value, maximum=96):
    return (isinstance(value, str) and 1 <= len(value) <= maximum and
            all(character in "0123456789abcdefABCDEF-_" for character in value))


class _HashWriter(io.IOBase):
    # MicroPython json.dump requires the native stream protocol. IOBase
    # provides it; a CPython-only duck-typed write() object is insufficient.
    def __init__(self):
        self.hash = hashlib.sha256()

    def write(self, value):
        self.hash.update(value.encode("utf-8") if isinstance(value, str) else value)
        return len(value)


class _HeaderWriter(io.IOBase):
    """Count UTF-8 bytes or stream them through one bounded staging buffer.

    json.dump uses MicroPython's native stream protocol. The counting pass
    allocates no header buffer; the writing pass batches small serializer
    writes without building a full JSON string and a second UTF-8 copy.
    """
    def __init__(self, target=None):
        self.count = 0
        self._target = target
        self._used = 0
        self._buffer = bytearray(256) if target is not None else None
        self._view = memoryview(self._buffer) if target is not None else None

    def _consume(self, value):
        self.count += len(value)
        if self.count > HEADER_BYTES:
            raise ValueError("Archive header too large")
        if self._target is None:
            return
        view = memoryview(value)
        offset = 0
        while offset < len(view):
            take = min(256 - self._used, len(view) - offset)
            self._view[self._used:self._used + take] = view[offset:offset + take]
            self._used += take
            offset += take
            if self._used == 256:
                self._target(self._view)
                self._used = 0

    def write(self, value):
        if isinstance(value, str):
            # Bound UTF-8 encoding even when a serializer emits a long string
            # token. A Unicode code point needs at most four UTF-8 bytes.
            for offset in range(0, len(value), 64):
                self._consume(value[offset:offset + 64].encode("utf-8"))
        else:
            self._consume(value)
        return len(value)

    def finish(self):
        if self._used:
            self._target(self._view[:self._used])
            self._used = 0


def calibration_hash(calibration):
    writer = _HashWriter()
    json.dump(calibration, writer)
    return _hex(writer.hash.digest())


class _HeaderReader(io.IOBase):
    """Present exactly the JSON header to MicroPython's streaming json.load.

    The native IOBase stream protocol calls readinto, never read(). Neither
    the header bytes nor a decoded duplicate are retained on the Pico. A small
    read-ahead buffer also avoids a filesystem call for every parser byte.
    """
    def __init__(self, stream, length):
        self.stream, self.remaining = stream, length
        self._buffer, self._offset = b"", 0

    def readinto(self, target):
        take = min(len(target), self.remaining)
        if not take:
            return 0
        if self._offset == len(self._buffer):
            self._buffer = self.stream.read(min(HEADER_CHUNK_BYTES, self.remaining))
            self._offset = 0
            if not self._buffer:
                raise ValueError("Truncated scan header")
        take = min(take, len(self._buffer) - self._offset)
        target[:take] = memoryview(self._buffer)[self._offset:self._offset + take]
        self._offset += take
        self.remaining -= take
        return take

    def read(self, size=-1):
        # CPython json.load calls read() once and materializes its input. This
        # compatibility method is not used by MicroPython's native stream
        # parser. Underlying file reads remain bounded on both runtimes.
        size = self.remaining if size < 0 else min(size, self.remaining)
        parts = []
        buffer = bytearray(min(HEADER_CHUNK_BYTES, size))
        view = memoryview(buffer)
        while size:
            count = self.readinto(view[:min(len(view), size)])
            parts.append(bytes(view[:count]))
            size -= count
        return b"".join(parts)


def _read_header(stream, length):
    # The saved format ends its header with '}', with no trailing whitespace.
    # Check this independently of parser read-ahead and reject a short file.
    stream.seek(PREFIX_BYTES + length - 1)
    if stream.read(1) != b"}":
        raise ValueError("Invalid scan header boundary")
    stream.seek(PREFIX_BYTES)
    reader = _HeaderReader(stream, length)
    header = json.load(reader)
    if reader.remaining or not isinstance(header, dict):
        raise ValueError("Invalid scan header")
    return header


def _digest_valid(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(character in "0123456789abcdef" for character in value))


def _metadata(path):
    if os.stat(path)[6] > 1024:
        raise ValueError("Invalid scan metadata")
    value = storage.load_json(path)
    if (not isinstance(value, dict) or value.get("format_version") != 1
            or not _safe_id(value.get("scan_id"))
            or not _digest_valid(value.get("sha256"))
            or not _digest_valid(value.get("binary_sha256"))
            or isinstance(value.get("point_count"), bool)
            or not isinstance(value.get("point_count"), int)
            or not 0 <= value["point_count"] <= model.MAX_POINTS
            or isinstance(value.get("wire_size"), bool) or not isinstance(value.get("wire_size"), int)
            or not 1 <= value["wire_size"] <= MAX_WIRE_BYTES):
        raise ValueError("Invalid scan metadata")
    if "ack_order" in value and (isinstance(value["ack_order"], bool)
            or not isinstance(value["ack_order"], int) or not 1 <= value["ack_order"] <= 0x7fffffff):
        raise ValueError("Invalid acknowledgement order")
    if value.get("metadata_sha256") != _metadata_hash(value):
        raise ValueError("Invalid scan metadata")
    return value


def _metadata_hash(value):
    payload = "%s|%s|%s|%d|%d" % (value["scan_id"], value["binary_sha256"],
                                    value["sha256"], value["wire_size"], value["point_count"])
    # Existing queued/sent files without this optional field keep their original
    # metadata digest. Acknowledgement bookkeeping never changes the wire body.
    if "ack_order" in value:
        payload += "|%d" % value["ack_order"]
    digest = hashlib.sha256()
    digest.update(payload.encode("ascii"))
    return _hex(digest.digest())


def _validate_record(values, index, batches):
    if values[0] != index or not 0 <= values[1] < model.MAX_POINTS:
        raise ValueError("Invalid binary point index")
    if values[14] != batches or values[15] & ~3:
        raise ValueError("Invalid binary point flags")
    for offset in range(2, 14):
        value = values[offset]
        if not math.isfinite(value) or abs(value) > (36.0 if offset < 6 else 1.0):
            raise ValueError("Invalid binary measurement")
    if any(values[offset] < 0 for offset in (8, 9, 12, 13)):
        raise ValueError("Invalid binary standard deviation")


def _scan_shape(settings):
    return [model.sweep_uv(settings[role + "_start_uv"], settings[role + "_stop_uv"],
                           settings[role + "_step_uv"]) for role in ("drain", "gate")]


def _validate_fault(fault, count, settings, strict=False):
    keys = ("channel", "kind", "raw_code", "sign", "limit_value", "limit_unit",
            "measured_value", "index", "curve", "requested_vds", "requested_vgs", "phase")
    if not isinstance(fault, dict) or len(fault) != len(keys) or any(key not in fault for key in keys):
        raise ValueError("Invalid stopped scan diagnostic")
    if (fault["channel"] not in ("drain", "gate") or fault["kind"] not in
            ("shunt_saturation", "bus_saturation", "current_limit") or
            fault["phase"] not in ("drive", "park_drain", "settle_gate", "settle", "sample")):
        raise ValueError("Invalid stopped scan diagnostic")
    for key in ("sign", "index", "curve"):
        if isinstance(fault[key], bool) or not isinstance(fault[key], int):
            raise ValueError("Invalid stopped scan diagnostic")
    if fault["sign"] not in (-1, 1) or fault["index"] != count:
        raise ValueError("Invalid stopped scan diagnostic")
    for key in ("limit_value", "requested_vds", "requested_vgs"):
        value = fault[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError("Invalid stopped scan diagnostic")
    if not 0 < fault["limit_value"] <= 100:
        raise ValueError("Invalid stopped scan diagnostic")
    if not 0 <= count < model.MAX_POINTS or not 0 <= fault["curve"] <= count:
        raise ValueError("Invalid stopped scan position")
    for key, role in (("requested_vds", "drain"), ("requested_vgs", "gate")):
        target = fault[key]
        tolerance = 1e-5 * max(1, abs(target))
        if not (model.setting_voltage_v(settings, role + "_start_v") - tolerance <= target <=
                model.setting_voltage_v(settings, role + "_stop_v") + tolerance):
            raise ValueError("Invalid stopped scan target")
    # New snapshots have an exact integer grid. Legacy readers retain the
    # recorded count: historical float32/float64 grids could disagree by one
    # interval at a nominal endpoint.
    if strict:
        drain, gate = _scan_shape(settings)
        if count >= len(drain) * len(gate) or fault["curve"] != count // len(drain):
            raise ValueError("Invalid stopped scan position")
        for key, target in (("requested_vds", drain[count % len(drain)]),
                            ("requested_vgs", gate[count // len(drain)])):
            target = model.volts_from_uv(target)
            if abs(fault[key] - target) > 1e-5 * max(1, abs(target)):
                raise ValueError("Invalid stopped scan target")
    if fault["kind"] == "current_limit":
        value = fault["measured_value"]
        if (fault["raw_code"] is not None or fault["limit_unit"] != "A" or
                isinstance(value, bool) or not isinstance(value, (float, int)) or
                not math.isfinite(value) or abs(value) > 1 or fault["limit_value"] > 1 or
                value * fault["sign"] <= fault["limit_value"]):
            raise ValueError("Invalid stopped scan current")
    elif (isinstance(fault["raw_code"], bool) or not isinstance(fault["raw_code"], int) or
            not 0 <= fault["raw_code"] <= 65535 or fault["limit_unit"] != "V" or
            fault["measured_value"] is not None or
            (fault["kind"] == "bus_saturation" and fault["sign"] != 1)):
        raise ValueError("Invalid stopped scan ADC code")
    if fault["kind"] == "shunt_saturation":
        code = 32767 if fault["sign"] == 1 else 32768
        if fault["raw_code"] != code or abs(fault["limit_value"] - code * 0.0000025) > 1e-6:
            raise ValueError("Invalid stopped scan shunt limit")
    elif fault["kind"] == "bus_saturation":
        if fault["raw_code"] != 32767 or abs(fault["limit_value"] - 40.95875) > 1e-4:
            raise ValueError("Invalid stopped scan bus limit")


def _read_tail(stream, file_size, data_start, version, settings):
    """Read a bounded final diagnostic; never infer points from partial bytes."""
    footer_bytes = 8 if version == 1 else FOOTER_BYTES
    if file_size < data_start + footer_bytes:
        raise ValueError("Invalid scan length")
    stream.seek(file_size - footer_bytes)
    footer = stream.read(footer_bytes)
    values = struct.unpack("<II" if version == 1 else FOOTER_FORMAT, footer)
    count, duration = values[:2]
    trailer = b""
    outcome, fault = "complete", None
    if version == 2:
        length = values[2]
        if not 1 <= length <= TRAILER_BYTES or file_size < data_start + footer_bytes + length:
            raise ValueError("Invalid scan diagnostic length")
        stream.seek(file_size - footer_bytes - length)
        trailer = stream.read(length)
        if not trailer.startswith(b"{") or not trailer.endswith(b"}"):
            raise ValueError("Invalid scan diagnostic")
        value = json.loads(trailer.decode("utf-8"))
        if not isinstance(value, dict) or len(value) != 2 or "outcome" not in value or "fault" not in value:
            raise ValueError("Invalid scan diagnostic")
        outcome, fault = value["outcome"], value["fault"]
    if file_size != data_start + count * RECORD_BYTES + len(trailer) + footer_bytes:
        raise ValueError("Invalid scan record count")
    if outcome == "complete":
        if not 1 <= count <= model.MAX_POINTS or fault is not None:
            raise ValueError("Incomplete scan")
        if settings["version"] == 2 and count != model.scan_point_count(settings):
            raise ValueError("Incomplete scan")
    elif version == 2 and outcome == "partial":
        _validate_fault(fault, count, settings, strict=settings["version"] == 2)
    else:
        raise ValueError("Invalid scan outcome")
    return count, duration, trailer, footer, outcome, fault


class UploadSource:
    """Read at most 1024 JSON bytes at a time from a verified binary scan.

    The sidecar hashes the complete binary file. Validate it before a caller
    can send any bytes. Stored header bytes and nine significant decimal digits
    for each float32 make subsequent reads/retries reproduce the same payload.
    verify=False is private to finish_scan, before its sidecar exists.
    """

    def __init__(self, path, verify=True):
        self._stream = None
        try:
            self.size = 0
            if verify:
                # Each LittleFS file handle needs a contiguous cache. Read and
                # close the sidecar before opening the binary stream, rather
                # than retaining two file handles during verification.
                info = _metadata(path.rsplit(".", 1)[0] + ".meta")
                expected_scan_id = info["scan_id"]
                expected_count = info["point_count"]
                self.size = info["wire_size"]
                expected_binary_sha = info["binary_sha256"]
                del info
            self._stream = open(path, "rb")
            prefix = self._stream.read(PREFIX_BYTES)
            if len(prefix) != PREFIX_BYTES:
                raise ValueError()
            magic, header_length = struct.unpack(PREFIX_FORMAT, prefix)
            if magic not in (MAGIC_V1, MAGIC) or not 1 <= header_length <= HEADER_BYTES:
                raise ValueError()
            self._header_length = header_length
            header = _read_header(self._stream, header_length)
            self._version = 1 if magic == MAGIC_V1 else 2
            if header.get("schema_version") != self._version or not _safe_id(header.get("scan_id")):
                raise ValueError()
            self.scan_id = header["scan_id"]
            settings = model.validate_archive_settings(header["settings"])
            # Parsed calibration metadata is only needed to validate the
            # header. Release that duplicate tree before parsing the trailer
            # and allocating the sidecar/hash verification workspace.
            del header
            self._batches = settings["batches"]
            self._data_start = PREFIX_BYTES + header_length
            file_size = os.stat(path)[6]
            (self._count, self._duration, self._trailer, footer, self.outcome,
             self.fault) = _read_tail(self._stream, file_size, self._data_start, self._version, settings)
            del settings
            count = self._count
            self.point_count = count
            if verify:
                if expected_scan_id != self.scan_id or expected_count != count:
                    raise ValueError()
                digest = hashlib.sha256()
                digest.update(prefix)
                self._stream.seek(PREFIX_BYTES)
                remaining = header_length
                while remaining:
                    block = self._stream.read(min(HEADER_CHUNK_BYTES, remaining))
                    if not block:
                        raise ValueError("Truncated scan header")
                    digest.update(block)
                    remaining -= len(block)
                self._stream.seek(self._data_start)
                for index in range(count):
                    record = self._stream.read(RECORD_BYTES)
                    _validate_record(struct.unpack(RECORD_FORMAT, record), index, self._batches)
                    digest.update(record)
                digest.update(self._trailer)
                digest.update(footer)
                if _hex(digest.digest()) != expected_binary_sha:
                    raise ValueError()
            self._stream.seek(PREFIX_BYTES)
            self._index, self._stage = 0, 0
            self._header_offset = 0
            self._piece, self._offset = b"", 0
        except Exception as error:
            kind = _error_kind(error)
            self.close()
            if kind == "memory":
                raise MemoryError("Saved scan needs memory")
            raise ArchiveReadError(kind if kind in ("storage", "invalid") else "invalid")

    def _next_piece(self):
        if self._stage == 0:
            remaining = self._header_length - 1 - self._header_offset
            if remaining:
                piece = self._stream.read(min(HEADER_CHUNK_BYTES, remaining))
                if not piece:
                    raise ArchiveReadError("invalid")
                self._header_offset += len(piece)
                return piece
            self._stage = 1
            self._stream.seek(self._data_start)
            return b',"columns":' + _COLUMNS_JSON + b',"points":['
        if self._index < self._count:
            record = self._stream.read(RECORD_BYTES)
            if len(record) != RECORD_BYTES:
                raise RuntimeError("Saved scan is invalid or damaged")
            values = struct.unpack(RECORD_FORMAT, record)
            _validate_record(values, self._index, self._batches)
            row = [str(values[0]), str(values[1])]
            row.extend("%.9g" % value for value in values[2:14])
            row.extend((str(values[14]), "true" if values[15] & 1 else "false",
                        "true" if values[15] & 2 else "false"))
            result = ("," if self._index else "") + "[" + ",".join(row) + "]"
            self._index += 1
            encoded = result.encode("ascii")
            if len(encoded) > WIRE_POINT_BYTES:
                raise RuntimeError("Saved scan exceeds upload limits")
            return encoded
        if self._stage == 1:
            self._stage = 2
            if self._version == 2:
                return (b'],' + self._trailer[1:-1] +
                        (',"point_count":%d,"duration_ms":%d}' % (self._count, self._duration)).encode("ascii"))
            return ('],"outcome":"complete","point_count":%d,"duration_ms":%d}'
                    % (self._count, self._duration)).encode("ascii")
        return b""

    def read(self, count=1024):
        if not isinstance(count, int) or not 1 <= count <= 1024:
            raise ValueError("Upload reads must be between 1 and 1024 bytes")
        # Preallocate exactly the requested bound. Repeated bytearray.extend
        # can grow its capacity beyond it on MicroPython's fragmented heap.
        result = bytearray(count)
        view, used = memoryview(result), 0
        while used < count:
            if self._offset >= len(self._piece):
                self._piece, self._offset = self._next_piece(), 0
                if not self._piece:
                    break
            take = min(count - used, len(self._piece) - self._offset)
            view[used:used + take] = memoryview(self._piece)[self._offset:self._offset + take]
            self._offset += take
            used += take
        return bytes(view[:used])

    def close(self):
        if self._stream is not None:
            stream, self._stream = self._stream, None
            stream.close()


class Archive:
    def __init__(self, directory="scans", device_id=None, free_bytes=None,
                 random_bytes=None, utc_provider=None):
        if device_id is None:
            from machine import unique_id
            device_id = _hex(unique_id())
        if (not isinstance(device_id, str) or not 1 <= len(device_id) <= 64
                or not all(character in "0123456789abcdefABCDEF" for character in device_id)):
            raise ValueError("Invalid archive device identifier")
        self.directory, self.device_id = directory.rstrip("/"), device_id
        self._free_bytes = free_bytes
        self._random_bytes = random_bytes or os.urandom
        self._utc_provider = utc_provider
        self._stream = None
        self._current = None
        self._accepted_id = None
        self._calibration_object = None
        self._calibration_sha = None
        self.last_finalize_error = None
        self.state = "idle"
        try:
            os.mkdir(self.directory)
        except OSError:
            # An existing directory is expected; any other error remains fatal.
            try:
                if not os.stat(self.directory)[0] & 0x4000:
                    raise OSError()
            except OSError:
                raise RuntimeError("Scan archive could not be opened")

    def _path(self, scan_id, suffix):
        if not _safe_id(scan_id):
            raise RuntimeError("Scan archive metadata is invalid")
        return self.directory + "/" + scan_id + suffix

    def invalidate_calibration(self):
        """Explicitly discard a fingerprint before any in-place edit."""
        self._calibration_object = None
        self._calibration_sha = None

    def prepare_calibration(self, calibration):
        """Cache the existing streamed JSON fingerprint of an immutable object.

        UI calibration replacement supplies a different object. Retaining that
        object prevents identity reuse; callers that deliberately edit it in
        place must invalidate first. Never substitute the on-disk file hash.
        """
        if calibration is None:
            self.invalidate_calibration()
            return None
        if calibration is self._calibration_object and self._calibration_sha is not None:
            return self._calibration_sha
        if not isinstance(calibration, dict):
            raise ValueError("Invalid calibration fingerprint input")
        fingerprint = calibration_hash(calibration)
        # Publish together only after serialization has completed successfully.
        self._calibration_object, self._calibration_sha = calibration, fingerprint
        return fingerprint

    @staticmethod
    def _required_bytes(expected):
        return (PREFIX_BYTES + HEADER_BYTES + expected * RECORD_BYTES +
                TRAILER_BYTES + FOOTER_BYTES + RESERVE_BYTES)

    def _names(self, suffix):
        return [name[:-len(suffix)] for name in os.listdir(self.directory)
                if name.endswith(suffix) and _safe_id(name[:-len(suffix)])]

    @property
    def pending_count(self):
        return len(self._names(".pending"))

    @property
    def saved_count(self):
        return self.pending_count + len(self._names(".sent"))

    def _available(self):
        if self._free_bytes is not None:
            return self._free_bytes()
        stat = os.statvfs(self.directory)
        return stat[1] * stat[4]

    def _file_size(self, path, missing_ok=False):
        try:
            # LittleFS has no symlinks. On a host, do not follow one while
            # inspecting a downloaded archive or counting reclaimable space.
            info = getattr(os, "lstat", os.stat)(path)
            if info[0] & 0xf000 != 0x8000:
                raise ValueError("Invalid archive file")
            return info[6]
        except OSError as error:
            if missing_ok and error.args and error.args[0] == 2:
                return 0
            raise

    def _accepted_size(self, scan_id, suffix):
        return (self._file_size(self._path(scan_id, suffix)) +
                self._file_size(self._path(scan_id, ".meta"), missing_ok=True))

    def _reclaimable_sent(self, pending=None):
        pending = self._names(".pending") if pending is None else pending
        entries = []
        for scan_id in self._names(".sent"):
            if scan_id in pending:
                continue
            try:
                size = self._accepted_size(scan_id, ".sent")
                info = _metadata(self._path(scan_id, ".meta"))
                if info["scan_id"] != scan_id:
                    continue
                stamp = os.stat(self._path(scan_id, ".sent"))[8]
                entries.append((info.get("ack_order", 0), stamp, scan_id, size))
            except Exception:
                # An unreadable item needs explicit selection, not automatic
                # deletion based on an untrusted acknowledgement record.
                continue
        entries.sort()
        return entries

    def _unaccepted_ids(self):
        # Use the same exclusions for the read-only preview and actual cleanup.
        # A sidecar shared by .writing/.draft is counted/deleted only once.
        accepted = self._names(".pending") + self._names(".sent")
        ids = set(self._names(".writing") + self._names(".draft"))
        return [scan_id for scan_id in ids
                if scan_id != self._current and scan_id not in accepted]

    def scan_capacity(self, settings):
        """Inspect space/queue limits without deleting or creating any files.

        Free space is measured now. Reclaimable bytes count acknowledged files
        and separately abandoned unaccepted work, using their logical sizes.
        begin_scan still checks actual free space after any normal reclamation.
        """
        try:
            settings = model.validate_settings(settings)
            expected = model.scan_point_count(settings)
            required = self._required_bytes(expected)
            pending, sent = self._names(".pending"), self._names(".sent")
            available = self._available()
            reclaimable = sum(entry[3] for entry in self._reclaimable_sent(pending))
            unaccepted = sum(self._file_size(self._path(scan_id, suffix), missing_ok=True)
                             for scan_id in self._unaccepted_ids()
                             for suffix in (".writing", ".draft", ".meta", ".meta.tmp"))
            reason = ("queue" if len(pending) >= MAX_PENDING else
                      "space" if available + reclaimable + unaccepted < required else None)
            return {"can_start": reason is None, "can_start_after_reclaim": reason is None,
                    "reason": reason, "required_bytes": required,
                    "free_bytes": available, "actual_free_bytes": available,
                    "reclaimable_bytes": reclaimable, "reclaimable_unaccepted_bytes": unaccepted,
                    "pending_count": len(pending),
                    "pending_limit": MAX_PENDING, "accepted_count": len(pending) + len(sent)}
        except Exception:
            raise RuntimeError("Scan storage capacity could not be checked")

    def _listing_info(self, scan_id, suffix):
        """Read bounded metadata/header/footer, never the measurement body."""
        path = self._path(scan_id, suffix)
        result = {"scan_id": scan_id, "status": suffix[1:], "size_bytes": 0,
                  "point_count": None, "created_utc": None, "outcome": None, "damaged": True}
        try:
            result["size_bytes"] = self._accepted_size(scan_id, suffix)
            info = _metadata(self._path(scan_id, ".meta"))
            if info["scan_id"] != scan_id:
                return result
            with open(path, "rb") as stream:
                prefix = stream.read(PREFIX_BYTES)
                if len(prefix) != PREFIX_BYTES:
                    return result
                magic, length = struct.unpack(PREFIX_FORMAT, prefix)
                if magic not in (MAGIC_V1, MAGIC) or not 1 <= length <= HEADER_BYTES:
                    return result
                header = _read_header(stream, length)
                version = 1 if magic == MAGIC_V1 else 2
                if (not isinstance(header, dict) or header.get("schema_version") != version or
                        header.get("scan_id") != scan_id):
                    return result
                count, unused, trailer, footer, outcome, fault = _read_tail(
                    stream, self._file_size(path), PREFIX_BYTES + length, version,
                    model.validate_archive_settings(header["settings"]))
                if count != info["point_count"]:
                    return result
            utc = header.get("created_utc")
            if utc is not None and (not isinstance(utc, str) or not 20 <= len(utc) <= 32 or
                    utc[4] != "-" or utc[7] != "-" or utc[10] != "T" or
                    utc[13] != ":" or utc[16] != ":" or utc[-1] != "Z" or
                    any(character not in "0123456789" for character in
                        utc[:4] + utc[5:7] + utc[8:10] + utc[11:13] + utc[14:16] + utc[17:19]) or
                    (len(utc) > 20 and (utc[19] != "." or not utc[20:-1] or
                        any(character not in "0123456789" for character in utc[20:-1])))):
                return result
            result.update(point_count=count, created_utc=utc, outcome=outcome, damaged=False)
        except Exception:
            # A damaged item must remain selectable for explicit deletion.
            # Its filename, never untrusted header/sidecar identity, is used.
            pass
        return result

    def list_scans(self):
        """List accepted scans oldest-file-first, including damaged entries."""
        try:
            entries = []
            for suffix in (".pending", ".sent"):
                for scan_id in self._names(suffix):
                    path = self._path(scan_id, suffix)
                    stamp = getattr(os, "lstat", os.stat)(path)[8]
                    entries.append((stamp, scan_id, suffix))
            entries.sort()
            return [self._listing_info(scan_id, suffix) for unused, scan_id, suffix in entries]
        except Exception:
            raise RuntimeError("Saved scans could not be listed")

    def delete_scan(self, scan_id):
        """Delete one explicitly selected accepted item; never infer a path."""
        try:
            if not _safe_id(scan_id) or scan_id == self._current:
                raise ValueError("Invalid selected scan")
            if any(scan_id in self._names(suffix) for suffix in (".writing", ".draft")):
                raise ValueError("Selected scan is in use")
            suffixes = [suffix for suffix in (".pending", ".sent")
                        if scan_id in self._names(suffix)]
            if len(suffixes) != 1:
                raise ValueError("Selected scan is missing or ambiguous")
            # Delete the payload first. If that fails, its metadata stays intact.
            # Corrupt metadata cannot redirect this operation to another scan.
            self._remove(self._path(scan_id, suffixes[0]))
            if self._accepted_id == scan_id:
                self._accepted_id = None
            self._remove(self._path(scan_id, ".meta"))
            _sync()
            return True
        except Exception:
            raise RuntimeError("Selected scan could not be deleted")

    def _remove(self, path):
        try:
            os.remove(path)
        except OSError as error:
            if not error.args or error.args[0] != 2:
                raise

    def _ordered(self, suffix):
        ids = self._names(suffix)
        if suffix == ".sent":
            # A cold Pico clock can make a newly captured file older than all
            # prior files. Retain by acknowledgement sequence, not capture mtime.
            ids.sort(key=lambda scan_id: (_metadata(self._path(scan_id, ".meta")).get("ack_order", 0),
                                         os.stat(self._path(scan_id, suffix))[8], scan_id))
        else:
            ids.sort(key=lambda scan_id: (os.stat(self._path(scan_id, suffix))[8], scan_id))
        return ids

    def _delete_sent(self, scan_id):
        # Remove the data first; never remove metadata for a pending payload.
        self._remove(self._path(scan_id, ".sent"))
        if scan_id not in self._names(".pending"):
            self._remove(self._path(scan_id, ".meta"))

    def _reclaim_unaccepted(self):
        # A reboot cannot turn an unaccepted scan into a queued upload. Such
        # abandoned working/draft files are reclaimed before a new acquisition.
        for scan_id in self._unaccepted_ids():
            for suffix in (".writing", ".draft", ".meta", ".meta.tmp"):
                self._remove(self._path(scan_id, suffix))

    def _reserve(self, required):
        available = self._available()
        if available >= required:
            return
        sent = self._reclaimable_sent()
        available = self._available()
        if available + sum(entry[3] for entry in sent) < required:
            # Preserve the previous capacity policy: do not evict accepted
            # files when even all eligible reclamation cannot make this fit.
            raise ArchiveCapacityError("space")
        while available < required and sent:
            self._delete_sent(sent.pop(0)[2])
            available = self._available()
        if available < required:
            raise ArchiveCapacityError("space")

    def _write(self, data):
        written = self._stream.write(data)
        if written is not None and written != len(data):
            raise OSError("Short archive write")
        self._hash.update(data)

    def _close(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def begin_scan(self, settings, calibration, now_ms):
        if self._current is not None:
            raise RuntimeError("Keep or discard the previous scan first")
        self.last_finalize_error = None
        try:
            # This retained writer describes software-averaged 17-column rows.
            # Never reinterpret a historical snapshot as triggered acquisition.
            settings = model.validate_archive_settings(settings)
            if settings["version"] == 3:
                raise ValueError("Triggered acquisition requires the RAM scan format")
            if settings["version"] == 1:
                # Preserve the previous writer's v1 -> microvolt-v2 behavior,
                # including its original software averaging counts.
                for field in model.VOLTAGE_FIELDS:
                    settings[field + "_uv"] = model.volts_to_uv(settings.pop(field + "_v"))
                settings["version"] = 2
                settings = model.validate_archive_settings(settings)
            expected = model.scan_point_count(settings)
            if self.pending_count >= MAX_PENDING:
                raise ArchiveCapacityError("queue")
            self._reclaim_unaccepted()
            # The authoritative check is fresh, after abandoned-work cleanup.
            # Full read-only previews enumerate sent metadata for the Storage
            # UI; ordinary preparation needs that walk only when space is low.
            self._reserve(self._required_bytes(expected))
            scan_id = None
            for unused in range(8):
                candidate = self.device_id + "-" + _hex(self._random_bytes(8))
                if len(candidate) > 96:
                    raise RuntimeError("Invalid archive identifier")
                if not any(candidate in self._names(suffix)
                           for suffix in (".writing", ".draft", ".pending", ".sent", ".meta")):
                    scan_id = candidate
                    break
            if scan_id is None:
                raise RuntimeError("A unique scan identifier could not be created")
            utc = self._utc_provider() if self._utc_provider is not None else None
            if utc is not None:
                if (not isinstance(utc, str) or not 20 <= len(utc) <= 32
                        or utc[4] != "-" or utc[7] != "-" or utc[10] != "T"
                        or utc[13] != ":" or utc[16] != ":" or utc[-1] != "Z"
                        or not all(character in "0123456789" for character in
                                   utc[:4] + utc[5:7] + utc[8:10] + utc[11:13] + utc[14:16] + utc[17:19])
                        or (len(utc) > 20 and (utc[19] != "." or
                            not utc[20:-1] or not all(character in "0123456789" for character in utc[20:-1])))):
                    raise ValueError("Invalid archive time")
            header = {"schema_version": 2, "scan_id": scan_id,
                      "device_id": self.device_id, "created_utc": utc,
                      "settings": settings,
                      "calibration": {"sha256": self.prepare_calibration(calibration),
                                      "metadata": calibration.get("metadata", {})}}
            counter = _HeaderWriter()
            json.dump(header, counter)
            header_length = counter.count
            self._current = scan_id
            self._accepted_id = None
            self._expected, self._count = expected, 0
            self._settings = settings
            self._started_ms = now_ms
            self._hash = hashlib.sha256()
            self._stream = open(self._path(scan_id, ".writing"), "wb")
            self.state = "writing"
            self._write(struct.pack(PREFIX_FORMAT, MAGIC, header_length))
            writer = _HeaderWriter(self._write)
            json.dump(header, writer)
            writer.finish()
            if writer.count != header_length:
                raise ValueError("Archive header changed while writing")
            self._stream.flush()
            return scan_id
        except RuntimeError:
            if self._current is not None:
                self.state = "error"
                self._close()
            raise
        except Exception:
            self.state = "error"
            self._close()
            raise RuntimeError("Scan archive could not be started")

    def record_point(self, row):
        if self.state != "writing" or self._stream is None:
            raise RuntimeError("No scan archive is open")
        try:
            values = [row[key] for key in COLUMNS]
            if self._count >= self._expected or values[0] != self._count:
                raise ValueError("Unexpected scan point")
            for index, value in enumerate(values):
                if index >= 15:
                    if not isinstance(value, bool):
                        raise ValueError("Invalid voltage limit flag")
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Invalid measurement")
            for index in (0, 1, 14):
                if values[index] != int(values[index]) or values[index] < 0:
                    raise ValueError("Invalid scan index")
            if values[14] != self._settings["batches"]:
                raise ValueError("Incomplete averaged scan point")
            if any(values[index] < 0 for index in (8, 9, 12, 13)):
                raise ValueError("Invalid standard deviation")
            flags = int(values[15]) | (int(values[16]) << 1)
            packed = struct.pack(RECORD_FORMAT, int(values[0]), int(values[1]),
                                 *values[2:14], int(values[14]), flags)
            _validate_record(struct.unpack(RECORD_FORMAT, packed), self._count, self._settings["batches"])
            self._write(packed)
            self._stream.flush()  # One completed point, never each raw sample.
            self._count += 1
        except Exception:
            self.state = "error"
            self._close()
            raise RuntimeError("Scan data could not be written")

    def finish_scan(self, now_ms, fault=None):
        self.last_finalize_error = None
        if self.state != "writing" or (fault is None and self._count != self._expected):
            self.last_finalize_error = ("validate", "invalid")
            raise RuntimeError("Incomplete scan cannot be saved")
        stage = "validate"
        try:
            if fault is not None:
                _validate_fault(fault, self._count, self._settings, strict=True)
            stage = "trailer"
            trailer = json.dumps({"outcome": "partial" if fault is not None else "complete",
                                  "fault": fault}).encode("utf-8")
            if len(trailer) > TRAILER_BYTES:
                raise ValueError("Scan diagnostic too large")
            duration = _ticks_diff(now_ms, self._started_ms)
            if not 0 <= duration <= 0xffffffff:
                raise ValueError("Invalid duration")
            stage = "write"
            self._write(trailer)
            self._write(struct.pack(FOOTER_FORMAT, self._count, duration, len(trailer)))
            self._stream.flush()
            self._close()
            _sync()
            stage = "source"
            source = UploadSource(self._path(self._current, ".writing"), verify=False)
            wire_hash, wire_size = hashlib.sha256(), 0
            try:
                stage = "stream"
                while True:
                    chunk = source.read(HEADER_CHUNK_BYTES)
                    if not chunk:
                        break
                    wire_hash.update(chunk)
                    wire_size += len(chunk)
                    if wire_size > MAX_WIRE_BYTES:
                        raise ValueError("Upload too large")
            finally:
                source.close()
            stage = "metadata"
            info = {"format_version": 1, "scan_id": self._current,
                    "sha256": _hex(wire_hash.digest()), "wire_size": wire_size,
                    "binary_sha256": _hex(self._hash.digest()), "point_count": self._count}
            info["metadata_sha256"] = _metadata_hash(info)
            storage.save_json_atomic(self._path(self._current, ".meta"), info)
            stage = "rename"
            _replace(self._path(self._current, ".writing"), self._path(self._current, ".draft"))
            _sync()
            self.state = "complete"
            return dict(info)
        except Exception as error:
            self.last_finalize_error = (stage, _error_kind(error))
            self.state = "error"
            self._close()
            raise RuntimeError("Completed scan could not be finalized")

    def accept_scan(self):
        if self._current is None and self._accepted_id is not None:
            return self._accepted_id
        if self.state != "complete":
            raise RuntimeError("Only a completed scan can be kept")
        scan_id = self._current
        try:
            if self.pending_count >= MAX_PENDING:
                raise RuntimeError("Upload queue is full")
            _replace(self._path(scan_id, ".draft"), self._path(scan_id, ".pending"))
            # Once renamed, Cancel must never delete this accepted payload,
            # even if the following filesystem sync reports a failure.
            self._accepted_id, self._current = scan_id, None
            self.state = "idle"
            _sync()
            return scan_id
        except Exception:
            raise RuntimeError("Scan could not be added to the upload queue")

    def discard_scan(self):
        try:
            self._close()
            if self._current is not None:
                scan_id = self._current
                for suffix in (".writing", ".draft"):
                    self._remove(self._path(scan_id, suffix))
                if scan_id not in self._names(".pending") and scan_id not in self._names(".sent"):
                    self._remove(self._path(scan_id, ".meta"))
                    self._remove(self._path(scan_id, ".meta.tmp"))
                self._current = None
            self.state = "idle"
            return True
        except Exception:
            raise RuntimeError("Unaccepted scan could not be discarded")

    def next_pending(self):
        try:
            ids = self._ordered(".pending")
            if not ids:
                return None
            scan_id = ids[0]
            path = self._path(scan_id, ".meta")
            info = _metadata(path)
            if info["scan_id"] != scan_id:
                raise ValueError("Invalid metadata")
            return {"scan_id": scan_id, "sha256": info["sha256"], "point_count": info["point_count"],
                    "path": self._path(scan_id, ".pending")}
        except Exception:
            raise RuntimeError("Queued scan metadata could not be read")

    def mark_sent(self, scan_id):
        try:
            info = _metadata(self._path(scan_id, ".meta"))
            if info["scan_id"] != scan_id:
                raise ValueError("Invalid acknowledgement identity")
            highest = 0
            for saved_id in self._names(".sent"):
                saved = _metadata(self._path(saved_id, ".meta"))
                highest = max(highest, saved.get("ack_order", 0))
            if highest >= 0x7fffffff:
                raise ValueError("Acknowledgement order is exhausted")
            info["ack_order"] = highest + 1
            info["metadata_sha256"] = _metadata_hash(info)
            # Persist order before publishing .sent. On any failure before the
            # rename, the pending file and its exact upload bytes remain usable.
            storage.save_json_atomic(self._path(scan_id, ".meta"), info)
            _replace(self._path(scan_id, ".pending"), self._path(scan_id, ".sent"))
            _sync()
            sent = self._ordered(".sent")
            while len(sent) > KEEP_SENT:
                self._delete_sent(sent.pop(0))
            return True
        except Exception:
            raise RuntimeError("Upload acknowledgement could not be saved")
