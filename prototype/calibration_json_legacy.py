"""One-time migration reader for old calibration JSON; not deployed firmware.

Only one point object is decoded at a time. Metadata and extension values have
explicit size/depth bounds; a corrupt file cannot build an unbounded JSON tree.
The caller still validates the finished calibration with model.validate_calibration.
"""

import json
import math

try:
    from firmware.calibration_data import PackedPoints, MAX_CALIBRATION_POINTS
except ImportError:
    from calibration_data import PackedPoints, MAX_CALIBRATION_POINTS

CHUNK_SIZE = 128
MAX_VALUE_CHARS = 8192
MAX_METADATA_CHARS = 16384
MAX_POINT_CHARS = 512
MAX_KEY_CHARS = 128
MAX_FIELDS = 32
MAX_DEPTH = 32
POINT_FIELDS = ("terminal_v", "shunt_v", "shunt_std_v", "duty")
POINT_OPTIONAL = ("zero_min_v", "zero_max_v", "drive_v", "drive_duty")


def _number(value):
    try:
        finite = math.isfinite(value) if isinstance(value, (int, float)) else False
    except OverflowError:
        finite = False
    if isinstance(value, bool) or not finite:
        raise ValueError("Invalid calibration point number")
    return value


class _Reader:
    def __init__(self, stream):
        # Allocate both contiguous numeric tables before any JSON parsing can
        # scatter metadata and transient point objects across the heap.
        self.tables = {"drain": PackedPoints(MAX_CALIBRATION_POINTS),
                       "gate": PackedPoints(MAX_CALIBRATION_POINTS)}
        self.current_points = None
        self.stream = stream
        self.chunk = ""
        self.index = 0
        self.position = 0
        self.metadata_chars = 0
        self.point_endpoint = None

    def peek(self):
        if self.index == len(self.chunk):
            self.chunk = self.stream.read(CHUNK_SIZE)
            self.index = 0
        return self.chunk[self.index] if self.chunk else ""

    def take(self):
        value = self.peek()
        if not value:
            raise ValueError("Truncated calibration JSON")
        self.index += 1
        self.position += 1
        return value

    def whitespace(self):
        while self.peek() in (" ", "\t", "\r", "\n"):
            self.take()

    def expect(self, value):
        self.whitespace()
        if self.take() != value:
            raise ValueError("Invalid calibration JSON structure")

    def value(self, limit=MAX_VALUE_CHARS):
        """Collect one bounded JSON value without consuming its separator."""
        self.whitespace()
        parts, part = [], ""
        size, depth = 0, 0
        quoted = escaped = False
        first = self.peek()
        if not first:
            raise ValueError("Truncated calibration JSON")
        compound = first in ("{", "[")
        string = first == '"'
        while True:
            char = self.peek()
            if not char:
                if quoted or depth or size == 0:
                    raise ValueError("Truncated calibration JSON")
                break
            if not compound and not string and char in (",", "}", "]", " ", "\t", "\r", "\n"):
                break
            char = self.take()
            size += 1
            if size > limit:
                raise ValueError("Calibration JSON value is too large")
            part += char
            if len(part) == CHUNK_SIZE:
                parts.append(part)
                part = ""
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                    if string:
                        break
            elif char == '"':
                quoted = True
            elif char in ("{", "["):
                depth += 1
                if depth > MAX_DEPTH:
                    raise ValueError("Calibration JSON nesting is too deep")
            elif char in ("}", "]"):
                depth -= 1
                if depth == 0:
                    break
        if part:
            parts.append(part)
        return json.loads("".join(parts))

    def object(self, field_reader=None, limit=None):
        start = self.position
        self.expect("{")
        result = {}
        self.whitespace()
        if self.peek() == "}":
            self.take()
            return result
        while True:
            key = self.value(MAX_KEY_CHARS)
            if not isinstance(key, str):
                raise ValueError("Calibration JSON object key must be a string")
            if key in result:
                raise ValueError("Duplicate calibration JSON field")
            if len(result) == MAX_FIELDS:
                raise ValueError("Too many calibration JSON fields")
            self.expect(":")
            result[key] = field_reader(key) if field_reader is not None else self.value(MAX_POINT_CHARS)
            if limit is not None and self.position - start > limit:
                raise ValueError("Calibration point is too large")
            self.whitespace()
            separator = self.take()
            if separator == "}":
                return result
            if separator != ",":
                raise ValueError("Invalid calibration JSON object separator")

    def points(self):
        self.expect("[")
        points = self.current_points
        self.whitespace()
        if self.peek() == "]":
            self.take()
            return points
        while True:
            if len(points) == MAX_CALIBRATION_POINTS:
                raise ValueError("Too many calibration points")
            point = self.object(limit=MAX_POINT_CHARS)
            for key in point:
                if key not in POINT_FIELDS and key not in POINT_OPTIONAL:
                    raise ValueError("Unsupported calibration point field")
                _number(point[key])
            if any(key not in point for key in ("terminal_v", "shunt_v", "duty")):
                raise ValueError("Incomplete calibration point")
            zero_min, zero_max = "zero_min_v" in point, "zero_max_v" in point
            drive_v, drive_duty = "drive_v" in point, "drive_duty" in point
            if zero_min != zero_max or (zero_min and len(points) != 0):
                raise ValueError("Invalid zero-drive calibration support")
            if zero_min and not point["zero_min_v"] <= point["terminal_v"] <= point["zero_max_v"]:
                raise ValueError("Invalid zero-drive calibration support")
            if drive_v != drive_duty:
                raise ValueError("Incomplete calibration endpoint")
            if drive_duty and not 0 <= point["drive_duty"] <= point["duty"]:
                raise ValueError("Invalid calibration endpoint duty")
            # Historical version-1 maps did not save baseline uncertainty.
            try:
                points.append(point["terminal_v"], point["shunt_v"], point.get("shunt_std_v", 0.0), point["duty"])
            except OverflowError:
                raise ValueError("Calibration point exceeds packed numeric range")
            for key in POINT_FIELDS:
                _number(points.value(-1, key))
            if zero_min:
                # JSON from a host may contain float64. Include only the
                # float32 rounding shift after checking the original bounds.
                terminal = points.value(0, "terminal_v")
                points.set_zero_bounds(min(point["zero_min_v"], terminal),
                                       max(point["zero_max_v"], terminal))
            self.whitespace()
            separator = self.take()
            if separator == "]":
                self.point_endpoint = point["terminal_v"]
                if drive_v:
                    shift = points.value(-1, "terminal_v") - point["terminal_v"]
                    points.set_drive_endpoint(point["drive_v"] + shift,
                                              min(point["drive_duty"], points.value(-1, "duty")))
                return points
            if separator != ",":
                raise ValueError("Invalid calibration point separator")
            if drive_v:
                raise ValueError("Drive calibration endpoint must be last")

    def metadata_value(self):
        start = self.position
        value = self.value()
        self.metadata_chars += self.position - start
        if self.metadata_chars > MAX_METADATA_CHARS:
            raise ValueError("Calibration metadata is too large")
        return value

    def channel_field(self, key):
        return self.points() if key == "points" else self.metadata_value()

    def channel(self, key):
        if key not in ("drain", "gate"):
            raise ValueError("Unknown calibration channel")
        self.current_points = self.tables[key]
        self.point_endpoint = None
        channel = self.object(self.channel_field)
        capability = channel.get("capability")
        points = channel.get("points")
        if self.point_endpoint is not None and isinstance(capability, dict):
            low = _number(capability.get("endpoint_min_v", self.point_endpoint))
            high = _number(capability.get("endpoint_max_v", self.point_endpoint))
            if not low <= self.point_endpoint <= high:
                raise ValueError("Invalid calibration endpoint support")
            terminal = points.value(-1, "terminal_v")
            if "endpoint_min_v" in capability:
                capability["endpoint_min_v"] = min(low, terminal)
            if "endpoint_max_v" in capability:
                capability["endpoint_max_v"] = max(high, terminal)
        return channel

    def root_field(self, key):
        return self.object(self.channel) if key == "channels" else self.metadata_value()


def load_calibration(stream):
    """Read a legacy/current JSON calibration without a full point-list tree."""
    reader = _Reader(stream)
    value = reader.object(reader.root_field)
    reader.whitespace()
    if reader.peek():
        raise ValueError("Trailing calibration JSON data")
    return value
