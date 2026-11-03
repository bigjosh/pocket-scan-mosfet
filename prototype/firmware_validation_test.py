"""Exercise invalid voltage ranges on actual MicroPython without output drive."""
import time
import json
import hashlib
from hardware import Instrument
from display import Display
from engine import Engine
from ui import App
import storage


class NoInputs:
    def poll(self, now):
        return []


def settings_hash():
    digest = hashlib.sha256()
    with open("settings.json", "rb") as stream:
        while True:
            chunk = stream.read(256)
            if not chunk:
                break
            digest.update(chunk)
    return digest.digest()


instrument = Instrument()
try:
    settings = storage.load_settings()
    original = settings_hash()
    app = App(Display(), NoInputs(), Engine(instrument), settings,
              storage.load_json("calibration.json"))
    messages = []
    for role, label in (("drain", "Drain"), ("gate", "Gate")):
        app.render()
        app.action("left")
        app.draft[role + "_stop_uv"] = 20000000
        app.action("right")
        app.tick(time.ticks_ms())
        assert app.screen == "notice"
        assert label + " terminal targets" in app.notice
        assert "0..5.000 V" in app.notice
        assert app.engine.state == "idle"
        assert settings_hash() == original
        messages.append(app.notice)
        app.action("right")
        assert app.screen == "settings"
        assert app.draft[role + "_stop_uv"] == 20000000
        app.action("left")
        assert app.screen == "home"
    app.render()
    print("VALIDATION_TEST", json.dumps({"messages": messages,
          "settings_preserved": True, "outputs_never_driven": True,
          "returned_home": app.screen == "home"}))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
