"""Visible, cancelable scan preparation and no-drive handoff between ticks."""
import unittest
from unittest.mock import patch

import test_cloud_ui as cloud_ui
from test_storage_ui import StorageCloud


class ScanPreparationTests(unittest.TestCase):
    setUp = cloud_ui.CloudUITests.setUp
    tick = cloud_ui.CloudUITests.tick

    def test_storage_work_waits_for_paint_and_engine_waits_for_scan_paint(self):
        self.cloud.calls.clear()
        engine_ticks = []
        tick = self.engine.tick
        self.engine.tick = lambda now: engine_ticks.append(now) or tick(now)
        begin = self.cloud.begin_scan
        preparation_order = []

        def checked_collect():
            self.assertEqual("preparing", self.app.screen)
            self.assertIn("Preparing scan", self.display.labels)
            self.assertFalse(self.app.dirty)
            self.assertEqual([], engine_ticks)
            self.assertEqual([], self.instrument.commands)
            preparation_order.append("collect")

        collector = patch("firmware.ui.gc.collect", side_effect=checked_collect)
        collector.start()
        self.addCleanup(collector.stop)

        def checked_begin(*args):
            self.assertIn("Preparing scan", self.display.labels)
            self.assertFalse(self.app.dirty)
            self.assertEqual([], engine_ticks)
            self.assertEqual([], self.instrument.commands)
            self.assertEqual(["collect"], preparation_order)
            preparation_order.append("begin")
            return begin(*args)

        self.cloud.begin_scan = checked_begin
        pause = self.cloud.pause

        def checked_pause():
            self.assertEqual("home", self.app.screen)
            self.assertIn("SCAN", self.display.labels)
            return pause()

        self.cloud.pause = checked_pause
        self.tick((("right",),))
        self.assertEqual("preparing", self.app.screen)
        self.assertEqual(["pause"], self.cloud.calls)
        self.assertEqual([], preparation_order)
        self.assertIn("CANCEL", self.display.labels)
        self.assertIn("OK", self.display.labels)
        self.assertTrue(any(hit[4] == "left" for hit in self.app.hits))
        self.assertFalse(any(hit[4] == "right" for hit in self.app.hits))
        self.app.action("right")  # Disabled physical key as well as touch.
        self.app.action("right")
        self.assertEqual(1, self.app._scan_prep)
        self.tick()  # Archive, then a new input opportunity.
        self.assertEqual(2, self.app._scan_prep)
        self.tick()  # Radio shutdown.
        self.assertEqual(3, self.app._scan_prep)
        self.tick()  # Arm and paint, with no engine tick or output command.
        self.assertEqual("scan", self.app.screen)
        self.assertIn("SCANNING", self.display.labels)
        self.assertEqual([], engine_ticks)
        self.assertEqual([], self.instrument.commands)
        self.assertEqual(1, self.cloud.calls.count("begin_scan"))
        self.assertEqual(["collect", "begin"], preparation_order)
        self.tick()
        self.assertEqual(1, len(engine_ticks))
        self.assertTrue(self.instrument.commands)

    def test_cancel_before_paint_and_between_every_stage_never_arms_or_drives(self):
        for ticks in range(4):
            with self.subTest(ticks=ticks):
                self.setUp()
                self.cloud.pending, self.cloud.saved = 3, 3
                self.app.action("right")
                for _ in range(ticks):
                    self.tick()
                self.assertEqual("idle", self.engine.state)
                self.app.action("left")
                self.assertEqual("home", self.app.screen)
                self.assertEqual(0, self.app._scan_prep)
                self.assertEqual([], self.instrument.commands)
                self.assertEqual(0, self.instrument.reads)
                self.assertIsNone(self.cloud.current)
                self.assertEqual(3, self.cloud.pending)
                self.assertEqual(3, self.cloud.saved)
                self.assertNotIn("accept_scan", self.cloud.calls)
                self.assertEqual(int(ticks == 3), self.wifi.calls.count(("resume",)))

    def test_cancel_does_not_resume_preexisting_paused_radio(self):
        self.app._wifi_paused = True
        self.wifi.state = "paused"
        self.app.action("right")
        for _ in range(3):
            self.tick()
        self.app.action("left")
        self.assertTrue(self.app._wifi_paused)
        self.assertNotIn(("resume",), self.wifi.calls)
        self.assertIsNone(self.cloud.current)
        self.assertEqual([], self.instrument.commands)

    def test_cancel_after_scan_paint_runs_before_first_drive(self):
        self.app.action("right")
        for _ in range(4):
            self.tick()
        self.assertEqual("scan", self.app.screen)
        self.assertEqual([], self.instrument.commands)
        self.tick((("left",),))
        self.assertEqual("home", self.app.screen)
        self.assertEqual("cancelled", self.engine.state)
        self.assertEqual([], self.instrument.commands)
        self.assertEqual(0, self.instrument.reads)
        self.assertIsNone(self.cloud.current)

    def test_capacity_failure_uses_authoritative_begin_and_never_pauses_wifi(self):
        self.app.cloud = self.cloud = StorageCloud()
        self.app.action("right")
        self.tick()
        self.tick()
        self.assertEqual("storage", self.app.screen)
        self.assertEqual(1, self.cloud.calls.count("begin_scan"))
        self.assertEqual([], self.instrument.commands)
        self.assertNotIn(("pause",), self.wifi.calls)
        self.assertEqual("idle", self.engine.state)
        self.assertEqual(0, self.app._scan_prep)

    def test_failed_radio_shutdown_discards_draft_and_never_arms(self):
        self.wifi.pause_error = OSError("PRIVATE_RADIO_SECRET")
        self.app.action("right")
        for _ in range(3):
            self.tick()
        self.assertEqual("notice", self.app.screen)
        self.assertEqual("Wi-Fi could not be disabled", self.app.notice)
        self.assertIsNone(self.cloud.current)
        self.assertEqual([], self.instrument.commands)
        self.assertEqual("idle", self.engine.state)
        self.assertEqual(0, self.app._scan_prep)
        self.assertNotIn("PRIVATE_RADIO_SECRET", repr(self.logs))

    def test_stale_setup_actions_cannot_interrupt_preparation(self):
        self.app.action("right")
        for action in ("wifi", "wifi_scan", "cloud", "storage", ("storage_select", 0),
                       ("edit", "driver_supply_uv", "Driver supply")):
            self.app.action(action)
        self.assertEqual("preparing", self.app.screen)
        self.assertEqual(1, self.app._scan_prep)
        self.assertNotIn(("scan",), self.wifi.calls)
        self.assertEqual([], self.instrument.commands)


if __name__ == "__main__":
    unittest.main()
