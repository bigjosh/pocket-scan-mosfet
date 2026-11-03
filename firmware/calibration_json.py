"""Stream the unchanged calibration JSON table from packed or legacy points.

Only one packed point's small logical dictionary exists at a time. Metadata,
provenance and other existing JSON fields retain their values. Both file saves
and fingerprints use this writer; never expand a packed table with list() or
json.dumps(). Legacy list-based calibrations use json.dump unchanged, keeping
their existing serialization and fingerprint behavior on the same runtime.
Packed calibrations use fixed schema-field order and recursively sorted extra
and metadata keys, independent of MicroPython dictionary allocation/iteration.
"""
import json

try:
    from .calibration_data import PackedPoints
except ImportError:
    from calibration_data import PackedPoints

_ROOT_FIELDS = ("version", "metadata", "channels")
_CHANNEL_ROLES = ("drain", "gate")
_CHANNEL_FIELDS = ("shunt_ohm", "points", "capability")
_POINT_FIELDS = ("terminal_v", "shunt_v", "shunt_std_v", "duty",
                 "zero_min_v", "zero_max_v", "drive_v", "drive_duty")


def _has_packed_points(value):
    channels = value.get("channels")
    if isinstance(channels, dict):
        for role in channels:
            channel = channels[role]
            if isinstance(channel, dict) and isinstance(channel.get("points"), PackedPoints):
                return True
    return False


def _dump_points(points, stream):
    stream.write("[")
    for index in range(len(points)):
        if index:
            stream.write(", ")
        # Do not retain this dictionary between iterations or collect a list
        # of views. __getitem__ exposes exactly the logical on-disk fields.
        _dump_object(points[index], stream, "point", _POINT_FIELDS)
    stream.write("]")


def _dump_json(value, stream):
    """Canonical small metadata/extra subtrees; no whole-value string copies."""
    if isinstance(value, dict):
        _dump_object(value, stream, "extra", ())
    elif isinstance(value, (list, tuple)):
        stream.write("[")
        first = True
        for item in value:
            if not first:
                stream.write(", ")
            first = False
            _dump_json(item, stream)
        stream.write("]")
    else:
        json.dump(value, stream)


def _dump_member(key, item, stream, level):
    json.dump(key, stream)
    stream.write(": ")
    if level == "root" and key == "channels" and isinstance(item, dict):
        _dump_object(item, stream, "channels", _CHANNEL_ROLES)
    elif level == "channels" and isinstance(item, dict):
        _dump_object(item, stream, "channel", _CHANNEL_FIELDS)
    elif level == "channel" and key == "points" and isinstance(item, (PackedPoints, list, tuple)):
        _dump_points(item, stream)
    else:
        _dump_json(item, stream)


def _dump_object(value, stream, level, known):
    # Only a bounded key list for one small object is sorted. Point tables
    # themselves are indexed and are never copied into lists or strings.
    for key in value:
        if not isinstance(key, str):
            raise ValueError("Calibration JSON field names must be strings")
    stream.write("{")
    first = True
    for key in known:
        if key not in value:
            continue
        if not first:
            stream.write(", ")
        first = False
        _dump_member(key, value[key], stream, level)
    for key in sorted(value):
        if key in known:
            continue
        if not first:
            stream.write(", ")
        first = False
        _dump_member(key, value[key], stream, level)
    stream.write("}")


def dump_calibration(value, stream):
    """Write calibration JSON without copying packed tables into a list.

The caller owns validation, the stream and any atomic file transaction. Write
errors propagate so the existing transaction can discard its temporary file.
"""
    if not isinstance(value, dict):
        raise ValueError("Invalid calibration JSON input")
    if _has_packed_points(value):
        _dump_object(value, stream, "root", _ROOT_FIELDS)
    else:
        json.dump(value, stream)
