"""Portable settings, scan geometry and calibration math (no hardware imports)."""

import math

try:
    from .calibration_data import PackedPoints, point_value
except ImportError:
    from calibration_data import PackedPoints, point_value

PWM_HZ = 100000
PWM_FULL_SCALE_V = 3.3 * 8.5
INA_MAX_BUS_V = 36.0
MAX_POINTS = 4096
DRAIN_SHUNT_OHM = 0.789  # Effective scale: nominal 1 ohm, 9980-ohm reference, 2026-11-02.
GATE_SHUNT_OHM = 638.0  # Provisional in-circuit meter reading.
DRAIN_CURRENT_LIMIT_A = 0.005
GATE_CURRENT_LIMIT_A = 0.000100
VOLTAGE_TOLERANCE_V = 0.020
# Curve membership is independent of drive/calibration accuracy checks.
GATE_PLOT_TOLERANCE_V = 0.100
CALIBRATION_EDGE_TOLERANCE_V = 0.005
MAX_DRAIN_PARK_OFFSET_V = 0.5
# A proven positive drain floor may move with gate loading. This parking-only
# allowance does not extend current-baseline coverage or calibration tolerance.
DRAIN_ZERO_PARK_TOLERANCE_V = 0.100
DRAIN_PARK_SOURCE_ALLOWANCE_V = 0.005
# A table duty is float32, while host tools/legacy JSON may use float64.
# One float32 ULP near full scale is below one Pico PWM counter step.
DUTY_STORAGE_TOLERANCE = 0.00000012
MICROVOLTS_PER_VOLT = 1000000
VOLTAGE_FIELDS = ("drain_start", "drain_stop", "drain_step",
                  "gate_start", "gate_stop", "gate_step",
                  "driver_supply", "source_voltage")
INA_AVERAGE_COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)
ACQUISITION_MODE = "ina226_triggered_v1"
INA_CONVERSION_US = 1100
CALIBRATION_REPEAT_BLOCKS = 3
LEGACY_TIMING = {"batches": 10, "samples_per_batch": 50,
                 "sample_pause_ms": 4, "settle_ms": 200}

DEFAULT_SETTINGS = {
    "version": 3,
    "drain_start_uv": 0, "drain_stop_uv": 2000000, "drain_step_uv": 500000,
    "gate_start_uv": 0, "gate_stop_uv": 2000000, "gate_step_uv": 500000,
    "driver_supply_uv": 5000000, "source_voltage_uv": 0,
    "ina_average_count": 64, "settle_ms": 100,
}


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + " must be a number")
    if not math.isfinite(value):
        raise ValueError(label + " must be finite")
    return float(value)


def parse_voltage_uv(text):
    """Parse a user-entered decimal voltage without passing through float32."""
    if not isinstance(text, str):
        raise ValueError("Enter a decimal voltage")
    text = text.strip()
    if not text or len(text) > 24:
        raise ValueError("Enter a decimal voltage")
    sign = -1 if text[0] == "-" else 1
    if text[0] in "+-":
        text = text[1:]
    parts = text.split(".")
    if len(parts) > 2 or not text or not any(c in "0123456789" for c in text):
        raise ValueError("Enter a decimal voltage")
    whole = parts[0]
    fraction = parts[1] if len(parts) == 2 else ""
    if any(c not in "0123456789" for c in whole + fraction):
        raise ValueError("Enter a decimal voltage")
    fraction = fraction.rstrip("0")
    if len(fraction) > 6:
        raise ValueError("Voltage resolution is 0.000001 V")
    return sign * (int(whole or "0") * MICROVOLTS_PER_VOLT +
                   int((fraction + "000000")[:6]))


