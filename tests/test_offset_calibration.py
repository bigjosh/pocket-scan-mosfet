"""Calibrate actual zero-duty floors without assuming either output is GND."""

import copy
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from test_model_engine import FakeInstrument, baseline, run_engine, settings


class OffsetInstrument(FakeInstrument):
    def __init__(self):
        super().__init__()
        self.floors = {"drain": 3.3, "gate": 0.0}
        self.gains = {"drain": 6.8 * 3.3, "gate": 7.8 * 3.3}
        self.voltage_ceiling = {"drain": 16.5, "gate": 16.5}
        self.floor_noise = 0.0

    def read_pair(self):
        self.reads += 1
        self.read_times.append(self.now)
        result = {}
        for role, resistance in (("drain", model.DRAIN_SHUNT_OHM),
                                 ("gate", model.GATE_SHUNT_OHM)):
            voltage = min(self.voltage_ceiling[role],
                          self.floors[role] + self.gains[role] * self.duties[role])
            if role == "drain" and self.duties[role] == 0:
                voltage += self.floor_noise * (-1, 0, 1)[self.reads % 3]
            shunt = baseline(role, voltage) + resistance * self.current[role]
            result[role] = {"terminal_v": voltage, "shunt_v": shunt,
                            "bus_v": voltage + shunt}
        return result


def offset_settings(**changes):
    value = settings(driver_supply_uv=18000000, source_voltage_uv=3300000)
    value.update(changes)
    return value


def calibrate(hardware, value=None):
    value = offset_settings() if value is None else value
    scanner = Engine(hardware)
    scanner.start_calibration(value)
    events = run_engine(scanner, hardware, maximum_ms=300000)
    return scanner, events


def plain_calibration(value):
    """Mutable legacy fixture for validation-corruption tests."""
    value = copy.deepcopy(value)
    for channel in value["channels"].values():
        channel["points"] = list(channel["points"])
    return value


