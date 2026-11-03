"""Replay the native partial helper with only fake GPIO and isolated files."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
import types
import unittest
from unittest.mock import patch

from firmware import archive, cloud, engine, model, storage, ui
from test_hardware import MachineFake
from test_ui import FakeDisplay


ROOT = Path(__file__).resolve().parents[1]


class PartialProbeTests(unittest.TestCase):
    def test_zero_only_native_helper_accept_cancel_capture_never_writes_files(self):
        for save in (False,):
            with self.subTest(save=save), tempfile.TemporaryDirectory() as folder:
                fake = MachineFake()
                spec = importlib.util.spec_from_file_location("firmware.hardware", ROOT / "firmware/hardware.py")
                hardware = importlib.util.module_from_spec(spec)
                with patch.dict("sys.modules", {"machine": fake.module}):
                    spec.loader.exec_module(hardware)
                class HostArchive(archive.Archive):
                    def __init__(self, directory="scans", device_id="abcd", **kwargs):
                        super().__init__(directory, device_id, free_bytes=lambda: 1000000, **kwargs)
                def display(raster_pool):
                    self.assertIs(raster_pool, main._display_buffer)
                    self.assertEqual(49152, len(raster_pool))
                    return FakeDisplay()
                main = types.SimpleNamespace(Pin=fake.module.Pin, Instrument=hardware.Instrument,
                    Display=display, Engine=engine.Engine, App=ui.App, Archive=HostArchive,
                    CloudManager=cloud.CloudManager, model=model, storage=storage,
                    _display_buffer=bytearray(49152),
                    _plot_buffers=(bytearray(32768), bytearray(8192), bytearray(4096)))
                root = Path(folder)
                (root / "scans").mkdir()
                existing = {name: b"private preserved fixture" for name in
                            ("settings.json", "calibration.json", "wifi.json", "cloud.json",
                             "scans/cafe-01.pending", "scans/cafe-02.sent", "scans/cafe-03.draft")}
                for name, data in existing.items():
                    (root / name).write_bytes(data)
                previous = os.getcwd()
                output = io.StringIO()
                try:
                    os.chdir(folder)
                    with patch.dict("sys.modules", {"main": main, "archive": archive}), contextlib.redirect_stdout(output):
                        runpy.run_path(str(ROOT / "prototype/firmware_partial_ui_test.py"),
                                       init_globals={"SAVE_UPLOAD_FIXTURE": save})
                finally:
                    os.chdir(previous)
                for name, data in existing.items():
                    self.assertEqual(data, (root / name).read_bytes())
                self.assertFalse((root / "__partial_ui_probe").exists())
                self.assertTrue(fake.events)
                self.assertTrue(all(event[0] == "pin" and event[1] in (20, 21) and event[3] == 0
                                    for event in fake.events))
                report = json.loads(next(line.split(" ", 1)[1] for line in output.getvalue().splitlines()
                                         if line.startswith("PARTIAL_UI_NATIVE ")))
                self.assertTrue(report["physical_outputs_always_low"])
                self.assertTrue(report["one_point_partial_captured"])
                self.assertTrue(report["zero_point_partial_captured"])
                self.assertTrue(report["no_files_or_network"])
                self.assertEqual(sorted(existing), sorted(str(path.relative_to(root)).replace("\\", "/")
                    for path in root.rglob("*") if path.is_file()))



if __name__ == "__main__":
    unittest.main()
