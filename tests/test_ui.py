"""Host UI flows using the real scan engine, never a serial/hardware device."""

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

from firmware import model, storage
from firmware.engine import Engine
from firmware.ui import App, CURVES, AMBER
from firmware.calibration_json import dump_calibration
from firmware.calibration_data import PackedPoints


def calibration_snapshot(value):
    stream = io.StringIO()
    dump_calibration(value, stream)
    return json.loads(stream.getvalue())


def packed_fixture(value):
    """Explicit fixture conversion; production never falls back to JSON."""
    result = deepcopy(value)
    for channel in result["channels"].values():
        if isinstance(channel["points"], PackedPoints):
            continue
        original = channel["points"]
        points = PackedPoints(len(original))
        for point in original:
            points.append(point["terminal_v"], point["shunt_v"], point.get("shunt_std_v", 0), point["duty"])
        first, last = original[0], original[-1]
        if "zero_min_v" in first:
            points.set_zero_bounds(min(first["zero_min_v"], points.value(0, "terminal_v")),
                                   max(first["zero_max_v"], points.value(0, "terminal_v")))
        if "drive_v" in last:
            points.set_drive_endpoint(last["drive_v"], last["drive_duty"])
        channel["points"] = points
    return result


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
        self.calibration_error = None
        self.writes = 0

    def save_settings(self, settings):
        if self.settings_error:
            raise self.settings_error
        value = model.validate_settings(settings)
        self.settings = deepcopy(value)
        self.writes += 1
        return value

    def save_json_atomic(self, path, value):
        if self.calibration_error:
            raise self.calibration_error
        self.files[path] = deepcopy(value)
        self.writes += 1

    def load_json(self, path):
        return deepcopy(self.files.get(path))

    def load_calibration(self):
        return self.load_json("calibration.bin")

    def save_calibration_atomic(self, value):
        return self.save_json_atomic("calibration.bin", value)


class FileStore(MemoryStore):
    def __init__(self, folder):
        super().__init__()
        self.path = str(Path(folder) / "settings.json")

    def save_settings(self, value):
        return storage.save_settings(value, self.path)


class CalibrationTransactionStore(MemoryStore):
    """Use the real atomic writer with an isolated file and observable lifetime."""
    def __init__(self, folder):
        super().__init__()
        self.path = str(Path(folder) / "calibration.bin")
        self.operations = []
        self.fail_begin = False
        self.fail_commit = False
        self.load_check = None
        self.commit_check = None
        self.writer_reference = None

    def begin_calibration_atomic(self):
        self.operations.append("begin")
        if self.fail_begin:
            raise MemoryError("1148 bytes")
        delegate = storage.begin_calibration_atomic(path=self.path)
        owner = self

        class Writer:
            def commit(self, value):
                owner.operations.append("commit")
                if owner.commit_check:
                    owner.commit_check()
                if owner.fail_commit:
                    raise MemoryError("synthetic save failure")
                delegate.commit(value)

            def abort(self):
                owner.operations.append("abort")
                delegate.abort()

        writer = Writer()
        self.writer_reference = weakref.ref(writer)
        return writer

    def load_calibration(self):
        self.operations.append("load")
        if self.load_check:
            self.load_check()
        return storage.load_calibration(path=self.path)


class HostInstrument:
    identity = {"test": "host simulation"}

    def __init__(self):
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.commands = []
        self.reads = 0
        self.zeroes = 0
        self.error = None
        self.measurements = []

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
            terminal = value * model.PWM_FULL_SCALE_V
            shunt = terminal * (0.00001 if role == "drain" else 0.0003)
            result[role] = {"terminal_v": terminal, "shunt_v": shunt,
                            "bus_v": terminal + shunt}
        return result


def fixture_calibration():
    return {
        "version": 1,
        "metadata": {"driver_supply_v": 5.0, "pwm_hz": model.PWM_HZ,
                     "acquisition_mode": model.ACQUISITION_MODE, "ina_average_count": 4,
                     "bus_conversion_us": model.INA_CONVERSION_US,
                     "shunt_conversion_us": model.INA_CONVERSION_US,
                     "calibration_repeat_blocks": model.CALIBRATION_REPEAT_BLOCKS},
        "channels": {
            role: {"shunt_ohm": resistance,
                   "points": [{"terminal_v": voltage,
                               "duty": voltage / model.PWM_FULL_SCALE_V,
                               "shunt_v": voltage * coefficient}
                              for voltage in (0.0, 1.5, 3.0)]}
            for role, resistance, coefficient in (
                ("drain", model.DRAIN_SHUNT_OHM, 0.00001),
                ("gate", model.GATE_SHUNT_OHM, 0.0003))
        },
    }


def finish_scan_preparation(test):
    """Existing end-to-end flows wait for the asynchronous SCAN handoff."""
    for _ in range(8):
        if test.app.screen != "preparing":
            break
        test.tick()
    else:
        test.fail("Scan preparation did not finish")
    if test.app.screen == "scan":
        test.tick()  # First drive only after the scan screen was painted.


