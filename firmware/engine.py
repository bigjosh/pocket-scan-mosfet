"""Cooperative bench scanner. No sleeping, filesystem writes or boot-time drive.

    Instrument contract: set_duty(role, fraction), begin_measurement(count),
    poll_measurement(), poll_alerts(), read_pair(), zero(), identity.
    read_pair returns {role: {bus_v, shunt_v, terminal_v}} for drain and gate.
    Call tick often; it performs at most one paired sample on each invocation.
"""

import math

try:
    from time import ticks_diff as _ticks_diff
except ImportError:
    def _ticks_diff(now, before):
        return now - before

try:
    from time import ticks_ms as _ticks_ms
except ImportError:
    _ticks_ms = None

try:
    from . import model, scan_timing
    from .calibration_data import PackedPoints, point_value, float_buffer
except ImportError:
    import model, scan_timing
    from calibration_data import PackedPoints, point_value, float_buffer

ROLES = ("drain", "gate")
PLATEAU_DELTA_V = 0.005
MAX_CORRECTIONS = 3
STATUS_POLL_MS = 5
MEASURE_FIELDS = ("vds", "vgs", "id_a", "ig_a", "drain_shunt_v", "gate_shunt_v")
STD_FIELDS = (("id_a", "id_std_a"), ("ig_a", "ig_std_a"),
              ("drain_shunt_v", "drain_shunt_std_v"), ("gate_shunt_v", "gate_shunt_std_v"))
POINT_FIELDS = ("terminal_v", "shunt_v", "shunt_std_v", "duty")


def _calibration_slots(count):
    """Reserve the complete numeric table before either output is driven."""
    return PackedPoints(count)


def _measure_values(reserve_floats=False):
    values = {"vds": 0.0, "vgs": 0.0, "id_a": 0.0, "ig_a": 0.0,
              "drain_shunt_v": 0.0, "gate_shunt_v": 0.0}
    if reserve_floats:
        for key in MEASURE_FIELDS:
            values[key] = float(0)
    return values


def _calibration_workspace(batches):
    batchmeans = float_buffer(batches * 6)
    summary = {"vds": 0.0, "vgs": 0.0, "id_a": 0.0, "ig_a": 0.0,
               "drain_shunt_v": 0.0, "gate_shunt_v": 0.0,
               "id_std_a": 0.0, "ig_std_a": 0.0,
               "drain_shunt_std_v": 0.0, "gate_shunt_std_v": 0.0,
               "index": 0, "total": 0, "curve": 0, "batches_completed": 0,
               "acquisition_blocks_completed": 0, "point_variance": "between_blocks",
               "batchmeans": batchmeans, "requested_vds": 0.0, "requested_vgs": 0.0,
               "drain_voltage_limited": False, "gate_voltage_limited": False,
               "voltage_limited": False}
    for key in MEASURE_FIELDS:
        summary[key] = float(0)
    for _, key in STD_FIELDS:
        summary[key] = float(0)
    summary["requested_vds"], summary["requested_vgs"] = float(0), float(0)
    return {"summary": summary, "batchmeans": batchmeans,
            "sample": float_buffer(6), "sums": float_buffer(6),
            "target": {"drain": 0.0, "gate": 0.0}, "duties": {"drain": 0.0, "gate": 0.0},
            "minimum": {"drain": None, "gate": None}, "maximum": {"drain": None, "gate": None},
            "settle": {role: {"previous_v": None, "previous_duty": None, "flat": 0, "attempts": 0}
                       for role in ROLES},
            "settle_sums": float_buffer(2),
            "settle_average": {role: {"terminal_v": 0.0} for role in ROLES}}


def _calibration_capability():
    return {"complete": False, "max_duty": float(0), "limit_reason": None,
            "endpoint_min_v": float(0), "endpoint_max_v": float(0)}


class OverrangeError(Exception):
    """Recognized measurement range/current limit, never a fabricated point.

    Kept in this portable module so the driver and host-side engine share one
    exception type without importing machine into acquisition/model tests.
    """

    def __init__(self, channel, kind, raw_code, sign, limit_value, limit_unit,
                 measured_value=None):
        if channel not in ROLES or kind not in ("shunt_saturation", "bus_saturation", "current_limit", "current_limit_alert"):
            raise ValueError("Invalid overrange category")
        if isinstance(sign, bool) or not isinstance(sign, int) or sign not in (-1, 1):
            raise ValueError("Invalid overrange sign")
        limit_value = model._number(limit_value, "Overrange limit")
        if limit_value <= 0:
            raise ValueError("Invalid overrange limit")
        if kind == "current_limit_alert":
            if raw_code is not None or measured_value is not None or limit_unit != "A":
                raise ValueError("Invalid latched current overrange")
            message = channel + " current limit alert latched"
        elif kind == "current_limit":
            measured_value = model._number(measured_value, "Overrange current")
            if (raw_code is not None or limit_unit != "A" or
                    abs(measured_value) <= limit_value or measured_value * sign <= 0):
                raise ValueError("Invalid current overrange")
            message = channel + " current limit exceeded"
        else:
            if (isinstance(raw_code, bool) or not isinstance(raw_code, int) or
                    not 0 <= raw_code <= 65535 or limit_unit != "V" or measured_value is not None):
                raise ValueError("Invalid ADC overrange")
            if ((kind == "shunt_saturation" and (raw_code not in (0x7fff, 0x8000) or
                    sign != (1 if raw_code == 0x7fff else -1))) or
                    (kind == "bus_saturation" and (raw_code != 0x7fff or sign != 1))):
                raise ValueError("Invalid ADC saturation code")
            message = channel + (" shunt ADC saturated" if kind == "shunt_saturation" else " bus ADC saturated")
        self.channel, self.kind, self.raw_code = channel, kind, raw_code
        self.sign, self.limit_value, self.limit_unit = sign, limit_value, limit_unit
        self.measured_value = measured_value
        super().__init__(message)

    def details(self):
        return {"channel": self.channel, "kind": self.kind, "raw_code": self.raw_code,
                "sign": self.sign, "limit_value": self.limit_value,
                "limit_unit": self.limit_unit, "measured_value": self.measured_value}


