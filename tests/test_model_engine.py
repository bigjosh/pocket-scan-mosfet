"""Host checks for scan timing, electrical math, aborts and committed settings."""

import math
import os
import tempfile
import unittest
from unittest.mock import patch

from firmware import model, storage
from firmware.engine import Engine, OverrangeError


def settings(**changes):
    value = dict(model.DEFAULT_SETTINGS)
    value.update({"drain_start_uv": 1000000, "drain_stop_uv": 1000000,
                  "gate_start_uv": 1000000, "gate_stop_uv": 1000000,
                  "ina_average_count": 4})
    if any(key in changes for key in ("batches", "samples_per_batch", "sample_pause_ms")):
        total = changes.pop("batches", 2) * changes.pop("samples_per_batch", 2)
        changes.pop("sample_pause_ms", None)
        changes["ina_average_count"] = next((n for n in model.INA_AVERAGE_COUNTS if n >= total), 1024)
    for key, item in changes.items():
        # Keep physical-unit test calls readable, but exercise canonical settings.
        if key.endswith("_v") and key[:-2] in model.VOLTAGE_FIELDS:
            key = key[:-2] + "_uv"
            if isinstance(item, (int, float)) and math.isfinite(item):
                item = model.volts_to_uv(item)
        value[key] = item
    return value


def baseline(role, voltage):
    return (0.000010 if role == "drain" else 0.000300) * voltage


def calibration(maximum=3.0):
    return {
        "version": 1,
        "metadata": {"driver_supply_v": 5.0, "pwm_hz": model.PWM_HZ,
                     "acquisition_mode": model.ACQUISITION_MODE, "ina_average_count": 4,
                     "bus_conversion_us": 1100, "shunt_conversion_us": 1100,
                     "calibration_repeat_blocks": model.CALIBRATION_REPEAT_BLOCKS},
        "channels": {
            role: {"shunt_ohm": resistance,
                   "points": [{"terminal_v": voltage,
                               "shunt_v": baseline(role, voltage),
                               "duty": voltage / model.PWM_FULL_SCALE_V}
                              for voltage in (0.0, maximum / 2, maximum)]}
            for role, resistance in (("drain", model.DRAIN_SHUNT_OHM),
                                     ("gate", model.GATE_SHUNT_OHM))
        },
    }


def calibration_with_measured_zero_floor():
    value = calibration()
    value["channels"]["drain"]["points"][0].update({
        "terminal_v": 0.006435747, "shunt_v": -8.244999e-6})
    return value


def discovered_calibration(maximum=3.5):
    value = calibration(maximum)
    value["version"] = 2
    for channel in value["channels"].values():
        channel["capability"] = {"complete": True,
                                 "max_duty": model.calibration_duty_limit(settings()),
                                 "limit_reason": "plateau"}
    return value


class FakeInstrument:
    identity = {"drain": {"address": 0x44}, "gate": {"address": 0x41}}

    def __init__(self, drain_current=0.0, gate_current=0.0):
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.current = {"drain": drain_current, "gate": gate_current}
        self.commands = []
        self.timed_commands = []
        self.reads = 0
        self.zeroes = 0
        self.read_error = None
        self.voltage_offset = 0.0
        self.voltage_gain = {"drain": 1.0, "gate": 1.0}
        self.voltage_ceiling = {"drain": float("inf"), "gate": float("inf")}
        self.read_times = []
        self.now = 0
        self.triggers = []
        self.ready_at = None

    def set_duty(self, role, fraction):
        self.commands.append((role, fraction))
        self.timed_commands.append((self.now, role, fraction))
        self.duties[role] = fraction

    def zero(self):
        self.zeroes += 1
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.ready_at = None

    def poll_alerts(self):
        pass

    def begin_measurement(self, count):
        self.triggers.append((self.now, count))
        self.ready_at = self.now + math.ceil(count * 2.2)

    def poll_measurement(self):
        return self.now >= self.ready_at

    def read_pair(self):
        self.reads += 1
        self.read_times.append(self.now)
        if self.read_error:
            raise self.read_error
        result = {}
        for role, resistance in (("drain", model.DRAIN_SHUNT_OHM),
                                 ("gate", model.GATE_SHUNT_OHM)):
            terminal = (self.duties[role] * model.PWM_FULL_SCALE_V *
                        self.voltage_gain[role] + self.voltage_offset)
            terminal = min(terminal, self.voltage_ceiling[role])
            shunt = baseline(role, terminal) + resistance * self.current[role]
            result[role] = {"terminal_v": terminal, "shunt_v": shunt,
                            "bus_v": terminal + shunt}
        return result


class LowGroundReading(FakeInstrument):
    """Saved zero baseline floor was 6.44 mV; a later sample reads 1.3 mV."""

    def read_pair(self):
        result = super().read_pair()
        result["drain"] = {"terminal_v": 0.0013, "shunt_v": -7.5e-6,
                           "bus_v": 0.0013 - 7.5e-6}
        self.last_pair = result
        return result


def run_engine(engine, hardware, maximum_ms=100000):
    events = []
    for now in range(maximum_ms):
        hardware.now = now
        before = hardware.reads
        events.extend(engine.tick(now))
        assert hardware.reads - before <= 1
        if engine.state not in ("scanning", "calibrating"):
            return events
    raise AssertionError("Engine did not terminate")