class OffsetCalibrationTests(unittest.TestCase):
    def test_affine_drain_and_ground_gate_calibrate_from_measured_zero_duty(self):
        hardware = OffsetInstrument()
        scanner, events = calibrate(hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        self.assertEqual(1, sum(kind == "calibrated" for kind, _ in events))
        model.validate_scan_calibration(scanner.calibration, offset_settings())
        for role, floor in hardware.floors.items():
            points = scanner.calibration["channels"][role]["points"]
            self.assertEqual(0.0, points[0]["duty"])
            self.assertAlmostEqual(floor, points[0]["terminal_v"])
            self.assertAlmostEqual(floor, points[0]["zero_min_v"])
            self.assertAlmostEqual(floor, points[0]["zero_max_v"])
            self.assertTrue(all(b["terminal_v"] > a["terminal_v"] and b["duty"] > a["duty"]
                                for a, b in zip(points, list(points)[1:])))
            self.assertTrue(all(abs(p["shunt_v"] - baseline(role, p["terminal_v"])) / scanner.calibration["channels"][role]["shunt_ohm"] < 0.00000005
                                for p in points))
        self.assertTrue(all(0 <= duty <= model.calibration_duty_limit(offset_settings())
                            for _, duty in hardware.commands))

    def test_each_channel_can_have_its_own_nonzero_floor(self):
        hardware = OffsetInstrument()
        hardware.floors = {"drain": 1.75, "gate": 0.35}
        hardware.voltage_ceiling = {"drain": 4.5, "gate": 4.5}
        scanner, _ = calibrate(hardware, settings(source_voltage_uv=2000000))
        self.assertEqual("complete", scanner.state, scanner.error)
        for role, floor in hardware.floors.items():
            self.assertAlmostEqual(floor, scanner.calibration["channels"][role]["points"][0]["terminal_v"])

    def test_offset_channel_finishes_at_rail_before_global_grid_ends(self):
        hardware = OffsetInstrument()
        hardware.gains = {"drain": 100.0, "gate": 100.0}
        hardware.voltage_ceiling = {"drain": float("inf"), "gate": float("inf")}
        with patch.object(model, "PWM_FULL_SCALE_V", 100.0):
            scanner, _ = calibrate(hardware)
            self.assertEqual("complete", scanner.state, scanner.error)
            model.validate_calibration(scanner.calibration, offset_settings())
        drain = scanner.calibration["channels"]["drain"]
        gate = scanner.calibration["channels"]["gate"]
        self.assertAlmostEqual(18.0, drain["points"][-1]["terminal_v"])
        self.assertEqual("rail_envelope", drain["capability"]["limit_reason"])
        self.assertLess(len(drain["points"]), len(gate["points"]))
        last_drain = max(t for t, role, _ in hardware.timed_commands if role == "drain")
        last_gate = max(t for t, role, _ in hardware.timed_commands if role == "gate")
        self.assertLess(last_drain, last_gate)

    def test_initial_floor_still_checks_current_voltage_and_stability(self):
        for failure in ("current", "voltage", "stability"):
            with self.subTest(failure=failure):
                hardware = OffsetInstrument()
                if failure == "current":
                    hardware.current["drain"] = model.DRAIN_CURRENT_LIMIT_A * 2
                elif failure == "voltage":
                    hardware.floors["drain"] = 19.0
                    hardware.voltage_ceiling["drain"] = 19.0
                else:
                    hardware.floor_noise = 0.05
                scanner, events = calibrate(hardware)
                self.assertEqual("error", scanner.state)
                self.assertEqual(0, scanner._cal_counts["drain"])
                self.assertFalse(any(kind == "calibrated" for kind, _ in events))
                self.assertGreater(hardware.zeroes, 0)

    def test_flat_nonzero_output_is_not_accepted_as_a_calibration(self):
        hardware = OffsetInstrument()
        hardware.voltage_ceiling["drain"] = 3.3
        scanner, _ = calibrate(hardware)
        self.assertEqual("error", scanner.state)
        self.assertIn("no usable calibration span", scanner.error)

    def test_above_source_floor_records_actual_zero_vds_and_parks_gate_first(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        self.assertEqual("complete", discovery.state, discovery.error)
        hardware.commands.clear()
        hardware.current["drain"] = 0.001
        value = offset_settings(drain_start_uv=0, drain_stop_uv=400000,
                                drain_step_uv=100000, gate_start_uv=-1000000,
                                gate_stop_uv=-1000000)
        scanner = Engine(hardware)
        scanner.start_scan(value, discovery.calibration)
        events = run_engine(scanner, hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        rows = [row for kind, row in events if kind == "point"]
        self.assertEqual(5, len(rows))
        for row in rows:
            self.assertAlmostEqual(max(0.28, row["requested_vds"]), row["vds"], delta=0.000002)
            self.assertAlmostEqual(0.001, row["id_a"])
            self.assertEqual(row["requested_vds"] < 0.28, row["drain_voltage_limited"])
        self.assertEqual([("gate", 0.0), ("drain", 0.0)], hardware.commands[-2:])
        scanner.start_scan(value, discovery.calibration)
        scanner.tick(0)
        scanner.cancel()
        self.assertEqual("cancelled", scanner.state)
        self.assertEqual([("gate", 0.0), ("drain", 0.0)], hardware.commands[-2:])

    def test_above_source_floor_requires_fresh_matching_source_and_bounded_offset(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        self.assertEqual("complete", discovery.state, discovery.error)
        value = offset_settings(drain_start_uv=0, drain_stop_uv=0)
        for missing in ("source_voltage_uv", "zero_min_v", "zero_max_v"):
            invalid = plain_calibration(discovery.calibration)
            owner = invalid["metadata"] if missing == "source_voltage_uv" else invalid["channels"]["drain"]["points"][0]
            del owner[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                Engine(hardware).start_scan(value, invalid)
        for changes in ({"source_voltage_uv": 3200000},
                        {"source_voltage_uv": 3700000},
                        {"drain_start_uv": -100000, "drain_stop_uv": -100000}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Engine(hardware).start_scan(dict(value, **changes), discovery.calibration)
        hardware.floors["drain"] = 15.0
        high, _ = calibrate(hardware)
        self.assertEqual("complete", high.state, high.error)
        with self.assertRaisesRegex(ValueError, "floor.*0.5"):
            Engine(hardware).start_scan(value, high.calibration)

    def test_floor_drift_and_raw_current_trip_before_and_after_gate_change(self):
        for phase in ("park_drain", "settle_gate", "settle"):
            for failure in ("up", "down", "current"):
                with self.subTest(phase=phase, failure=failure):
                    hardware = OffsetInstrument()
                    hardware.floors["drain"] = 3.58
                    discovery, _ = calibrate(hardware)
                    scanner = Engine(hardware)
                    original_read = hardware.read_pair
                    def read():
                        if scanner._phase == phase:
                            if failure == "current":
                                hardware.current["drain"] = 0.006
                            else:
                                hardware.floors["drain"] = 3.69 if failure == "up" else 3.20
                        return original_read()
                    hardware.read_pair = read
                    scanner.start_scan(offset_settings(drain_start_uv=0, drain_stop_uv=0), discovery.calibration)
                    events = run_engine(scanner, hardware)
                    self.assertEqual("partial" if failure == "current" else "error", scanner.state)
                    self.assertIn("current limit" if failure == "current" else "floor", scanner.error)
                    self.assertFalse(any(kind == "point" for kind, _ in events))
                    self.assertEqual(0.0, hardware.duties["gate"])
                    self.assertEqual(0.0, hardware.duties["drain"])

    def test_floor_parking_does_not_expand_baseline_readback_coverage(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        hardware.floors["drain"] = 3.53  # Within 100 mV parking, outside the 5 mV baseline edge.
        scanner = Engine(hardware)
        scanner.start_scan(offset_settings(drain_start_uv=0, drain_stop_uv=0), discovery.calibration)
        events = run_engine(scanner, hardware)
        self.assertEqual("error", scanner.state)
        self.assertIn("below calibration coverage", scanner.error)
        self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_zero_duty_parking_accepts_positive_floor_shifts_without_servo(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        saved_floor = discovery.calibration["channels"]["drain"]["points"][0]["terminal_v"]
        for change in (-0.100, -0.099, -0.050, -0.025, 0.025, 0.050, 0.099, 0.100):
            with self.subTest(change=change):
                hardware.floors["drain"] = saved_floor + change
                hardware.commands.clear()
                scanner = Engine(hardware)
                scanner.start_scan(offset_settings(drain_start_uv=500000, drain_stop_uv=500000),
                                   discovery.calibration)
                events = run_engine(scanner, hardware)
                self.assertEqual("complete", scanner.state, scanner.error)
                self.assertEqual(1, sum(kind == "point" for kind, _ in events))
                # No intermediate correction of the accepted zero-duty park.
                self.assertEqual(1, sum(role == "drain" and duty > 0 for role, duty in hardware.commands))
                self.assertAlmostEqual(scanner.latest["vds"], 0.5 + change, delta=0.00001)

    def test_zero_duty_parking_rejects_more_than_100mv_in_either_direction(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        for change in (-0.101, 0.101):
            hardware.floors["drain"] = 3.58 + change
            scanner = Engine(hardware)
            scanner.start_scan(offset_settings(), discovery.calibration)
            events = run_engine(scanner, hardware)
            self.assertEqual("error", scanner.state)
            self.assertIn("drifted", scanner.error)
            self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_source_lower_bound_is_independent_of_100mv_parking_band(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.36
        discovery, _ = calibrate(hardware)
        for actual, accepted in ((3.296, True), (3.294, False)):
            with self.subTest(actual=actual):
                hardware.floors["drain"] = actual
                scanner = Engine(hardware)
                scanner.start_scan(offset_settings(drain_start_uv=500000, drain_stop_uv=500000),
                                   discovery.calibration)
                events = run_engine(scanner, hardware)
                self.assertEqual("complete" if accepted else "error", scanner.state, scanner.error)
                if not accepted:
                    self.assertIn("below source safety bound", scanner.error)
                    self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_parking_allowance_does_not_widen_nonzero_drive_or_sampling_stability(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        scanner = Engine(hardware)
        scanner.start_scan(offset_settings(), discovery.calibration)
        scanner.tick(0)
        scanner._applied["drain"] = 0.01
        settled = scanner._check_settled({"drain": {"terminal_v": scanner._drain_park_v - 0.05}},
                                        {"drain": scanner._drain_park_v}, strict_roles=("drain",))
        self.assertFalse(settled)
        self.assertGreater(scanner._applied["drain"], 0.01)
        scanner.cancel()

        scanner = Engine(hardware)
        initial_trigger_count = len(hardware.triggers)
        original_read = hardware.read_pair
        def moving_read():
            if len(hardware.triggers) == initial_trigger_count + 3:
                hardware.floors["drain"] = 3.53
            return original_read()
        hardware.read_pair = moving_read
        scanner.start_scan(offset_settings(drain_start_uv=0, drain_stop_uv=0), discovery.calibration)
        events = run_engine(scanner, hardware)
        self.assertEqual("error", scanner.state)
        self.assertIn("output changed during averaging", scanner.error)
        self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_wider_floor_parking_preserves_both_channel_raw_current_limits(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        discovery, _ = calibrate(hardware)
        for role, limit in (("drain", model.DRAIN_CURRENT_LIMIT_A),
                            ("gate", model.GATE_CURRENT_LIMIT_A)):
            for sign in (-1, 1):
                with self.subTest(role=role, sign=sign):
                    hardware.floors["drain"] = 3.53
                    hardware.current = {"drain": 0.0, "gate": 0.0}
                    hardware.current[role] = sign * limit * 2
                    scanner = Engine(hardware)
                    scanner.start_scan(offset_settings(), discovery.calibration)
                    events = run_engine(scanner, hardware)
                    self.assertEqual("partial", scanner.state, scanner.error)
                    self.assertEqual(role, scanner.fault["channel"])
                    self.assertEqual(sign, scanner.fault["sign"])
                    self.assertEqual("current_limit", scanner.fault["kind"])
                    self.assertFalse(any(kind == "point" for kind, _ in events))

    def test_flat_plateau_support_includes_stored_mean_with_three_hardware_blocks(self):
        hardware = OffsetInstrument()
        scanner = Engine(hardware)
        scanner.start_calibration(offset_settings(ina_average_count=512))
        run_engine(scanner, hardware, maximum_ms=900000)
        self.assertEqual("complete", scanner.state, scanner.error)
        model.validate_calibration(scanner.calibration)
        for channel in scanner.calibration["channels"].values():
            endpoint = channel["points"][-1]["terminal_v"]
            self.assertLessEqual(channel["capability"]["endpoint_min_v"], endpoint)
            self.assertGreaterEqual(channel["capability"]["endpoint_max_v"], endpoint)

    def test_scan_zero_vds_uses_observed_floor_support_without_changing_voltage(self):
        hardware = OffsetInstrument()
        hardware.floor_noise = 0.01
        discovery, _ = calibrate(hardware)
        self.assertEqual("complete", discovery.state, discovery.error)
        channel = discovery.calibration["channels"]["drain"]
        first = channel["points"][0]
        self.assertAlmostEqual(3.29, first["zero_min_v"])
        self.assertAlmostEqual(3.31, first["zero_max_v"])
        hardware.floor_noise = 0.0
        hardware.floors["drain"] = 3.294
        scanner = Engine(hardware)
        scanner.start_scan(offset_settings(drain_start_uv=0, drain_stop_uv=0,
                                           gate_start_uv=0, gate_stop_uv=0), discovery.calibration)
        with patch.object(model, "corrected_current", wraps=model.corrected_current) as correction:
            events = run_engine(scanner, hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        row = next(value for kind, value in events if kind == "point")
        self.assertAlmostEqual(-0.006, row["vds"])
        self.assertAlmostEqual(baseline("drain", 3.294), row["drain_shunt_v"])
        drain_calls = [call for call in correction.call_args_list if call.args[0] is channel]
        self.assertTrue(drain_calls)
        self.assertTrue(all(call.kwargs["zero_drive"] for call in drain_calls))
        with self.assertRaisesRegex(ValueError, "below calibration coverage"):
            model.corrected_current(channel, 3.294, 0)
        model.corrected_current(channel, 3.285, 0, zero_drive=True)
        with self.assertRaisesRegex(ValueError, "below calibration coverage"):
            model.corrected_current(channel, 3.2849, 0, zero_drive=True)

    def test_zero_floor_support_is_validated_and_optional_for_legacy_tables(self):
        scanner, _ = calibrate(OffsetInstrument())
        self.assertEqual("complete", scanner.state, scanner.error)
        saved = scanner.calibration
        invalid_cases = (
            {"zero_min_v": None},
            {"zero_min_v": 3.4},
            {"zero_min_v": 3.0},
            {"zero_max_v": 3.2},
        )
        for changes in invalid_cases:
            with self.subTest(changes=changes):
                invalid = plain_calibration(saved)
                invalid["channels"]["drain"]["points"][0].update(changes)
                with self.assertRaisesRegex(ValueError, "zero-drive"):
                    model.validate_calibration(invalid)
        legacy = plain_calibration(saved)
        for channel in legacy["channels"].values():
            del channel["points"][0]["zero_min_v"]
            del channel["points"][0]["zero_max_v"]
        model.validate_calibration(legacy)
        invalid = plain_calibration(saved)
        del invalid["channels"]["drain"]["points"][0]["zero_max_v"]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            model.validate_calibration(invalid)

    def test_new_near_ground_table_preserves_original_zero_floor_allowance(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 0.006435747
        value = settings(drain_start_uv=0, drain_stop_uv=0)
        discovery, _ = calibrate(hardware, value)
        self.assertEqual("complete", discovery.state, discovery.error)
        first = discovery.calibration["channels"]["drain"]["points"][0]
        self.assertIn("zero_min_v", first)
        self.assertAlmostEqual(0.006435747, first["zero_min_v"])
        hardware.floors["drain"] = 0.0013
        scanner = Engine(hardware)
        scanner.start_scan(value, discovery.calibration)
        events = run_engine(scanner, hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        row = next(value for kind, value in events if kind == "point")
        self.assertAlmostEqual(0.0013, row["vds"])
        expected = (baseline("drain", 0.0013) - first["shunt_v"]) / model.DRAIN_SHUNT_OHM
        self.assertAlmostEqual(expected, row["id_a"])


if __name__ == "__main__":
    unittest.main()
