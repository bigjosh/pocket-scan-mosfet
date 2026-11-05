"""Portable current-tare format, bounded acquisition and scanner integration."""

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from firmware import current_baseline as cb, model
from firmware.engine import Engine, OverrangeError
from tests.test_model_engine import FakeInstrument, run_engine, settings


IDENTITY = {"drain": {"address": 0x44, "die": 0x2260},
            "gate": {"address": 0x40, "die": 0x2281}}


def example(config=None):
    table = cb.Baseline()
    table.begin(config or settings(), IDENTITY)
    for role in range(2):
        table.add_knot(role, 0, 1000 * (role + 1), 0)
        table.add_knot(role, 1000000, 3000 * (role + 1), 4000)
        table.add_knot(role, 2000000, 2000 * (role + 1), 8000)
        table.set_zero(role, 0, -3300000, False)
    table.finish()
    return table


class TableTests(unittest.TestCase):
    def test_bulk_grid_is_fixed_strict_and_dense_near_ground(self):
        self.assertEqual(cb.SWEEP_POINTS + cb.ZERO_STEPS, cb.MAX_KNOTS)
        grid = [cb.sweep_duty_u16(index) for index in range(cb.SWEEP_POINTS)]
        self.assertEqual(grid[0], 0)
        self.assertEqual(grid[-1], 65535)
        self.assertTrue(all(left < right for left, right in zip(grid, grid[1:])))
        self.assertLess(grid[1] * model.GATE_PWM_FULL_SCALE_V / 65535, 0.011)
        self.assertGreater(grid[-1] - grid[-2], grid[1] - grid[0])

    def test_fixed_external_buffer_and_integer_interpolation_endpoint_hold(self):
        table = example()
        self.assertEqual(cb.BUFFER_BYTES, 1720)
        self.assertEqual(len(table._data), cb.BUFFER_BYTES)
        self.assertEqual(table.offset_nv("drain", -100000), 1000)
        self.assertEqual(table.offset_nv(0, 500000), 2000)
        self.assertEqual(table.offset_nv(0, 1500000), 2500)
        self.assertEqual(table.offset_nv(0, 5000000), 2000)
        self.assertEqual(table.offset_nv(1, 1500000), 5000)
        self.assertEqual(cb.Baseline().offset_nv(0, 100), 0)
        with self.assertRaises(ValueError):
            cb.Baseline(bytearray(1))

    def test_binary_roundtrip_is_exact_and_metadata_is_compact(self):
        table = example()
        digest = table.sha256
        self.assertEqual(len(digest), 64)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "baseline.bin")
            table.save(path)
            self.assertEqual(Path(path).stat().st_size, cb.BUFFER_BYTES)
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
            storage = bytearray(cb.BUFFER_BYTES)
            loaded = cb.Baseline(storage)
            self.assertTrue(loaded.load(path))
            self.assertIs(loaded._data, storage)
            self.assertEqual(loaded._data, table._data)
            self.assertEqual(loaded.sha256, digest)
            self.assertTrue(loaded.compatible(settings(), IDENTITY))
            self.assertFalse(loaded.compatible(settings(ina_average_count=64), IDENTITY))
            self.assertFalse(loaded.compatible(settings(), {"drain": {"address": 0x41}, "gate": {}}))
            metadata = loaded.metadata(settings())
            self.assertEqual(metadata["lookup"], "linear_endpoint_hold")
            self.assertEqual(metadata["drain"]["min_bus_uv"], 0)
            self.assertEqual(metadata["gate"]["max_bus_uv"], 2000000)
            self.assertNotIn("knots", metadata)

    def test_drain_only_metadata_keeps_binary_and_both_zero_anchors(self):
        table = example()
        table.set_zero(0, 100, 0, True)
        table.set_zero(1, 9000, 0, True)
        table.finish()
        original = bytes(table._data)
        digest = table.sha256
        metadata = table.metadata(settings())
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["applied_to"], "drain")
        self.assertEqual(len(metadata), 6)
        self.assertEqual(metadata["gate"]["zero_duty_u16"], 9000)
        self.assertTrue(metadata["gate"]["zero_valid"])
        self.assertEqual(table.drive_duty("gate", 3.3, settings()), 9000 / 65535)
        self.assertEqual(bytes(table._data), original)
        self.assertEqual(table.sha256, digest)
        self.assertEqual(len(original), 1720)

    def test_missing_corrupt_truncated_oversize_and_semantically_invalid_fail_closed(self):
        table = example()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.bin"
            loaded = cb.Baseline()
            self.assertFalse(loaded.load(str(path)))
            for content in (table._data[:100], table._data + b"x"):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    loaded.load(str(path))
                self.assertFalse(loaded.available)
            corrupt = bytearray(table._data)
            corrupt[cb.HEADER_BYTES + 10] ^= 1
            path.write_bytes(corrupt)
            with self.assertRaisesRegex(ValueError, "checksum"):
                loaded.load(str(path))
            corrupt = bytearray(table._data)
            # Rehash invalid ordering, proving semantic checks beyond CRC.
            struct.pack_into("<i", corrupt, cb.HEADER_BYTES + cb.RECORD_BYTES, 0)
            corrupt[cb.DATA_END:] = hashlib.sha256(corrupt[:cb.DATA_END]).digest()
            path.write_bytes(corrupt)
            with self.assertRaisesRegex(ValueError, "knot"):
                loaded.load(str(path))
            self.assertFalse(loaded.available)

    def test_atomic_save_failure_keeps_committed_file(self):
        table = example()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "baseline.bin")
            table.save(path)
            original = Path(path).read_bytes()
            candidate = example(settings(driver_supply_uv=19000000))
            with patch.object(cb.os, "replace", side_effect=OSError("rename failed")):
                with self.assertRaises(OSError):
                    candidate.save(path)
            self.assertEqual(Path(path).read_bytes(), original)
            self.assertFalse(Path(path + ".tmp").exists())
            with self.assertRaises(ValueError):
                cb.Baseline().save(path)
            self.assertEqual(Path(path).read_bytes(), original)

    def test_previous_sensor_baseline_loads_but_never_corrects_or_anchors(self):
        table = example()
        # Reconstruct the earlier dual-INA226 physical-unit file: original
        # gate resistor, 0x41 identity and zero-filled reserved ADC words.
        table._put(32, 638000000)
        struct.pack_into("<HH", table._data, 68, 0x41, 0x2260)
        for offset in cb._ADC_OFFSETS:
            table._put(offset, 0)
        table.set_zero(1, 8300, 0, True)
        table.finish()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "baseline.bin")
            table.save(path)
            loaded = cb.Baseline()
            self.assertTrue(loaded.load(path))
            self.assertTrue(loaded.available)
            self.assertFalse(loaded.compatible(settings()))
            self.assertFalse(loaded.compatible(settings(), IDENTITY))
            self.assertIsNone(loaded.metadata(settings()))
            self.assertEqual(loaded.drive_duty("gate", 3.3, settings()),
                             model.drive_duty("gate", 3.3))

    def test_shunt_timing_range_and_sensor_identity_are_fingerprinted(self):
        for offset, value in ((32, 101000000), (104, 1100)):
            table = example()
            table._put(offset, value)
            table.finish()
            self.assertFalse(table.compatible(settings(), IDENTITY))
        table = example()
        self.assertFalse(table.compatible(settings(), {
            "drain": IDENTITY["drain"], "gate": {"address": 0x40, "die": 0x2282}}))
        table._put(112, 40960)  # Narrow INA228 range is unsupported.
        with self.assertRaisesRegex(ValueError, "ADC"):
            table.finish()

    def test_gate_range_uses_ina228_limits_while_drain_keeps_ina226_limits(self):
        table = example()
        table.add_knot(1, 85000000, 100000000, 65535)
        table.finish()
        self.assertEqual(table.offset_nv(1, 85000000), 100000000)
        for role, bus, shunt in ((0, 42000000, 0), (0, 3000000, 81920000),
                                 (1, 103000000, 0), (1, 86000000, 163840000)):
            invalid = example()
            invalid.add_knot(role, bus, shunt, 65535)
            with self.assertRaisesRegex(ValueError, "knot"):
                invalid.finish()

    def test_clipped_duplicates_do_not_grow_or_break_lookup(self):
        table = cb.Baseline()
        table.begin(settings(), IDENTITY)
        for role in range(2):
            table.add_knot(role, 1000, 5000, 0)
            table.add_knot(role, 2000, 6000, 100)
            table.add_knot(role, 2000, 7000, 200)
            table.add_knot(role, 1999, 9999, 300)
        table.finish()
        self.assertEqual(table.count(0), 2)
        self.assertEqual(table.offset_nv(0, 2000), 7000)

    def test_refinement_inserts_in_place_sorted_and_preserves_other_channel(self):
        table = example()
        original = bytes(table._data[table._slot(1, 0):table._slot(1, cb.MAX_KNOTS)])
        buffer_id = id(table._data)
        table.insert_knot(0, 1500000, 9999, 6000)
        table.insert_knot(0, 500000, 8888, 2000)
        table.insert_knot(0, 1500000, 9998, 6001)
        self.assertEqual(table.count(0), 5)
        self.assertEqual([table.bus_uv(0, i) for i in range(5)], [0, 500000, 1000000, 1500000, 2000000])
        self.assertEqual(table.offset_nv(0, 1500000), 9998)
        self.assertEqual(id(table._data), buffer_id)
        self.assertEqual(bytes(table._data[table._slot(1, 0):table._slot(1, cb.MAX_KNOTS)]), original)
        for index in range(5, cb.MAX_KNOTS):
            table.insert_knot(0, 2000000 + index, 1000, 9000)
        self.assertEqual(table.count(0), cb.MAX_KNOTS)
        with self.assertRaises(ValueError):
            table.insert_knot(0, 4000000, 1000, 10000)

    def test_zero_anchor_uses_nominal_slope_and_only_compatible_verified_capture(self):
        config = settings(source_voltage_uv=3300000)
        table = example(config)
        table.set_zero(0, 100, 500, True)
        table.set_zero(1, 8300, -500, True)
        table.finish()
        self.assertAlmostEqual(table.drive_duty("drain", 3.3, config), 100 / 65535)
        self.assertAlmostEqual(table.drive_duty("gate", 3.3, config), 8300 / 65535)
        self.assertAlmostEqual(table.drive_duty("drain", 4.3, config), 100 / 65535 + 1 / 22.44)
        stale = dict(config, source_voltage_uv=3200000)
        self.assertEqual(table.drive_duty("drain", 3.3, stale), model.drive_duty("drain", 3.3))
        table.set_zero(0, 100, 90000, False)
        self.assertEqual(table.drive_duty("drain", 3.3, config), model.drive_duty("drain", 3.3))


