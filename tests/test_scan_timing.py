"""Deterministic clocks prove timing attribution and bounded report storage."""
import io
import unittest
from unittest.mock import patch

from firmware import scan_timing as timing


class Clock:
    def __init__(self, value=0, period=None):
        self.value, self.period, self.reads = value, period, 0

    def ticks_us(self):
        self.reads += 1
        return self.value if self.period is None else self.value % self.period

    def ticks_diff(self, newer, older):
        delta = newer - older
        return delta if self.period is None else (delta + self.period // 2) % self.period - self.period // 2

    def advance(self, amount):
        self.value += amount


def fields(line):
    parts = line.decode("ascii").strip().split()
    assert parts[0] == "DOT_US"
    return {key: int(value) for key, value in (part.split("=") for part in parts[1:])}


class ScanTimingTests(unittest.TestCase):
    def make(self, clock=None, emit_cost=0):
        clock = Clock() if clock is None else clock
        lines = []
        def emit(line):
            lines.append(bytes(line))
            clock.advance(emit_cost)
            return len(line)
        return timing.ScanTiming(clock=clock, emit=emit), clock, lines

    def test_inactive_instrumentation_and_empty_finish_do_not_read_clock_or_emit(self):
        timer, clock, lines = self.make()
        self.assertFalse(timer.active)
        self.assertIsNone(timer.stamp())
        timer.add(timing.ENGINE, 0)
        timer.count(timing.POLLS)
        timer.phase(timing.DRIVE)
        timer.point(0, 0)
        self.assertFalse(timer.finish_point())
        self.assertEqual(0, clock.reads)
        timer.begin_scan()
        before = clock.reads
        self.assertFalse(timer.finish_point())
        self.assertEqual(before, clock.reads)
        self.assertEqual([], lines)

    def test_all_phase_buckets_partition_wall_and_reporting_cost_belongs_to_next_point(self):
        timer, clock, lines = self.make(emit_cost=23)
        timer.begin_scan()
        for elapsed, next_phase in ((3, timing.DRIVE), (5, timing.PARK_SETTLE),
                                   (7, timing.GATE_SETTLE), (11, timing.DRAIN_SETTLE),
                                   (13, timing.CONVERT), (17, timing.PROCESS)):
            clock.advance(elapsed)
            timer.phase(next_phase)
        clock.advance(19)
        timer.point(0, 0)
        self.assertTrue(timer.finish_point())
        self.assertEqual([3, 5, 7, 11, 13, 17, 19], timer.last[timing.OTHER:])
        self.assertEqual(75, timer.last_wall_us)
        self.assertEqual(timer.last_wall_us, sum(timer.last[timing.OTHER:]))
        self.assertEqual(0, timer.last[timing.EMIT])
        self.assertEqual(23, timer.last_emit_us)
        clock.advance(2)
        timer.phase(timing.CONVERT)
        clock.advance(4)
        timer.point(1, 0)
        timer.finish_point()
        self.assertEqual(29, timer.last_wall_us)
        self.assertEqual(23, timer.last[timing.OTHER])
        self.assertEqual(23, timer.last[timing.EMIT])
        self.assertEqual(2, timer.last[timing.PROCESS])
        self.assertEqual(4, timer.last[timing.CONVERT])
        self.assertEqual(29, sum(timer.last[timing.OTHER:]))
        self.assertEqual(23, fields(lines[-1])["emit"])

    def test_work_counters_are_overlapping_views_not_added_to_wall(self):
        timer, clock, lines = self.make()
        timer.begin_scan()
        engine_start = timer.stamp()
        clock.advance(4)
        i2c_start = timer.stamp()
        clock.advance(5)
        timer.add(timing.I2C, i2c_start)
        clock.advance(3)
        timer.add(timing.ENGINE, engine_start)
        for counter in (timing.INPUT, timing.RECORD, timing.RENDER, timing.SLEEP):
            started = timer.stamp()
            clock.advance(2)
            timer.add(counter, started)
        timer.count(timing.ADC_BLOCKS)
        timer.count(timing.POLLS, 4)
        timer.add(timing.RENDER, None)
        timer.point(7, 2)
        timer.finish_point(plotted=6)
        self.assertEqual(12, timer.last[timing.ENGINE])
        self.assertEqual(5, timer.last[timing.I2C])
        self.assertEqual(20, timer.last_wall_us)
        self.assertEqual(1, timer.last[timing.ADC_BLOCKS])
        self.assertEqual(4, timer.last[timing.POLLS])
        self.assertEqual(2, timer.last_plot_backlog)
        actual = fields(lines[0])
        self.assertEqual({"index": 7, "curve": 2, "wall": 20, "backlog": 2},
                         {key: actual[key] for key in ("index", "curve", "wall", "backlog")})
        self.assertEqual(20, actual["other"])
        self.assertGreater(sum(timer.last[:timing.EMIT]), timer.last_wall_us)

    def test_ticks_wrap_uses_injected_ticks_diff_for_work_wall_and_phase(self):
        timer, clock, unused = self.make(Clock(1000, 1024), emit_cost=9)
        timer.begin_scan()
        timer.phase(timing.CONVERT)
        started = timer.stamp()
        clock.advance(40)
        timer.add(timing.ENGINE, started)
        timer.point(0, 0)
        timer.finish_point()
        self.assertEqual(40, timer.last_wall_us)
        self.assertEqual(40, timer.last[timing.ENGINE])
        self.assertEqual(40, timer.last[timing.CONVERT])
        self.assertEqual(9, timer.last_emit_us)
        clock.advance(2)
        timer.point(1, 0)
        timer.finish_point()
        self.assertEqual(11, timer.last_wall_us)
        self.assertEqual(9, timer.last[timing.OTHER])
        self.assertEqual(2, timer.last[timing.CONVERT])

    def test_emitter_failure_is_counted_and_next_report_exposes_failure_and_overhead(self):
        clock, lines = Clock(), []
        def emit(line):
            clock.advance(12)
            if not lines:
                lines.append(None)
                raise MemoryError("Synthetic logging failure")
            lines.append(bytes(line))
        timer = timing.ScanTiming(clock, emit)
        timer.begin_scan()
        timer.point(0, 0)
        self.assertFalse(timer.finish_point())
        self.assertTrue(timer.active)
        self.assertTrue(timer.report_failed)
        self.assertEqual(1, timer.drop_count)
        self.assertEqual(12, timer.last_emit_us)
        timer.point(1, 0)
        self.assertTrue(timer.finish_point())
        self.assertEqual(12, timer.last[timing.EMIT])
        self.assertEqual(12, timer.last[timing.OTHER])
        self.assertEqual(1, fields(lines[-1])["dropped"])
        self.assertEqual(1, fields(lines[-1])["failed"])

    def test_short_write_and_formatter_overflow_cannot_abort_scan(self):
        timer = timing.ScanTiming(Clock(), lambda line: len(line) - 1)
        timer.begin_scan()
        timer.point(0, 0)
        self.assertFalse(timer.finish_point())
        self.assertEqual(1, timer.drop_count)
        timer.point(10000, 0)
        self.assertFalse(timer.finish_point())
        self.assertEqual(2, timer.drop_count)
        self.assertTrue(timer.active)

    def test_counter_lists_buffer_and_emitter_view_are_reused_across_4096_points(self):
        clock, views = Clock(), []
        def emit(line):
            if not views:
                views.append(line)
            self.assertIs(views[0], line)
            self.assertLessEqual(len(line), 512)
            self.assertEqual(10, line[-1])
        timer = timing.ScanTiming(clock, emit)
        last, work, buffer = timer.last, timer._work, timer._buffer
        timer.begin_scan()
        for index in range(4096):
            clock.advance(1)
            timer.count(timing.POLLS)
            timer.point(index, index // 64)
            self.assertTrue(timer.finish_point(plotted=index))
            self.assertIs(last, timer.last)
            self.assertIs(work, timer._work)
            self.assertIs(buffer, timer._buffer)
        self.assertEqual(4095, timer.last_index)
        self.assertEqual(63, timer.last_curve)
        self.assertEqual(1, timer.last[timing.POLLS])

    def test_stop_discards_pending_and_new_begin_resets_existing_state(self):
        timer, clock, lines = self.make(emit_cost=1)
        timer.begin_scan()
        timer.phase(timing.DRIVE)
        timer.count(timing.POLLS, 2)
        timer.point(0, 0)
        timer.stop()
        self.assertFalse(timer.finish_point())
        self.assertIsNone(timer.stamp())
        timer.count(timing.POLLS)
        timer.begin_scan()
        timer.point(0, 1)
        timer.finish_point()
        self.assertEqual(1, len(lines))
        self.assertEqual(0, timer.last[timing.POLLS])
        self.assertEqual(0, timer.last[timing.DRIVE])
        self.assertEqual(0, timer.last[timing.EMIT])

    def test_plot_backlog_is_nonnegative_and_optional(self):
        timer, unused, lines = self.make()
        timer.begin_scan()
        for plotted in (None, 99, 1):
            timer.point(1, 0)
            timer.finish_point(plotted=plotted)
        self.assertEqual([0, 0, 1], [fields(line)["backlog"] for line in lines])

    def test_callable_clock_and_cached_default_binary_stdout(self):
        now = [100]
        output = io.BytesIO()
        class Stdout:
            buffer = output
        with patch.object(timing.sys, "stdout", Stdout()):
            timer = timing.ScanTiming(clock=lambda: now[0])
        timer.begin_scan()
        now[0] += 20
        timer.point(0, 0)
        self.assertTrue(timer.finish_point())
        self.assertEqual(20, fields(output.getvalue())["wall"])

    def test_real_default_clock_is_usable_without_micropython(self):
        lines = []
        timer = timing.ScanTiming(emit=lambda line: lines.append(bytes(line)))
        timer.begin_scan()
        timer.point(0, 0)
        self.assertTrue(timer.finish_point())
        self.assertGreaterEqual(timer.last_wall_us, 0)
        self.assertEqual(1, len(lines))


if __name__ == "__main__":
    unittest.main()
