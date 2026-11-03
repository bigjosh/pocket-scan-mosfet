"""Pico 2 W PWM drive and INA226 voltage readback.

No output is enabled by construction. VBUS on each INA226 is wired to IN+
(the amplifier side of its shunt), so terminal_v subtracts the RAW shunt
drop. This layer always returns raw readings; Engine may separately subtract
an open-output baseline for current only.

PWM changes enable continuous, unaveraged monitoring during settling. After
settling, begin_measurement selects one triggered hardware-averaged block;
poll_measurement waits cooperatively for both sensors. Each ADC conversion
takes 1.1 ms nominal. Current comparators are disabled. A returned signed
shunt ADC endpoint stops both outputs; intermediate conversions are not
inspected, so this is not a transient-current interlock.
"""

from machine import I2C, PWM, Pin

try:
    from .engine import OverrangeError
    from . import model, scan_timing
except ImportError:
    from engine import OverrangeError
    import model, scan_timing


PWM_HZ = 100000
INA_CONFIG = 0x4127
INA_TRIGGER_CONFIG = 0x4123
ALERT_MASK = 0  # Disable every programmable comparator and alert latch.
ALERT_CONTROL_MASK = 0xFC03
READY_FLAG = 0x0008
CHANNELS = (("drain", 20, 0x44), ("gate", 21, 0x41))
# Keep sensor reads in drain/gate order, but remove NMOS gate drive first
# whenever stopping outputs, including a driver fault before Engine sees it.
SHUTDOWN_CHANNELS = (CHANNELS[1], CHANNELS[0])
SHUNT_LSB_V = 0.0000025
BUS_LSB_V = 0.00125


class HardwareError(Exception):
    """Hardware initialization, readback or output control failed."""