class CodeInstrument:
    identity = IDENTITY

    def __init__(self, drain_floor=3.25):
        self.duties = [0.0, 0.0]
        self.now = 0
        self.ready_at = None
        self.zeroes = self.reads = self.triggers = self.commands = 0
        self.drain_floor = drain_floor
        self.saturation = False
        self.bad_stop = False

    def set_duty(self, role, duty):
        self.commands += 1
        self.duties[cb.ROLES.index(role)] = duty

    def zero(self):
        self.zeroes += 1
        self.duties[0] = self.duties[1] = 0.0
        self.ready_at = None
        if self.bad_stop:
            raise OSError("cannot stop")

    def poll_alerts(self):
        pass

    def begin_measurement(self, count):
        self.triggers += 1
        self.ready_at = self.now + (count * 2200 + 999) // 1000

    def poll_measurement(self):
        return self.now >= self.ready_at

    def read_codes_into(self, buffer):
        self.reads += 1
        if self.saturation:
            raise OverrangeError("gate", "shunt_saturation", 524287, 1, 524287 * 0.3125e-6, "V")
        for role in range(2):
            voltage = min(16.8, (self.drain_floor + 22.44 * self.duties[0] if role == 0 else
                                25.74 * self.duties[1]))
            bus_lsb = 0.00125 if role == 0 else 0.0001953125
            buffer[role * 2] = int(voltage / bus_lsb + 0.5)
            # Deliberately nonconstant baseline, with gate parasitic ~8uA.
            buffer[role * 2 + 1] = (2 if role == 0 else 2000) + buffer[role * 2] // 100


