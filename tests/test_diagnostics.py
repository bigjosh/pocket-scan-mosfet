"""Bounded persistent fault reporting; no serial or hardware access."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from firmware import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "last_error.log"
        self.path_patch = patch.object(diagnostics, "LOG_PATH", str(self.path))
        self.path_patch.start()
        self.stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        self.stdout = self.stdout_patch.start()

    def tearDown(self):
        self.stdout_patch.stop()
        self.path_patch.stop()
        self.directory.cleanup()

    def test_streams_traceback_and_uptime_to_committed_log(self):
        try:
            raise ValueError("sensor read failed")
        except ValueError as error:
            with patch.object(diagnostics, "_ticks_ms", return_value=1234):
                self.assertTrue(diagnostics.record_error("scan tick", error))
        content = self.path.read_text()
        self.assertIn("Context: scan tick", content)
        self.assertIn("Uptime ms: 1234", content)
        self.assertIn("Traceback", content)
        self.assertIn("ValueError: sensor read failed", content)
        self.assertIn("Scanner error [scan tick]: sensor read failed", self.stdout.getvalue())
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_replaces_previous_log_only_on_new_error(self):
        self.path.write_text("old fault")
        self.assertTrue(diagnostics.record_error("engine", "drain voltage outside envelope"))
        self.assertNotIn("old fault", self.path.read_text())
        self.assertIn("Message: drain voltage outside envelope", self.path.read_text())

    def test_long_unicode_traceback_is_bounded_in_bytes(self):
        self.assertTrue(diagnostics.record_error("render", ValueError("Ω" * 10000)))
        data = self.path.read_bytes()
        self.assertLessEqual(len(data), diagnostics.MAX_LOG_BYTES)
        self.assertTrue(data.endswith(b"[log truncated]\n"))
        self.assertLess(len(self.stdout.getvalue()), 400)

    def test_failed_replace_preserves_old_log_and_removes_temporary(self):
        self.path.write_text("prior committed fault")
        with patch.object(diagnostics.os, "replace", side_effect=OSError("flash unavailable")):
            self.assertFalse(diagnostics.record_error("settings", OSError("write failed")))
        self.assertEqual(self.path.read_text(), "prior committed fault")
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_failed_open_or_usb_print_never_raises(self):
        self.path.write_text("prior committed fault")
        with patch("builtins.open", side_effect=OSError("full")):
            self.assertFalse(diagnostics.record_error("loop", RuntimeError("fault")))
        self.assertEqual(self.path.read_text(), "prior committed fault")
        class DisconnectedUSB(io.StringIO):
            def write(self, data):
                raise OSError("USB disconnected")
        with patch("sys.stdout", DisconnectedUSB()):
            self.assertTrue(diagnostics.record_error("loop", RuntimeError("fault")))

    def test_micropython_exception_printer_receives_binary_capable_stream(self):
        def printer(error, stream):
            stream.write(b"Traceback from MicroPython\n")
            stream.write(b"RuntimeError: device fault\n")
        with patch.object(diagnostics.sys, "print_exception", printer, create=True):
            self.assertTrue(diagnostics.record_error("render", RuntimeError("device fault")))
        self.assertIn("Traceback from MicroPython", self.path.read_text())

    def test_unprintable_exception_fails_without_losing_prior_log(self):
        class BrokenError(Exception):
            def __str__(self):
                raise RuntimeError("broken __str__")
        # The host traceback formatter handles broken __str__ itself.
        self.assertTrue(diagnostics.record_error("render", BrokenError()))
        self.assertIn("<message unavailable>", self.stdout.getvalue())

    def test_memory_failure_building_temp_path_never_replaces_original_error(self):
        class ExhaustedPath(str):
            def __add__(self, suffix):
                raise MemoryError("512 bytes")
        self.path.write_text("prior committed fault")
        with patch.object(diagnostics, "LOG_PATH", ExhaustedPath(str(self.path))):
            self.assertFalse(diagnostics.record_error("calibrating", MemoryError("original")))
        self.assertEqual(self.path.read_text(), "prior committed fault")
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_memory_failure_constructing_writer_preserves_committed_log(self):
        self.path.write_text("prior committed fault")
        with patch.object(diagnostics, "_BoundedWriter", side_effect=MemoryError("512 bytes")):
            self.assertFalse(diagnostics.record_error("calibrating", MemoryError("original")))
        self.assertEqual(self.path.read_text(), "prior committed fault")
        self.assertFalse(Path(str(self.path) + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
