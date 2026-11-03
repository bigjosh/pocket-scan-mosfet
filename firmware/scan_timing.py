"""Allocation-bounded, synchronous per-point USB timing diagnostics.

The cycle boundary is point acceptance through that UI tick's rendering, not
necessarily visible-dot completion: backlog counts points awaiting plotting.
Phase buckets partition wall time; work counters overlap (ENGINE includes I2C)
and must not be added to the phases. CONVERT includes readiness-poll latency.
ADC_BLOCKS and POLLS are event counts, not microseconds.

One fixed byte buffer/view and two counter lists are allocated at construction.
Reports use zero-padded decimal fields, never JSON or per-point mappings. The
stdout write is synchronous and may block. Its formatting/write time is charged
to the FOLLOWING point as EMIT and OTHER; last_emit_us exposes the latest cost.
Reporting failures are dropped, counted, and never propagated into acquisition.
Injected emitters must consume the supplied reusable view synchronously.
"""
import sys
import time

INPUT, ENGINE, I2C, RECORD, RENDER, SLEEP, EMIT = range(7)
ADC_BLOCKS, POLLS = 7, 8
OTHER, DRIVE, PARK_SETTLE, GATE_SETTLE, DRAIN_SETTLE, CONVERT, PROCESS = range(9, 16)
_SIZE = 16
_LABELS = (b"input", b"engine", b"i2c", b"record", b"render", b"sleep", b"emit",
           b"adc_blocks", b"polls", b"other", b"drive", b"park_settle",
           b"gate_settle", b"drain_settle", b"convert", b"process")


def _host_us():
    return time.monotonic_ns() // 1000


def _linear_diff(new, old):
    return new - old


class ScanTiming:
    def __init__(self, clock=None, emit=None):
        clock = time if clock is None else clock
        self._ticks = getattr(clock, "ticks_us", clock if callable(clock) else _host_us)
        self._diff = getattr(clock, "ticks_diff", _linear_diff)
        if emit is None:
            # RP2 stdout accepts the buffer protocol; CPython exposes .buffer.
            output = getattr(sys.stdout, "buffer", sys.stdout)
            emit = output.write
        self._emit = emit
        self._work = [0] * _SIZE
        self.last = [0] * _SIZE
        self.active = False
        self._pending = False
        self._index = self._curve = -1
        self.last_index = self.last_curve = -1
        self.last_wall_us = self.last_emit_us = self.last_plot_backlog = 0
        self.drop_count = 0
        self.report_failed = False
        self._phase = OTHER
        self._phase_started = self._window_started = 0
        # Fixed widths avoid constructing/slicing a new view for every report.
        self._buffer = bytearray(512)
        self._buffer[:6] = b"DOT_US"
        offset, slots = 6, []
        for label, width in ((b"index", 4), (b"curve", 4), (b"wall", 10),
                             (b"backlog", 4), (b"dropped", 6), (b"failed", 1)):
            offset = self._field(offset, label, width, slots)
        self._header_slots = tuple(slots)
        slots = []
        for label in _LABELS:
            offset = self._field(offset, label, 10, slots)
        self._counter_slots = tuple(slots)
        self._buffer[offset] = 10
        self._line = memoryview(self._buffer)[:offset + 1]

    def _field(self, offset, label, width, slots):
        self._buffer[offset] = 32
        offset += 1
        self._buffer[offset:offset + len(label)] = label
        offset += len(label)
        self._buffer[offset] = 61
        offset += 1
        slots.append((offset, width))
        return offset + width

    def begin_scan(self):
        for index in range(_SIZE):
            self._work[index] = self.last[index] = 0
        self.last_index = self.last_curve = -1
        self.last_wall_us = self.last_emit_us = self.last_plot_backlog = 0
        self.drop_count = 0
        self.report_failed = False
        self._pending = False
        self._phase = OTHER
        self._window_started = self._phase_started = self._ticks()
        self.active = True

    def stamp(self):
        return self._ticks() if self.active else None

    def add(self, counter, started):
        if self.active and started is not None:
            elapsed = self._diff(self._ticks(), started)
            if elapsed > 0:
                self._work[counter] += elapsed

    def count(self, counter, amount=1):
        if self.active:
            self._work[counter] += amount

    def _close_phase(self, now):
        elapsed = self._diff(now, self._phase_started)
        if elapsed > 0:
            self._work[self._phase] += elapsed
        self._phase_started = now

    def phase(self, code):
        if self.active and code != self._phase:
            self._close_phase(self._ticks())
            self._phase = code

    def point(self, index, curve):
        if self.active:
            self._index, self._curve, self._pending = index, curve, True

    def _number(self, slot, value):
        offset, width = slot
        if value < 0:
            raise ValueError("Negative scan timing field")
        for index in range(offset + width - 1, offset - 1, -1):
            self._buffer[index] = 48 + value % 10
            value //= 10
        if value:
            raise ValueError("Scan timing field overflow")

    def _format(self):
        self._number(self._header_slots[0], self.last_index)
        self._number(self._header_slots[1], self.last_curve)
        self._number(self._header_slots[2], self.last_wall_us)
        self._number(self._header_slots[3], self.last_plot_backlog)
        self._number(self._header_slots[4], self.drop_count)
        self._number(self._header_slots[5], int(self.report_failed))
        for index in range(_SIZE):
            self._number(self._counter_slots[index], self.last[index])

    def finish_point(self, plotted=None):
        """Snapshot a pending point after its tick; report errors return False."""
        if not self.active or not self._pending:
            return False
        boundary = self._ticks()
        self._close_phase(boundary)
        prior_phase = self._phase
        for index in range(_SIZE):
            self.last[index], self._work[index] = self._work[index], 0
        self.last_index, self.last_curve = self._index, self._curve
        self.last_wall_us = max(0, self._diff(boundary, self._window_started))
        self.last_plot_backlog = 0 if plotted is None else max(0, self._index + 1 - plotted)
        self._pending = False
        # Advance BEFORE reporting so logging cost appears in the next cycle.
        self._window_started = self._phase_started = boundary
        self._phase = OTHER
        success = False
        try:
            self._format()
            written = self._emit(self._line)
            if written is not None and written != len(self._line):
                raise OSError("Short scan timing report")
            success = True
        except Exception:
            self.drop_count += 1
            self.report_failed = True
        finally:
            ended = self._ticks()
            self.last_emit_us = max(0, self._diff(ended, boundary))
            self._work[EMIT] += self.last_emit_us
            self._close_phase(ended)
            self._phase = prior_phase
        return success

    def stop(self):
        self.active = False
        self._pending = False
