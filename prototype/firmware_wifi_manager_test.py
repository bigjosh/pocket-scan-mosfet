"""Native Wi-Fi checks; no real credentials are entered, printed or changed.

Real hardware: scan, start/cancel a join to a deliberately nonexistent SSID,
and verify radio shutdown. Fake radio: exercise 21 synthetic profiles using
a temporary file, including eviction, reload and failed-password preservation.
Both analog outputs remain at zero throughout.
"""
import gc
import hashlib
import json
import os
import time
from hardware import Instrument
from wifi import WifiManager


class NoCredentialStore:
    def load_json(self, path):
        return None

    def save_json_atomic(self, path, value):
        raise RuntimeError("Hardware probe cannot save credentials")


class FakeRadio:
    def __init__(self):
        self.connected = False
        self.fail = False
        self.last_key = None

    def activate(self):
        pass

    def deactivate(self):
        self.connected = False
        return True

    def disconnect(self):
        self.connected = False

    def connect(self, ssid, password, security):
        self.last_key = password
        self.connected = not self.fail

    def isconnected(self):
        return self.connected

    def ip(self):
        return "192.0.2.1"

    def status(self):
        return -3 if self.fail else 3


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            data = stream.read(256)
            if not data:
                break
            digest.update(data)
    return digest.digest()


hardware = Instrument()
manager = None
temporary = "__wifi_test_%x.json" % time.ticks_ms()
try:
    radio = FakeRadio()
    test = WifiManager(adapter=radio, path=temporary)
    for index in range(21):
        now = time.ticks_ms()
        assert test.connect("synthetic-%02d" % index, "synthetic-key-only", now_ms=now)
        test.tick(now)
        assert test.state == "connected"
    saved = test.saved_networks()
    assert len(saved) == 20 and saved[0]["ssid"] == "synthetic-20"
    assert not test.has_saved("synthetic-00")
    del test
    gc.collect()
    test = WifiManager(adapter=radio, path=temporary)
    assert len(test.saved_networks()) == 20
    assert test.connect("synthetic-01", use_saved=True, now_ms=time.ticks_ms())
    test.tick(time.ticks_ms())
    assert test.saved_networks()[0]["ssid"] == "synthetic-01"
    test.cancel()
    before = file_hash(temporary)
    radio.fail = True
    assert test.connect("synthetic-01", "incorrect-test-key", now_ms=time.ticks_ms())
    test.tick(time.ticks_ms())
    assert test.state == "error" and before == file_hash(temporary)
    radio.fail = False
    assert test.connect("synthetic-01", use_saved=True, now_ms=time.ticks_ms())
    assert radio.last_key == "synthetic-key-only"
    test.tick(time.ticks_ms())
    assert "synthetic-key-only" not in json.dumps(test.status)
    assert all("password" not in row for row in test.saved_networks())
    assert "synthetic-key-only" not in json.dumps(test.saved_networks())
    test.pause()
    print("WIFI_CACHE_NATIVE", json.dumps({"synthetic_profiles_tested": 21,
          "capacity": 20, "reload_verified": True, "mru_verified": True,
          "failed_replacement_preserved": True, "no_real_credentials_used": True}))

    manager = WifiManager(store=NoCredentialStore())
    manager.pause()
    manager.resume(time.ticks_ms())
    started = time.ticks_ms()
    rows = manager.scan()
    assert rows and len(rows) <= 32
    assert all("password" not in row for row in rows)
    report = {"visible_networks": len(rows), "supported_networks": sum(row["supported"] for row in rows),
              "scan_ms": time.ticks_diff(time.ticks_ms(), started)}
    # This unique name is not selected from visible access points.
    name = "pocket-probe-%x" % time.ticks_ms()
    assert not any(row["ssid"] == name for row in rows)
    assert manager.connect(name, "synthetic-probe-only", now_ms=time.ticks_ms()), manager.status["message"]
    time.sleep_ms(300)
    manager.tick(time.ticks_ms())
    assert manager.state in ("connecting", "error")
    assert manager.cancel() and manager.state == "off"
    assert manager.pause()
    report.update({"join_api_verified": True, "cancel_verified": True,
                   "radio_shutdown_verified": True, "saved_credentials_unchanged": True})
    print("WIFI_HARDWARE_NATIVE", json.dumps(report))
finally:
    hardware.close()
    if manager is not None:
        manager.pause()
    try:
        os.remove(temporary)
    except OSError as error:
        if error.args[0] != 2:
            raise
    print("RADIO_AND_OUTPUTS_DISABLED")