class Instrument:
    """Two output channels; every hardware error attempts to stop both."""

    def __init__(self, timing=None):
        self._timing = timing
        self.identity = {}
        self._pwms = {}
        self._closed = False
        self._bus = None
        self._monitoring = True
        self._measurement_pending = False
        self._ready_mask = 0
        self._i2c_bytes = bytearray(2)
        try:
            # Clear any outputs left by an interrupted REPL experiment before
            # initializing either PWM peripheral or touching I2C.
            errors = self._force_pins_low()
            if errors:
                raise HardwareError("Initial output shutdown: " + "; ".join(errors))
            for role, gpio, _ in CHANNELS:
                self._pwms[role] = PWM(Pin(gpio), freq=PWM_HZ, duty_u16=0)
            self._bus = I2C(1, sda=Pin(18), scl=Pin(19), freq=100000)
            for role, _, address in CHANNELS:
                manufacturer = self._read16(address, 0xFE)
                die = self._read16(address, 0xFF)
                if manufacturer != 0x5449 or die not in (0x2260, 0x2261):
                    raise HardwareError(
                        "%s INA226 identity at 0x%02X: manufacturer=%04X die=%04X"
                        % (role, address, manufacturer, die))
                self._bus.writeto_mem(address, 0, bytes((INA_CONFIG >> 8,
                                                       INA_CONFIG & 0xFF)))
                config = self._read16(address, 0)
                if config != INA_CONFIG:
                    raise HardwareError("%s INA226 configuration readback %04X"
                                        % (role, config))
                self.identity[role] = {"address": address,
                                       "manufacturer": manufacturer,
                                       "die": die, "config": config}
                # Soft reset does not reset the INA226. Explicitly disable
                # comparator settings left by older firmware or diagnostics.
                self._write16(address, 6, ALERT_MASK)
                if self._read16(address, 6) & ALERT_CONTROL_MASK != ALERT_MASK:
                    raise HardwareError(role + " INA226 alert mask readback failed")
        except Exception as exc:
            try:
                self.close()
            except Exception:
                # Preserve the failure that prevented initialization. close()
                # already attempted every PWM and forced both pins low.
                pass
            raise HardwareError("Instrument initialization failed: %s" % exc)

    @staticmethod
    def _force_pins_low():
        errors = []
        for role, gpio, _ in SHUTDOWN_CHANNELS:
            try:
                Pin(gpio, Pin.OUT, value=0)
            except Exception as exc:
                errors.append("%s pin low: %s" % (role, exc))
        return errors

    def _read16(self, address, register):
        timing = self._timing
        started = timing.stamp() if timing is not None else None
        try:
            if hasattr(self._bus, "readfrom_mem_into"):
                value = self._i2c_bytes
                self._bus.readfrom_mem_into(address, register, value)
            else:  # Portable host fakes; Pico uses the reusable buffer above.
                value = self._bus.readfrom_mem(address, register, 2)
            if len(value) != 2:
                raise HardwareError("Short INA226 read at 0x%02X register 0x%02X"
                                    % (address, register))
            return (value[0] << 8) | value[1]
        finally:
            if started is not None:
                timing.add(scan_timing.I2C, started)

    def _write16(self, address, register, value):
        timing = self._timing
        started = timing.stamp() if timing is not None else None
        try:
            self._i2c_bytes[0], self._i2c_bytes[1] = value >> 8, value & 255
            self._bus.writeto_mem(address, register, self._i2c_bytes)
        finally:
            if started is not None:
                timing.add(scan_timing.I2C, started)

    def poll_alerts(self):
        """Compatibility name: verify disabled alerts and latch conversion-ready.

        This does not impose a current threshold. read_pair() checks the actual
        ADC codes, and only its returned endpoints are saturation stops.
        """
        self._ensure_open()
        try:
            for index, (role, _, address) in enumerate(CHANNELS):
                status = self._read16(address, 6)
                if status & ALERT_CONTROL_MASK != ALERT_MASK:
                    raise HardwareError(role + " INA226 alert configuration changed")
                if self._measurement_pending and status & READY_FLAG:
                    self._ready_mask |= 1 << index
        except Exception as exc:
            self._raise_after_stop(HardwareError("INA226 status read failed: %s" % exc))

    def _ensure_monitoring(self):
        self.poll_alerts()
        if self._monitoring:
            return
        try:
            for _, _, address in CHANNELS:
                self._write16(address, 0, INA_CONFIG)
            self._monitoring = True
            self._measurement_pending = False
            self._ready_mask = 0
        except Exception as exc:
            self._raise_after_stop(HardwareError("INA226 monitoring failed: %s" % exc))

    def begin_measurement(self, average_count):
        """Trigger both chips after settling; no sleeps and no stale ready flag."""
        self._ensure_open()
        if (isinstance(average_count, bool) or not isinstance(average_count, int)
                or average_count not in model.INA_AVERAGE_COUNTS):
            self._raise_after_stop(ValueError("Invalid INA226 averaging count"))
        if self._measurement_pending:
            self._raise_after_stop(HardwareError("INA226 measurement already pending"))
        # Verify configuration, then discard ready bits from the previous
        # continuous frame before triggering a new averaged block.
        self.poll_alerts()
        config = INA_TRIGGER_CONFIG | (model.INA_AVERAGE_COUNTS.index(average_count) << 9)
        self._ready_mask = 0
        try:
            for _, _, address in CHANNELS:
                self._write16(address, 0, config)
                if self._read16(address, 0) != config:
                    raise HardwareError("INA226 triggered configuration readback failed")
            self._measurement_pending = True
            self._monitoring = False
        except Exception as exc:
            self._raise_after_stop(HardwareError("INA226 trigger failed: %s" % exc))
        if self._timing is not None and self._timing.active:
            self._timing.count(scan_timing.ADC_BLOCKS)

    def poll_measurement(self):
        self._ensure_open()
        if not self._measurement_pending:
            self._raise_after_stop(HardwareError("No INA226 measurement pending"))
        if self._timing is not None and self._timing.active:
            self._timing.count(scan_timing.POLLS)
        self.poll_alerts()
        return self._ready_mask == 3

    def _ensure_open(self):
        if self._closed:
            raise HardwareError("Instrument is closed")

    def _raise_after_stop(self, error):
        try:
            self.zero()
        except Exception as cleanup_error:
            raise HardwareError("%s; shutdown also failed: %s"
                                % (error, cleanup_error))
        raise error

    def set_duty(self, role, fraction):
        """Set a duty fraction in [0, 1]; invalid requests stop both outputs."""
        self._ensure_open()
        try:
            if role not in self._pwms:
                raise ValueError("Unknown output channel: %s" % role)
            fraction = float(fraction)
            # This also rejects NaN and positive/negative infinity.
            if not 0.0 <= fraction <= 1.0:
                raise ValueError("PWM duty must be finite and within 0..1")
        except (ValueError, TypeError) as exc:
            self._raise_after_stop(ValueError(str(exc)))
        try:
            self._ensure_monitoring()
            self._pwms[role].duty_u16(int(fraction * 65535 + 0.5))
        except Exception as exc:
            self._raise_after_stop(HardwareError("%s PWM write failed: %s"
                                                 % (role, exc)))

    def read_pair(self):
        """Return raw voltages/codes for both sensors, or stop both and raise.

        Every 15-bit bus code is returned, including zero and full scale.
        Signed shunt endpoints raise typed overrange after stopping both
        outputs. Reserved bus bits remain hardware errors. No measured-voltage
        envelope or target-accuracy checks are applied here.
        """
        self._ensure_open()
        if self._measurement_pending and self._ready_mask != 3:
            self._raise_after_stop(HardwareError("INA226 measurement is not ready"))
        self.poll_alerts()
        result = {}
        try:
            for role, _, address in CHANNELS:
                raw_shunt = self._read16(address, 1)
                bus_code = self._read16(address, 2)
                if bus_code & 0x8000:
                    raise HardwareError("%s invalid bus ADC code" % role)
                if raw_shunt in (0x7FFF, 0x8000):
                    sign = 1 if raw_shunt == 0x7FFF else -1
                    magnitude = (32767 if sign == 1 else 32768) * SHUNT_LSB_V
                    raise OverrangeError(role, "shunt_saturation", raw_shunt, sign, magnitude, "V")
                shunt_code = raw_shunt - 65536 if raw_shunt & 0x8000 else raw_shunt
                shunt_v = shunt_code * SHUNT_LSB_V
                bus_v = bus_code * BUS_LSB_V
                result[role] = {"bus_v": bus_v, "shunt_v": shunt_v,
                                "terminal_v": bus_v - shunt_v,
                                "bus_code": bus_code, "shunt_code": shunt_code}
        except OverrangeError as exc:
            # Preserve typed range information only when shutdown succeeds.
            # _raise_after_stop promotes a failed shutdown to HardwareError.
            self._raise_after_stop(exc)
        except Exception as exc:
            self._raise_after_stop(HardwareError("INA226 read failed: %s" % exc))
        self._measurement_pending = False
        return result

    def read_codes_into(self, result):
        """Fill four reusable ints: busD, signed-shuntD, busG, signed-shuntG.

        Used by calibration so repeated blocks do not allocate dictionaries
        or floats. It has the same ready/transport/saturation rules as
        read_pair, including gate-first shutdown on a raw shunt endpoint.
        """
        self._ensure_open()
        if len(result) != 4:
            self._raise_after_stop(ValueError("INA226 code buffer needs four slots"))
        if self._measurement_pending and self._ready_mask != 3:
            self._raise_after_stop(HardwareError("INA226 measurement is not ready"))
        self.poll_alerts()
        try:
            for index, (role, _, address) in enumerate(CHANNELS):
                raw_shunt = self._read16(address, 1)
                bus_code = self._read16(address, 2)
                if bus_code & 0x8000:
                    raise HardwareError("%s invalid bus ADC code" % role)
                if raw_shunt in (0x7fff, 0x8000):
                    sign = 1 if raw_shunt == 0x7fff else -1
                    magnitude = (32767 if sign == 1 else 32768) * SHUNT_LSB_V
                    raise OverrangeError(role, "shunt_saturation", raw_shunt, sign, magnitude, "V")
                result[index * 2] = bus_code
                result[index * 2 + 1] = raw_shunt - 65536 if raw_shunt & 0x8000 else raw_shunt
        except OverrangeError as exc:
            self._raise_after_stop(exc)
        except Exception as exc:
            self._raise_after_stop(HardwareError("INA226 read failed: %s" % exc))
        self._measurement_pending = False

    def zero(self):
        """Stop gate then drain, retaining PWM pin mux for later set_duty calls.

        A failed PWM shutdown closes the whole instrument and forces GPIO
        outputs low. This fallback must be reported, because the instrument
        then needs reconstruction before it can drive again.
        """
        self._measurement_pending = False
        self._ready_mask = 0
        if self._closed:
            errors = self._force_pins_low()
        else:
            errors = []
            for role, _, _ in SHUTDOWN_CHANNELS:
                pwm = self._pwms.get(role)
                if pwm is not None:
                    try:
                        pwm.duty_u16(0)
                    except Exception as exc:
                        errors.append("%s PWM zero: %s" % (role, exc))
            if errors:
                try:
                    self.close()
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise HardwareError("Output shutdown failed: " + "; ".join(errors))

    def close(self):
        """Best-effort gate-first stop/deinit, then force GP21/20 low.

        Always attempts every cleanup step, reports failures afterwards,
        and can be called repeatedly. Does not modify the I2C pins.
        """
        errors = []
        for role, _, _ in SHUTDOWN_CHANNELS:
            pwm = self._pwms.get(role)
            if pwm is not None:
                try:
                    pwm.duty_u16(0)
                except Exception as exc:
                    errors.append("%s PWM zero: %s" % (role, exc))
                try:
                    pwm.deinit()
                except Exception as exc:
                    errors.append("%s PWM deinit: %s" % (role, exc))
        self._pwms = {}
        self._closed = True
        errors.extend(self._force_pins_low())
        if errors:
            raise HardwareError("Instrument close failed: " + "; ".join(errors))
