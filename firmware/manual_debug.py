"""Cooperative manual drive and live readback with fixed measurement storage.

Targets are ground-relative voltages. PWM previews describe a possible APPLY,
even while outputs are off.
START only monitors physical PWM zero; STOP never applies a source-relative zero.
Live voltages are raw bus ADC readings to GND, without source or shunt subtraction.
No histories, files, network operations or blocking waits are used here.
"""

try:
    from time import ticks_diff as _ticks_diff, ticks_ms as _ticks_ms
except ImportError:
    def _ticks_diff(now, before):
        return now - before
    _ticks_ms = None

try:
    from . import model
except ImportError:
    import model

_POLL_MS = 5
_ROLES = ("drain", "gate")


class ManualDebug:
    def __init__(self, instrument, settings, baseline=None):
        # Validate and own the settings without changing hardware or storage.
        self.settings = model.validate_settings(settings)
        self.instrument, self.baseline = instrument, baseline
        self.targets_uv = [0, 0]
        self.duty_u16 = [0, 0]
        self.applied_duty_u16 = [0, 0]
        self.outputs_enabled = False
        self.state, self.error = "idle", None
        self.shutdown_succeeded = False
        self.latest = None
        self.sample_revision = 0
        self.correction_active = False
        self._active_baseline = None
        self._codes = [0, 0, 0, 0]
        self._read_into = getattr(instrument, "read_codes_into", None)
        self._values = {"drain_bus_v": 0.0, "gate_bus_v": 0.0,
                        "id_a": 0.0, "ig_a": 0.0}
        self._phase = None
        self._clock = self._next_poll = self._deadline = 0
        self._measurement_deadline = 0
        self._last_clock = None
        self.set_targets(0, 0)

    def set_targets(self, drain_uv, gate_uv):
        """Validate targets to GND and update their PWM previews, without I/O."""
        ceiling = min(self.settings["driver_supply_uv"], 36000000)
        for value in (drain_uv, gate_uv):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Manual voltages must be integer microvolts")
            if not 0 <= value <= ceiling:
                raise ValueError("Manual terminal targets must be within 0..%.3f V to GND"
                                 % (ceiling / 1000000))
        baseline = self.baseline
        if baseline is not None and not baseline.compatible(
                self.settings, getattr(self.instrument, "identity", None)):
            baseline = None
        drain = self._preview("drain", drain_uv, baseline)
        gate = self._preview("gate", gate_uv, baseline)
        # A bad second target/preview cannot partially change a live command.
        self.targets_uv[0], self.targets_uv[1] = drain_uv, gate_uv
        self.duty_u16[0], self.duty_u16[1] = drain, gate
        self._active_baseline = baseline
        self.correction_active = baseline is not None

    def _preview(self, role, terminal_uv, baseline):
        terminal_v = model.volts_from_uv(terminal_uv)
        duty = (model.drive_duty(role, terminal_v) if baseline is None else
                baseline.drive_duty(role, terminal_v, self.settings))
        duty = model._number(duty, "PWM duty")
        if not 0.0 <= duty <= 1.0:
            raise ValueError("PWM duty must be within 0..1")
        return int(duty * 65535 + 0.5)

    def _stop_outputs(self):
        self.outputs_enabled = False
        self.shutdown_succeeded = False
        self._phase = None
        # Instrument.zero is gate-first and independent of I2C availability.
        self.instrument.zero()
        self.applied_duty_u16[0] = self.applied_duty_u16[1] = 0
        self.shutdown_succeeded = True

    def _pwm_readback(self, role, requested):
        # Pico PWM duty is quantized by the hardware counter. Read it only
        # when applying, rather than displaying the unquantized request as
        # the current drive. Portable adapters need not expose PWM objects.
        pwms = getattr(self.instrument, "_pwms", None)
        pwm = pwms.get(role) if pwms is not None else None
        getter = getattr(pwm, "duty_u16", None)
        if not callable(getter):
            return requested
        try:
            value = getter()
        except TypeError:
            # Some host adapters implement only the setter signature.
            return requested
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
            raise ValueError("Invalid PWM duty readback")
        return value

    def _monitor(self, enabled):
        self.outputs_enabled = enabled
        self.state, self._phase = "monitoring", "settle"
        self._clock = self._next_poll = 0
        self._last_clock = _ticks_ms() if _ticks_ms is not None else None
        self._deadline = self.settings["settle_ms"]
        self._measurement_deadline = 0

    def start(self):
        """Idempotently park both outputs and acquire live PWM-off readings."""
        self.error, self.latest = None, None
        try:
            self.set_targets(self.targets_uv[0], self.targets_uv[1])
            self._stop_outputs()
            # zero() clears pending conversions but deliberately does not use
            # I2C. A zero command restores continuous monitoring after a trigger.
            self.instrument.set_duty("drain", 0.0)
            self._monitor(False)
        except Exception as error:
            self._fail(error)

    def apply(self):
        """Stop the previous drive, then explicitly apply drain before gate."""
        self.error, self.latest = None, None
        try:
            self.set_targets(self.targets_uv[0], self.targets_uv[1])
            self._stop_outputs()
            self.shutdown_succeeded = False
            self.instrument.set_duty("drain", self.duty_u16[0] / 65535)
            self.instrument.set_duty("gate", self.duty_u16[1] / 65535)
            drain = self._pwm_readback("drain", self.duty_u16[0])
            gate = self._pwm_readback("gate", self.duty_u16[1])
            self.applied_duty_u16[0], self.applied_duty_u16[1] = drain, gate
            self._monitor(True)
        except Exception as error:
            self._fail(error)

    def stop(self):
        """Idempotently stop acquisition and remove both PWM drives."""
        try:
            self._stop_outputs()
            self.state = "stopped"
        except Exception as error:
            self.state, self.error = "error", "Could not stop outputs: " + str(error)

    def _fail(self, error):
        message = str(error)
        try:
            self._stop_outputs()
        except Exception as shutdown_error:
            message += "; could not stop outputs: " + str(shutdown_error)
        self.state, self.error = "error", message

    def _advance_clock(self, now_ms):
        if self._last_clock is not None:
            elapsed = _ticks_diff(now_ms, self._last_clock)
            if elapsed < 0:
                raise ValueError("Clock moved backwards")
            self._clock += elapsed
        self._last_clock = now_ms

    def _read(self, publish):
        # Native hardware fills the same four integers for the page's lifetime.
        if callable(self._read_into):
            self._read_into(self._codes)
            drain_bus_uv = self._codes[0] * 1250
            drain_bus = self._codes[0] * 0.00125
            drain_shunt = self._codes[1] * 0.0000025
            gate_bus = self._codes[2] * 0.0001953125
            gate_shunt = self._codes[3] * 0.0000003125
        else:
            # Portable test/tool adapters can supply physical raw measurements.
            pair = self.instrument.read_pair()
            drain, gate = pair["drain"], pair["gate"]
            drain_bus = model._number(drain["bus_v"], "Drain bus voltage")
            drain_shunt = model._number(drain["shunt_v"], "Drain shunt voltage")
            gate_bus = model._number(gate["bus_v"], "Gate bus voltage")
            gate_shunt = model._number(gate["shunt_v"], "Gate shunt voltage")
            drain_bus_uv = (drain["bus_code"] * 1250 if "bus_code" in drain else
                            int(drain_bus * 1000000 + 0.5))
        if not publish:
            return
        # Debug voltages show the raw bus ADCs to GND. Only the drain CURRENT
        # receives a baseline; gate current remains raw as in normal scanning.
        baseline = self._active_baseline
        if baseline is not None:
            drain_shunt -= baseline.offset_nv(0, drain_bus_uv) / 1000000000
        values = self._values
        values["drain_bus_v"] = drain_bus
        values["gate_bus_v"] = gate_bus
        values["id_a"] = drain_shunt / model.DRAIN_SHUNT_OHM
        values["ig_a"] = gate_shunt / model.GATE_SHUNT_OHM
        self.latest = values
        self.sample_revision += 1

    def _trigger(self):
        count = self.settings["ina_average_count"]
        self.instrument.begin_measurement(count)
        if _ticks_ms is not None:
            self._advance_clock(_ticks_ms())
        # 10% margin over the slower INA226 bus+shunt block, plus 50 ms.
        self._measurement_deadline = self._clock + (count * 2420 + 999) // 1000 + 50
        self._phase = "convert"

    def tick(self, now_ms):
        """Advance once; at most one paired read, with no blocking waits."""
        if self.state != "monitoring":
            return
        try:
            self._advance_clock(now_ms)
            if self._clock < self._next_poll:
                return
            self._next_poll = self._clock + _POLL_MS
            if self._phase == "settle":
                self.instrument.poll_alerts()
                if self._clock < self._deadline:
                    return
                self._read(False)  # Continuous endpoint check, never a sample.
                self._trigger()
            elif self._phase == "convert":
                # Accept a completed block even if a delayed UI tick is late.
                if self.instrument.poll_measurement():
                    self._read(True)
                    self._phase = "trigger"
                elif self._clock >= self._measurement_deadline:
                    raise ValueError("Manual sensor conversion timed out")
            else:
                # Unchanged outputs need a fresh trigger, not another settle.
                self._trigger()
        except Exception as error:
            self._fail(error)
