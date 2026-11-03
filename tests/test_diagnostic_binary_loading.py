"""Corrupt authoritative calibration must stop diagnostics before hardware."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


class DiagnosticBinaryLoadingTests(unittest.TestCase):
    def test_corrupt_binary_never_falls_back_to_json_or_initializes_hardware(self):
        for name in ("cancel_feedback_smoke", "source33_zero_state_check"):
            with self.subTest(helper=name):
                pins = []

                class Pin:
                    OUT = 1

                    def __init__(self, number, mode, value):
                        pins.append((number, value))

                load = Mock(side_effect=ValueError("binary integrity check failed"))
                legacy = Mock(side_effect=AssertionError("legacy JSON must not be loaded"))
                instrument = Mock(side_effect=AssertionError("hardware must not be initialized"))
                main = types.SimpleNamespace(
                    storage=types.SimpleNamespace(load_settings=lambda **kwargs: {},
                        load_calibration=load, load_json=legacy),
                    Instrument=instrument, Pin=Pin)
                path = Path(__file__).resolve().parents[1] / "prototype" / (name + ".py")
                spec = importlib.util.spec_from_file_location(name + "_binary_test", path)
                helper = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, main=main):
                    spec.loader.exec_module(helper)
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "integrity"):
                    helper.run()
                load.assert_called_once_with()
                legacy.assert_not_called()
                instrument.assert_not_called()
                self.assertEqual(pins, [(21, 0), (20, 0)])


if __name__ == "__main__":
    unittest.main()