class ModelTests(unittest.TestCase):
    def test_exact_steps_and_nondivisible_stop(self):
        self.assertEqual(list(model.sweep_values(0, 1, 0.3)), [0, 0.3, 0.6, 0.9])
        self.assertEqual(len(model.sweep_values(-1, 1, 0.1)), 21)
        self.assertAlmostEqual(model.sweep_values(-1, 1, 0.1)[-1], 1)

    def test_lazy_sweep_indexing_and_maximum_count(self):
        values = model.sweep_values(-1, 1, 0.5)
        self.assertEqual(len(values), 5)
        self.assertEqual(values[0], -1)
        self.assertEqual(values[-1], 1)
        self.assertEqual(values[-5], -1)
        self.assertEqual(list(values), [-1, -0.5, 0, 0.5, 1])
        for index in (-6, 5):
            with self.assertRaises(IndexError):
                values[index]
        with self.assertRaises(TypeError):
            values[1.0]
        maximum = model.sweep_values(0, 4095, 1)
        self.assertNotIsInstance(maximum, list)
        self.assertEqual(len(maximum), model.MAX_POINTS)
        self.assertEqual(maximum[-1], 4095)
        with self.assertRaises(ValueError):
            model.sweep_values(0, 4096, 1)

    def test_envelope_signed_voltage_and_limits(self):
        value = model.validate_settings(settings(source_voltage_v=1.0,
            gate_start_v=-1.0, gate_stop_v=1.0))
        self.assertEqual(value["gate_start_uv"], -1000000)
        self.assertEqual(model.voltage_limit(value), 5)
        for invalid in (settings(gate_start_v=-0.1), settings(gate_stop_v=5.1),
                        settings(drain_step_v=0), settings(gate_start_v=float("nan")),
                        settings(drain_start_v=0, drain_stop_v=2, drain_step_v=0.001,
                                 gate_start_v=0, gate_stop_v=2, gate_step_v=0.001),
                        settings(ina_average_count=True), settings(ina_average_count=3)):
            with self.assertRaises(ValueError):
                model.validate_settings(invalid)

    def test_baseline_uses_absolute_voltage_and_does_not_extrapolate(self):
        channel = calibration()["channels"]["gate"]
        shunt = baseline("gate", 2.0) + model.GATE_SHUNT_OHM * 2e-6
        self.assertAlmostEqual(model.corrected_current(channel, 2.0, shunt), 2e-6)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.corrected_current(channel, 3.1, shunt)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.duty_for_voltage(channel, 3.1)

    def test_real_zero_floor_regression_requires_explicit_zero_drive(self):
        channel = calibration_with_measured_zero_floor()["channels"]["drain"]
        before = dict(channel["points"][0])
        expected = (-7.5e-6 - before["shunt_v"]) / channel["shunt_ohm"]
        with self.assertRaisesRegex(ValueError, "below calibration coverage"):
            model.corrected_current(channel, 0.0013, -7.5e-6)
        self.assertAlmostEqual(model.corrected_current(channel, 0.0013, -7.5e-6,
                                                       zero_drive=True), expected)
        self.assertNotEqual(expected, 0.0)
        self.assertEqual(channel["points"][0], before)

    def test_zero_drive_fallback_bounds_and_other_coverage_are_unchanged(self):
        channel = calibration_with_measured_zero_floor()["channels"]["drain"]
        expected = (-7.5e-6 - channel["points"][0]["shunt_v"]) / channel["shunt_ohm"]
        for voltage in (-0.005, 0.0, 0.0013, 0.006435747):
            self.assertAlmostEqual(model.corrected_current(channel, voltage, -7.5e-6,
                                                           zero_drive=True), expected)
        for voltage in (-0.0051, 3.0051):
            with self.assertRaisesRegex(ValueError, "calibration coverage"):
                model.corrected_current(channel, voltage, -7.5e-6, zero_drive=True)
        # A nonzero-duty endpoint or a floor above 20 mV does not qualify.
        for field, value in (("duty", 0.0001), ("terminal_v", 0.020001)):
            invalid = calibration_with_measured_zero_floor()["channels"]["drain"]
            invalid["points"][0][field] = value
            with self.assertRaisesRegex(ValueError, "below calibration coverage"):
                model.corrected_current(invalid, 0.0013, -7.5e-6, zero_drive=True)
        boundary = calibration_with_measured_zero_floor()["channels"]["drain"]
        boundary["points"][0]["terminal_v"] = 0.020
        self.assertAlmostEqual(model.corrected_current(boundary, 0.0013, -7.5e-6,
                                                       zero_drive=True), expected)
        with self.assertRaises(ValueError):
            model.corrected_current(channel, float("nan"), 0, zero_drive=True)

    def test_drive_endpoint_tolerance_does_not_widen_baseline_coverage(self):
        channel = calibration(maximum=2.985)["channels"]["gate"]
        endpoint = channel["points"][-1]
        self.assertEqual(model.duty_for_voltage(channel, 3.0), endpoint["duty"])
        self.assertEqual(model.duty_for_voltage(channel, 3.005), endpoint["duty"])
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.duty_for_voltage(channel, 3.0051)
        shunt = endpoint["shunt_v"] + model.GATE_SHUNT_OHM * 2e-6
        self.assertAlmostEqual(model.corrected_current(channel, 2.989, shunt), 2e-6)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.corrected_current(channel, 2.9901, shunt)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.corrected_current(channel, 3.0, shunt)
        low_channel = calibration()["channels"]["gate"]
        low_channel["points"][0]["terminal_v"] = 0.015
        self.assertEqual(model.duty_for_voltage(low_channel, 0.001), 0.0)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.corrected_current(low_channel, 0.001, 0.0)

    def test_gate_plot_tolerance_does_not_widen_legacy_calibration_tolerances(self):
        self.assertEqual(model.GATE_PLOT_TOLERANCE_V, 0.100)
        self.assertEqual(model.VOLTAGE_TOLERANCE_V, 0.020)
        self.assertEqual(model.CALIBRATION_EDGE_TOLERANCE_V, 0.005)
        channel = calibration()["channels"]["gate"]
        endpoint = channel["points"][-1]
        self.assertEqual(model.duty_for_voltage(channel, 3.020), endpoint["duty"])
        for voltage in (3.020001, 3.050, 3.100):
            with self.subTest(voltage=voltage), self.assertRaisesRegex(ValueError, "coverage"):
                model.duty_for_voltage(channel, voltage)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.corrected_current(channel, 3.005001, endpoint["shunt_v"])

    def test_calibration_rejects_nonmonotonic_or_wrong_rail(self):
        value = calibration()
        value["channels"]["gate"]["points"][1]["terminal_v"] = 0.0
        with self.assertRaises(ValueError):
            model.validate_calibration(value, settings())
        with self.assertRaises(ValueError):
            model.validate_calibration(calibration(), settings(driver_supply_v=6.0))
        value = calibration()
        value["channels"]["gate"]["points"][0] = None
        with self.assertRaises(ValueError):
            model.validate_calibration(value)

    def test_calibration_grid_refines_nonlinear_region(self):
        values = model.calibration_targets(settings())
        self.assertEqual(values[0], 0.0)
        self.assertEqual(values[-1], 5.0)
        self.assertTrue(any(abs(v - 1.05) < 1e-9 for v in values))
        self.assertFalse(any(abs(v - 0.35) < 1e-9 for v in values))

    def test_discovered_capability_allows_targets_but_not_unreachable_source(self):
        value = settings(drain_start_v=5, drain_stop_v=5)
        model.validate_scan_calibration(discovered_calibration(), value)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.validate_scan_calibration(calibration(), value)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.validate_scan_calibration(discovered_calibration(),
                settings(source_voltage_v=4, drain_start_v=0, drain_stop_v=0,
                         gate_start_v=0, gate_stop_v=0))
        channel = discovered_calibration()["channels"]["drain"]
        with self.assertRaises(ValueError):
            model.duty_for_voltage(channel, float("inf"), allow_limited=True)

    def test_capability_metadata_cannot_expand_legacy_or_unprobed_duty(self):
        for mutation in ("legacy", "incomplete", "unsafe_duty", "below_endpoint", "above_rail"):
            value = discovered_calibration()
            channel = value["channels"]["drain"]
            if mutation == "legacy":
                value["version"] = 1
            elif mutation == "incomplete":
                channel["capability"]["complete"] = False
            elif mutation == "unsafe_duty":
                channel["capability"]["max_duty"] = 0.5
            elif mutation == "below_endpoint":
                channel["capability"]["max_duty"] = 0.01
            else:
                channel["points"][-1]["terminal_v"] = 5.1
            with self.assertRaises(ValueError, msg=mutation):
                model.validate_calibration(value, settings())

    def test_endpoint_noise_support_is_measured_and_bounded(self):
        value = discovered_calibration()
        channel = value["channels"]["gate"]
        channel["capability"].update(endpoint_min_v=3.489, endpoint_max_v=3.512)
        model.validate_calibration(value, settings())
        raw = channel["points"][-1]["shunt_v"] + model.GATE_SHUNT_OHM * 1e-6
        self.assertAlmostEqual(model.corrected_current(channel, 3.510, raw), 1e-6)
        self.assertAlmostEqual(model.corrected_current(channel, 3.516, raw), 1e-6)
        with self.assertRaisesRegex(ValueError, "coverage"):
            model.corrected_current(channel, 3.518, raw)
        channel["capability"]["endpoint_max_v"] = 3.6
        with self.assertRaises(ValueError):
            model.validate_calibration(value, settings())