def run_calibration(instrument, config=None):
    workspace = bytearray(cb.WORKSPACE_BYTES)
    runner = cb.Calibrator(instrument, config or settings(), buffer=workspace)
    runner.start()
    assert instrument.commands == instrument.zeroes == 0
    for now in range(0, 2000000, 5):
        instrument.now = now
        before = instrument.reads
        runner.tick(now)
        assert instrument.reads - before <= 1
        if runner.state != "running":
            return runner, workspace
    raise AssertionError("Calibration never completed")


class CalibrationTests(unittest.TestCase):
    def test_gate_fractional_lsb_rounds_once_after_native_code_averaging(self):
        class OneReadInstrument(CodeInstrument):
            def poll_measurement(self):
                return True

            def read_codes_into(self, buffer):
                buffer[0], buffer[1], buffer[2], buffer[3] = 800, 2, 101, self.sign

        for sign in (-1, 1):
            hw = OneReadInstrument()
            hw.sign = sign
            runner = cb.Calibrator(hw, settings())
            runner.start()
            runner._role, runner.channel = 1, "gate"
            runner.phase = "convert"
            runner._blocks, runner._bus_sum, runner._shunt_sum = 3, 300, 0
            code_buffer_id = id(runner._codes)
            runner.tick(0)
            self.assertEqual(runner._candidate.bus_uv(1, 0), 19580)
            self.assertEqual(runner._candidate.shunt_nv(1, 0), sign * 78)
            self.assertEqual(id(runner._codes), code_buffer_id)

    def test_gate_twenty_bit_codes_and_fitted_shunt_are_scaled(self):
        class OneReadInstrument(CodeInstrument):
            def poll_measurement(self):
                return True

            def read_codes_into(self, buffer):
                # 85 V bus; 100 uV shunt across the fitted 98.8 ohms ~= 1.01215 uA.
                buffer[0], buffer[1], buffer[2], buffer[3] = 800, 2, 435200, 320

        runner = cb.Calibrator(OneReadInstrument(), settings())
        runner.start()
        runner._role, runner.channel, runner.phase = 1, "gate", "convert"
        runner._blocks, runner._bus_sum, runner._shunt_sum = 3, 435200 * 3, 320 * 3
        runner.tick(0)
        self.assertEqual(runner._candidate.bus_uv(1, 0), 85000000)
        self.assertEqual(runner._candidate.shunt_nv(1, 0), 100000)
        self.assertEqual(runner._candidate.terminal_uv(1, 0), 84999900)
        self.assertAlmostEqual(runner._candidate.shunt_nv(1, 0) / 1e9 / model.GATE_SHUNT_OHM,
                               1.0121457489878543e-6, places=13)

    def test_native_and_portable_voltage_readbacks_use_identical_gate_codes(self):
        class VoltageInstrument(CodeInstrument):
            read_codes_into = None

            def read_pair(self):
                codes = [0, 0, 0, 0]
                CodeInstrument.read_codes_into(self, codes)
                return {"drain": {"bus_v": codes[0] * 0.00125,
                                   "shunt_v": codes[1] * 0.0000025},
                        "gate": {"bus_v": codes[2] * 0.0001953125,
                                  "shunt_v": codes[3] * 0.0000003125}}

        native, portable = CodeInstrument(), VoltageInstrument()
        native.duties = portable.duties = [0.2, 0.4]
        direct = cb.Calibrator(native, settings())
        fallback = cb.Calibrator(portable, settings())
        direct._read()
        fallback._read()
        self.assertEqual(direct._codes, fallback._codes)
        self.assertGreater(direct._codes[2], 32767)

    def test_zero_search_samples_correct_a_bias_kink_missed_by_bulk_interpolation(self):
        class KinkInstrument(CodeInstrument):
            def read_codes_into(self, buffer):
                super().read_codes_into(buffer)
                bus_uv = cb._round_div(buffer[2] * 3125, 16)
                # Representative supply-adjacent transition: a bulk-only
                # table invents a ramp across a much narrower actual change.
                fraction = min(1.0, max(0.0, (bus_uv - 3180000) / 70000))
                buffer[3] = 4472 + int(11056 * fraction)

        hw = KinkInstrument()
        runner, _ = run_calibration(hw)
        self.assertEqual(runner.state, "complete", runner.error)
        metadata = runner.result.metadata(settings())
        self.assertTrue(metadata["gate"]["zero_valid"])
        hw.duties[1] = metadata["gate"]["zero_duty_u16"] / 65535
        observed = [0, 0, 0, 0]
        hw.read_codes_into(observed)
        bus_uv = cb._round_div(observed[2] * 3125, 16)
        shunt_nv = cb._round_div(observed[3] * 625, 2)
        refined_error_ua = abs(shunt_nv - runner.result.offset_nv(1, bus_uv)) / 1000 / model.GATE_SHUNT_OHM
        coarse = cb.Baseline()
        coarse.begin(settings(), IDENTITY)
        coarse.add_knot(0, 0, 0, 0)
        coarse.add_knot(0, 30000000, 0, 65535)
        for index in range(cb.SWEEP_POINTS):
            duty = cb.sweep_duty_u16(index)
            hw.duties[1] = duty / 65535
            hw.read_codes_into(observed)
            coarse.add_knot(1, cb._round_div(observed[2] * 3125, 16),
                            cb._round_div(observed[3] * 625, 2), duty)
        coarse.finish()
        coarse_error_ua = abs(shunt_nv - coarse.offset_nv(1, bus_uv)) / 1000 / model.GATE_SHUNT_OHM
        self.assertGreater(coarse_error_ua, 1.0)
        self.assertLess(refined_error_ua, 0.01)
        self.assertGreater(runner.result.count(1), coarse.count(1))

    def test_complete_fixed_workspace_sweep_zero_capture_and_repeatable_lookup(self):
        hw = CodeInstrument()
        runner, workspace = run_calibration(hw)
        self.assertEqual(runner.state, "complete", runner.error)
        self.assertTrue(runner.shutdown_succeeded)
        self.assertEqual(hw.duties, [0, 0])
        self.assertIs(runner.result._data, workspace)
        self.assertEqual(runner.progress, 1)
        for role in range(2):
            self.assertGreater(runner.result.count(role), 20)
            self.assertLessEqual(runner.result.count(role), cb.MAX_KNOTS)
            zero = runner.result.metadata(settings())[cb.ROLES[role]]
            self.assertTrue(zero["zero_valid"], zero)
            self.assertLess(abs(zero["zero_residual_uv"]), 2000)
        self.assertGreaterEqual(hw.triggers, 2 * cb.SWEEP_POINTS * cb.BLOCKS_PER_KNOT)
        self.assertLessEqual(hw.triggers, 2 * (cb.SWEEP_POINTS + cb.ZERO_STEPS) * cb.BLOCKS_PER_KNOT)
        self.assertEqual(hw.reads, hw.triggers * 2)
        self.assertEqual(hw.commands, hw.triggers)

    def test_unreachable_zero_is_recorded_without_false_anchor_or_scan_failure(self):
        hw = CodeInstrument(drain_floor=3.4)
        runner, _ = run_calibration(hw)
        self.assertEqual(runner.state, "complete", runner.error)
        zero = runner.result.metadata(settings())["drain"]
        self.assertFalse(zero["zero_valid"])
        self.assertEqual(zero["zero_duty_u16"], 0)
        self.assertGreater(zero["zero_residual_uv"], 90000)

    def test_cancel_saturation_timeout_and_failed_stop_never_publish_candidate(self):
        for failure in ("cancel", "saturation", "timeout", "stop"):
            hw = CodeInstrument()
            runner = cb.Calibrator(hw, settings())
            runner.start()
            if failure == "saturation":
                hw.saturation = True
            if failure == "timeout":
                hw.poll_measurement = lambda: False
            for now in range(0, 500, 5):
                hw.now = now
                runner.tick(now)
                if now == 150 and failure in ("cancel", "stop"):
                    hw.bad_stop = failure == "stop"
                    runner.cancel()
                if runner.state != "running":
                    break
            self.assertIn(runner.state, ("cancelled", "error"), failure)
            self.assertIsNone(runner.result)
            self.assertFalse(runner._candidate.available)
            self.assertEqual(runner.shutdown_succeeded, failure != "stop")


