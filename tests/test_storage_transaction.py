"""Atomic JSON transactions reserve real file resources before acquisition."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from firmware import storage


class FaultStream:
    def __init__(self, stream, stage):
        self.stream = stream
        self.stage = stage
        self.failed = False

    @property
    def closed(self):
        return self.stream.closed

    def write(self, data):
        return self.stream.write(data)

    def flush(self):
        if self.stage == "flush" and not self.failed:
            self.failed = True
            raise OSError("flush failed")
        return self.stream.flush()

    def close(self):
        if self.stage == "close" and not self.failed:
            self.failed = True
            raise OSError("close failed")
        return self.stream.close()


class JsonTransactionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "calibration.json"
        self.temporary = Path(str(self.path) + ".tmp")
        self.path.write_text('{"old":true}')
        self.before = self.path.read_bytes()

    def tearDown(self):
        self.directory.cleanup()

    def begin(self):
        return storage.begin_json_atomic(str(self.path))

    def assert_old_committed(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(self.temporary.exists())

    def test_begin_preopens_real_file_and_commit_uses_it_without_reopening(self):
        transaction = self.begin()
        self.assertTrue(self.temporary.exists())
        self.assertFalse(transaction._stream.closed)
        self.assertEqual(self.path.read_bytes(), self.before)
        with patch("builtins.open", side_effect=AssertionError("must use preopened file")):
            transaction.commit({"new": [1, 2, 3]})
        self.assertEqual(json.loads(self.path.read_text()), {"new": [1, 2, 3]})
        self.assertFalse(self.temporary.exists())

    def test_abort_is_idempotent_and_never_removes_replacement_staging_file(self):
        transaction = self.begin()
        stream = transaction._stream
        transaction.abort()
        self.assertTrue(stream.closed)
        self.assert_old_committed()
        self.temporary.write_text("belongs to a later operation")
        transaction.abort()
        self.assertEqual(self.temporary.read_text(), "belongs to a later operation")
        with self.assertRaises(ValueError):
            transaction.commit({"forbidden": True})

    def test_abort_after_commit_cannot_remove_new_staging_or_committed_file(self):
        transaction = self.begin()
        transaction.commit({"new": True})
        self.temporary.write_text("later operation")
        transaction.abort()
        self.assertEqual(json.loads(self.path.read_text()), {"new": True})
        self.assertEqual(self.temporary.read_text(), "later operation")
        with self.assertRaises(ValueError):
            transaction.commit({"again": True})

    def test_open_memory_failure_cleans_file_created_before_allocation_failed(self):
        def failed_open(path, mode):
            self.temporary.write_text("partial open")
            raise MemoryError("1148 bytes")
        with patch("builtins.open", side_effect=failed_open):
            with self.assertRaises(MemoryError):
                self.begin()
        self.assert_old_committed()

    def test_json_failure_closes_file_and_preserves_prior_commit(self):
        transaction = self.begin()
        stream = transaction._stream
        def failed_dump(data, target):
            target.write('{"partial":')
            raise MemoryError("512 bytes")
        with patch.object(storage.json, "dump", side_effect=failed_dump):
            with self.assertRaises(MemoryError):
                transaction.commit({"new": True})
        self.assertTrue(stream.closed)
        self.assert_old_committed()

    def test_flush_close_and_rename_failures_preserve_old_file_and_cleanup(self):
        for stage in ("flush", "close", "rename"):
            with self.subTest(stage=stage):
                transaction = self.begin()
                stream = FaultStream(transaction._stream, stage)
                transaction._stream = stream
                if stage == "rename":
                    failure = patch.object(storage.os, "replace", side_effect=OSError("rename failed"))
                else:
                    failure = patch.object(storage.os, "replace", wraps=storage.os.replace)
                with failure:
                    with self.assertRaises(OSError):
                        transaction.commit({"new": True})
                self.assertTrue(stream.closed)
                self.assert_old_committed()
                transaction.abort()

    def test_sync_before_rename_preserves_old_and_sync_after_rename_keeps_new(self):
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                self.path.write_bytes(self.before)
                transaction = self.begin()
                calls = []
                def sync():
                    calls.append(True)
                    if len(calls) == fail_at:
                        raise OSError("sync failed")
                with patch.object(storage.os, "sync", side_effect=sync, create=True):
                    with self.assertRaises(OSError):
                        transaction.commit({"new": True})
                self.assertFalse(self.temporary.exists())
                if fail_at == 1:
                    self.assertEqual(self.path.read_bytes(), self.before)
                else:
                    self.assertEqual(json.loads(self.path.read_text()), {"new": True})
                transaction.abort()

    def test_abort_close_failure_does_not_mask_original_save_error(self):
        transaction = self.begin()
        stream = transaction._stream
        class FailedCleanup:
            def write(self, value):
                raise ValueError("original serialization error")
            def close(self):
                stream.close()
                raise OSError("cleanup failure")
        transaction._stream = FailedCleanup()
        with self.assertRaisesRegex(ValueError, "original serialization"):
            transaction.commit({"new": True})
        self.assertTrue(stream.closed)
        self.assert_old_committed()


if __name__ == "__main__":
    unittest.main()
