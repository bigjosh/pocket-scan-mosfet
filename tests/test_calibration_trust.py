"""Calibration validation is owned by lifecycle, never by a stored flag."""
from copy import deepcopy
import gc
import unittest
import weakref
from unittest.mock import patch

from firmware import model
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore, fixture_calibration
from test_ram_ui import RamCloud, RamWifi
from test_offset_calibration import OffsetInstrument, calibrate, offset_settings
from test_model_engine import calibration, settings


class CalibrationTrustTests(unittest.TestCase):
    def app(self, value=None, trusted=False):
        value = fixture_calibration() if value is None else value
        buffer = PackedScanBuffer(capacity=32)
        cloud = RamCloud(buffer)
        app = App(FakeDisplay(), FakeInputs(), Engine(HostInstrument()), settings(), value,
                  store=MemoryStore(), wifi=RamWifi(), cloud=cloud, scan_buffer=buffer,
                  restart=lambda: None, validated_calibration=value if trusted else None)
        return app

    def test_boot_trust_avoids_table_walk_on_home_preparation_and_engine_arm(self):
        value = fixture_calibration()
        model.validate_calibration(value)  # The main.run boundary.
        with patch.object(model, "validate_calibration", side_effect=AssertionError("duplicate table walk")), \
                patch.object(model, "interpolate", side_effect=AssertionError("preflight interpolation")):
            app = self.app(value, trusted=True)
            for unused in range(3):
                app.render()
                self.assertIsNone(app.calibration_problem())
            app.action("right")
            for now in range(20, 200, 20):
                app.tick(now)
                if app.engine.state == "scanning":
                    break
            self.assertEqual("scanning", app.engine.state)
            self.assertEqual([], app.engine.instrument.commands)

    def test_unknown_and_replacement_maps_validate_once_and_release_old_trust(self):
        class WeakMap(dict):
            pass
        initial = WeakMap(fixture_calibration())
        app = self.app(initial)
        calls = []
        original = model.validate_calibration
        def validate(value):
            calls.append(id(value))  # Do not retain the map like Mock.call_args.
            return original(value)
        with patch.object(model, "validate_calibration", validate):
            self.assertIsNone(app.calibration_problem())
            self.assertIsNone(app.calibration_problem())
            self.assertEqual(1, len(calls))
            previous = weakref.ref(initial)
            app.cloud.cached = initial
            app.calibration = deepcopy(dict(initial))
            del initial
            self.assertIsNone(app.calibration_problem())
            gc.collect()
            self.assertIsNone(previous())
            self.assertEqual(2, len(calls))
            self.assertIs(app._validated_calibration, app.calibration)

    def test_replacement_and_public_engine_ignore_serialized_trust_claims(self):
        valid = fixture_calibration()
        invalid = deepcopy(valid)
        invalid["validated"] = True
        invalid["channels"]["drain"]["points"][1]["terminal_v"] = -1
        app = self.app(valid, trusted=True)
        app.calibration = invalid
        self.assertIn("calibration", app.calibration_problem())
        self.assertIsNone(app._validated_calibration)
        for trusted_arg in (None, valid):
            engine = Engine(HostInstrument())
            with self.assertRaises(ValueError):
                engine.start_scan(settings(), invalid, validated_calibration=trusted_arg)
            self.assertEqual([], engine.instrument.commands)

    def test_changed_acquisition_supply_pwm_shunt_and_coverage_still_reject(self):
        cases = ("count", "rail", "pwm", "shunt", "coverage")
        for change in cases:
            with self.subTest(change=change):
                app = self.app(trusted=True)
                if change == "count":
                    app.settings["ina_average_count"] = 16
                elif change == "rail":
                    app.settings["driver_supply_uv"] = 6000000
                elif change == "pwm":
                    app.calibration["metadata"]["pwm_hz"] = 1
                elif change == "shunt":
                    app.calibration["channels"]["gate"]["shunt_ohm"] = 1
                else:
                    app.settings["drain_stop_uv"] = 4000000
                with patch.object(model, "validate_calibration", side_effect=AssertionError("table walk")):
                    self.assertIsNotNone(app.calibration_problem())
                    with self.assertRaises(ValueError):
                        app._arm_scan()
                self.assertEqual([], app.engine.instrument.commands)

    def test_positive_floor_source_binding_remains_checked_on_trusted_arm(self):
        hardware = OffsetInstrument()
        hardware.floors["drain"] = 3.58
        scanner, _ = calibrate(hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        app = self.app(scanner.calibration, trusted=True)
        app.settings = offset_settings(source_voltage_uv=3500000)
        with patch.object(model, "validate_calibration", side_effect=AssertionError("table walk")):
            self.assertIn("Source reference changed", app.calibration_problem())
            with self.assertRaisesRegex(ValueError, "Source reference changed"):
                app._arm_scan()

    def test_failed_restore_never_marks_unknown_disk_map_as_trusted(self):
        app = self.app(trusted=True)
        invalid = deepcopy(app.calibration)
        invalid["channels"]["gate"]["points"][1]["duty"] = -1
        app.store.files["calibration.bin"] = invalid
        app._calibration_restore_pending = app._calibration_restore_ready = True
        app._restore_saved_calibration()
        self.assertIsNone(app.calibration)
        self.assertIsNone(app._validated_calibration)
        self.assertEqual("notice", app.screen)

    def test_successful_restore_validates_once_before_reusing_trust(self):
        app = self.app(trusted=True)
        app.store.files["calibration.bin"] = deepcopy(app.calibration)
        app._calibration_restore_pending = app._calibration_restore_ready = True
        with patch.object(model, "validate_calibration", wraps=model.validate_calibration) as validate:
            app._restore_saved_calibration()
            self.assertEqual(1, validate.call_count)
            self.assertIs(app._validated_calibration, app.calibration)
            self.assertIsNone(app.calibration_problem())
            self.assertEqual(1, validate.call_count)

    def test_new_engine_calibration_releases_old_trust_and_transfers_validated_result(self):
        app = self.app(trusted=True)
        app._start_calibration()
        self.assertIsNone(app._validated_calibration)
        app.goto("calibrating")
        with patch.object(model, "validate_calibration", wraps=model.validate_calibration) as validate:
            for now in range(0, 100000, 20):
                app.tick(now)
                if app.engine.state != "calibrating":
                    break
            self.assertEqual("complete", app.engine.state, app.engine.error)
            self.assertEqual(1, validate.call_count)  # Engine verifies before its calibrated event.
            self.assertIs(app._validated_calibration, app.calibration)
            self.assertIsNone(app.calibration_problem())
            self.assertEqual(1, validate.call_count)

    def test_ram_seal_releases_trust_alias_with_live_calibration(self):
        app = self.app(trusted=True)
        app.cloud.begin_scan(app.settings, app.calibration, 0)
        app._outputs_safe = True
        app._ram_seal()
        self.assertIsNone(app.calibration)
        self.assertIsNone(app._validated_calibration)
        self.assertIsNone(app.engine.calibration)

    def test_endpoint_only_coverage_matches_real_inverse_lookup(self):
        maps = [calibration()["channels"]["drain"]]
        shifted = deepcopy(maps[0])
        shifted["points"][0]["terminal_v"] = 0.006
        maps.append(shifted)
        for channel in maps:
            for voltage in (-0.021, -0.020, 0, 0.006, 0.5, 2.98, 3, 3.02, 3.021):
                try:
                    model.duty_for_voltage(channel, voltage)
                    expected = True
                except ValueError:
                    expected = False
                with patch.object(model, "interpolate", side_effect=AssertionError("point traversal")):
                    try:
                        model._check_drive_coverage(channel, voltage)
                        actual = True
                    except ValueError:
                        actual = False
                self.assertEqual(expected, actual, (channel, voltage))


if __name__ == "__main__":
    unittest.main()
