"""Native display/touch-routing Wi-Fi test with synthetic, memory-only data.

No real WLAN object, SSID discovery, credentials file or user-settings file is
used. Instrument outputs start at zero and a guard rejects every drive command.
The caller must stop the normal application before executing this diagnostic.
Only boolean/count results are printed; synthetic names/keys stay in memory.
"""

from machine import Pin

Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)

import gc
import json
import time
from hardware import Instrument
from display import Display
from engine import Engine
from ui import App
from wifi import WifiManager, WPA2
import model


class QueuedInputs:
    def __init__(self):
        self.events = []

    def poll(self, now_ms):
        events, self.events = self.events, []
        return events


class DriveGuard:
    def __init__(self, hardware):
        self.hardware = hardware
        self.identity = hardware.identity
        self.commands = 0

    def set_duty(self, role, fraction):
        self.commands += 1
        raise RuntimeError("Unexpected drive during Wi-Fi UI verification")

    def read_pair(self):
        return self.hardware.read_pair()

    def zero(self):
        self.hardware.zero()


class MemoryStore:
    def __init__(self):
        self.profiles = [{"ssid": "Synthetic-%02d" % index,
                          "password": "synthetic-key-only", "security": WPA2}
                         for index in range(20)]
        self.loads = 0
        self.writes = 0

    def load_json(self, path):
        assert path == "memory-only-wifi.json", "Unexpected storage read"
        self.loads += 1
        return {"version": 1, "profiles": [dict(item) for item in self.profiles]}

    def save_json_atomic(self, path, data):
        assert path == "memory-only-wifi.json", "Unexpected storage write"
        self.profiles = [dict(item) for item in data["profiles"]]
        self.writes += 1

    def save_settings(self, settings):
        raise RuntimeError("Wi-Fi UI diagnostic must not save scanner settings")


class FakeRadio:
    def __init__(self):
        self.active = False
        self.connected = False
        self.scans = 0
        self.joins = 0
        self.last_key = None

    def activate(self):
        self.active = True

    def deactivate(self):
        self.active = self.connected = False
        return True

    def scan(self):
        self.scans += 1
        rows = [(('Synthetic-%02d' % index).encode(), b'abcdef', 1,
                 -30 - index, 5, 1) for index in range(9)]
        rows.append((b'Synthetic-05', b'ghijkl', 6, -90, 5, 1))
        return rows

    def connect(self, ssid, password, security):
        self.joins += 1
        self.last_key = password
        self.connected = False  # The test explicitly completes this fake join.

    def disconnect(self):
        self.connected = False

    def isconnected(self):
        return self.connected

    def status(self):
        return 3 if self.connected else 1

    def ip(self):
        return "192.0.2.1"


def pump():
    app.tick(time.ticks_ms())
    assert guard.commands == 0, "UI attempted to enable a driver"
    assert app.engine.state == "idle", "UI unexpectedly started acquisition"


def footer(busy=False):
    if app.dirty:
        app.render()
    left = [item for item in app.hits if item[4] == "left"]
    right = [item for item in app.hits if item[4] == "right"]
    assert len(left) == 1 and left[0][0] < 240 and left[0][1] >= 270
    if busy:
        assert not right, "Busy OK button should be disabled"
    else:
        assert len(right) == 1 and right[0][0] >= 240 and right[0][1] >= 270


