"""Isolated native storage UI check; no acquisition, WLAN or user-file reads.

Stop the normal application first. Pins20/21 stay low. The only filesystem
changes are three synthetic scans in a newly created __storage_ui_probe folder;
an existing folder is refused. Set STORAGE_UI_REAL_DISPLAY=False for host replay
with a stub machine.Pin. All scanner/cloud settings and calibration are synthetic.
"""

from machine import Pin

Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
# Match production's early fixed plot allocation without importing the UI.
plot_buffers = (bytearray(32 * 1024), bytearray(8 * 1024), bytearray(4 * 1024))

import os
import time
import json
import hashlib
import binascii
import gc

try:
    from firmware.archive import Archive, COLUMNS
    from firmware.cloud import CloudManager
    from firmware.ui import App
    from firmware import model
except ImportError:
    from archive import Archive, COLUMNS
    from cloud import CloudManager
    from ui import App
    import model

TEST_DIR = "__storage_ui_probe"
TEST_DEVICE = "570a"
MEMORY_CONFIG = "__memory_storage_cloud"


class QueuedInputs:
    def __init__(self):
        self.events = []

    def poll(self, now_ms):
        events, self.events = self.events, []
        return events


class NoDriveEngine:
    state, latest, progress = "idle", None, 0.0
    shutdown_succeeded = True

    def __init__(self):
        self.starts = 0

    def start_scan(self, *unused):
        self.starts += 1
        raise AssertionError("Storage UI must not start acquisition")

    start_calibration = start_scan

    def tick(self, now_ms):
        return []


class MemoryStore:
    def load_json(self, path):
        assert path == MEMORY_CONFIG, "Unexpected configuration read"
        return None

    def save_json_atomic(self, *unused):
        raise AssertionError("Storage UI must not save configuration")

    save_settings = save_json_atomic


class NoNetwork:
    starts = 0

    def start(self, *unused):
        self.starts += 1
        raise AssertionError("Storage UI must not start a network request")

    step = start

    def close(self):
        pass


class TestDisplay:
    width, height = 480, 320

    def __init__(self, real=None):
        self.real, self.labels = real, []

    def fill(self, *args):
        self.labels = []
        if self.real is not None:
            self.real.fill(*args)

    def text(self, value, *args, **kwargs):
        self.labels.append(str(value))
        if self.real is not None:
            self.real.text(value, *args, **kwargs)

    def fill_rect(self, *args):
        if self.real is not None:
            self.real.fill_rect(*args)

    def rect(self, *args):
        if self.real is not None:
            self.real.rect(*args)

    def line(self, *args):
        if self.real is not None:
            self.real.line(*args)


class SpaceBudget:
    simulated = False
    free_start, initial_used = 0, 0

    def used(self):
        return sum(os.stat(TEST_DIR + "/" + name)[6] for name in os.listdir(TEST_DIR))

    def __call__(self):
        return self.free_start + self.initial_used - self.used() if self.simulated else 4 * 1024 * 1024


def calibration_fixture():
    return {"version": 1, "metadata": {"driver_supply_v": 5.0, "pwm_hz": model.PWM_HZ},
            "channels": {role: {"shunt_ohm": shunt, "points": [
                {"terminal_v": voltage, "duty": voltage / model.PWM_FULL_SCALE_V, "shunt_v": 0.0}
                for voltage in (0.0, 3.0)]}
                for role, shunt in (("drain", model.DRAIN_SHUNT_OHM), ("gate", model.GATE_SHUNT_OHM))}}


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(512)
            if not block:
                break
            digest.update(block)
    return binascii.hexlify(digest.digest()).decode()


def pump():
    now = time.ticks_ms() if hasattr(time, "ticks_ms") else int(time.monotonic() * 1000)
    app.tick(now)
    assert engine.starts == 0 and engine.state == "idle", "Unexpected acquisition"
    assert transport.starts == 0, "Unexpected network request"


