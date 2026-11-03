"""Gate inverse lookup is reused only within one constant-gate scan curve."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine, OverrangeError
from test_model_engine import FakeInstrument, calibration, run_engine, settings


class UncachedEngine(Engine):
    """Exercise the same state machine with a fresh gate lookup every point."""
    def _drive(self):
        if self.state == "scanning":
            self._gate_drive_curve = None
        return super()._drive()


class GateDriveCacheTests(unittest.TestCase):
    def grid(self, **changes):
        value = settings(drain_start_v=0.5, drain_stop_v=1.5, drain_step_v=0.5,
                         gate_start_v=0.5, gate_stop_v=1.5, gate_step_v=0.5)
        value.update(changes)
        return value

    def scan(self, engine_class=Engine, config=None, table=None):
        hardware = FakeInstrument(drain_current=20e-6, gate_current=0.2e-6)
        engine = engine_class(hardware)
        table = calibration() if table is None else table
        engine.start_scan(self.grid() if config is None else config, table)
        calls = []
        original = model.duty_for_voltage

        def lookup(channel, voltage, **kwargs):
            role = "gate" if channel is table["channels"]["gate"] else "drain"
            calls.append((role, voltage, kwargs.get("allow_limited", False)))
            return original(channel, voltage, **kwargs)

        with patch.object(model, "duty_for_voltage", lookup):
            events = run_engine(engine, hardware)
        self.assertEqual("complete", engine.state, engine.error)
        return engine, hardware, events, calls

    def test_one_gate_lookup_per_curve_and_one_drain_lookup_per_point(self):
        engine, hardware, events, calls = self.scan()
        self.assertEqual([0.5, 1.0, 1.5], [v for role, v, _ in calls if role == "gate"])
        self.assertEqual(9, sum(role == "drain" and limited for role, _, limited in calls))
        self.assertEqual(3, sum(role == "drain" and not limited for role, _, limited in calls))
        self.assertEqual(3, sum(role == "gate" for role, _ in hardware.commands))
        self.assertEqual(9, sum(kind == "point" for kind, _ in events))

    def test_cached_and_uncached_scans_have_identical_commands_timing_and_results(self):
        cached = self.scan()
        uncached = self.scan(UncachedEngine)
        self.assertEqual(cached[1].commands, uncached[1].commands)
        self.assertEqual(cached[1].timed_commands, uncached[1].timed_commands)
        self.assertEqual(cached[1].triggers, uncached[1].triggers)
        self.assertEqual(cached[2], uncached[2])
        self.assertEqual(9, sum(role == "gate" for role, _, _ in uncached[3]))
        self.assertEqual(cached[0].latest, uncached[0].latest)

    def test_single_drain_point_still_computes_each_new_gate_curve(self):
        _, hardware, events, calls = self.scan(config=self.grid(drain_stop_uv=500000))
        self.assertEqual([0.5, 1.0, 1.5], [v for role, v, _ in calls if role == "gate"])
        points = [row for kind, row in events if kind == "point"]
        self.assertEqual([0, 1, 2], [row["curve"] for row in points])
        for expected, row in zip((0.5, 1.0, 1.5), points):
            self.assertAlmostEqual(expected, row["vgs"], places=12)
        self.assertEqual(3, sum(role == "gate" for role, _ in hardware.commands))

    def first_point(self, engine, hardware):
        for now in range(10000):
            hardware.now = now
            events = engine.tick(now)
            if any(kind == "point" for kind, _ in events):
                return
            self.assertEqual("scanning", engine.state, engine.error)
        self.fail("No point completed")

    def test_cancel_restart_with_new_calibration_and_source_recomputes_curve_zero(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        config = self.grid(gate_stop_uv=500000)
        engine.start_scan(config, calibration())
        self.first_point(engine, hardware)
        first_gate_duty = next(duty for role, duty in hardware.commands if role == "gate")
        engine.cancel()
        self.assertEqual("cancelled", engine.state)
        table = deepcopy(calibration())
        for point in table["channels"]["gate"]["points"]:
            point["duty"] /= 2
        hardware.voltage_gain["gate"] = 2.0
        hardware.commands.clear()
        config["source_voltage_uv"] = 1000000
        engine.start_scan(config, table)
        events = run_engine(engine, hardware)
        self.assertEqual("complete", engine.state, engine.error)
        gate_duty = next(duty for role, duty in hardware.commands if role == "gate")
        self.assertNotEqual(first_gate_duty, gate_duty)
        self.assertEqual(1.5 / (2 * model.PWM_FULL_SCALE_V), gate_duty)
        self.assertTrue(all(abs(row["vgs"] - 0.5) < 1e-12
                            for kind, row in events if kind == "point"))

    def test_fault_then_restart_never_reuses_previous_curve_zero_duty(self):
        for failure in (OSError("sensor offline"),
                        OverrangeError("gate", "current_limit", None, 1,
                                       model.GATE_CURRENT_LIMIT_A, "A",
                                       model.GATE_CURRENT_LIMIT_A * 2)):
            with self.subTest(failure=type(failure).__name__):
                hardware = FakeInstrument()
                engine = Engine(hardware)
                engine.start_scan(self.grid(gate_stop_uv=500000), calibration())
                self.first_point(engine, hardware)
                hardware.read_error = failure
                run_engine(engine, hardware)
                self.assertIn(engine.state, ("error", "partial"))
                self.assertEqual({"drain": 0.0, "gate": 0.0}, hardware.duties)
                hardware.read_error = None
                hardware.commands.clear()
                engine.start_scan(self.grid(gate_start_uv=1250000, gate_stop_uv=1250000), calibration())
                events = run_engine(engine, hardware)
                self.assertEqual("complete", engine.state, engine.error)
                self.assertEqual(1.25 / model.PWM_FULL_SCALE_V,
                                 next(duty for role, duty in hardware.commands if role == "gate"))
                self.assertTrue(all(abs(row["vgs"] - 1.25) < 1e-12
                                    for kind, row in events if kind == "point"))

    def test_calibration_bypasses_scan_cache_and_following_scan_recomputes(self):
        hardware = FakeInstrument()
        engine = Engine(hardware)
        config = self.grid(gate_stop_uv=500000)
        engine.start_scan(config, calibration())
        self.first_point(engine, hardware)
        engine.cancel()
        hardware.commands.clear()
        hardware.voltage_gain["gate"] = 1.25
        engine.start_calibration(config)
        with patch.object(model, "duty_for_voltage", side_effect=AssertionError("Calibration used scan lookup")):
            run_engine(engine, hardware)
        self.assertEqual("complete", engine.state, engine.error)
        self.assertEqual(0.0, next(duty for role, duty in hardware.commands if role == "gate"))
        self.assertGreater(len(set(duty for role, duty in hardware.commands if role == "gate")), 5)
        expected = model.duty_for_voltage(engine.calibration["channels"]["gate"], 0.5,
                                          allow_limited=True)
        hardware.commands.clear()
        engine.start_scan(config, engine.calibration)
        events = run_engine(engine, hardware)
        self.assertEqual("complete", engine.state, engine.error)
        self.assertEqual(expected, next(duty for role, duty in hardware.commands if role == "gate"))
        self.assertTrue(all(abs(row["vgs"] - 0.5) < 0.02
                            for kind, row in events if kind == "point"))


if __name__ == "__main__":
    unittest.main()
