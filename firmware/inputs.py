"""Debounced buttons and polled GT911 touch for the EP-0172 Plus kit.

poll(now_ms) returns press-edge tuples: ('left',), ('right',), ('touch', x, y).
The manufacturer's EP-0172 pinout names GP14 BTN2 and GP15 BTN1:
https://wiki.52pi.com/index.php?title=EP-0172
The assembled prototype's physical buttons are left BTN1/GP15 and right
BTN2/GP14, as confirmed by the user. Touch
configuration is read, never rewritten. Native portrait touch coordinates
are rotated clockwise to match the display's 480x320 landscape orientation.
"""

from machine import I2C, Pin
import time


class Inputs:
    DEBOUNCE_MS = 30
    TOUCH_POLL_MS = 15
    MAX_TOUCH_ERRORS = 3

    def __init__(self, width=480, height=320):
        self.width, self.height = width, height
        now = time.ticks_ms()
        self._buttons = []
        for name, number in (("left", 15), ("right", 14)):
            pin = Pin(number, Pin.IN, Pin.PULL_UP)
            state = pin.value()
            # A button held during boot must be released before it can act.
            self._buttons.append([name, pin, state, state, now])
        self.status = {
            "touch_available": False,
            "touch_address": None,
            "touch_product": None,
            "native_size": None,
            "touch_error": None,
            "touch_error_count": 0,
            "button_mapping": {"left": 15, "right": 14},
            "orientation": None,
        }
        self._touch_down = False
        self._last_touch_poll = None
        self._consecutive_errors = 0
        self._i2c = None
        self._address = None
        try:
            self._initialize_touch()
        except (OSError, ValueError, RuntimeError) as error:
            self.status["touch_error"] = str(error)

    def _read(self, register, count):
        return self._i2c.readfrom_mem(self._address, register, count, addrsize=16)

    def _initialize_touch(self):
        # INT low while reset rises selects GT911 I2C address 0x5D. Return
        # INT to an input afterward; this driver polls rather than using IRQs.
        reset = Pin(10, Pin.OUT, value=0)
        interrupt = Pin(11, Pin.OUT, value=0)
        time.sleep_ms(10)
        reset.value(1)
        time.sleep_ms(50)
        interrupt.init(Pin.IN)
        time.sleep_ms(50)
        self._touch_reset = reset
        self._touch_interrupt = interrupt
        self._i2c = I2C(0, sda=Pin(8, Pin.IN, Pin.PULL_UP),
                        scl=Pin(9, Pin.IN, Pin.PULL_UP), freq=100_000)
        errors = []
        for address in (0x5D, 0x14):
            self._address = address
            try:
                product = self._read(0x8140, 4)
                if product[:3] != b"911":
                    raise ValueError("Unexpected touch product ID: %r" % product)
                dimensions = self._read(0x8146, 4)
                native_width = dimensions[0] | dimensions[1] << 8
                native_height = dimensions[2] | dimensions[3] << 8
                if not (64 <= native_width <= 4096 and 64 <= native_height <= 4096):
                    raise ValueError("Invalid GT911 coordinate dimensions")
                self._native_width, self._native_height = native_width, native_height
                self.status["touch_available"] = True
                self.status["touch_address"] = address
                self.status["touch_product"] = product.decode().rstrip("\x00")
                self.status["native_size"] = (native_width, native_height)
                portrait_to_landscape = self.width > self.height and native_width < native_height
                landscape_to_portrait = self.width < self.height and native_width > native_height
                self.status["orientation"] = ("clockwise" if portrait_to_landscape else
                                               "counterclockwise" if landscape_to_portrait else
                                               "native")
                return
            except (OSError, ValueError, RuntimeError) as error:
                errors.append("0x%02x: %s" % (address, error))
        self._address = None
        raise OSError("GT911 unavailable; " + "; ".join(errors))

    def _coordinates(self, raw_x, raw_y):
        width, height = self._native_width, self._native_height
        raw_x = min(width - 1, max(0, raw_x))
        raw_y = min(height - 1, max(0, raw_y))
        if self.status["orientation"] == "clockwise":
            raw_x, raw_y = raw_y, width - 1 - raw_x
            width, height = height, width
        elif self.status["orientation"] == "counterclockwise":
            raw_x, raw_y = height - 1 - raw_y, raw_x
            width, height = height, width
        return (raw_x * (self.width - 1) // (width - 1),
                raw_y * (self.height - 1) // (height - 1))

    def poll(self, now_ms):
        events = []
        # Button processing is independent of touch detection/health.
        for button in self._buttons:
            name, pin, stable, candidate, changed = button
            state = pin.value()
            if state != candidate:
                button[3], button[4] = state, now_ms
            elif state != stable and time.ticks_diff(now_ms, changed) >= self.DEBOUNCE_MS:
                button[2] = state
                if state == 0:
                    events.append((name,))
        if not self.status["touch_available"]:
            return events
        if (self._last_touch_poll is not None and
                time.ticks_diff(now_ms, self._last_touch_poll) < self.TOUCH_POLL_MS):
            return events
        self._last_touch_poll = now_ms
        try:
            status = self._read(0x814E, 1)[0]
            if status & 0x80:
                count = status & 0x0F
                point = self._read(0x814F, 8) if count == 1 else None
                # Acknowledge only after the coordinate block has been read.
                self._i2c.writeto_mem(self._address, 0x814E, b"\x00", addrsize=16)
                if count == 0:
                    self._touch_down = False
                elif count == 1:
                    x, y = self._coordinates(point[1] | point[2] << 8,
                                             point[3] | point[4] << 8)
                    if not self._touch_down:
                        events.append(("touch", x, y))
                    self._touch_down = True
                else:
                    # Ignore multitouch until every finger is lifted.
                    self._touch_down = True
            self._consecutive_errors = 0
            self.status["touch_error"] = None
        except (OSError, ValueError, RuntimeError) as error:
            self._consecutive_errors += 1
            self.status["touch_error_count"] += 1
            self.status["touch_error"] = str(error)
            if self._consecutive_errors >= self.MAX_TOUCH_ERRORS:
                # Avoid an endless stream of failing bus operations. Buttons
                # remain usable; constructing Inputs again retries detection.
                self.status["touch_available"] = False
        return events