class EngineTests(unittest.TestCase):
    def test_quiescent_ticks_reuse_the_empty_pending_queue(self):
        instrument = FakeInstrument()
        engine = Engine(instrument)
        pending = engine._pending
        for state in ("idle", "complete", "partial", "cancelled", "error"):
            engine.state = state
            for now in range(5):
                self.assertIs(pending, engine.tick(now))
                self.assertIs(pending, engine._pending)
                self.assertEqual([], pending)
        self.assertEqual([], instrument.commands)
        self.assertEqual(0, instrument.reads)

    def test_pending_terminal_events_detach_once_then_idle_queue_stays_stable(self):
        engine = Engine(FakeInstrument())
        engine.start_scan(settings(), calibration())
        engine.cancel()
        queued = engine._pending
        self.assertEqual([("cancelled", {})], queued)
        delivered = engine.tick(1)
        self.assertIs(queued, delivered)
        self.assertEqual([("cancelled", {})], delivered)
        self.assertIsNot(delivered, engine._pending)
        empty = engine._pending
        for now in range(2, 10):
            self.assertIs(empty, engine.tick(now))
            self.assertEqual([], engine.tick(now))
        self.assertEqual([("cancelled", {})], delivered)

    def test_discovery_finds_independent_plateaus_with_monotonic_maps(self):
        hardware = FakeInstrument()
        hardware.voltage_gain = {"drain": 1.03, "gate": 0.998}
        hardware.voltage_ceiling = {"drain": 3.25, "gate": 3.7}
        engine = Engine(hardware)
        engine.start_calibration(settings())
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertEqual(engine.progress, 1)
        self.assertEqual(engine.calibration["version"], 2)
        model.validate_calibration(engine.calibration, settings())
        for role in ("drain", "gate"):
            channel = engine.calibration["channels"][role]
            self.assertTrue(channel["capability"]["complete"])
            self.assertEqual(channel["capability"]["limit_reason"], "plateau")
            self.assertAlmostEqual(channel["points"][-1]["terminal_v"], hardware.voltage_ceiling[role])
            self.assertAlmostEqual(channel["points"][-1]["duty"], channel["capability"]["max_duty"], delta=1.2e-7)
            self.assertTrue(all(b["terminal_v"] > a["terminal_v"] and b["duty"] > a["duty"]
                                for a, b in zip(channel["points"], list(channel["points"])[1:])))
        last_drain = max(t for t, role, _ in hardware.timed_commands if role == "drain")
        last_gate = max(t for t, role, _ in hardware.timed_commands if role == "gate")
        self.assertLess(last_drain, last_gate)
        self.assertTrue(all(duty <= model.calibration_duty_limit(settings())
                            for _, duty in hardware.commands))

    def test_discovered_plateau_does_not_distort_nearby_reachable_targets(self):
        hardware = FakeInstrument()
        hardware.voltage_ceiling = {"drain": 3.5, "gate": 3.5}
        discovery = Engine(hardware)
        discovery.start_calibration(settings())
        run_engine(discovery, hardware)
        self.assertEqual(discovery.state, "complete", discovery.error)
        for channel in discovery.calibration["channels"].values():
            endpoint = channel["points"][-1]
            self.assertLess(endpoint["drive_duty"], endpoint["duty"])
        for target in (3.47, 3.48):
            engine = Engine(hardware)
            engine.start_scan(settings(drain_start_v=target, drain_stop_v=target), discovery.calibration)
            run_engine(engine, hardware)
            self.assertEqual(engine.state, "complete", engine.error)
            self.assertAlmostEqual(engine.latest["vds"], target, delta=model.VOLTAGE_TOLERANCE_V)
            self.assertFalse(engine.latest["drain_voltage_limited"])

    def test_noisy_hardware_blocks_retain_observed_plateau_support(self):
        class NoisyPlateau(FakeInstrument):
            def read_pair(self):
                pair = super().read_pair()
                for role in ("drain", "gate"):
                    reading = pair[role]
                    if reading["terminal_v"] >= self.voltage_ceiling[role]:
                        change = 0.001 if self.reads % 2 else -0.001
                        reading["terminal_v"] += change
                        reading["bus_v"] += change
                return pair

        hardware = NoisyPlateau()
        hardware.voltage_ceiling = {"drain": 3.5, "gate": 3.5}
        engine = Engine(hardware)
        engine.start_calibration(settings())
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        for channel in engine.calibration["channels"].values():
            self.assertAlmostEqual(channel["capability"]["endpoint_min_v"], 3.499)
            self.assertAlmostEqual(channel["capability"]["endpoint_max_v"], 3.501)
        scan = Engine(hardware)
        scan.start_scan(settings(drain_start_v=5, drain_stop_v=5,
                                 gate_start_v=5, gate_stop_v=5), engine.calibration)
        run_engine(scan, hardware)
        self.assertEqual(scan.state, "complete", scan.error)
        self.assertAlmostEqual(scan.latest["vds"], 3.5, delta=0.00101)
        self.assertTrue(scan.latest["drain_voltage_limited"])

    def test_completed_calibration_channel_cannot_drift_while_other_finishes(self):
        class ChangesHeldChannel(FakeInstrument):
            def read_pair(self):
                if engine._cal_done["drain"]:
                    self.voltage_ceiling["drain"] = 3.1
                return super().read_pair()

        hardware = ChangesHeldChannel()
        hardware.voltage_ceiling = {"drain": 3.25, "gate": 3.7}
        engine = Engine(hardware)
        engine.start_calibration(settings())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        self.assertIn("completed calibration output changed", engine.error)
        self.assertFalse(any(kind == "calibrated" for kind, _ in events))

    def test_limited_points_keep_requested_actual_values_and_channel_flags(self):
        hardware = FakeInstrument(drain_current=100e-6, gate_current=1e-6)
        hardware.voltage_ceiling = {"drain": 3.5, "gate": 3.5}
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=0, drain_stop_v=5, drain_step_v=2.5,
                                   gate_start_v=0, gate_stop_v=5, gate_step_v=2.5),
                          discovered_calibration())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        points = [data for kind, data in events if kind == "point"]
        self.assertEqual(len(points), 9)
        self.assertEqual(sum(point["voltage_limited"] for point in points), 5)
        for point in points:
            self.assertAlmostEqual(point["vds"], min(point["requested_vds"], 3.5))
            self.assertAlmostEqual(point["vgs"], min(point["requested_vgs"], 3.5))
            self.assertEqual(point["drain_voltage_limited"], point["requested_vds"] == 5)
            self.assertEqual(point["gate_voltage_limited"], point["requested_vgs"] == 5)
            self.assertAlmostEqual(point["id_a"], 100e-6)
            self.assertAlmostEqual(point["ig_a"], 1e-6)

    def test_limited_scan_does_not_relax_current_or_baseline_checks(self):
        for maximum, current, reason in ((3.5, 0.006, "current limit"),
                                          (3.51, 0.0, "calibration coverage")):
            hardware = FakeInstrument(drain_current=current)
            hardware.voltage_ceiling = {"drain": maximum, "gate": 3.5}
            engine = Engine(hardware)
            engine.start_scan(settings(drain_start_v=5, drain_stop_v=5), discovered_calibration())
            events = run_engine(engine, hardware)
            self.assertEqual(engine.state, "partial" if current else "error")
            self.assertIn(reason, engine.error)
            self.assertFalse(any(kind == "point" for kind, _ in events))
            self.assertEqual(hardware.zeroes, 1)

    def test_gate_limiting_never_makes_source_parking_optional(self):
        class GateChangesDrainCapability(FakeInstrument):
            def read_pair(self):
                self.voltage_ceiling["drain"] = 0.5 if self.duties["gate"] > 0.1 else 3.5
                self.voltage_ceiling["gate"] = 3.5
                return super().read_pair()

        hardware = GateChangesDrainCapability()
        engine = Engine(hardware)
        engine.start_scan(settings(source_voltage_v=1, drain_start_v=0, drain_stop_v=0,
                                   gate_start_v=4, gate_stop_v=4), discovered_calibration())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        self.assertIn("drain", engine.error)
        self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_limited_output_drop_during_averaging_is_a_fault(self):
        class ChangesWhileSampling(FakeInstrument):
            def read_pair(self):
                self.voltage_ceiling["drain"] = 3.0 if len(self.triggers) == 3 else 3.5
                return super().read_pair()

        hardware = ChangesWhileSampling()
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=5, drain_stop_v=5), discovered_calibration())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        self.assertIn("averaging", engine.error)
        self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_average_target_miss_after_settling_keeps_actual_samples(self):
        class SmallStableChange(FakeInstrument):
            def read_pair(self):
                self.voltage_offset = change if len(self.triggers) == 3 else 0.0
                return super().read_pair()

        for change in (-0.03, 0.03):
            with self.subTest(change=change):
                hardware = SmallStableChange()
                engine = Engine(hardware)
                engine.start_scan(settings(), calibration())
                events = run_engine(engine, hardware)
                self.assertEqual(engine.state, "complete", engine.error)
                points = [row for kind, row in events if kind == "point"]
                self.assertEqual(len(points), 1)
                self.assertEqual(points[0]["acquisition_blocks_completed"], 1)
                for measured, requested in (("vds", "requested_vds"), ("vgs", "requested_vgs")):
                    self.assertAlmostEqual(points[0][measured], 1.0 + change)
                    self.assertEqual(points[0][requested], 1.0)
                self.assertFalse(points[0]["voltage_limited"])
                self.assertEqual(len(hardware.commands), 3)

    def test_known_ceiling_flag_uses_averaged_undershoot_only(self):
        class LimitedThenWithinTolerance(FakeInstrument):
            def read_pair(self):
                self.voltage_ceiling["drain"] = 3.495 if len(self.triggers) == 3 else 3.479
                return super().read_pair()

        hardware = LimitedThenWithinTolerance()
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=3.505, drain_stop_v=3.505), discovered_calibration())
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertFalse(engine.latest["drain_voltage_limited"])
        self.assertFalse(engine.latest["voltage_limited"])

        # An overshoot at the same drive ceiling remains a valid measurement
        # when it lies inside measured baseline support. It is not a limit.
        hardware = FakeInstrument()
        hardware.voltage_ceiling["drain"] = 3.53
        saved = discovered_calibration()
        saved["channels"]["drain"]["capability"].update(endpoint_min_v=3.5, endpoint_max_v=3.535)
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=3.505, drain_stop_v=3.505), saved)
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertAlmostEqual(engine.latest["vds"], 3.53)
        self.assertFalse(engine.latest["drain_voltage_limited"])

    def test_stable_scan_misses_do_not_trigger_drive_corrections_or_extra_windows(self):
        for gain in (0.7, 1.3):
            with self.subTest(gain=gain):
                hardware = FakeInstrument(drain_current=100e-6, gate_current=2e-6)
                hardware.voltage_gain = {"drain": gain, "gate": gain}
                engine = Engine(hardware)
                engine.start_scan(settings(), calibration())
                events = run_engine(engine, hardware)
                self.assertEqual(engine.state, "complete", engine.error)
                self.assertEqual([kind for kind, _ in events].count("point"), 1)
                self.assertEqual(hardware.commands,
                                 [("drain", 0.0), ("gate", 1.0 / model.PWM_FULL_SCALE_V),
                                  ("drain", 1.0 / model.PWM_FULL_SCALE_V)])
                self.assertEqual(hardware.reads, 4)
                self.assertAlmostEqual(engine.latest["vds"], gain)
                self.assertAlmostEqual(engine.latest["vgs"], gain)
                self.assertAlmostEqual(engine.latest["id_a"], 100e-6)
                self.assertAlmostEqual(engine.latest["ig_a"], 2e-6)
                self.assertFalse(engine.latest["voltage_limited"])

    def test_scan_stability_is_relative_to_measured_settle_not_target(self):
        for role in ("drain", "gate"):
            class LateMotion(FakeInstrument):
                def read_pair(self):
                    # Stable but 300 mV off target is allowed. A later 50 mV
                    # jump, even toward target, fails the same 40 mV span.
                    self.voltage_gain[role] = 0.75 if len(self.triggers) == 3 else 0.7
                    return super().read_pair()

            with self.subTest(role=role):
                hardware = LateMotion()
                engine = Engine(hardware)
                engine.start_scan(settings(), calibration())
                events = run_engine(engine, hardware)
                self.assertEqual(engine.state, "error")
                self.assertEqual(engine.error, role + " output changed during averaging")
                self.assertFalse(any(kind == "point" for kind, _ in events))
                self.assertTrue(engine.shutdown_succeeded)

    def test_initial_drain_park_failure_prevents_gate_drive(self):
        hardware = FakeInstrument()
        hardware.voltage_offset = 0.03
        engine = Engine(hardware)
        engine.start_scan(settings(), calibration())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        self.assertIn("drain correction exceeds calibrated drive range", engine.error)
        self.assertEqual(hardware.commands, [("drain", 0.0)])
        self.assertFalse(any(kind == "point" for kind, _ in events))
        self.assertTrue(engine.shutdown_succeeded)

    def test_zero_drive_scan_preserves_actual_voltage_and_raw_shunt(self):
        hardware = LowGroundReading()
        engine = Engine(hardware)
        saved = calibration_with_measured_zero_floor()
        engine.start_scan(settings(drain_start_v=0.0, drain_stop_v=0.0), saved)
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        row = [data for kind, data in events if kind == "point"][0]
        self.assertAlmostEqual(row["vds"], 0.0013)
        self.assertAlmostEqual(row["drain_shunt_v"], -7.5e-6)
        self.assertAlmostEqual(row["id_a"], (-7.5e-6 + 8.244999e-6) / model.DRAIN_SHUNT_OHM)
        self.assertAlmostEqual(hardware.last_pair["drain"]["terminal_v"], 0.0013)
        self.assertEqual(hardware.last_pair["drain"]["shunt_v"], -7.5e-6)

    def test_nonzero_target_cannot_opt_in_even_when_duty_clamps_to_zero(self):
        hardware = LowGroundReading()
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=0.001, drain_stop_v=0.001),
                          calibration_with_measured_zero_floor())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        self.assertIn("drain: Voltage 0.0013 V is below calibration coverage", engine.error)
        self.assertTrue(all(duty == 0.0 for role, duty in hardware.commands if role == "drain"))
        self.assertFalse(any(kind == "point" for kind, _ in events))
        self.assertEqual(hardware.zeroes, 1)

    def test_zero_target_outside_zero_floor_support_still_fails_after_strict_park(self):
        class NeedsPositiveDrive(LowGroundReading):
            def read_pair(self):
                pair = super().read_pair()
                if self.duties["drain"] == 0.0:
                    # A shunt drop can put the terminal below the bus sense.
                    pair["drain"] = {"bus_v": 0.0, "shunt_v": 0.025,
                                     "terminal_v": -0.025}
                return pair

        hardware = NeedsPositiveDrive()
        # This historical fixture needs a 25 mV shunt drop without tripping
        # 5 mA. The replacement 0.789-ohm scale correctly trips before that
        # feedback condition, so retain the old resistance for this edge test.
        with patch.object(model, "DRAIN_SHUNT_OHM", 7.3):
            engine = Engine(hardware)
            engine.start_scan(settings(drain_start_v=0.0, drain_stop_v=0.0),
                              calibration_with_measured_zero_floor())
            run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        # Parking corrects its own drive, but the actual zero-volt scan point
        # uses zero duty without feedback. Its -25 mV reading is still outside
        # the narrow zero-floor baseline support and cannot become a point.
        self.assertIn("drain: Voltage -0.0250 V is below calibration coverage", engine.error)
        self.assertTrue(any(duty > 0 for role, duty in hardware.commands if role == "drain"))
        self.assertEqual(hardware.zeroes, 1)

    def test_zero_device_voltage_at_positive_source_does_not_opt_in(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_scan(settings(source_voltage_v=1.0, drain_start_v=0.0,
                                   drain_stop_v=0.0, gate_start_v=0.0, gate_stop_v=0.0),
                          calibration_with_measured_zero_floor())
        with patch.object(model, "corrected_current", wraps=model.corrected_current) as correction:
            run_engine(engine, hardware)
            self.assertGreater(correction.call_count, 0)
            self.assertTrue(all(call.kwargs["zero_drive"] is False for call in correction.call_args_list))
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertAlmostEqual(engine.latest["vds"], 0.0, delta=model.VOLTAGE_TOLERANCE_V)

    def test_maximum_sweep_preflight_checks_endpoints_without_walking_sequence(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        value = settings(drain_start_uv=0, drain_stop_uv=2047500,
                         drain_step_uv=500)
        with patch.object(model.SweepValues, "__iter__",
                          side_effect=AssertionError("Preflight traversed the sweep")):
            with patch.object(model, "_check_drive_coverage", wraps=model._check_drive_coverage) as lookup:
                engine.start_scan(value, calibration())
                self.assertEqual(lookup.call_count, 6)  # Source + two endpoints, per channel.
        self.assertEqual(engine._total, model.MAX_POINTS)
        self.assertEqual(len(engine._drains), model.MAX_POINTS)
        self.assertEqual(engine._drains[-1], 2047500)
        self.assertEqual(hardware.commands, [])

    def test_constructor_and_start_do_not_drive_and_invalid_start_preserves_idle(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        self.assertEqual(hardware.commands, [])
        with self.assertRaises(ValueError):
            engine.start_scan(settings(), calibration(maximum=0.5))
        self.assertEqual(engine.state, "idle")
        self.assertEqual(hardware.commands, [])
        self.assertEqual(hardware.zeroes, 0)
        engine.start_scan(settings(), calibration())
        self.assertEqual(hardware.commands, [])

    def test_gate_outer_drain_inner_and_raw_voltage_current_correction(self):
        hardware = FakeInstrument(drain_current=200e-6, gate_current=2e-6)
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=0, drain_stop_v=1, drain_step_v=0.5,
                                   gate_start_v=0, gate_stop_v=1, gate_step_v=1), calibration())
        events = run_engine(engine, hardware)
        rows = [value for event, value in events if event == "point"]
        self.assertEqual([(round(row["vds"], 5), round(row["vgs"], 5)) for row in rows],
                         [(0, 0), (0.5, 0), (1, 0), (0, 1), (0.5, 1), (1, 1)])
        self.assertEqual([row["curve"] for row in rows], [0, 0, 0, 1, 1, 1])
        for row in rows:
            self.assertAlmostEqual(row["id_a"], 200e-6)
            self.assertAlmostEqual(row["ig_a"], 2e-6)
            self.assertEqual(len(row["batchmeans"]), 1)
            self.assertIsNone(row["id_std_a"])
        self.assertEqual(engine.state, "complete")
        self.assertEqual(engine.progress, 1)
        self.assertEqual(hardware.zeroes, 1)

    def test_hardware_512_average_reads_only_completed_blocks(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        saved = calibration()
        saved["metadata"]["ina_average_count"] = 512
        engine.start_scan(settings(ina_average_count=512), saved)
        events = run_engine(engine, hardware)
        self.assertEqual(hardware.reads, 4)  # Two parks, fresh reference, final block.
        self.assertEqual(len(hardware.triggers), 3)
        for triggered, read in zip(hardware.triggers, (hardware.read_times[0],
                                  hardware.read_times[1], hardware.read_times[3])):
            self.assertEqual(triggered[1], 512)
            self.assertGreaterEqual(read - triggered[0], math.ceil(512 * 2.2))
        self.assertEqual(len([event for event, _ in events if event == "point"]), 1)
        self.assertEqual(engine.latest["acquisition_blocks_completed"], 1)
        self.assertEqual(len(engine.latest["batchmeans"]), 1)

    def test_single_hardware_block_has_no_invented_variance(self):
        hardware = FakeInstrument(gate_current=2e-6)
        engine = Engine(hardware)
        engine.start_scan(settings(), calibration())
        run_engine(engine, hardware)
        self.assertAlmostEqual(engine.latest["ig_a"], 2e-6)
        self.assertEqual(engine.latest["point_variance"], "unavailable")
        for key in ("id_std_a", "ig_std_a", "drain_shunt_std_v", "gate_shunt_std_v"):
            self.assertIsNone(engine.latest[key])

    def test_curve_transition_waits_for_drain_then_gate_before_applying_drain(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=0.5, drain_stop_v=1.0,
                                   gate_start_v=0.5, gate_stop_v=1.0), calibration())
        run_engine(engine, hardware)
        commands = hardware.timed_commands
        gates = [i for i, (_, role, _) in enumerate(commands) if role == "gate"]
        self.assertEqual(len(gates), 2)  # Gate untouched within each curve.
        for index in gates:
            park_time, park_role, park_duty = commands[index - 1]
            gate_time, _, _ = commands[index]
            drain_time, drain_role, drain_duty = commands[index + 1]
            self.assertEqual(park_role, "drain")
            self.assertEqual(park_duty, 0)
            self.assertGreaterEqual(gate_time - park_time, engine.settings["settle_ms"])
            self.assertEqual(drain_role, "drain")
            self.assertGreater(drain_duty, 0)
            self.assertGreaterEqual(drain_time - gate_time, engine.settings["settle_ms"])
            self.assertIn(gate_time, hardware.read_times)
            self.assertIn(drain_time, hardware.read_times)

    def test_cancel_during_gate_settle_prevents_drain_scan_drive(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_scan(settings(), calibration())
        engine.tick(0)
        for now in range(200, 265, 4):
            hardware.now = now
            engine.tick(now)
        self.assertEqual(engine._phase, "settle_gate")
        engine.cancel()
        self.assertEqual(engine.state, "cancelled")
        self.assertFalse(any(role == "drain" and duty > 0 for role, duty in hardware.commands))
        self.assertEqual(hardware.zeroes, 1)
        self.assertFalse(any(event == "point" for event, _ in engine.tick(1000)))

    def test_cancel_during_settle_parks_immediately_without_acquisition(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        self.assertFalse(engine.shutdown_succeeded)
        engine.start_scan(settings(), calibration())
        self.assertFalse(engine.shutdown_succeeded)
        engine.tick(0)
        engine.cancel()
        self.assertEqual(engine.state, "cancelled")
        self.assertTrue(engine.shutdown_succeeded)
        self.assertEqual(hardware.zeroes, 1)
        self.assertEqual(hardware.reads, 0)
        self.assertEqual(engine.tick(1), [("cancelled", {})])
        self.assertEqual(engine.tick(10000), [])
        engine.start_scan(settings(), calibration())
        self.assertFalse(engine.shutdown_succeeded)

    def test_nonzero_source_signed_gate_and_completion_park_at_source(self):
        hardware = FakeInstrument(gate_current=2e-6)
        engine = Engine(hardware)
        value = settings(source_voltage_v=1.0, drain_start_v=1, drain_stop_v=1,
                         gate_start_v=-0.5, gate_stop_v=-0.5)
        engine.start_scan(value, calibration())
        run_engine(engine, hardware)
        self.assertAlmostEqual(engine.latest["vgs"], -0.5)
        self.assertAlmostEqual(engine.latest["vds"], 1)
        self.assertAlmostEqual(engine.latest["ig_a"], 2e-6)
        self.assertEqual(hardware.zeroes, 0)
        self.assertTrue(engine.shutdown_succeeded)
        for duty in hardware.duties.values():
            self.assertAlmostEqual(duty * model.PWM_FULL_SCALE_V, 1)

    def test_cancel_nonzero_source_parks_at_source(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_scan(settings(source_voltage_v=1.0), calibration())
        engine.tick(0)
        engine.cancel()
        self.assertEqual(engine.state, "cancelled")
        for duty in hardware.duties.values():
            self.assertAlmostEqual(duty * model.PWM_FULL_SCALE_V, 1)

    def test_runtime_sensor_fault_and_overcurrent_zero_outputs(self):
        for error, drain_current in ((OSError("I2C disconnected"), 0), (None, 0.006)):
            hardware = FakeInstrument(drain_current=drain_current)
            hardware.read_error = error
            engine = Engine(hardware)
            engine.start_scan(settings(), calibration())
            events = run_engine(engine, hardware)
            expected = "partial" if drain_current else "error"
            self.assertEqual(engine.state, expected)
            self.assertEqual(hardware.zeroes, 1)
            self.assertTrue(any(event == expected for event, _ in events))
            self.assertFalse(any(event == "point" for event, _ in events))
            self.assertTrue(engine.shutdown_succeeded)

    def test_current_overrange_has_signed_finite_raw_current_for_both_channels(self):
        for role, limit in (("drain", model.DRAIN_CURRENT_LIMIT_A),
                            ("gate", model.GATE_CURRENT_LIMIT_A)):
            for sign in (-1, 1):
                with self.subTest(role=role, sign=sign):
                    hardware = FakeInstrument()
                    hardware.current[role] = sign * limit * 1.1
                    engine = Engine(hardware)
                    engine.start_scan(settings(), calibration())
                    events = run_engine(engine, hardware)
                    self.assertEqual(engine.state, "partial", engine.error)
                    fault = [data for kind, data in events if kind == "partial"][0]
                    self.assertEqual(fault["channel"], role)
                    self.assertEqual(fault["kind"], "current_limit")
                    self.assertEqual(fault["sign"], sign)
                    self.assertEqual(fault["limit_value"], limit)
                    self.assertEqual(fault["limit_unit"], "A")
                    self.assertAlmostEqual(fault["measured_value"], sign * limit * 1.1)
                    self.assertIsNone(fault["raw_code"])
                    self.assertIsNone(engine.latest)
                    self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_invalid_other_channel_takes_precedence_over_current_overrange(self):
        for field, value in (("shunt_v", float("nan")), ("bus_v", 5.2)):
            class InvalidGate(FakeInstrument):
                def read_pair(self):
                    pair = super().read_pair()
                    pair["gate"][field] = value
                    return pair
            hardware = InvalidGate(drain_current=0.006)
            engine = Engine(hardware)
            engine.start_scan(settings(), calibration())
            events = run_engine(engine, hardware)
            self.assertEqual(engine.state, "error")
            self.assertIsNone(engine.fault)
            self.assertTrue(engine.shutdown_succeeded)
            self.assertEqual(hardware.zeroes, 1)
            self.assertEqual(hardware.reads, 1)
            self.assertFalse(any(kind in ("partial", "point") for kind, _ in events))

    def test_adc_overrange_preserves_exact_fault_context_and_no_further_activity(self):
        for kind, code, sign, limit in (("shunt_saturation", 0x8000, -1, 0.08192),
                                        ("bus_saturation", 0x7fff, 1, 40.95875)):
            hardware = FakeInstrument()
            hardware.read_error = OverrangeError("gate", kind, code, sign, limit, "V")
            engine = Engine(hardware)
            engine.start_scan(settings(source_voltage_v=1.0, drain_start_v=0.5,
                                       drain_stop_v=0.5, gate_start_v=-0.5,
                                       gate_stop_v=-0.5), calibration())
            events = run_engine(engine, hardware)
            self.assertEqual(engine.state, "partial")
            self.assertEqual(events, [("partial", {
                "channel": "gate", "kind": kind, "raw_code": code, "sign": sign,
                "limit_value": limit, "limit_unit": "V", "measured_value": None,
                "index": 0, "curve": 0, "requested_vds": 0.5,
                "requested_vgs": -0.5, "phase": "park_drain"})])
            self.assertEqual(engine.fault, events[0][1])
            self.assertTrue(engine.shutdown_succeeded)
            self.assertEqual(hardware.duties, {"drain": 0.0, "gate": 0.0})
            reads, commands = hardware.reads, list(hardware.commands)
            for now in (10000, 20000, 30000):
                self.assertEqual(engine.tick(now), [])
            self.assertEqual(hardware.reads, reads)
            self.assertEqual(hardware.commands, commands)

    def test_partial_discards_unfinished_batches_and_retains_last_completed_point(self):
        class FailsDuringSecondPoint(FakeInstrument):
            def read_pair(self):
                if engine._index == 1 and len(self.triggers) == 4:
                    raise OverrangeError("drain", "shunt_saturation", 0x7fff, 1,
                                         0.0819175, "V")
                return super().read_pair()
        hardware = FailsDuringSecondPoint()
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=0.5, drain_stop_v=1.0, drain_step_v=0.5,
                                   gate_start_v=0.5, gate_stop_v=1.0, gate_step_v=0.5), calibration())
        events = run_engine(engine, hardware)
        points = [data for kind, data in events if kind == "point"]
        self.assertEqual(len(points), 1)
        self.assertEqual(engine.state, "partial")
        self.assertIs(engine.latest, points[0])
        self.assertEqual(engine.latest["index"], 0)
        self.assertEqual(engine.latest["acquisition_blocks_completed"], 1)
        self.assertEqual(engine.fault["index"], 1)
        self.assertEqual(engine.fault["curve"], 0)
        self.assertEqual(engine.fault["requested_vds"], 1.0)
        self.assertEqual(engine.fault["requested_vgs"], 0.5)
        self.assertEqual(engine.fault["phase"], "settle")
        self.assertEqual(engine.progress, 0.25)
        self.assertEqual(engine._batchmeans, [])
        self.assertEqual(engine._count, 0)
        self.assertTrue(all(value == 0 for value in engine._sums.values()))
        self.assertFalse(any(kind in ("complete", "error") for kind, _ in events))

    def test_calibration_overrange_and_lookalike_generic_errors_remain_hard_faults(self):
        for calibrating, error, current in (
                (True, OverrangeError("gate", "shunt_saturation", 0x7fff, 1, 0.0819175, "V"), 0),
                (True, None, 0.006),
                (False, ValueError("drain shunt ADC saturated"), 0),
                (False, OSError("I2C disconnected"), 0)):
            hardware = FakeInstrument(drain_current=current)
            hardware.read_error = error
            engine = Engine(hardware)
            if calibrating:
                engine.start_calibration(settings())
            else:
                engine.start_scan(settings(), calibration())
            events = run_engine(engine, hardware)
            self.assertEqual(engine.state, "error")
            self.assertTrue(engine.shutdown_succeeded)
            self.assertIsNone(engine.fault)
            self.assertTrue(any(kind == "error" for kind, _ in events))
            self.assertFalse(any(kind in ("partial", "point", "calibrated") for kind, _ in events))

    def test_overrange_shutdown_failure_cannot_emit_partial_result(self):
        hardware = FakeInstrument()
        hardware.read_error = OverrangeError("drain", "shunt_saturation", 0x8000, -1, 0.08192, "V")
        engine = Engine(hardware)
        engine.start_scan(settings(), calibration())
        with patch.object(hardware, "zero", side_effect=OSError("PWM unavailable")):
            events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "error")
        self.assertFalse(engine.shutdown_succeeded)
        self.assertIsNone(engine.fault)
        self.assertFalse(any(kind == "partial" for kind, _ in events))

    def test_overrange_constructor_rejects_invalid_or_nonfinite_measurement_claims(self):
        for arguments in (("gate", "other", None, 1, 1.0, "A", 2.0),
                          ("gate", "current_limit", None, 1, 0.0001, "A", float("inf")),
                          ("drain", "current_limit", None, -1, 0.005, "A", 0.006),
                          ("drain", "shunt_saturation", 0x7fff, 1, float("nan"), "V"),
                          ("drain", "shunt_saturation", 0x7ffe, 1, 0.0819175, "V"),
                          ("gate", "bus_saturation", 0x7fff, -1, 40.95875, "V")):
            with self.assertRaises(ValueError):
                OverrangeError(*arguments)

    def test_shutdown_failure_never_reports_outputs_stopped(self):
        for cancel in (False, True):
            hardware = FakeInstrument()
            engine = Engine(hardware)
            engine.start_scan(settings(), calibration())
            with patch.object(hardware, "zero", side_effect=OSError("PWM unavailable")):
                if cancel:
                    engine.cancel()
                else:
                    engine._fail(OSError("Sensor disconnected"))
            self.assertEqual(engine.state, "error")
            self.assertFalse(engine.shutdown_succeeded)

    def test_cancel_failed_park_marks_successful_zero_fallback(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_scan(settings(source_voltage_v=1.0), calibration())
        with patch.object(hardware, "set_duty", side_effect=OSError("Park failed")):
            engine.cancel()
        self.assertEqual(engine.state, "error")
        self.assertEqual(hardware.zeroes, 1)
        self.assertTrue(engine.shutdown_succeeded)

    def test_only_strict_drain_parking_corrects_small_voltage_error(self):
        hardware = FakeInstrument()
        hardware.voltage_offset = -0.04
        engine = Engine(hardware)
        engine.start_scan(settings(), calibration())
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertAlmostEqual(engine.latest["vds"], 0.96)
        self.assertAlmostEqual(engine.latest["vgs"], 0.96)
        self.assertEqual(len(hardware.commands), 4)
        self.assertEqual(hardware.commands[0], ("drain", 0.0))
        self.assertEqual(hardware.commands[1], ("drain", 0.04 / model.PWM_FULL_SCALE_V))
        self.assertEqual(hardware.commands[2:],
                         [("gate", 1.0 / model.PWM_FULL_SCALE_V),
                          ("drain", 1.0 / model.PWM_FULL_SCALE_V)])
        self.assertTrue(any(b - a >= engine.settings["settle_ms"]
                            for a, b in zip(hardware.read_times, hardware.read_times[1:])))

    def test_scan_at_nominal_upper_endpoint_records_actual_voltage(self):
        hardware = FakeInstrument(gate_current=2e-6)
        engine = Engine(hardware)
        engine.start_scan(settings(drain_start_v=3.0, drain_stop_v=3.0,
                                   gate_start_v=3.0, gate_stop_v=3.0),
                          calibration(maximum=2.985))
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertAlmostEqual(engine.latest["vds"], 2.985)
        self.assertAlmostEqual(engine.latest["vgs"], 2.985)
        self.assertAlmostEqual(engine.latest["ig_a"], 2e-6)
        self.assertTrue(all(duty <= 2.985 / model.PWM_FULL_SCALE_V
                            for _, duty in hardware.commands))

    def test_calibration_is_complete_before_publishing_and_never_written_by_engine(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_calibration(settings(source_voltage_v=1.0))
        events = run_engine(engine, hardware)
        completed = [value for event, value in events if event == "calibrated"]
        self.assertEqual(len(completed), 1)
        result = completed[0]
        model.validate_calibration(result, settings())
        for role in ("drain", "gate"):
            points = result["channels"][role]["points"]
            self.assertEqual(len(points), len(model.calibration_targets(settings())))
            for point in points:
                self.assertAlmostEqual(point["shunt_v"], baseline(role, point["terminal_v"]))
        self.assertEqual(hardware.zeroes, 1)

    def test_calibration_adapts_different_channel_gains_through_upper_endpoint(self):
        hardware = FakeInstrument()
        hardware.voltage_gain = {"drain": 1.03, "gate": 0.998}
        engine = Engine(hardware)
        engine.start_calibration(settings())
        events = run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        self.assertTrue(any(kind == "calibrated" for kind, _ in events))
        for role in ("drain", "gate"):
            points = engine.calibration["channels"][role]["points"]
            for target, point in zip(model.calibration_targets(settings()), points):
                self.assertLessEqual(abs(point["terminal_v"] - target), model.VOLTAGE_TOLERANCE_V)
                self.assertAlmostEqual(point["duty"] * model.PWM_FULL_SCALE_V *
                                       hardware.voltage_gain[role], point["terminal_v"], delta=0.000002)
            self.assertAlmostEqual(points[-1]["terminal_v"], 5.0, delta=0.02)

    def test_calibration_feedback_saves_corrected_applied_duty(self):
        hardware = FakeInstrument()
        hardware.voltage_gain["drain"] = 1.5
        value = settings(driver_supply_v=2.3, drain_start_v=0.0, drain_stop_v=0.2,
                         gate_start_v=0.0, gate_stop_v=0.2)
        engine = Engine(hardware)
        engine.start_calibration(value)
        run_engine(engine, hardware)
        self.assertEqual(engine.state, "complete", engine.error)
        points = engine.calibration["channels"]["drain"]["points"]
        self.assertGreater(len([command for command in hardware.commands if command[0] == "drain"]),
                           len(points))
        for point in points:
            self.assertAlmostEqual(point["duty"] * model.PWM_FULL_SCALE_V * 1.5,
                                   point["terminal_v"], delta=0.000002)
        model.validate_calibration(engine.calibration, value)

    def test_calibration_feedback_is_bounded_and_retains_voltage_guard(self):
        value = settings(driver_supply_v=2.3, drain_start_v=0.0, drain_stop_v=0.2,
                         gate_start_v=0.0, gate_stop_v=0.2)
        for gain, reason in ((0.0, "no usable calibration span"), (30.0, "voltage outside")):
            hardware = FakeInstrument()
            hardware.voltage_gain["drain"] = gain
            engine = Engine(hardware)
            engine.start_calibration(value)
            events = run_engine(engine, hardware)
            self.assertEqual(engine.state, "error")
            self.assertIn(reason, engine.error)
            self.assertEqual(hardware.zeroes, 1)
            self.assertFalse(any(kind == "calibrated" for kind, _ in events))
            if gain == 0.0:
                # Zero point + initial next point + three correction attempts.
                self.assertLessEqual(len([command for command in hardware.commands
                                          if command[0] == "drain"]), 5)

    def test_cancelled_calibration_does_not_publish_partial_result(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        engine.start_calibration(settings())
        engine.tick(0)
        engine.cancel()
        events = engine.tick(1)
        self.assertFalse(any(event == "calibrated" for event, _ in events))


class StorageTests(unittest.TestCase):
    def test_only_missing_settings_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "absent.json")
            self.assertEqual(storage.load_settings(path), model.DEFAULT_SETTINGS)
        with patch("builtins.open", side_effect=OSError(5, "I/O error")):
            with self.assertRaises(OSError):
                storage.load_settings("settings.json")

    def test_settings_round_trip_and_edit_draft_is_not_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            committed = storage.save_settings(settings(), path)
            draft = dict(committed)
            draft["gate_stop_uv"] = 2000000
            self.assertEqual(storage.load_settings(path), committed)
            storage.save_settings(draft, path)
            self.assertEqual(storage.load_settings(path)["gate_stop_uv"], 2000000)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_failed_write_keeps_committed_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            storage.save_settings(settings(), path)
            before = storage.load_settings(path)

            def interrupted(data, stream):
                stream.write('{"partial":')
                raise OSError("Power interrupted")

            with patch.object(storage.json, "dump", interrupted):
                with self.assertRaises(OSError):
                    storage.save_settings(settings(gate_stop_v=2), path)
            self.assertEqual(storage.load_settings(path), before)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_corrupted_committed_json_is_reported_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with open(path, "w") as stream:
                stream.write("{")
            with self.assertRaises(ValueError):
                storage.load_settings(path)
            with open(path) as stream:
                self.assertEqual(stream.read(), "{")


if __name__ == "__main__":
    unittest.main()
