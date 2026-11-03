"""Explicit per-scan deletion, capacity routing, and no-drive storage UI."""

import unittest

import test_cloud_ui as cloud_ui


def entry(index, status="pending", size=60):
    return {"scan_id": "abcd-%016x" % index, "status": status,
            "size_bytes": size, "point_count": 2, "created_utc": None,
            "damaged": False}


class StorageCloud(cloud_ui.FakeCloud):
    def __init__(self):
        super().__init__()
        self.rows = [entry(index) for index in range(3)]
        self.free = 0
        self.required = 100
        self.deleted = []

    def scan_capacity(self, settings):
        self.check("scan_capacity")
        pending = sum(row["status"] == "pending" for row in self.rows)
        reclaim = sum(row["size_bytes"] for row in self.rows if row["status"] == "sent")
        enough = self.free + reclaim >= self.required
        return {"can_start": enough and pending < 20,
                "reason": "queue" if pending >= 20 else None if enough else "space",
                "actual_free_bytes": self.free, "free_bytes": self.free,
                "reclaimable_bytes": reclaim, "required_bytes": self.required,
                "pending_count": pending, "pending_limit": 20,
                "accepted_count": len(self.rows)}

    def list_scans(self):
        self.check("list_scans")
        return [dict(row) for row in self.rows]

    def begin_scan(self, settings, calibration, now_ms):
        if not self.scan_capacity(settings)["can_start"]:
            self.check("begin_scan")
            return False
        return super().begin_scan(settings, calibration, now_ms)

    def delete_scan(self, scan_id):
        self.check("delete_scan")
        selected = next(row for row in self.rows if row["scan_id"] == scan_id)
        self.rows.remove(selected)
        self.free += selected["size_bytes"]
        self.deleted.append(scan_id)
        return True


