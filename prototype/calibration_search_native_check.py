"""Read-only native equivalence/timing check for calibration binary search.

Run from a fresh REPL BEFORE importing main (no reserved display/scan buffers):
    import calibration_search_native_check as check
    check.run()

Only calibration.bin is read. No hardware, PWM, network, settings or main import;
no file writes or reset. Each candidate result must match the previous linear
search's float32 bits, including its original operation order and endpoint
fallbacks. Tolerance-edge acceptance is judged by the old implementation rather
than assumed from decimal arithmetic, because native float32 rounding matters.
The case stream is lazy and no full point/map copies are built.
"""
import binascii
import gc
import hashlib
import json
import struct
import time

try:
    from firmware import model, storage
    from firmware.calibration_data import PackedPoints, point_value
except ImportError:
    import model
    import storage
    from calibration_data import PackedPoints, point_value


def _linear(points, voltage, field, edge_tolerance_v=0.0):
    """Frozen former model.interpolate, with identical floating-point ops."""
    voltage = model._number(voltage, "Calibration voltage")
    drive_map = field == "drive_duty"
    coordinate = "drive_v" if drive_map else "terminal_v"
    value_field = "duty" if drive_map else field
    first_v = point_value(points, 0, coordinate, point_value(points, 0, "terminal_v"))
    last_v = point_value(points, -1, coordinate, point_value(points, -1, "terminal_v"))
    first_value = point_value(points, 0, field, point_value(points, 0, value_field))
    if voltage < first_v:
        if first_v - voltage <= edge_tolerance_v + 1e-9:
            return first_value
        raise ValueError("Voltage %.4f V is below calibration coverage" % voltage)
    if voltage > last_v:
        if voltage - last_v <= edge_tolerance_v + 1e-9:
            return point_value(points, -1, field, point_value(points, -1, value_field))
        raise ValueError("Voltage %.4f V is above calibration coverage" % voltage)
    low_value, lower_v = first_value, first_v
    for index in range(1, len(points)):
        upper_v = point_value(points, index, coordinate, point_value(points, index, "terminal_v"))
        high_value = point_value(points, index, field, point_value(points, index, value_field))
        if voltage <= upper_v:
            fraction = (voltage - lower_v) / (upper_v - lower_v)
            return low_value + fraction * (high_value - low_value)
        low_value, lower_v = high_value, upper_v
    return point_value(points, -1, field, point_value(points, -1, value_field))


def _host_us():
    return time.perf_counter_ns() // 1000


_ticks = getattr(time, "ticks_us", _host_us)
_diff = getattr(time, "ticks_diff", lambda new, old: new - old)


def _file_digest(path):
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(256)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size, binascii.hexlify(digest.digest()).decode()


def _tables_digest(calibration):
    digest = hashlib.sha256()
    for role in ("drain", "gate"):
        points = calibration["channels"][role]["points"]
        digest.update(struct.pack("<II", len(points), points.capacity))
        digest.update(memoryview(points._values))
        for index, field in ((0, "zero_min_v"), (0, "zero_max_v"),
                             (-1, "drive_v"), (-1, "drive_duty")):
            value = points.value(index, field)
            digest.update(b"n" if value is None else b"f" + struct.pack("<d", value))
    return digest.digest()


def _coordinate(points, index, field):
    terminal = point_value(points, index, "terminal_v")
    return point_value(points, index, "drive_v", terminal) if field == "drive_duty" else terminal