class UITests(unittest.TestCase):
    def setUp(self):
        self.display = FakeDisplay()
        self.inputs = FakeInputs()
        self.instrument = HostInstrument()
        self.engine = Engine(self.instrument)
        self.store = MemoryStore()
        self.settings = dict(model.DEFAULT_SETTINGS)
        self.settings.update(drain_start_uv=0, drain_stop_uv=1000000, drain_step_uv=1000000,
                             gate_start_uv=1000000, gate_stop_uv=1000000, gate_step_uv=1000000,
                             ina_average_count=4)
        self.app = App(self.display, self.inputs, self.engine, self.settings,
                       fixture_calibration(), self.store)
        self.now = 0
        self.tick()

    def tick(self, events=()):
        self.now += 20
        self.inputs.events = list(events)
        self.app.tick(self.now)

    def press(self, name):
        starting = self.app.screen == "home" and name == "right"
        self.tick(((name,),))
        if starting:
            finish_scan_preparation(self)

    def touch_action(self, action):
        for x, y, width, height, target in self.app.hits:
            if target == action:
                starting = self.app.screen == "home" and action == "right"
                self.tick((("touch", x + width // 2, y + height // 2),))
                if starting:
                    finish_scan_preparation(self)
                return
        self.fail("Missing touch target %r on %s" % (action, self.app.screen))

    def open_calibration(self):
        self.press("left")
        self.touch_action("advanced")
        self.touch_action("calibrate")

    def open_averaging(self):
        self.press("left")
        self.touch_action("advanced")
        self.touch_action("averaging")

    def select_average(self, count):
        self.touch_action(("ina_average", count))

    def finish_operation(self):
        for _ in range(2000):
            self.tick()
            if self.engine.state not in ("scanning", "calibrating"):
                return
        self.fail("Host operation did not finish")

    def test_idle_and_settings_navigation_never_drive(self):
        self.press("left")
        self.touch_action("advanced")
        self.press("right")
        self.press("left")
        for _ in range(5):
            self.tick()
        self.assertEqual(self.app.screen, "home")
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.instrument.reads, 0)
        self.assertEqual(self.store.writes, 0)

    def test_settings_keypad_ok_persists_and_reload_keeps_values(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(folder)
            self.app.store = store
            self.press("left")
            self.touch_action(("edit", "drain_stop_uv", "VDS stop (V)"))
            for key in "2.000001":
                self.touch_action(("key", key))
            self.press("right")
            self.assertEqual(self.app.draft["drain_stop_uv"], 2000001)
            self.assertIs(type(self.app.draft["drain_stop_uv"]), int)
            self.assertEqual(self.app.settings["drain_stop_uv"], 1000000)
            self.press("right")
            self.assertEqual(self.app.screen, "home")
            loaded = storage.load_settings(store.path)
            self.assertEqual(loaded["drain_stop_uv"], 2000001)
            restarted = App(FakeDisplay(), FakeInputs(), Engine(HostInstrument()), loaded)
            self.assertEqual(restarted.settings["drain_stop_uv"], 2000001)
            restarted.action("left")
            restarted.action(("edit", "drain_stop_uv", "VDS stop (V)"))
            self.assertEqual(restarted.edit_value, "2.000001")

    def test_voltage_keypad_preserves_microvolts_and_negative_sign(self):
        self.press("left")
        for text, expected in (("0.025", 25000), ("0.000001", 1),
                               ("0.000001", -1)):
            self.touch_action(("edit", "gate_start_uv", "VGS start (V)"))
            for key in text:
                self.touch_action(("key", key))
            if expected < 0:
                self.touch_action(("key", "+/-"))
            self.press("right")
            self.assertEqual(self.app.screen, "settings")
            self.assertEqual(self.app.draft["gate_start_uv"], expected)
            self.assertIs(type(self.app.draft["gate_start_uv"]), int)

    def test_voltage_editor_rejects_sub_microvolt_values_without_rounding(self):
        self.press("left")
        for invalid in ("0.0000001", "1.0000009", "nan", "inf", ""):
            self.touch_action(("edit", "gate_start_uv", "VGS start (V)"))
            before = self.app.draft["gate_start_uv"]
            self.app.edit_value = invalid
            self.press("right")
            self.assertEqual(self.app.screen, "notice")
            self.assertEqual(self.app.draft["gate_start_uv"], before)
            self.assertEqual(self.store.writes, 0)
            self.press("right")
            self.press("left")

    def test_home_count_includes_exact_decimal_endpoints(self):
        self.app.settings.update(drain_start_uv=0, drain_stop_uv=1550000,
                                 drain_step_uv=25000, gate_start_uv=0,
                                 gate_stop_uv=1575000, gate_step_uv=25000)
        self.app.render()
        self.assertEqual(self.app._count(self.app.settings), 4032)
        self.assertIn("4032 points | INA226 averages: 4", self.display.labels)
        self.assertIn("step 0.025", self.display.labels)

    def test_legacy_settings_enter_ui_as_canonical_microvolts(self):
        legacy = {"version": 1, "drain_start_v": 0.0, "drain_stop_v": 1.3,
                  "drain_step_v": 0.1, "gate_start_v": 0.0,
                  "gate_stop_v": 0.0, "gate_step_v": 0.1}
        app = App(FakeDisplay(), FakeInputs(), Engine(HostInstrument()), legacy)
        self.assertEqual(app.settings["version"], 3)
        self.assertEqual(app.settings["drain_stop_uv"], 1300000)
        self.assertNotIn("drain_stop_v", app.settings)
        self.assertEqual(app._count(app.settings), 14)

    def test_hardware_average_choice_saves_and_requires_matching_calibration(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FileStore(folder)
            self.app.store = store
            old_calibration = self.app.calibration
            self.open_averaging()
            self.select_average(64)
            self.assertIn("Recalibration required", self.display.labels)
            self.assertIn("Conversion: 140.8 ms + settling", self.display.labels)
            self.assertFalse(Path(store.path).exists())
            self.press("right")
            self.assertEqual(self.app.draft["ina_average_count"], 64)
            self.assertEqual(self.app.settings["ina_average_count"], 4)
            self.press("right")
            self.press("right")
            loaded = storage.load_settings(store.path)
            self.assertEqual(loaded["ina_average_count"], 64)
            self.assertIs(type(loaded["ina_average_count"]), int)
            self.assertIs(self.app.calibration, old_calibration)
            self.assertIn("recalibrate", self.app.calibration_problem())
            self.assertEqual(self.instrument.commands, [])
            self.press("right")
            self.assertEqual(self.app.screen, "notice")
            self.assertIn("recalibrate", self.app.notice)
            self.assertEqual(self.instrument.commands, [])

    def test_old_acquisition_calibration_blocks_drive_but_settings_remains_reachable(self):
        self.app.calibration["metadata"].pop("acquisition_mode")
        self.app.render()
        self.assertIn("measurement mode changed", self.app.calibration_problem())
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.instrument.commands, [])
        self.press("right")
        self.press("left")
        self.assertEqual(self.app.screen, "settings")

    def test_averaging_cancel_and_outer_cancel_restore_their_own_drafts(self):
        self.press("left")
        self.touch_action("advanced")
        self.app.draft["driver_supply_uv"] = 6000000
        self.touch_action("averaging")
        self.select_average(16)
        self.press("left")
        self.assertEqual(self.app.draft["ina_average_count"], 4)
        self.assertEqual(self.app.draft["driver_supply_uv"], 6000000)
        self.touch_action("averaging")
        self.select_average(128)
        self.press("right")
        self.assertEqual(self.app.draft["ina_average_count"], 128)
        self.press("left")
        self.assertEqual(self.app.screen, "settings")
        self.assertEqual(self.app.draft["ina_average_count"], 4)
        self.assertEqual(self.app.draft["driver_supply_uv"], 5000000)
        self.touch_action("advanced")
        self.touch_action("averaging")
        self.select_average(256)
        self.press("right")
        self.press("right")
        self.press("left")
        self.assertEqual(self.app.settings["ina_average_count"], 4)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])

    def test_averaging_offers_only_eight_hardware_choices_and_voltage_keypad_stays_decimal(self):
        self.open_averaging()
        choices = [target[1] for *bounds, target in self.app.hits
                   if isinstance(target, tuple) and target[0] == "ina_average"]
        self.assertEqual(tuple(choices), model.INA_AVERAGE_COUNTS)
        self.assertIn("Calibration matches this averaging", self.display.labels)
        self.press("right")
        self.touch_action(("edit", "driver_supply_uv", "Driver supply (V+)"))
        actions = [hit[4] for hit in self.app.hits]
        self.assertIn(("key", "."), actions)
        self.assertIn(("key", "+/-"), actions)

    def test_unsupported_average_actions_do_not_change_draft_or_drive(self):
        self.open_averaging()
        for invalid in (0, 2, 3, 5, 1025, 4.0, True, "64"):
            with self.subTest(invalid=invalid):
                self.app.action(("ina_average", invalid))
                self.assertEqual(self.app.draft["ina_average_count"], 4)
                self.assertEqual(self.app.screen, "notice")
                self.assertIn("Unsupported INA226", self.app.notice)
                self.press("right")
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])

    def test_highest_average_count_is_selectable_without_numeric_keypad(self):
        self.open_averaging()
        self.select_average(1024)
        self.assertEqual(self.app.draft["ina_average_count"], 1024)
        self.assertIn("Conversion: 2252.8 ms + settling", self.display.labels)
        self.assertIn("Recalibration required", self.display.labels)

    def test_calibration_confirmation_saves_selected_hardware_average_count(self):
        self.open_averaging()
        self.select_average(16)
        self.press("right")
        self.touch_action("calibrate")
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])
        self.press("right")
        self.assertEqual(self.app.screen, "calibrating")
        self.assertEqual(self.store.settings["ina_average_count"], 16)
        self.assertEqual(self.engine.settings["ina_average_count"], 16)
        self.press("left")
        self.press("left")
        self.assertEqual(self.app.screen, "settings")
        self.assertEqual(self.app.draft["ina_average_count"], 16)

    def test_cancel_keypad_advanced_and_settings_discards_at_each_level(self):
        self.press("left")
        self.touch_action(("edit", "gate_stop_uv", "VGS stop (V)"))
        self.touch_action(("key", "2"))
        self.press("left")
        self.assertEqual(self.app.draft["gate_stop_uv"], 1000000)
        self.app.draft["drain_stop_uv"] = 2000000
        self.touch_action("advanced")
        self.app.draft["driver_supply_uv"] = 6000000
        self.press("left")
        self.assertEqual(self.app.draft["driver_supply_uv"], 5000000)
        self.assertEqual(self.app.draft["drain_stop_uv"], 2000000)
        self.press("left")
        self.assertEqual(self.app.settings["drain_stop_uv"], 1000000)
        self.assertEqual(self.store.writes, 0)

    def test_settings_save_failure_preserves_committed_values_and_draft(self):
        self.store.settings_error = OSError("disk full")
        self.press("left")
        self.app.draft["drain_stop_uv"] = 2000000
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("disk full", self.app.notice)
        self.assertEqual(self.app.settings["drain_stop_uv"], 1000000)
        self.press("right")
        self.assertEqual(self.app.screen, "settings")
        self.assertEqual(self.app.draft["drain_stop_uv"], 2000000)
        self.store.settings_error = None
        self.press("right")
        self.assertEqual(self.app.settings["drain_stop_uv"], 2000000)

    def test_invalid_settings_do_not_overwrite_or_drive(self):
        self.press("left")
        self.app.draft["gate_step_uv"] = 0
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.app.settings["gate_step_uv"], 1000000)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])

    def test_missing_calibration_blocks_scan_without_driving(self):
        self.app.calibration = None
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("calibration", self.app.notice)
        self.assertEqual(self.instrument.commands, [])
        self.assertEqual(self.engine.state, "idle")

    def test_legacy_map_does_not_claim_a_voltage_limit_beyond_coverage(self):
        self.app.settings.update(drain_stop_uv=5000000)
        self.assertIsNotNone(self.app.calibration_problem())
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.instrument.commands, [])

    def test_measured_capability_allows_limited_scan_and_retains_actual_point(self):
        class ClippedInstrument(HostInstrument):
            def read_pair(self):
                result = super().read_pair()
                for role, coefficient in (("drain", 0.00001), ("gate", 0.0003)):
                    terminal = min(result[role]["terminal_v"], 3.0)
                    shunt = terminal * coefficient
                    result[role].update(terminal_v=terminal, shunt_v=shunt,
                                        bus_v=terminal + shunt)
                return result

        self.instrument = ClippedInstrument()
        self.engine = Engine(self.instrument)
        self.app.engine = self.engine
        self.app.settings.update(drain_start_uv=5000000, drain_stop_uv=5000000)
        calibration = fixture_calibration()
        calibration["version"] = 2
        for channel in calibration["channels"].values():
            channel["capability"] = {"complete": True, "max_duty": 0.15,
                                     "limit_reason": "plateau"}
        self.app.calibration = calibration
        self.assertIsNone(self.app.calibration_problem())
        self.press("right")
        self.finish_operation()
        self.assertEqual(self.app.screen, "complete")
        self.assertIn("VOLTAGE LIMITED", self.display.labels)
        self.assertIn("COMPLETE", self.display.labels)
        self.assertEqual(len(self.app.curves), 1)
        self.assertAlmostEqual(self.app.rows[0], 3.0, places=5)
        self.assertAlmostEqual(self.app.latest["requested_vds"], 5.0)
        self.assertEqual(self.app.limit_flags[0], 1)
        self.assertEqual(self.app.limited_points, 1)

    def test_capability_does_not_relax_source_parking_readiness(self):
        calibration = fixture_calibration()
        calibration["version"] = 2
        for channel in calibration["channels"].values():
            channel["capability"] = {"complete": True, "max_duty": 0.15,
                                     "limit_reason": "plateau"}
        self.app.calibration = calibration
        self.app.settings.update(source_voltage_uv=4000000, drain_start_uv=0,
                                 drain_stop_uv=0, gate_start_uv=-1000000,
                                 gate_stop_uv=0)
        self.assertIsNotNone(self.app.calibration_problem())
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.instrument.commands, [])

    def test_calibration_requires_confirmation_and_cancel_preserves_previous(self):
        old = self.app.calibration
        self.open_calibration()
        self.assertEqual(self.app.screen, "confirm_calibration")
        self.assertEqual(self.instrument.commands, [])
        self.press("left")
        self.assertEqual(self.app.screen, "advanced")
        self.assertIs(self.app.calibration, old)
        self.touch_action("calibrate")
        self.press("right")
        self.assertEqual(self.app.screen, "calibrating")
        self.assertTrue(self.instrument.commands)
        self.press("left")
        self.assertEqual(self.app.screen, "advanced")
        self.assertIs(self.app.calibration, old)
        self.assertIs(self.engine.calibration, old)
        self.assertNotIn("calibration.bin", self.store.files)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})

    def test_settings_save_failure_prevents_calibration_drive(self):
        self.open_calibration()
        self.store.settings_error = OSError("write blocked")
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.engine.state, "idle")
        self.assertEqual(self.instrument.commands, [])
        self.press("left")
        self.assertEqual(self.app.screen, "confirm_calibration")

    def test_completed_calibration_is_saved_before_replacing_previous(self):
        old = self.app.calibration
        self.open_calibration()
        self.press("right")
        self.finish_operation()
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("Calibration saved", self.app.notice)
        self.assertIsNot(self.app.calibration, old)
        self.assertEqual(calibration_snapshot(self.app.calibration),
                         calibration_snapshot(self.store.files["calibration.bin"]))
        model.validate_calibration(self.app.calibration, self.app.settings)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
        self.press("right")
        self.assertEqual(self.app.screen, "advanced")

    def test_calibration_save_failure_preserves_previous_and_zeroes_outputs(self):
        old = self.app.calibration
        self.store.calibration_error = OSError("disk full")
        self.open_calibration()
        self.press("right")
        self.finish_operation()
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("could not be saved", self.app.notice)
        self.assertIs(self.app.calibration, old)
        self.assertIs(self.engine.calibration, old)
        self.assertNotIn("calibration.bin", self.store.files)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})

    def test_calibration_setup_memory_error_is_recoverable_before_or_after_arm(self):
        for armed in (False, True):
            with self.subTest(armed=armed):
                self.setUp()
                old = self.app.calibration
                logs = []
                self.app.error_logger = lambda context, error: logs.append((context, str(error)))
                self.open_calibration()
                start = self.engine.start_calibration

                def fail(settings):
                    if armed:
                        start(settings)
                    else:
                        self.engine.calibration = {"unfinished": True}
                    raise MemoryError("memory allocation failed, allocating 512 bytes")

                self.engine.start_calibration = fail
                self.press("right")
                self.assertEqual(self.app.screen, "notice")
                self.assertIn("Not enough memory", self.app.notice)
                self.assertEqual(self.app.notice_return, "confirm_calibration")
                self.assertNotIn(self.engine.state, ("scanning", "calibrating"))
                self.assertIs(self.app.calibration, old)
                self.assertIs(self.engine.calibration, old)
                self.assertEqual(self.instrument.commands, [])
                self.assertTrue(self.app._outputs_safe)
                self.assertEqual(logs[0][0], "ui action: confirm_calibration")
                self.assertIn("512 bytes", logs[0][1])
                self.engine.start_calibration = start
                self.press("right")
                self.press("right")
                self.assertEqual(self.app.screen, "calibrating")
                self.press("left")

    def test_calibration_settings_memory_error_does_not_arm(self):
        self.open_calibration()
        self.store.settings_error = MemoryError("512 bytes")
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("Not enough memory", self.app.notice)
        self.assertEqual(self.engine.state, "idle")
        self.assertEqual(self.instrument.commands, [])

    def test_calibration_setup_failure_before_state_change_releases_published_workspace(self):
        class WeakMap(dict):
            pass
        old = self.app.calibration
        references = []
        def fail_start(settings):
            workspace = WeakMap(summary={"batchmeans": []})
            self.engine._cal_workspace = workspace
            self.engine._batchmeans = workspace["summary"]["batchmeans"]
            self.engine.calibration = {"unfinished": True}
            references.append(weakref.ref(workspace))
            raise MemoryError("final startup setup")
        self.engine.start_calibration = fail_start
        self.open_calibration()
        self.press("right")
        self.assertEqual(self.engine.state, "idle")
        self.assertIsNone(self.engine._cal_workspace)
        self.assertIsNone(self.engine._batchmeans)
        self.assertIsNone(references[0]())
        self.assertIs(self.engine.calibration, old)
        self.assertEqual(self.instrument.commands, [])

    def test_calibration_setup_failure_does_not_resume_radio_after_failed_shutdown(self):
        old = self.app.calibration
        start = self.engine.start_calibration
        resumed = []
        self.app._pause_wifi = lambda: setattr(self.app, "_wifi_paused", True)
        self.app._resume_wifi = lambda: resumed.append(True)

        def fail_start(settings):
            start(settings)
            raise MemoryError("512 bytes")

        def fail_zero():
            raise OSError("output bus offline")

        self.engine.start_calibration = fail_start
        self.instrument.zero = fail_zero
        with self.assertRaises(MemoryError):
            self.app._start_calibration()
        self.assertEqual(self.engine.state, "error")
        self.assertFalse(self.app._outputs_safe)
        self.assertTrue(self.app._wifi_paused)
        self.assertEqual(resumed, [])
        self.assertIs(self.engine.calibration, old)

    def test_calibration_save_memory_error_retains_saved_map_and_allows_retry(self):
        old = self.app.calibration
        logs = []
        self.app.error_logger = lambda context, error: logs.append((context, str(error)))
        self.store.calibration_error = MemoryError("512 bytes")
        self.open_calibration()
        self.press("right")
        self.finish_operation()
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("could not be saved: not enough memory", self.app.notice)
        self.assertIs(self.app.calibration, old)
        self.assertIs(self.engine.calibration, old)
        self.assertEqual(logs[-1], ("calibration save", "512 bytes"))
        self.assertNotIn("calibration.bin", self.store.files)
        self.assertTrue(self.app._outputs_safe)
        self.store.calibration_error = None
        self.press("right")
        self.touch_action("calibrate")
        self.press("right")
        self.finish_operation()
        self.assertIn("Calibration saved", self.app.notice)
        self.assertIs(self.engine.calibration, self.app.calibration)
        self.assertIsNot(self.app.calibration, old)

    def test_calibration_runtime_error_releases_partial_candidate(self):
        old = self.app.calibration
        self.open_calibration()
        self.press("right")
        self.assertIsNot(self.engine.calibration, old)
        self.instrument.error = MemoryError("512 bytes")
        self.finish_operation()
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.engine.state, "error")
        self.assertIs(self.app.calibration, old)
        self.assertIs(self.engine.calibration, old)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})

    def test_calibration_collects_after_pause_and_saves_before_radio_resume(self):
        for failed_save in (False, True):
            with self.subTest(failed_save=failed_save):
                self.setUp()
                self.open_calibration()
                order = []
                pause_cloud, pause_wifi = self.app._pause_cloud, self.app._pause_wifi
                start, save = self.engine.start_calibration, self.store.save_json_atomic
                self.app._pause_cloud = lambda: order.append("cloud") or pause_cloud()
                self.app._pause_wifi = lambda: order.append("wifi") or pause_wifi()
                self.engine.start_calibration = lambda settings: order.append("start") or start(settings)

                def checked_save(path, value):
                    self.assertEqual(order[-1], "collect")
                    self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
                    order.append("save")
                    if failed_save:
                        raise MemoryError("512 bytes")
                    return save(path, value)

                def checked_resume():
                    self.assertIn("save", order)
                    self.assertIs(self.engine.calibration, self.app.calibration)
                    order.append("resume")

                self.store.save_json_atomic = checked_save
                self.app._resume_wifi = checked_resume
                with patch("firmware.ui.gc.collect", side_effect=lambda: order.append("collect")):
                    self.press("right")
                    self.assertEqual(order[:4], ["cloud", "wifi", "collect", "start"])
                    self.finish_operation()
                self.assertEqual(order[-3:], ["collect", "save", "resume"])

    def test_disk_calibration_drops_all_old_map_references_before_setup(self):
        from test_cloud_ui import FakeCloud

        class WeakMap(dict):
            pass

        self.app.calibration = WeakMap(self.app.calibration)
        self.app._ensure_calibration_validated()
        self.store.files["calibration.bin"] = deepcopy(self.app.calibration)
        self.app._calibration_on_disk = True
        self.engine.calibration = self.app.calibration
        old = weakref.ref(self.app.calibration)
        cloud = FakeCloud()
        cloud.cached = self.app.calibration
        invalidate = cloud.invalidate_calibration

        def clear_cache():
            cloud.cached = None
            invalidate()

        cloud.invalidate_calibration = clear_cache
        self.app.cloud = cloud
        start = self.engine.start_calibration

        def checked_start(settings):
            self.assertIsNone(old())
            self.assertIsNone(self.app._validated_calibration)
            self.assertIsNone(self.app.calibration)
            self.assertIsNone(self.engine.calibration)
            self.assertIsNone(cloud.cached)
            self.assertIsNone(self.app._calibration_writer)
            return start(settings)

        self.engine.start_calibration = checked_start
        self.open_calibration()
        self.press("right")
        self.assertEqual(self.app.screen, "calibrating")
        self.assertIn("invalidate_calibration", cloud.calls)
        self.press("left")
        self.assertIsNone(self.app.calibration)
        self.tick()
        self.assertEqual(calibration_snapshot(self.app.calibration),
                         calibration_snapshot(self.store.files["calibration.bin"]))

    def test_recalibration_preserves_shunt_provenance_without_old_voltage_tables(self):
        class WeakMap(dict):
            pass

        class WeakList(list):
            pass

        record = {"effective_shunt_ohm": model.DRAIN_SHUNT_OHM,
                  "reference_load_ohm": 9980, "date": "2026-11-02"}
        self.app.calibration = WeakMap(self.app.calibration)
        self.app.calibration["metadata"]["drain_shunt_calibration"] = record
        self.app.calibration["metadata"]["drive_baseline_calibration"] = {"old": True}
        self.app.calibration["channels"]["drain"]["points"] = WeakList(
            self.app.calibration["channels"]["drain"]["points"])
        old_map = weakref.ref(self.app.calibration)
        old_points = weakref.ref(self.app.calibration["channels"]["drain"]["points"])
        self.engine.calibration = self.app.calibration
        self.store.files["calibration.bin"] = deepcopy(self.app.calibration)
        self.app._calibration_on_disk = True
        start = self.engine.start_calibration

        def checked_start(settings):
            self.assertIsNone(old_map())
            self.assertIsNone(old_points())
            return start(settings)

        self.engine.start_calibration = checked_start
        self.open_calibration()
        self.press("right")
        self.assertIs(self.engine.calibration["metadata"]["drain_shunt_calibration"], record)
        self.assertNotIn("drive_baseline_calibration", self.engine.calibration["metadata"])
        self.finish_operation()
        self.assertEqual(self.store.files["calibration.bin"]["metadata"]["drain_shunt_calibration"], record)
        self.assertEqual(self.app.calibration["channels"]["drain"]["shunt_ohm"], model.DRAIN_SHUNT_OHM)

    def test_recalibration_does_not_carry_mismatched_shunt_provenance(self):
        for mismatch in ("channel", "record", "missing_scale", "not_a_record"):
            with self.subTest(mismatch=mismatch):
                self.setUp()
                record = {"effective_shunt_ohm": model.DRAIN_SHUNT_OHM}
                if mismatch == "channel":
                    self.app.calibration["channels"]["drain"]["shunt_ohm"] = 7.3
                elif mismatch == "record":
                    record["effective_shunt_ohm"] = 7.3
                elif mismatch == "missing_scale":
                    record = {}
                else:
                    record = "old calibration"
                self.app.calibration["metadata"]["drain_shunt_calibration"] = record
                self.open_calibration()
                self.press("right")
                self.assertEqual(self.app.screen, "calibrating")
                self.assertNotIn("drain_shunt_calibration", self.engine.calibration["metadata"])
                self.press("left")

    def test_disk_calibration_restore_waits_for_candidate_frames_and_precedes_resume(self):
        class WeakMap(dict):
            pass

        for mode in ("cancel", "error", "save"):
            with self.subTest(mode=mode):
                self.setUp()
                saved = deepcopy(self.app.calibration)
                self.store.files["calibration.bin"] = deepcopy(saved)
                self.app = App(self.display, self.inputs, self.engine, self.settings,
                               self.app.calibration, self.store, calibration_on_disk=True)
                self.tick()
                self.open_calibration()
                self.press("right")
                self.engine.calibration = WeakMap(self.engine.calibration)
                candidate = weakref.ref(self.engine.calibration)
                order = []
                load = self.store.load_json

                def checked_load(path):
                    self.assertIsNone(candidate())
                    self.assertIsNone(self.engine.calibration)
                    order.append("load")
                    return load(path)

                self.store.load_json = checked_load
                self.app._resume_wifi = lambda: order.append("resume")
                if mode == "cancel":
                    # Also cover cancellation queued outside tick: the next
                    # tick must consume its event before a later tick reloads.
                    self.app.action("left")
                    self.tick()
                else:
                    if mode == "error":
                        self.instrument.error = MemoryError("512 bytes")
                    else:
                        def fail_save(path, value):
                            # Do not retain the exception in the fixture: its
                            # CPython traceback would hold the value argument.
                            raise MemoryError("1148 bytes")
                        self.store.save_json_atomic = fail_save
                    self.finish_operation()
                self.assertEqual(order, [])
                self.assertIsNone(self.app.calibration)
                self.assertIsNone(self.engine.calibration)
                self.assertIsNone(self.app._calibration_writer)
                self.assertTrue(self.app._calibration_restore_ready)
                self.tick()
                self.assertEqual(order, ["load", "resume"])
                self.assertEqual(self.app.calibration, saved)
                self.assertIs(self.app._validated_calibration, self.app.calibration)
                self.assertIs(self.engine.calibration, self.app.calibration)
                self.assertEqual(self.store.files["calibration.bin"], saved)
                self.assertFalse(self.app._calibration_restore_pending)

    def test_disk_calibration_reload_failure_is_recoverable_and_keeps_disk_map(self):
        saved = deepcopy(self.app.calibration)
        self.store.files["calibration.bin"] = deepcopy(saved)
        self.app._calibration_on_disk = True
        logs = []
        self.app.error_logger = lambda context, error: logs.append((context, str(error)))
        self.open_calibration()
        self.press("right")
        self.press("left")

        def fail_load(path):
            raise MemoryError("reload memory")

        self.store.load_json = fail_load
        self.tick()
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("could not be loaded", self.app.notice)
        self.assertIsNone(self.app.calibration)
        self.assertIsNone(self.app._validated_calibration)
        self.assertIsNone(self.engine.calibration)
        self.assertEqual(self.store.files["calibration.bin"], saved)
        self.assertEqual(logs[-1], ("calibration restore", "reload memory"))
        self.assertFalse(self.app._calibration_restore_pending)
        self.assertTrue(self.app._outputs_safe)

    def test_successful_calibration_fallback_marks_disk_for_next_run(self):
        save = self.store.save_json_atomic

        def checked_save(path, value):
            self.assertIsNone(self.app._calibration_writer)
            return save(path, value)

        self.store.save_json_atomic = checked_save
        self.open_calibration()
        self.press("right")
        self.finish_operation()
        self.assertTrue(self.app._calibration_on_disk)
        self.assertFalse(self.app._calibration_restore_pending)
        self.assertIs(self.app.calibration, self.engine.calibration)
        self.press("right")
        self.touch_action("calibrate")
        self.press("right")
        self.assertIsNone(self.app.calibration)
        self.assertTrue(self.app._calibration_restore_pending)
        self.assertIsNone(self.app._calibration_writer)
        self.press("left")
        self.tick()
        self.assertEqual(calibration_snapshot(self.app.calibration),
                         calibration_snapshot(self.store.files["calibration.bin"]))

    def test_calibration_transaction_opens_before_drive_and_commits_successive_maps(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CalibrationTransactionStore(folder)
            self.app.calibration = packed_fixture(self.app.calibration)
            storage.save_calibration_atomic(self.app.calibration, path=store.path)
            self.app.store = store
            self.app._calibration_on_disk = True
            start = self.engine.start_calibration

            def checked_start(settings):
                self.assertEqual(store.operations[-1], "begin")
                self.assertIsNotNone(self.app._calibration_writer)
                self.assertTrue(Path(store.path + ".tmp").exists())
                return start(settings)

            self.engine.start_calibration = checked_start
            store.commit_check = lambda: self.assertEqual(
                self.instrument.duties, {"drain": 0.0, "gate": 0.0})
            self.open_calibration()
            for attempt in range(2):
                self.press("right")
                self.assertEqual(self.app.screen, "calibrating")
                self.assertIsNone(self.app.calibration)
                self.finish_operation()
                self.assertIn("Calibration saved", self.app.notice)
                self.assertEqual(store.operations, ["begin", "commit"] * (attempt + 1))
                self.assertIsNone(self.app._calibration_writer)
                self.assertIsNone(store.writer_reference())
                self.assertFalse(Path(store.path + ".tmp").exists())
                self.assertEqual(calibration_snapshot(storage.load_calibration(path=store.path)),
                                 calibration_snapshot(self.app.calibration))
                self.assertIs(self.app.calibration, self.engine.calibration)
                if attempt == 0:
                    self.press("right")
                    self.touch_action("calibrate")

    def test_calibration_transaction_aborts_before_restore_on_each_failure_path(self):
        class WeakMap(dict):
            pass

        for mode in ("setup", "cancel", "error", "save"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                self.setUp()
                store = CalibrationTransactionStore(folder)
                self.app.calibration = packed_fixture(self.app.calibration)
                storage.save_calibration_atomic(self.app.calibration, path=store.path)
                before = Path(store.path).read_bytes()
                self.app.store = store
                self.app._calibration_on_disk = True
                self.open_calibration()
                if mode == "setup":
                    def fail_start(settings):
                        raise MemoryError("setup allocation")
                    self.engine.start_calibration = fail_start
                self.press("right")
                candidate = None
                if mode != "setup":
                    self.engine.calibration = WeakMap(self.engine.calibration)
                    candidate = weakref.ref(self.engine.calibration)
                    if mode == "cancel":
                        self.press("left")
                    else:
                        if mode == "error":
                            self.instrument.error = OSError("sensor disconnected")
                        else:
                            store.fail_commit = True
                        self.finish_operation()
                expected = ["begin", "commit", "abort"] if mode == "save" else ["begin", "abort"]
                self.assertEqual(store.operations, expected)
                self.assertIsNone(self.app._calibration_writer)
                self.assertFalse(Path(store.path + ".tmp").exists())
                self.assertEqual(Path(store.path).read_bytes(), before)

                def checked_load():
                    self.assertIsNone(store.writer_reference())
                    if candidate is not None:
                        self.assertIsNone(candidate())

                store.load_check = checked_load
                self.tick()
                self.assertEqual(store.operations, expected + ["load"])
                self.assertEqual(Path(store.path).read_bytes(), before)
                self.assertEqual(calibration_snapshot(self.app.calibration),
                                 calibration_snapshot(storage.load_calibration(path=store.path)))

    def test_calibration_transaction_open_failure_never_drives_or_changes_saved_file(self):
        with tempfile.TemporaryDirectory() as folder:
            store = CalibrationTransactionStore(folder)
            self.app.calibration = packed_fixture(self.app.calibration)
            storage.save_calibration_atomic(self.app.calibration, path=store.path)
            before = Path(store.path).read_bytes()
            store.fail_begin = True
            self.app.store = store
            self.app._calibration_on_disk = True
            self.open_calibration()
            self.press("right")
            self.assertEqual(self.app.screen, "notice")
            self.assertIn("Not enough memory", self.app.notice)
            self.assertEqual(self.instrument.commands, [])
            self.assertEqual(store.operations, ["begin"])
            self.assertEqual(Path(store.path).read_bytes(), before)
            self.tick()
            self.assertEqual(store.operations, ["begin", "load"])
            self.assertIsNotNone(self.app.calibration)

    def test_live_scan_points_completion_and_either_button_returns_home(self):
        for button in ("left", "right"):
            with self.subTest(button=button):
                self.press("right")
                self.assertEqual(self.app.screen, "scan")
                self.press("right")  # Busy OK does not advance/finish scan.
                self.assertEqual(self.app.screen, "scan")
                self.finish_operation()
                self.assertEqual(self.app.screen, "complete")
                self.assertEqual(len(self.app.curves), 2)
                self.assertEqual(len(self.app.rows), 4)
                self.assertEqual(self.engine.progress, 1.0)
                self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
                self.assertIn("SCAN COMPLETE", self.display.labels)
                self.press(button)
                self.assertEqual(self.app.screen, "home")

    def test_limited_point_keeps_actual_values_and_warning_survives_completion(self):
        limited = {"vds": 2.97, "vgs": 2.81, "id_a": 0.0001, "ig_a": 0.000002,
                   "id_std_a": 0.000001, "ig_std_a": 0.00000001, "curve": 0,
                   "batches_completed": 2, "requested_vds": 5.0, "requested_vgs": 4.0,
                   "drain_voltage_limited": True, "gate_voltage_limited": True}
        self.app.latest = limited
        self.app.goto("scan")
        self.app.render()
        self.app._add_point(limited)
        self.app._render_live()
        self.app._draw_points()
        self.assertIn("VOLTAGE LIMITED", self.display.labels)
        self.assertIn("2.970", self.display.labels)
        self.assertIn("2.810", self.display.labels)
        self.assertIn("SET 5 V", self.display.labels)
        self.assertIn("SET 4 V", self.display.labels)
        self.assertAlmostEqual(self.app.rows[0], 2.97, places=5)
        self.assertAlmostEqual(self.app.rows[1], 0.0001, places=8)
        self.assertEqual(self.app.limit_flags[0], 7)
        self.assertEqual(self.app.limited_points, 1)

        # A later reachable point must not erase the completed-scan warning.
        reachable = dict(limited, vds=1.0, vgs=1.0, requested_vds=1.0,
                         requested_vgs=1.0, drain_voltage_limited=False,
                         gate_voltage_limited=False)
        self.app.latest = reachable
        self.app._add_point(reachable)
        self.app.goto("complete")
        self.app.render()
        self.assertIn("VOLTAGE LIMITED", self.display.labels)
        self.assertIn("COMPLETE", self.display.labels)
        self.assertEqual(list(self.app.limit_flags), [7, 0])
        self.assertTrue(any("1 limited" in text for text in self.display.labels))

        buffers = (self.app.rows._data, self.app.curves._data, self.app.limit_flags._data)
        self.press("right")
        self.press("right")
        self.assertEqual(self.app.screen, "scan")
        self.assertEqual(len(self.app.limit_flags), 0)
        self.assertEqual(self.app.limited_points, 0)
        self.assertIs(buffers[0], self.app.rows._data)
        self.assertIs(buffers[1], self.app.curves._data)
        self.assertIs(buffers[2], self.app.limit_flags._data)

    def test_live_limit_status_changes_with_latest_batch(self):
        row = {"vds": 2.9, "vgs": 1.0, "id_a": 0.0, "ig_a": 0.0,
               "batches_completed": 1, "requested_vds": 5.0, "requested_vgs": 1.0,
               "drain_voltage_limited": True, "gate_voltage_limited": False}
        self.app.latest = row
        self.app.goto("scan")
        self.app.render()
        self.assertEqual(self.app._shown_scan_header, ("scan", True))
        self.assertIn("VOLTAGE LIMITED", self.display.labels)
        self.app.latest = dict(row, requested_vds=2.9, drain_voltage_limited=False)
        self.app._render_live()
        self.assertEqual(self.app._shown_scan_header, ("scan", False))
        self.assertTrue(any(label.startswith("0 points | ") for label in self.display.labels))

    def test_plot_expands_below_requested_start_for_limited_measurement(self):
        self.app.settings.update(drain_start_uv=4000000, drain_stop_uv=5000000)
        self.app.clear_results()
        row = {"vds": 3.5, "vgs": 1.0, "id_a": 0.0, "ig_a": 0.0,
               "id_std_a": 0.0, "ig_std_a": 0.0, "curve": 0,
               "batches_completed": 2, "requested_vds": 4.0, "requested_vgs": 1.0,
               "drain_voltage_limited": True, "gate_voltage_limited": False}
        self.app._add_point(row)
        self.app._add_point(dict(row, vds=4.0, drain_voltage_limited=False))
        self.assertLess(self.app._plot_xmin, 3.5)  # Headroom absorbs endpoint scatter.
        self.assertEqual(self.app._plot_xmax, 5.0)
        self.assertLess(self.app._xy(0)[0], self.app._xy(1)[0])
        self.assertTrue(self.app._plot_axes_dirty)
        self.app._draw_points()
        self.assertIn("%.3g" % self.app._plot_xmin, self.display.labels)

    def test_off_target_gate_points_break_both_segments_across_draw_chunks_and_redraw(self):
        lines, marks = [], []
        self.display.line = lambda *args: lines.append(args)
        self.display.fill_rect = lambda *args: marks.append(args)
        for index, difference in enumerate((0, 0, 0, -0.100001, 0, 0.100001, 0, 0)):
            self.app._add_point({"vds": (index + 1) / 10, "vgs": 1 + difference,
                                 "id_a": index * 0.1e-6, "curve": 0,
                                 "requested_vds": (index + 1) / 10, "requested_vgs": 1.0})
        self.assertEqual([0, 0, 0, 4, 0, 4, 0, 0], list(self.app.limit_flags))
        self.assertEqual(0, self.app.limited_points)
        self.app._draw_points()
        self.assertEqual(4, self.app._plot_cursor)
        self.app._draw_points()
        expected = [(*self.app._xy(left), *self.app._xy(right), CURVES[0])
                    for left, right in ((0, 1), (1, 2), (6, 7))]
        self.assertEqual(expected, lines)
        self.assertEqual(8, len(marks))  # Every point remains visible.
        self.assertEqual(2, sum(mark[-1] == AMBER for mark in marks))
        self.assertAlmostEqual(0.4, self.app.rows[6])
        self.assertAlmostEqual(0.3e-6, self.app.rows[7])

        # A real rescale clears the plot and resets its incremental cursor.
        # Redrawing must preserve both gaps and cannot bridge missing segments.
        self.app._add_point({"vds": 1.5, "vgs": 1.0, "id_a": 0.8e-6, "curve": 0,
                             "requested_vds": 1.5, "requested_vgs": 1.0})
        self.assertEqual(0, self.app._plot_cursor)
        lines.clear()
        marks.clear()
        while self.app._plot_cursor < len(self.app.curves):
            self.app._draw_points()
        expected = [(*self.app._xy(left), *self.app._xy(right), CURVES[0])
                    for left, right in ((0, 1), (1, 2), (6, 7), (7, 8))]
        self.assertEqual(expected, [line for line in lines if line[-1] == CURVES[0]])
        self.assertEqual(9, sum(mark[2:4] in ((3, 3), (1, 1)) for mark in marks))
        self.assertEqual(2, sum(mark[-1] == AMBER for mark in marks))

    def test_gate_plot_tolerance_includes_boundary_and_is_independent_of_limit_flag(self):
        for requested in (0.0, 1.0, 2.5):
            for difference in (-0.05, 0.05):
                self.assertFalse(self.app._gate_off_target({
                    "vgs": requested + difference, "requested_vgs": requested}))
            for difference in (-model.GATE_PLOT_TOLERANCE_V, model.GATE_PLOT_TOLERANCE_V):
                row = {"vgs": requested + difference, "requested_vgs": requested,
                       "gate_voltage_limited": True}
                self.assertFalse(self.app._gate_off_target(row))
                row["vgs"] += -1e-6 if difference < 0 else 1e-6
                row["gate_voltage_limited"] = False
                self.assertTrue(self.app._gate_off_target(row))
        self.assertFalse(self.app._gate_off_target({"vgs": 2.0}))

    def test_drain_mismatch_alone_keeps_line_and_gate_miss_does_not_claim_voltage_limit(self):
        lines = []
        self.display.line = lambda *args: lines.append(args)
        row = {"vds": 0.2, "vgs": 1.0, "id_a": 0.0, "ig_a": 0.0,
               "requested_vds": 0.8, "requested_vgs": 1.0, "curve": 0,
               "batches_completed": 2, "drain_voltage_limited": True}
        self.app._add_point(row)
        self.app._add_point(dict(row, vds=0.3))
        self.app._draw_points()
        self.assertEqual([(*self.app._xy(0), *self.app._xy(1), CURVES[0])], lines)
        self.assertEqual([1, 1], list(self.app.limit_flags))
        self.assertEqual(2, self.app.limited_points)

        buffers = (self.app.rows._data, self.app.curves._data, self.app.limit_flags._data)
        self.app.clear_results()
        row.update(vgs=1.100001, drain_voltage_limited=False)
        self.app.latest = row
        self.app._add_point(row)
        self.app.goto("complete")
        self.app.render()
        self.assertEqual([4], list(self.app.limit_flags))
        self.assertEqual(0, self.app.limited_points)
        self.assertIn("SCAN COMPLETE", self.display.labels)
        self.assertNotIn("VOLTAGE LIMITED", self.display.labels)
        self.assertFalse(any("limited" in text for text in self.display.labels))
        self.app.clear_results()
        self.assertEqual([], list(self.app.limit_flags))
        self.assertEqual(0, self.app._plot_cursor)
        self.assertIs(buffers[0], self.app.rows._data)
        self.assertIs(buffers[1], self.app.curves._data)
        self.assertIs(buffers[2], self.app.limit_flags._data)

    def test_single_voltage_scan_starts_with_nonzero_plot_span(self):
        self.app.settings.update(drain_start_uv=0, drain_stop_uv=0)
        self.app.clear_results()
        self.assertLess(self.app._plot_xmin, 0.0)
        self.assertGreater(self.app._plot_xmax, 0.0)

    def test_cancel_during_scan_returns_home_and_stops_sampling(self):
        self.press("right")
        self.press("left")
        self.assertEqual(self.app.screen, "home")
        self.assertEqual(self.engine.state, "cancelled")
        reads = self.instrument.reads
        self.tick()
        self.assertEqual(self.instrument.reads, reads)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})

    def test_sensor_failure_reaches_notice_with_outputs_zero(self):
        logged = []
        self.app.error_logger = lambda context, error: logged.append((context, str(error), dict(self.instrument.duties)))
        self.instrument.error = OSError("sensor disconnected")
        self.press("right")
        self.finish_operation()
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("sensor disconnected", self.app.notice)
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
        self.assertEqual(logged[0][0], "acquisition")
        self.assertIn("sensor disconnected", logged[0][1])
        self.assertEqual(logged[0][2], {"drain": 0.0, "gate": 0.0})
        self.press("right")
        self.assertEqual(self.app.screen, "home")

    def test_log_failure_cannot_replace_original_ui_error(self):
        def broken_logger(context, error):
            raise OSError("log unavailable")
        self.app.error_logger = broken_logger
        self.store.settings_error = OSError("settings write failed")
        self.press("left")
        self.press("right")
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("settings write failed", self.app.notice)
        self.assertNotIn("log unavailable", self.app.notice)

    def test_simultaneous_buttons_do_not_trigger_new_screen_action(self):
        self.tick((("left",), ("right",)))
        self.assertEqual(self.app.screen, "settings")
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.instrument.commands, [])

    def test_footer_labels_and_touch_targets_follow_screen_state(self):
        self.assertIn("SETTINGS", self.display.labels)
        self.assertIn("SCAN", self.display.labels)
        self.touch_action("left")
        self.assertIn("CANCEL", self.display.labels)
        self.assertIn("OK", self.display.labels)
        self.touch_action("left")
        self.touch_action("right")
        self.assertEqual(self.app.screen, "scan")
        self.assertNotIn("right", [hit[4] for hit in self.app.hits])
        self.touch_action("left")
        self.assertEqual(self.app.screen, "home")


if __name__ == "__main__":
    unittest.main()
