"""Settling starts after actual command/setup latency, including clock wrap."""
import unittest
from unittest.mock import patch

from firmware import engine as engine_module, model
from firmware.engine import Engine, STATUS_POLL_MS
from test_model_engine import FakeInstrument, settings


class Clock:
    def __init__(self, now=0, period=None):
        self.now, self.period = now, period

    def __call__(self):
        return self.now if self.period is None else self.now % self.period

    def advance(self, milliseconds):
        self.now += milliseconds


class LatentInstrument(FakeInstrument):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def set_duty(self, role, fraction):
        self.clock.advance(37)
        self.now = self.clock.now
        super().set_duty(role, fraction)

    def read_pair(self):
        self.clock.advance(11)
        self.now = self.clock.now
        return super().read_pair()

    def begin_measurement(self, count):
        self.now = self.clock.now
        super().begin_measurement(count)

    def poll_measurement(self):
        self.now = self.clock.now
        return super().poll_measurement()


class SettleDeadlineTests(unittest.TestCase):
    def test_every_transition_waits_full_interval_after_commands_despite_draw_overlap(self):
        for dwell in (100, 200):
            with self.subTest(dwell=dwell):
                clock = Clock(1000)
                hardware = LatentInstrument(clock)
                scanner = Engine(hardware, clock_ms=clock)
                scanner.start_scan(settings(drain_stop_v=2, drain_step_v=1,
                                             gate_stop_v=2, gate_step_v=1,
                                             settle_ms=dwell))
                points = []
                self.assertFalse(scanner.waiting_for_settle)
                for unused in range(3000):
                    events = scanner.tick(clock())
                    points.extend(data for kind, data in events if kind == "point")
                    if scanner.state != "scanning":
                        break
                    # Real drawing can consume part or all of a settling window.
                    clock.advance(60 if scanner.waiting_for_settle else 5)
                self.assertEqual(scanner.state, "complete", scanner.error)
                self.assertEqual(len(points), 4)
                self.assertEqual(len(hardware.triggers), 4)
                for triggered, _ in hardware.triggers:
                    command_finished = max(when for when, _, _ in hardware.timed_commands
                                           if when <= triggered)
                    self.assertGreaterEqual(triggered - command_finished, dwell)
                self.assertFalse(scanner.waiting_for_settle)
                self.assertTrue(scanner.shutdown_succeeded)

    def test_drive_setup_is_completed_before_the_deadline_is_anchored(self):
        clock = Clock()
        hardware = LatentInstrument(clock)
        scanner = Engine(hardware, clock_ms=clock)
        scanner.start_scan(settings(settle_ms=100))
        scanner.tick(clock())
        self.assertEqual(clock.now, 74)  # Both PWM writes finish before dwell.
        self.assertEqual(scanner._deadline, 174)
        self.assertTrue(scanner.waiting_for_settle)
        clock.advance(99)
        scanner.tick(clock())
        self.assertEqual(hardware.triggers, [])
        clock.advance(1 + STATUS_POLL_MS)
        scanner.tick(clock())
        self.assertEqual(len(hardware.triggers), 1)
        self.assertGreaterEqual(hardware.triggers[0][0], 174)
        self.assertFalse(scanner.waiting_for_settle)  # Conversion is now pending.

    def test_post_command_deadline_handles_native_tick_wrap(self):
        clock = Clock(1000, period=1024)
        hardware = LatentInstrument(clock)
        scanner = Engine(hardware, clock_ms=clock)
        scanner.start_scan(settings(settle_ms=100))
        wrap_diff = lambda now, before: (now - before + 512) % 1024 - 512
        with patch.object(engine_module, "_ticks_diff", wrap_diff):
            scanner.tick(clock())
            self.assertEqual(clock(), 50)
            self.assertEqual(scanner._clock, 74)
            self.assertEqual(scanner._deadline, 174)
            clock.advance(99)
            scanner.tick(clock())
            self.assertEqual(hardware.triggers, [])
            clock.advance(1 + STATUS_POLL_MS)
            scanner.tick(clock())
        self.assertEqual(len(hardware.triggers), 1)
        self.assertGreaterEqual(hardware.triggers[0][0] - hardware.timed_commands[0][0], 100)
        self.assertEqual(scanner.state, "scanning")

    def test_native_default_clock_is_sampled_after_commands_and_host_none_is_compatible(self):
        clock = Clock()
        hardware = LatentInstrument(clock)
        with patch.object(engine_module, "_ticks_ms", clock):
            scanner = Engine(hardware)
        scanner.start_scan(settings(settle_ms=100))
        scanner.tick(clock())
        self.assertEqual(scanner._deadline, 174)
        with patch.object(engine_module, "_ticks_ms", None):
            host = Engine(FakeInstrument())
        host.start_scan(settings(settle_ms=100))
        host.tick(500)
        self.assertEqual(host._deadline, 100)

    def test_new_settings_accept_100_ms_without_widening_legacy_timing_contract(self):
        self.assertEqual(model.DEFAULT_SETTINGS["settle_ms"], 100)
        for value in (100, 200, 10000):
            self.assertEqual(model.validate_settings(settings(settle_ms=value))["settle_ms"], value)
        for value in (99, 100.5, 10001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                model.validate_settings(settings(settle_ms=value))
        legacy = dict(model.DEFAULT_SETTINGS)
        legacy.update(version=2, **model.LEGACY_TIMING)
        del legacy["ina_average_count"]
        legacy["settle_ms"] = 100
        with self.assertRaises(ValueError):
            model.validate_settings(legacy)
        legacy["settle_ms"] = 200
        self.assertEqual(model.validate_settings(legacy)["settle_ms"], 200)


if __name__ == "__main__":
    unittest.main()
