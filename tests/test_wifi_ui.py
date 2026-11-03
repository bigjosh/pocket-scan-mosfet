"""Wi-Fi touch flows and radio/acquisition exclusion; no serial or radio access."""

import unittest

from firmware import model
from firmware.engine import Engine
from firmware.ui import App, WIFI_KEYS
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore, finish_scan_preparation


def network(name, saved=False, rssi=-48, password=True, supported=True):
    return {"ssid": name, "saved": saved, "rssi": rssi, "security": 5,
            "security_label": "Personal" if password else "Open",
            "requires_password": password, "supported": supported}


class FakeWifi:
    def __init__(self):
        self.state = "off"
        self.ssid, self.ip, self.message = "", "", "Wi-Fi off"
        self.rows = [network("Bench network", True), network("New network")]
        self.saved = ["Bench network"]
        self.calls = []
        self.pause_error = None
        self.resume_error = None
        self.connect_error = None
        self.connect_valid = True
        self.scan_check = None
        self.resume_check = None
        self.want = False

    @property
    def status(self):
        return {"state": self.state, "ssid": self.ssid, "ip": self.ip,
                "message": self.message, "saved_count": len(self.saved)}

    def scan(self):
        self.calls.append(("scan",))
        if self.scan_check:
            self.scan_check()
        return self.rows

    def saved_networks(self):
        self.calls.append(("saved_networks",))
        return [network(name, True, None) for name in self.saved]

    def has_saved(self, name):
        return name in self.saved

    def connect(self, name, password="", use_saved=False, now_ms=0):
        self.calls.append(("connect", name, password, use_saved))
        if self.connect_error:
            raise self.connect_error
        if not self.connect_valid:
            self.message = "Use 8-63 printable characters or a 64-digit hex key"
            return False
        self.state, self.ssid, self.message = "connecting", name, "Connecting"
        self.want = True
        return True

    def tick(self, now_ms):
        self.calls.append(("tick",))

    def complete(self):
        self.state, self.ip, self.message = "connected", "192.0.2.5", "Connected"
        if self.ssid not in self.saved:
            self.saved.append(self.ssid)

    def cancel(self):
        self.calls.append(("cancel",))
        self.state, self.message, self.ssid = "off", "Wi-Fi off", ""
        self.want = False
        return True

    def pause(self):
        self.calls.append(("pause",))
        if self.pause_error:
            raise self.pause_error
        self.state = "paused"
        return True

    def resume(self, now_ms=0):
        self.calls.append(("resume",))
        if self.resume_check:
            self.resume_check()
        if self.resume_error:
            raise self.resume_error
        self.state = "connecting" if self.want else "off"

    def forget(self, name):
        self.calls.append(("forget", name))
        self.saved.remove(name)
        return True


