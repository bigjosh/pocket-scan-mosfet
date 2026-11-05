"""Direct nominal drive, raw measurements and reported ADC endpoint stops."""
import math
import unittest
from firmware import model
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


class FakeInstrument:
    identity = {"drain": {"address": 0x44}, "gate": {"address": 0x40}}

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
            terminal = ((3.3 + 22.44 * self.duties[role] if role == "drain"
                         else 25.74 * self.duties[role]) * self.voltage_gain[role]
                        + self.voltage_offset)
            terminal = min(terminal, self.voltage_ceiling[role])
            shunt = resistance * self.current[role]
            result[role] = {"terminal_v": terminal, "shunt_v": shunt,
                            "bus_v": terminal + shunt}
        return result


def run_engine(engine, hardware, maximum_ms=100000):
    events = []
    for now in range(maximum_ms):
        hardware.now = now
        before = hardware.reads
        events.extend(engine.tick(now))
        assert hardware.reads - before <= 1
        if engine.state != "scanning":
            return events
    raise AssertionError("Engine did not terminate")


class ModelTests(unittest.TestCase):
    def test_nominal_resistor_network_and_source_offset(self):
        self.assertAlmostEqual(model.AMPLIFIER_GAIN, 7.8)
        self.assertAlmostEqual(model.DRAIN_PWM_FULL_SCALE_V, 22.44)
        self.assertAlmostEqual(model.GATE_PWM_FULL_SCALE_V, 25.74)
        self.assertAlmostEqual(model.DRAIN_ZERO_DUTY_V, 3.3)
        for vds in (0, 0.1, 1, 10):
            self.assertAlmostEqual(model.drive_duty("drain", 3.3 + vds), vds / 22.44)
        self.assertAlmostEqual(model.drive_duty("gate", 0), 0)
        self.assertAlmostEqual(model.drive_duty("gate", 3.3), 3.3 / 25.74)
        self.assertAlmostEqual(model.drive_duty("gate", 13.3), 13.3 / 25.74)
        # The physical bias stays3.3 when a configured source differs.
        self.assertAlmostEqual(model.drive_duty("drain", 2.0 + 3.0), 1.7 / 22.44)

    def test_direct_duty_clamps_and_rejects_invalid_numeric_commands(self):
        for role in ("drain", "gate"):
            self.assertEqual(model.drive_duty(role, -100), 0)
            self.assertEqual(model.drive_duty(role, 100), 1)
            for invalid in (True, None, "1", float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    model.drive_duty(role, invalid)
        with self.assertRaises(ValueError):
            model.drive_duty("source", 3.3)

    def test_integer_grid_and_capacity_validation(self):
        self.assertEqual(list(model.sweep_uv(0, 1000000, 300000)), [0, 300000, 600000, 900000])
        value = settings(drain_start_uv=0, drain_stop_uv=630000, drain_step_uv=10000,
                         gate_start_uv=0, gate_stop_uv=630000, gate_step_uv=10000)
        self.assertEqual(model.scan_point_count(model.validate_settings(value)), 4096)
        value["gate_stop_uv"] = 640000
        with self.assertRaisesRegex(ValueError, "4096"):
            model.validate_settings(value)
        for key, bad in (("drain_start_uv", True), ("gate_step_uv", 0),
                          ("ina_average_count", 2), ("settle_ms", 99)):
            with self.assertRaises(ValueError):
                model.validate_settings(settings(**{key: bad}))

    def test_current_hardware_defaults_without_independent_current_limits(self):
        self.assertEqual(model.DEFAULT_SETTINGS["source_voltage_uv"], 3300000)
        self.assertEqual(model.DEFAULT_SETTINGS["driver_supply_uv"], 18800000)
        self.assertEqual(model.DEFAULT_SETTINGS["drain_start_uv"], 100000)
        self.assertFalse(hasattr(model, "DRAIN_CURRENT_LIMIT_A"))
        self.assertFalse(hasattr(model, "GATE_CURRENT_LIMIT_A"))
        self.assertEqual(model.GATE_SHUNT_OHM, 98.8)
        for obsolete in ("validate_calibration", "corrected_current", "interpolate",
                          "calibration_duty_limit", "calibration_targets_uv"):
            self.assertFalse(hasattr(model, obsolete), obsolete)


class EngineTests(unittest.TestCase):
    def test_complete_grid_one_hardware_block_per_point_and_raw_snapshots(self):
        hw = FakeInstrument(drain_current=0.021, gate_current=-12e-6)
        scanner = Engine(hw)
        config = settings(drain_start_v=0.1, drain_stop_v=1.1, drain_step_v=0.5,
                          gate_start_v=-3.3, gate_stop_v=0.7, gate_step_v=2)
        scanner.start_scan(config)
        self.assertEqual(hw.commands, [])
        config["source_voltage_uv"] = 0  # Armed configuration owns its copy.
        events = run_engine(scanner, hw)
        rows = [row for event, row in events if event == "point"]
        self.assertEqual(scanner.state, "complete", scanner.error)
        self.assertEqual(len(rows), 9)
        self.assertEqual(len(hw.triggers), 9)
        self.assertEqual(hw.reads, 18)  # Endpoint check + one averaged pair.
        self.assertEqual([row["curve"] for row in rows], [0]*3 + [1]*3 + [2]*3)
        self.assertEqual(len({id(row) for row in rows}), 9)
        expected_fields = {"vds", "vgs", "id_a", "ig_a", "index", "total", "curve",
                           "requested_vds", "requested_vgs", "acquisition_blocks_completed"}
        for index, row in enumerate(rows):
            self.assertEqual(set(row), expected_fields)
            self.assertEqual(row["index"], index)
            self.assertAlmostEqual(row["vds"], row["requested_vds"])
            self.assertAlmostEqual(row["vgs"], row["requested_vgs"])
            self.assertAlmostEqual(row["id_a"], 0.021)
            self.assertAlmostEqual(row["ig_a"], -12e-6)
        self.assertEqual(len([x for x in hw.commands if x[0] == "gate"]), 3)
        self.assertTrue(scanner.shutdown_succeeded)
        self.assertEqual(hw.duties, {"drain": 0, "gate": 0})
        self.assertFalse(hasattr(scanner, "calibration"))

    def test_target_misses_drift_and_negative_vds_do_not_stop_scan(self):
        class Unreachable(FakeInstrument):
            def read_pair(self):
                pair = super().read_pair()
                # Match the recent raw negative20.9mA case without a computed-current stop.
                pair["drain"].update(bus_v=2.6225, shunt_v=-0.0165, terminal_v=999)
                pair["gate"].update(bus_v=0.24125 + self.reads * 0.08, shunt_v=10e-6)
                return pair
        hw, scanner = Unreachable(), None
        scanner = Engine(hw)
        scanner.start_scan(settings(drain_stop_v=2, drain_step_v=1))
        rows = [row for kind, row in run_engine(scanner, hw) if kind == "point"]
        self.assertEqual(scanner.state, "complete", scanner.error)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["vds"], 2.6225 + 0.0165 - 3.3)
        self.assertAlmostEqual(rows[0]["id_a"], -0.0165 / 0.789)
        self.assertAlmostEqual(rows[0]["ig_a"], 10e-6 / model.GATE_SHUNT_OHM)
        self.assertNotEqual(rows[0]["vgs"], rows[1]["vgs"])

    def test_reported_adc_endpoints_stop_both_channels_and_signs_before_trigger(self):
        for role in ("drain", "gate"):
            for code, sign in (((0x7FFF, 1), (0x8000, -1)) if role == "drain" else
                               ((0x7FFFF, 1), (0x80000, -1))):
                hw = FakeInstrument()
                hw.read_error = OverrangeError(role, "shunt_saturation", code, sign,
                                               code * (2.5e-6 if role == "drain" else 0.3125e-6), "V")
                scanner = Engine(hw)
                scanner.start_scan(settings())
                events = run_engine(scanner, hw)
                self.assertEqual(scanner.state, "partial")
                self.assertEqual(scanner.fault["kind"], "shunt_saturation")
                self.assertEqual(scanner.fault["sign"], sign)
                self.assertEqual(scanner.fault["index"], 0)
                self.assertIsNone(scanner.latest)
                self.assertEqual(hw.triggers, [])
                self.assertFalse(any(kind == "point" for kind, _ in events))
                self.assertTrue(scanner.shutdown_succeeded)

    def test_near_adc_endpoints_record_raw_current_without_lower_thresholds(self):
        for signed in (-32767, 32766):
            shunt = signed * 2.5e-6
            hw = FakeInstrument(drain_current=shunt / 0.789, gate_current=shunt / model.GATE_SHUNT_OHM)
            scanner = Engine(hw)
            scanner.start_scan(settings())
            rows = [row for kind, row in run_engine(scanner, hw) if kind == "point"]
            self.assertEqual(scanner.state, "complete", scanner.error)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["id_a"], shunt / 0.789)
            self.assertAlmostEqual(rows[0]["ig_a"], shunt / model.GATE_SHUNT_OHM)
            self.assertGreater(abs(rows[0]["id_a"]), 0.1)

    def test_partial_trip_retains_only_previously_completed_points(self):
        class TripsOnSecondPoint(FakeInstrument):
            def read_pair(self):
                if self.reads == 3:
                    raise OverrangeError("gate", "shunt_saturation", 0x80000, -1,
                                         0.16384, "V")
                return super().read_pair()
        hw = TripsOnSecondPoint()
        scanner = Engine(hw)
        scanner.start_scan(settings(drain_stop_v=3, drain_step_v=1))
        rows = [row for kind, row in run_engine(scanner, hw) if kind == "point"]
        self.assertEqual(scanner.state, "partial")
        self.assertEqual(len(rows), 1)
        self.assertIs(scanner.latest, rows[0])
        self.assertEqual(scanner.fault["index"], 1)
        self.assertEqual(scanner.fault["kind"], "shunt_saturation")
        self.assertEqual(scanner.fault["raw_code"], 0x80000)
        self.assertIsNone(scanner.fault["measured_value"])

    def test_cancel_in_settle_and_long_average_never_emits_an_extra_point(self):
        for after_trigger in (False, True):
            hw = FakeInstrument()
            scanner = Engine(hw)
            scanner.start_scan(settings(ina_average_count=1024))
            scanner.tick(0)
            if after_trigger:
                hw.now = 100
                scanner.tick(100)
                self.assertTrue(scanner._measurement_waiting)
            scanner.cancel()
            self.assertEqual(scanner.state, "cancelled")
            self.assertEqual(scanner.tick(4000), [("cancelled", {})])
            self.assertEqual(scanner.tick(4001), [])
            self.assertTrue(scanner.shutdown_succeeded)
            self.assertEqual(hw.duties, {"drain": 0, "gate": 0})

    def test_missing_ready_times_out_but_late_poll_accepts_finished_data(self):
        class NeverReady(FakeInstrument):
            def poll_measurement(self):
                return False
        hw = NeverReady()
        scanner = Engine(hw)
        scanner.start_scan(settings(ina_average_count=1))
        run_engine(scanner, hw)
        self.assertEqual(scanner.state, "error")
        self.assertIn("timed out", scanner.error)
        self.assertTrue(scanner.shutdown_succeeded)
        hw = FakeInstrument()
        scanner = Engine(hw)
        scanner.start_scan(settings(ina_average_count=1))
        scanner.tick(0)
        hw.now = 100
        scanner.tick(100)
        hw.now = 2000
        rows = [row for kind, row in scanner.tick(2000) if kind == "point"]
        self.assertEqual(scanner.state, "complete")
        self.assertEqual(len(rows), 1)

    def test_io_failure_and_failed_shutdown_remain_hard_errors(self):
        hw = FakeInstrument()
        hw.read_error = OSError("I2C disconnected")
        scanner = Engine(hw)
        scanner.start_scan(settings())
        run_engine(scanner, hw)
        self.assertEqual(scanner.state, "error")
        self.assertIn("I2C disconnected", scanner.error)
        self.assertTrue(scanner.shutdown_succeeded)
        hw = FakeInstrument()
        hw.read_error = OverrangeError("drain", "shunt_saturation", 0x7FFF, 1, 0.0819175, "V")
        def broken_zero():
            raise OSError("PWM failure")
        hw.zero = broken_zero
        scanner = Engine(hw)
        scanner.start_scan(settings())
        run_engine(scanner, hw)
        self.assertEqual(scanner.state, "error")
        self.assertFalse(scanner.shutdown_succeeded)
        self.assertIsNone(scanner.fault)

    def test_restart_clears_gate_command_state_and_rejects_reentrant_start(self):
        hw = FakeInstrument()
        scanner = Engine(hw)
        scanner.start_scan(settings())
        with self.assertRaises(ValueError):
            scanner.start_scan(settings())
        run_engine(scanner, hw)
        scanner.start_scan(settings(gate_start_v=2, gate_stop_v=2))
        scanner.tick(0)
        self.assertAlmostEqual(hw.duties["gate"], 5.3 / 25.74)


if __name__ == "__main__":
    unittest.main()
