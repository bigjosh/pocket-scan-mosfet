"""Small-buffer ST7796 display driver for the EP-0172 Plus breadboard kit.

Pixels and blit_buffer data use RGB565 in wire order (most significant byte
first). No full-screen framebuffer is allocated. Text uses MicroPython's
built-in 8x8 font; scale=2 gives a readable 16x16 cell on the 480x320 screen.

Manufacturer pinout/example: https://github.com/geeekpi/pico_breadboard_kit/tree/pico2
"""

from machine import Pin, SPI
import framebuf
import time


def rgb565(red, green, blue):
    return ((red & 0xF8) << 8) | ((green & 0xFC) << 3) | (blue >> 3)


class Display:
    # Allocate raster storage contiguously at startup. Retaining independent
    # bytes objects for every glyph fragmented the RP2350 MicroPython heap.
    # A slot holds a scale-2 glyph; scale-1 glyphs use its first 128 bytes.
    RASTER_CACHE_BYTES = 48 * 1024
    RASTER_CACHE_ENTRIES = 96
    RASTER_SLOT_BYTES = 512
    ASCII_FIRST = 32
    ASCII_COUNT = 95

    def __init__(self, width=480, height=320, raster_pool=None):
        if (width, height) not in ((480, 320), (320, 480)):
            raise ValueError("Display size must be 480x320 or 320x480")
        self._raster_capacity = min(self.RASTER_CACHE_ENTRIES,
                                    self.RASTER_CACHE_BYTES // self.RASTER_SLOT_BYTES)
        raster_bytes = self._raster_capacity * self.RASTER_SLOT_BYTES
        if raster_pool is not None and (not isinstance(raster_pool, bytearray) or len(raster_pool) != raster_bytes):
            raise ValueError("Display raster buffer size does not match cache capacity")
        # Production allocates this raw pool before imports fragment the heap;
        # retain its identity and create views, never copy the supplied bytes.
        self._raster_pool = bytearray(raster_bytes) if raster_pool is None else raster_pool
        self.width = width
        self.height = height
        self.cs = Pin(5, Pin.OUT, value=1)
        self.dc = Pin(6, Pin.OUT, value=0)
        self.reset = Pin(7, Pin.OUT, value=1)
        self.spi = SPI(0, baudrate=40_000_000, polarity=0, phase=0,
                       sck=Pin(2), mosi=Pin(3))
        self._solid = bytearray(1024)
        self._solid_view = memoryview(self._solid)
        self._solid_color = None
        self._glyph_data = bytearray(8)
        self._glyph_fb = framebuf.FrameBuffer(self._glyph_data, 8, 8,
                                              framebuf.MONO_HLSB)
        # _plain_text admits only printable ASCII (newlines are handled by
        # text). Fixed indexing avoids a growing glyph dictionary mid-scan.
        self._glyphs = [None] * self.ASCII_COUNT
        pool = memoryview(self._raster_pool)
        self._raster_slots = [pool[index * self.RASTER_SLOT_BYTES:
                                   (index + 1) * self.RASTER_SLOT_BYTES]
                              for index in range(self._raster_capacity)]
        self._raster_small_slots = [slot[:128] for slot in self._raster_slots]
        self._raster_keys = [None] * self._raster_capacity
        self._raster_next = 0
        self._raster_bytes = 0
        # Larger glyphs are uncommon and uncached. Preallocate their scratch
        # buffer and views too, so repeated drawing never retains tile bytes.
        self._text_buffer = bytearray(2048)
        scratch = memoryview(self._text_buffer)
        self._text_views = (None, scratch[:128], scratch[:512],
                            scratch[:1152], scratch)
        self.reset.value(0)
        time.sleep_ms(20)
        self.reset.value(1)
        time.sleep_ms(120)
        self._initialize()
        self.fill(0)

    def _command(self, command, data=None):
        self.cs.value(0)
        try:
            self.dc.value(0)
            self.spi.write(bytes((command,)))
            if data:
                self.dc.value(1)
                self.spi.write(data)
        finally:
            self.cs.value(1)

    def _initialize(self):
        # Power/gamma sequence from the kit's manufacturer example. Explicit
        # 16-bit format and final MADCTL select the wire format/orientation.
        settings = (
            (0xCF, b"\x00\x83\x30"),
            (0xED, b"\x64\x03\x12\x81"),
            (0xE8, b"\x85\x01\x79"),
            (0xCB, b"\x39\x2c\x00\x34\x02"),
            (0xF7, b"\x20"), (0xEA, b"\x00\x00"),
            (0xC0, b"\x26"), (0xC1, b"\x11"),
            (0xC5, b"\x35\x3e"), (0xC7, b"\xbe"),
            (0x3A, b"\x55"), (0xB1, b"\x00\x1b"),
            (0xF2, b"\x08"), (0x26, b"\x01"),
            (0xE0, b"\x1f\x1a\x18\x0a\x0f\x06\x45\x87\x32\x0a\x07\x02\x07\x05\x00"),
            (0xE1, b"\x00\x25\x27\x05\x10\x09\x3a\x78\x4d\x05\x18\x0d\x38\x3a\x1f"),
            (0xB7, b"\x07"), (0xB6, b"\x0a\x82\x27\x00"),
        )
        for command, data in settings:
            self._command(command, data)
        self._command(0x36, b"\x28" if self.width == 480 else b"\x48")
        self._command(0x21)  # Inversion setting used by this panel's example.
        self._command(0x11)
        time.sleep_ms(120)
        self._command(0x29)
        time.sleep_ms(100)

    def _window(self, x, y, width, height):
        right = x + width - 1
        bottom = y + height - 1
        self._command(0x2A, bytes((x >> 8, x & 255, right >> 8, right & 255)))
        self._command(0x2B, bytes((y >> 8, y & 255, bottom >> 8, bottom & 255)))
        self._command(0x2C)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def fill_rect(self, x, y, width, height, color):
        x, y, width, height = int(x), int(y), int(width), int(height)
        right = min(self.width, x + width)
        bottom = min(self.height, y + height)
        x, y = max(0, x), max(0, y)
        width, height = right - x, bottom - y
        if width <= 0 or height <= 0:
            return
        color &= 0xFFFF
        if color != self._solid_color:
            high, low = color >> 8, color & 255
            self._solid[0], self._solid[1] = high, low
            # Double an initialized prefix in place. Memoryview slices need
            # only small descriptors; never allocate another 1 KiB tile just
            # to change color, including when drawing a low-memory error.
            filled = 2
            while filled < len(self._solid):
                count = min(filled, len(self._solid) - filled)
                self._solid_view[filled:filled + count] = self._solid_view[:count]
                filled += count
            self._solid_color = color
        self._window(x, y, width, height)
        remaining = width * height * 2
        self.cs.value(0)
        self.dc.value(1)
        try:
            while remaining:
                count = min(remaining, len(self._solid))
                self.spi.write(self._solid_view[:count])
                remaining -= count
        finally:
            self.cs.value(1)

    def pixel(self, x, y, color):
        self.fill_rect(x, y, 1, 1, color)

    def rect(self, x, y, width, height, color):
        x, y, width, height = int(x), int(y), int(width), int(height)
        if width <= 0 or height <= 0:
            return
        self.fill_rect(x, y, width, 1, color)
        if height > 1:
            self.fill_rect(x, y + height - 1, width, 1, color)
        self.fill_rect(x, y, 1, height, color)
        if width > 1:
            self.fill_rect(x + width - 1, y, 1, height, color)

    def _outcode(self, x, y):
        return ((1 if x < 0 else 2 if x >= self.width else 0) |
                (4 if y < 0 else 8 if y >= self.height else 0))

    def line(self, x0, y0, x1, y1, color):
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        # Clip before Bresenham so an off-screen plot point cannot cause a
        # very long draw loop. Intersections are calculated with integers.
        code0, code1 = self._outcode(x0, y0), self._outcode(x1, y1)
        while code0 or code1:
            if code0 & code1:
                return
            code = code0 or code1
            if code & 8:
                y = self.height - 1
                x = x0 + (x1 - x0) * (y - y0) // (y1 - y0)
            elif code & 4:
                y = 0
                x = x0 + (x1 - x0) * (y - y0) // (y1 - y0)
            elif code & 2:
                x = self.width - 1
                y = y0 + (y1 - y0) * (x - x0) // (x1 - x0)
            else:
                x = 0
                y = y0 + (y1 - y0) * (x - x0) // (x1 - x0)
            if code == code0:
                x0, y0, code0 = x, y, self._outcode(x, y)
            else:
                x1, y1, code1 = x, y, self._outcode(x, y)
        if y0 == y1:
            self.fill_rect(min(x0, x1), y0, abs(x1 - x0) + 1, 1, color)
            return
        if x0 == x1:
            self.fill_rect(x0, min(y0, y1), 1, abs(y1 - y0) + 1, color)
            return
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        horizontal = dx >= -dy
        run_x, run_y = x0, y0
        # Preserve Bresenham's pixels/tie handling, but send each contiguous
        # major-axis run in one window instead of opening a window per pixel.
        # Only scalar run endpoints are retained; clipping above bounds work.
        while True:
            if x0 == x1 and y0 == y1:
                if horizontal:
                    self.fill_rect(min(run_x, x0), run_y, abs(x0 - run_x) + 1, 1, color)
                else:
                    self.fill_rect(run_x, min(run_y, y0), 1, abs(y0 - run_y) + 1, color)
                break
            previous_x, previous_y = x0, y0
            twice = 2 * error
            if twice >= dy:
                error += dy
                x0 += sx
            if twice <= dx:
                error += dx
                y0 += sy
            if horizontal:
                if y0 != run_y:
                    self.fill_rect(min(run_x, previous_x), run_y,
                                   abs(previous_x - run_x) + 1, 1, color)
                    run_x, run_y = x0, y0
            elif x0 != run_x:
                self.fill_rect(run_x, min(run_y, previous_y), 1,
                               abs(previous_y - run_y) + 1, color)
                run_x, run_y = x0, y0

    def blit_buffer(self, buffer, x, y, width, height):
        """Draw packed RGB565 big-endian pixels, clipping at screen edges."""
        x, y, width, height = int(x), int(y), int(width), int(height)
        if width <= 0 or height <= 0:
            return
        if len(buffer) < width * height * 2:
            raise ValueError("RGB565 buffer is too short")
        left, top = max(0, x), max(0, y)
        right, bottom = min(self.width, x + width), min(self.height, y + height)
        if right <= left or bottom <= top:
            return
        self._window(left, top, right - left, bottom - top)
        view = memoryview(buffer)
        self.cs.value(0)
        self.dc.value(1)
        try:
            if left == x and right == x + width:
                start = (top - y) * width * 2
                self.spi.write(view[start:start + (bottom - top) * width * 2])
            else:
                for row in range(top - y, bottom - y):
                    start = (row * width + left - x) * 2
                    self.spi.write(view[start:start + (right - left) * 2])
        finally:
            self.cs.value(1)

    @staticmethod
    def _plain_text(value):
        value = str(value)
        for original, replacement in (("\u00b5", "u"), ("\u03bc", "u"),
                                      ("\u03a9", "ohm"), ("\u2212", "-"),
                                      ("\u00d7", "x"), ("\u00b0", "deg")):
            value = value.replace(original, replacement)
        return "".join(character if character == "\n" or 32 <= ord(character) < 127
                       else "?" for character in value)

    @staticmethod
    def _scale(scale):
        if scale not in (1, 2, 3, 4):
            raise ValueError("Text scale must be an integer from 1 to 4")
        return int(scale)

    def text_width(self, text, scale=1):
        scale = self._scale(scale)
        return max(len(line) for line in self._plain_text(text).split("\n")) * 8 * scale

    def _glyph(self, character):
        index = ord(character) - self.ASCII_FIRST
        if not 0 <= index < self.ASCII_COUNT:
            character = "?"
            index = ord(character) - self.ASCII_FIRST
        rows = self._glyphs[index]
        if rows is None:
            self._glyph_fb.fill(0)
            self._glyph_fb.text(character, 0, 0, 1)
            rows = bytes(sum((1 << x) if self._glyph_fb.pixel(x, y) else 0
                             for x in range(8)) for y in range(8))
            # Publish only the completed bitmap. A failed allocation leaves
            # this fixed slot empty so the next error-rendering attempt retries.
            self._glyphs[index] = rows
        return rows

    def _write_raster(self, target, character, color, bg, scale):
        foreground = bytes((color >> 8, color & 255)) * scale
        background = bytes((bg >> 8, bg & 255)) * scale
        # Expand the 64 font pixels once. Byte repetition performs horizontal
        # and vertical scaling in native code instead of per-output-pixel
        # Python loops. Only one row group is transient, at most 256 bytes.
        offset = 0
        for bits in self._glyph(character):
            row = b"".join(foreground if bits & (1 << column) else background
                           for column in range(8))
            block = row * scale
            target[offset:offset + len(block)] = block
            offset += len(block)

    def _raster(self, character, color, bg, scale):
        # Returned views are borrowed: blit them immediately. FIFO replacement
        # can reuse cached slots, and scales 3/4 share the scratch buffer.
        if scale > 2 or not self._raster_capacity:
            target = self._text_views[scale]
            self._write_raster(target, character, color, bg, scale)
            return target
        # The old dict could need a 776-byte map resize long after startup,
        # despite its entry count being capped. This bounded scan never grows
        # metadata and creates no candidate tuple on a cache hit.
        for index in range(self._raster_capacity):
            cached = self._raster_keys[index]
            if (cached is not None and cached[0] == character and cached[1] == color
                    and cached[2] == bg and cached[3] == scale):
                return self._raster_small_slots[index] if scale == 1 else self._raster_slots[index]
        # Allocate the small replacement key before touching the old slot.
        key = (character, color, bg, scale)
        index = self._raster_next
        previous = self._raster_keys[index]
        if previous is not None:
            self._raster_bytes -= 128 * previous[3] * previous[3]
            # A partially written raster must not remain a valid cache hit.
            # Error rendering may immediately retry this same FIFO slot.
            self._raster_keys[index] = None
        target = self._raster_small_slots[index] if scale == 1 else self._raster_slots[index]
        self._write_raster(target, character, color, bg, scale)
        # Commit into fixed metadata storage only after all pixels succeeded.
        self._raster_keys[index] = key
        self._raster_bytes += 128 * scale * scale
        self._raster_next = (index + 1) % self._raster_capacity
        return target

    def text(self, text, x, y, color, bg=None, scale=1):
        """Draw 8x8 text. bg=None preserves pixels behind each glyph.

        Passing a background color is faster and erases the full glyph cell.
        Supported scales are 1 through 4. Unicode unit symbols are converted
        to ASCII; the built-in MicroPython font does not contain them.
        """
        scale = self._scale(scale)
        x, y = int(x), int(y)
        origin_x = x
        size = 8 * scale
        color &= 0xFFFF
        if bg is not None:
            bg &= 0xFFFF
        for character in self._plain_text(text):
            if character == "\n":
                x, y = origin_x, y + size
                continue
            if x < self.width and y < self.height and x + size > 0 and y + size > 0:
                if bg is None:
                    rows = self._glyph(character)
                    for row, bits in enumerate(rows):
                        column = 0
                        while column < 8:
                            if not bits & (1 << column):
                                column += 1
                                continue
                            start = column
                            while column < 8 and bits & (1 << column):
                                column += 1
                            self.fill_rect(x + start * scale, y + row * scale,
                                           (column - start) * scale, scale, color)
                else:
                    self.blit_buffer(self._raster(character, color, bg, scale),
                                     x, y, size, size)
            x += size
