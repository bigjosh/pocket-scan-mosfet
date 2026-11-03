"""Raw drive diagnostics retain production limits and cancellation/cleanup."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine, OverrangeError
from test_offset_calibration import OffsetInstrument, calibrate, offset_settings
from test_ui import FakeDisplay


class DriveProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.2867
        scanner, _ = calibrate(hardware)
        assert scanner.state == "complete", scanner.error
        cls.calibration = scanner.calibration

    def setUp(self):
        self.now, self.polls = 0, 0
        self.order, self.pins = [], []
        self.cancel_at = None
        self.hardware = OffsetInstrument()
        self.hardware.floors["drain"] = 3.278
        self.settings = offset_settings()
        owner = self
        class PWM:
            def __init__(self, role):
                self.role = role
            def duty_u16(self):
                return int(owner.hardware.duties[self.role] * 65535 + 0.5)
            def freq(self):
                return 100000
        self.hardware._pwms = {role: PWM(role) for role in ("drain", "gate")}
        def close():
            self.order.append("close")
            self.hardware.zero()
        self.hardware.close = close
        def load_settings(**kwargs):
            self.assertEqual(kwargs, {"migrate": False})
            self.order.append("settings")
            return self.settings
        def load_calibration():
            self.order.append("calibration")
            return self.calibration
        def create_display(**kwargs):
            self.order.append("display")
            return FakeDisplay()
        def create_instrument():
            self.order.append("instrument")
            return self.hardware
        def poll(now):
            self.polls += 1
            return [("left",)] if self.polls == self.cancel_at else []
        def create_inputs():
            self.order.append("inputs")
            return types.SimpleNamespace(poll=poll)
        def pause():
            self.order.append("pause")
            return True
        def create_wifi():
            self.order.append("wifi")
            return types.SimpleNamespace(pause=pause)
        def sleep(delay):
            self.now += delay
            self.hardware.now = self.now
        class Pin:
            OUT = 1
            def __init__(self, number, mode, value):
                owner.pins.append((number, value))
        self.main = types.SimpleNamespace(
            model=model, Engine=Engine, Display=create_display, Instrument=create_instrument,
            Inputs=create_inputs, WifiManager=create_wifi, Pin=Pin, _display_buffer=None, BG=0, INK=1,
            storage=types.SimpleNamespace(load_settings=load_settings, load_calibration=load_calibration, json=json),
            gc=types.SimpleNamespace(collect=lambda: None),
            time=types.SimpleNamespace(ticks_ms=lambda: self.now, ticks_diff=lambda a,b:a-b, sleep_ms=sleep))
        path = Path(__file__).resolve().parents[1] / "prototype" / "source33_drive_probe.py"
        spec = importlib.util.spec_from_file_location("source33_drive_probe_test", path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"main": self.main}):
            spec.loader.exec_module(self.helper)

    def test_full_fixture_and_bounded_raw_statistics_even_below_baseline_coverage(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.open", side_effect=AssertionError("unexpected filesystem")):
            self.helper.run()
        rows = [(line.split(" ",1)[0], json.loads(line.split(" ",1)[1])) for line in output.getvalue().splitlines()]
        commands = [row for kind,row in rows if kind == "DRIVE_PROBE_COMMAND"]
        measured = [row for kind,row in rows if kind == "DRIVE_PROBE_MEASURE"]
        self.assertEqual(len(commands), 7)
        self.assertEqual(len(measured), 14)
        self.assertEqual(self.hardware.reads, 7 * (100 + 80))
        self.assertEqual(self.order[:7], ["settings","calibration","display","instrument","inputs","wifi","pause"])
        self.assertAlmostEqual(measured[0]["terminal_mean_v"], 3.278, places=6)
        self.assertEqual(commands[0]["channels"]["drain"]["readback_u16"], 0)
        self.assertGreater(commands[1]["channels"]["drain"]["command_u16"], 0)
        self.assertEqual(rows[-1][0], "DRIVE_PROBE_COMPLETE")
        self.assertEqual(self.pins, [(21,0),(20,0)])

    def test_cancel_during_settling_stops_without_measurement_or_completion(self):
        self.cancel_at = 4
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.helper.run()
        self.assertIn("DRIVE_PROBE_CANCELLED", output.getvalue())
        self.assertNotIn("DRIVE_PROBE_MEASURE", output.getvalue())
        self.assertNotIn("DRIVE_PROBE_COMPLETE", output.getvalue())
        self.assertEqual(self.pins, [(21,0),(20,0)])
        self.assertEqual(self.hardware.duties, {"drain":0.0,"gate":0.0})

    def test_raw_current_trip_is_active_during_settling(self):
        self.hardware.current["drain"] = model.DRAIN_CURRENT_LIMIT_A * 2
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(OverrangeError):
            self.helper.run()
        self.assertEqual(self.hardware.reads, 1)
        self.assertEqual(self.pins, [(21,0),(20,0)])

    def test_configured_absolute_envelope_is_enforced(self):
        self.hardware.floors["drain"] = 25
        self.hardware.voltage_ceiling["drain"] = 25
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "envelope"):
            self.helper.run()
        self.assertEqual(self.hardware.reads, 1)
        self.assertEqual(self.pins, [(21,0),(20,0)])

    def test_wrong_source_rejected_before_any_hardware_initialization(self):
        self.settings["source_voltage_uv"] = 0
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "3.3 V source"):
            self.helper.run()
        self.assertEqual(self.order, ["settings", "calibration"])
        self.assertEqual(self.hardware.commands, [])
        self.assertEqual(self.pins, [(21,0),(20,0)])


if __name__ == "__main__":
    unittest.main()
