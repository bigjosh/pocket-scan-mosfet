"""Calibration reserves packed numeric buffers and reuses only private scratch."""
import copy
import json
import unittest
from unittest.mock import patch

from firmware import engine as module, model
from firmware.engine import Engine
from firmware.calibration_data import PackedPoints
from firmware.calibration_json import dump_calibration
from array import array
from io import StringIO
from test_model_engine import FakeInstrument, calibration, run_engine, settings
from test_offset_calibration import OffsetInstrument, offset_settings


class CalibrationWorkspaceTests(unittest.TestCase):
    def test_batch_prefix_mean_and_sample_deviation_ignore_unfilled_slots(self):
        scanner = Engine(FakeInstrument())
        scanner.start_calibration(settings(batches=3, samples_per_batch=2))
        scanner.tick(0)
        batches = scanner._batchmeans
        for index in range(len(batches)):
            batches[index] = 1234.0  # Unfilled tail must never enter a summary.
        summary = scanner._cal_workspace["summary"]
        for batch_index in range(3):
            for column in range(6):
                batches[batch_index * 6 + column] = (batch_index + 1) * (column + 1)
            scanner._batch_count = batch_index + 1
            result = scanner._summary()
            self.assertIs(summary, result)
            self.assertEqual(batch_index + 1, result["batches_completed"])
            for column, key in enumerate(module.MEASURE_FIELDS):
                self.assertEqual((batch_index + 2) / 2 * (column + 1), result[key])
            expected_std = (0.0, 2 ** -0.5, 1.0)[batch_index]
            for column, (_, std_key) in enumerate(module.STD_FIELDS):
                self.assertAlmostEqual(expected_std * (column + 3), result[std_key])
        scanner._drive()
        self.assertEqual(0, scanner._batch_count)
        for column in range(6):
            batches[column] = 0.25
        scanner._batch_count = 1
        self.assertEqual(0.25, scanner._summary()["vds"])
        self.assertEqual(0.0, scanner.latest["id_std_a"] if scanner.latest else summary["id_std_a"])
        scanner.cancel()

    def test_full_199_point_tables_fill_existing_numeric_buffers_and_keep_json_shape(self):
        hardware = FakeInstrument()
        scanner = Engine(hardware)
        value = settings(driver_supply_uv=18800000, source_voltage_uv=3300000,
                         batches=10, samples_per_batch=50)
        scanner.start_calibration(value)
        self.assertEqual(199, scanner._total)
        self.assertEqual([], hardware.commands)
        workspace = scanner._cal_workspace
        original_points = {role: channel["points"]
                           for role, channel in scanner.calibration["channels"].items()}
        capabilities = {role: channel["capability"]
                        for role, channel in scanner.calibration["channels"].items()}
        buffers = {role: points._values for role, points in original_points.items()}
        for points in original_points.values():
            self.assertIsInstance(points, PackedPoints)
            self.assertEqual(0, len(points))
            self.assertEqual(199 * 4 * 4, len(points._values) * points._values.itemsize)
        # No retained map or table buffer allocation after arming.
        with patch.object(module, "_calibration_slots", side_effect=AssertionError("late table allocation")), \
                patch.object(PackedPoints, "__getitem__", side_effect=AssertionError("snapshot during acquisition")):
            # Validation exports a bounded snapshot only after outputs stop.
            validate = model.validate_calibration
            def validate_after_stop(*args):
                self.assertTrue(scanner.shutdown_succeeded)
                return args[0]
            with patch.object(model, "validate_calibration", validate_after_stop):
                run_engine(scanner, hardware, maximum_ms=900000)
        validate(scanner.calibration, value)
        self.assertEqual("complete", scanner.state, scanner.error)
        for role, channel in scanner.calibration["channels"].items():
            self.assertEqual(199, len(channel["points"]))
            self.assertIs(capabilities[role], channel["capability"])
            for index, point in enumerate(channel["points"]):
                self.assertIs(original_points[role], channel["points"])
                self.assertIs(buffers[role], channel["points"]._values)
                self.assertEqual(set(module.POINT_FIELDS) | ({"zero_min_v", "zero_max_v"} if index == 0 else set()),
                                 set(point))
        self.assertIs(workspace["summary"], scanner.latest)
        self.assertIsNone(scanner._cal_workspace)
        self.assertEqual((), scanner.latest["batchmeans"])
        self.assertEqual(3, scanner.latest["acquisition_blocks_completed"])
        output = StringIO()
        dump_calibration(scanner.calibration, output)
        model.validate_calibration(json.loads(output.getvalue()), value)

    def test_live_calibration_reuses_summary_batches_and_settling_maps(self):
        hardware = OffsetInstrument()
        scanner = Engine(hardware)
        scanner.start_calibration(offset_settings(ina_average_count=16))
        workspace = scanner._cal_workspace
        batches = workspace["batchmeans"]
        self.assertIsInstance(batches, array)
        self.assertEqual(3 * 6 * 4, len(batches) * batches.itemsize)
        summary_keys = set(workspace["summary"])
        for now in range(300000):
            hardware.now = now
            scanner.tick(now)
            self.assertIs(batches, workspace["batchmeans"])
            self.assertEqual(summary_keys, set(workspace["summary"]))
            if scanner.latest is not None:
                self.assertIs(workspace["summary"], scanner.latest)
            if scanner.state != "calibrating":
                break
            if scanner._index:
                self.assertIs(workspace["sums"], scanner._sums)
                self.assertIs(workspace["settle"], scanner._settle)
                self.assertIs(workspace["target"], scanner._target)
                if scanner._batch_count:
                    self.assertEqual(scanner._batch_count, scanner.latest["batches_completed"])
        self.assertEqual("complete", scanner.state, scanner.error)
        used_endpoints = 0
        for role, channel in scanner.calibration["channels"].items():
            if "drive_v" in channel["points"][-1]:
                self.assertLess(channel["points"][-1]["drive_duty"], channel["points"][-1]["duty"])
                used_endpoints += 1
        self.assertGreater(used_endpoints, 0)
        self.assertIsNone(scanner._cal_workspace)

    def test_cancel_and_fault_release_workspace_aliases_before_saved_map_reload(self):
        for fault in (False, True):
            with self.subTest(fault=fault):
                hardware = FakeInstrument()
                scanner = Engine(hardware)
                scanner.start_calibration(settings())
                scanner.tick(0)
                if fault:
                    hardware.current["drain"] = 0.006
                    run_engine(scanner, hardware)
                    self.assertEqual("error", scanner.state)
                else:
                    scanner.cancel()
                    self.assertEqual("cancelled", scanner.state)
                self.assertIsNone(scanner._cal_workspace)
                for name in ("_batchmeans", "_sums", "_target", "_duties", "_settle",
                             "_settle_voltage_sums", "_voltage_min", "_voltage_max"):
                    self.assertIsNone(getattr(scanner, name), name)

    def test_capacity_or_scratch_allocation_failure_never_arms_or_replaces_map(self):
        for failure in ("points", "scratch"):
            with self.subTest(failure=failure):
                hardware = FakeInstrument()
                scanner = Engine(hardware)
                saved = calibration()
                scanner.calibration = saved
                original = module._calibration_slots
                calls = []
                def point(count):
                    calls.append(count)
                    if len(calls) == 2:
                        raise MemoryError("synthetic table capacity")
                    return original(count)
                target = "_calibration_slots" if failure == "points" else "_calibration_workspace"
                effect = point if failure == "points" else MemoryError("synthetic workspace capacity")
                with patch.object(module, target, side_effect=effect), self.assertRaises(MemoryError):
                    scanner.start_calibration(settings(driver_supply_uv=30000000))
                self.assertEqual("idle", scanner.state)
                self.assertIs(saved, scanner.calibration)
                self.assertEqual([], hardware.commands)
                self.assertEqual(0, hardware.reads)
                self.assertEqual(311, len(model.calibration_targets_uv(settings(driver_supply_uv=30000000))))

    def test_scan_rows_remain_independent_snapshots_after_calibration(self):
        hardware = FakeInstrument()
        scanner = Engine(hardware)
        value = settings(drain_start_uv=0, drain_stop_uv=1000000, drain_step_uv=500000)
        scanner.start_calibration(value)
        run_engine(scanner, hardware)
        self.assertEqual("complete", scanner.state, scanner.error)
        old_summary = scanner.latest
        scanner.start_scan(value, scanner.calibration)
        self.assertIsNone(scanner._cal_workspace)
        rows, snapshots = [], []
        for now in range(100000):
            hardware.now = now
            for kind, result in scanner.tick(now):
                if kind == "point":
                    rows.append(result)
                    snapshots.append(copy.deepcopy(result))
            if scanner.state != "scanning":
                break
        self.assertEqual("complete", scanner.state, scanner.error)
        self.assertEqual(3, len(rows))
        self.assertEqual(rows, snapshots)
        self.assertEqual(3, len({id(row) for row in rows}))
        self.assertTrue(all(row is not old_summary for row in rows))
        self.assertEqual(3, len({id(row["batchmeans"]) for row in rows}))


if __name__ == "__main__":
    unittest.main()
