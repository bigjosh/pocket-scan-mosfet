"""Bounded binary calibration: packed float32 tables and lossless metadata.

Format v1: <8sHHI> magic, drain/gate counts, typed-JSON byte length;
typed metadata; drain then gate <4f> records; SHA256 of every preceding byte.
Only metadata is JSON, with float values encoded as exact binary64 hex nodes.
This avoids decimal parsing drift on MicroPython float32 builds. Electrical
validation remains model.validate_calibration's responsibility after loading.
"""
import binascii
import hashlib
import io
import json
import math
import struct

try:
    from .calibration_data import PackedPoints, MAX_CALIBRATION_POINTS
except ImportError:
    from calibration_data import PackedPoints, MAX_CALIBRATION_POINTS

MAGIC = b"PSMCAL\x01\x00"
HEADER_FORMAT = "<8sHHI"
HEADER_BYTES = 16
MAX_METADATA_BYTES = 8192
MAX_METADATA_NODES = 512
MAX_DEPTH = 12
CHUNK_BYTES = 256
ROLES = ("drain", "gate")
ENDPOINT_FIELDS = ("zero_min_v", "zero_max_v", "drive_v", "drive_duty")
_LITTLE_ENDIAN = struct.pack("I", 1) == b"\x01\x00\x00\x00"


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid binary calibration number")
    return value


def _write(stream, data):
    if stream.write(data) != len(data):
        raise OSError("Short binary calibration write")


class _MetadataWriter(io.IOBase):
    def __init__(self, stream=None, digest=None):
        self.stream, self.digest, self.count = stream, digest, 0

    def write(self, text):
        # json.dump uses the native stream protocol; bound UTF-8 staging too.
        for offset in range(0, len(text), 64):
            data = text[offset:offset + 64]
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.count += len(data)
            if self.count > MAX_METADATA_BYTES:
                raise ValueError("Binary calibration metadata is too large")
            if self.stream is not None:
                _write(self.stream, data)
                self.digest.update(data)
        return len(text)


def _typed_dump(value, stream, budget, depth=0):
    budget[0] += 1
    if depth > MAX_DEPTH or budget[0] > MAX_METADATA_NODES:
        raise ValueError("Binary calibration metadata is too complex")
    if isinstance(value, float):
        _finite(value)
        stream.write('["f",')
        json.dump(binascii.hexlify(struct.pack("<d", value)).decode("ascii"), stream)
        stream.write("]")
    elif isinstance(value, dict):
        if len(value) > 64 or any(not isinstance(key, str) for key in value):
            raise ValueError("Invalid binary calibration metadata object")
        stream.write('["d",[')
        for index, key in enumerate(sorted(value)):
            if index:
                stream.write(",")
            stream.write("[")
            json.dump(key, stream)
            stream.write(",")
            _typed_dump(value[key], stream, budget, depth + 1)
            stream.write("]")
        stream.write("]]")
    elif isinstance(value, (list, tuple)):
        if len(value) > 128:
            raise ValueError("Binary calibration metadata list is too large")
        stream.write('["l",[')
        for index, item in enumerate(value):
            if index:
                stream.write(",")
            _typed_dump(item, stream, budget, depth + 1)
        stream.write("]]")
    elif value is None or isinstance(value, (bool, int, str)):
        json.dump(value, stream)
    else:
        raise ValueError("Unsupported binary calibration metadata value")


def _typed_load(value, budget, depth=0):
    budget[0] += 1
    if depth > MAX_DEPTH or budget[0] > MAX_METADATA_NODES:
        raise ValueError("Binary calibration metadata is too complex")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Invalid typed calibration metadata")
    kind, items = value
    if kind == "f":
        if (not isinstance(items, str) or len(items) != 16 or
                any(char not in "0123456789abcdef" for char in items)):
            raise ValueError("Invalid calibration float encoding")
        return _finite(struct.unpack("<d", binascii.unhexlify(items))[0])
    if kind not in ("d", "l") or not isinstance(items, list) or len(items) > (64 if kind == "d" else 128):
        raise ValueError("Invalid calibration container encoding")
    if kind == "l":
        return [_typed_load(item, budget, depth + 1) for item in items]
    result = {}
    for pair in items:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or pair[0] in result:
            raise ValueError("Duplicate or invalid calibration metadata key")
        result[pair[0]] = _typed_load(pair[1], budget, depth + 1)
    return result