class CorrectedEngineTests(unittest.TestCase):
    def test_corrects_drain_only_preserves_raw_gate_voltage_and_raw_fallback(self):
        config = settings()
        table = example(config)
        hardware = FakeInstrument(drain_current=0.001, gate_current=10e-6)
        hardware.identity = IDENTITY
        engine = Engine(hardware, baseline=table)
        metadata = engine.current_correction(config)
        self.assertEqual(metadata["sha256"], table.sha256)
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["applied_to"], "drain")
        engine.start_scan(config)
        events = run_engine(engine, hardware)
        row = next(row for kind, row in events if kind == "point")
        self.assertAlmostEqual(row["vds"], row["requested_vds"])
        self.assertAlmostEqual(row["vgs"], row["requested_vgs"])
        self.assertAlmostEqual(row["id_a"], 0.001 - 2000e-9 / model.DRAIN_SHUNT_OHM)
        self.assertAlmostEqual(row["ig_a"], 10e-6)
        hardware = FakeInstrument(drain_current=0.001, gate_current=10e-6)
        hardware.identity = IDENTITY
        engine = Engine(hardware, baseline=table)
        changed = dict(config, settle_ms=200)
        self.assertIsNone(engine.current_correction(changed))
        engine.start_scan(changed)
        row = next(row for kind, row in run_engine(engine, hardware) if kind == "point")
        self.assertAlmostEqual(row["id_a"], 0.001)
        self.assertAlmostEqual(row["ig_a"], 10e-6)

    def test_raw_saturation_cannot_be_subtracted_away(self):
        hw = FakeInstrument()
        hw.identity = IDENTITY
        hw.read_error = OverrangeError("gate", "shunt_saturation", 524287, 1, 524287 * 0.3125e-6, "V")
        engine = Engine(hw, baseline=example())
        engine.start_scan(settings())
        events = run_engine(engine, hw)
        self.assertEqual(engine.state, "partial")
        self.assertFalse(any(kind == "point" for kind, row in events))
        self.assertEqual(hw.duties, {"drain": 0, "gate": 0})


if __name__ == "__main__":
    unittest.main()
