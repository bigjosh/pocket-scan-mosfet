"""Incremental upload stages distinguish bytes sent from confirmed storage."""
import unittest
from unittest.mock import patch

from firmware.ui import BG, INK, UPLOAD_DONE_MS, UPLOAD_FAILURE, UPLOAD_STAGES
from test_incremental_render import RecordingDisplay
import test_ram_ui as ram_ui


class UploadProgressUITests(unittest.TestCase):
    setUp = ram_ui.RamUITests.setUp
    tick = ram_ui.RamUITests.tick
    until = ram_ui.RamUITests.until
    start = ram_ui.RamUITests.start

    def progress_screen(self):
        self.display = RecordingDisplay()
        self.app.d = self.display
        self.cloud.upload_stage = self.cloud.upload_sent = 0
        self.cloud.upload_total = 1000
        self.cloud.state = "preparing"
        self.app.goto("upload")
        self.app.render()
        self.display.calls.clear()

    def test_each_stage_starts_pending_and_checks_follow_actual_progress(self):
        self.progress_screen()
        self.assertEqual(self.app._upload_marks, [1, 0, 0, 0])
        self.assertTrue(all(label in self.display.labels for label in UPLOAD_STAGES))
        self.assertEqual([hit[4] for hit in self.app.hits], ["left"])
        for stage, state, expected in ((1, "waiting_wifi", [2, 1, 0, 0]),
                                       (2, "uploading", [2, 2, 1, 0]),
                                       (3, "uploading", [2, 2, 2, 1]),
                                       (4, "complete", [2, 2, 2, 2])):
            self.cloud.upload_stage, self.cloud.state = stage, state
            self.app._render_upload_progress()
            self.assertEqual(self.app._upload_marks, expected)
        self.assertFalse(any(call[0] == "fill" for call in self.display.calls))

    def test_each_chunk_recomputes_percent_but_unchanged_regions_are_not_redrawn(self):
        self.progress_screen()
        cache = self.app._upload_marks
        self.cloud.upload_stage, self.cloud.state = 2, "uploading"
        self.app._render_upload_progress()
        self.display.calls.clear()
        for sent in (1, 2, 9):
            self.cloud.upload_sent = sent
            self.app._render_upload_progress()
        self.assertEqual(self.display.calls, [])
        self.cloud.upload_sent = 10
        self.app._render_upload_progress()
        self.assertEqual(self.display.calls, [("fill_rect", 368, 146, 96, 16, BG),
                                             ("text", "1%", 368, 146, INK, 2)])
        self.display.calls.clear()
        self.cloud.upload_sent = self.cloud.upload_total
        self.cloud.upload_stage = 3
        self.app._render_upload_progress()
        self.assertEqual(self.app._upload_percent, 100)
        self.assertEqual(self.app._upload_marks, [2, 2, 2, 1])
        self.assertEqual(self.restarts, [])
        self.assertIs(self.app._upload_marks, cache)

    def test_failure_marks_only_active_stage_and_keeps_prior_checks(self):
        for stage in range(4):
            with self.subTest(stage=stage):
                self.setUp()
                self.progress_screen()
                self.cloud.upload_stage = stage
                self.app._ram_upload_error()
                self.app.render()
                self.assertEqual(self.app._upload_marks, [2] * stage + [3] + [0] * (3 - stage))
                self.assertEqual(self.app.notice, UPLOAD_FAILURE)
                self.assertIn("UPLOAD FAILED", self.display.labels)
                self.assertEqual([hit[4] for hit in self.app.hits], ["left", "right"])

    def test_confirmed_check_is_painted_before_timed_restart_without_more_network(self):
        self.start()
        self.until(lambda: self.cloud.state == "complete")
        self.assertEqual(self.app.screen, "upload")
        self.assertEqual(self.app._upload_marks, [2, 2, 2, 2])
        self.assertEqual(self.restarts, [])
        calls = list(self.cloud.calls)
        confirmed_at = self.now
        while self.now - confirmed_at < UPLOAD_DONE_MS - 20:
            self.tick()
            self.assertEqual(self.restarts, [])
            self.assertEqual(self.cloud.calls, calls)
        self.tick()
        self.assertEqual(self.restarts, [True])
        self.assertEqual(len(self.buffer), 0)

    def test_cancel_remains_responsive_during_confirmed_display(self):
        self.start()
        self.until(lambda: self.cloud.state == "complete")
        self.tick("left")
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.cloud.state, "idle")
        self.assertEqual(len(self.buffer), 0)

    def test_confirmed_display_deadline_handles_native_tick_wrap(self):
        self.start()
        self.until(lambda: self.cloud.state == "complete")
        period = 1 << 30
        self.app._upload_complete_ms = period - 200
        diff = lambda now, before: (now - before + period // 2) % period - period // 2
        with patch("firmware.ui._ticks_diff", side_effect=diff):
            self.app.tick(300)
            self.assertEqual(self.restarts, [])
            self.app.tick(400)
            self.assertEqual(self.restarts, [True])


if __name__ == "__main__":
    unittest.main()