def _metadata_depth(data):
    # Bound container nesting before the native JSON parser can recurse.
    depth, tokens = 0, 0
    quoted = escaped = False
    for char in data:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char == 91:
            depth += 1
            tokens += 1
            if depth > 3 * MAX_DEPTH + 4 or tokens > MAX_METADATA_NODES * 3:
                raise ValueError("Binary calibration JSON is too complex")
        elif char == 93:
            depth -= 1
            if depth < 0:
                raise ValueError("Invalid binary calibration JSON")
        elif char in (123, 125):
            raise ValueError("Untyped binary calibration JSON object")
    if quoted or depth:
        raise ValueError("Truncated binary calibration JSON")


def _envelope(value):
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in (1, 2) or
            not isinstance(value.get("metadata"), dict) or not isinstance(value.get("channels"), dict) or
            set(value["channels"]) != set(ROLES)):
        raise ValueError("Invalid binary calibration root")
    root = dict(value)
    channels, endpoints, tables = {}, {}, []
    for role in ROLES:
        channel = value["channels"][role]
        if not isinstance(channel, dict):
            raise ValueError("Invalid binary calibration channel")
        points = channel.get("points")
        if not isinstance(points, PackedPoints) or not 2 <= len(points) <= MAX_CALIBRATION_POINTS:
            raise ValueError("Binary calibration requires packed points")
        tables.append(points)
        channels[role] = {key: item for key, item in channel.items() if key != "points"}
        sidecar = {}
        for key in ENDPOINT_FIELDS:
            number = points.value(0 if key.startswith("zero_") else -1, key)
            if number is not None:
                sidecar[key] = number
        _check_sidecar(sidecar, points, len(points))
        endpoints[role] = sidecar
    root["channels"] = channels
    return {"calibration": root, "endpoints": endpoints}, tables


def _check_table(points, count):
    previous_v = previous_duty = None
    for index in range(count):
        offset = index * 4
        terminal, shunt, deviation, duty = (points._values[offset + column] for column in range(4))
        for number in (terminal, shunt, deviation, duty):
            _finite(number)
        if deviation < 0 or not 0 <= duty <= 1:
            raise ValueError("Invalid binary calibration point")
        if index and (terminal <= previous_v or duty <= previous_duty):
            raise ValueError("Nonmonotonic binary calibration table")
        previous_v, previous_duty = terminal, duty


def _check_sidecar(sidecar, table, count):
    if not isinstance(sidecar, dict) or any(key not in ENDPOINT_FIELDS for key in sidecar):
        raise ValueError("Invalid binary calibration endpoint fields")
    for number in sidecar.values():
        _finite(number)
    for low, high in (("zero_min_v", "zero_max_v"), ("drive_v", "drive_duty")):
        if (low in sidecar) != (high in sidecar):
            raise ValueError("Incomplete binary calibration endpoint")
    if "zero_min_v" in sidecar and not sidecar["zero_min_v"] <= table._values[0] <= sidecar["zero_max_v"]:
        raise ValueError("Invalid binary calibration zero bounds")
    if "drive_v" in sidecar and not 0 <= sidecar["drive_duty"] <= table._values[(count - 1) * 4 + 3]:
        raise ValueError("Invalid binary calibration drive endpoint")