def _neighbor(value, upward):
    """Adjacent float32, including both sides of zero, without math.nextafter."""
    if value == 0.0:
        bits = 1 if upward else 0x80000001
    else:
        bits = struct.unpack("<I", struct.pack("<f", value))[0]
        bits += 1 if (value > 0) == upward else -1
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _cases(points, field):
    count = len(points)
    first, last = _coordinate(points, 0, field), _coordinate(points, -1, field)
    for index in range(count):
        voltage = _coordinate(points, index, field)
        yield voltage, 0.0
        if index + 1 < count:
            upper = _coordinate(points, index + 1, field)
            yield voltage + (upper - voltage) * 0.5, 0.0
    # Probe bracket ties at the first, middle and final knots, without assuming
    # that a rounded tolerance boundary lies inside or outside the old guard.
    for index in (0, count // 2, count - 1):
        voltage = _coordinate(points, index, field)
        yield _neighbor(voltage, False), 0.0
        yield _neighbor(voltage, True), 0.0
    for tolerance in (0.0, model.CALIBRATION_EDGE_TOLERANCE_V, model.VOLTAGE_TOLERANCE_V):
        below, above = first - tolerance, last + tolerance
        for voltage in (below, _neighbor(below, False), _neighbor(below, True),
                        above, _neighbor(above, False), _neighbor(above, True),
                        below - 0.001, above + 0.001):
            yield voltage, tolerance


def _check(points, voltage, field, tolerance):
    try:
        expected = _linear(points, voltage, field, tolerance)
    except ValueError as old_error:
        try:
            model.interpolate(points, voltage, field, tolerance)
        except ValueError as new_error:
            if str(new_error) != str(old_error):
                raise AssertionError(("Changed coverage error", field, voltage,
                                      str(old_error), str(new_error)))
            return 1
        raise AssertionError(("Accepted formerly rejected voltage", field, voltage, tolerance))
    actual = model.interpolate(points, voltage, field, tolerance)
    if struct.pack("<f", expected) != struct.pack("<f", actual):
        raise AssertionError(("Float32 interpolation mismatch", field, voltage, expected, actual))
    return 0


def _benchmark(function, points, field, inputs, repeats):
    gc.collect()
    started = _ticks()
    result = 0.0
    for unused in range(repeats):
        for voltage in inputs:
            result = function(points, voltage, field, 0.0)
    elapsed = _diff(_ticks(), started)
    return elapsed, result


def run(path="calibration.bin", benchmark_repeats=16):
    if (isinstance(benchmark_repeats, bool) or not isinstance(benchmark_repeats, int)
            or not 1 <= benchmark_repeats <= 64):
        raise ValueError("Use 1..64 benchmark repetitions")
    file_before = _file_digest(path)
    calibration = storage.load_calibration(path=path)
    if calibration is None:
        raise ValueError("No saved binary calibration")
    model.validate_calibration(calibration)
    for role in ("drain", "gate"):
        if not isinstance(calibration["channels"][role]["points"], PackedPoints):
            raise ValueError("Diagnostic requires actual packed calibration tables")
    tables_before = _tables_digest(calibration)
    total_checks = total_rejections = 0
    results = []
    for role in ("drain", "gate"):
        points = calibration["channels"][role]["points"]
        for field in ("terminal_v", "shunt_v", "drive_duty"):
            checks = rejected = 0
            for voltage, tolerance in _cases(points, field):
                rejected += _check(points, voltage, field, tolerance)
                checks += 1
            total_checks += checks
            total_rejections += rejected
            low, high = _coordinate(points, 0, field), _coordinate(points, -1, field)
            # Both implementations receive these exact same stored float inputs.
            inputs = (low, low + (high - low) * 0.125, low + (high - low) * 0.5,
                      low + (high - low) * 0.875, high)
            old_us, old_result = _benchmark(_linear, points, field, inputs, benchmark_repeats)
            new_us, new_result = _benchmark(model.interpolate, points, field, inputs, benchmark_repeats)
            if struct.pack("<f", old_result) != struct.pack("<f", new_result):
                raise AssertionError("Benchmark changed the interpolation result")
            row = {"channel": role, "field": field, "points": len(points),
                   "checks": checks, "coverage_rejections": rejected,
                   "benchmark_calls": len(inputs) * benchmark_repeats,
                   "linear_us": old_us, "candidate_us": new_us,
                   "speedup": old_us / new_us if new_us else None}
            results.append(row)
            print("CAL_SEARCH_MAP", json.dumps(row))
    if _tables_digest(calibration) != tables_before:
        raise AssertionError("Calibration point arrays or endpoint fields mutated")
    if _file_digest(path) != file_before:
        raise AssertionError("Saved calibration file changed")
    report = {"checks": total_checks, "coverage_rejections": total_rejections,
              "float32_bits_equal": True, "coverage_errors_equal": True,
              "point_arrays_unchanged": True, "file_unchanged": True,
              "calibration_bytes": file_before[0], "calibration_file_sha256": file_before[1],
              "benchmark_repeats": benchmark_repeats, "results": results}
    print("CAL_SEARCH_COMPLETE", json.dumps(report))
    return report


if __name__ == "__main__":
    run()
