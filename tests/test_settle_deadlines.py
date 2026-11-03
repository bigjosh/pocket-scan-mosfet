"""Settling starts after actual command/setup latency, including clock wrap."""
import unittest
from unittest.mock import patch

from firmware import engine as engine_module, model
from firmware.engine import Engine, STATUS_POLL_MS
from test_model_engine import FakeInstrument, calibration, settings


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
                                             settle_ms=dwell), calibration())
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
                self.assertEqual(len(hardware.triggers), 8)
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
        scanner.start_scan(settings(settle_ms=100), calibration())
        reset_batch = scanner._reset_batch

        def slow_setup():
            reset_batch()
            clock.advance(23)

        scanner._reset_batch = slow_setup
        scanner.tick(clock())
        self.assertEqual(clock.now, 60)  # 37 ms command + 23 ms retained setup.
        self.assertEqual(scanner._deadline, 160)
        self.assertTrue(scanner.waiting_for_settle)
        clock.advance(99)
        scanner.tick(clock())
        self.assertEqual(hardware.triggers, [])
        clock.advance(1 + STATUS_POLL_MS)
        scanner.tick(clock())
        self.assertEqual(len(hardware.triggers), 1)
        self.assertGreaterEqual(hardware.triggers[0][0], 160)
        self.assertFalse(scanner.waiting_for_settle)  # Conversion is now pending.

    def test_correction_retry_deadline_starts_after_the_corrected_pwm_write(self):
        for phase in ("park_drain", "settle_gate", "settle"):
            with self.subTest(phase=phase):
                clock = Clock()
                hardware = LatentInstrument(clock)
                scanner = Engine(hardware, clock_ms=clock)
                scanner.start_scan(settings(source_voltage_v=1, settle_ms=100), calibration())
                scanner.tick(clock())
                scanner._phase = phase
                clock.advance(30)  # Completed read/check work after tick entry.
                self.assertFalse(scanner._check_settled(
                    {"drain": {"terminal_v": 0.95}}, {"drain": 1.0}, ("drain",)))
                self.assertEqual(scanner._clock, clock.now)
                self.assertEqual(scanner._deadline, clock.now + 100)
                self.assertEqual(hardware.timed_commands[-1][0], clock.now)
                self.assertTrue(scanner.waiting_for_settle)

    def test_post_command_deadline_handles_native_tick_wrap(self):
        clock = Clock(1000, period=1024)
        hardware = LatentInstrument(clock)
        scanner = Engine(hardware, clock_ms=clock)
        scanner.start_scan(settings(settle_ms=100), calibration())
        wrap_diff = lambda now, before: (now - before + 512) % 1024 - 512
        with patch.object(engine_module, "_ticks_diff", wrap_diff):
            scanner.tick(clock())
            self.assertEqual(clock(), 13)
            self.assertEqual(scanner._clock, 37)
            self.assertEqual(scanner._deadline, 137)
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
        scanner.start_scan(settings(settle_ms=100), calibration())
        scanner.tick(clock())
        self.assertEqual(scanner._deadline, 137)
        with patch.object(engine_module, "_ticks_ms", None):
            host = Engine(FakeInstrument())
        host.start_scan(settings(settle_ms=100), calibration())
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