class WifiUITests(unittest.TestCase):
    def setUp(self):
        self.display, self.inputs = FakeDisplay(), FakeInputs()
        self.instrument = HostInstrument()
        self.engine = Engine(self.instrument)
        self.wifi, self.store, self.logs = FakeWifi(), MemoryStore(), []
        settings = dict(model.DEFAULT_SETTINGS)
        settings.update(drain_start_uv=0, drain_stop_uv=1000000, drain_step_uv=1000000,
                        gate_start_uv=1000000, gate_stop_uv=1000000, gate_step_uv=1000000,
                        ina_average_count=4)
        from firmware.ram_scan import PackedScanBuffer
        from test_ram_ui import RamCloud
        self.buffer, self.restarts = PackedScanBuffer(capacity=32), []
        self.cloud = RamCloud(self.buffer)
        self.app = App(self.display, self.inputs, self.engine, settings,
                       store=self.store, wifi=self.wifi, cloud=self.cloud,
                       scan_buffer=self.buffer, restart=lambda: self.restarts.append(True),
                       error_logger=lambda context, error: self.logs.append((context, str(error))))
        self.now = 0
        self.app.render()

    def tick(self, events=()):
        self.now += 20
        self.inputs.events = list(events)
        self.app.tick(self.now)

    def press(self, name):
        starting = self.app.screen == "home" and name == "right"
        self.tick(((name,),))
        if starting:
            finish_scan_preparation(self)

    def touch(self, action):
        for x, y, width, height, target in self.app.hits:
            if target == action:
                self.tick((("touch", x + width // 2, y + height // 2),))
                return
        self.fail("Missing touch target %r on %s" % (action, self.app.screen))

    def hub(self):
        self.press("left")
        self.touch("advanced")
        self.touch("wifi")

    def search(self):
        self.hub()
        self.touch("wifi_scan")
        self.tick()

    def password(self):
        self.search()
        self.touch(("wifi_select", 1))
        self.touch("wifi_password")

    def finish(self):
        for _ in range(2000):
            self.tick()
            if self.engine.state != "scanning":
                return
        self.fail("Acquisition did not finish")

    def test_constructor_does_not_activate_or_scan_wifi(self):
        self.assertEqual(self.wifi.calls, [])
        self.assertEqual(self.instrument.commands, [])

    def test_optional_manager_unavailable_notice_preserves_old_app(self):
        self.app.wifi = None
        self.hub()
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.app.notice, "Wi-Fi is unavailable")
        self.assertEqual(self.instrument.commands, [])

    def test_search_is_painted_before_synchronous_scan(self):
        self.hub()
        self.wifi.scan_check = lambda: self.assertIn("Searching...", self.display.labels)
        self.touch("wifi_scan")
        self.assertEqual(self.app.screen, "wifi_search")
        self.assertNotIn(("scan",), self.wifi.calls)
        self.tick()
        self.assertEqual(self.app.screen, "wifi_list")
        self.assertEqual(self.wifi.calls.count(("scan",)), 1)
        self.assertIn("LOCK Personal | SAVED", self.display.labels)
        self.assertIn("-48 dBm", self.display.labels)
        self.assertEqual(self.instrument.commands, [])

    def test_cancel_before_pending_discovery_does_not_scan(self):
        self.hub()
        self.touch("wifi_scan")
        self.press("left")
        self.tick()
        self.assertEqual(self.app.screen, "wifi")
        self.assertNotIn(("scan",), self.wifi.calls)

    def test_all_32_scanned_and_20_saved_networks_are_reachable(self):
        self.wifi.rows = [network("Network %02d" % i) for i in range(32)]
        self.search()
        for page in range(8):
            self.assertIn("Network %02d" % (page * 4), self.display.labels)
            if page < 7:
                self.touch("wifi_next")
        self.touch(("wifi_select", 31))
        self.assertEqual(self.app._wifi_selected["ssid"], "Network 31")
        self.press("left")
        self.press("left")
        self.wifi.saved = ["Saved %02d" % i for i in range(20)]
        self.touch("wifi_saved")
        for _ in range(4):
            self.touch("wifi_next")
        self.touch(("wifi_select", 19))
        self.assertIn("USE SAVED PW", self.display.labels)
        self.touch("wifi_use_saved")
        self.assertIn(("connect", "Saved 19", "", True), self.wifi.calls)

    def test_saved_password_option_never_reveals_stored_credential(self):
        self.search()
        self.touch(("wifi_select", 0))
        self.assertIn("USE SAVED PW", self.display.labels)
        self.press("right")
        self.assertIn(("connect", "Bench network", "", True), self.wifi.calls)
        self.assertEqual(self.app.screen, "wifi_status")
        self.assertEqual(self.app._wifi_password, "")
        self.assertEqual(self.store.writes, 0)

    def test_password_mask_show_backspace_and_cancel_reset(self):
        self.password()
        for char in "qwerty":
            self.touch(("wifi_char", char))
        self.app.render()
        self.assertIn("******", self.display.labels)
        self.assertNotIn("qwerty", self.display.labels)
        self.touch("wifi_show")
        self.assertIn("qwerty", self.display.labels)
        self.touch("wifi_backspace")
        self.assertEqual(self.app._wifi_password, "qwert")
        self.press("left")
        self.assertEqual(self.app._wifi_password, "")
        self.assertFalse(self.app._wifi_show)
        self.touch("wifi_password")
        self.assertFalse(self.app._wifi_show)
        self.assertEqual(self.app._wifi_password, "")

    def test_keyboard_modes_cover_printable_ascii_and_space_with_bounded_input(self):
        characters = {" "}
        for rows in WIFI_KEYS.values():
            for row in rows:
                characters.update(row)
        self.assertEqual(characters, {chr(i) for i in range(32, 127)})
        self.password()
        self.touch(("wifi_mode", "ABC"))
        self.touch(("wifi_char", "Q"))
        self.touch(("wifi_mode", "123"))
        self.touch(("wifi_char", "9"))
        self.touch(("wifi_mode", "#+="))
        self.touch(("wifi_char", "!"))
        self.touch(("wifi_char", " "))
        self.assertEqual(self.app._wifi_password, "Q9! ")
        for _ in range(70):
            self.app.action(("wifi_char", "A"))
        self.assertEqual(len(self.app._wifi_password), 64)
        self.app.action(("wifi_char", "\n"))
        self.assertEqual(len(self.app._wifi_password), 64)

    def test_successful_connection_is_independent_of_scan_draft(self):
        self.password()
        self.app.draft["ina_average_count"] = 16
        for char in "qwertyui":
            self.touch(("wifi_char", char))
        self.press("right")
        self.assertEqual(self.app._wifi_password, "")
        self.assertEqual(self.app.screen, "wifi_status")
        self.assertFalse(any(hit[4] == "right" for hit in self.app.hits))
        self.wifi.complete()
        self.tick()
        self.assertIn("IP 192.0.2.5", self.display.labels)
        self.assertIn("New network", self.wifi.saved)
        self.press("right")  # Wi-Fi hub
        self.press("left")   # Advanced
        self.press("left")   # discard Advanced draft
        self.press("left")   # discard Settings draft
        self.assertEqual(self.app.settings["ina_average_count"], 4)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.wifi.state, "paused")
        self.assertEqual(self.restarts, [True])

    def test_connect_cancel_keeps_radio_off_and_does_not_save(self):
        self.password()
        self.app._wifi_password = "dummy-password"
        self.press("right")
        self.press("right")  # Disabled while connecting, including physical key.
        self.assertEqual(self.app.screen, "wifi_status")
        self.press("left")
        self.assertEqual(self.wifi.state, "off")
        self.assertNotIn("New network", self.wifi.saved)
        self.assertEqual(self.app._wifi_password, "")

    def test_bad_password_and_raw_driver_exceptions_do_not_leak(self):
        self.password()
        self.app._wifi_password = "secret-do-not-display"
        self.app._wifi_show = True
        self.wifi.connect_error = OSError("driver included secret-do-not-display")
        self.press("right")
        self.assertFalse(self.app._wifi_show)
        self.assertEqual(self.app._wifi_password, "")
        self.assertIn("Wi-Fi operation failed", self.display.labels)
        self.assertNotIn("secret-do-not-display", repr(self.display.labels) + repr(self.logs))
        self.press("right")
        self.touch("wifi_password")
        self.assertFalse(self.app._wifi_show)

    def test_invalid_password_validation_shows_fixed_message_and_clears_editor(self):
        self.password()
        self.wifi.connect_valid = False
        self.app._wifi_password = "tiny"
        self.press("right")
        self.assertEqual(self.app._wifi_password, "")
        self.assertIn("Use 8-63 printable", " ".join(self.display.labels))
        self.assertEqual(self.store.writes, 0)

    def test_open_and_unsupported_networks(self):
        self.wifi.rows = [network("Open test", password=False), network("Unsupported", supported=False)]
        self.search()
        self.touch(("wifi_select", 1))
        self.assertFalse(any(hit[4] == "right" for hit in self.app.hits))
        self.press("right")
        self.assertFalse(any(call[0] == "connect" for call in self.wifi.calls))
        self.press("left")
        self.touch(("wifi_select", 0))
        self.touch("wifi_connect")
        self.assertIn(("connect", "Open test", "", False), self.wifi.calls)

    def test_saved_open_network_does_not_claim_to_have_password(self):
        self.wifi.saved.append("Open test")
        self.wifi.rows = [network("Open test", True, password=False)]
        self.search()
        self.touch(("wifi_select", 0))
        self.assertIn("CONNECT SAVED", self.display.labels)
        self.assertNotIn("USE SAVED PW", self.display.labels)
        self.touch("wifi_use_saved")
        self.assertIn(("connect", "Open test", "", True), self.wifi.calls)

    def test_wifi_hub_cancel_stops_pending_connection(self):
        self.hub()
        self.wifi.state, self.wifi.want = "connecting", True
        self.press("left")
        self.assertEqual(self.app.screen, "advanced")
        self.assertEqual(self.wifi.state, "off")

    def test_forget_requires_confirmation_and_removes_only_selected(self):
        self.wifi.saved.append("Keep me")
        self.hub()
        self.touch("wifi_saved")
        self.touch(("wifi_select", 0))
        self.touch("wifi_forget")
        self.press("left")
        self.assertIn("Bench network", self.wifi.saved)
        self.touch("wifi_forget")
        self.press("right")
        self.assertEqual(self.wifi.saved, ["Keep me"])
        self.assertEqual(self.app.screen, "wifi_list")

    def test_pause_precedes_first_drive_and_radio_ticks_stop_during_scan(self):
        original = self.instrument.set_duty
        def checked_drive(role, value):
            self.assertEqual(self.wifi.state, "paused")
            original(role, value)
        self.instrument.set_duty = checked_drive
        self.press("right")
        self.assertEqual(self.wifi.calls, [("pause",)])
        self.tick()
        self.assertEqual(self.wifi.calls, [("pause",)])
        self.wifi.resume_check = lambda: self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
        self.finish()
        self.assertNotIn(("resume",), self.wifi.calls)
        self.assertEqual(self.app.screen, "upload")

    def test_pause_failure_never_starts_acquisition_and_is_sanitized(self):
        self.wifi.pause_error = OSError("private-radio-data")
        self.press("right")
        self.assertEqual(self.engine.state, "idle")
        self.assertEqual(self.instrument.commands, [])
        self.assertIn("Wi-Fi could not be disabled", self.app.notice)
        self.assertNotIn("private-radio-data", repr(self.logs))
        self.assertNotIn(("resume",), self.wifi.calls)




    def test_stale_setup_actions_and_pending_search_cannot_run_during_acquisition(self):
        self.press("right")
        self.app.action("wifi_scan")
        self.app.action(("wifi_select", 0))
        self.assertEqual(self.app.screen, "scan")
        self.app._wifi_search_pending = True
        self.app.screen, self.app.dirty = "wifi_search", False
        self.tick()
        self.assertNotIn(("scan",), self.wifi.calls)
        self.assertEqual(self.wifi.calls, [("pause",)])




if __name__ == "__main__":
    unittest.main()