class Engine:
    def __init__(self, instrument, timing=None, clock_ms=None):
        self.instrument = instrument
        self._timing = timing
        # Native acquisition samples the clock again after commands finish.
        # Host/synthetic clocks must use the same domain as tick(now_ms).
        self._clock_ms = _ticks_ms if clock_ms is None else clock_ms
        self.state = "idle"
        self.progress = 0.0
        self.latest = None
        self.error = None
        self.fault = None
        self._last_completed = None
        self.shutdown_succeeded = False
        self._pending = []
        self._last_clock = None
        self._clock = 0
        self._measurement_waiting = False
        self._status_deadline = 0
        self._drain_floor_active = False
        self._cal_workspace = None

    def _configure(self, settings):
        if self.state in ("scanning", "calibrating"):
            raise ValueError("An operation is already running")
        return model.validate_settings(settings)

    def _begin(self, settings, mode):
        self.settings = settings
        self.state = mode
        self.progress = 0.0
        self.latest = None
        self.error = None
        self.fault = None
        self._last_completed = None
        self.shutdown_succeeded = False
        self._pending = []
        self._index = 0
        self._curve = 0
        # Scan calibration/settings stay fixed for the whole operation. Only
        # a new gate sweep needs a fresh inverse-map lookup; never carry this
        # cache across calibration, cancellation or a newly armed scan.
        self._gate_drive_curve = -1
        self._gate_drive_duty = 0.0
        self._applied = {}
        self._voltage_limited = {role: False for role in ROLES}
        self._phase = "drive"
        self._last_clock = None
        self._clock = 0
        self._measurement_waiting = False
        self._status_deadline = 0

    def start_scan(self, settings, calibration, validated_calibration=None):
        """Arm a scan; external callers fully validate by default.

        The UI may pass its exact owned, previously validated read-only map
        to avoid a duplicate table walk. Settings compatibility is never cached.
        """
        normalized = self._configure(settings)
        if calibration is not None and validated_calibration is calibration:
            model.validate_scan_calibration_settings(calibration, normalized)
        else:
            model.validate_scan_calibration(calibration, normalized)
        drains = model.sweep_uv(normalized["drain_start_uv"], normalized["drain_stop_uv"], normalized["drain_step_uv"])
        gates = model.sweep_uv(normalized["gate_start_uv"], normalized["gate_stop_uv"], normalized["gate_step_uv"])
        # Detach the settings and calibration from UI draft objects. Calibration
        # is read-only during an operation; its points need not be duplicated.
        self.calibration = calibration
        self._drains, self._gates = drains, gates
        self._total = len(drains) * len(gates)
        self._drain_park_v = model.drain_parking_voltage(calibration, normalized)
        self._drain_floor_active = self._drain_park_v > model.volts_from_uv(normalized["source_voltage_uv"])
        self._cal_workspace = None
        self._begin(normalized, "scanning")

    def start_calibration(self, settings):
        normalized = self._configure(settings)
        self._drain_floor_active = False
        targets = model.calibration_targets_uv(normalized)
        total = len(targets)
        done = {role: False for role in ROLES}
        counts = {role: 0 for role in ROLES}
        maximum_duty = model.calibration_duty_limit(normalized)
        # Keep the old calibration until both channel arrays are allocated.
        # Growing a list during acquisition needs an increasingly large
        # contiguous block on MicroPython's non-compacting heap.
        calibration = {
            "version": 2,
            "metadata": {
                "driver_supply_v": model.volts_from_uv(normalized["driver_supply_uv"]),
                "source_voltage_v": model.volts_from_uv(normalized["source_voltage_uv"]),
                "driver_supply_uv": normalized["driver_supply_uv"],
                "source_voltage_uv": normalized["source_voltage_uv"],
                "pwm_hz": model.PWM_HZ,
                "ina_config": 0x4123 | (model.INA_AVERAGE_COUNTS.index(normalized["ina_average_count"]) << 9),
                "acquisition_mode": model.ACQUISITION_MODE,
                "ina_average_count": normalized["ina_average_count"],
                "bus_conversion_us": model.INA_CONVERSION_US,
                "shunt_conversion_us": model.INA_CONVERSION_US,
                "calibration_repeat_blocks": model.CALIBRATION_REPEAT_BLOCKS,
                "shunts_provisional": True,
                "identity": getattr(self.instrument, "identity", {}),
            },
            "channels": {
                "drain": {"shunt_ohm": model.DRAIN_SHUNT_OHM,
                          "points": _calibration_slots(total), "capability": _calibration_capability()},
                "gate": {"shunt_ohm": model.GATE_SHUNT_OHM,
                         "points": _calibration_slots(total), "capability": _calibration_capability()},
            },
        }
        workspace = _calibration_workspace(model.CALIBRATION_REPEAT_BLOCKS)
        self._targets, self._total = targets, total
        self._cal_done, self._cal_counts = done, counts
        self._cal_duty_max = maximum_duty
        self.calibration = calibration
        self._cal_workspace = workspace
        self._begin(normalized, "calibrating")

    def cancel(self):
        if self.state not in ("scanning", "calibrating"):
            return
        was_calibrating = self.state == "calibrating"
        try:
            self._park()
            self.state = "cancelled"
            self._pending.append(("cancelled", {}))
        except Exception as exc:
            self.state = "error"
            self.error = "Cancel could not park outputs: " + str(exc)
            try:
                self._zero_outputs()
                self.shutdown_succeeded = True
            except Exception as shutdown_error:
                self.error += "; zero fallback failed: " + str(shutdown_error)
            self._pending.append(("error", self.error))
        if was_calibrating:
            self._release_calibration_workspace()

    def _release_calibration_workspace(self):
        """Retain only the scalar live summary after a calibration terminates."""
        if self._cal_workspace is None:
            return
        self._cal_workspace["summary"]["batchmeans"] = ()
        self._cal_workspace = None
        # These are aliases into the workspace; clearing just its owner would
        # retain the buffers during a saved-map reload or another calibration.
        self._batchmeans = self._sums = self._target = self._duties = None
        self._settle = self._settle_voltage_sums = None
        self._voltage_min = self._voltage_max = None

    def discard_calibration_workspace(self):
        """Release startup/terminated scratch; the caller must stop outputs first."""
        if self.state == "calibrating":
            raise ValueError("Cannot discard an active calibration")
        self._release_calibration_workspace()

    def _park(self):
        """Normal stop only; power loss/faults have no hardware isolation."""
        self.shutdown_succeeded = False
        source = model.volts_from_uv(self.settings["source_voltage_uv"])
        if self.state == "scanning" and self._drain_floor_active:
            # With a positive drain floor, remove gate drive before moving
            # drain to zero duty. This is sequencing, not hardware isolation.
            self._command("gate", 0.0)
            self._command("drain", 0.0)
            self.shutdown_succeeded = True
            return
        if self.state == "calibrating" or source == 0.0:
            self.instrument.zero()
            self.shutdown_succeeded = True
            return
        for role in ROLES:
            duty = model.duty_for_voltage(self.calibration["channels"][role], source)
            self.instrument.set_duty(role, duty)
        self.shutdown_succeeded = True

    def _zero_outputs(self):
        # The driver guarantees gate-first zeroing without reading I2C. Do
        # not route emergency shutdown through set_duty, which must poll a
        # possibly latched alert before any ordinary output change.
        self.instrument.zero()

    def _fail(self, error):
        was_scanning = self.state == "scanning"
        was_calibrating = self.state == "calibrating"
        message = str(error)
        self.shutdown_succeeded = False
        try:
            self._zero_outputs()
            self.shutdown_succeeded = True
        except Exception as shutdown_error:
            message += "; could not zero outputs: " + str(shutdown_error)
        if was_scanning and isinstance(error, OverrangeError) and self.shutdown_succeeded:
            curve, drain_index = divmod(self._index, len(self._drains))
            fault = error.details()
            fault.update(index=self._index, curve=curve,
                         requested_vds=model.volts_from_uv(self._drains[drain_index]),
                         requested_vgs=model.volts_from_uv(self._gates[curve]), phase=self._phase)
            self.state, self.error, self.fault = "partial", message, fault
            self.latest = self._last_completed
            self.progress = self._index / self._total
            # Only point events already emitted are retainable. A partial
            # averaging batch is not a measured point and must not be saved.
            self._batchmeans = []
            self._reset_batch()
            return [("partial", fault)]
        self.state = "error"
        self.error = message
        if was_calibrating:
            self._release_calibration_workspace()
        return [("error", message)]

    def _advance_clock(self, now_ms):
        if self._last_clock is not None:
            # MicroPython ticks_ms uses a power-of-two wrap period. The signed
            # difference helper handles it when available; CPython tests use
            # ordinary monotonic integers.
            delta = _ticks_diff(now_ms, self._last_clock)
            if delta < 0:
                raise ValueError("Clock moved backwards")
            self._clock += delta
        self._last_clock = now_ms

    def _settle_deadline(self):
        """Start the full configured interval after output/setup work finishes."""
        if self._clock_ms is not None:
            self._advance_clock(self._clock_ms())
        self._deadline = self._clock + self.settings["settle_ms"]

    @property
    def waiting_for_settle(self):
        """Whether scan drawing can overlap a post-command settling interval."""
        return (self.state == "scanning" and
                self._phase in ("park_drain", "settle_gate", "settle") and
                not self._measurement_waiting)

    def tick(self, now_ms):
        timing = self._timing
        started = timing.stamp() if timing is not None else None
        try:
            if self.state not in ("scanning", "calibrating") and not self._pending:
                # Idle UI ticks also run during TLS uploads. Keep this empty queue
                # in its startup allocation instead of retaining a fresh list in
                # the temporarily released contiguous TLS workspace on every tick.
                return self._pending
            events, self._pending = self._pending, []
            if self.state not in ("scanning", "calibrating"):
                return events
            try:
                self._advance_clock(now_ms)
                events.extend(self._tick())
            except Exception as exc:
                events.extend(self._fail(exc))
            return events
        finally:
            if started is not None:
                # Inclusive of sensor I/O; the I2C counter is nested, not additive.
                timing.add(scan_timing.ENGINE, started)

    def _timing_phase(self, phase):
        if self._timing is not None and self._timing.active:
            self._timing.phase(phase)

    def _timing_settle_phase(self):
        if self._phase == "park_drain":
            self._timing_phase(scan_timing.PARK_SETTLE)
        elif self._phase == "settle_gate":
            self._timing_phase(scan_timing.GATE_SETTLE)
        else:
            self._timing_phase(scan_timing.DRAIN_SETTLE)

    def _drive(self):
        self._timing_phase(scan_timing.DRIVE)
        if self.state == "calibrating":
            # The first point measures each channel's actual zero-duty floor.
            # Later targets advance from that floor, which need not be GND.
            increment = model.volts_from_uv(self._targets[self._index])
            ceiling = model.voltage_limit(self.settings)
            self._target = self._cal_workspace["target"]
            self._duties = self._cal_workspace["duties"]
            for role in ROLES:
                points = self.calibration["channels"][role]["points"]
                count = self._cal_counts[role]
                floor = points.value(0, "terminal_v") if count else 0.0
                self._target[role] = min(ceiling, floor + increment)
                if self._cal_done[role]:
                    continue
                # Carry the observed gain/offset forward rather than assuming
                # both amplifiers follow the nominal 8.5 gain all the way up.
                duty = (points.value(count - 1, "duty") +
                        (self._target[role] - points.value(count - 1, "terminal_v")) / model.PWM_FULL_SCALE_V
                        if count else 0.0)
                self._duties[role] = max(0.0, min(self._cal_duty_max, duty))
        else:
            curve, drain_index = divmod(self._index, len(self._drains))
            self._curve = curve
            source_uv = self.settings["source_voltage_uv"]
            self._target = {"drain": model.volts_from_uv(self._drains[drain_index] + source_uv),
                            "gate": model.volts_from_uv(self._gates[curve] + source_uv)}
            if self._gate_drive_curve != curve:
                self._gate_drive_duty = model.duty_for_voltage(
                    self.calibration["channels"]["gate"], self._target["gate"],
                    allow_limited=True)
                self._gate_drive_curve = curve
            drain_target = (max(self._target["drain"], self._drain_park_v)
                            if self._drain_floor_active else self._target["drain"])
            self._duties = {
                "drain": model.duty_for_voltage(self.calibration["channels"]["drain"],
                                                drain_target, allow_limited=True),
                "gate": self._gate_drive_duty}
        for role in ROLES:
            self._voltage_limited[role] = False
        if self.state == "calibrating":
            # Explicit open-fixture calibration has no DUT channel to sequence.
            for role in ROLES:
                if not self._cal_done[role]:
                    self._command(role, self._duties[role])
            self._phase = "settle"
        elif self._index % len(self._drains) == 0:
            # GPIO writes alone do not let the 10 ms RC filter settle. Park
            # drain, wait and verify it, then change gate in a later tick.
            idle = model.duty_for_voltage(self.calibration["channels"]["drain"],
                                          self._drain_park_v)
            self._command("drain", idle)
            self._phase = "park_drain"
        else:
            # Within one gate curve, retain the gate and move only the drain.
            self._command("drain", self._duties["drain"])
            self._phase = "settle"
        self._measurement_waiting = False
        self._reset_settle()
        if self.state == "calibrating":
            self._batchmeans = self._cal_workspace["batchmeans"]
            self._batch_count = 0
            self._voltage_min = self._cal_workspace["minimum"]
            self._voltage_max = self._cal_workspace["maximum"]
            for role in ROLES:
                self._voltage_min[role] = self._voltage_max[role] = None
        else:
            self._batchmeans = []
            self._voltage_min = {role: None for role in ROLES}
            self._voltage_max = {role: None for role in ROLES}
        self._reset_batch()
        self._settle_deadline()
        self._timing_settle_phase()

    def _command(self, role, duty):
        self.instrument.set_duty(role, duty)
        self._applied[role] = duty

    def _reset_settle(self):
        if self.state == "calibrating":
            self._settle = self._cal_workspace["settle"]
            for role in ROLES:
                status = self._settle[role]
                status["previous_v"] = status["previous_duty"] = None
                status["flat"] = status["attempts"] = 0
        else:
            self._settle = {role: {"previous_v": None, "previous_duty": None, "flat": 0, "attempts": 0}
                            for role in ROLES}

    def _drive_bounds(self, role):
        if self.state == "calibrating":
            return 0.0, self._cal_duty_max
        channel = self.calibration["channels"][role]
        capability = channel.get("capability")
        return (point_value(channel["points"], 0, "duty"), capability["max_duty"] if capability
                else point_value(channel["points"], -1, "duty"))

    def _reset_batch(self):
        self._count = 0
        if self.state == "calibrating":
            self._sums = self._cal_workspace["sums"]
            for index in range(6):
                self._sums[index] = 0.0
        else:
            self._sums = _measure_values()

    def _checked_read(self):
        pair = self.instrument.read_pair()
        limit = model.voltage_limit(self.settings)
        # Validate the complete pair before classifying a recoverable current
        # overrange. A simultaneous invalid/envelope reading on the other
        # channel must remain a hard fault, not a partially savable scan.
        for role in ROLES:
            reading = pair[role]
            for field in ("bus_v", "shunt_v", "terminal_v"):
                model._number(reading[field], role + " " + field)
            # Keep voltage reconstruction tied to raw shunt, never the corrected
            # current baseline, even if the hardware adapter supplies a value.
            reading["terminal_v"] = reading["bus_v"] - reading["shunt_v"]
            if (reading["bus_v"] > min(limit + 0.050, model.INA_MAX_BUS_V)
                    or reading["terminal_v"] < -0.050
                    or reading["terminal_v"] > limit + 0.050):
                raise ValueError(role + " voltage outside configured envelope")
            if self.state == "calibrating" and self._cal_done[role]:
                endpoint = self.calibration["channels"][role]["points"].value(self._cal_counts[role] - 1, "terminal_v")
                if abs(reading["terminal_v"] - endpoint) > model.VOLTAGE_TOLERANCE_V:
                    raise ValueError(role + " completed calibration output changed")
        for role, resistance, current_limit in (
                ("drain", model.DRAIN_SHUNT_OHM, model.DRAIN_CURRENT_LIMIT_A),
                ("gate", model.GATE_SHUNT_OHM, model.GATE_CURRENT_LIMIT_A)):
            current = pair[role]["shunt_v"] / resistance
            if abs(current) > current_limit:
                raise OverrangeError(role, "current_limit", None,
                                     1 if current > 0 else -1, current_limit, "A", current)
        if (self.state == "scanning" and self._drain_floor_active
                and self._applied.get("drain") == 0.0):
            # Check every read before/after gate changes and during averaging.
            # Accept the specified parking accuracy without allowing the wider
            # band to authorize a meaningfully negative VDS or new baseline.
            drain_v = pair["drain"]["terminal_v"]
            source = model.volts_from_uv(self.settings["source_voltage_uv"])
            if drain_v < source - model.DRAIN_PARK_SOURCE_ALLOWANCE_V:
                raise ValueError("Drain zero-drive floor is below source safety bound")
            if not (self._drain_park_v - model.DRAIN_ZERO_PARK_TOLERANCE_V <= drain_v
                    <= self._drain_park_v + model.DRAIN_ZERO_PARK_TOLERANCE_V):
                raise ValueError("Drain output drifted from calibrated zero-drive floor")
        return pair

    def _check_settled(self, pair, targets=None, strict_roles=()):
        waiting = False
        if targets is None:
            targets = self._target
        for role, target in targets.items():
            if self.state == "calibrating" and self._cal_done[role]:
                continue
            if self.state == "calibrating" and not self._cal_counts[role]:
                # No inverse map exists yet. Keep zero duty and measure its
                # stable output instead of trying to servo an offset to GND.
                # Raw envelope/current checks and averaging stability remain.
                if self._applied[role] != 0.0:
                    raise ValueError(role + " calibration must start at zero drive")
                continue
            measured = pair[role]["terminal_v"]
            error = target - measured
            status = self._settle[role]
            if self.state == "scanning" and role not in strict_roles:
                # An ordinary scan records the measured voltage. The calibrated
                # drive and fixed settling window are sufficient; do not chase
                # its requested value. Drain parking remains a strict sequence
                # prerequisite, and calibration still discovers/corrects drive.
                continue
            within_tolerance = abs(error) <= model.VOLTAGE_TOLERANCE_V
            if (self.state == "scanning" and role == "drain" and role in strict_roles
                    and self._drain_floor_active and self._applied.get("drain") == 0.0):
                within_tolerance = (target - model.DRAIN_ZERO_PARK_TOLERANCE_V <= measured
                                    <= target + model.DRAIN_ZERO_PARK_TOLERANCE_V)
            if within_tolerance:
                self._voltage_limited[role] = False
                continue
            previous = status["previous_v"]
            if previous is not None:
                delta = measured - previous
                increased = self._applied[role] >= status["previous_duty"]
                if delta < -PLATEAU_DELTA_V and error > 0 and increased:
                    raise ValueError("%s output decreased during increasing drive (%.4f to %.4f V)" %
                                     (role, previous, measured))
                status["flat"] = status["flat"] + 1 if abs(delta) <= PLATEAU_DELTA_V and increased else 0
            status["previous_v"] = measured
            status["previous_duty"] = self._applied[role]
            low, high = self._drive_bounds(role)
            at_ceiling = self._applied[role] >= high - 1e-9
            plateau = status["flat"] >= 2
            limited = error > 0 and (plateau or (at_ceiling and status["flat"] >= 1))
            if limited and role not in strict_roles:
                if self.state == "calibrating" and self._cal_counts[role] < 2:
                    raise ValueError(role + " has no usable calibration span")
                self._voltage_limited[role] = True
                continue
            if status["attempts"] >= MAX_CORRECTIONS:
                raise ValueError(role + " target not reached or output did not settle")
            corrected = self._applied[role] + error / model.PWM_FULL_SCALE_V
            if corrected < low:
                raise ValueError(role + " correction exceeds calibrated drive range")
            corrected = min(high, corrected)
            # Even an unchanged ceiling command resumes continuous monitoring
            # while the next settling interval runs after a triggered block.
            self._command(role, corrected)
            status["attempts"] += 1
            waiting = True
        if waiting:
            self._settle_deadline()
            self._timing_settle_phase()
            return False
        return True

    def _sample(self, pair):
        source = model.volts_from_uv(self.settings["source_voltage_uv"])
        if self.state == "calibrating":
            row = self._cal_workspace["sample"]
            row[0] = pair["drain"]["terminal_v"] - source
            row[1] = pair["gate"]["terminal_v"] - source
            row[4] = pair["drain"]["shunt_v"]
            row[5] = pair["gate"]["shunt_v"]
        else:
            row = {"vds": pair["drain"]["terminal_v"] - source,
                   "vgs": pair["gate"]["terminal_v"] - source,
                   "drain_shunt_v": pair["drain"]["shunt_v"],
                   "gate_shunt_v": pair["gate"]["shunt_v"]}
        for role, key, resistance in (("drain", "id_a", model.DRAIN_SHUNT_OHM),
                                       ("gate", "ig_a", model.GATE_SHUNT_OHM)):
            raw = pair[role]["shunt_v"]
            voltage = pair[role]["terminal_v"]
            minimum = self._voltage_min[role]
            maximum = self._voltage_max[role]
            minimum = voltage if minimum is None else min(minimum, voltage)
            maximum = voltage if maximum is None else max(maximum, voltage)
            self._voltage_min[role], self._voltage_max[role] = minimum, maximum
            if maximum - minimum > 2 * model.VOLTAGE_TOLERANCE_V:
                raise ValueError(role + " output changed during averaging")
            if self.state == "calibrating":
                row[2 if role == "drain" else 3] = raw / resistance
            else:
                # A source-referenced channel can have a nonzero output floor.
                # Endpoint support applies only at actual zero PWM duty.
                # Legacy tables without measured support retain their original
                # opt-in rule: both absolute command and duty must be zero.
                points = self.calibration["channels"][role]["points"]
                zero_drive = (self._applied[role] == 0.0 and
                              (self._target[role] == 0.0 or point_value(points, 0, "zero_min_v") is not None))
                try:
                    row[key] = model.corrected_current(
                        self.calibration["channels"][role], pair[role]["terminal_v"],
                        raw, zero_drive=zero_drive)
                except ValueError as error:
                    raise ValueError(role + ": " + str(error))
        for key in (range(6) if self.state == "calibrating" else self._sums):
            self._sums[key] += row[key]
        self._count += 1
        self._deadline = self._clock

    def _summary(self):
        if self.state == "calibrating":
            return self._calibration_summary()
        count = len(self._batchmeans)
        mean = {key: sum(batch[key] for batch in self._batchmeans) / count
                for key in self._batchmeans[0]}
        for key in ("id_a", "ig_a", "drain_shunt_v", "gate_shunt_v"):
            variance = (sum((batch[key] - mean[key]) ** 2 for batch in self._batchmeans) / (count - 1)
                        if count > 1 else 0.0)
            mean[key.replace("_a", "_std_a") if key.endswith("_a") else key.replace("_v", "_std_v")] = (math.sqrt(variance) if count > 1 else None)
        mean.update({"index": self._index, "total": self._total,
                     "curve": getattr(self, "_curve", 0),
                     "batches_completed": count,
                     "acquisition_blocks_completed": count,
                     "point_variance": "unavailable" if count == 1 else "between_blocks",
                     "batchmeans": list(self._batchmeans)})
        source = model.volts_from_uv(self.settings["source_voltage_uv"])
        flags = {}
        for role, field in (("drain", "vds"), ("gate", "vgs")):
            error = self._target[role] - (mean[field] + source)
            proved_limited = self._voltage_limited[role]
            if self.state == "scanning":
                # A target miss is informational, not an acquisition failure.
                # Keep limit flags specific to known calibrated drive bounds;
                # requested and actual voltages preserve all
                # other mismatches without changing the stored record schema.
                capability = self.calibration["channels"][role].get("capability")
                proved_limited = bool(capability and capability.get("complete") is True
                                      and self._applied[role] >= capability["max_duty"] - 1e-9)
            flags[role] = proved_limited and error > model.VOLTAGE_TOLERANCE_V
            if (self.state == "scanning" and role == "drain" and self._drain_floor_active
                    and self._applied[role] == 0.0 and self._target[role] < self._drain_park_v):
                flags[role] = flags[role] or -error > model.VOLTAGE_TOLERANCE_V
        if self.state == "scanning":
            curve, drain_index = divmod(self._index, len(self._drains))
            requested_vds = model.volts_from_uv(self._drains[drain_index])
            requested_vgs = model.volts_from_uv(self._gates[curve])
        else:
            requested_vds = self._target["drain"] - source
            requested_vgs = self._target["gate"] - source
        mean.update({"requested_vds": requested_vds,
                     "requested_vgs": requested_vgs,
                     "drain_voltage_limited": flags["drain"],
                     "gate_voltage_limited": flags["gate"],
                     "voltage_limited": any(flags.values())})
        return mean

    def _calibration_summary(self):
        # This live UI view is calibration-only. Scan point events still use
        # independent snapshots; no emitted scan row aliases mutable scratch.
        count = self._batch_count
        mean = self._cal_workspace["summary"]
        for column, key in enumerate(MEASURE_FIELDS):
            total = 0.0
            for index in range(count):
                total += self._batchmeans[index * 6 + column]
            mean[key] = total / count
        for column, (key, std_key) in enumerate(STD_FIELDS):
            variance = 0.0
            if count > 1:
                for index in range(count):
                    difference = self._batchmeans[index * 6 + column + 2] - mean[key]
                    variance += difference * difference
                variance /= count - 1
            mean[std_key] = math.sqrt(variance)
        mean["index"], mean["total"], mean["curve"] = self._index, self._total, self._curve
        mean["batches_completed"] = count
        mean["acquisition_blocks_completed"] = count
        source = model.volts_from_uv(self.settings["source_voltage_uv"])
        mean["requested_vds"] = self._target["drain"] - source
        mean["requested_vgs"] = self._target["gate"] - source
        for role, field, flag in (("drain", "vds", "drain_voltage_limited"),
                                  ("gate", "vgs", "gate_voltage_limited")):
            error = self._target[role] - (mean[field] + source)
            mean[flag] = self._voltage_limited[role] and error > model.VOLTAGE_TOLERANCE_V
        mean["voltage_limited"] = mean["drain_voltage_limited"] or mean["gate_voltage_limited"]
        return mean

    def _finish_point(self, result=None):
        if result is None:
            result = self._summary()
        self.latest = result
        if self.state == "scanning":
            self._last_completed = result
        events = []
        if self.state == "calibrating":
            for role, voltage_key in (("drain", "vds"), ("gate", "vgs")):
                if self._cal_done[role]:
                    continue
                channel = self.calibration["channels"][role]
                points = channel["points"]
                count = self._cal_counts[role]
                voltage = result[voltage_key] + model.volts_from_uv(self.settings["source_voltage_uv"])
                shunt = result[role + "_shunt_v"]
                deviation = result[role + "_shunt_std_v"]
                duty = self._applied[role]
                limited = self._voltage_limited[role]
                at_ceiling = self._applied[role] >= self._cal_duty_max - 1e-9
                last_target = (self._index + 1 == self._total or
                               self._target[role] >= model.voltage_limit(self.settings))
                finished = limited or at_ceiling or last_target
                if not count or voltage > points.value(count - 1, "terminal_v") + PLATEAU_DELTA_V:
                    if count and duty <= points.value(count - 1, "duty"):
                        raise ValueError(role + " calibration drive is not monotonic")
                    points.append(voltage, shunt, deviation, duty)
                    count += 1
                    self._cal_counts[role] = count
                    if count == 1:
                        # Include the stored float32 mean in observed support.
                        stored_v = points.value(0, "terminal_v")
                        points.set_zero_bounds(min(self._voltage_min[role], stored_v),
                                               max(self._voltage_max[role], stored_v))
                elif voltage < points.value(count - 1, "terminal_v") - PLATEAU_DELTA_V:
                    raise ValueError(role + " calibration output rolled over")
                elif not finished:
                    raise ValueError(role + " calibration made no measurable progress")
                else:
                    # Consolidate a plateau into the final measured state,
                    # keeping voltage, baseline and applied duty matched. This
                    # avoids leaving the calibrated endpoint several mV below
                    # the voltage actually held at the discovered drive bound.
                    if count < 2 or voltage <= points.value(count - 2, "terminal_v"):
                        raise ValueError(role + " plateau leaves no monotonic calibration span")
                    # Keep the lower measured plateau drive for inverse lookup;
                    # a higher confirmation duty would distort nearby targets.
                    drive_v = points.value(count - 1, "drive_v", points.value(count - 1, "terminal_v"))
                    drive_duty = points.value(count - 1, "drive_duty", points.value(count - 1, "duty"))
                    points.replace(count - 1, voltage, shunt, deviation, duty)
                    points.set_drive_endpoint(drive_v, drive_duty)
                if finished:
                    self._cal_done[role] = True
                    capability = channel["capability"]
                    capability["max_duty"] = self._applied[role]
                    capability["limit_reason"] = ("drive_ceiling" if at_ceiling else
                                                  "plateau" if limited else "rail_envelope")
                    capability["endpoint_min_v"] = min(self._voltage_min[role], points.value(-1, "terminal_v"))
                    capability["endpoint_max_v"] = max(self._voltage_max[role], points.value(-1, "terminal_v"))
                    capability["complete"] = True
        else:
            events.append(("point", result))
        self._index += 1
        self.progress = self._index / self._total
        events.append(("progress", {"progress": self.progress, "index": self._index, "total": self._total}))
        finished = self._index == self._total or (self.state == "calibrating" and all(self._cal_done.values()))
        if finished:
            self._park()
            if self.state == "calibrating":
                model.validate_calibration(self.calibration, self.settings)
                events.append(("calibrated", self.calibration))
                self._release_calibration_workspace()
            self.state = "complete"
            self.progress = 1.0
            events.append(("progress", {"progress": 1.0, "index": self._index, "total": self._total}))
            events.append(("complete", {"points": self._index}))
        else:
            self._phase = "drive"
        return events

    def _tick(self):
        if self._phase == "drive":
            self._drive()
            return []
        if self._clock < self._status_deadline:
            return []
        self._status_deadline = self._clock + STATUS_POLL_MS
        if self._measurement_waiting:
            # Ready registers are stable in triggered mode. A delayed UI poll
            # must accept completed data before checking the timeout deadline.
            if not self.instrument.poll_measurement():
                if self._clock >= self._measurement_deadline:
                    raise ValueError("INA226 conversion timed out")
                return []
            self._measurement_waiting = False
            pair = self._checked_read()
            self._timing_phase(scan_timing.PROCESS)
        else:
            self.instrument.poll_alerts()
            if self._clock < self._deadline:
                return []
            if self._phase == "settle":
                reference = self._checked_read()
                if self.state == "scanning":
                    # Compare the completed block with a fresh unaveraged
                    # post-settle reference. One averaged block alone cannot
                    # reveal its internal extrema or establish a span.
                    for role in ROLES:
                        self._voltage_min[role] = self._voltage_max[role] = reference[role]["terminal_v"]
            count = self.settings["ina_average_count"]
            # Observed conversion wall time includes trigger I/O and the final
            # checked read; it can also include work delaying a ready poll.
            self._timing_phase(scan_timing.CONVERT)
            self.instrument.begin_measurement(count)
            self._measurement_waiting = True
            # TI specifies 1.21 ms maximum for each nominal 1.1 ms
            # conversion; include both conversions and scheduling/I2C margin.
            self._measurement_deadline = self._clock + (count * 2420 + 999) // 1000 + 50
            return []
        if self._phase == "park_drain":
            if not self._check_settled(pair, {"drain": self._drain_park_v}, strict_roles=("drain",)):
                return []
            self._command("gate", self._duties["gate"])
            self._phase = "settle_gate"
            self._reset_settle()
            self._settle_deadline()
            self._timing_settle_phase()
            return []
        if self._phase == "settle_gate":
            targets = {"drain": self._drain_park_v,
                       "gate": self._target["gate"]}
            if not self._check_settled(pair, targets, strict_roles=("drain",)):
                return []
            self._command("drain", self._duties["drain"])
            self._phase = "settle"
            self._reset_settle()
            self._settle_deadline()
            self._timing_settle_phase()
            return []
        if self._phase == "settle":
            if not self._check_settled(pair):
                return []
            self._phase = "sample"
            # The completed post-settling hardware block is the first (and
            # for scans, only) measurement. Parking checks are separate.
        self._sample(pair)
        if self.state == "calibrating":
            offset = self._batch_count * 6
            for column in range(6):
                self._batchmeans[offset + column] = self._sums[column] / self._count
            self._batch_count += 1
            completed = self._batch_count
        else:
            self._batchmeans.append({key: value / self._count for key, value in self._sums.items()})
            completed = len(self._batchmeans)
        self.latest = self._summary()
        required = model.CALIBRATION_REPEAT_BLOCKS if self.state == "calibrating" else 1
        self.progress = (self._index + completed / required) / self._total
        if completed == required:
            return self._finish_point(self.latest)
        self._reset_batch()
        return [("progress", {"progress": self.progress, "index": self._index, "total": self._total})]
