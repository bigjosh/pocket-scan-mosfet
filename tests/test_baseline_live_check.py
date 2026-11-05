"""Run the physical-check orchestration against synthetic peripherals only."""

import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from firmware import current_baseline as cb, model
from tests.test_current_baseline import CodeInstrument


ROOT = Path(__file__).resolve().parents[1]


class LiveCheckTests(unittest.TestCase):
    def fixture(self, directory):
        clock = types.SimpleNamespace(now=0, instrument=None)

        def ticks_ms():
            if clock.instrument is not None:
                clock.instrument.now = clock.now
            return clock.now

        def sleep_ms(value):
            clock.now += value

        fake_time = types.SimpleNamespace(ticks_ms=ticks_ms, sleep_ms=sleep_ms,
                                         ticks_diff=lambda now, before: now - before)

        class Pin:
            OUT = 1
            levels = {20: 0, 21: 0}

            def __init__(self, number, mode=None, value=None):
                self.number = number
                if value is not None:
                    self.levels[number] = value

            def value(self):
                return self.levels[self.number]

        class Instrument(CodeInstrument):
            def __init__(self, timing=None):
                super().__init__()
                clock.instrument = self
                self.closed = False

            def close(self):
                self.zero()
                self.closed = True

        class Display:
            def __init__(self, **kwargs):
                self.paints = 0

            def fill(self, color):
                self.paints += 1

            def text(self, *args, **kwargs):
                pass

        class Inputs:
            def poll(self, now):
                return ()

        class Wifi:
            def pause(self):
                pass

        config = dict(model.DEFAULT_SETTINGS)
        config["ina_average_count"] = 4
        main = types.SimpleNamespace(
            _baseline_bytes=bytearray(cb.BUFFER_BYTES),
            _baseline_workspace=bytearray(cb.WORKSPACE_BYTES),
            _display_buffer=bytearray(1),
            _scan_timing=types.SimpleNamespace(stop=lambda: None),
            BG=0, INK=1, RED=2, Display=Display, Inputs=Inputs,
            Instrument=Instrument, WifiManager=Wifi,
            storage=types.SimpleNamespace(load_settings=lambda migrate: dict(config)))
        main.create_calibrator = lambda instrument, settings: cb.Calibrator(
            instrument, settings, buffer=main._baseline_workspace, clock_ms=ticks_ms)
        modules = {"main": main, "time": fake_time,
                   "gc": types.SimpleNamespace(collect=lambda: None, mem_free=lambda: 50000),
                   "machine": types.SimpleNamespace(Pin=Pin), "current_baseline": cb,
                   "model": model}
        spec = importlib.util.spec_from_file_location("baseline_live_check_host", ROOT / "prototype/baseline_live_check.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", modules):
            spec.loader.exec_module(module)
        module._STAGE = str(Path(directory) / "stage.bin")
        return module, clock, main

    def test_stages_reload_checks_twelve_channel_results_and_commits_once(self):
        with tempfile.TemporaryDirectory() as directory:
            module, clock, main = self.fixture(directory)
            path = str(Path(directory) / "current.bin")
            Path(path).write_bytes(b"previous baseline")
            output = io.StringIO()
            with patch.object(cb, "PATH", path), contextlib.redirect_stdout(output):
                module.run()
            text = output.getvalue()
            self.assertIn("BASELINE_LIVE_PASS", text)
            self.assertEqual(text.count("BASELINE_LIVE_RESIDUAL "), 14)
            self.assertEqual(text.count("BASELINE_LIVE_SAVED "), 1)
            self.assertIn("BASELINE_LIVE_CLEANUP True", text)
            self.assertTrue(clock.instrument.closed)
            self.assertEqual(clock.instrument.duties, [0, 0])
            self.assertFalse(Path(module._STAGE).exists())
            loaded = cb.Baseline()
            self.assertTrue(loaded.load(path))
            self.assertTrue(loaded.compatible(main.storage.load_settings(False), clock.instrument.identity))
            self.assertGreater(loaded.count(0), 2)
            self.assertGreater(loaded.count(1), 2)
            self.assertIn("gate", loaded.metadata(main.storage.load_settings(False)))

    def test_residual_transport_failure_preserves_old_file_and_cleans_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            module, clock, _ = self.fixture(directory)
            path = str(Path(directory) / "current.bin")
            original = b"previous baseline remains untouched"
            Path(path).write_bytes(original)
            with patch.object(cb, "PATH", path), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(module.LiveCheck, "_read_block", side_effect=OSError("test transport fault")):
                    with self.assertRaises(OSError):
                        module.run()
            self.assertEqual(Path(path).read_bytes(), original)
            self.assertFalse(Path(module._STAGE).exists())
            self.assertTrue(clock.instrument.closed)
            self.assertEqual(clock.instrument.duties, [0, 0])

    def test_cancel_never_replaces_committed_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            module, clock, main = self.fixture(directory)
            path = str(Path(directory) / "current.bin")
            original = b"previous baseline"
            Path(path).write_bytes(original)

            class CancelInputs:
                def poll(self, now):
                    return (("left",),) if now > 1000 else ()

            main.Inputs = CancelInputs
            with patch.object(cb, "PATH", path), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(module.Cancelled):
                    module.run()
            self.assertEqual(Path(path).read_bytes(), original)
            self.assertTrue(clock.instrument.closed)

    def test_requested_pairs_stay_within_configured_rail(self):
        with tempfile.TemporaryDirectory() as directory:
            module, _, _ = self.fixture(directory)
            config = dict(model.DEFAULT_SETTINGS)
            config["driver_supply_uv"] = 5000000
            for index in range(module._PAIRS):
                drain, gate = module._target_uv(config, index)
                self.assertTrue(0 <= drain <= 5000000)
                self.assertTrue(0 <= gate <= 5000000)

    def test_residual_values_correct_only_drain_and_report_unmodified_raw_gate(self):
        class VoltageDependentTable:
            def __init__(self):
                self.lookups = []
                self.drive_calls = []

            def drive_duty(self, role, voltage, settings):
                self.drive_calls.append(role)
                return 0

            def offset_nv(self, role, bus_uv):
                if role != 0:
                    raise AssertionError("Raw gate current must never consult its baseline")
                self.lookups.append((role, bus_uv))
                return bus_uv // 800

        for sign in (-1, 1):
            with self.subTest(sign=sign), tempfile.TemporaryDirectory() as directory:
                module, _, main = self.fixture(directory)
                check = module.LiveCheck(main.Instrument(), main.Inputs(), main.Display(),
                                         main.storage.load_settings(False))
                check.table = VoltageDependentTable()

                def read_block(drain_duty, gate_duty):
                    # Drain: 4 V, 10 uV; gate: 1 V, +/-100 uV across the fitted shunt.
                    check.codes[0], check.codes[1] = 3200, 4
                    check.codes[2], check.codes[3] = 5120, sign * 320

                output = io.StringIO()
                with patch.object(check, "_read_block", side_effect=read_block), \
                        contextlib.redirect_stdout(output):
                    check.residuals()
                rows = [line.split() for line in output.getvalue().splitlines()
                        if line.startswith("BASELINE_LIVE_RESIDUAL ")]
                self.assertEqual(len(rows), module._PAIRS * 2)
                for row in rows:
                    terminal, relative, raw, reported = map(float, row[3:7])
                    if row[2] == "gate":
                        self.assertAlmostEqual(terminal, 1 - sign * 0.0001)
                        self.assertAlmostEqual(raw, sign * 100 / model.GATE_SHUNT_OHM)
                        self.assertEqual(reported, raw)
                        self.assertEqual(row[7], "raw")
                    else:
                        self.assertAlmostEqual(terminal, 3.99999)
                        self.assertAlmostEqual(raw, 10 / model.DRAIN_SHUNT_OHM)
                        self.assertAlmostEqual(reported, 5 / model.DRAIN_SHUNT_OHM)
                        self.assertEqual(row[7], "baseline_corrected")
                    self.assertAlmostEqual(relative, terminal - 3.3)
                    self.assertEqual(int(row[8]), 16)
                self.assertEqual(set(check.table.lookups), {(0, 4000000)})
                self.assertEqual(check.table.drive_calls.count("drain"), module._PAIRS)
                self.assertEqual(check.table.drive_calls.count("gate"), module._PAIRS)
                self.assertAlmostEqual(check.maximum_residual_ua[1], 100 / model.GATE_SHUNT_OHM)
                self.assertIn("raw_uA reported_uA basis blocks", output.getvalue())
                self.assertIn("gate_raw", output.getvalue())


if __name__ == "__main__":
    unittest.main()
