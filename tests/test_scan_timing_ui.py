"""Per-point timing follows actual RAM UI work without delaying scan control."""
import unittest

from firmware import model, scan_timing
from firmware.engine import Engine, OverrangeError
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_ram_ui import RamCloud, RamWifi
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore


class Clock:
    def __init__(self):
        self.now = 0

    def ticks_us(self):
        return self.now

    @staticmethod
    def ticks_diff(now, before):
        return now - before

    def advance(self, elapsed):
        self.now += elapsed


class ScanTimingUITests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.trace, self.reports, self.logs, self.restarts = [], [], [], []
        self.expected = {"input": 0, "record": 0, "render": 0}
        self.fail_reports = False
        owner = self

        class Timing(scan_timing.ScanTiming):
            def begin_scan(self):
                owner.trace.append(("begin", owner.app.screen, owner.app.dirty,
                                    len(owner.instrument.commands)))
                super().begin_scan()

            def point(self, index, curve):
                owner.trace.append(("point", index, len(owner.buffer)))
                super().point(index, curve)

            def stop(self):
                owner.trace.append(("stop", owner.app.screen))
                super().stop()

        def charge(region, amount):
            owner.clock.advance(amount)
            if owner.timing.active and region in owner.expected:
                owner.expected[region] += amount

        class Display(FakeDisplay):
            def fill(self, color):
                charge("render", 11)
                super().fill(color)

            def text(self, *args, **kwargs):
                charge("render", 13)
                super().text(*args, **kwargs)

            def fill_rect(self, *args):
                charge("render", 17)

            rect = fill_rect
            line = fill_rect

        class Inputs(FakeInputs):
            def poll(self, now):
                charge("input", 7)
                return super().poll(now)

        class Instrument(HostInstrument):
            def set_duty(self, role, value):
                charge("engine", 9)
                owner.trace.append(("drive", role, value, len(owner.buffer)))
                super().set_duty(role, value)

            def poll_alerts(self):
                charge("engine", 5)

            def begin_measurement(self, count):
                charge("engine", 13)
                owner.trace.append(("trigger", len(owner.buffer)))
                super().begin_measurement(count)

            def poll_measurement(self):
                charge("engine", 3)
                return super().poll_measurement()

            def read_pair(self):
                charge("engine", 19)
                pair = super().read_pair()
                # Trigger one real axis rescale with a small raw shunt reading.
                extra_shunt = 5e-6 * model.DRAIN_SHUNT_OHM
                pair["drain"]["shunt_v"] += extra_shunt
                pair["drain"]["bus_v"] += extra_shunt
                return pair

        class Cloud(RamCloud):
            def record_point(self, row):
                charge("record", 23)
                owner.trace.append(("record", row["index"]))
                super().record_point(row)

            def finish_scan(self, now, fault=None):
                charge("record", 29)
                owner.trace.append(("seal",))
                super().finish_scan(now, fault)

            def tick(self, now, online=False):
                owner.trace.append(("upload_tick", owner.timing.active))
                super().tick(now, online)

        self.timing = Timing(clock=self.clock, emit=self.emit)
        self.display, self.inputs, self.instrument = Display(), Inputs(), Instrument()
        self.engine = Engine(self.instrument, timing=self.timing)
        self.buffer = PackedScanBuffer(capacity=16)
        self.cloud, self.wifi = Cloud(self.buffer), RamWifi()
        self.settings = dict(model.DEFAULT_SETTINGS)
        self.settings.update(drain_start_uv=100000, drain_stop_uv=300000,
                             drain_step_uv=100000, gate_start_uv=1000000,
                             gate_stop_uv=1000000, gate_step_uv=1000000,
                             ina_average_count=4)
        self.app = App(self.display, self.inputs, self.engine, self.settings,
                       store=MemoryStore(), wifi=self.wifi,
                       cloud=self.cloud, scan_buffer=self.buffer, timing=self.timing,
                       restart=lambda: self.restarts.append(True),
                       error_logger=lambda context, error: self.logs.append((context, str(error))))
        for name in ("render", "_plot_axes", "_render_live", "_draw_points"):
            original = getattr(self.app, name)

            def traced(*args, _name=name, _original=original, **kwargs):
                self.trace.append(("draw_begin", _name, self.app.screen))
                result = _original(*args, **kwargs)
                self.trace.append(("draw_end", _name, self.app.screen))
                return result

            setattr(self.app, name, traced)
        self.tick()

    def emit(self, line):
        text = bytes(line).decode("ascii")
        self.assertTrue(text.startswith("DOT_US "))
        values = {key: int(value) for key, value in
                  (part.split("=", 1) for part in text.split()[1:])}
        self.trace.append(("emit", values["index"], self.app.screen))
        self.reports.append({"values": values, "expected": dict(self.expected),
                             "buffer_count": len(self.buffer), "screen": self.app.screen,
                             "cloud": self.cloud.state, "active": self.timing.active,
                             "dirty": self.app.dirty, "cursor": self.app._plot_cursor})
        for key in self.expected:
            self.expected[key] = 0
        self.clock.advance(31)  # Synchronous logging belongs to the following point.
        if self.fail_reports:
            raise OSError("diagnostic output unavailable")
        return len(line)

    def tick(self, action=None):
        self.clock.advance(20000)
        self.inputs.events = [(action,)] if action else []
        self.app.tick(self.clock.now // 1000)

    def until(self, predicate, limit=500):
        for _ in range(limit):
            if predicate():
                return
            self.tick()
        self.fail("Did not reach state: %s / %s" % (self.app.screen, self.engine.state))

    def start(self):
        self.tick("right")
        self.until(lambda: self.app.screen == "scan")

    def complete(self):
        self.start()
        self.until(lambda: self.app.screen == "upload")

    def test_begin_excludes_preparation_and_first_scan_paint(self):
        self.start()
        self.assertFalse(self.timing.active)
        self.assertEqual(self.instrument.commands, [])
        self.assertFalse(self.app.dirty)
        self.assertEqual(self.reports, [])
        self.tick()
        begin = [event for event in self.trace if event[0] == "begin"]
        self.assertEqual(begin, [("begin", "scan", False, 0)])
        self.assertTrue(self.timing.active)
        self.assertTrue(self.instrument.commands)

    def test_input_record_and_all_rendering_are_accounted_exactly_once(self):
        self.complete()
        self.assertEqual([r["values"]["index"] for r in self.reports], [0, 1, 2])
        for report in self.reports:
            for counter in ("input", "record", "render"):
                self.assertEqual(report["values"][counter], report["expected"][counter],
                                 (report["values"]["index"], counter))
            self.assertGreater(report["values"]["input"], 0)
            if report["values"]["index"] == 0:
                self.assertEqual(report["values"]["render"], 0)
            else:
                self.assertGreater(report["values"]["render"], 0)
            self.assertEqual(sum(report["values"][key] for key in
                                 ("other", "drive", "park_settle", "gate_settle",
                                  "drain_settle", "convert", "process")), report["values"]["wall"])
        self.assertEqual([r["values"]["record"] for r in self.reports], [23, 23, 52])
        self.assertEqual([r["values"]["emit"] for r in self.reports], [0, 31, 31])
        point = next(i for i, event in enumerate(self.trace) if event[:2] == ("point", 0))
        record = next(i for i, event in enumerate(self.trace) if event[:2] == ("record", 0))
        self.assertFalse(any(event[0].startswith("draw_")
                             for event in self.trace[record:point]))

    def test_previous_point_draws_after_next_drive_and_before_trigger(self):
        self.complete()
        for index in (0, 1):
            report = self.trace.index(("emit", index, "scan"))
            drive = next(i for i in range(report + 1, len(self.trace))
                         if self.trace[i][0] == "drive")
            trigger = next(i for i in range(drive + 1, len(self.trace))
                           if self.trace[i][0] == "trigger")
            draws = [i for i in range(report + 1, trigger)
                     if self.trace[i][0].startswith("draw_")]
            self.assertTrue(draws)
            self.assertGreater(min(draws), drive)
            self.assertTrue(any(self.trace[i][:2] == ("draw_end", "_draw_points")
                                for i in draws))

    def test_report_follows_record_and_describes_deferred_plot_backlog(self):
        self.complete()
        previous = 0
        for report in self.reports:
            index = report["values"]["index"]
            end = next(i for i in range(previous, len(self.trace))
                       if self.trace[i][:2] == ("emit", index))
            window = self.trace[previous:end]
            self.assertIn(("record", index), window)
            self.assertIn(("point", index, index + 1), window)
            if report["screen"] == "scan":
                self.assertEqual(window[-1][0], "point")
                self.assertGreaterEqual(report["values"]["backlog"], 1)
            self.assertEqual(report["buffer_count"], index + 1)
            self.assertEqual(report["values"]["backlog"], max(0, index + 1 - report["cursor"]))
            self.assertFalse(report["dirty"])
            previous = end + 1

    def test_final_point_emits_then_stops_before_any_upload_tick(self):
        self.complete()
        final = self.reports[-1]
        self.assertEqual(final["screen"], "upload")
        self.assertEqual(final["cloud"], "preparing")
        self.assertTrue(final["active"])
        self.assertFalse(self.timing.active)
        emit_index = self.trace.index(("emit", 2, "upload"))
        begin_index = next(i for i, event in enumerate(self.trace) if event[0] == "begin")
        stop_index = next(i for i in range(begin_index, len(self.trace))
                          if self.trace[i][0] == "stop")
        self.assertGreater(stop_index, emit_index)
        self.assertFalse(any(event[0] == "upload_tick" for event in self.trace))
        self.tick()
        upload_index = next(i for i, event in enumerate(self.trace) if event[0] == "upload_tick")
        self.assertGreater(upload_index, stop_index)
        self.assertEqual(self.trace[upload_index], ("upload_tick", False))
        self.assertEqual(len(self.reports), 3)

    def test_reporting_failure_does_not_break_scan_or_drop_measured_points(self):
        self.fail_reports = True
        self.complete()
        self.assertEqual(len(self.buffer), 3)
        self.assertEqual(self.engine.state, "complete")
        self.assertEqual(self.cloud.state, "preparing")
        self.assertEqual(self.timing.drop_count, 3)
        self.assertFalse(self.timing.active)
        self.assertEqual(self.logs, [])

    def test_every_gate_curve_uses_one_shared_settle_and_no_verification_passes(self):
        self.app.settings["gate_stop_uv"] = 2000000
        self.complete()
        self.assertEqual([r["values"]["index"] for r in self.reports], list(range(6)))
        self.assertEqual([r["values"]["curve"] for r in self.reports], [0, 0, 0, 1, 1, 1])
        for index, report in enumerate(self.reports):
            for counter in ("park_settle", "gate_settle"):
                self.assertEqual(report["values"][counter], 0)
            self.assertGreater(report["values"]["drain_settle"], 0)

    def test_overrange_keeps_prior_report_without_inventing_completed_fault_point(self):
        self.start()
        self.until(lambda: len(self.reports) == 1)
        self.instrument.error = OverrangeError("drain", "shunt_saturation", 0x8000,
                                              -1, 0.08192, "V")
        self.until(lambda: self.app.screen == "upload")
        self.assertEqual(len(self.buffer), 1)
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(self.engine.state, "partial")
        self.assertIsNotNone(self.cloud.fault)
        self.assertFalse(self.timing.active)

    def test_cancel_before_first_drive_emits_no_completed_point(self):
        self.start()
        self.tick("left")
        self.assertEqual(self.reports, [])
        self.assertEqual(self.instrument.commands, [])
        self.assertFalse(self.timing.active)
        self.assertEqual(self.restarts, [True])
        self.assertEqual(len(self.buffer), 0)

    def test_cancel_after_one_point_emits_no_extra_partial_timing_row(self):
        self.start()
        self.until(lambda: len(self.reports) == 1)
        self.tick("left")
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(self.reports[0]["values"]["index"], 0)
        self.assertFalse(self.timing.active)
        self.assertEqual(self.restarts, [True])
        self.assertEqual(len(self.buffer), 0)
        self.assertFalse(any(event[0] == "upload_tick" for event in self.trace))


if __name__ == "__main__":
    unittest.main()
