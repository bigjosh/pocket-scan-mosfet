"""Native cloud UI/compact archive diagnostic with no drive or real network.

Stop the normal app before running. Real display and Instrument are used, but a
DriveGuard rejects every drive command and a FakeEngine emits synthetic rows.
Only a new cloud-ui-native-test directory is written; credentials/configuration
and scanner settings are memory-only. A pre-existing test directory is refused.
"""

from machine import Pin

Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)

import gc
import json
import os
import time
from hardware import Instrument
from display import Display
from archive import Archive, MAGIC
from cloud import CloudManager
from ui import App, WIFI_KEYS
import model

TEST_DIR = "cloud-ui-native-test"
TEST_DEVICE = "c10d"
TEST_URL = "https://script.google.com/macros/s/EXAMPLE_ONLY/exec"
MEMORY_CONFIG = "memory-only-cloud.json"


class QueuedInputs:
    def __init__(self):
        self.events = []

    def poll(self, now_ms):
        events, self.events = self.events, []
        return events


class DriveGuard:
    def __init__(self, hardware):
        self.hardware, self.commands = hardware, 0

    def set_duty(self, role, fraction):
        self.commands += 1
        raise RuntimeError("Unexpected drive during cloud UI verification")

    def zero(self):
        self.hardware.zero()


class MemoryConfig:
    def __init__(self):
        self.data = None
        self.loads = self.writes = 0

    def load_json(self, path):
        assert path == MEMORY_CONFIG, "Unexpected configuration read"
        self.loads += 1
        return None if self.data is None else dict(self.data)

    def save_json_atomic(self, path, value):
        assert path == MEMORY_CONFIG, "Unexpected configuration write"
        self.data = dict(value)
        self.writes += 1

    def save_settings(self, value):
        raise RuntimeError("Cloud UI test must not write scanner settings")


class FakeWifi:
    def __init__(self):
        self.state = "off"
        self.pauses, self.resumes = 0, 0

    def pause(self):
        self.pauses += 1
        self.state = "paused"
        return True

    def resume(self, now_ms=0):
        self.resumes += 1
        self.state = "off"

    def tick(self, now_ms):
        pass


class FakeTransport:
    def __init__(self):
        self.starts, self.closes = 0, 0

    def start(self, *args):
        self.starts += 1
        raise RuntimeError("Offline diagnostic must not start an upload")

    def step(self):
        raise RuntimeError("Offline diagnostic must not step an upload")

    def close(self):
        self.closes += 1


class FakeEngine:
    def __init__(self, instrument):
        self.instrument = instrument
        self.state, self.latest, self.progress = "idle", None, 0.0
        self.shutdown_succeeded = True  # Caller has already zeroed hardware.
        self._pending = []

    def start_scan(self, settings, calibration):
        model.validate_scan_calibration(calibration, settings)
        self.settings = dict(settings)
        self.state, self.progress = "scanning", 0.0
        self.shutdown_succeeded = False
        self._index, self._pending = 0, []

    def start_calibration(self, settings):
        raise RuntimeError("Cloud UI test must not start calibration")

    def cancel(self):
        self.instrument.zero()
        self.shutdown_succeeded = True
        self.state = "cancelled"
        self._pending = [("cancelled", {})]

    def tick(self, now_ms):
        if self._pending:
            result, self._pending = self._pending, []
            return result
        if self.state != "scanning":
            return []
        index = self._index
        row = {"index": index, "curve": 0, "requested_vds": float(index),
               "requested_vgs": 1.0, "vds": float(index), "vgs": 1.0,
               "id_a": (index + 1) * 1e-6, "ig_a": 0.02e-6,
               "id_std_a": 0.01e-6, "ig_std_a": 0.001e-6,
               "drain_shunt_v": 10e-6, "gate_shunt_v": 20e-6,
               "drain_shunt_std_v": 1e-6, "gate_shunt_std_v": 1e-6,
               "batches_completed": self.settings["batches"],
               "drain_voltage_limited": False, "gate_voltage_limited": False}
        self._index += 1
        self.latest, self.progress = row, self._index / 2
        events = [("point", row)]
        if self._index == 2:
            self.instrument.zero()
            self.shutdown_succeeded = True
            self.state = "complete"
            events.append(("complete", {"points": 2}))
        return events


