"""Replay the zero-output native actual-voltage integration helper on the host."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import types
import unittest
from unittest.mock import patch

from firmware import archive, cloud, engine, model, ui
from test_hardware import MachineFake
from test_ui import FakeDisplay

ROOT = Path(__file__).resolve().parents[1]


class ActualTargetProbeTests(unittest.TestCase):
    def test_native_helper_keeps_actual_data_plot_gaps_and_all_physical_pins_low(self):
        fake = MachineFake()

        class HostArchive(archive.Archive):
            def __init__(self, directory, device_id):
                super().__init__(directory, device_id, free_bytes=lambda: 1000000)

        class Offline:
            def pause(self):
                return True

        main = types.SimpleNamespace(Pin=fake.module.Pin, Engine=engine.Engine,
            App=ui.App, Archive=HostArchive, CloudManager=cloud.CloudManager,
            WifiManager=Offline, model=model,
            _plot_buffers=(bytearray(32768), bytearray(8192), bytearray(4096)))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scans').mkdir()
            existing = {name: b'preserve original data' for name in
                        ('settings.json', 'calibration.json', 'wifi.json', 'cloud.json',
                         'scans/cafe-01.pending', 'scans/cafe-02.sent', 'scans/cafe-03.draft')}
            for name, value in existing.items():
                (root / name).write_bytes(value)
            previous = os.getcwd()
            output = io.StringIO()
            try:
                os.chdir(folder)
                with patch.dict('sys.modules', {'main': main, 'archive': archive, 'ui': ui}), contextlib.redirect_stdout(output):
                    runpy.run_path(str(ROOT / 'prototype/firmware_actual_target_test.py'),
                                   init_globals={'display': FakeDisplay()})
            finally:
                os.chdir(previous)
            for name, value in existing.items():
                self.assertEqual(value, (root / name).read_bytes())
            self.assertFalse((root / '__actual_target_probe').exists())
            self.assertTrue(fake.events)
            self.assertTrue(all(event[0] == 'pin' and event[1] in (20, 21) and event[3] == 0
                                for event in fake.events))
            report = json.loads(next(line.split(' ', 1)[1] for line in output.getvalue().splitlines()
                                     if line.startswith('ACTUAL_TARGET_NATIVE ')))
            self.assertEqual(8, report['completed_points'])
            self.assertEqual(3, report['redraw_line_segments'])
            self.assertTrue(report['accepted_capture_preserves_actual_values'])
            self.assertTrue(report['physical_outputs_always_low'])


if __name__ == '__main__':
    unittest.main()
