"""Bounded open-output current tare and source-relative zero-drive capture.

Two boot-reserved buffers hold the committed table and calibration candidate.
Each table has 65 fixed slots/channel: measured bus microvolts, shunt nanovolts
and the PWM code used. 53 quadratic-spaced sweep points leave 12 slots for
the measurements already taken during zero search. Calibration never grows a
point list or writes files.
Only a complete candidate may atomically replace the binary file.
"""

import struct
import os
import hashlib
import binascii

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

ROLES = ("drain", "gate")
MAX_KNOTS = 65
BLOCKS_PER_KNOT = 4
ZERO_STEPS = 12
SWEEP_POINTS = MAX_KNOTS - ZERO_STEPS
ZERO_TOLERANCE_UV = 100000
HEADER_BYTES = 128
RECORD_BYTES = 12
DATA_END = HEADER_BYTES + 2 * MAX_KNOTS * RECORD_BYTES
BUFFER_BYTES = DATA_END + 32
WORKSPACE_BYTES = BUFFER_BYTES
PATH = "current-baseline.bin"
_EMPTY = b""
_SETTINGS = ((12, "ina_average_count"), (16, "settle_ms"),
             (20, "driver_supply_uv"), (24, "source_voltage_uv"))


def _round_div(value, divisor):
    return ((value + divisor // 2) // divisor if value >= 0 else
            -((-value + divisor // 2) // divisor))


def sweep_duty_u16(index):
    """Deterministic denser low-end sweep, with exact zero/full-scale ends."""
    denominator = (SWEEP_POINTS - 1) * (SWEEP_POINTS - 1)
    return (index * index * 65535 + denominator // 2) // denominator


def _hardware_value(offset):
    # Integers deliberately retain shunt precision without float tables.
    if offset == 28:
        return int(model.DRAIN_SHUNT_OHM * 1000000 + 0.5)
    if offset == 32:
        return int(model.GATE_SHUNT_OHM * 1000000 + 0.5)
    if offset == 36:
        return model.PWM_HZ
    if offset == 40:
        return int(model.FEEDBACK_TOP_OHM)
    if offset == 44:
        return int(model.FEEDBACK_BOTTOM_OHM)
    if offset == 48:
        return int(model.DRAIN_PWM_FILTER_OHM)
    if offset == 52:
        return int(model.DRAIN_BIAS_RESISTOR_OHM)
    if offset == 56:
        return int(model.PWM_RAIL_V * 1000000 + 0.5)
    return int(model.DRAIN_BIAS_V * 1000000 + 0.5)


class Baseline:
    def __init__(self, buffer=None):
        if buffer is None:
            buffer = bytearray(BUFFER_BYTES)
        if not isinstance(buffer, bytearray) or len(buffer) != BUFFER_BYTES:
            raise ValueError("Baseline buffer size is invalid")
        self._data = buffer
        self._view = memoryview(buffer)
        self._hash_view = self._view[:DATA_END]
        self.available = False
        self.sha256 = None

    def _get(self, offset):
        return struct.unpack_from("<i", self._data, offset)[0]

    def _put(self, offset, value):
        struct.pack_into("<i", self._data, offset, value)

    def count(self, role):
        index = ROLES.index(role) if isinstance(role, str) else role
        return struct.unpack_from("<H", self._data, 8 + index * 2)[0]

    def _set_count(self, role, value):
        struct.pack_into("<H", self._data, 8 + role * 2, value)

    def _slot(self, role, index):
        return HEADER_BYTES + (role * MAX_KNOTS + index) * RECORD_BYTES

    def bus_uv(self, role, index):
        return self._get(self._slot(role, index))

    def shunt_nv(self, role, index):
        return self._get(self._slot(role, index) + 4)

    def duty_u16(self, role, index):
        return self._get(self._slot(role, index) + 8)

    def terminal_uv(self, role, index):
        return self.bus_uv(role, index) - _round_div(self.shunt_nv(role, index), 1000)

    def begin(self, settings, identity=None):
        """Reset only this candidate; no instrument or filesystem operations."""
        self.available, self.sha256 = False, None
        for index in range(BUFFER_BYTES):
            self._data[index] = 0
        self._data[:4] = b"PSB1"
        struct.pack_into("<HH", self._data, 4, 1, HEADER_BYTES)
        for offset, key in _SETTINGS:
            self._put(offset, settings[key])
        for offset in range(28, 64, 4):
            self._put(offset, _hardware_value(offset))
        for index in range(2):
            item = identity.get(ROLES[index], {}) if identity else {}
            address = item.get("address", 0x44 if index == 0 else 0x41)
            die = item.get("die", 0)
            struct.pack_into("<HH", self._data, 64 + index * 4, address, die)
        self._put(96, BLOCKS_PER_KNOT)

    def add_knot(self, role, bus_uv, shunt_nv, duty_u16):
        """Append increasing measured bus knots, replacing an exact duplicate.

        Output clipping can yield many repeated/decreasing-by-noise readings.
        Those points do not extend the interpolation domain. No requested
        voltage or analog-headroom requirement is imposed on a scan.
        """
        count = self.count(role)
        if count:
            last = self.bus_uv(role, count - 1)
            if bus_uv < last:
                return
            if bus_uv == last:
                count -= 1
        if count >= MAX_KNOTS:
            raise ValueError("Baseline table is full")
        struct.pack_into("<iii", self._data, self._slot(role, count),
                         bus_uv, shunt_nv, duty_u16)
        self._set_count(role, count + 1)

    def set_zero(self, role, duty, residual, valid):
        struct.pack_into("<iii", self._data, 72 + role * 12,
                         duty, residual, 1 if valid else 0)

    def insert_knot(self, role, bus_uv, shunt_nv, duty_u16):
        """Keep an already-measured zero-search sample in sorted fixed storage.

        The bulk sweep reserves ZERO_STEPS slots/channel. Reverse byte copies
        shift records in-place without a sliced table, scratch array or copied
        tuple list. Exact duplicate voltages replace the previous reading.
        """
        count = self.count(role)
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            if self.bus_uv(role, middle) < bus_uv:
                low = middle + 1
            else:
                high = middle
        position = low
        duplicate = position < count and self.bus_uv(role, position) == bus_uv
        if not duplicate:
            if count >= MAX_KNOTS:
                raise ValueError("Baseline table is full")
            start = self._slot(role, position)
            for offset in range((count - position) * RECORD_BYTES - 1, -1, -1):
                self._data[start + offset + RECORD_BYTES] = self._data[start + offset]
            self._set_count(role, count + 1)
        struct.pack_into("<iii", self._data, self._slot(role, position),
                         bus_uv, shunt_nv, duty_u16)

    def _validate(self):
        if (self._data[:4] != b"PSB1" or
                struct.unpack_from("<HH", self._data, 4) != (1, HEADER_BYTES)):
            raise ValueError("Unsupported current baseline file")
        if (self._get(12) not in model.INA_AVERAGE_COUNTS or
                not 100 <= self._get(16) <= 10000 or
                not 2100000 <= self._get(20) <= 30000000 or
                not 0 <= self._get(24) <= self._get(20) or
                self._get(96) != BLOCKS_PER_KNOT):
            raise ValueError("Invalid baseline acquisition settings")
        for offset in range(28, 64, 4):
            if self._get(offset) != _hardware_value(offset):
                raise ValueError("Baseline hardware values changed")
        for index in range(100, HEADER_BYTES):
            if self._data[index]:
                raise ValueError("Invalid baseline reserved data")
        for role in range(2):
            address, die = struct.unpack_from("<HH", self._data, 64 + role * 4)
            if address != (0x44 if role == 0 else 0x41) or die not in (0, 0x2260, 0x2261):
                raise ValueError("Invalid baseline sensor identity")
            count = self.count(role)
            if not 2 <= count <= MAX_KNOTS:
                raise ValueError("Baseline needs two distinct voltages per channel")
            previous = -1
            for index in range(count):
                bus = self.bus_uv(role, index)
                if (not previous < bus <= 40958750 or
                        abs(self.shunt_nv(role, index)) >= 81920000 or
                        not 0 <= self.duty_u16(role, index) <= 65535):
                    raise ValueError("Invalid baseline knot")
                previous = bus
            duty, residual, valid = struct.unpack_from("<iii", self._data, 72 + role * 12)
            if (not 0 <= duty <= 65535 or abs(residual) > 42000000 or
                    valid not in (0, 1) or (valid and abs(residual) > ZERO_TOLERANCE_UV)):
                raise ValueError("Invalid baseline zero capture")

    def finish(self):
        """Validate and seal a complete candidate once, before any file write."""
        self._validate()
        digest = hashlib.sha256(self._hash_view).digest()
        self._view[DATA_END:] = digest
        # Public provenance identifies the exact file, including its checksum.
        self.sha256 = binascii.hexlify(hashlib.sha256(self._view).digest()).decode()
        self.available = True

    def load(self, path=PATH):
        self.available, self.sha256 = False, None
        try:
            stream = open(path, "rb")
        except OSError as error:
            if error.args and error.args[0] == 2:
                return False
            raise
        with stream:
            used = 0
            while used < BUFFER_BYTES:
                amount = stream.readinto(self._view[used:])
                if not amount:
                    raise ValueError("Truncated current baseline file")
                used += amount
            if stream.read(1):
                raise ValueError("Oversized current baseline file")
        digest = hashlib.sha256(self._hash_view).digest()
        if digest != self._data[DATA_END:]:
            raise ValueError("Current baseline checksum failed")
        self._validate()
        self.sha256 = binascii.hexlify(hashlib.sha256(self._view).digest()).decode()
        self.available = True
        return True

    def save(self, path=PATH):
        """Publish only a sealed candidate; old file survives pre-rename errors."""
        if not self.available:
            raise ValueError("Current baseline is incomplete")
        temporary = path + ".tmp"
        try:
            with open(temporary, "wb") as stream:
                written = stream.write(self._data)
                if written != BUFFER_BYTES:
                    raise OSError("Short current baseline write")
                stream.flush()
            if hasattr(os, "sync"):
                os.sync()
            getattr(os, "replace", os.rename)(temporary, path)
            if hasattr(os, "sync"):
                os.sync()
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

    def copy_from(self, other):
        if not isinstance(other, Baseline) or not other.available:
            raise ValueError("Current baseline is incomplete")
        self._view[:] = other._view
        self.sha256, self.available = other.sha256, True

    def compatible(self, settings, identity=None):
        if not self.available:
            return False
        for offset, key in _SETTINGS:
            if self._get(offset) != settings.get(key):
                return False
        if identity is not None:
            for index in range(2):
                item = identity.get(ROLES[index], {})
                address, die = struct.unpack_from("<HH", self._data, 64 + index * 4)
                if item.get("address") != address or (die and item.get("die") != die):
                    return False
        return True

    def offset_nv(self, role, bus_uv):
        """Binary-search linear interpolation, holding nearest endpoint outside.

        Values are signed nanovolts. Intermediate integer multiplication can
        create a small scalar bigint, never a buffer/list proportional to scan
        length. No dictionaries, slices, tuples of knots or copied arrays.
        """
        if not self.available:
            return 0
        role = ROLES.index(role) if isinstance(role, str) else role
        count = self.count(role)
        if bus_uv <= self.bus_uv(role, 0):
            return self.shunt_nv(role, 0)
        if bus_uv >= self.bus_uv(role, count - 1):
            return self.shunt_nv(role, count - 1)
        low, high = 0, count - 1
        while high - low > 1:
            middle = (low + high) // 2
            if self.bus_uv(role, middle) <= bus_uv:
                low = middle
            else:
                high = middle
        x0, x1 = self.bus_uv(role, low), self.bus_uv(role, high)
        y0, y1 = self.shunt_nv(role, low), self.shunt_nv(role, high)
        return y0 + _round_div((bus_uv - x0) * (y1 - y0), x1 - x0)

    def drive_duty(self, role, terminal_v, settings):
        index = ROLES.index(role)
        if not self.compatible(settings) or not self._get(80 + index * 12):
            return model.drive_duty(role, terminal_v)
        slope = (model.DRAIN_PWM_FULL_SCALE_V if index == 0 else
                 model.GATE_PWM_FULL_SCALE_V)
        target = model._number(terminal_v, "Terminal target")
        duty = self._get(72 + index * 12) / 65535 + (
            target - model.volts_from_uv(self._get(24))) / slope
        return max(0.0, min(1.0, duty))

    def metadata(self, settings):
        if not self.compatible(settings):
            return None
        result = {"version": 1, "sha256": self.sha256,
                  "lookup": "linear_endpoint_hold"}
        for index in range(2):
            result[ROLES[index]] = {
                "min_bus_uv": self.bus_uv(index, 0),
                "max_bus_uv": self.bus_uv(index, self.count(index) - 1),
                "zero_duty_u16": self._get(72 + index * 12),
                "zero_residual_uv": self._get(76 + index * 12),
                "zero_valid": bool(self._get(80 + index * 12))}
        return result


class Calibrator:
    """One open-output sweep, bounded zero search and four blocks per point.

    Caller supplies the boot-reserved workspace. Construction/start do no I/O;
    tick advances at most one read and never sleeps, saves or builds a history.
    The public result becomes available only after a successful output stop.
    """
    def __init__(self, instrument, settings, buffer=None, clock_ms=None):
        self.instrument = instrument
        self.settings = model.validate_settings(settings)
        self._candidate = Baseline(buffer)
        self._candidate.begin(self.settings, getattr(instrument, "identity", None))
        self._codes = [0, 0, 0, 0]
        self._read_into = getattr(instrument, "read_codes_into", None)
        self._clock_ms = _ticks_ms if clock_ms is None else clock_ms
        self.state, self.phase, self.channel = "idle", "drive", "drain"
        self.error, self.result = None, None
        self.progress = 0.0
        self.shutdown_succeeded = False
        self._role = self._point = self._blocks = 0
        self._bus_sum = self._shunt_sum = 0
        self._duty = 0
        self._last_clock = None
        self._clock = self._deadline = self._status_deadline = 0
        self._measurement_deadline = 0
        self._zero_search = False
        self._zero_step = 0
        self._low_duty = self._high_duty = self._best_duty = 0
        self._best_residual = 72000000
        self._zero_bracket = False
        self._completed = 0
        self._total = 2 * (SWEEP_POINTS + ZERO_STEPS)

    def start(self):
        if self.state != "idle":
            raise ValueError("Calibration already started")
        self.state = "running"

    def _stop(self, state):
        self.shutdown_succeeded = False
        self.instrument.zero()
        self.shutdown_succeeded = True
        self.state = state

    def cancel(self):
        if self.state != "running":
            return
        try:
            self._stop("cancelled")
        except Exception as error:
            self.error, self.state = "Calibration could not stop outputs: " + str(error), "error"

    def _advance_clock(self, now_ms):
        if self._last_clock is not None:
            delta = _ticks_diff(now_ms, self._last_clock)
            if delta < 0:
                raise ValueError("Calibration clock moved backwards")
            self._clock += delta
        self._last_clock = now_ms

    def _read(self):
        if self._read_into is not None:
            self._read_into(self._codes)
            return
        # Portable fake-instrument compatibility; production always uses the
        # allocation-bounded integer read_codes_into method.
        pair = self.instrument.read_pair()
        for index in range(2):
            item = pair[ROLES[index]]
            self._codes[index * 2] = item.get("bus_code", int(item["bus_v"] / 0.00125 + 0.5))
            value = item["shunt_v"] / 0.0000025
            self._codes[index * 2 + 1] = item.get("shunt_code", int(value + (0.5 if value >= 0 else -0.5)))

    def tick(self, now_ms):
        if self.state != "running":
            return
        try:
            self._advance_clock(now_ms)
            self._tick()
        except Exception as error:
            self.error = str(error)
            self.result = None
            try:
                self._stop("error")
            except Exception as stop_error:
                self.state = "error"
                self.error += "; could not stop outputs: " + str(stop_error)

    def _tick(self):
        if self.phase == "drive":
            if self._blocks == 0 and self._point == 0 and not self._zero_search:
                self.instrument.zero()
            # Re-enter monitoring + settle before EVERY block, matching the
            # normal scanner's continuous/triggered mode history.
            self.instrument.set_duty(self.channel, self._duty / 65535)
            if self._clock_ms is not None:
                self._advance_clock(self._clock_ms())
            self._deadline = self._clock + self.settings["settle_ms"]
            self.phase = "settle"
            return
        if self._clock < self._status_deadline:
            return
        self._status_deadline = self._clock + 5
        if self.phase == "settle":
            self.instrument.poll_alerts()
            if self._clock < self._deadline:
                return
            self._read()  # Same continuous endpoint check as a normal scan.
            count = self.settings["ina_average_count"]
            self.instrument.begin_measurement(count)
            self._measurement_deadline = self._clock + (count * 2420 + 999) // 1000 + 50
            self.phase = "convert"
            return
        if not self.instrument.poll_measurement():
            if self._clock >= self._measurement_deadline:
                raise ValueError("Calibration INA226 conversion timed out")
            return
        self._read()
        self._bus_sum += self._codes[self._role * 2]
        self._shunt_sum += self._codes[self._role * 2 + 1]
        self._blocks += 1
        if self._blocks < BLOCKS_PER_KNOT:
            self.phase = "drive"
            return
        bus_uv = _round_div(self._bus_sum * 1250, BLOCKS_PER_KNOT)
        shunt_nv = _round_div(self._shunt_sum * 2500, BLOCKS_PER_KNOT)
        terminal_uv = bus_uv - _round_div(shunt_nv, 1000)
        self._blocks = self._bus_sum = self._shunt_sum = 0
        self._completed += 1
        self.progress = min(0.99, self._completed / self._total)
        if self._zero_search:
            self._candidate.insert_knot(self._role, bus_uv, shunt_nv, self._duty)
            self._zero_read(terminal_uv)
        else:
            self._candidate.add_knot(self._role, bus_uv, shunt_nv, self._duty)
            self._point += 1
            if self._point == SWEEP_POINTS:
                self._start_zero()
            else:
                self._duty = sweep_duty_u16(self._point)
                self.phase = "drive"

    def _start_zero(self):
        table, role = self._candidate, self._role
        count = table.count(role)
        if count < 2:
            raise ValueError(self.channel + " baseline has no voltage span")
        source = self.settings["source_voltage_uv"]
        self._best_residual, self._best_duty = 72000000, 0
        self._zero_bracket = False
        previous = table.terminal_uv(role, 0)
        for index in range(count):
            terminal = table.terminal_uv(role, index)
            residual = terminal - source
            if abs(residual) < abs(self._best_residual):
                self._best_residual = residual
                self._best_duty = table.duty_u16(role, index)
            if terminal == source:
                self._zero_bracket = True
                self._low_duty = self._high_duty = table.duty_u16(role, index)
                break
            if index and previous <= source <= terminal:
                self._zero_bracket = True
                self._low_duty, self._high_duty = (table.duty_u16(role, index - 1),
                                                    table.duty_u16(role, index))
            previous = terminal
        self._zero_step = 0
        if not self._zero_bracket or self._low_duty == self._high_duty:
            self._finish_zero()
            return
        self._zero_search = True
        self._duty = (self._low_duty + self._high_duty) // 2
        self.phase = "drive"

    def _zero_read(self, terminal_uv):
        residual = terminal_uv - self.settings["source_voltage_uv"]
        if abs(residual) < abs(self._best_residual):
            self._best_residual, self._best_duty = residual, self._duty
        if residual < 0:
            self._low_duty = self._duty
        else:
            self._high_duty = self._duty
        self._zero_step += 1
        if (self._zero_step >= ZERO_STEPS or self._high_duty - self._low_duty <= 1 or
                residual == 0):
            self._finish_zero()
            return
        self._duty = (self._low_duty + self._high_duty) // 2
        self.phase = "drive"

    def _finish_zero(self):
        self._candidate.set_zero(self._role, self._best_duty, self._best_residual,
                                 self._zero_bracket and abs(self._best_residual) <= ZERO_TOLERANCE_UV)
        if self._role == 0:
            self._completed = SWEEP_POINTS + ZERO_STEPS
            self._role, self._point, self._duty = 1, 0, 0
            self.channel = "gate"
            self._zero_search = False
            self.phase = "drive"
            return
        self._stop("complete")
        self._candidate.finish()
        self.result = self._candidate
        self.progress = 1.0
        self.phase = "complete"
