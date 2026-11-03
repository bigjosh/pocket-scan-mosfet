"""Optional timing hooks preserve scan sequencing, data, and fault cleanup."""

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from firmware import scan_timing
from firmware.engine import Engine
from test_hardware import MachineFake
from test_model_engine import FakeInstrument, run_engine, settings


class TimingRecorder:
    def __init__(self, active=True):
        self.active = active
        self.phases = []
        self.added = []
        self.counts = {}

    def stamp(self):
        # Zero is a valid clock stamp, not a disabled timer sentinel.
        return 0 if self.active else None

    def add(self, counter, started):
        self.added.append((counter, started))

    def phase(self, code):
        self.phases.append(code)

    def count(self, counter, n=1):
        self.counts[counter] = self.counts.get(counter, 0) + n


class EngineTimingTests(unittest.TestCase):
    def test_every_point_has_one_block_and_timed_data_is_unchanged(self):
        config = settings(drain_stop_v=2, drain_step_v=1, gate_stop_v=2, gate_step_v=1)
        plain_hardware = FakeInstrument()
        plain = Engine(plain_hardware)
        plain.start_scan(config)
        expected_events = run_engine(plain, plain_hardware)

        timing, hardware = TimingRecorder(), FakeInstrument()
        engine = Engine(hardware, timing=timing)
        engine.start_scan(config)
        events, phase_groups, first_phase, tick_count = [], [], 0, 0
        for now in range(10000):
            hardware.now = now
            emitted = engine.tick(now)
            tick_count += 1
            events.extend(emitted)
            if any(kind == "point" for kind, _ in emitted):
                phase_groups.append(timing.phases[first_phase:])
                first_phase = len(timing.phases)
            if engine.state != "scanning":
                break
        self.assertEqual(engine.state, "complete")
        phases = [scan_timing.DRIVE, scan_timing.DRAIN_SETTLE,
                  scan_timing.CONVERT, scan_timing.PROCESS]
        self.assertEqual(phase_groups, [phases] * 4)
        self.assertEqual(events, expected_events)
        self.assertEqual(hardware.timed_commands, plain_hardware.timed_commands)
        self.assertEqual(hardware.triggers, plain_hardware.triggers)
        self.assertEqual(hardware.read_times, plain_hardware.read_times)
        self.assertEqual(timing.added, [(scan_timing.ENGINE, 0)] * tick_count)
        self.assertTrue(timing.active)  # UI still owns painting and finishing the profile.

    def test_fault_tick_is_timed_and_still_stops_outputs_without_a_point(self):
        timing, hardware = TimingRecorder(), FakeInstrument()
        engine = Engine(hardware, timing=timing)
        engine.start_scan(settings())
        engine.tick(0)
        hardware.read_error = OSError("synthetic read failure")
        events = []
        for now in range(1, 1000):
            hardware.now = now
            events.extend(engine.tick(now))
            if engine.state == "error":
                break
        self.assertEqual(engine.state, "error")
        self.assertTrue(engine.shutdown_succeeded)
        self.assertEqual(hardware.duties, {"drain": 0.0, "gate": 0.0})
        self.assertFalse(any(kind == "point" for kind, _ in events))
        self.assertEqual(len(timing.added), now + 1)
        self.assertEqual(timing.added[-1], (scan_timing.ENGINE, 0))

    def test_inactive_timer_does_not_record_scan_work(self):
        timing, hardware = TimingRecorder(active=False), FakeInstrument()
        engine = Engine(hardware, timing=timing)
        engine.start_scan(settings(drain_stop_v=2, drain_step_v=1))
        for now in range(500):
            hardware.now = now
            engine.tick(now)
        engine.cancel()
        self.assertEqual(timing.phases, [])
        self.assertEqual(timing.added, [])
        self.assertEqual(timing.counts, {})
        self.assertTrue(engine.shutdown_succeeded)


class HardwareTimingTests(unittest.TestCase):
    def setUp(self):
        self.fake = MachineFake()
        spec = importlib.util.spec_from_file_location(
            "firmware.hardware", Path(__file__).resolve().parents[1] / "firmware" / "hardware.py")
        self.hw = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"machine": self.fake.module}):
            spec.loader.exec_module(self.hw)
        self.timing = TimingRecorder(active=False)
        self.instrument = self.hw.Instrument(timing=self.timing)
        self.assertEqual(self.timing.added, [])
        self.timing.active = True

    def test_register_read_write_and_errors_are_all_timed_in_finally(self):
        self.assertEqual(self.instrument._read16(0x44, 1), 0)
        self.instrument._write16(0x44, 7, 123)
        self.fake.read_error = (0x44, 1)
        with self.assertRaises(OSError):
            self.instrument._read16(0x44, 1)
        self.fake.write_error = (0x44, 7)
        with self.assertRaises(OSError):
            self.instrument._write16(0x44, 7, 456)
        self.assertEqual(self.timing.added, [(scan_timing.I2C, 0)] * 4)

    def test_only_successful_trigger_counts_and_ready_polls_are_counted(self):
        self.instrument.begin_measurement(64)
        self.assertEqual(self.timing.counts, {scan_timing.ADC_BLOCKS: 1})
        self.assertFalse(self.instrument.poll_measurement())
        self.fake.registers[0x44][6] |= self.hw.READY_FLAG
        self.assertFalse(self.instrument.poll_measurement())
        self.fake.registers[0x41][6] |= self.hw.READY_FLAG
        self.assertTrue(self.instrument.poll_measurement())
        self.assertEqual(self.timing.counts[scan_timing.POLLS], 3)
        self.instrument.read_pair()
        self.fake.write_error = (0x41, 0)
        self.fake.events.clear()
        with self.assertRaises(self.hw.HardwareError):
            self.instrument.begin_measurement(64)
        self.assertEqual(self.timing.counts[scan_timing.ADC_BLOCKS], 1)
        self.assertEqual([event for event in self.fake.events if event[0] == "duty"],
                         [("duty", 21, 0), ("duty", 20, 0)])
        self.assertTrue(all(pwm.duty == 0 for pwm in self.fake.pwms.values()))

    def test_failed_ready_poll_is_counted_without_masking_hardware_shutdown(self):
        self.instrument.begin_measurement(64)
        self.fake.read_error = (0x44, 6)
        self.fake.events.clear()
        with self.assertRaises(self.hw.HardwareError):
            self.instrument.poll_measurement()
        self.assertEqual(self.timing.counts[scan_timing.POLLS], 1)
        self.assertEqual(self.timing.added[-1], (scan_timing.I2C, 0))
        self.assertEqual([event for event in self.fake.events if event[0] == "duty"],
                         [("duty", 21, 0), ("duty", 20, 0)])


if __name__ == "__main__":
    unittest.main()
