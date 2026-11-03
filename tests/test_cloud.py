"""Upload gating, stable retry identity, acknowledgement and secret handling."""

import copy
import json
import tempfile
import os
import unittest
from unittest.mock import patch

from firmware import archive, cloud
from test_archive import settings, row, fault, CALIBRATION

URL = "https://script.google.com/macros/s/PRIVATE_TEST_DEPLOYMENT/exec"


class Store:
    def __init__(self):
        self.data = None
        self.fail = False
    def load_json(self, path):
        return copy.deepcopy(self.data)
    def save_json_atomic(self, path, data):
        if self.fail:
            raise OSError(URL)
        self.data = copy.deepcopy(data)


class Transport:
    def __init__(self):
        self.calls = []
        self.answer = None
        self.error = None
        self.close_error = False
    def start(self, url, path, scan_id, sha256, point_count):
        self.calls.append((url, path, scan_id, sha256, point_count))
    def step(self):
        if self.error:
            raise OSError(self.error)
        return self.answer
    def close(self):
        if self.close_error:
            raise OSError(URL)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = archive.Archive(os.path.join(self.temp.name, "scans"), "abcd",
                                       free_bytes=lambda: 4 * 1024 * 1024)
        self.transport, self.store = Transport(), Store()
        self.manager = cloud.CloudManager(self.archive, self.transport, self.store)

    def queue(self):
        self.manager.begin_scan(settings(), CALIBRATION, 0)
        self.manager.record_point(row())
        self.manager.finish_scan(10)
        return self.manager.accept_scan()

    def configure(self, enabled=True):
        self.manager.save_config(URL, enabled)

    def ack(self):
        entry = self.archive.next_pending()
        return {"ok": True, "scan_id": entry["scan_id"], "sha256": entry["sha256"],
                "point_count": entry["point_count"]}

    def test_disabled_or_unconfigured_scans_still_save_locally(self):
        scan_id = self.queue()
        self.manager.tick(0, online=True, safe=True)
        self.assertEqual([], self.transport.calls)
        self.assertEqual(1, self.manager.status["pending_count"])
        self.assertEqual(scan_id, self.archive.next_pending()["scan_id"])

    def test_integer_grid_endpoint_count_survives_archive_upload_and_ack(self):
        self.configure()
        value = settings()
        value.update(drain_stop_uv=1300000, drain_step_uv=100000)
        self.manager.begin_scan(value, CALIBRATION, 0)
        for index in range(14):
            point = row(index)
            point.update(requested_vds=index / 10, vds=index / 10)
            self.manager.record_point(point)
        self.manager.finish_scan(10)
        self.manager.accept_scan()
        self.manager.tick(20, online=True, safe=True)
        self.assertEqual(14, self.transport.calls[-1][-1])
        source = archive.UploadSource(self.transport.calls[-1][1])
        try:
            parts = []
            while True:
                part = source.read(1024)
                if not part:
                    break
                parts.append(part)
            payload = json.loads(b"".join(parts))
        finally:
            source.close()
        self.assertEqual(value, payload["settings"])
        self.assertEqual(14, payload["point_count"])
        self.assertEqual(14, len(payload["points"]))
        self.assertAlmostEqual(1.3, payload["points"][-1][2], places=6)
        self.transport.answer = self.ack()
        self.manager.tick(30, online=True, safe=True)
        self.assertEqual(0, self.archive.pending_count)

    def test_zero_point_partial_only_uploads_after_accept_and_matching_zero_ack(self):
        self.configure()
        scan_id = self.manager.begin_scan(settings(), CALIBRATION, 0)
        self.manager.finish_scan(10, fault())
        self.manager.tick(20, online=True, safe=True)
        self.assertEqual([], self.transport.calls)
        self.manager.accept_scan()
        self.manager.tick(30, online=True, safe=True)
        self.assertEqual(0, self.transport.calls[-1][-1])
        self.transport.answer = self.ack()
        self.manager.tick(40, online=True, safe=True)
        self.assertEqual(0, self.archive.pending_count)
        self.assertTrue(os.path.exists(self.archive._path(scan_id, ".sent")))

    def test_unsafe_offline_and_unaccepted_never_upload(self):
        self.configure()
        self.manager.begin_scan(settings(), CALIBRATION, 0)
        self.manager.record_point(row())
        self.manager.finish_scan(10)
        self.manager.tick(10, online=True, safe=True)
        self.assertEqual([], self.transport.calls)
        self.manager.accept_scan()
        self.manager.tick(11, online=False, safe=True)
        self.manager.tick(12, online=True, safe=False)
        self.assertEqual([], self.transport.calls)
        self.manager.tick(13, online=True, safe=True)
        self.assertEqual("uploading", self.manager.state)
        self.assertEqual(1, len(self.transport.calls))
        self.manager.tick(14, online=True, safe=False)
        self.assertIsNone(self.manager._active)
        self.assertEqual(1, self.archive.pending_count)

    def test_success_requires_exact_ack_then_marks_sent(self):
        self.configure()
        self.queue()
        self.transport.answer = self.ack()
        self.manager.tick(0, online=True, safe=True)
        self.assertEqual(1, self.archive.pending_count)  # start never processes an ACK.
        self.manager.tick(1, online=True, safe=True)
        self.assertEqual(0, self.archive.pending_count)
        self.assertEqual(1, self.manager.status["saved_count"])
        self.assertEqual(1, len(self.archive._names(".sent")))

    def test_mismatched_or_malformed_ack_preserves_queue(self):
        self.configure()
        self.queue()
        good = self.ack()
        for changes in ({"ok": False}, {"scan_id": "different"}, {"sha256": "0" * 64},
                        {"point_count": 2}, {"point_count": True}, {"point_count": 1.0}):
            self.manager.request_upload()
            self.transport.answer = dict(good, **changes)
            self.manager.tick(0, online=True, safe=True)
            self.manager.tick(1, online=True, safe=True)
            self.assertEqual(1, self.archive.pending_count)
            self.assertEqual("retry", self.manager.state)

    def test_retry_preserves_identity_digest_and_has_bounded_backoff(self):
        self.configure()
        self.queue()
        self.transport.error = URL
        now = 0
        for failure in range(cloud.MAX_FAILURES):
            self.manager.tick(now, online=True, safe=True)
            self.manager.tick(now + 1, online=True, safe=True)
            if failure < cloud.MAX_FAILURES - 1:
                before = len(self.transport.calls)
                self.manager.tick(now + cloud.BACKOFF_MS[failure], online=True, safe=True)
                self.assertEqual(before, len(self.transport.calls))
                now += cloud.BACKOFF_MS[failure] + 1
        self.assertEqual(cloud.MAX_FAILURES, len(self.transport.calls))
        self.assertTrue(all(call == self.transport.calls[0] for call in self.transport.calls))
        self.manager.tick(now + 9999999, online=True, safe=True)
        self.assertEqual(cloud.MAX_FAILURES, len(self.transport.calls))
        self.assertNotIn(URL, repr(self.manager.status))
        self.assertEqual("error", self.manager.state)
        self.manager.request_upload()
        self.manager.tick(now + 10000000, online=True, safe=True)
        self.assertEqual(cloud.MAX_FAILURES + 1, len(self.transport.calls))

    def test_manual_upload_can_override_automatic_disabled(self):
        self.configure(False)
        self.queue()
        self.manager.tick(0, online=True, safe=True)
        self.assertEqual([], self.transport.calls)
        self.assertTrue(self.manager.request_upload())
        self.manager.tick(1, online=True, safe=True)
        self.assertEqual(1, len(self.transport.calls))
        self.assertFalse(self.manager.status["enabled"])

    def test_pause_failure_blocks_new_scan_with_fixed_message(self):
        self.configure()
        self.queue()
        self.manager.tick(0, online=True, safe=True)
        self.transport.close_error = True
        with self.assertRaisesRegex(RuntimeError, "^Upload connection could not be closed$"):
            self.manager.begin_scan(settings(), CALIBRATION, 1)
        self.assertIsNone(self.archive._current)
        self.assertEqual(1, self.archive.pending_count)
        self.assertNotIn(URL, repr(self.manager.status))

    def test_begin_reports_capacity_block_as_false_and_preserves_accepted_files(self):
        scan_id = self.queue()
        before = self.archive.next_pending()
        for reason in ("queue", "space"):
            with patch.object(self.archive, "begin_scan", side_effect=archive.ArchiveCapacityError(reason)):
                self.assertIs(False, self.manager.begin_scan(settings(), CALIBRATION, 0))
            self.assertEqual(before, self.archive.next_pending())
            self.assertEqual(1, self.manager.status["pending_count"])
            self.assertTrue(os.path.exists(self.archive._path(scan_id, ".pending")))
        with patch.object(self.archive, "begin_scan", side_effect=RuntimeError("other failure")):
            with self.assertRaisesRegex(RuntimeError, "other failure"):
                self.manager.begin_scan(settings(), CALIBRATION, 0)

    def test_begin_uses_fresh_actual_capacity_without_a_preview(self):
        self.assertTrue(self.manager.scan_capacity(settings())["can_start"])
        self.archive._free_bytes = lambda: 0
        with patch.object(self.manager, "scan_capacity", side_effect=AssertionError("duplicate preview")):
            self.assertIs(False, self.manager.begin_scan(settings(), CALIBRATION, 0))
        self.assertIsNone(self.archive._current)
        self.assertEqual(0, self.manager.status["pending_count"])

    def test_calibration_cache_hooks_share_archive_fingerprint(self):
        fingerprint = self.manager.prepare_calibration(CALIBRATION)
        self.assertEqual(archive.calibration_hash(CALIBRATION), fingerprint)
        with patch.object(archive, "calibration_hash", side_effect=AssertionError("unexpected rehash")):
            self.assertEqual(fingerprint, self.manager.prepare_calibration(CALIBRATION))
            self.manager.begin_scan(settings(), CALIBRATION, 0)
        self.manager.discard_scan()
        self.manager.invalidate_calibration()
        self.assertIsNone(self.archive._calibration_object)

    def test_config_validation_is_strict_and_secret_free(self):
        for invalid in ("http://script.google.com/macros/s/id/exec", URL + "?key=secret",
                        "https://evil.example/macros/s/id/exec", URL + "#secret",
                        "https://script.google.com/macros/s//exec", "https://script.google.com/macros/s/a/b/exec"):
            with self.assertRaises(ValueError) as error:
                self.manager.save_config(invalid)
            self.assertNotIn(invalid, str(error.exception))
        self.configure()
        self.assertNotIn(URL, repr(self.manager.status))
        self.assertEqual(URL, self.manager.config_for_edit()["url"])
        self.store.fail = True
        with self.assertRaisesRegex(RuntimeError, "^Cloud settings could not be saved$"):
            self.manager.save_config(URL, False)
        self.assertTrue(self.manager.status["enabled"])

    def test_queue_and_config_survive_manager_reconstruction(self):
        self.configure()
        scan_id = self.queue()
        rebooted = cloud.CloudManager(archive.Archive(self.archive.directory, "abcd"),
                                      Transport(), self.store)
        self.assertTrue(rebooted.status["configured"])
        self.assertEqual(1, rebooted.status["pending_count"])
        rebooted.tick(0, online=True, safe=True)
        self.assertEqual(scan_id, rebooted._active["scan_id"])

    def test_accept_sync_failure_refreshes_visible_queue_and_allows_retry(self):
        self.configure()
        self.manager.begin_scan(settings(), CALIBRATION, 0)
        self.manager.record_point(row())
        self.manager.finish_scan(10)
        with patch.object(archive, "_sync", side_effect=OSError("post-rename sync failure")):
            with self.assertRaisesRegex(RuntimeError, "could not be added"):
                self.manager.accept_scan()
        self.manager.discard_scan()  # Mirrors the UI's Cancel after the notice.
        self.assertEqual(1, self.archive.pending_count)
        self.assertEqual(1, self.manager.status["pending_count"])
        self.assertTrue(self.manager.request_upload())
        self.manager.tick(20, online=True, safe=True)
        self.assertEqual(1, len(self.transport.calls))

    def test_accept_preserves_original_error_when_count_refresh_also_fails(self):
        self.configure()
        self.manager.begin_scan(settings(), CALIBRATION, 0)
        self.manager.record_point(row())
        self.manager.finish_scan(10)
        with patch.object(archive, "_sync", side_effect=OSError("sync")), \
                patch.object(self.manager, "_refresh_counts", side_effect=OSError("refresh")):
            with self.assertRaisesRegex(RuntimeError, "could not be added"):
                self.manager.accept_scan()
        self.assertEqual(0, self.manager.status["pending_count"])
        self.assertTrue(self.manager.request_upload())
        self.assertEqual(1, self.manager.status["pending_count"])

    def test_status_and_idle_ticks_do_not_repeatedly_read_directories(self):
        self.configure()
        with patch.object(archive.os, "listdir", side_effect=AssertionError("Unexpected directory scan")):
            for now in range(20):
                self.manager.status
                self.manager.tick(now, online=True, safe=True)
        self.assertEqual("idle", self.manager.state)

    def test_storage_list_closes_upload_before_reading_and_preserves_payloads(self):
        self.configure()
        scan_id = self.queue()
        self.manager.tick(0, online=True, safe=True)
        events = []
        self.transport.close = lambda: events.append("close")
        original_list = self.archive.list_scans
        def listed():
            events.append("list")
            self.assertIsNone(self.manager._active)
            return original_list()
        with patch.object(self.archive, "list_scans", side_effect=listed):
            rows = self.manager.list_scans()
        self.assertEqual(["close", "list"], events)
        self.assertEqual(scan_id, rows[0]["scan_id"])
        self.assertEqual(1, self.archive.pending_count)

    def test_storage_delete_selected_only_after_close_and_refreshes_counts(self):
        selected, other = self.queue(), self.queue()
        events = []
        self.transport.close = lambda: events.append("close")
        original_delete = self.archive.delete_scan
        def deleted(scan_id):
            events.append("delete")
            return original_delete(scan_id)
        with patch.object(self.archive, "delete_scan", side_effect=deleted):
            self.assertTrue(self.manager.delete_scan(selected))
        self.assertEqual(["close", "delete"], events)
        self.assertEqual(1, self.manager.status["pending_count"])
        self.assertEqual(other, self.archive.next_pending()["scan_id"])
        reopened = cloud.CloudManager(archive.Archive(self.archive.directory, "abcd"),
                                      Transport(), self.store)
        self.assertEqual(1, reopened.status["saved_count"])

    def test_current_upload_cannot_be_deleted_without_fresh_review(self):
        self.configure()
        scan_id = self.queue()
        self.manager.tick(0, online=True, safe=True)
        with self.assertRaisesRegex(RuntimeError, "Review the stopped upload"):
            self.manager.delete_scan(scan_id)
        self.assertEqual(1, self.archive.pending_count)
        self.assertIsNone(self.manager._active)
        self.assertEqual(scan_id, self.manager.list_scans()[0]["scan_id"])
        self.assertTrue(self.manager.delete_scan(scan_id))

    def test_storage_close_failure_prevents_listing_and_deletion(self):
        scan_id = self.queue()
        self.transport.close_error = True
        for operation in (self.manager.list_scans, lambda: self.manager.delete_scan(scan_id)):
            with self.assertRaisesRegex(RuntimeError, "Upload connection could not be closed"):
                operation()
        self.assertEqual(1, self.archive.pending_count)

    def test_storage_delete_sync_failure_refreshes_remaining_queue(self):
        selected, other = self.queue(), self.queue()
        with patch.object(archive, "_sync", side_effect=OSError("sync failed")):
            with self.assertRaises(RuntimeError):
                self.manager.delete_scan(selected)
        self.assertEqual(1, self.manager.status["pending_count"])
        self.assertEqual(other, self.archive.next_pending()["scan_id"])

    def test_memory_restore_waits_for_next_idle_tick_after_ack_even_offline(self):
        self.configure()
        self.queue()
        self.transport.answer = self.ack()
        calls = []
        self.transport.maintain_memory = lambda: calls.append("restore") or True
        self.manager.tick(0, online=True, safe=True)
        before_ack = len(calls)
        self.manager.tick(1, online=True, safe=True)
        self.assertEqual(0, self.archive.pending_count)
        self.assertEqual(before_ack, len(calls))
        self.manager.tick(2, online=False, safe=True)
        self.assertEqual(before_ack + 1, len(calls))
        self.manager.tick(3, online=False, safe=True)
        self.assertEqual(before_ack + 1, len(calls))

    def test_memory_failure_uses_bounded_backoff_and_keeps_scan_queued(self):
        self.configure()
        self.queue()
        calls = []
        self.transport.maintain_memory = lambda: calls.append("restore") or False
        self.manager.tick(0, online=True, safe=True)
        self.assertEqual("retry", self.manager.state)
        self.assertEqual(cloud.MEMORY_UNAVAILABLE, self.manager.status["message"])
        self.assertEqual(1, self.manager._failures)
        self.assertEqual([], self.transport.calls)
        for now in (2, 4, 20, 100, 59999):
            self.manager.tick(now, online=True, safe=True)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.manager._failures)
        self.manager.tick(60000, online=True, safe=True)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, self.manager._failures)
        self.assertEqual(1, self.archive.pending_count)

    def test_pause_restores_after_close_and_memory_failure_does_not_block_scan(self):
        self.configure()
        self.queue()
        events = []
        self.transport.close = lambda: events.append("close")
        self.transport.maintain_memory = lambda: events.append("restore") or False
        self.assertTrue(self.manager.pause())
        self.assertEqual(["close", "restore"], events)
        self.assertEqual(cloud.MEMORY_UNAVAILABLE, self.manager.status["message"])
        self.manager.begin_scan(settings(), CALIBRATION, 30)
        self.assertIsNotNone(self.archive._current)
        self.manager.discard_scan()

    def test_memory_maintenance_never_runs_during_unsafe_acquisition(self):
        self.configure()
        calls = []
        self.transport.maintain_memory = lambda: calls.append("restore") or True
        self.manager._memory_check_due = True
        for now in range(10):
            self.manager.tick(now, online=True, safe=False)
        self.assertEqual([], calls)
        self.manager.tick(10, online=True, safe=True)
        self.assertEqual(["restore"], calls)

    def test_unsafe_tick_closes_active_transport_without_allocating_reserve(self):
        self.configure()
        self.queue()
        self.manager.tick(0, online=True, safe=True)
        events = []
        self.transport.close = lambda: events.append("close")
        self.transport.maintain_memory = lambda: events.append("restore") or True
        self.manager.tick(1, online=True, safe=False)
        self.assertEqual(["close"], events)
        self.manager.maintain_memory(2)
        self.assertEqual(["close", "restore"], events)



if __name__ == "__main__":
    unittest.main()