def dump_calibration(value, stream):
    """Write pure sequential bytes, at most 256 per write; no seek/read needed."""
    envelope, tables = _envelope(value)
    for table in tables:
        _check_table(table, len(table))
    counter = _MetadataWriter()
    _typed_dump(envelope, counter, [0])
    header = struct.pack(HEADER_FORMAT, MAGIC, len(tables[0]), len(tables[1]), counter.count)
    digest = hashlib.sha256()
    digest.update(header)
    _write(stream, header)
    writer = _MetadataWriter(stream, digest)
    _typed_dump(envelope, writer, [0])
    if writer.count != counter.count:
        raise ValueError("Calibration metadata changed during save")
    for table in tables:
        # The dedicated codec shares PackedPoints' internal contiguous layout.
        # Copy only bounded chunks, never bytes(the entire numeric array).
        if _LITTLE_ENDIAN:
            view = memoryview(table._values)
            for index in range(0, len(table) * 4, CHUNK_BYTES // 4):
                chunk = bytes(view[index:min(index + CHUNK_BYTES // 4, len(table) * 4)])
                _write(stream, chunk)
                digest.update(chunk)
        else:
            for index in range(len(table)):
                chunk = struct.pack("<4f", *(table._values[index * 4 + column] for column in range(4)))
                _write(stream, chunk)
                digest.update(chunk)
    _write(stream, digest.digest())


def _read(stream, count):
    data = stream.read(count)
    if len(data) != count:
        raise ValueError("Truncated binary calibration")
    return data


def load_calibration(stream):
    """Load a regular binary file; any short read, checksum or format error fails."""
    header = _read(stream, HEADER_BYTES)
    magic, drain_count, gate_count, metadata_size = struct.unpack(HEADER_FORMAT, header)
    if (magic != MAGIC or not 2 <= drain_count <= MAX_CALIBRATION_POINTS or
            not 2 <= gate_count <= MAX_CALIBRATION_POINTS or not 1 <= metadata_size <= MAX_METADATA_BYTES):
        raise ValueError("Invalid binary calibration header")
    # Reserve BOTH contiguous buffers before metadata parsing fragments memory.
    tables = (PackedPoints(drain_count), PackedPoints(gate_count))
    digest = hashlib.sha256()
    digest.update(header)
    metadata = _read(stream, metadata_size)
    digest.update(metadata)
    for table, count in zip(tables, (drain_count, gate_count)):
        if _LITTLE_ENDIAN:
            view = memoryview(table._values)
            for index in range(0, count * 4, CHUNK_BYTES // 4):
                chunk = view[index:min(index + CHUNK_BYTES // 4, count * 4)]
                if stream.readinto(chunk) != len(chunk) * 4:
                    raise ValueError("Truncated binary calibration table")
                digest.update(chunk)
        else:
            for index in range(count):
                chunk = _read(stream, 16)
                digest.update(chunk)
                for column, number in enumerate(struct.unpack("<4f", chunk)):
                    table._values[index * 4 + column] = number
    if _read(stream, 32) != digest.digest() or stream.read(1):
        raise ValueError("Binary calibration integrity check failed")
    _metadata_depth(metadata)
    envelope = _typed_load(json.loads(metadata.decode("utf-8")), [0])
    if not isinstance(envelope, dict) or set(envelope) != {"calibration", "endpoints"}:
        raise ValueError("Invalid binary calibration envelope")
    value, endpoints = envelope["calibration"], envelope["endpoints"]
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] not in (1, 2) or
            not isinstance(value.get("metadata"), dict) or not isinstance(value.get("channels"), dict) or
            set(value["channels"]) != set(ROLES) or not isinstance(endpoints, dict) or set(endpoints) != set(ROLES)):
        raise ValueError("Invalid binary calibration root")
    for role, table, count in zip(ROLES, tables, (drain_count, gate_count)):
        channel, sidecar = value["channels"][role], endpoints[role]
        if not isinstance(channel, dict) or "points" in channel or not isinstance(sidecar, dict):
            raise ValueError("Invalid binary calibration channel")
        _check_table(table, count)
        _check_sidecar(sidecar, table, count)
        table._count = count  # Publish only validated, integrity-checked records.
        if "zero_min_v" in sidecar:
            table.set_zero_bounds(sidecar["zero_min_v"], sidecar["zero_max_v"])
        if "drive_v" in sidecar:
            table.set_drive_endpoint(sidecar["drive_v"], sidecar["drive_duty"])
        channel["points"] = table
    return value