class StorageUITests(unittest.TestCase):
    tick = cloud_ui.CloudUITests.tick
    press = cloud_ui.CloudUITests.press
    touch = cloud_ui.CloudUITests.touch

    def setUp(self):
        cloud_ui.CloudUITests.setUp(self)
        self.cloud = StorageCloud()
        self.app.cloud = self.cloud

    def open_storage(self):
        self.press("left")
        self.touch("advanced")
        self.touch("storage")

    def test_advanced_and_cloud_entry_pause_before_listing(self):
        self.open_storage()
        self.assertEqual("storage", self.app.screen)
        self.assertLess(self.cloud.calls.index("pause"), self.cloud.calls.index("list_scans"))
        self.assertIn("CANCEL", self.display.labels)
        self.assertIn("OK", self.display.labels)
        self.press("left")
        self.assertEqual("advanced", self.app.screen)
        self.touch("cloud")
        self.app._cloud_url_draft = "unsaved draft"
        self.touch("storage")
        self.press("left")
        self.assertEqual("cloud", self.app.screen)
        self.assertEqual("unsaved draft", self.app._cloud_url_draft)
        self.assertEqual([], self.instrument.commands)

    def test_full_preflight_offers_storage_before_wifi_or_outputs_start(self):
        self.press("right")
        self.assertEqual("storage", self.app.screen)
        self.assertIn("begin_scan", self.cloud.calls)
        self.assertEqual("idle", self.engine.state)
        self.assertEqual([], self.instrument.commands)
        self.assertFalse(self.app._wifi_paused)

    def test_pending_warning_cancel_and_selective_deletion_until_capacity_ready(self):
        original = [row["scan_id"] for row in self.cloud.rows]
        self.press("right")
        self.touch(("storage_select", 0))
        self.assertIn("No confirmed cloud copy.", self.display.labels)
        self.assertIn("This may be the only copy of this scan.", self.display.labels)
        self.press("left")
        self.assertEqual([], self.cloud.deleted)
        for expected_id in original[:2]:
            self.touch(("storage_select", 0))
            self.cloud.calls.clear()
            self.press("right")
            self.assertEqual(expected_id, self.cloud.deleted[-1])
            self.assertLess(self.cloud.calls.index("pause"), self.cloud.calls.index("delete_scan"))
            self.assertLess(self.cloud.calls.index("delete_scan"), self.cloud.calls.index("list_scans"))
            self.assertLess(self.cloud.calls.index("list_scans"), self.cloud.calls.index("scan_capacity"))
            self.assertEqual("storage", self.app.screen)
            self.assertEqual([], self.instrument.commands)
        self.assertEqual([original[2]], [row["scan_id"] for row in self.cloud.rows])
        self.assertIn("Ready for saved scan settings", self.display.labels)
        self.press("right")
        self.assertEqual("home", self.app.screen)
        self.assertEqual("idle", self.engine.state)
        self.assertEqual([], self.instrument.commands)
        self.assertEqual(0, self.store.writes)

    def test_sent_selection_explains_cloud_copy_is_preserved(self):
        self.cloud.rows[0]["status"] = "sent"
        self.open_storage()
        self.touch(("storage_select", 0))
        self.assertIn("Upload was acknowledged.", self.display.labels)
        self.assertIn("The Google Sheets copy is not deleted.", self.display.labels)
        self.press("left")
        self.assertEqual([], self.cloud.deleted)

    def test_queue_block_and_pagination_keep_selected_identity(self):
        self.cloud.free = 100000
        self.cloud.rows = [entry(index) for index in range(20)]
        self.press("right")
        self.assertIn("Queue full: 20/20 not uploaded", self.display.labels)
        self.touch("storage_next")
        selected = self.cloud.rows[3]["scan_id"]
        self.touch(("storage_select", 3))
        self.press("right")
        self.assertEqual([selected], self.cloud.deleted)
        self.assertEqual(19, len(self.cloud.rows))
        self.assertTrue(self.app._storage_capacity["can_start"])

    def test_damaged_record_remains_selectable_and_failure_is_sanitized(self):
        self.cloud.rows[0].update(damaged=True, point_count=None)
        self.open_storage()
        self.assertTrue(any("metadata unreadable" in label for label in self.display.labels))
        self.touch(("storage_select", 0))
        self.cloud.fail["delete_scan"] = OSError("PRIVATE_SECRET")
        self.press("right")
        self.assertEqual("notice", self.app.screen)
        self.assertNotIn("PRIVATE_SECRET", self.app.notice)
        self.assertEqual(3, len(self.cloud.rows))
        self.press("left")
        self.assertEqual("storage", self.app.screen)
        self.assertEqual([], self.cloud.deleted)

    def test_failed_pause_prevents_listing_and_deletion(self):
        self.press("left")
        self.touch("advanced")
        self.cloud.fail["pause"] = OSError("PRIVATE_SECRET")
        self.touch("storage")
        self.assertEqual("notice", self.app.screen)
        self.assertNotIn("list_scans", self.cloud.calls)
        self.assertEqual([], self.cloud.deleted)
        self.assertEqual([], self.instrument.commands)

    def test_refresh_failure_after_deletion_returns_to_list_not_empty_confirmation(self):
        self.open_storage()
        self.touch(("storage_select", 0))
        self.cloud.fail["scan_capacity"] = OSError("PRIVATE_SECRET")
        self.press("right")
        self.assertEqual("notice", self.app.screen)
        self.assertEqual("storage", self.app.notice_return)
        self.assertEqual(2, len(self.cloud.rows))
        self.press("left")
        self.assertEqual("storage", self.app.screen)
        self.assertIsNone(self.app._storage_selected)
        self.assertNotIn("PRIVATE_SECRET", repr(self.logs))

    def test_existing_uploaded_reclamation_can_allow_scan_without_storage_prompt(self):
        self.cloud.rows = [entry(1, "sent", 150)]
        self.press("right")
        self.assertEqual("scan", self.app.screen)
        self.assertIn("begin_scan", self.cloud.calls)
        self.assertNotIn("list_scans", self.cloud.calls)
        self.press("left")

    def test_stale_storage_actions_are_ignored_during_acquisition(self):
        self.cloud.free = 1000
        self.press("right")
        self.app.action("storage")
        self.app.action(("storage_select", 0))
        self.assertEqual("scan", self.app.screen)
        self.assertNotIn("list_scans", self.cloud.calls)
        self.assertEqual([], self.cloud.deleted)
        self.press("left")


if __name__ == "__main__":
    unittest.main()
