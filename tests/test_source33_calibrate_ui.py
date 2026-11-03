"""Native calibration helper selects/persists hardware averaging via real UI."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_offset_calibration import OffsetInstrument, calibrate, offset_settings
from test_ram_ui import RamCloud, RamWifi
from test_ui import FakeDisplay, MemoryStore


class SourceCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scanner, _ = calibrate(OffsetInstrument())
        assert scanner.state == "complete", scanner.error
        cls.calibration = scanner.calibration

    def setUp(self):
        self.now, self.cancel = 0, False
        self.settings = offset_settings()
        self.original = dict(self.settings)
        self.hardware = OffsetInstrument()
        self.hardware.close = self.hardware.zero
        self.pins = []
        owner = self
        class Store(MemoryStore):
            json = json
            def __init__(self):
                super().__init__()
                self.settings = dict(owner.settings)
                self.files["calibration.bin"] = copy.deepcopy(owner.calibration)
            def load_settings(self, migrate=False):
                assert migrate is False
                return dict(self.settings)
            def begin_calibration_atomic(self):
                store = self
                class Writer:
                    def commit(self, value):
                        store.save_calibration_atomic(value)
                    def abort(self):
                        pass
                return Writer()
        self.store = Store()
        def sleep(delay):
            self.now += delay
            self.hardware.now = self.now
        def poll(now):
            return [("left",)] if self.cancel else []
        class Pin:
            OUT = 1
            def __init__(self, number, mode=None, value=None):
                if mode is not None:
                    owner.pins.append((number, value))
            def value(self):
                return 0
        def cloud(buffer, **kwargs):
            value = RamCloud(buffer)
            value.prepare_calibration = lambda calibration: None
            return value
        self.main = types.SimpleNamespace(
            App=App, model=model, Engine=Engine, storage=self.store,
            Instrument=lambda: self.hardware, Display=lambda **kwargs: FakeDisplay(),
            Inputs=lambda: types.SimpleNamespace(poll=poll), WifiManager=RamWifi,
            RamCloudManager=cloud, PackedScanBuffer=PackedScanBuffer, Pin=Pin,
            _scan_bytes=bytearray(4096*17), _display_buffer=None, _upload_transport=None,
            scan_utc=lambda: None, record_error=lambda context,error: None,
            gc=types.SimpleNamespace(collect=lambda: None, mem_free=lambda: 100000),
            time=types.SimpleNamespace(ticks_ms=lambda:self.now, ticks_diff=lambda a,b:a-b,
                                      sleep_ms=sleep))
        path=Path(__file__).resolve().parents[1]/"prototype"/"source33_calibrate_ui.py"
        spec=importlib.util.spec_from_file_location("source33_calibrate_ui_test",path)
        self.helper=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{"main":self.main}):
            spec.loader.exec_module(self.helper)

    def test_real_averaging_calibrate_flow_saves_only_chosen_count_and_new_metadata(self):
        self.helper.CALIBRATION_AVERAGES=16
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.helper.run()
        expected=dict(self.original,ina_average_count=16)
        self.assertEqual(self.store.settings,expected)
        value=self.store.files["calibration.bin"]
        model.validate_calibration(value,expected)
        self.assertEqual(value["metadata"]["calibration_repeat_blocks"],3)
        self.assertEqual(self.store.writes,2)  # Settings commit and completed calibration.
        self.assertIn("SOURCE33_CAL_SAVED",output.getvalue())
        self.assertEqual(self.pins[-2:],[(21,0),(20,0)])
        self.assertEqual(self.hardware.duties,{"drain":0.0,"gate":0.0})

    def test_zero_fixture_cancel_does_not_write_settings_or_candidate(self):
        self.cancel=True
        with contextlib.redirect_stdout(io.StringIO()):
            self.helper.run()
        self.assertEqual(self.store.settings,self.original)
        self.assertEqual(self.store.writes,0)
        self.assertTrue(all(duty == 0 for role,duty in self.hardware.commands))
        self.assertEqual(self.pins[-2:],[(21,0),(20,0)])

    def test_wrong_source_rejects_before_any_settings_or_output_changes(self):
        self.store.settings["source_voltage_uv"]=0
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError,"3.3 V source"):
            self.helper.run()
        self.assertEqual(self.store.writes,0)
        self.assertEqual(self.hardware.commands,[])


if __name__ == "__main__":
    unittest.main()