def format_voltage_uv(value):
    """Exact decimal volts for display/editing; retain all stored microvolts."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Voltage must be integer microvolts")
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), MICROVOLTS_PER_VOLT)
    return (("%s%d.%06d" % (sign, whole, fraction)).rstrip("0")
            if fraction else sign + str(whole))


def volts_from_uv(value):
    """Convert only at physical-drive, measurement and presentation boundaries."""
    return value / MICROVOLTS_PER_VOLT


def volts_to_uv(value):
    """Migrate legacy numeric volts to the nearest supported microvolt.

    Decimal formatting avoids float32 multiplication by a million, which loses
    individual microvolts at larger voltages. New UI input uses the exact parser.
    Historical archives are never migrated or rewritten by this function.
    """
    value = _number(value, "Legacy voltage")
    if abs(value) > 1000000:
        raise ValueError("Voltage is outside the supported range")
    return parse_voltage_uv("%.6f" % value)


def setting_voltage_v(settings, key):
    """Access a settings voltage in volts; also accepts legacy calibration metadata."""
    uv_key = key[:-2] + "_uv"
    if uv_key in settings:
        return volts_from_uv(settings[uv_key])
    return _number(settings[key], key)


def voltage_limit(settings):
    """Requested terminal-to-GND envelope, separate from measured capability."""
    rail = setting_voltage_v(settings, "driver_supply_v")
    return max(0.0, min(rail, INA_MAX_BUS_V))


def calibration_duty_limit(settings):
    """Probe only a bounded drive appropriate to the declared analog supply.

    The input common-mode bound is independent of output swing. At 5 V this
    limit is about 17.8% PWM, not a blind probe at 100% duty.
    """
    rail = setting_voltage_v(settings, "driver_supply_v")
    return max(0.0, min(1.0, rail / PWM_FULL_SCALE_V, (rail - 2.0) / 3.3))


class SweepValues:
    """Small indexed sequence; no contiguous buffer grows with point count.

    A 4096-element list needs a large contiguous allocation on MicroPython,
    which can fail after UI activity despite sufficient total free heap.
    Keep only the geometry and calculate each target when it is needed.
    """

    def __init__(self, start, step, count):
        self._start, self._step, self._count = start, step, count

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        if not isinstance(index, int):
            raise TypeError("Sweep index must be an integer")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError("Sweep index out of range")
        return self._start + index * self._step

    def __iter__(self):
        for index in range(self._count):
            yield self._start + index * self._step


def sweep_uv(start, stop, step):
    """Ascending exact-step sweep; include stop only when a step reaches it.

    Return a compact lazy sequence, computed from the index rather than
    repeated floating-point additions or a materialized target list.
    A non-divisible span stops at the last step below stop; it never changes
    the requested step or appends a shorter final step.
    """
    if any(isinstance(value, bool) or not isinstance(value, int)
           for value in (start, stop, step)):
        raise ValueError("Sweep voltages must be integer microvolts")
    if step <= 0 or stop < start:
        raise ValueError("Step must be positive and stop must be >= start")
    count = (stop - start) // step + 1
    if count > MAX_POINTS:
        raise ValueError("Too many scan points (maximum %d)" % MAX_POINTS)
    return SweepValues(start, step, count)


class SweepVolts:
    """Legacy volts-facing adapter; geometry and indexing still use integers."""
    def __init__(self, values):
        self._values = values

    def __len__(self):
        return len(self._values)

    def __getitem__(self, index):
        return volts_from_uv(self._values[index])

    def __iter__(self):
        for value in self._values:
            yield volts_from_uv(value)


def sweep_values(start, stop, step):
    """Compatibility for volts-based tools; new planning calls sweep_uv directly."""
    return SweepVolts(sweep_uv(volts_to_uv(start), volts_to_uv(stop), volts_to_uv(step)))


def scan_point_count(settings):
    count = 1
    for role in ("drain", "gate"):
        count *= len(sweep_uv(settings[role + "_start_uv"],
                              settings[role + "_stop_uv"], settings[role + "_step_uv"]))
    if count > MAX_POINTS:
        raise ValueError("Scan exceeds %d points" % MAX_POINTS)
    return count


def _validate_legacy_timing(result):
    for key, low, high in (("batches", 2, 32),
                           ("samples_per_batch", 1, 500),
                           ("sample_pause_ms", 4, 1000),
                           ("settle_ms", 200, 10000)):
        value = _number(result[key], key)
        if value != int(value) or not low <= value <= high:
            raise ValueError("%s must be an integer in %d..%d" % (key, low, high))
        result[key] = int(value)


def _validate_timing(result):
    count = result["ina_average_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count not in INA_AVERAGE_COUNTS:
        raise ValueError("INA226 average count must be 1, 4, 16, 64, 128, 256, 512 or 1024")
    value = _number(result["settle_ms"], "settle_ms")
    if value != int(value) or not 100 <= value <= 10000:
        raise ValueError("settle_ms must be an integer in 100..10000")
    result["settle_ms"] = int(value)


def acquisition_metadata(settings, blocks_per_point=1):
    """Describe the actual INA226 conversion method independently of voltages."""
    if (isinstance(blocks_per_point, bool) or not isinstance(blocks_per_point, int)
            or blocks_per_point < 1):
        raise ValueError("Invalid acquisition block count")
    _validate_timing(dict(settings))
    return {"acquisition_mode": ACQUISITION_MODE,
            "ina_average_count": settings["ina_average_count"],
            "bus_conversion_us": INA_CONVERSION_US,
            "shunt_conversion_us": INA_CONVERSION_US,
            "blocks_per_point": blocks_per_point,
            "point_variance": "unavailable" if blocks_per_point == 1 else "between_blocks"}


def validate_calibration_acquisition(metadata, settings):
    """Reject old/different conversion bias without rewalking the point tables."""
    if not isinstance(metadata, dict) or metadata.get("acquisition_mode") != ACQUISITION_MODE:
        raise ValueError("INA226 measurement mode changed; recalibrate with DUT disconnected")
    for key, expected in (("ina_average_count", settings["ina_average_count"]),
                          ("bus_conversion_us", INA_CONVERSION_US),
                          ("shunt_conversion_us", INA_CONVERSION_US),
                          ("calibration_repeat_blocks", CALIBRATION_REPEAT_BLOCKS)):
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value != expected:
            raise ValueError("INA226 averaging changed; recalibrate with DUT disconnected")
    return metadata


def validate_settings(settings):
    """Return a normalized copy, or raise ValueError without changing inputs."""
    if not isinstance(settings, dict):
        raise ValueError("Settings must be an object")
    version = settings.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in (1, 2, 3):
        raise ValueError("Unsupported settings version")
    result = dict(DEFAULT_SETTINGS)
    result.update(settings)
    if version == 1:
        if any(field + "_uv" in settings for field in VOLTAGE_FIELDS):
            raise ValueError("Settings contain mixed voltage units")
        for field in VOLTAGE_FIELDS:
            legacy = field + "_v"
            if legacy in settings:
                result[field + "_uv"] = volts_to_uv(settings[legacy])
            result.pop(legacy, None)
    elif any(field + "_v" in settings for field in VOLTAGE_FIELDS):
        raise ValueError("Settings contain mixed voltage units")
    if version < 3:
        if "ina_average_count" in settings:
            raise ValueError("Settings contain mixed averaging modes")
        previous = dict(LEGACY_TIMING)
        for key in LEGACY_TIMING:
            if key in settings:
                previous[key] = settings[key]
        _validate_legacy_timing(previous)
        total = previous["batches"] * previous["samples_per_batch"]
        result["ina_average_count"] = INA_AVERAGE_COUNTS[-1]
        for count in INA_AVERAGE_COUNTS:
            if count >= total:
                result["ina_average_count"] = count
                break
        for key in ("batches", "samples_per_batch", "sample_pause_ms"):
            result.pop(key, None)
        result["version"] = 3
    elif any(key in settings for key in ("batches", "samples_per_batch", "sample_pause_ms")):
        raise ValueError("Settings contain mixed averaging modes")
    for field in VOLTAGE_FIELDS:
        value = result[field + "_uv"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(field + " must be integer microvolts")
    rail, source = result["driver_supply_uv"], result["source_voltage_uv"]
    if not 2100000 <= rail <= 30000000:
        raise ValueError("Driver supply must be between 2.1 and 30 V")
    if not 0 <= source <= rail:
        raise ValueError("Source voltage is outside the driver envelope")
    _validate_timing(result)
    for role in ("drain", "gate"):
        start, stop, step = (result[role + "_" + field + "_uv"]
                             for field in ("start", "stop", "step"))
        if not -36000000 <= start <= stop <= 36000000 or not 1 <= step <= 72000000:
            raise ValueError(role + " sweep is outside supported limits")
        if source + start < 0 or source + stop > rail:
            # MicroPython on Pico does not implement str.capitalize(). Keep
            # the validation path portable so invalid ranges remain editable.
            label = "Drain" if role == "drain" else "Gate"
            raise ValueError("%s terminal targets must be within 0..%.3f V to GND"
                             % (label, voltage_limit(result)))
    scan_point_count(result)
    return result


def validate_archive_settings(settings):
    """Validate saved snapshots without reinterpreting a historical float grid."""
    if not isinstance(settings, dict):
        raise ValueError("Invalid saved settings")
    if settings.get("version") == 3:
        return validate_settings(settings)
    if settings.get("version") == 2:
        # Validate its integer voltage grid, but keep the historical acquisition
        # settings instead of reinterpreting an already captured scan.
        normalized = validate_settings(settings)
        result = dict(settings)
        for field in VOLTAGE_FIELDS:
            result[field + "_uv"] = normalized[field + "_uv"]
        for key, default in LEGACY_TIMING.items():
            if key not in result:
                result[key] = default
        _validate_legacy_timing(result)
        return result
    if isinstance(settings.get("version"), bool) or settings.get("version") != 1:
        raise ValueError("Unsupported saved settings version")
    if any(field + "_uv" in settings for field in VOLTAGE_FIELDS):
        raise ValueError("Saved settings contain mixed voltage units")
    result = dict(settings)
    for field in VOLTAGE_FIELDS:
        result[field + "_v"] = _number(result[field + "_v"], field)
    rail, source = result["driver_supply_v"], result["source_voltage_v"]
    if not 2.1 <= rail <= 30 or not 0 <= source <= rail:
        raise ValueError("Invalid saved voltage envelope")
    for role in ("drain", "gate"):
        start, stop, step = (result[role + "_" + field + "_v"]
                             for field in ("start", "stop", "step"))
        if (not -36 <= start <= stop <= 36 or not 0 < step <= 72
                or source + start < -0.00001 or source + stop > rail + 0.00001):
            raise ValueError("Invalid saved voltage sweep")
    _validate_legacy_timing(result)
    # The recorded footer count is authoritative for version1 float settings.
    return result


class CalibrationTargets:
    """Compact indexed calibration increments; no growing point list.

    The engine adds each channel's measured zero-duty floor to this grid.
    Every supported rail is above the complete 0.8..1.8 V refined region.
    Indices 0..7 cover 0..0.7 V, 8..28 cover 0.8..1.8 V in 50 mV steps,
    and later indices resume 100 mV steps. The final item is the exact rail.
    """
    def __init__(self, limit):
        if (isinstance(limit, bool) or not isinstance(limit, int) or
                not 2100000 <= limit <= 30000000):
            raise ValueError("Driver supply must be between 2.1 and 30 V")
        self._limit = limit
        # A regular grid strictly below rail contributes ceil(rail/100 mV)
        # items including zero. The refined region adds ten odd 50 mV
        # steps, and the final exact rail adds one more item.
        self._count = (limit - 1) // 100000 + 12

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        if not isinstance(index, int):
            raise TypeError("Calibration index must be an integer")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError("Calibration index out of range")
        if index == self._count - 1:
            return self._limit
        if index < 8:
            return index * 100000
        if index < 29:
            return 800000 + (index - 8) * 50000
        return (index - 10) * 100000

    def __iter__(self):
        for index in range(self._count):
            yield self[index]


def calibration_targets_uv(settings):
    """Integer increments; retain the legacy grid's finer 0.8..1.8 V region."""
    return CalibrationTargets(settings["driver_supply_uv"])


