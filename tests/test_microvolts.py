"""Exact settings geometry and safe one-time migration regression tests."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from firmware import model, storage
from firmware.engine import Engine
from test_model_engine import FakeInstrument, calibration, run_engine, settings


def legacy_settings():
    value = dict(model.DEFAULT_SETTINGS)
    value.pop('ina_average_count')
    value.update(model.LEGACY_TIMING)
    value['version'] = 1
    for field in model.VOLTAGE_FIELDS:
        value[field + '_v'] = value.pop(field + '_uv') / 1000000
    return value


class MicrovoltTests(unittest.TestCase):
    def test_decimal_round_trip_without_floats(self):
        for value in (-36000000, -20000001, -1, 0, 1, 2000001, 20000001, 72000000):
            self.assertEqual(model.parse_voltage_uv(model.format_voltage_uv(value)), value)
        self.assertEqual(model.parse_voltage_uv(' +.025000000 '), 25000)
        for text in ('1e-6', 'nan', 'inf', '.', '--1', '1.0000001', '1.2.3', '１２'):
            with self.assertRaises(ValueError, msg=text):
                model.parse_voltage_uv(text)

    def test_endpoint_and_large_rail_precision(self):
        values = model.sweep_uv(0, 1300000, 100000)
        self.assertEqual(len(values), 14)
        self.assertEqual(values[-1], 1300000)
        self.assertEqual(list(model.sweep_uv(20000000, 20000003, 1)),
                         [20000000, 20000001, 20000002, 20000003])
        self.assertEqual(list(model.sweep_uv(-2, 2, 3)), [-2, 1])

    def test_grid_at_and_above_limit(self):
        value = settings(drain_start_uv=0, drain_stop_uv=630000, drain_step_uv=10000,
                         gate_start_uv=0, gate_stop_uv=630000, gate_step_uv=10000)
        self.assertEqual(model.scan_point_count(model.validate_settings(value)), 4096)
        value.update(drain_stop_uv=1550000, drain_step_uv=25000,
                     gate_stop_uv=1625000, gate_step_uv=25000)
        with self.assertRaisesRegex(ValueError, '4096'):
            model.validate_settings(value)

    def test_integer_fields_and_envelope_are_strict(self):
        for invalid in (True, 1.0, '1', None):
            with self.assertRaises(ValueError):
                model.validate_settings(settings(drain_start_uv=invalid))
        with self.assertRaisesRegex(ValueError, 'mixed'):
            model.validate_settings(dict(model.DEFAULT_SETTINGS, drain_start_v=0))
        value = settings(driver_supply_uv=20000001, source_voltage_uv=20000000,
                         drain_start_uv=-20000000, drain_stop_uv=1, drain_step_uv=1000000,
                         gate_start_uv=0, gate_stop_uv=1, gate_step_uv=1)
        model.validate_settings(value)
        value['gate_stop_uv'] = 2
        with self.assertRaises(ValueError):
            model.validate_settings(value)

    def test_legacy_migration_does_not_modify_input(self):
        legacy = legacy_settings()
        legacy['drain_step_v'] = 0.025
        before = dict(legacy)
        value = model.validate_settings(legacy)
        self.assertEqual(legacy, before)
        self.assertEqual(value['version'], 3)
        self.assertEqual(value['drain_step_uv'], 25000)
        self.assertFalse(any(field + '_v' in value for field in model.VOLTAGE_FIELDS))

    def test_migration_is_committed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'settings.json')
            storage.save_json_atomic(path, legacy_settings())
            value = storage.load_settings(path)
            with open(path) as stream:
                self.assertEqual(json.load(stream), value)
            with patch.object(storage, 'save_json_atomic', side_effect=AssertionError('second write')):
                self.assertEqual(storage.load_settings(path), value)

    def test_scan_boot_can_normalize_legacy_settings_without_flash_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'settings.json')
            legacy = legacy_settings()
            storage.save_json_atomic(path, legacy)
            with patch.object(storage, 'save_json_atomic', side_effect=AssertionError('boot write')):
                normalized = storage.load_settings(path, migrate=False)
            self.assertEqual(normalized['version'], 3)
            self.assertEqual(storage.load_json(path), legacy)

    def test_failed_migration_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'settings.json')
            storage.save_json_atomic(path, legacy_settings())
            with open(path, 'rb') as stream:
                before = stream.read()
            def interrupted(data, stream):
                stream.write('{"partial":')
                raise OSError('full flash')
            with patch.object(storage.json, 'dump', interrupted):
                with self.assertRaises(OSError):
                    storage.load_settings(path)
            with open(path, 'rb') as stream:
                self.assertEqual(stream.read(), before)
            self.assertFalse(os.path.exists(path + '.tmp'))

    def test_engine_uses_exact_grid_and_physical_unit_rows(self):
        value = settings(drain_start_uv=0, drain_stop_uv=1300000, drain_step_uv=100000,
                         source_voltage_uv=1000000, gate_start_uv=-1000000,
                         gate_stop_uv=-1000000)
        instrument = FakeInstrument()
        engine = Engine(instrument)
        engine.start_scan(value, calibration())
        self.assertEqual(engine._drains[-1], 1300000)
        points = [row for event, row in run_engine(engine, instrument) if event == 'point']
        self.assertEqual(len(points), 14)
        self.assertEqual(points[-1]['requested_vds'], 1.3)
        self.assertEqual(points[-1]['requested_vgs'], -1.0)
        self.assertAlmostEqual(points[-1]['vds'], 1.3)
        self.assertEqual(engine.state, 'complete')

    def test_new_calibration_retains_exact_rail_identity(self):
        value = settings(driver_supply_uv=20000001)
        engine = Engine(FakeInstrument())
        engine.start_calibration(value)
        self.assertEqual(engine.calibration['metadata']['driver_supply_uv'], 20000001)
        saved = calibration()
        saved['metadata'].update(driver_supply_v=20.0, driver_supply_uv=20000001)
        model.validate_calibration(saved, value)
        value['driver_supply_uv'] = 20000000
        with self.assertRaisesRegex(ValueError, 'supply changed'):
            model.validate_calibration(saved, value)
