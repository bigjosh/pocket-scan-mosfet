"""Manual output control, fresh mixed-sensor monitoring and bounded storage."""

import gc
import importlib.util
import math
from pathlib import Path
import sys
import tracemalloc
import unittest
from unittest.mock import patch

from firmware import current_baseline, manual_debug, model
from firmware.engine import Engine, OverrangeError
from tests.test_hardware import MachineFake


def settings(**changes):
    value = dict(model.DEFAULT_SETTINGS, driver_supply_uv=19000000,
                 ina_average_count=4)
    value.update(changes)
    return value


class Instrument:
    """Fresh-block fake; rejects reading a pending conversion prematurely."""
    identity = {"drain": {"address": 0x44, "die": 0x2261},
                "gate": {"address": 0x40, "die": 0x2281}}

    def __init__(self, record=True):
        self.now = 0
        self.events = [] if record else None
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.codes = [3000, 400, 16896, 320]
        self.pending = False
        self.ready_at = 0
        self.reads = self.completed = self.triggers = self.zeroes = 0
        self.first_trigger = None
        self.last_count = None
        self.never_ready = False
        self.sequence = False
        self.read_error = self.alert_error = self.trigger_error = None
        self.set_error = self.zero_error = None

    def event(self, *value):
        if self.events is not None:
            self.events.append(value)

    def zero(self):
        self.zeroes += 1
        self.event("zero")
        if self.zero_error:
            raise self.zero_error
        self.duties["gate"] = self.duties["drain"] = 0.0
        self.pending = False

    def set_duty(self, role, fraction):
        self.event("duty", role, fraction)
        if self.set_error == role:
            raise OSError(role + " PWM failed")
        self.duties[role] = fraction
        self.pending = False

    def poll_alerts(self):
        if self.alert_error:
            raise self.alert_error

    def begin_measurement(self, count):
        if self.trigger_error:
            raise self.trigger_error
        assert not self.pending, "A pending conversion must be read/cancelled"
        self.triggers += 1
        if self.first_trigger is None:
            self.first_trigger = self.now
        self.last_count = count
        self.pending = True
        self.ready_at = self.now + math.ceil(count * 2.2)

    def poll_measurement(self):
        assert self.pending, "No triggered conversion"
        return not self.never_ready and self.now >= self.ready_at

    def read_codes_into(self, values):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        fresh = self.pending
        if fresh:
            assert self.now >= self.ready_at, "Read before both conversions completed"
            self.pending = False
            self.completed += 1
        for index in range(4):
            values[index] = self.codes[index]
        if self.sequence:
            # Continuous frames intentionally differ from each fresh average.
            values[0] = 3000 + self.triggers if fresh else 1


class PhysicalInstrument(Instrument):
    read_codes_into = None

    def read_pair(self):
        codes = [0, 0, 0, 0]
        Instrument.read_codes_into(self, codes)
        return {"drain": {"bus_v": codes[0] * 0.00125,
                          "shunt_v": codes[1] * 0.0000025,
                          "bus_code": codes[0], "terminal_v": 999},
                "gate": {"bus_v": codes[2] * 0.0001953125,
                         "shunt_v": codes[3] * 0.0000003125,
                         "terminal_v": 999}}


class Baseline(current_baseline.Baseline):
    def __init__(self, config=None):
        super().__init__()
        self.lookups = []
        self.begin(config or settings(), Instrument.identity)
        for role, offset in ((0, 2500), (1, 100000)):
            self.add_knot(role, 0, offset, 0)
            self.add_knot(role, 19000000, offset, 65535)
        self.set_zero(0, 400, 0, True)
        self.set_zero(1, 9000, 0, True)
        self.finish()

    def offset_nv(self, role, bus_uv):
        if role != 0:
            raise AssertionError("Gate current must remain raw")
        self.lookups.append((role, bus_uv))
        return super().offset_nv(role, bus_uv)


