"""Pico 2 W drive, INA226 drain and INA228 gate voltage readback.

No output is enabled by construction. VBUS on each sensor is wired to IN+
(the amplifier side of its shunt), so terminal_v subtracts the RAW shunt
drop. This layer always returns raw readings; Engine may separately subtract
an open-output baseline for current only.

PWM changes enable continuous, unaveraged monitoring during settling. After
settling, begin_measurement selects one triggered hardware-averaged block;
poll_measurement waits cooperatively for both sensors. Drain conversions take
1.1 ms, gate conversions 1.052 ms. Comparator flags are not software trip
conditions; ALERT pins are unused. A returned signed
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
INA228_CONFIG = 0x0000  # ADCRANGE=0: +/-163.84 mV; no delay/temp compensation.
# MODE=B/3: continuous/triggered bus+shunt, no temperature conversion.
# VBUSCT=VSHCT=5: 1052 us. AVG is bits 2:0, unlike INA226 bits 11:9.
INA228_ADC_CONFIG = 0xBB40
INA228_TRIGGER_CONFIG = 0x3B40
INA228_ALERT_MASK = 0x8000  # ALATCH: make conversion-ready read-to-clear.
INA228_ALERT_CONTROL_MASK = 0xF000
INA228_READY_FLAG = 0x0002
CHANNELS = (("drain", 20, 0x44), ("gate", 21, 0x40))
# Keep sensor reads in drain/gate order, but remove NMOS gate drive first
# whenever stopping outputs, including a driver fault before Engine sees it.
SHUTDOWN_CHANNELS = (CHANNELS[1], CHANNELS[0])
SHUNT_LSB_V = 0.0000025
BUS_LSB_V = 0.00125
INA228_SHUNT_LSB_V = 0.0000003125
INA228_BUS_LSB_V = 0.0001953125


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
        self._i2c_bytes24 = bytearray(3)
        self._codes = [0, 0, 0, 0]
        try:
            # Clear any outputs left by an interrupted REPL experiment before
            # initializing either PWM peripheral or touching I2C.
            errors = self._force_pins_low()
            if errors:
                raise HardwareError("Initial output shutdown: " + "; ".join(errors))
            for role, gpio, _ in CHANNELS:
                self._pwms[role] = PWM(Pin(gpio), freq=PWM_HZ, duty_u16=0)
            self._bus = I2C(1, sda=Pin(18), scl=Pin(19), freq=100000)
            for index, (role, _, address) in enumerate(CHANNELS):
                manufacturer = self._read16(address, 0xFE if index == 0 else 0x3E)
                die = self._read16(address, 0xFF if index == 0 else 0x3F)
                identity_ok = die in (0x2260, 0x2261) if index == 0 else die >> 4 == 0x228
                if manufacturer != 0x5449 or not identity_ok:
                    raise HardwareError(
                        "%s sensor identity at 0x%02X: manufacturer=%04X die=%04X"
                        % (role, address, manufacturer, die))
                if index == 0:
                    config = INA_CONFIG
                    self._write16(address, 0, config)
                    if self._read16(address, 0) != config:
                        raise HardwareError("Drain INA226 configuration readback failed")
                    self._write16(address, 6, ALERT_MASK)
                    if self._read16(address, 6) & ALERT_CONTROL_MASK != ALERT_MASK:
                        raise HardwareError("Drain INA226 alert mask readback failed")
                else:
                    # Soft reboot preserves the monitor's registers. Explicitly
                    # restore the range and timing before accepting any codes.
                    self._write16(address, 0, INA228_CONFIG)
                    if self._read16(address, 0) != INA228_CONFIG:
                        raise HardwareError("Gate INA228 range configuration readback failed")
                    config = INA228_ADC_CONFIG
                    self._write16(address, 1, config)
                    if self._read16(address, 1) != config:
                        raise HardwareError("Gate INA228 ADC configuration readback failed")
                    self._write16(address, 0x0B, INA228_ALERT_MASK)
                    status = self._read16(address, 0x0B)
                    self._check_gate_status(status)
                self.identity[role] = {"address": address,
                                       "manufacturer": manufacturer,
                                       "die": die, "config": config}
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
                raise HardwareError("Short sensor read at 0x%02X register 0x%02X"
                                    % (address, register))
            return (value[0] << 8) | value[1]
        finally:
            if started is not None:
                timing.add(scan_timing.I2C, started)

    def _read24(self, address, register):
        timing = self._timing
        started = timing.stamp() if timing is not None else None
        try:
            if hasattr(self._bus, "readfrom_mem_into"):
                value = self._i2c_bytes24
                self._bus.readfrom_mem_into(address, register, value)
            else:
                value = self._bus.readfrom_mem(address, register, 3)
            if len(value) != 3:
                raise HardwareError("Short INA228 read at register 0x%02X" % register)
            return (value[0] << 16) | (value[1] << 8) | value[2]
        finally:
            if started is not None:
                timing.add(scan_timing.I2C, started)

    @staticmethod
    def _check_gate_status(status):
        if status & INA228_ALERT_CONTROL_MASK != INA228_ALERT_MASK:
            raise HardwareError("Gate INA228 alert configuration changed")
        if not status & 1:
            raise HardwareError("Gate INA228 trim memory checksum failed")

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
        """Verify status configuration and latch each conversion-ready flag.

        Threshold/math/accumulator flags do not imply raw shunt ADC saturation.
        INA228 MEMSTAT is checked because invalid factory trim is a read fault.
        """
        self._ensure_open()
        try:
            for index, (role, _, address) in enumerate(CHANNELS):
                status = self._read16(address, 6 if index == 0 else 0x0B)
                if index == 0:
                    if status & ALERT_CONTROL_MASK != ALERT_MASK:
                        raise HardwareError("Drain INA226 alert configuration changed")
                    ready = status & READY_FLAG
                else:
                    self._check_gate_status(status)
                    ready = status & INA228_READY_FLAG
                if self._measurement_pending and ready:
                    self._ready_mask |= 1 << index
        except Exception as exc:
            self._raise_after_stop(HardwareError("Sensor status read failed: %s" % exc))

    def _ensure_monitoring(self):
        self.poll_alerts()
        if self._monitoring:
            return
        try:
            for index, (_, _, address) in enumerate(CHANNELS):
                self._write16(address, 0 if index == 0 else 1,
                              INA_CONFIG if index == 0 else INA228_ADC_CONFIG)
            self._monitoring = True
            self._measurement_pending = False
            self._ready_mask = 0
        except Exception as exc:
            self._raise_after_stop(HardwareError("Sensor monitoring failed: %s" % exc))

    def begin_measurement(self, average_count):
        """Trigger both chips after settling; no sleeps and no stale ready flag."""
        self._ensure_open()
        if (isinstance(average_count, bool) or not isinstance(average_count, int)
                or average_count not in model.INA_AVERAGE_COUNTS):
            self._raise_after_stop(ValueError("Invalid sensor averaging count"))
        if self._measurement_pending:
            self._raise_after_stop(HardwareError("Sensor measurement already pending"))
        # Verify configuration, then discard ready bits from the previous
        # continuous frame before triggering a new averaged block.
        self.poll_alerts()
        average_index = model.INA_AVERAGE_COUNTS.index(average_count)
        self._ready_mask = 0
        try:
            # A changed INA228 range would silently change the shunt scale by 4.
            if self._read16(CHANNELS[1][2], 0) != INA228_CONFIG:
                raise HardwareError("Gate INA228 range configuration changed")
            for index, (_, _, address) in enumerate(CHANNELS):
                register = 0 if index == 0 else 1
                config = (INA_TRIGGER_CONFIG | (average_index << 9) if index == 0
                          else INA228_TRIGGER_CONFIG | average_index)
                self._write16(address, register, config)
                if self._read16(address, register) != config:
                    raise HardwareError("Sensor triggered configuration readback failed")
            self._measurement_pending = True
            self._monitoring = False
        except Exception as exc:
            self._raise_after_stop(HardwareError("Sensor trigger failed: %s" % exc))
        if self._timing is not None and self._timing.active:
            self._timing.count(scan_timing.ADC_BLOCKS)

    def poll_measurement(self):
        self._ensure_open()
        if not self._measurement_pending:
            self._raise_after_stop(HardwareError("No sensor measurement pending"))
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
        """Return raw voltages/native codes, with upstream shunt drop removed.

        A missed target or bus full-scale code is still a measured point. Only
        shunt endpoints are typed overrange; malformed ADC data is a read fault.
        """
        self.read_codes_into(self._codes)
        try:
            result = {}
            for index, (role, _, _) in enumerate(CHANNELS):
                bus_code, shunt_code = self._codes[index * 2], self._codes[index * 2 + 1]
                shunt_v = shunt_code * (SHUNT_LSB_V if index == 0 else INA228_SHUNT_LSB_V)
                bus_v = bus_code * (BUS_LSB_V if index == 0 else INA228_BUS_LSB_V)
                result[role] = {"bus_v": bus_v, "shunt_v": shunt_v,
                                "terminal_v": bus_v - shunt_v,
                                "bus_code": bus_code, "shunt_code": shunt_code}
        except Exception as exc:
            self._raise_after_stop(HardwareError("Sensor result failed: %s" % exc))
        return result

    def read_codes_into(self, result):
        """Fill busD, signed-shuntD, busG, signed-shuntG without new containers.

        Drain codes use INA226 scales; gate codes are native INA228 20-bit
        values after removing the four reserved low bits. Both readers use
        fixed I2C buffers and gate-first shutdown on a raw shunt endpoint.
        """
        self._ensure_open()
        if len(result) != 4:
            self._raise_after_stop(ValueError("Sensor code buffer needs four slots"))
        if self._measurement_pending and self._ready_mask != 3:
            self._raise_after_stop(HardwareError("Sensor measurement is not ready"))
        self.poll_alerts()
        try:
            if self._read16(CHANNELS[1][2], 0) != INA228_CONFIG:
                raise HardwareError("Gate INA228 range configuration changed")
            for index, (role, _, address) in enumerate(CHANNELS):
                if index == 0:
                    raw_shunt = self._read16(address, 1)
                    bus_code = self._read16(address, 2)
                    if bus_code & 0x8000:
                        raise HardwareError("Drain invalid bus ADC code")
                    sign_bit, modulus, lsb = 0x8000, 0x10000, SHUNT_LSB_V
                else:
                    shunt_word = self._read24(address, 4)
                    bus_word = self._read24(address, 5)
                    if (shunt_word | bus_word) & 0xF:
                        raise HardwareError("Gate invalid ADC reserved bits")
                    # VBUS is signed 20-bit but valid readings are nonnegative.
                    if bus_word & 0x800000:
                        raise HardwareError("Gate invalid bus ADC code")
                    raw_shunt, bus_code = shunt_word >> 4, bus_word >> 4
                    sign_bit, modulus, lsb = 0x80000, 0x100000, INA228_SHUNT_LSB_V
                if raw_shunt == sign_bit - 1 or raw_shunt == sign_bit:
                    sign = 1 if raw_shunt == sign_bit - 1 else -1
                    magnitude = (sign_bit - 1 if sign == 1 else sign_bit) * lsb
                    raise OverrangeError(role, "shunt_saturation", raw_shunt, sign, magnitude, "V")
                result[index * 2] = bus_code
                result[index * 2 + 1] = raw_shunt - modulus if raw_shunt & sign_bit else raw_shunt
        except OverrangeError as exc:
            self._raise_after_stop(exc)
        except Exception as exc:
            self._raise_after_stop(HardwareError("Sensor read failed: %s" % exc))
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
