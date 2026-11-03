"""Credential transactions and radio lifecycle without board/network access."""

import copy
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from firmware import wifi


SECRET = "private-test-key"


def profile(ssid, password=SECRET, security=wifi.WPA2):
    return {"ssid": ssid, "password": password, "security": security}


def raw(ssid, rssi=-40, security=5):
    return (ssid.encode() if isinstance(ssid, str) else ssid,
            b"abcdef", 1, rssi, security, 1)


class Store:
    def __init__(self, profiles=None):
        self.data = {"version": 1, "profiles": profiles or []}
        self.writes = 0
        self.fail = False

    def load_json(self, path):
        return copy.deepcopy(self.data)

    def save_json_atomic(self, path, data):
        if self.fail:
            raise OSError("write failure " + SECRET)
        self.data = copy.deepcopy(data)
        self.writes += 1


class Radio:
    def __init__(self):
        self.active = False
        self.connected = False
        self.join_status = 1
        self.address = "192.0.2.3"
        self.results = []
        self.calls = []
        self.fail = None
        self.shutdown_ok = True

    def _call(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise OSError("driver exposed " + SECRET)

    def activate(self):
        self._call("activate")
        self.active = True

    def deactivate(self):
        self._call("deactivate")
        if self.shutdown_ok:
            self.active = self.connected = False
        return self.shutdown_ok

    def scan(self):
        self._call("scan")
        return self.results

    def disconnect(self):
        self._call("disconnect")
        self.connected = False

    def connect(self, ssid, password, security):
        self._call("connect")
        self.credentials = (ssid, password, security)

    def isconnected(self):
        self._call("isconnected")
        return self.connected

    def status(self):
        self._call("status")
        return self.join_status

    def ip(self):
        self._call("ip")
        return self.address


class WifiTests(unittest.TestCase):
    def setUp(self):
        self.radio, self.store = Radio(), Store()
        self.manager = wifi.WifiManager(self.radio, store=self.store)

    def success(self, ssid="Home", password=SECRET, now=0, use_saved=False):
        self.assertTrue(self.manager.connect(ssid, password, use_saved=use_saved, now_ms=now))
        self.radio.connected = True
        self.manager.tick(now + 1)
        self.assertEqual("connected", self.manager.state)

    def test_constructor_is_inert_and_status_is_secret_free(self):
        self.assertEqual([], self.radio.calls)
        self.assertEqual("off", self.manager.state)
        self.success()
        self.assertNotIn(SECRET, repr(self.manager.status))
        self.assertNotIn(SECRET, repr(self.manager.saved_networks()))

    def test_real_pico_security_five_and_new_cyw43_flags_are_personal(self):
        for raw_code, expected in ((0, 0), (3, wifi.WPA), (5, wifi.WPA2),
                                   (7, wifi.WPA_MIXED), (wifi.WPA2 | 1, wifi.WPA2),
                                   (wifi.WPA3 | 1, wifi.WPA3)):
            self.assertEqual(expected, wifi._security(raw_code))
        for unsupported in (1, 0x02400005, 99):
            self.assertEqual(-1, wifi._security(unsupported))

    def test_scan_deduplicates_strongest_and_bounds_results(self):
        self.radio.results = [raw("Home", -80), raw("Home", -30), raw("Open", -20, 0),
                              raw("WEP", -50, 1), raw(b""), raw(b"bad\xff"),
                              raw("Control\nSSID")]
        rows = self.manager.scan()
        self.assertEqual(["Open", "Home", "WEP"], [row["ssid"] for row in rows])
        self.assertEqual(-30, rows[1]["rssi"])
        self.assertFalse(rows[0]["requires_password"])
        self.assertFalse(rows[2]["supported"])
        self.radio.results = [raw("N%02d" % index, -index) for index in range(60)]
        self.assertEqual(32, len(self.manager.scan()))
        self.assertEqual("N31", self.manager.scan()[-1]["ssid"])

    def test_open_and_unsupported_networks(self):
        self.radio.results = [raw("Open", security=0), raw("WEP", security=1)]
        self.manager.scan()
        self.success("Open", "")
        self.assertEqual(0, self.store.data["profiles"][0]["security"])
        before = len(self.radio.calls)
        self.assertFalse(self.manager.connect("WEP", SECRET))
        self.assertEqual(before, len(self.radio.calls))

    def test_only_successful_join_with_ip_commits(self):
        self.manager.connect("Home", SECRET, now_ms=100)
        self.manager.tick(101)
        self.assertEqual(0, self.store.writes)
        self.radio.connected, self.radio.address = True, "0.0.0.0"
        self.manager.tick(401)
        self.assertEqual(0, self.store.writes)
        self.radio.address = "192.0.2.3"
        self.manager.tick(701)
        self.assertEqual(1, self.store.writes)
        self.assertTrue(self.manager.has_saved("Home"))

    def test_failed_replacement_keeps_cache_and_restores_previous_once(self):
        self.success()
        original = copy.deepcopy(self.store.data)
        self.manager.connect("Home", "changed-but-wrong", now_ms=10)
        self.radio.join_status = -3
        self.manager.tick(11)
        self.assertEqual(original, self.store.data)
        self.assertEqual(("Home", SECRET, wifi.WPA_MIXED), self.radio.credentials)
        self.assertEqual("connecting", self.manager.state)
        self.manager.tick(12)
        self.assertEqual("error", self.manager.state)
        self.assertEqual(original, self.store.data)
        self.assertFalse(self.radio.active)

    def test_failed_new_network_never_added_or_promoted(self):
        self.success("First")
        self.success("Second", now=10)
        self.manager.cancel()
        before = copy.deepcopy(self.store.data)
        self.manager.connect("First", "incorrect-password", now_ms=20)
        self.radio.join_status = -3
        self.manager.tick(21)
        self.assertEqual(before, self.store.data)
        self.assertEqual(["Second", "First"], [row["ssid"] for row in self.manager.saved_networks()])

    def test_cached_password_is_not_reused_after_scanned_security_changes(self):
        self.success("Home")
        before = copy.deepcopy(self.store.data)
        self.radio.results = [raw("Home", security=0)]
        self.manager.scan()
        call_count = len(self.radio.calls)
        self.assertFalse(self.manager.connect("Home", use_saved=True))
        self.assertEqual(call_count, len(self.radio.calls))
        self.assertIn("security changed", self.manager.status["message"])
        self.assertEqual(before, self.store.data)
        # Explicit open-network confirmation, rather than cached reuse, works.
        self.success("Home", "", now=10)
        self.assertEqual("", self.store.data["profiles"][0]["password"])
        self.assertEqual(wifi.OPEN, self.store.data["profiles"][0]["security"])

    def test_twenty_unique_mru_profiles_and_saved_reuse(self):
        for index in range(21):
            self.success("Network%d" % index, now=index * 100)
        names = [row["ssid"] for row in self.manager.saved_networks()]
        self.assertEqual(20, len(names))
        self.assertEqual("Network20", names[0])
        self.assertNotIn("Network0", names)
        self.success("Network5", use_saved=True, now=2200)
        self.assertEqual("Network5", self.manager.saved_networks()[0]["ssid"])
        self.assertEqual(20, len(self.manager.saved_networks()))
        self.success("network5", now=2300)
        self.assertTrue(self.manager.has_saved("Network5"))
        self.assertTrue(self.manager.has_saved("network5"))

    def test_cancel_drops_pending_password_without_resume(self):
        self.success()
        self.manager.connect("Other", "different-secret", now_ms=10)
        self.assertTrue(self.manager.cancel())
        self.assertIsNone(self.manager._pending)
        self.assertFalse(self.manager.has_saved("Other"))
        self.manager.pause()
        self.assertFalse(self.manager.resume(20))
        self.assertEqual("off", self.manager.state)
        self.assertFalse(self.radio.active)

    def test_timeout_is_bounded_and_respects_tick_wrap(self):
        period = 1 << 30
        def wrapped(now, before):
            return ((now - before + period // 2) % period) - period // 2
        with patch.object(wifi, "_ticks_diff", wrapped):
            self.manager.connect("Home", SECRET, now_ms=period - 1000)
            self.manager.tick(18999)
            self.assertEqual("connecting", self.manager.state)
            self.manager.tick(19000)
            self.assertEqual("error", self.manager.state)
        self.assertIn("timed out", self.manager.status["message"])
        self.assertFalse(self.radio.active)

    def test_pause_restores_only_prior_intent_without_scanning(self):
        self.manager.pause()
        self.assertFalse(self.manager.resume(0))
        self.success()
        self.manager.pause()
        self.assertFalse(self.radio.active)
        self.assertEqual([], self.manager.scan())
        self.assertFalse(self.manager.connect("Other", SECRET))
        self.assertTrue(self.manager.resume(100))
        self.assertEqual("connecting", self.manager.state)
        self.assertNotIn("scan", self.radio.calls)

    def test_pause_failure_is_fixed_and_fail_closed_even_when_unused(self):
        self.radio.shutdown_ok = False
        with self.assertRaisesRegex(RuntimeError, "^Wi-Fi could not be disabled$"):
            self.manager.pause()
        self.assertEqual(["deactivate"], self.radio.calls)
        self.assertEqual("error", self.manager.state)

    def test_autoconnect_uses_latest_committed_profile(self):
        self.store.data["profiles"] = [profile("Recent"), profile("Older")]
        self.manager = wifi.WifiManager(self.radio, store=self.store)
        self.assertTrue(self.manager.autoconnect(100))
        self.assertEqual("Recent", self.radio.credentials[0])
        self.assertNotIn("scan", self.radio.calls)

    def test_upload_autoconnect_unpauses_without_profile_writes(self):
        self.store.data["profiles"] = [profile("Recent"), profile("Older")]
        self.manager = wifi.WifiManager(self.radio, store=self.store)
        self.manager.pause()
        with patch.object(self.store, "save_json_atomic", side_effect=AssertionError("flash write")) as save:
            self.assertTrue(self.manager.autoconnect(100, persist=False))
            self.radio.connected = True
            self.manager.tick(101)
            self.assertEqual(self.manager.state, "connected")
            save.assert_not_called()
        self.assertEqual(self.radio.credentials[0], "Recent")

    def test_readonly_connection_cannot_reorder_but_later_settings_connection_can(self):
        self.store.data["profiles"] = [profile("Recent"), profile("Older")]
        self.manager = wifi.WifiManager(self.radio, store=self.store)
        before = copy.deepcopy(self.store.data)
        self.manager.connect("Older", use_saved=True, persist=False)
        self.radio.connected = True
        self.manager.tick(1)
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.store.writes, 0)
        self.manager.connect("Older", use_saved=True, now_ms=10)
        self.radio.connected = True
        self.manager.tick(11)
        self.assertEqual(self.store.data["profiles"][0]["ssid"], "Older")
        self.assertEqual(self.store.writes, 1)

    def test_storage_failure_keeps_old_cache_and_connection(self):
        self.success("First")
        before = copy.deepcopy(self.store.data)
        self.store.fail = True
        self.success("Second", now=10)
        self.assertEqual(before, self.store.data)
        self.assertFalse(self.manager.has_saved("Second"))
        self.assertIn("could not be saved", self.manager.status["message"])
        self.assertNotIn(SECRET, repr(self.manager.status))

    def test_adapter_exceptions_never_expose_password(self):
        for failure in ("activate", "disconnect", "connect", "isconnected", "status", "ip"):
            with self.subTest(failure=failure):
                radio = Radio()
                manager = wifi.WifiManager(radio, store=Store())
                radio.fail = failure
                manager.connect("Home", SECRET)
                radio.connected = failure == "ip"
                manager.tick(1)
                self.assertNotIn(SECRET, repr(manager.status))
        self.radio.fail = "scan"
        self.assertEqual([], self.manager.scan())
        self.assertNotIn(SECRET, repr(self.manager.status))

    def test_validation_never_starts_radio(self):
        for name, password in (("", SECRET), ("A" * 33, SECRET), ("Home", "short"),
                               ("Home", "x" * 64), ("Home", "contains\nnewline")):
            self.assertFalse(self.manager.connect(name, password))
        self.assertEqual([], self.radio.calls)
        self.assertFalse(self.manager.connect("Missing", use_saved=True))
        self.assertEqual([], self.radio.calls)

    def test_forget_persists_and_disconnects_current_profile(self):
        self.success()
        self.assertTrue(self.manager.forget("Home"))
        self.assertFalse(self.manager.has_saved("Home"))
        self.assertEqual([], self.store.data["profiles"])
        self.assertEqual("off", self.manager.state)
        self.manager.pause()
        self.assertFalse(self.manager.resume(100))

    def test_forget_failed_write_preserves_profile_and_connection(self):
        self.success()
        self.store.fail = True
        self.assertFalse(self.manager.forget("Home"))
        self.assertTrue(self.manager.has_saved("Home"))
        self.assertTrue(self.radio.connected)

    def test_corrupt_profile_file_reports_fixed_message(self):
        self.store.data = {"version": 1, "profiles": [profile("Home", SECRET + "\n")]}
        manager = wifi.WifiManager(self.radio, store=self.store)
        self.assertEqual("error", manager.state)
        self.assertEqual([], manager.saved_networks())
        self.assertNotIn(SECRET, repr(manager.status))
        self.assertEqual([], self.radio.calls)

    def test_real_atomic_file_round_trip_and_no_settings_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, "wifi.json")
            manager = wifi.WifiManager(self.radio, path=filename)
            self.assertTrue(manager.connect("Home", SECRET))
            self.radio.connected = True
            manager.tick(1)
            self.assertFalse(os.path.exists(filename + ".tmp"))
            loaded = wifi.WifiManager(Radio(), path=filename)
            self.assertEqual(["Home"], [row["ssid"] for row in loaded.saved_networks()])
            self.assertEqual(["wifi.json"], os.listdir(directory))


class AdapterTests(unittest.TestCase):
    def test_soft_reset_shutdown_initializes_handles_and_disables_both(self):
        class Interface:
            def __init__(self):
                self.enabled = True
                self.calls = []
            def active(self, value=None):
                self.calls.append(value)
                if value is not None:
                    self.enabled = value
                return self.enabled
        station, ap = Interface(), Interface()
        def wlan(index):
            return (station, ap)[index]
        wlan.IF_STA, wlan.IF_AP = 0, 1
        module = types.SimpleNamespace(WLAN=wlan)
        adapter = wifi.MicroPythonAdapter()
        with patch.dict(sys.modules, {"network": module}):
            self.assertTrue(adapter.deactivate())
        self.assertEqual([False, None], station.calls)
        self.assertEqual([False, None], ap.calls)

    def test_shutdown_checks_both_even_when_one_raises(self):
        class Interface:
            def __init__(self, fail=False):
                self.fail, self.called = fail, False
            def active(self, value=None):
                self.called = True
                if self.fail:
                    raise OSError(SECRET)
                return False
        adapter = wifi.MicroPythonAdapter()
        adapter._sta, adapter._ap = Interface(True), Interface()
        self.assertFalse(adapter.deactivate())
        self.assertTrue(adapter._ap.called)


if __name__ == "__main__":
    unittest.main()