def calibration_targets(settings):
    """Volts-facing compatibility helper for host tools and older diagnostics."""
    return [volts_from_uv(value) for value in calibration_targets_uv(validate_settings(settings))]


def _validate_calibration_settings(calibration, settings):
    if not isinstance(calibration, dict) or calibration.get("version") not in (1, 2):
        raise ValueError("Run open-output calibration first")
    metadata = calibration.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid calibration metadata")
    validate_calibration_acquisition(metadata, settings)
    if "driver_supply_uv" in metadata:
        supply = metadata["driver_supply_uv"]
        matches = (isinstance(supply, int) and not isinstance(supply, bool)
                   and supply == settings["driver_supply_uv"])
    else:
        # Historical metadata is measured/stored in float volts. Convert
        # the requested rail to that same representation for comparison;
        # float32 cannot preserve every microvolt above roughly 16 V.
        legacy_supply = _number(metadata.get("driver_supply_v"), "Calibration supply")
        matches = (legacy_supply == volts_from_uv(settings["driver_supply_uv"]) or
                   volts_to_uv(legacy_supply) == settings["driver_supply_uv"])
    if not matches:
        raise ValueError("Driver supply changed; recalibrate with DUT disconnected")
    if metadata.get("pwm_hz") != PWM_HZ:
        raise ValueError("Calibration PWM frequency does not match")
    channels = calibration.get("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("Invalid calibration channels")
    for role, resistance in (("drain", DRAIN_SHUNT_OHM), ("gate", GATE_SHUNT_OHM)):
        channel = channels.get(role, {})
        if not isinstance(channel, dict) or channel.get("shunt_ohm") != resistance:
            raise ValueError(role + " shunt does not match calibration")


def validate_calibration(calibration, settings=None):
    if not isinstance(calibration, dict) or calibration.get("version") not in (1, 2):
        raise ValueError("Run open-output calibration first")
    metadata = calibration.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Invalid calibration metadata")
    if settings is not None:
        _validate_calibration_settings(calibration, settings)
    channels = calibration.get("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("Invalid calibration channels")
    for role, resistance in (("drain", DRAIN_SHUNT_OHM), ("gate", GATE_SHUNT_OHM)):
        channel = channels.get(role, {})
        if not isinstance(channel, dict):
            raise ValueError("Invalid " + role + " calibration")
        if channel.get("shunt_ohm") != resistance:
            raise ValueError(role + " shunt does not match calibration")
        points = channel.get("points", [])
        if not isinstance(points, (list, PackedPoints)) or len(points) < 2:
            raise ValueError(role + " calibration requires at least two points")
        last_v, last_duty = -1e6, -1.0
        last_drive_v, last_drive_duty = -1e6, -1.0
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("Invalid " + role + " calibration point")
            v = _number(point.get("terminal_v"), role + " calibration voltage")
            duty = _number(point.get("duty"), role + " calibration duty")
            shunt = _number(point.get("shunt_v"), role + " calibration baseline")
            if not -VOLTAGE_TOLERANCE_V <= v <= INA_MAX_BUS_V or abs(shunt) > 0.08192:
                raise ValueError(role + " calibration is outside sensor range")
            if v <= last_v or duty <= last_duty or not 0 <= duty <= 1:
                raise ValueError(role + " calibration is not monotonic")
            drive_v = _number(point.get("drive_v", v), role + " drive-map voltage")
            drive_duty = _number(point.get("drive_duty", duty), role + " drive-map duty")
            if (abs(drive_v - v) > CALIBRATION_EDGE_TOLERANCE_V + 1e-9
                    or not 0 <= drive_duty <= duty
                    or drive_v <= last_drive_v or drive_duty <= last_drive_duty):
                raise ValueError(role + " inverse drive map is invalid")
            last_v, last_duty = v, duty
            last_drive_v, last_drive_duty = drive_v, drive_duty
        if points[0]["duty"] != 0.0:
            raise ValueError(role + " calibration must include zero drive")
        first = points[0]
        has_zero_min, has_zero_max = "zero_min_v" in first, "zero_max_v" in first
        if has_zero_min != has_zero_max:
            raise ValueError(role + " zero-drive measurement support is incomplete")
        if has_zero_min:
            low = _number(first["zero_min_v"], role + " zero-drive minimum")
            high = _number(first["zero_max_v"], role + " zero-drive maximum")
            if (not -VOLTAGE_TOLERANCE_V <= low <= first["terminal_v"] <= high <= INA_MAX_BUS_V
                    or high - low > 2 * VOLTAGE_TOLERANCE_V + 1e-9):
                raise ValueError(role + " zero-drive measurement support is invalid")
        capability = channel.get("capability")
        if calibration["version"] == 2:
            if not isinstance(capability, dict) or capability.get("complete") is not True:
                raise ValueError(role + " capability discovery is incomplete")
            maximum_duty = _number(capability.get("max_duty"), role + " maximum duty")
            safe_duty = calibration_duty_limit(metadata)
            if points[-1]["terminal_v"] > min(metadata["driver_supply_v"] + 0.050, INA_MAX_BUS_V):
                raise ValueError(role + " calibration voltage exceeds the declared supply")
            if not points[-1]["duty"] - DUTY_STORAGE_TOLERANCE <= maximum_duty <= safe_duty + 1e-9:
                raise ValueError(role + " capability duty is outside calibrated bounds")
            if capability.get("limit_reason") not in ("plateau", "drive_ceiling", "rail_envelope"):
                raise ValueError(role + " capability reason is invalid")
            low_support = _number(capability.get("endpoint_min_v", points[-1]["terminal_v"]), role + " endpoint minimum")
            high_support = _number(capability.get("endpoint_max_v", points[-1]["terminal_v"]), role + " endpoint maximum")
            if (not -CALIBRATION_EDGE_TOLERANCE_V <= low_support <= points[-1]["terminal_v"] <= high_support
                    or high_support - low_support > 2 * VOLTAGE_TOLERANCE_V + 1e-9
                    or high_support > min(metadata["driver_supply_v"] + 0.050, INA_MAX_BUS_V)):
                raise ValueError(role + " endpoint measurement support is invalid")
        elif capability is not None:
            # An older partial-range calibration must not gain a fabricated
            # physical ceiling merely by adding a capability flag.
            raise ValueError("Legacy calibration cannot contain discovered capability")
    return calibration


def drain_parking_voltage(calibration, settings):
    """Use a proven small positive drain floor, never an arbitrary high output.

    This fallback needs the measured zero-duty endpoint and the same source
    reference used to acquire it. Legacy maps keep ordinary source parking.
    """
    source = volts_from_uv(settings["source_voltage_uv"])
    channel = calibration["channels"]["drain"]
    first = channel["points"][0]
    floor = first["terminal_v"]
    saved_source = calibration["metadata"].get("source_voltage_uv")
    source_known = isinstance(saved_source, int) and not isinstance(saved_source, bool)
    if (source_known and "zero_min_v" in first
            and floor - volts_from_uv(saved_source) > VOLTAGE_TOLERANCE_V
            and saved_source != settings["source_voltage_uv"]):
        # A higher newly entered VS must not silently bypass the binding just
        # because it would now lie above the old, reference-dependent floor.
        raise ValueError("Source reference changed; recalibrate the drain floor")
    if floor <= source:
        return source
    measured = (calibration["version"] == 2 and first["duty"] == 0.0
                and "zero_min_v" in first and "zero_max_v" in first
                and source_known
                and saved_source == settings["source_voltage_uv"])
    if not measured:
        # Preserve the original few-mV legacy endpoint allowance, without
        # authorizing a new offset from an old or differently referenced map.
        _check_drive_coverage(channel, source)
        return source
    if floor - source > MAX_DRAIN_PARK_OFFSET_V:
        raise ValueError("Drain floor exceeds the 0.5 V source parking allowance")
    return floor


def validate_scan_calibration(calibration, settings):
    """Validate an untrusted map completely, then its scan settings."""
    validate_calibration(calibration)
    return validate_scan_calibration_settings(calibration, settings)


def validate_scan_calibration_settings(calibration, settings):
    """Check settings against a validated, caller-owned read-only map.

    This deliberately does not walk every point. Callers must fully validate
    unknown/replaced maps first and must not mutate a trusted table in place.
    Acquisition/rail/PWM/shunt compatibility and source/range coverage are
    checked on every call; no trust marker is accepted from a stored file.
    """
    _validate_calibration_settings(calibration, settings)
    source_uv = settings["source_voltage_uv"]
    source = volts_from_uv(source_uv)
    drain_park = drain_parking_voltage(calibration, settings)
    for role in ("drain", "gate"):
        channel = calibration["channels"][role]
        parking = drain_park if role == "drain" else source
        _check_drive_coverage(channel, parking)
        values = sweep_uv(settings[role + "_start_uv"],
                          settings[role + "_stop_uv"], settings[role + "_step_uv"])
        for target in (volts_from_uv(values[0] + source_uv), volts_from_uv(values[-1] + source_uv)):
            if role == "drain" and drain_park > source:
                if target < source:
                    raise ValueError("Negative VDS is unavailable with the calibrated drain floor")
                target = max(target, drain_park)
            _check_drive_coverage(channel, target, allow_limited=True)
    return calibration


def _check_drive_coverage(channel, voltage, allow_limited=False):
    """Check endpoints of a validated monotonic map without interpolating it."""
    voltage = _number(voltage, "Target voltage")
    points = channel["points"]
    first_v = point_value(points, 0, "terminal_v")
    first_duty = point_value(points, 0, "duty")
    if first_duty == 0.0 and (abs(voltage - first_v) <= max(abs(first_v), 1.0) * 0.00000006
                              or voltage == 0.0 and abs(first_v) <= VOLTAGE_TOLERANCE_V):
        return
    last_v = point_value(points, -1, "terminal_v")
    capability = channel.get("capability")
    if allow_limited and capability and capability.get("complete") is True and voltage > last_v:
        return
    lower = point_value(points, 0, "drive_v", first_v)
    upper = point_value(points, -1, "drive_v", last_v)
    if voltage < lower and lower - voltage > VOLTAGE_TOLERANCE_V + 1e-9:
        raise ValueError("Voltage %.4f V is below calibration coverage" % voltage)
    if voltage > upper and voltage - upper > VOLTAGE_TOLERANCE_V + 1e-9:
        raise ValueError("Voltage %.4f V is above calibration coverage" % voltage)


_INTERPOLATION_MISSING = object()


def _interpolation_value(points, index, field, fallback):
    """Read an optional endpoint override without evaluating its fallback."""
    if field == fallback:
        return point_value(points, index, field)
    value = point_value(points, index, field, _INTERPOLATION_MISSING)
    return point_value(points, index, fallback) if value is _INTERPOLATION_MISSING else value


def interpolate(points, voltage, field, edge_tolerance_v=0.0):
    """Piecewise linear interpolation; no slope extrapolation.

    The caller chooses an endpoint tolerance for drive commands or readback.
    Values within it use the endpoint value, never an extrapolation. Tables
    have already passed strict coordinate-monotonicity validation.
    """
    voltage = _number(voltage, "Calibration voltage")
    drive_map = field == "drive_duty"
    coordinate = "drive_v" if drive_map else "terminal_v"
    value_field = "duty" if drive_map else field
    first_v = _interpolation_value(points, 0, coordinate, "terminal_v")
    last_v = _interpolation_value(points, -1, coordinate, "terminal_v")
    if voltage < first_v:
        if first_v - voltage <= edge_tolerance_v + 1e-9:
            return _interpolation_value(points, 0, field, value_field)
        raise ValueError("Voltage %.4f V is below calibration coverage" % voltage)
    if voltage > last_v:
        if voltage - last_v <= edge_tolerance_v + 1e-9:
            return _interpolation_value(points, -1, field, value_field)
        raise ValueError("Voltage %.4f V is above calibration coverage" % voltage)
    last = len(points) - 1
    if last < 1:
        return _interpolation_value(points, -1, field, value_field)
    # Find the first upper endpoint >= voltage, matching the old linear walk
    # at exact knots. Do not shortcut knots to their stored value: preserving
    # this arithmetic order also preserves native float32 rounding.
    left, right = 1, last
    while left < right:
        middle = (left + right) // 2
        upper_v = _interpolation_value(points, middle, coordinate, "terminal_v")
        if voltage <= upper_v:
            right = middle
        else:
            left = middle + 1
    lower_v = first_v if left == 1 else _interpolation_value(points, left - 1, coordinate, "terminal_v")
    upper_v = last_v if left == last else _interpolation_value(points, left, coordinate, "terminal_v")
    low_value = _interpolation_value(points, left - 1, field, value_field)
    high_value = _interpolation_value(points, left, field, value_field)
    fraction = (voltage - lower_v) / (upper_v - lower_v)
    return low_value + fraction * (high_value - low_value)


def duty_for_voltage(channel, voltage, allow_limited=False):
    """Map a target to measured drive, allowing the settling error at edges.

    PWM quantization can put a calibrated endpoint a little below its nominal
    target. Clamp such a command to the measured endpoint duty; baseline
    correction still uses actual voltage and its smaller readback tolerance.
    """
    voltage = _number(voltage, "Target voltage")
    points = channel["points"]
    first_v = point_value(points, 0, "terminal_v")
    # Host/JSON targets may be float64; the stored endpoint is float32.
    # A rounding-only difference at zero duty must not invent a tiny drive
    # which would disable the measured zero-floor readback support.
    if (point_value(points, 0, "duty") == 0.0
            and abs(voltage - first_v) <= max(abs(first_v), 1.0) * 0.00000006):
        return 0.0
    # A real amplifier may sit a few mV above ground at zero duty. A 0 V
    # command means zero duty, with its actual terminal voltage still recorded.
    if voltage == 0.0 and point_value(points, 0, "duty") == 0.0 and abs(point_value(points, 0, "terminal_v")) <= VOLTAGE_TOLERANCE_V:
        return 0.0
    capability = channel.get("capability")
    if (allow_limited and capability and capability.get("complete") is True
            and voltage > point_value(points, -1, "terminal_v")):
        return capability["max_duty"]
    return interpolate(points, voltage, "drive_duty", VOLTAGE_TOLERANCE_V)


def corrected_current(channel, terminal_v, shunt_v, zero_drive=False):
    """Correct current without changing the measured terminal voltage.

    A zero-drive calibration endpoint is an averaged output floor, not a
    physical lower voltage limit. Its observed range plus the normal 5 mV
    readback edge may reuse that baseline at actual zero duty. Near-ground
    endpoints retain their existing narrow ground-floor allowance. Other
    measurements retain normal interpolation/upper-end coverage rules.
    """
    points = channel["points"]
    first_v = point_value(points, 0, "terminal_v")
    last_v = point_value(points, -1, "terminal_v")
    first_duty = point_value(points, 0, "duty")
    zero_min = point_value(points, 0, "zero_min_v")
    capability = channel.get("capability")
    if zero_drive:
        terminal_v = _number(terminal_v, "Calibration voltage")
    if (zero_drive and first_duty == 0.0 and zero_min is not None
            and zero_min - CALIBRATION_EDGE_TOLERANCE_V - 1e-9 <= terminal_v <= first_v):
        baseline = point_value(points, 0, "shunt_v")
    elif (zero_drive and first_duty == 0.0
            and 0.0 <= first_v <= VOLTAGE_TOLERANCE_V
            and -CALIBRATION_EDGE_TOLERANCE_V <= terminal_v <= first_v):
        baseline = point_value(points, 0, "shunt_v")
    elif (capability and capability.get("complete") is True
          and last_v < terminal_v <= capability.get("endpoint_max_v", last_v) + CALIBRATION_EDGE_TOLERANCE_V):
        # This is constant endpoint support measured during open calibration,
        # not an extrapolated bias slope or a globally wider voltage tolerance.
        baseline = point_value(points, -1, "shunt_v")
    else:
        baseline = interpolate(points, terminal_v, "shunt_v",
                               CALIBRATION_EDGE_TOLERANCE_V)
    return (shunt_v - baseline) / channel["shunt_ohm"]
