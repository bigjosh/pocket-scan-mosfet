"""Portable settings, exact scan geometry and nominal resistor-based drive math."""

import math

PWM_HZ = 100000
# Nominal hardware values. The physical drain bias is Pico 3V3, independent
# of the configured source voltage used to request/reconstruct VDS and VGS.
PWM_RAIL_V = 3.3
DRAIN_BIAS_V = 3.3
FEEDBACK_TOP_OHM = 68000.0
FEEDBACK_BOTTOM_OHM = 10000.0
DRAIN_PWM_FILTER_OHM = 1000.0
DRAIN_BIAS_RESISTOR_OHM = 6800.0
AMPLIFIER_GAIN = 1.0 + FEEDBACK_TOP_OHM / FEEDBACK_BOTTOM_OHM
GATE_PWM_FULL_SCALE_V = AMPLIFIER_GAIN * PWM_RAIL_V
DRAIN_PWM_FULL_SCALE_V = (AMPLIFIER_GAIN * PWM_RAIL_V * DRAIN_BIAS_RESISTOR_OHM /
                          (DRAIN_PWM_FILTER_OHM + DRAIN_BIAS_RESISTOR_OHM))
DRAIN_ZERO_DUTY_V = (AMPLIFIER_GAIN * DRAIN_BIAS_V * DRAIN_PWM_FILTER_OHM /
                     (DRAIN_PWM_FILTER_OHM + DRAIN_BIAS_RESISTOR_OHM))
INA_MAX_BUS_V = 36.0
MAX_POINTS = 4096
DRAIN_SHUNT_OHM = 0.789  # Fixed effective resistance of the fitted nominal 1-ohm shunt.
GATE_SHUNT_OHM = 638.0  # Provisional in-circuit meter reading.
MICROVOLTS_PER_VOLT = 1000000
VOLTAGE_FIELDS = ("drain_start", "drain_stop", "drain_step",
                  "gate_start", "gate_stop", "gate_step",
                  "driver_supply", "source_voltage")
INA_AVERAGE_COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)
ACQUISITION_MODE = "ina226_triggered_v1"
INA_CONVERSION_US = 1100
LEGACY_TIMING = {"batches": 10, "samples_per_batch": 50,
                 "sample_pause_ms": 4, "settle_ms": 200}

DEFAULT_SETTINGS = {
    "version": 3,
    "drain_start_uv": 100000, "drain_stop_uv": 2000000, "drain_step_uv": 500000,
    "gate_start_uv": 0, "gate_stop_uv": 2000000, "gate_step_uv": 500000,
    "driver_supply_uv": 18800000, "source_voltage_uv": 3300000,
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
    """Access a settings voltage in volts; also accepts legacy volts-based settings."""
    uv_key = key[:-2] + "_uv"
    if uv_key in settings:
        return volts_from_uv(settings[uv_key])
    return _number(settings[key], key)


def voltage_limit(settings):
    """Requested terminal-to-GND envelope, separate from measured capability."""
    rail = setting_voltage_v(settings, "driver_supply_v")
    return max(0.0, min(rail, INA_MAX_BUS_V))



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



def drive_duty(role, terminal_v):
    """Nominal output-to-GND command, bounded only by the PWM duty range.

    Drain's 1k PWM / 6.8k bias summing node attenuates the PWM before the
    noninverting gain. Gate uses the noninverting gain directly. Actual output
    may differ or clip at the analog supply; measurements remain authoritative.
    """
    target = _number(terminal_v, "Terminal target")
    if role == "drain":
        duty = (target - DRAIN_ZERO_DUTY_V) / DRAIN_PWM_FULL_SCALE_V
    elif role == "gate":
        duty = target / GATE_PWM_FULL_SCALE_V
    else:
        raise ValueError("Unknown output channel")
    return max(0.0, min(1.0, duty))
