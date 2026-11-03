"""Host checks for the module-scoped native RAM diagnostic guard."""

import builtins
import importlib.util
import io
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import (model, storage, ram_scan, ram_payload, ram_cloud, https_upload,
                      calibration_data, calibration_json, calibration_binary)


def diagnostic(main):
    path = Path(__file__).resolve().parents[1] / "prototype" / "firmware_ram_smoke.py"
    spec = importlib.util.spec_from_file_location("tested_ram_smoke", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, main=main):
        spec.loader.exec_module(module)
    return module


class ReadOnlyNative:
    def __init__(self, module):
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name):
        return getattr(self._module, name)

    def __setattr__(self, name, value):
        raise AttributeError("native module is immutable")


class RamSmokeGuardTests(unittest.TestCase):
    def setUp(self):
        self.main = types.ModuleType("main")
        self.smoke = diagnostic(self.main)

    def test_scoped_proxies_leave_native_modules_untouched_and_delegate_nonfiles(self):
        native = {name: ReadOnlyNative(value) for name, value in
                  (("builtins", builtins), ("io", io), ("os", os))}
        module = types.SimpleNamespace(**native)
        guard = self.smoke.NoFilesystem((module,))
        with patch.multiple(self.smoke, **native):
            guard.install()
        self.assertEqual(guard.attempts, 0)
        for operation in (module.open, module.builtins.open, module.io.open,
                          module.os.open, module.os.remove, module.os.rename, module.os.sync):
            with self.assertRaisesRegex(AssertionError, "filesystem access"):
                operation(None)
        self.assertIs(module.io.IOBase, io.IOBase)
        self.assertIs(module.os.urandom, os.urandom)
        self.assertIs(module.builtins.len, len)
        self.assertIs(native["io"].open, io.open)
        self.assertEqual(guard.restore(), 0)
        self.assertFalse(hasattr(module, "open"))
        for name, value in native.items():
            self.assertIs(getattr(module, name), value)

    def test_partial_install_rolls_back_only_successful_changes_and_keeps_original_error(self):
        class FailingModule:
            os = os

            def __setattr__(self, name, value):
                if name == "os":
                    raise RuntimeError("original install failure")
                object.__setattr__(self, name, value)

        module = FailingModule()
        guard = self.smoke.NoFilesystem((module,))
        with self.assertRaisesRegex(RuntimeError, "original install failure"):
            guard.install()
        self.assertFalse(hasattr(module, "open"))
        self.assertIs(module.os, os)
        self.assertEqual(guard.originals, [])
        self.assertEqual(guard.restore_errors, 0)

    def test_restore_continues_after_a_failed_attribute_cleanup(self):
        class RefuseDelete:
            def __delattr__(self, name):
                raise RuntimeError("cleanup failed")

        first, second = types.SimpleNamespace(open="original"), RefuseDelete()
        guard = self.smoke.NoFilesystem((first, second))
        guard.install()
        with patch.object(self.smoke, "report") as report:
            self.assertEqual(guard.restore(), 1)
        self.assertEqual(first.open, "original")
        self.assertEqual(guard.originals, [])
        report.assert_called_once_with("GUARD_RESTORE_ERROR", {"failed_changes": 1})

    def test_full_real_ram_diagnostic_runs_with_scoped_guard(self):
        self.main.model, self.main.storage = model, storage
        self.main.PackedScanBuffer = ram_scan.PackedScanBuffer
        self.main.RamCloudManager = ram_cloud.RamCloudManager
        self.main.PayloadReader = ram_payload.PayloadReader
        self.main._scan_bytes = bytearray(4096 * 17)
        self.main._display_buffer = bytearray(48 * 1024)
        self.main._tls_reserved = True
        self.main.Pin = lambda number: types.SimpleNamespace(value=lambda: 0)
        self.smoke.RAM_SMOKE_USE_SAVED_CALIBRATION = False
        modules = (self.main, model, storage, ram_scan, ram_payload, ram_cloud, https_upload,
                   calibration_data, calibration_json, calibration_binary)
        guard = self.smoke.NoFilesystem(modules)
        with patch.object(self.smoke, "NoFilesystem", return_value=guard), \
                patch.object(self.smoke, "report") as report:
            self.smoke.run()
        self.assertEqual(guard.attempts, 0)
        self.assertEqual(guard.restore_errors, 0)
        self.assertEqual(report.call_args.args[0], "PASS")
        self.assertEqual(report.call_args.args[1]["remaining_records"], 0)
        self.assertIn("module APIs", report.call_args.args[1]["filesystem_guard_scope"])
        self.assertIs(storage.os, os)
        self.assertIs(ram_payload.io, io)
        for module in modules:
            self.assertFalse(hasattr(module, "open"))

    def test_saved_legacy_map_is_only_a_heap_fixture_and_is_never_relabelled(self):
        legacy = {"version": 2, "metadata": {"batches": 10, "samples_per_batch": 10},
                  "channels": {"drain": {"points": []}, "gate": {"points": []}}}
        self.main.model = model
        self.main.storage = types.SimpleNamespace(load_calibration=lambda: legacy)
        self.main.PackedScanBuffer = ram_scan.PackedScanBuffer
        self.main._scan_bytes, self.main._display_buffer = bytearray(4096 * 17), bytearray(48 * 1024)
        self.main._tls_reserved = True
        self.main.Pin = lambda number: types.SimpleNamespace(value=lambda: 0)
        self.smoke.RAM_SMOKE_INA_AVERAGE_COUNT = 128
        guard = self.smoke.NoFilesystem((self.main, model))

        def probe(buffer, candidate, *args):
            self.assertIsNot(candidate, legacy)
            model.validate_calibration_acquisition(candidate["metadata"], self.smoke.config(1))
            self.assertTrue(candidate["metadata"]["synthetic_no_hardware"])
            self.assertEqual(128, candidate["metadata"]["ina_average_count"])
            self.assertNotIn("acquisition_mode", legacy["metadata"])

        with patch.object(self.smoke, "NoFilesystem", return_value=guard), \
                patch.object(self.smoke, "full_scan", side_effect=probe), \
                patch.object(self.smoke, "failure_case", side_effect=probe), \
                patch.object(self.smoke, "zero_partial", side_effect=probe), \
                patch.object(self.smoke, "report") as report:
            self.smoke.run()
        begin = next(call.args[1] for call in report.call_args_list if call.args[0] == "BEGIN")
        self.assertTrue(begin["saved_calibration_retained_for_heap_only"])
        self.assertEqual("synthetic_no_hardware", begin["payload_calibration"])
        self.assertEqual({"batches": 10, "samples_per_batch": 10}, legacy["metadata"])


if __name__ == "__main__":
    unittest.main()
