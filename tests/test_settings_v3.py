"""Hardware averaging migration and acquisition metadata."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from firmware import model, storage


def legacy_settings(version=2, batches=2, samples=2):
    result = {"version": version, "batches": batches, "samples_per_batch": samples,
              "sample_pause_ms": 4, "settle_ms": 200}
    for field in model.VOLTAGE_FIELDS:
        value = model.DEFAULT_SETTINGS[field + "_uv"]
        result[field + ("_v" if version == 1 else "_uv")] = value / 1000000 if version == 1 else value
    return result



class HardwareAveragingSettingsTests(unittest.TestCase):
    def test_new_default_uses_64_averages_and_no_software_sampling_knobs(self):
        value = model.validate_settings(model.DEFAULT_SETTINGS)
        self.assertEqual(value["version"], 3)
        self.assertEqual(value["ina_average_count"], 64)
        self.assertEqual(value["settle_ms"], 100)
        for key in ("batches", "samples_per_batch", "sample_pause_ms"):
            self.assertNotIn(key, value)

    def test_all_hardware_counts_are_accepted_as_true_integers(self):
        for count in model.INA_AVERAGE_COUNTS:
            value = dict(model.DEFAULT_SETTINGS, ina_average_count=count)
            self.assertIs(type(model.validate_settings(value)["ina_average_count"]), int)
        for count in (0, 2, 3, 5, 65, 1025, True, 4.0, "64", None):
            with self.subTest(count=count), self.assertRaises(ValueError):
                model.validate_settings(dict(model.DEFAULT_SETTINGS, ina_average_count=count))

    def test_legacy_products_round_up_and_clamp_without_modifying_original(self):
        for version in (1, 2):
            for batches, samples, expected in ((2, 1, 4), (2, 2, 4), (3, 2, 16),
                                               (10, 50, 512), (32, 500, 1024)):
                value = legacy_settings(version, batches, samples)
                before = copy.deepcopy(value)
                normalized = model.validate_settings(value)
                self.assertEqual(normalized["ina_average_count"], expected)
                self.assertEqual(normalized["version"], 3)
                self.assertEqual(value, before)
                for field in model.VOLTAGE_FIELDS:
                    self.assertIs(type(normalized[field + "_uv"]), int)
                    self.assertNotIn(field + "_v", normalized)

    def test_mixed_sampling_modes_and_invalid_old_counts_are_not_silently_accepted(self):
        for key in ("batches", "samples_per_batch", "sample_pause_ms"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "mixed averaging"):
                model.validate_settings(dict(model.DEFAULT_SETTINGS, **{key: 2}))
        for version in (1, 2):
            value = legacy_settings(version)
            value["ina_average_count"] = 4
            with self.assertRaisesRegex(ValueError, "mixed averaging"):
                model.validate_settings(value)
        for batches, samples in ((1, 2), (33, 2), (2, 0), (2, 501), (True, 2)):
            with self.assertRaises(ValueError):
                model.validate_settings(legacy_settings(2, batches, samples))

    def test_storage_boot_normalizes_without_write_then_explicit_migration_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(json.dumps(legacy_settings()), encoding="utf-8")
            original = path.read_bytes()
            with patch.object(storage, "save_json_atomic", side_effect=AssertionError("boot write")):
                normalized = storage.load_settings(str(path), migrate=False)
            self.assertEqual(normalized["ina_average_count"], 4)
            self.assertEqual(path.read_bytes(), original)
            committed = storage.load_settings(str(path))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), committed)
            self.assertEqual(committed["version"], 3)

    def test_single_block_metadata_does_not_invent_point_variance(self):
        value = model.acquisition_metadata(model.DEFAULT_SETTINGS)
        self.assertEqual(value, {"acquisition_mode": "ina226_ina228_triggered_v1", "ina_average_count": 64,
                                 "bus_conversion_us": 1100, "shunt_conversion_us": 1100,
                                 "gate_bus_conversion_us": 1052, "gate_shunt_conversion_us": 1052,
                                 "blocks_per_point": 1, "point_variance": "unavailable"})
        self.assertEqual(model.acquisition_metadata(model.DEFAULT_SETTINGS, 3)["point_variance"],
                         "between_blocks")
        for count in (0, -1, True, 1.0):
            with self.assertRaises(ValueError):
                model.acquisition_metadata(model.DEFAULT_SETTINGS, count)


if __name__ == "__main__":
    unittest.main()
