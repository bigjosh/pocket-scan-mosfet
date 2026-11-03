"""Cooperative direct-drive scanner: one settled hardware average per point.

Optional open-output baseline subtraction changes current only. PWM uses
nominal resistor gain, optionally anchored at a captured source-relative zero.
Reported ADC endpoints and communication failures still stop outputs.
Instrument supplies set_duty, begin_measurement, poll_measurement, poll_alerts,
read_pair and gate-first zero. tick() never sleeps or writes files.
"""

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
except ImportError:
    import model, scan_timing

ROLES = ("drain", "gate")
STATUS_POLL_MS = 5


class OverrangeError(Exception):
    """Reported ADC endpoint, never a fabricated out-of-range measurement.

    Kept in this portable module so the driver and host-side engine share one
    exception type without importing machine into acquisition/model tests.
    """

    def __init__(self, channel, kind, raw_code, sign, limit_value, limit_unit,
                 measured_value=None):
        if channel not in ROLES or kind not in ("shunt_saturation", "bus_saturation"):
            raise ValueError("Invalid overrange category")
        if isinstance(sign, bool) or not isinstance(sign, int) or sign not in (-1, 1):
            raise ValueError("Invalid overrange sign")
        limit_value = model._number(limit_value, "Overrange limit")
        if limit_value <= 0:
            raise ValueError("Invalid overrange limit")
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
    def __init__(self, instrument, timing=None, clock_ms=None, baseline=None):
        self.instrument = instrument
        self.baseline = baseline
        self._active_baseline = None
        self._timing = timing
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

    def current_correction(self, settings):
        """Snapshot provenance once before scan preparation; no table copying."""
        baseline = self.baseline
        if baseline is None or not baseline.compatible(
                settings, getattr(self.instrument, "identity", None)):
            return None
        return baseline.metadata(settings)

    def start_scan(self, settings):
        """Validate finite settings and arm the exact grid without driving."""
        if self.state == "scanning":
            raise ValueError("An operation is already running")
        normalized = model.validate_settings(settings)
        drains = model.sweep_uv(normalized["drain_start_uv"], normalized["drain_stop_uv"],
                                normalized["drain_step_uv"])
        gates = model.sweep_uv(normalized["gate_start_uv"], normalized["gate_stop_uv"],
                               normalized["gate_step_uv"])
        self.settings = normalized
        self._active_baseline = (self.baseline if self.baseline is not None and
            self.baseline.compatible(normalized, getattr(self.instrument, "identity", None)) else None)
        self._drains, self._gates = drains, gates
        self._total = len(drains) * len(gates)
        self.state = "scanning"
        self.progress = 0.0
        self.latest = self.error = self.fault = self._last_completed = None
        self.shutdown_succeeded = False
        self._pending = []
        self._index = self._curve = 0
        self._gate_drive_curve = -1
        self._applied = {"drain": 0.0, "gate": 0.0}
        self._phase = "drive"
        self._last_clock = None
        self._clock = 0
        self._measurement_waiting = False
        self._status_deadline = 0

    def cancel(self):
        if self.state != "scanning":
            return
        try:
            self._park()
            self.state = "cancelled"
            self._pending.append(("cancelled", {}))
        except Exception as exc:
            self.state = "error"
            self.error = "Cancel could not stop outputs: " + str(exc)
            self._pending.append(("error", self.error))

    def _zero_outputs(self):
        # Hardware zero is gate-first and performs no I2C operations. Emergency
        # shutdown must not depend on reading a failed bus.
        self.instrument.zero()
        self._measurement_waiting = False
        if hasattr(self, "_applied"):
            self._applied["gate"] = self._applied["drain"] = 0.0

    def _park(self):
        self.shutdown_succeeded = False
        self._zero_outputs()
        self.shutdown_succeeded = True

    def _fail(self, error):
        was_scanning = self.state == "scanning"
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
            return [("partial", fault)]
        self.state, self.error = "error", message
        return [("error", message)]

    def _advance_clock(self, now_ms):
        if self._last_clock is not None:
            delta = _ticks_diff(now_ms, self._last_clock)
            if delta < 0:
                raise ValueError("Clock moved backwards")
            self._clock += delta
        self._last_clock = now_ms

    def _settle_deadline(self):
        """Start the complete configured dwell after PWM/setup work finishes."""
        if self._clock_ms is not None:
            self._advance_clock(self._clock_ms())
        self._deadline = self._clock + self.settings["settle_ms"]

    @property
    def waiting_for_settle(self):
        return (self.state == "scanning" and self._phase == "settle" and
                not self._measurement_waiting)

    def tick(self, now_ms):
        timing = self._timing
        started = timing.stamp() if timing is not None else None
        try:
            if self.state != "scanning" and not self._pending:
                return self._pending
            events, self._pending = self._pending, []
            if self.state != "scanning":
                return events
            try:
                self._advance_clock(now_ms)
                events.extend(self._tick())
            except Exception as exc:
                events.extend(self._fail(exc))
            return events
        finally:
            if started is not None:
                timing.add(scan_timing.ENGINE, started)

    def _timing_phase(self, phase):
        if self._timing is not None and self._timing.active:
            self._timing.phase(phase)

    def _command(self, role, duty):
        self.instrument.set_duty(role, duty)
        self._applied[role] = duty

    def _drive(self):
        self._timing_phase(scan_timing.DRIVE)
        curve, drain_index = divmod(self._index, len(self._drains))
        self._curve = curve
        source_uv = self.settings["source_voltage_uv"]
        drain_target = model.volts_from_uv(self._drains[drain_index] + source_uv)
        gate_target = model.volts_from_uv(self._gates[curve] + source_uv)
        # Move drain back to the first requested voltage before increasing gate
        # for a new curve. No parking/voltage-verification acquisition is needed.
        baseline = self._active_baseline
        drain_duty = (model.drive_duty("drain", drain_target) if baseline is None else
                      baseline.drive_duty("drain", drain_target, self.settings))
        self._command("drain", drain_duty)
        if self._gate_drive_curve != curve:
            gate_duty = (model.drive_duty("gate", gate_target) if baseline is None else
                         baseline.drive_duty("gate", gate_target, self.settings))
            self._command("gate", gate_duty)
            self._gate_drive_curve = curve
        self._phase = "settle"
        self._measurement_waiting = False
        self._settle_deadline()
        self._timing_phase(scan_timing.DRAIN_SETTLE)

    def _checked_read(self):
        """Read raw voltages without independent current or accuracy thresholds."""
        pair = self.instrument.read_pair()
        for role in ROLES:
            reading = pair[role]
            # Nonfinite/invalid transport data cannot become a measurement.
            bus = model._number(reading["bus_v"], role + " bus voltage")
            shunt = model._number(reading["shunt_v"], role + " shunt voltage")
            reading["terminal_v"] = bus - shunt
        return pair

    def _finish_point(self, pair):
        source = model.volts_from_uv(self.settings["source_voltage_uv"])
        curve, drain_index = divmod(self._index, len(self._drains))
        # Each emitted snapshot owns its scalars; later points cannot mutate it.
        drain_shunt, gate_shunt = pair["drain"]["shunt_v"], pair["gate"]["shunt_v"]
        baseline = self._active_baseline
        if baseline is not None:
            drain_bus_uv = (pair["drain"]["bus_code"] * 1250 if "bus_code" in pair["drain"] else
                            int(pair["drain"]["bus_v"] * 1000000 + 0.5))
            gate_bus_uv = (pair["gate"]["bus_code"] * 1250 if "bus_code" in pair["gate"] else
                           int(pair["gate"]["bus_v"] * 1000000 + 0.5))
            drain_shunt -= baseline.offset_nv(0, drain_bus_uv) / 1000000000
            gate_shunt -= baseline.offset_nv(1, gate_bus_uv) / 1000000000
        result = {"vds": pair["drain"]["terminal_v"] - source,
                  "vgs": pair["gate"]["terminal_v"] - source,
                  "id_a": drain_shunt / model.DRAIN_SHUNT_OHM,
                  "ig_a": gate_shunt / model.GATE_SHUNT_OHM,
                  "index": self._index, "total": self._total, "curve": curve,
                  "requested_vds": model.volts_from_uv(self._drains[drain_index]),
                  "requested_vgs": model.volts_from_uv(self._gates[curve]),
                  "acquisition_blocks_completed": 1}
        self.latest = self._last_completed = result
        self._index += 1
        self.progress = self._index / self._total
        events = [("point", result), ("progress", {"progress": self.progress,
                  "index": self._index, "total": self._total})]
        if self._index == self._total:
            self._park()
            self.state = "complete"
            events.append(("complete", {"points": self._index}))
        else:
            # UI can issue the next drive on its following tick, then paint
            # this completed point inside the new point's settling interval.
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
            # A delayed display tick must accept an already finished block.
            if not self.instrument.poll_measurement():
                if self._clock >= self._measurement_deadline:
                    raise ValueError("INA226 conversion timed out")
                return []
            self._measurement_waiting = False
            pair = self._checked_read()
            self._timing_phase(scan_timing.PROCESS)
            self._phase = "sample"
            return self._finish_point(pair)
        self.instrument.poll_alerts()
        if self._clock < self._deadline:
            return []
        # One continuous reading catches a returned ADC endpoint before averaging;
        # it is never used as a stability reference or an extra saved sample.
        self._checked_read()
        count = self.settings["ina_average_count"]
        self._timing_phase(scan_timing.CONVERT)
        self.instrument.begin_measurement(count)
        self._measurement_waiting = True
        self._measurement_deadline = self._clock + (count * 2420 + 999) // 1000 + 50
        return []
