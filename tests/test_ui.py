"""Settings and controls with the real direct-drive scan engine; no hardware."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from firmware import model, storage
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App


class FakeDisplay:
    width, height = 480, 320
    def __init__(self):
        self.labels = []
    def fill(self, color):
        self.labels = []
    def text(self, text, x, y, color, bg=None, scale=1):
        self.labels.append(str(text))
    def fill_rect(self, *args):
        pass
    rect = fill_rect
    line = fill_rect


class FakeInputs:
    def __init__(self):
        self.events = []
    def poll(self, now):
        events, self.events = self.events, []
        return events


class MemoryStore:
    def __init__(self):
        self.settings = None
        self.files = {}
        self.settings_error = None
        self.writes = 0
    def save_settings(self, settings):
        if self.settings_error:
            raise self.settings_error
        value = model.validate_settings(settings)
        self.settings = deepcopy(value)
        self.writes += 1
        return value
    def save_json_atomic(self, path, value):
        self.files[path] = deepcopy(value)
        self.writes += 1
    def load_json(self, path, default=None):
        return deepcopy(self.files.get(path, default))


class FileStore(MemoryStore):
    def __init__(self, folder):
        super().__init__()
        self.path = str(Path(folder) / "settings.json")
    def save_settings(self, value):
        return storage.save_settings(value, self.path)


class HostInstrument:
    identity = {"test": "host simulation"}
    def __init__(self):
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.commands, self.measurements = [], []
        self.reads = self.zeroes = 0
        self.error = None
    def begin_measurement(self, average_count):
        self.measurements.append(average_count)
    def poll_measurement(self):
        return True
    def poll_alerts(self):
        pass
    def set_duty(self, role, value):
        self.commands.append((role, value))
        self.duties[role] = value
    def zero(self):
        self.zeroes += 1
        self.duties = {"drain": 0.0, "gate": 0.0}
    def read_pair(self):
        self.reads += 1
        if self.error:
            raise self.error
        result = {}
        for role, value in self.duties.items():
            # Independent as-built resistor math, with an intentionally raw
            # finite current offset; production must not subtract a baseline.
            terminal = 3.3 + value * 22.44 if role == "drain" else value * 25.74
            shunt = -2.5e-6 if role == "drain" else 1.25e-6
            result[role] = {"terminal_v": terminal, "shunt_v": shunt,
                            "bus_v": terminal + shunt}
        return result


def finish_scan_preparation(test):
    for _ in range(8):
        if test.app.screen != "preparing":
            break
        test.tick()
    else:
        test.fail("Scan preparation did not finish")
    if test.app.screen == "scan":
        test.tick()


class UITests(unittest.TestCase):
    def setUp(self):
        from test_ram_ui import RamCloud
        self.display, self.inputs = FakeDisplay(), FakeInputs()
        self.instrument, self.store = HostInstrument(), MemoryStore()
        self.engine = Engine(self.instrument)
        self.settings = dict(model.DEFAULT_SETTINGS, drain_start_uv=100000,
                             drain_stop_uv=1100000, drain_step_uv=1000000,
                             gate_start_uv=1000000, gate_stop_uv=1000000,
                             gate_step_uv=1000000, ina_average_count=4)
        self.buffer = PackedScanBuffer(capacity=32)
        self.cloud = RamCloud(self.buffer)
        self.restarts = []
        self.app = App(self.display, self.inputs, self.engine, self.settings,
                       store=self.store, cloud=self.cloud, scan_buffer=self.buffer,
                       restart=lambda: self.restarts.append(True))
        self.now = 0
        self.tick()
    def tick(self, events=()):
        self.now += 20
        self.inputs.events = list(events)
        self.app.tick(self.now)
    def press(self, name):
        self.tick(((name,),))
    def touch_action(self, action):
        for x, y, width, height, target in self.app.hits:
            if target == action:
                self.tick((("touch", x + width // 2, y + height // 2),))
                return
        self.fail("Missing touch target %r on %s" % (action, self.app.screen))
    def open_averaging(self):
        self.press("left")
        self.touch_action("advanced")
        self.touch_action("averaging")

    def test_home_ready_without_calibration_or_any_file_read(self):
        self.assertIn("READY TO SCAN", self.display.labels)
        self.assertFalse(hasattr(self.app, "calibration"))
        self.assertFalse(hasattr(self.engine, "calibration"))
        self.assertEqual([hit[4] for hit in self.app.hits], ["left", "right"])
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])

    def test_advanced_offers_calibration_without_driving_on_entry(self):
        self.press("left")
        self.touch_action("advanced")
        actions = [hit[4] for hit in self.app.hits]
        self.assertTrue(all(name in actions for name in ("averaging", "calibrate", "wifi", "cloud")))
        self.assertNotIn("storage", actions)
        self.app.action("calibrate")
        self.assertEqual(self.app.screen, "calibration_confirm")
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.store.writes, 0)

    def test_decimal_keypad_commits_exact_microvolts_and_settings_exit_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(folder)
            self.app.store = store
            self.press("left")
            self.touch_action(("edit", "drain_stop_uv", "VDS stop (V)"))
            for key in "2.000001":
                self.touch_action(("key", key))
            self.press("right")
            self.assertIs(type(self.app.draft["drain_stop_uv"]), int)
            self.assertEqual(self.app.draft["drain_stop_uv"], 2000001)
            self.press("right")
            self.assertEqual(storage.load_settings(store.path)["drain_stop_uv"], 2000001)
            self.assertEqual(self.restarts, [True])

    def test_submicrovolt_editor_error_keeps_draft_and_does_not_drive(self):
        self.press("left")
        original = self.app.draft["drain_stop_uv"]
        self.touch_action(("edit", "drain_stop_uv", "VDS stop (V)"))
        self.app.edit_value = "0.0000001"
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.app.draft["drain_stop_uv"], original)
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.store.writes, 0)

    def test_averaging_supported_choices_do_not_require_calibration(self):
        self.open_averaging()
        actions = [hit[4] for hit in self.app.hits]
        self.assertEqual([a[1] for a in actions if isinstance(a, tuple)], list(model.INA_AVERAGE_COUNTS))
        self.touch_action(("ina_average", 1024))
        self.assertFalse(any("calibr" in label.lower() for label in self.display.labels))
        self.press("right")
        self.press("right")
        self.press("right")
        self.assertEqual(self.store.settings["ina_average_count"], 1024)
        self.assertEqual(self.restarts, [True])

    def test_averaging_cancel_restores_draft_and_settings_cancel_does_not_save(self):
        self.open_averaging()
        self.touch_action(("ina_average", 256))
        self.press("left")
        self.assertEqual(self.app.draft["ina_average_count"], 4)
        self.press("left")
        self.press("left")
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.restarts, [True])

    def test_save_failure_preserves_draft_and_does_not_restart(self):
        self.press("left")
        self.app.draft["drain_stop_uv"] = 2000000
        self.store.settings_error = OSError("settings unavailable")
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.app.draft["drain_stop_uv"], 2000000)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.restarts, [])

    def test_unsupported_average_action_cannot_change_draft(self):
        self.open_averaging()
        self.app.action(("ina_average", 5))
        self.assertEqual(self.app.draft["ina_average_count"], 4)
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.instrument.commands, [])

    def test_simultaneous_buttons_do_not_start_scan_after_opening_settings(self):
        self.tick((("left",), ("right",)))
        self.assertEqual(self.app.screen, "settings")
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.restarts, [])

    def test_footer_roles_and_numeric_editor_cancel(self):
        self.assertIn("SETTINGS", self.display.labels)
        self.assertIn("SCAN", self.display.labels)
        self.press("left")
        self.assertIn("CANCEL", self.display.labels)
        self.assertIn("OK", self.display.labels)
        previous = dict(self.app.draft)
        self.touch_action(("edit", "gate_start_uv", "VGS start (V)"))
        self.app.edit_value = "-2.000001"
        self.press("left")
        self.assertEqual(self.app.draft, previous)


if __name__ == "__main__":
    unittest.main()
