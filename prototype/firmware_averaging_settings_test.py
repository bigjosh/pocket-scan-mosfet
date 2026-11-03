"""Native UI/persistence check with outputs held at zero throughout.

Temporarily saves 3 batches x 7 samples, verifies reload and cancellation,
then restores the exact original settings bytes, including on failure.
No scan or calibration is started; a drive guard rejects any PWM command.
"""
import gc
import hashlib
import json
import os
import time
from hardware import Instrument
from display import Display
from engine import Engine
from ui import App
import storage


def file_hash(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            while True:
                chunk = stream.read(512)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as error:
        if error.args[0] == 2:
            return None
        raise
    return digest.digest()


class QueuedInputs:
    def __init__(self):
        self.events = []

    def poll(self, now):
        events, self.events = self.events, []
        return events


class DriveGuard:
    def __init__(self, hardware):
        self.hardware = hardware
        self.identity = hardware.identity
        self.commands = 0

    def set_duty(self, role, duty):
        self.commands += 1
        raise RuntimeError("Unexpected drive during settings-only verification")

    def read_pair(self):
        return self.hardware.read_pair()

    def zero(self):
        self.hardware.zero()


def tap(action):
    if app.dirty:
        app.render()
    for x, y, width, height, candidate in app.hits:
        if candidate == action:
            inputs.events = [("touch", x + width // 2, y + height // 2)]
            app.tick(time.ticks_ms())
            return
    raise AssertionError("Missing touch action: " + str(action))


def edit(key, value):
    if app.dirty:
        app.render()
    for hit in app.hits:
        action = hit[4]
        if isinstance(action, tuple) and action[:2] == ("edit", key):
            tap(action)
            actions = [item[4] for item in app.hits]
            assert ("key", ".") not in actions and ("key", "+/-") not in actions
            break
    else:
        raise AssertionError("Missing count editor: " + key)
    tap(("key", "CLEAR"))
    for digit in str(value):
        tap(("key", digit))
    tap("right")


hardware = Instrument()
original = None
try:
    with open("settings.json", "rb") as stream:
        original = stream.read(4097)
    assert len(original) <= 4096, "Settings unexpectedly large"
    before = {name: file_hash(name) for name in ("settings.json", "calibration.json", "last_error.log")}
    guard = DriveGuard(hardware)
    inputs = QueuedInputs()
    app = App(Display(), inputs, Engine(guard), storage.load_settings(),
              storage.load_json("calibration.json"))
    app.render()
    tap("left")
    tap("advanced")
    tap("averaging")
    assert app.screen == "averaging"
    edit("samples_per_batch", 7)
    edit("batches", 3)
    assert app.draft["samples_per_batch"] == 7 and app.draft["batches"] == 3
    assert before["settings.json"] == file_hash("settings.json")
    tap("right")  # Averaging -> Advanced, still draft only.
    tap("right")  # Advanced -> Settings.
    assert before["settings.json"] == file_hash("settings.json")
    tap("right")  # Settings OK commits both fields.
    loaded = storage.load_settings()
    assert loaded["samples_per_batch"] == 7 and loaded["batches"] == 3
    assert isinstance(loaded["samples_per_batch"], int) and isinstance(loaded["batches"], int)
    saved = file_hash("settings.json")
    tap("left")
    tap("advanced")
    tap("averaging")
    edit("samples_per_batch", 99)
    tap("left")  # Averaging Cancel restores its entry snapshot.
    assert app.draft["samples_per_batch"] == 7 and app.draft["batches"] == 3
    tap("averaging")
    notices = []
    for key, value in (("samples_per_batch", 0), ("samples_per_batch", 501),
                       ("batches", 1), ("batches", 33)):
        edit(key, value)
        assert app.screen == "notice", key + " range was accepted"
        notices.append(app.notice)
        assert file_hash("settings.json") == saved
        tap("right")  # Back to keypad; invalid entry stays editable.
        assert app.screen == "edit"
        tap("left")
        assert app.screen == "averaging"
    tap("left")
    tap("left")
    tap("left")
    assert app.screen == "home" and guard.commands == 0
    assert before["calibration.json"] == file_hash("calibration.json")
    assert before["last_error.log"] == file_hash("last_error.log")
    print("AVERAGING_SETTINGS_TEST", json.dumps({"saved_then_loaded": {"batches": 3, "samples_per_batch": 7},
          "draft_and_cancel_verified": True, "rejected_ranges": notices,
          "outputs_never_driven": True, "calibration_preserved": True, "error_log_preserved": True}))
finally:
    hardware.close()
    if original is not None and len(original) <= 4096:
        temporary = "settings.json.restore.tmp"
        with open(temporary, "wb") as stream:
            stream.write(original)
            stream.flush()
        if hasattr(os, "sync"):
            os.sync()
        os.rename(temporary, "settings.json")
        if hasattr(os, "sync"):
            os.sync()
        assert hashlib.sha256(original).digest() == file_hash("settings.json")
        print("ORIGINAL_SETTINGS_RESTORED")
    gc.collect()
    print("OUTPUTS_DISABLED")