def tap(action):
    if app.dirty:
        app.render()
    for x, y, width, height, candidate in app.hits:
        if candidate == action:
            inputs.events = [("touch", x + width // 2, y + height // 2)]
            pump()
            return
    # Do not include action payloads: keyboard actions may contain key text.
    raise AssertionError("Expected Wi-Fi touch target is missing")


def press(side):
    inputs.events = [(side,)]
    pump()


hardware = Instrument()
manager = None
try:
    hardware.zero()
    guard = DriveGuard(hardware)
    inputs, memory, radio = QueuedInputs(), MemoryStore(), FakeRadio()
    manager = WifiManager(adapter=radio, path="memory-only-wifi.json", store=memory)
    app = App(Display(), inputs, Engine(guard), dict(model.DEFAULT_SETTINGS),
              calibration=None, store=memory, wifi=manager)
    pump()
    footer()
    tap("left")
    tap("advanced")
    tap("wifi")
    assert app.screen == "wifi"
    footer()

    # Search is painted before execution, leaving a cancellation opportunity.
    tap("wifi_scan")
    assert app.screen == "wifi_search" and radio.scans == 0
    footer(busy=True)
    tap("left")
    assert app.screen == "wifi" and radio.scans == 0
    tap("wifi_scan")
    assert app.screen == "wifi_search" and radio.scans == 0
    pump()
    assert app.screen == "wifi_list" and radio.scans == 1
    assert len(app._wifi_rows) == 9
    footer()
    tap("wifi_next")
    assert app._wifi_page == 1
    tap("wifi_prev")
    assert app._wifi_page == 0
    tap("wifi_next")
    tap(("wifi_select", 5))
    assert app.screen == "wifi_network" and app._wifi_selected["saved"]
    footer()
    tap("wifi_use_saved")
    assert app.screen == "wifi_status" and manager.state == "connecting"
    footer(busy=True)
    assert radio.last_key == "synthetic-key-only"
    radio.connected = True
    time.sleep_ms(260)
    pump()
    assert manager.state == "connected" and len(manager.saved_networks()) == 20
    assert manager.saved_networks()[0]["ssid"] == "Synthetic-05"
    footer()
    press("right")
    assert app.screen == "wifi"

    # Saved-list pagination uses the complete twenty-profile MRU list.
    tap("wifi_saved")
    assert app.screen == "wifi_list" and len(app._wifi_rows) == 20
    for expected in range(1, 5):
        tap("wifi_next")
        assert app._wifi_page == expected
    for expected in range(3, -1, -1):
        tap("wifi_prev")
        assert app._wifi_page == expected
    tap(("wifi_select", 0))
    tap("wifi_password")
    assert app.screen == "wifi_password" and not app._wifi_show
    footer()
    before = [dict(item) for item in memory.profiles]
    writes_before, joins_before = memory.writes, radio.joins
    tap(("wifi_char", "a"))
    tap(("wifi_mode", "ABC"))
    tap(("wifi_char", "A"))
    tap(("wifi_mode", "#+="))
    tap(("wifi_char", "!"))
    tap(("wifi_char", " "))
    tap(("wifi_mode", "123"))
    tap(("wifi_char", "7"))
    tap(("wifi_mode", "ABC"))
    tap(("wifi_mode", "abc"))
    tap(("wifi_char", "x"))
    tap(("wifi_char", "y"))
    tap(("wifi_mode", "ABC"))
    tap(("wifi_char", "Z"))
    assert app._wifi_password == "aA! 7xyZ"
    tap("wifi_backspace")
    assert app._wifi_password == "aA! 7xy"
    tap(("wifi_char", "Z"))
    tap("wifi_show")
    assert app._wifi_show
    tap("wifi_show")
    assert not app._wifi_show
    press("left")
    assert app.screen == "wifi_network" and app._wifi_password == "" and not app._wifi_show
    assert before == memory.profiles and memory.writes == writes_before
    assert radio.joins == joins_before and len(manager.saved_networks()) == 20
    assert all("password" not in row for row in manager.saved_networks())
    assert "synthetic-key-only" not in json.dumps(manager.status)

    # Cancel a pending saved-profile join without changing the cache.
    tap("wifi_use_saved")
    assert manager.state == "connecting"
    footer(busy=True)
    tap("left")
    assert app.screen == "wifi" and manager.state == "off"
    assert before == memory.profiles and memory.writes == writes_before
    tap("right")
    assert app.screen == "advanced"
    tap("left")
    assert app.screen == "settings"
    tap("left")
    assert app.screen == "home"
    footer()
    assert memory.loads == 1 and guard.commands == 0
    gc.collect()
    print("WIFI_UI_NATIVE", json.dumps({
        "scan_cancel_before_execution": True, "deduplicated_rows": 9,
        "saved_profiles": 20, "saved_password_reused": True,
        "mru_updated": True, "saved_list_pages": 5,
        "keyboard_modes_and_backspace": True, "password_show_hide": True,
        "cancel_preserved_cache": True, "pending_join_cancelled": True,
        "footer_mapping_verified": True, "returned_home": True,
        "outputs_never_driven": True, "no_real_credentials_or_network": True,
        "free_heap_bytes": gc.mem_free()}))
finally:
    hardware.close()
    if manager is not None:
        manager.cancel()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print("OUTPUTS_DISABLED")
