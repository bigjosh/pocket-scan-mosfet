"""Host checks for UI driver wire format, clipping and input edge handling."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class FakePin:
    IN, OUT, PULL_UP = 0, 1, 2
    states = {}

    def __init__(self, number, mode=None, pull=None, value=None):
        self.number = number
        self.init(mode, pull, value)

    def init(self, mode=None, pull=None, value=None):
        if value is not None:
            self.states[self.number] = value
        elif self.number not in self.states:
            self.states[self.number] = 1

    def value(self, value=None):
        if value is not None:
            self.states[self.number] = value
        return self.states[self.number]


class FakeSPI:
    def __init__(self, *args, **kwargs):
        self.writes = []

    def write(self, data):
        self.writes.append((FakePin.states[6], bytes(data)))


class FakeFrameBuffer:
    def __init__(self, *args):
        self.character = " "

    def fill(self, color):
        self.character = " "

    def text(self, character, *args):
        self.character = character

    def pixel(self, x, y):
        # A known foreground pixel lets the test distinguish RGB565 byte
        # order, scaling, foreground/background and clipping.
        return int(self.character != " " and x == 0 and y == 0)


class FakeI2C:
    available = True
    instances = []

    def __init__(self, *args, **kwargs):
        self.available = type(self).available
        self.operations = []
        self.status = 0
        self.point = bytes(8)
        self.fail_reads = False
        self.instances.append(self)

    def readfrom_mem(self, address, register, count, addrsize=8):
        self.operations.append(("read", register, count, addrsize))
        if not self.available or self.fail_reads or address != 0x5D:
            raise OSError("missing touch")
        data = {
            0x8140: b"911\x00",
            0x8146: b"\x40\x01\xe0\x01",  # Native 320 x 480.
            0x814E: bytes((self.status,)),
            0x814F: self.point,
        }
        return data[register]

    def writeto_mem(self, address, register, data, addrsize=8):
        self.operations.append(("write", register, bytes(data), addrsize))
        self.status = 0

    def touch(self, x=0, y=0, count=1):
        self.status = 0x80 | count
        self.point = bytes((0, x & 255, x >> 8, y & 255, y >> 8, 0, 0, 0))


def load_driver(name):
    machine = types.SimpleNamespace(Pin=FakePin, SPI=FakeSPI, I2C=FakeI2C)
    framebuf = types.SimpleNamespace(FrameBuffer=FakeFrameBuffer, MONO_HLSB=0)
    clock = types.SimpleNamespace(sleep_ms=lambda ms: None,
                                  ticks_ms=lambda: 0,
                                  ticks_diff=lambda a, b: ((a - b + (1 << 29)) % (1 << 30)) - (1 << 29))
    specification = importlib.util.spec_from_file_location(
        "tested_ui_" + name, ROOT / "firmware" / (name + ".py"))
    module = importlib.util.module_from_spec(specification)
    with patch.dict(sys.modules, machine=machine, framebuf=framebuf, time=clock):
        specification.loader.exec_module(module)
    return module


def reference_line_pixels(x0, y0, x1, y1, width, height):
    """Prior clipped, per-pixel Bresenham behavior, including reversed ties."""
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    def outcode(x, y):
        return ((1 if x < 0 else 2 if x >= width else 0) |
                (4 if y < 0 else 8 if y >= height else 0))
    a, b = outcode(x0, y0), outcode(x1, y1)
    while a or b:
        if a & b:
            return set()
        code = a or b
        if code & 8:
            y = height - 1
            x = x0 + (x1 - x0) * (y - y0) // (y1 - y0)
        elif code & 4:
            y = 0
            x = x0 + (x1 - x0) * (y - y0) // (y1 - y0)
        elif code & 2:
            x = width - 1
            y = y0 + (y1 - y0) * (x - x0) // (x1 - x0)
        else:
            x = 0
            y = y0 + (y1 - y0) * (x - x0) // (x1 - x0)
        if code == a:
            x0, y0, a = x, y, outcode(x, y)
        else:
            x1, y1, b = x, y, outcode(x, y)
    result = set()
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    error = dx + dy
    while True:
        result.add((x0, y0))
        if x0 == x1 and y0 == y1:
            return result
        twice = 2 * error
        if twice >= dy:
            error += dy
            x0 += sx
        if twice <= dx:
            error += dx
            y0 += sy


class DisplayTests(unittest.TestCase):
    def setUp(self):
        FakePin.states = {}
        self.module = load_driver("display")
        self.display = self.module.Display()
        self.display.spi.writes.clear()

    def test_fill_clips_window_and_sends_big_endian(self):
        self.display.fill_rect(-1, -1, 3, 3, 0xF81F)
        self.assertEqual(self.display.spi.writes, [
            (0, b"\x2a"), (1, b"\x00\x00\x00\x01"),
            (0, b"\x2b"), (1, b"\x00\x00\x00\x01"),
            (0, b"\x2c"), (1, b"\xf8\x1f" * 4)])

    def test_color_changes_reuse_solid_tile_without_large_bytes_expansion(self):
        class SmallBytes(bytes):
            def __mul__(self, count):
                if len(self) * count > 64:
                    raise MemoryError("large temporary color tile")
                return super().__mul__(count)

        tile, view = self.display._solid, self.display._solid_view
        # The old bytes((high, low)) * 512 expansion fails under this guard;
        # small command/address buffers remain available to the wire driver.
        with patch.object(self.module, "bytes", SmallBytes, create=True):
            for color, pixel in ((0xF81F, b"\xf8\x1f"),
                                 (0x1234, b"\x12\x34"),
                                 (0x0000, b"\x00\x00")):
                self.display.fill_rect(0, 0, 480, 2, color)
                self.assertEqual(tile, pixel * 512)
                self.assertEqual(self.display.spi.writes[-1][1], pixel * 448)
                self.assertIs(self.display._solid, tile)
                self.assertIs(self.display._solid_view, view)

    def test_blit_clipping_preserves_source_row_stride(self):
        self.display.blit_buffer(bytes(range(12)), -1, 0, 3, 2)
        self.assertEqual(self.display.spi.writes[-2:],
                         [(1, bytes(range(2, 6))), (1, bytes(range(8, 12)))])
        with self.assertRaises(ValueError):
            self.display.blit_buffer(b"\x00", 0, 0, 1, 1)

    def test_scaled_text_big_endian_and_transparent_background(self):
        self.display.text("A", 0, 0, 0xF800, bg=0x001F, scale=2)
        data = self.display.spi.writes[-1][1]
        self.assertEqual(len(data), 16 * 16 * 2)
        self.assertEqual(data[:6], b"\xf8\x00\xf8\x00\x00\x1f")
        self.assertEqual(data[32:38], data[:6])
        self.display.spi.writes.clear()
        self.display.text("A", 0, 0, 0x07E0, scale=2)
        self.assertEqual(self.display.spi.writes[-1], (1, b"\x07\xe0" * 4))
        self.assertEqual(self.display.text_width("1\u00b5A", 2), 48)

    def test_offscreen_line_clips_before_drawing(self):
        self.display.line(-1_000_000, 5, 1_000_000, 5, 0xFFFF)
        self.assertEqual(self.display.spi.writes[-1], (1, b"\xff\xff" * 480))
        self.display.spi.writes.clear()
        self.display.line(-20, -20, -1, -1, 0xFFFF)
        self.assertEqual(self.display.spi.writes, [])

    def test_batched_line_pixels_match_prior_bresenham_all_octants_and_clipping(self):
        # Small viewport permits broad boundary/reversal coverage without
        # retaining a display framebuffer or relying on run grouping itself.
        self.display.width, self.display.height = 13, 11
        endpoints = [(x, y) for x in (-3, -1, 0, 1, 6, 12, 13, 17)
                     for y in (-3, -1, 0, 1, 5, 10, 11, 15)]
        rectangles = []
        with patch.object(self.display, "fill_rect", side_effect=lambda *args: rectangles.append(args)):
            for start in endpoints:
                for end in endpoints:
                    rectangles.clear()
                    line = start + end
                    self.display.line(*line, 0x1234)
                    expected = reference_line_pixels(*line, 13, 11)
                    actual, drawn = set(), 0
                    for x, y, width, height, color in rectangles:
                        self.assertTrue(width == 1 or height == 1)
                        self.assertTrue(0 <= x < x + width <= 13)
                        self.assertTrue(0 <= y < y + height <= 11)
                        self.assertEqual(0x1234, color)
                        actual.update((px, py) for px in range(x, x + width)
                                      for py in range(y, y + height))
                        drawn += width * height
                    self.assertEqual(expected, actual, line)
                    self.assertEqual(len(actual), drawn, ("duplicate pixels", line))

    def test_batched_line_large_clipped_reversed_and_degenerate_cases(self):
        lines = [(-1_000_000, -100_000, 1_000_000, 100_000),
                 (-1_000_000, 100_000, 1_000_000, -100_000),
                 (12, -1_000_000, 14, 1_000_000),
                 (0, 0, 479, 319), (479, 0, 0, 319),
                 (-0.9, 5.9, 479.9, 100.9), (479, 319, 479, 319),
                 (-1, -1, -1, -1), (0, 0, 0, 0), (480, 320, 480, 320)]
        rectangles = []
        with patch.object(self.display, "fill_rect", side_effect=lambda *args: rectangles.append(args)):
            for line in lines:
                for coordinates in (line, line[2:] + line[:2]):
                    rectangles.clear()
                    self.display.line(*coordinates, 0xFFFF)
                    actual = {(px, py) for x, y, width, height, unused in rectangles
                              for px in range(x, x + width) for py in range(y, y + height)}
                    self.assertEqual(reference_line_pixels(*coordinates, 480, 320), actual, coordinates)
                    self.assertLessEqual(len(rectangles), 480)

    def test_shallow_and_steep_lines_reduce_actual_spi_windows_and_preserve_wire_pixels(self):
        for line in ((10, 10, 210, 30), (10, 10, 30, 210),
                     (210, 30, 10, 10), (30, 210, 10, 10)):
            self.display.spi.writes.clear()
            self.display.line(*line, 0xF81F)
            writes = self.display.spi.writes
            windows = sum(dc == 0 and data == b"\x2c" for dc, data in writes)
            expected = reference_line_pixels(*line, 480, 320)
            self.assertEqual(21, windows)
            self.assertEqual(6 * windows, len(writes))
            self.assertLess(len(writes), len(expected) * 6 // 5)
            # Reconstruct actual window payloads, independently checking that
            # fewer transactions have not gained/missed or duplicated pixels.
            actual, total = set(), 0
            for offset in range(0, len(writes), 6):
                self.assertEqual((0, b"\x2a"), writes[offset])
                self.assertEqual((0, b"\x2b"), writes[offset + 2])
                self.assertEqual((0, b"\x2c"), writes[offset + 4])
                xs, ys = writes[offset + 1][1], writes[offset + 3][1]
                left, right = int.from_bytes(xs[:2], "big"), int.from_bytes(xs[2:], "big")
                top, bottom = int.from_bytes(ys[:2], "big"), int.from_bytes(ys[2:], "big")
                pixels = {(x, y) for x in range(left, right + 1) for y in range(top, bottom + 1)}
                self.assertEqual((1, b"\xf8\x1f" * len(pixels)), writes[offset + 5])
                actual.update(pixels)
                total += len(pixels)
            self.assertEqual(expected, actual)
            self.assertEqual(len(expected), total)

    def test_diagonal_horizontal_vertical_and_point_lines_do_not_add_windows(self):
        for line, expected_windows in (((0, 0, 10, 10), 11), ((0, 10, 10, 0), 11),
                                       ((10, 2, 1, 2), 1), ((2, 10, 2, 1), 1),
                                       ((3, 3, 3, 3), 1)):
            self.display.spi.writes.clear()
            self.display.line(*line, 0xFFFF)
            windows = sum(dc == 0 and data == b"\x2c" for dc, data in self.display.spi.writes)
            self.assertEqual(expected_windows, windows)

    def test_cached_raster_matches_reference_for_all_scales_and_colors(self):
        # Deliberately asymmetric rows catch horizontal/vertical scaling and
        # bit-order errors that a single test pixel would miss.
        rows = bytes((0x01, 0x82, 0x24, 0x18, 0x7E, 0x00, 0xFF, 0xA5))
        self.display._glyphs[ord("X") - self.display.ASCII_FIRST] = rows
        for scale in (1, 2, 3, 4):
            for color, bg in ((0xF81F, 0x07E0), (0x1234, 0xABCD)):
                expected = bytearray()
                for y in range(8 * scale):
                    for x in range(8 * scale):
                        pixel = color if rows[y // scale] & (1 << (x // scale)) else bg
                        expected.extend((pixel >> 8, pixel & 255))
                first = self.display._raster("X", color, bg, scale)
                self.assertEqual(first, bytes(expected))
                if scale <= 2:
                    with patch.object(self.display, "_glyph", side_effect=AssertionError("cache miss")):
                        second = self.display._raster("X", color, bg, scale)
                else:
                    second = self.display._raster("X", color, bg, scale)
                    self.assertNotIn(("X", color, bg, scale), self.display._raster_keys)
                self.assertIs(first, second)

    def test_cached_text_keeps_identical_wire_output_and_clipping(self):
        self.display.text("A", -2, -1, 0xF800, bg=0x001F, scale=2)
        cold = list(self.display.spi.writes)
        self.display.spi.writes.clear()
        with patch.object(self.display, "_glyph", side_effect=AssertionError("cache miss")):
            self.display.text("A", -2, -1, 0xF800, bg=0x001F, scale=2)
        self.assertEqual(self.display.spi.writes, cold)

    def test_raster_cache_uses_preallocated_pool_and_reuses_fifo_slots(self):
        self.module.Display.RASTER_CACHE_BYTES = 1024
        self.module.Display.RASTER_CACHE_ENTRIES = 3
        self.display = self.module.Display()
        pool = self.display._raster_pool
        views = tuple(self.display._raster_slots)
        small_views = tuple(self.display._raster_small_slots)
        self.assertEqual(len(pool), 1024)
        self.assertEqual(self.display._raster_capacity, 2)
        for index in range(20):
            view = self.display._raster(chr(65 + index), index, 0, 2)
            self.assertIs(view, views[index % 2])
            self.assertIs(view.obj, pool)
            self.assertLessEqual(self.display._raster_bytes, 1024)
            self.assertLessEqual(len(self.display._raster_keys), 2)
            self.assertEqual(self.display._raster_bytes,
                             sum(128 * key[3] ** 2 for key in self.display._raster_keys if key is not None))
        self.assertEqual(len(self.display._raster_keys), 2)
        self.assertNotIn(("A", 0, 0, 2), self.display._raster_keys)
        for index in range(20):
            view = self.display._raster(chr(65 + index), index, 0, 1)
            self.assertIs(view, small_views[index % 2])
            self.assertIs(view.obj, pool)
        self.assertEqual(len(self.display._raster_keys), 2)
        self.assertEqual(self.display._raster_bytes, 2 * 128)
        self.assertIs(self.display._raster_pool, pool)
        self.assertEqual(tuple(self.display._raster_slots), views)
        # Larger glyphs reuse startup scratch storage without evicting cache.
        keys = list(self.display._raster_keys)
        self.assertEqual(len(self.display._raster("Z", 0xFFFF, 0, 4)), 2048)
        self.assertEqual(self.display._raster_keys, keys)
        self.assertIs(self.display._raster("Z", 0xFFFF, 0, 4).obj, self.display._text_buffer)

    def test_injected_full_raster_pool_is_reused_without_copy(self):
        pool = bytearray(49152)
        display = self.module.Display(raster_pool=pool)
        self.assertIs(display._raster_pool, pool)
        self.assertEqual(96, display._raster_capacity)
        self.assertEqual(96, len(display._raster_slots))
        self.assertTrue(all(slot.obj is pool for slot in display._raster_slots))
        self.assertTrue(all(slot.obj is pool for slot in display._raster_small_slots))
        display._raster("A", 0xFFFF, 0, 2)
        self.assertNotEqual(pool[:512], bytes(512))
        self.assertIs(display._raster_pool, pool)

    def test_bad_injected_pool_is_rejected_before_display_gpio_initialization(self):
        for pool in (bytearray(49151), bytearray(49153), bytes(49152), memoryview(bytearray(49152))):
            with self.subTest(type=type(pool).__name__, size=len(pool)), patch.object(
                    self.module, "Pin", side_effect=AssertionError("unexpected display GPIO")):
                with self.assertRaisesRegex(ValueError, "raster buffer size"):
                    self.module.Display(raster_pool=pool)

    def test_raster_cache_entry_limit_is_applied_at_initialization(self):
        self.module.Display.RASTER_CACHE_BYTES = 4096
        self.module.Display.RASTER_CACHE_ENTRIES = 3
        display = self.module.Display()
        for character in "ABCDEFGHIJK":
            display._raster(character, 0xFFFF, 0, 1)
        self.assertEqual(len(display._raster_keys), 3)
        self.assertEqual(len(display._raster_pool), 3 * 512)
        self.assertEqual(len(display._raster_keys), 3)

    def test_failed_raster_replacement_does_not_poison_error_rendering(self):
        self.module.Display.RASTER_CACHE_ENTRIES = 1
        display = self.module.Display()
        display._raster("A", 0xFFFF, 0, 2)
        with patch.object(display, "_write_raster",
                          side_effect=MemoryError("raster allocation")):
            with self.assertRaises(MemoryError):
                display._raster("B", 0xFFFF, 0, 2)
        self.assertEqual(display._raster_keys, [None])
        self.assertEqual(display._raster_bytes, 0)
        # This used to raise KeyError for the already removed A entry.
        display._raster("C", 0xFFFF, 0, 2)
        self.assertEqual(display._raster_keys, [("C", 0xFFFF, 0, 2)])
        self.assertEqual(display._raster_bytes, 512)

    def test_partial_raster_failure_keeps_fifo_slot_empty_until_retry(self):
        self.module.Display.RASTER_CACHE_ENTRIES = 1
        display = self.module.Display()
        display._raster("A", 0xFFFF, 0, 1)
        keys, glyphs = display._raster_keys, display._glyphs
        expected = bytes(display._raster("B", 0x1234, 0xABCD, 2))
        before_next = display._raster_next

        def partial(target, *args):
            target[:32] = bytes(32)
            raise MemoryError("row allocation after partial raster overwrite")

        with patch.object(display, "_write_raster", partial):
            with self.assertRaises(MemoryError):
                display._raster("C", 0x1234, 0xABCD, 2)
        self.assertIs(display._raster_keys, keys)
        self.assertIs(display._glyphs, glyphs)
        self.assertEqual(display._raster_keys, [None])
        self.assertEqual(display._raster_bytes, 0)
        self.assertEqual(display._raster_next, before_next)
        recovered = display._raster("B", 0x1234, 0xABCD, 2)
        self.assertEqual(bytes(recovered), expected)
        self.assertEqual(display._raster_keys, [("B", 0x1234, 0xABCD, 2)])
        self.assertEqual(display._raster_bytes, 512)

    def test_cache_metadata_remains_fixed_through_many_replacements(self):
        display = self.display
        keys, glyphs, pool = display._raster_keys, display._glyphs, display._raster_pool
        self.assertIsInstance(keys, list)
        self.assertIsInstance(glyphs, list)
        self.assertFalse(hasattr(display, "_raster_cache"))
        for index in range(2400):
            character = chr(32 + index % 95)
            scale = 1 + index % 2
            result = display._raster(character, index % 65536, (index * 17) % 65536, scale)
            self.assertIs(result.obj, pool)
            self.assertIs(display._raster_keys, keys)
            self.assertIs(display._glyphs, glyphs)
            self.assertEqual(len(keys), 96)
            self.assertEqual(len(glyphs), 95)
            self.assertEqual(display._raster_bytes, sum(128 * key[3] ** 2 for key in keys if key is not None))
        self.assertTrue(all(glyph is not None for glyph in glyphs))
        self.assertEqual(sum(key is not None for key in keys), 96)
        self.assertEqual(display._raster_next, 2400 % 96)
        last = (character, index % 65536, (index * 17) % 65536, scale)
        previous_next = display._raster_next
        with patch.object(display, "_write_raster", side_effect=AssertionError("cache hit rebuilt raster")):
            self.assertIs(display._raster(*last), result)
        self.assertEqual(display._raster_next, previous_next)

    def test_failed_ascii_glyph_allocation_can_retry_without_growing_metadata(self):
        display = self.display
        glyphs = display._glyphs
        display._glyph("A")
        first = glyphs[ord("A") - display.ASCII_FIRST]
        with patch.object(display._glyph_fb, "pixel", side_effect=MemoryError("glyph row allocation")):
            with self.assertRaises(MemoryError):
                display._raster("B", 0xFFFF, 0, 2)
        self.assertIs(display._glyphs, glyphs)
        self.assertIsNone(glyphs[ord("B") - display.ASCII_FIRST])
        self.assertIs(first, glyphs[ord("A") - display.ASCII_FIRST])
        self.assertEqual(display._raster_keys, [None] * 96)
        self.assertEqual(display._raster_bytes, 0)
        display._raster("B", 0xFFFF, 0, 2)
        self.assertIsNotNone(glyphs[ord("B") - display.ASCII_FIRST])
        self.assertEqual(len(glyphs), 95)
        self.assertEqual(display._raster_bytes, 512)

    def test_all_public_text_glyphs_fit_fixed_printable_ascii_slots(self):
        display = self.display
        display.text("micro \u00b5 \u03bc \u03a9 \u2212 \u00d7 \u00b0 \u2603\n", 0, 0, 0xFFFF, bg=0)
        for character in "micro uohm-xdeg? ":
            self.assertIsNotNone(display._glyphs[ord(character) - display.ASCII_FIRST])
        self.assertEqual(len(display._glyphs), 95)
        self.assertEqual(display._glyph("\x01"), display._glyph("?"))


class InputTests(unittest.TestCase):
    def setUp(self):
        FakePin.states = {}
        FakeI2C.available = True
        FakeI2C.instances = []
        self.module = load_driver("inputs")

    def test_button_pin_roles_remain_fixed_and_independent_of_touch(self):
        for available in (False, True):
            with self.subTest(touch_available=available):
                FakePin.states = {}
                FakeI2C.available = available
                inputs = self.module.Inputs()
                self.assertEqual(inputs.status["button_mapping"], {"left": 15, "right": 14})
                for number, role in ((14, "right"), (15, "left")):
                    FakePin.states[number] = 0
                    inputs.poll(100 * number)
                    self.assertEqual(inputs.poll(100 * number + 30), [(role,)])
                    self.assertEqual(inputs.poll(100 * number + 60), [])
                    FakePin.states[number] = 1
                    inputs.poll(100 * number + 61)
                    self.assertEqual(inputs.poll(100 * number + 91), [])

    def test_buttons_debounce_hold_and_release_without_touch(self):
        FakeI2C.available = False
        inputs = self.module.Inputs()
        self.assertFalse(inputs.status["touch_available"])
        FakePin.states[15] = 0
        self.assertEqual(inputs.poll(1), [])
        self.assertEqual(inputs.poll(30), [])
        self.assertEqual(inputs.poll(31), [("left",)])
        self.assertEqual(inputs.poll(100), [])
        FakePin.states[15] = 1
        inputs.poll(101)
        inputs.poll(131)
        FakePin.states[15] = 0
        inputs.poll(132)
        self.assertEqual(inputs.poll(162), [("left",)])

    def test_touch_rotates_and_reads_point_before_acknowledging(self):
        inputs = self.module.Inputs()
        bus = FakeI2C.instances[-1]
        bus.operations.clear()
        bus.touch(0, 0)
        self.assertEqual(inputs.poll(20), [("touch", 0, 319)])
        self.assertEqual(bus.operations, [
            ("read", 0x814E, 1, 16), ("read", 0x814F, 8, 16),
            ("write", 0x814E, b"\x00", 16)])
        bus.touch(319, 479)
        self.assertEqual(inputs.poll(40), [])  # Held finger does not repeat.
        bus.touch(count=0)
        self.assertEqual(inputs.poll(60), [])
        bus.touch(319, 479)
        self.assertEqual(inputs.poll(80), [("touch", 479, 0)])

    def test_touch_failure_is_bounded_and_buttons_still_work(self):
        inputs = self.module.Inputs()
        bus = FakeI2C.instances[-1]
        bus.fail_reads = True
        for now in (20, 40, 60):
            self.assertEqual(inputs.poll(now), [])
        self.assertFalse(inputs.status["touch_available"])
        operations = len(bus.operations)
        FakePin.states[14] = 0
        inputs.poll(80)
        self.assertEqual(inputs.poll(110), [("right",)])
        self.assertEqual(len(bus.operations), operations)

    def test_boot_held_button_is_suppressed_and_tick_wrap_debounces(self):
        FakeI2C.available = False
        FakePin.states[14] = 0
        inputs = self.module.Inputs()
        self.assertEqual(inputs.poll(100), [])
        FakePin.states[14] = 1
        inputs.poll(110)
        inputs.poll(140)
        FakePin.states[14] = 0
        inputs.poll((1 << 30) - 10)
        self.assertEqual(inputs.poll(20), [("right",)])


if __name__ == "__main__":
    unittest.main()