class ManualDebugTests(unittest.TestCase):
    def tick(self, controller, instrument, now):
        instrument.now = now
        before = instrument.reads
        controller.tick(now)
        self.assertLessEqual(instrument.reads - before, 1)

    def sample(self, controller, instrument, revision=1, maximum=10000):
        for now in range(instrument.now, instrument.now + maximum):
            self.tick(controller, instrument, now)
            if controller.state == "error":
                self.fail(controller.error)
            if controller.sample_revision >= revision:
                return controller.latest
        self.fail("Manual sample did not complete")

    def test_constructor_and_target_edits_do_no_io_and_own_settings(self):
        hardware = Instrument()
        config = settings()
        controller = manual_debug.ManualDebug(hardware, config)
        self.assertEqual(hardware.events, [])
        self.assertEqual(controller.state, "idle")
        self.assertFalse(controller.outputs_enabled)
        self.assertIsNone(controller.latest)
        self.assertEqual(controller.targets_uv, [0, 0])
        self.assertEqual(controller.duty_u16, [0, 0])
        self.assertEqual(controller.applied_duty_u16, [0, 0])
        self.assertEqual(controller.sample_revision, 0)
        config["source_voltage_uv"] = 0
        self.assertEqual(controller.settings["source_voltage_uv"], 3300000)
        controller.set_targets(4300000, 0)
        self.assertEqual(controller.targets_uv, [4300000, 0])
        self.assertGreater(controller.duty_u16[0], 0)
        self.assertEqual(controller.duty_u16[1], 0)
        self.assertEqual(hardware.events, [])
        with self.assertRaises(ValueError):
            manual_debug.ManualDebug(hardware, settings(driver_supply_uv=2000000))
        self.assertEqual(hardware.events, [])

    def test_envelope_and_integer_validation_are_atomic(self):
        hardware = Instrument()
        controller = manual_debug.ManualDebug(hardware, settings())
        controller.set_targets(0, 19000000)
        # The upper bound is the full supply, without adding the 3.3 V source.
        self.assertEqual(controller.duty_u16[1], int(model.drive_duty("gate", 19.0) * 65535 + 0.5))
        original_targets = list(controller.targets_uv)
        original_preview = list(controller.duty_u16)
        for value in (-3300000, -1, 19000001, True, False, 1.0, "1", float("nan")):
            for role in range(2):
                pair = [0, 0]
                pair[role] = value
                with self.subTest(value=value, role=role):
                    with self.assertRaises(ValueError):
                        controller.set_targets(*pair)
                    self.assertEqual(controller.targets_uv, original_targets)
                    self.assertEqual(controller.duty_u16, original_preview)
        self.assertEqual(hardware.events, [])

    def test_off_monitoring_is_not_source_relative_zero_and_preview_persists(self):
        hardware = Instrument()
        controller = manual_debug.ManualDebug(hardware, settings())
        controller.set_targets(3300000, 3300000)
        preview = list(controller.duty_u16)
        self.assertGreater(preview[1], 0)
        for _ in range(2):
            controller.start()
            self.assertEqual(controller.state, "monitoring")
            self.assertFalse(controller.outputs_enabled)
            self.assertTrue(controller.shutdown_succeeded)
            self.assertEqual(hardware.duties, {"drain": 0.0, "gate": 0.0})
            self.assertEqual(controller.duty_u16, preview)
            self.assertEqual(controller.applied_duty_u16, [0, 0])
            controller.stop()
            controller.stop()
            self.assertEqual(controller.state, "stopped")
            self.assertTrue(controller.shutdown_succeeded)
        controller.start()
        self.sample(controller, hardware)
        self.assertFalse(controller.outputs_enabled)

    def test_apply_uses_exact_engine_pwm_math_and_zeroes_before_drain_then_gate(self):
        for use_baseline in (False, True):
            with self.subTest(baseline=use_baseline):
                drain_target, gate_target, source = 4300000, 2300000, 3300000
                # Manual inputs are to GND; only the scan uses source-relative
                # commands to reach these same absolute driver voltages.
                config = settings(drain_start_uv=drain_target - source,
                                  drain_stop_uv=drain_target - source,
                                  gate_start_uv=gate_target - source,
                                  gate_stop_uv=gate_target - source)
                table = Baseline(config) if use_baseline else None
                hardware = Instrument()
                controller = manual_debug.ManualDebug(hardware, config, table)
                controller.set_targets(drain_target, gate_target)
                controller.apply()
                self.assertEqual(hardware.events[0], ("zero",))
                self.assertEqual([event[1] for event in hardware.events[1:]], ["drain", "gate"])
                self.assertTrue(controller.outputs_enabled)
                self.assertFalse(controller.shutdown_succeeded)
                scan_hardware = Instrument()
                engine = Engine(scan_hardware, baseline=table)
                engine.start_scan(config)
                engine.tick(0)
                for index, role in enumerate(("drain", "gate")):
                    code = int(scan_hardware.duties[role] * 65535 + 0.5)
                    self.assertEqual(controller.duty_u16[index], code)
                    self.assertEqual(hardware.duties[role], code / 65535)
                    self.assertEqual(controller.applied_duty_u16[index], code)
                old_drive = dict(hardware.duties)
                old_applied = list(controller.applied_duty_u16)
                count = len(hardware.events)
                controller.set_targets(2000000, 2000000)
                self.assertEqual(hardware.duties, old_drive)
                self.assertEqual(controller.applied_duty_u16, old_applied)
                self.assertEqual(len(hardware.events), count)
                controller.apply()
                self.assertEqual(hardware.events[count][0], "zero")
                self.assertEqual([event[1] for event in hardware.events[count + 1:]], ["drain", "gate"])

    def test_applied_pwm_reads_hardware_quantization_once_per_apply(self):
        class PWM:
            def __init__(self):
                self.value = self.reads = 0

            def duty_u16(self, value=None):
                if value is None:
                    self.reads += 1
                    return self.value
                self.value = min(65535, ((value + 21) // 43) * 43)

        class QuantizedInstrument(Instrument):
            def __init__(self):
                super().__init__()
                self._pwms = {"drain": PWM(), "gate": PWM()}

            def set_duty(self, role, fraction):
                super().set_duty(role, fraction)
                self._pwms[role].duty_u16(int(fraction * 65535 + 0.5))

            def zero(self):
                super().zero()
                self._pwms["gate"].duty_u16(0)
                self._pwms["drain"].duty_u16(0)

        hardware = QuantizedInstrument()
        controller = manual_debug.ManualDebug(hardware, settings())
        applied_id = id(controller.applied_duty_u16)
        controller.set_targets(5000000, 5000000)
        controller.apply()
        for index, role in enumerate(("drain", "gate")):
            self.assertEqual(controller.applied_duty_u16[index], hardware._pwms[role].value)
            self.assertNotEqual(controller.applied_duty_u16[index], controller.duty_u16[index])
            self.assertEqual(hardware._pwms[role].reads, 1)
        self.sample(controller, hardware, 5)
        self.assertTrue(all(pwm.reads == 1 for pwm in hardware._pwms.values()))
        controller.stop()
        self.assertEqual(controller.applied_duty_u16, [0, 0])
        self.assertTrue(controller.shutdown_succeeded)
        controller.start()
        self.assertEqual(controller.applied_duty_u16, [0, 0])
        controller.apply()
        self.assertTrue(all(pwm.reads == 2 for pwm in hardware._pwms.values()))
        self.assertEqual(id(controller.applied_duty_u16), applied_id)

    def test_invalid_or_failed_pwm_readback_stops_both_outputs(self):
        class PWM:
            def __init__(self, result):
                self.result = result

            def duty_u16(self):
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        for result in (True, -1, 65536, 1.5, OSError("PWM read failed")):
            with self.subTest(result=result):
                hardware = Instrument()
                hardware._pwms = {"drain": PWM(4981), "gate": PWM(result)}
                controller = manual_debug.ManualDebug(hardware, settings())
                controller.set_targets(5000000, 5000000)
                controller.apply()
                self.assertEqual(controller.state, "error")
                self.assertFalse(controller.outputs_enabled)
                self.assertTrue(controller.shutdown_succeeded)
                self.assertEqual(hardware.duties, {"drain": 0, "gate": 0})
                self.assertEqual(controller.applied_duty_u16, [0, 0])

    def test_native_and_portable_reads_keep_raw_bus_voltages_and_signed_gate_current(self):
        for hardware_type in (Instrument, PhysicalInstrument):
            for sign in (-1, 1):
                with self.subTest(adapter=hardware_type.__name__, sign=sign):
                    hardware = hardware_type()
                    hardware.codes[1] *= sign
                    hardware.codes[3] *= sign
                    table = Baseline()
                    controller = manual_debug.ManualDebug(hardware, settings(), table)
                    controller.start()
                    row = self.sample(controller, hardware)
                    self.assertTrue(controller.correction_active)
                    self.assertEqual(table.lookups, [(0, 3750000)])
                    self.assertEqual(set(row), {"drain_bus_v", "gate_bus_v", "id_a", "ig_a"})
                    # The configured 3.3 V source and nonzero shunts of either
                    # sign must not alter either displayed bus voltage.
                    self.assertEqual(controller.settings["source_voltage_uv"], 3300000)
                    self.assertAlmostEqual(row["drain_bus_v"], 3.75)
                    self.assertAlmostEqual(row["gate_bus_v"], 3.3)
                    self.assertAlmostEqual(row["id_a"], (sign * 0.001 - 0.0000025) / 0.789)
                    self.assertAlmostEqual(row["ig_a"], sign * 0.0001 / 98.8)

    def test_incompatible_baseline_cannot_correct_or_anchor(self):
        for identity_mismatch in (False, True):
            hardware, table = Instrument(), Baseline()
            config = settings(source_voltage_uv=3200000)
            if identity_mismatch:
                config = settings()
                hardware.identity = {"drain": Instrument.identity["drain"],
                                     "gate": {"address": 0x40, "die": 0x2282}}
            controller = manual_debug.ManualDebug(hardware, config, table)
            controller.set_targets(4300000, 4300000)
            self.assertFalse(controller.correction_active)
            for index, role in enumerate(("drain", "gate")):
                expected = model.drive_duty(role, 4.3)
                self.assertEqual(controller.duty_u16[index], int(expected * 65535 + 0.5))
            controller.start()
            row = self.sample(controller, hardware)
            self.assertAlmostEqual(row["id_a"], 0.001 / model.DRAIN_SHUNT_OHM)
            self.assertAlmostEqual(row["drain_bus_v"], 3.75)
            self.assertAlmostEqual(row["gate_bus_v"], 3.3)
            self.assertEqual(table.lookups, [])

    def test_fresh_averages_after_settle_and_no_repeat_settle_for_unchanged_outputs(self):
        for count in (1, 4, 64, 1024):
            with self.subTest(count=count):
                hardware = Instrument()
                hardware.sequence = True
                controller = manual_debug.ManualDebug(hardware, settings(ina_average_count=count))
                controller.start()
                self.tick(controller, hardware, 0)
                self.tick(controller, hardware, 99)
                self.assertEqual(hardware.triggers, 0)
                self.assertIsNone(controller.latest)
                row = self.sample(controller, hardware)
                self.assertGreaterEqual(hardware.first_trigger, 100)
                self.assertEqual(hardware.last_count, count)
                self.assertEqual(hardware.reads, 2)  # Continuous check + fresh average.
                self.assertAlmostEqual(row["drain_bus_v"], 3001 * 0.00125)
                first_time = hardware.now
                same_row = self.sample(controller, hardware, 2)
                self.assertIs(row, same_row)
                self.assertEqual(hardware.reads, 3)
                self.assertEqual(hardware.completed, 2)
                self.assertAlmostEqual(row["drain_bus_v"], 3002 * 0.00125)
                self.assertLessEqual(hardware.now - first_time, math.ceil(count * 2.2) + 10)

    def test_target_misses_and_signed_readings_do_not_stop_monitoring(self):
        hardware = Instrument()
        hardware.codes = [2000, -6600, 100, -320]
        controller = manual_debug.ManualDebug(hardware, settings())
        controller.set_targets(10000000, 10000000)
        controller.apply()
        row = self.sample(controller, hardware)
        self.assertEqual(controller.state, "monitoring")
        self.assertTrue(controller.outputs_enabled)
        self.assertAlmostEqual(row["drain_bus_v"], 2.5)
        self.assertAlmostEqual(row["gate_bus_v"], 100 * 0.0001953125)
        self.assertLess(row["id_a"], -0.02)
        self.assertLess(row["ig_a"], 0)

    def test_stop_during_pending_conversion_cannot_publish_a_stale_sample(self):
        hardware = Instrument()
        controller = manual_debug.ManualDebug(hardware, settings(ina_average_count=1024))
        controller.set_targets(2000000, 1000000)
        controller.apply()
        self.tick(controller, hardware, 0)
        self.tick(controller, hardware, 100)
        self.assertTrue(hardware.pending)
        controller.stop()
        reads = hardware.reads
        self.tick(controller, hardware, 5000)
        self.assertEqual(hardware.reads, reads)
        self.assertEqual(controller.sample_revision, 0)
        self.assertFalse(controller.outputs_enabled)
        self.assertTrue(controller.shutdown_succeeded)
        controller.start()
        self.sample(controller, hardware)
        self.assertFalse(controller.outputs_enabled)
        self.assertEqual(controller.targets_uv, [2000000, 1000000])

    def test_endpoint_or_read_failure_keeps_last_good_sample_and_stops(self):
        hardware = Instrument()
        controller = manual_debug.ManualDebug(hardware, settings())
        controller.set_targets(5000000, 5000000)
        controller.apply()
        self.assertTrue(all(value > 0 for value in controller.applied_duty_u16))
        row = self.sample(controller, hardware)
        saved = dict(row)
        hardware.read_error = OverrangeError("gate", "shunt_saturation", 0x80000,
                                             -1, 0.16384, "V")
        for now in range(hardware.now, hardware.now + 100):
            self.tick(controller, hardware, now)
            if controller.state == "error":
                break
        self.assertEqual(controller.state, "error")
        self.assertIn("gate shunt ADC saturated", controller.error)
        self.assertEqual(controller.latest, saved)
        self.assertEqual(controller.sample_revision, 1)
        self.assertTrue(controller.shutdown_succeeded)
        self.assertFalse(controller.outputs_enabled)
        self.assertEqual(hardware.duties, {"drain": 0.0, "gate": 0.0})
        self.assertEqual(controller.applied_duty_u16, [0, 0])

    def test_status_trigger_and_second_pwm_failures_stop_outputs(self):
        for field in ("alert_error", "trigger_error", "set_error"):
            with self.subTest(failure=field):
                hardware = Instrument()
                controller = manual_debug.ManualDebug(hardware, settings())
                setattr(hardware, field, "gate" if field == "set_error" else OSError(field))
                controller.apply()
                if field != "set_error":
                    self.tick(controller, hardware, 0)
                    self.tick(controller, hardware, 100)
                self.assertEqual(controller.state, "error")
                self.assertTrue(controller.shutdown_succeeded)
                self.assertFalse(controller.outputs_enabled)
                self.assertEqual(hardware.duties, {"drain": 0.0, "gate": 0.0})

    def test_failed_shutdown_is_never_reported_successful(self):
        hardware = Instrument()
        controller = manual_debug.ManualDebug(hardware, settings())
        controller.set_targets(5000000, 5000000)
        controller.apply()
        before = list(controller.applied_duty_u16)
        self.assertTrue(all(value > 0 for value in before))
        hardware.zero_error = OSError("zero failed")
        controller.stop()
        self.assertEqual(controller.state, "error")
        self.assertFalse(controller.shutdown_succeeded)
        self.assertIn("zero failed", controller.error)
        self.assertEqual(controller.applied_duty_u16, before)
        hardware.alert_error = OSError("status failed")
        controller.start()
        self.assertFalse(controller.shutdown_succeeded)
        self.assertEqual(controller.state, "error")
        self.assertIn("could not stop outputs", controller.error)
        self.assertEqual(controller.applied_duty_u16, before)

    def test_conversion_timeout_and_delayed_ready_tick(self):
        for never_ready in (False, True):
            hardware = Instrument()
            hardware.never_ready = never_ready
            controller = manual_debug.ManualDebug(hardware, settings(ina_average_count=64))
            controller.apply()
            self.tick(controller, hardware, 0)
            self.tick(controller, hardware, 100)
            self.tick(controller, hardware, 1000)
            if never_ready:
                self.assertEqual(controller.state, "error")
                self.assertIn("timed out", controller.error)
                self.assertTrue(controller.shutdown_succeeded)
                self.assertIsNone(controller.latest)
            else:
                self.assertEqual(controller.state, "monitoring")
                self.assertEqual(controller.sample_revision, 1)

    def test_wraparound_clock_and_backward_clock(self):
        hardware = Instrument()
        controller = manual_debug.ManualDebug(hardware, settings(ina_average_count=1))
        controller.start()
        with patch.object(manual_debug, "_ticks_diff", lambda now, before: ((now - before + 512) % 1024) - 512):
            for elapsed in range(1500):
                hardware.now = elapsed
                controller.tick((1000 + elapsed) % 1024)
        self.assertEqual(controller.state, "monitoring", controller.error)
        self.assertGreater(controller.sample_revision, 50)
        controller.start()
        self.tick(controller, hardware, 100)
        self.tick(controller, hardware, 99)
        self.assertEqual(controller.state, "error")
        self.assertIn("backwards", controller.error)
        self.assertTrue(controller.shutdown_succeeded)

    def test_settling_begins_after_hardware_write_time(self):
        class SlowWrites(Instrument):
            def set_duty(self, role, fraction):
                super().set_duty(role, fraction)
                self.now += 20

        hardware = SlowWrites()
        controller = manual_debug.ManualDebug(hardware, settings())
        with patch.object(manual_debug, "_ticks_ms", lambda: hardware.now):
            controller.apply()
            applied = hardware.now
            for _ in range(101):
                controller.tick(hardware.now)
                hardware.now += 1
        self.assertEqual(applied, 40)
        self.assertGreaterEqual(hardware.first_trigger - applied, 100)

    def test_long_monitor_reuses_all_measurement_containers(self):
        hardware = Instrument(record=False)
        controller = manual_debug.ManualDebug(hardware, settings(ina_average_count=1))
        controller.start()
        first = self.sample(controller, hardware, 10)
        identities = tuple(id(item) for item in (controller.targets_uv, controller.duty_u16,
                                                 controller.applied_duty_u16,
                                                 controller._codes, controller._values))
        keys = set(controller.__dict__)
        tracemalloc.start()
        try:
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(15000):
                self.tick(controller, hardware, hardware.now + 5)
            gc.collect()
            growth = tracemalloc.get_traced_memory()[0] - before
        finally:
            tracemalloc.stop()
        self.assertGreater(controller.sample_revision, 5000)
        self.assertIs(controller.latest, first)
        self.assertEqual(identities, tuple(id(item) for item in (
            controller.targets_uv, controller.duty_u16, controller.applied_duty_u16,
            controller._codes, controller._values)))
        self.assertEqual(set(controller.__dict__), keys)
        self.assertLess(growth, 4096)


class NativeHardwareTests(unittest.TestCase):
    def test_real_driver_quantization_and_both_signed_endpoints_stop_gate_first(self):
        for address, endpoint in ((0x44, 0x8000), (0x40, 0x80000)):
            for raw in (endpoint - 1, -endpoint):
                with self.subTest(address=address, raw=raw):
                    fake = MachineFake()
                    spec = importlib.util.spec_from_file_location("firmware.hardware",
                        Path(__file__).resolve().parents[1] / "firmware/hardware.py")
                    hardware = importlib.util.module_from_spec(spec)
                    with patch.dict(sys.modules, {"machine": fake.module}):
                        spec.loader.exec_module(hardware)
                    instrument = hardware.Instrument()
                    try:
                        controller = manual_debug.ManualDebug(instrument, settings())
                        controller.set_targets(1000000, 1000000)
                        controller.apply()
                        self.assertEqual(fake.pwms[20].duty, controller.duty_u16[0])
                        self.assertEqual(fake.pwms[21].duty, controller.duty_u16[1])
                        # This portable PWM fake exposes only a setter; the
                        # controller falls back to the requested duty codes.
                        self.assertEqual(controller.applied_duty_u16, controller.duty_u16)
                        fake.set_codes(address, bus=3000, shunt=raw)
                        controller.tick(0)
                        controller.tick(100)
                        self.assertEqual(controller.state, "error")
                        self.assertIn("shunt ADC saturated", controller.error)
                        self.assertTrue(controller.shutdown_succeeded)
                        self.assertEqual(fake.pwms[20].duty, 0)
                        self.assertEqual(fake.pwms[21].duty, 0)
                        duties = [event[1:] for event in fake.events if event[0] == "duty"]
                        self.assertEqual(duties[-2:], [(21, 0), (20, 0)])
                    finally:
                        instrument.close()


if __name__ == "__main__":
    unittest.main()