def tap(action):
    if app.dirty:
        app.render()
    for x, y, width, height, candidate in app.hits:
        if candidate == action:
            inputs.events = [("touch", x + width // 2, y + height // 2)]
            pump()
            return
    raise AssertionError("Expected storage touch target is missing")


def footer():
    if app.dirty:
        app.render()
    left = [hit for hit in app.hits if hit[4] == "left"]
    right = [hit for hit in app.hits if hit[4] == "right"]
    assert len(left) == len(right) == 1
    assert left[0][0] < 240 and right[0][0] >= 240
    assert left[0][1] >= 270 and right[0][1] >= 270


def cleanup(directory, identifiers):
    assert directory == TEST_DIR and "/" not in directory and "\\" not in directory
    allowed = set(scan_id + suffix for scan_id in identifiers
                  for suffix in (".writing", ".draft", ".pending", ".sent", ".meta", ".meta.tmp"))
    names = os.listdir(directory)
    assert all(name in allowed for name in names), "Unexpected file in isolated storage probe"
    for name in names:
        os.remove(directory + "/" + name)
    os.rmdir(directory)


created, manager = False, None
identifiers = []
try:
    os.mkdir(TEST_DIR)  # Failure on an existing folder is intentional.
    created = True
    budget = SpaceBudget()
    counter = [0]
    def random_bytes(count):
        assert count == 8
        counter[0] += 1
        return bytes((0, 0, 0, 0, 0, 0, 0, counter[0]))
    archive = Archive(TEST_DIR, device_id=TEST_DEVICE, free_bytes=budget,
                      random_bytes=random_bytes)
    settings = dict(model.DEFAULT_SETTINGS)
    settings.update(drain_start_uv=0, drain_stop_uv=0, drain_step_uv=500000,
                    gate_start_uv=0, gate_stop_uv=0, gate_step_uv=500000,
                    batches=2, samples_per_batch=1)
    calibration = calibration_fixture()
    for index in range(3):
        scan_id = archive.begin_scan(settings, calibration, 0)
        identifiers.append(scan_id)
        point = {key: 0.0 for key in COLUMNS}
        point.update(index=0, curve=0, batches_completed=2,
                     drain_voltage_limited=False, gate_voltage_limited=False)
        archive.record_point(point)
        archive.finish_scan(10)
        archive.accept_scan()
    entries = archive.list_scans()
    first, second, remaining = [entry["scan_id"] for entry in entries]
    preserved = {suffix: file_digest(TEST_DIR + "/" + remaining + suffix)
                 for suffix in (".pending", ".meta")}
    required = archive.scan_capacity(settings)["required_bytes"]
    budget.initial_used = budget.used()
    budget.free_start = required - entries[0]["size_bytes"] - entries[1]["size_bytes"] + min(
        entries[0]["size_bytes"], entries[1]["size_bytes"]) // 2
    budget.simulated = True
    assert not archive.scan_capacity(settings)["can_start"]

    gc.collect()
    real_display = None
    if globals().get("STORAGE_UI_REAL_DISPLAY", True):
        from display import Display
        real_display = Display()
    display = TestDisplay(real_display)
    inputs, engine, memory, transport = QueuedInputs(), NoDriveEngine(), MemoryStore(), NoNetwork()
    manager = CloudManager(archive=archive, transport=transport, store=memory, path=MEMORY_CONFIG)
    gc.collect()
    app = App(display, inputs, engine, settings, calibration, store=memory, cloud=manager,
              plot_buffers=plot_buffers)
    pump()
    footer()
    tap("right")
    for unused in range(8):
        if app.screen != "preparing":
            break
        pump()
    assert app.screen == "storage" and not app._storage_capacity["can_start"]
    footer()
    tap(("storage_select", 0))
    assert app.screen == "storage_confirm"
    assert "No confirmed cloud copy." in display.labels
    footer()
    tap("left")
    assert app.screen == "storage" and archive.pending_count == 3
    assert all(file_digest(TEST_DIR + "/" + remaining + suffix) == digest
               for suffix, digest in preserved.items())

    tap(("storage_select", 0))
    tap("right")
    assert app.screen == "storage" and archive.pending_count == 2
    assert not app._storage_capacity["can_start"]
    tap(("storage_select", 0))
    tap("right")
    assert app.screen == "storage" and archive.pending_count == 1
    assert app._storage_capacity["can_start"]
    assert archive.list_scans()[0]["scan_id"] == remaining
    assert all(file_digest(TEST_DIR + "/" + remaining + suffix) == digest
               for suffix, digest in preserved.items())
    footer()
    tap("right")
    assert app.screen == "home" and engine.starts == 0 and archive._current is None
    assert manager.status["pending_count"] == 1
    print("STORAGE_UI_NATIVE", json.dumps({
        "synthetic_accepted_scans": 3, "home_scan_blocked_before_drive": True,
        "pending_warning_shown": True, "cancel_preserved_scans": True,
        "two_individually_confirmed_deletions": True,
        "capacity_refreshed_after_each_deletion": True,
        "unselected_payload_and_metadata_unchanged": True,
        "storage_ok_returns_home_without_scan": True,
        "footer_mapping_verified": True, "outputs_never_driven": True,
        "no_user_files_or_network": True}))
finally:
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    if manager is not None:
        manager.pause()
    if created:
        cleanup(TEST_DIR, identifiers)
    print("OUTPUTS_DISABLED")
