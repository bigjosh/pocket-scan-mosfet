"""Visible, cancelable RAM preparation with no hardware or filesystem access."""
import unittest
from unittest.mock import patch
import test_ram_ui as ram_ui


class ScanPreparationTests(unittest.TestCase):
    setUp = ram_ui.RamUITests.setUp
    tick = ram_ui.RamUITests.tick

    def test_preparation_paints_before_allocation_and_scan_paints_before_drive(self):
        order = []
        begin = self.cloud.begin_scan
        def checked_begin(*args):
            self.assertEqual(self.app.screen, "preparing")
            self.assertIn("Preparing scan", self.display.labels)
            self.assertFalse(self.app.dirty)
            self.assertEqual(self.instrument.commands, [])
            order.append("begin")
            return begin(*args)
        self.cloud.begin_scan = checked_begin
        self.tick("right")
        self.assertEqual(order, [])
        self.assertEqual(self.app._scan_prep, 1)
        self.assertEqual([hit[4] for hit in self.app.hits], ["left"])
        self.tick()
        self.assertEqual(order, ["begin"])
        self.assertEqual(self.app._scan_prep, 2)
        self.tick()
        self.assertEqual(self.app._scan_prep, 3)
        self.tick()
        self.assertEqual(self.app.screen, "scan")
        self.assertIn("SCANNING", self.display.labels)
        self.assertEqual(self.instrument.commands, [])
        self.tick()
        self.assertTrue(self.instrument.commands)
        self.assertEqual(self.store.writes, 0)

    def test_cancel_between_each_preparation_stage_never_drives_or_uploads(self):
        for stages in range(4):
            with self.subTest(stages=stages):
                self.setUp()
                self.tick("right")
                for _ in range(stages):
                    self.tick()
                self.tick("left")
                self.assertEqual(self.restarts, [True])
                self.assertEqual(self.instrument.commands, [])
                self.assertNotIn("finish", self.cloud.calls)
                self.assertEqual(self.store.writes, 0)

    def test_preparation_allocation_failure_never_drives_and_buttons_restart(self):
        self.cloud.begin_scan = lambda *args: (_ for _ in ()).throw(MemoryError("private context"))
        with patch("builtins.print"):
            self.tick("right")
            self.tick()
        self.assertEqual(self.app.screen, "scan_error")
        self.assertEqual(self.instrument.commands, [])
        self.assertNotIn("private context", self.app.notice)
        self.tick("right")
        self.assertEqual(self.restarts, [True])


if __name__ == "__main__":
    unittest.main()
