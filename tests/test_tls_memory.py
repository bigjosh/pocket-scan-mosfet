import importlib
import unittest
from unittest.mock import patch

from firmware import tls_memory


class ReserveTests(unittest.TestCase):
    def setUp(self):
        importlib.reload(tls_memory)

    def tearDown(self):
        tls_memory.release()

    def test_reserve_release_and_restore_same_budget(self):
        self.assertFalse(tls_memory.available())
        self.assertEqual(tls_memory.reserved_bytes(), 0)
        self.assertTrue(tls_memory.reserve(8192))
        self.assertTrue(tls_memory.available())
        self.assertEqual(tls_memory.reserved_bytes(), 8192)
        self.assertTrue(tls_memory.release())
        self.assertFalse(tls_memory.available())
        self.assertEqual(tls_memory.reserved_bytes(), 0)
        self.assertFalse(tls_memory.release())
        self.assertTrue(tls_memory.reserve())
        self.assertEqual(tls_memory.reserved_bytes(), 8192)

    def test_idempotent_reserve_does_not_allocate_or_collect_again(self):
        token = object()
        with patch.object(tls_memory, "bytearray", return_value=token, create=True) as alloc:
            with patch.object(tls_memory.gc, "collect") as collect:
                self.assertTrue(tls_memory.reserve())
                self.assertTrue(tls_memory.reserve())
                alloc.assert_called_once_with(96 * 1024)
                self.assertEqual(2, collect.call_count)
                self.assertTrue(all(not call.args for call in collect.call_args_list))

    def test_failed_restore_never_claims_available_or_reduces_budget(self):
        with patch.object(tls_memory, "bytearray", side_effect=MemoryError, create=True) as alloc:
            self.assertFalse(tls_memory.reserve(65536))
            self.assertFalse(tls_memory.available())
            self.assertEqual(tls_memory.reserved_bytes(), 0)
            self.assertFalse(tls_memory.reserve())
            self.assertEqual(alloc.call_args_list[0].args, (65536,))
            self.assertEqual(alloc.call_args_list[1].args, (65536,))
            self.assertEqual(alloc.call_count, 2)
        self.assertTrue(tls_memory.reserve())
        self.assertEqual(tls_memory.reserved_bytes(), 65536)

    def test_release_drops_reference_before_collection(self):
        self.assertTrue(tls_memory.reserve(64))
        with patch.object(tls_memory.gc, "collect",
                          side_effect=lambda: self.assertFalse(tls_memory.available())):
            self.assertTrue(tls_memory.release())

    def test_invalid_resize_preserves_held_reserve(self):
        self.assertTrue(tls_memory.reserve(64))
        for value in (0, -1, True, False, 1.5, "64", 128):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tls_memory.reserve(value)
            self.assertTrue(tls_memory.available())
            self.assertEqual(tls_memory.reserved_bytes(), 64)
        self.assertTrue(tls_memory.release())
        self.assertTrue(tls_memory.reserve(128))
        self.assertEqual(tls_memory.reserved_bytes(), 128)


if __name__ == "__main__":
    unittest.main()