def calibration_fixture():
    return {"version": 1, "metadata": {"driver_supply_v": 5.0, "pwm_hz": model.PWM_HZ},
            "channels": {role: {"shunt_ohm": shunt,
                "points": [{"terminal_v": voltage, "duty": voltage / model.PWM_FULL_SCALE_V,
                            "shunt_v": 0.0} for voltage in (0.0, 1.5, 3.0)]}
                for role, shunt in (("drain", model.DRAIN_SHUNT_OHM), ("gate", model.GATE_SHUNT_OHM))}}


def pump():
    app.tick(time.ticks_ms())
    assert guard.commands == 0, "Unexpected output drive"
    assert transport.starts == 0, "Unexpected upload attempt"


def tap(action):
    if app.dirty:
        app.render()
    for x, y, width, height, candidate in app.hits:
        if candidate == action:
            inputs.events = [("touch", x + width // 2, y + height // 2)]
            pump()
            return
    raise AssertionError("Expected cloud touch target is missing")


def footer():
    if app.dirty:
        app.render()
    left = [hit for hit in app.hits if hit[4] == "left"]
    right = [hit for hit in app.hits if hit[4] == "right"]
    assert len(left) == 1 and left[0][0] < 240 and left[0][1] >= 270
    assert len(right) == 1 and right[0][0] >= 240 and right[0][1] >= 270


def type_text(value):
    for char in value:
        if not any(char in row for row in WIFI_KEYS[app._cloud_keyboard]):
            for mode in ("abc", "ABC", "123", "#+="):
                if any(char in row for row in WIFI_KEYS[mode]):
                    # Alpha toggle from another page first opens uppercase.
                    if mode == "abc" and app._cloud_keyboard != "ABC":
                        tap(("cloud_mode", "ABC"))
                    tap(("cloud_mode", mode))
                    break
        tap(("cloud_char", char))


def cleanup_created_directory():
    # This directory was created exclusively by this invocation. Never remove
    # unknown entries or recurse into a directory based on device file data.
    allowed = (".writing", ".draft", ".pending", ".sent", ".meta", ".meta.tmp")
    for name in os.listdir(TEST_DIR):
        if (not name.startswith(TEST_DEVICE + "-") or "/" in name or "\\" in name
                or not any(name.endswith(suffix) for suffix in allowed)):
            raise RuntimeError("Unexpected file in native diagnostic directory")
        os.remove(TEST_DIR + "/" + name)
    os.rmdir(TEST_DIR)


hardware = Instrument()
created = False
manager = None
archive = None
try:
    hardware.zero()
    os.mkdir(TEST_DIR)  # Refuse a pre-existing directory instead of overwriting it.
    created = True
    guard = DriveGuard(hardware)
    inputs, memory, wifi, transport = QueuedInputs(), MemoryConfig(), FakeWifi(), FakeTransport()
    archive = Archive(TEST_DIR, device_id=TEST_DEVICE)
    manager = CloudManager(archive=archive, transport=transport, store=memory, path=MEMORY_CONFIG)
    settings = dict(model.DEFAULT_SETTINGS)
    settings.update(drain_start_uv=0, drain_stop_uv=1000000, drain_step_uv=1000000,
                    gate_start_uv=1000000, gate_stop_uv=1000000, gate_step_uv=1000000,
                    batches=2, samples_per_batch=1)
    app = App(Display(), inputs, FakeEngine(guard), settings, calibration_fixture(),
              store=memory, wifi=wifi, cloud=manager)
    pump()
    footer()
    buffer_ids = (id(app.rows._data), id(app.curves._data), id(app.limit_flags._data))
    assert len(app.rows._data) == 32768

    # Synthetic averaged points pass through the real archive lifecycle.
    tap("right")
    assert app.screen == "scan" and manager.status["pending_count"] == 0
    pump()
    assert app.screen == "complete" and app._archive_ready
    assert manager.status["pending_count"] == 0
    assert len(app.rows) == 4 and len(app.curves) == 2
    footer()
    tap("right")
    assert app.screen == "home" and manager.status["pending_count"] == 1
    pending = [name for name in os.listdir(TEST_DIR) if name.endswith(".pending")]
    assert len(pending) == 1
    with open(TEST_DIR + "/" + pending[0], "rb") as stream:
        assert stream.read(len(MAGIC)) == MAGIC, "Expected compact binary scan"
    tap("right")
    pump()
    assert app.screen == "complete"
    tap("left")
    assert app.screen == "home" and manager.status["pending_count"] == 1
    assert not any(name.endswith(".draft") or name.endswith(".writing") for name in os.listdir(TEST_DIR))
    assert (id(app.rows._data), id(app.curves._data), id(app.limit_flags._data)) == buffer_ids

    tap("left")
    tap("advanced")
    tap("cloud")
    assert app.screen == "cloud"
    footer()
    tap("cloud_url")
    assert app.screen == "cloud_url" and not app._cloud_show
    type_text("discarded")
    tap("cloud_backspace")
    assert app._cloud_url_edit == "discarde"
    tap("cloud_show")
    assert app._cloud_show
    tap("cloud_show")
    assert not app._cloud_show
    tap("left")
    assert app._cloud_url_edit == "" and app._cloud_url_draft == "" and not app._cloud_show
    tap("cloud_toggle")
    tap("left")
    assert app.screen == "advanced" and memory.writes == 0
    assert manager.config_for_edit() == {"url": "", "enabled": False}

    tap("cloud")
    tap("cloud_url")
    tap("cloud_clear")
    type_text(TEST_URL)
    assert app._cloud_url_edit == TEST_URL and not app._cloud_show
    footer()
    tap("right")
    assert app.screen == "cloud" and app._cloud_url_draft == TEST_URL
    assert app._cloud_url_edit == "" and memory.writes == 0
    tap("cloud_toggle")
    tap("right")
    assert app.screen == "advanced" and memory.writes == 1
    assert manager.config_for_edit() == {"url": TEST_URL, "enabled": True}
    tap("left")
    tap("left")
    assert app.screen == "home" and memory.writes == 1
    reloaded = CloudManager(archive=archive, transport=transport, store=memory, path=MEMORY_CONFIG)
    assert reloaded.config_for_edit() == {"url": TEST_URL, "enabled": True}
    assert reloaded.status["pending_count"] == 1
    assert TEST_URL not in json.dumps(manager.status)
    assert guard.commands == 0 and transport.starts == 0
    assert wifi.pauses == 2 and wifi.resumes == 2
    gc.collect()
    print("CLOUD_UI_NATIVE", json.dumps({
        "synthetic_points_per_scan": 2, "binary_archive_verified": True,
        "saved_only_on_ok": True, "second_cancel_preserved_pending": True,
        "config_cancel_preserved_previous": True, "url_editor_masked": True,
        "keyboard_show_clear_backspace": True, "config_saved_and_reloaded": True,
        "scanner_settings_untouched": True, "fixed_plot_bytes": len(app.rows._data),
        "fixed_buffers_reused": True, "footer_mapping_verified": True,
        "outputs_never_driven": True, "no_real_credentials_or_network": True,
        "free_heap_bytes": gc.mem_free()}))
finally:
    hardware.close()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    if manager is not None:
        manager.pause()
    if archive is not None:
        archive.discard_scan()
    if created:
        cleanup_created_directory()
    print("OUTPUTS_DISABLED")
