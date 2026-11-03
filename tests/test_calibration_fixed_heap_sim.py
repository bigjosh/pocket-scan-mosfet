"""Full-UI synthetic binary calibration without writable filesystem access."""
import contextlib
import copy
from array import array
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware import calibration_binary, model, ram_payload
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_offset_calibration import OffsetInstrument, calibrate, offset_settings
from test_ram_ui import RamCloud, RamWifi
from test_ui import FakeDisplay


class CalibrationHeapSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scanner, _ = calibrate(OffsetInstrument())
        assert scanner.state == "complete", scanner.error
        cls.calibration = scanner.calibration

    def setUp(self):
        self.now = 1
        self.cancel = False
        self.pins, self.files, self.errors = [], [], []
        self.loads = 0
        owner = self

        def load_calibration():
            owner.loads += 1
            return copy.deepcopy(owner.calibration)

        def ticks_ms():
            owner.now += 1
            return owner.now

        def forbidden(*args, **kwargs):
            raise AssertionError("Unexpected persistence or physical instrument")

        class Pin:
            OUT = 1

            def __init__(self, number, mode=None, value=None):
                if mode is not None:
                    owner.pins.append((number, value))

            def value(self):
                return 0

        def cloud(buffer, **kwargs):
            value = RamCloud(buffer)
            value.prepare_calibration = lambda candidate: setattr(value, "cached", candidate)
            return value

        self.main = types.SimpleNamespace(
            App=App, model=model, Engine=Engine, Instrument=forbidden,
            storage=types.SimpleNamespace(load_settings=lambda **kwargs: offset_settings(),
                load_calibration=load_calibration, load_json=forbidden,
                save_settings=forbidden, begin_calibration_atomic=forbidden),
            Display=lambda **kwargs: FakeDisplay(),
            Inputs=lambda: types.SimpleNamespace(poll=lambda now: [("left",)] if owner.cancel else []),
            WifiManager=RamWifi, RamCloudManager=cloud, PackedScanBuffer=PackedScanBuffer,
            Pin=Pin, _scan_bytes=bytearray(4096 * 17), _display_buffer=None,
            _upload_transport=None, scan_utc=lambda: None,
            record_error=lambda context, error: owner.errors.append((context, str(error))),
            gc=types.SimpleNamespace(collect=lambda: None, mem_free=lambda: 100000),
            sys=types.SimpleNamespace(print_exception=lambda error: None),
            time=types.SimpleNamespace(ticks_ms=ticks_ms, ticks_diff=lambda a, b: a - b))
        path = Path(__file__).resolve().parents[1] / "prototype" / "calibration_fixed_heap_sim.py"
        spec = importlib.util.spec_from_file_location("calibration_heap_sim_test", path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, main=self.main, ram_payload=ram_payload,
                        calibration_binary=calibration_binary):
            spec.loader.exec_module(self.helper)

    def read_only_open(self, path, mode):
        self.assertEqual((path, mode), ("calibration.bin", "rb"))
        stream = io.BytesIO(b"read-only buffer fixture")
        self.files.append(stream)
        return stream

    def run_simulation(self, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.open", side_effect=self.read_only_open):
            self.helper.run(**kwargs)
        self.assertEqual(self.pins[:2], [(21, 0), (20, 0)])
        self.assertEqual(self.pins[-2:], [(21, 0), (20, 0)])
        self.assertTrue(all(stream.closed for stream in self.files))
        return output.getvalue()

    def test_maximum_grid_binary_full_ui_has_no_file_writes_and_keeps_both_fingerprints(self):
        text = self.run_simulation(worst_case=True, maximum_grid=True, binary=True)
        self.assertIn("CAL_SIM_PASS counts (311, 311)", text)
        self.assertIn("persistence binary", text)
        self.assertIn("binary_sha256", text)
        self.assertIn("settings_disk_writes 0 calibration_disk_writes 0", text)
        self.assertIn("physical_pins_low True production_files_untouched True", text)
        self.assertEqual(self.loads, 1)
        self.assertEqual(len(self.files), 1)
        self.assertEqual(self.errors, [])

    def test_commit_matches_actual_binary_bytes_and_canonical_candidate_hash(self):
        store = self.helper.SimulationStore()
        expected = io.BytesIO()
        calibration_binary.dump_calibration(self.calibration, expected)
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.open", side_effect=self.read_only_open):
            writer = store.begin_calibration_atomic()
            writer.commit(self.calibration)
        self.assertEqual(store.binary_bytes, len(expected.getvalue()))
        self.assertEqual(store.binary_digest, hashlib.sha256(expected.getvalue()).hexdigest())
        self.assertEqual(store.digest, ram_payload.calibration_hash(self.calibration))
        self.assertLessEqual(store.max_write_bytes, 256)
        self.assertEqual(store.commits, 1)
        self.assertTrue(self.files[0].closed)
        with self.assertRaisesRegex(RuntimeError, "Inactive"):
            writer.commit(self.calibration)

    def test_cancel_closes_preopened_reader_without_commit(self):
        self.cancel = True
        text = self.run_simulation(worst_case=True)
        self.assertIn("CAL_SIM_STOP Cancelled", text)
        self.assertNotIn("CAL_SIM_PASS", text)
        self.assertEqual(len(self.files), 1)

    def test_native_json_metadata_buffer_views_keep_exact_binary_and_logical_hashes(self):
        expected = io.BytesIO()
        calibration_binary.dump_calibration(self.calibration, expected)
        logical_hash = ram_payload.calibration_hash(self.calibration)
        store = self.helper.SimulationStore()
        native_writes = []

        def native_dump(value, stream):
            data = calibration_binary.json.dumps(value).encode("utf-8")
            if isinstance(stream, calibration_binary._MetadataWriter):
                native_writes.append(len(data))
            return stream.write(memoryview(data))

        with patch.object(calibration_binary.json, "dump", native_dump), \
                contextlib.redirect_stdout(io.StringIO()), \
                patch("builtins.open", side_effect=self.read_only_open):
            writer = store.begin_calibration_atomic()
            writer.commit(self.calibration)
        self.assertTrue(native_writes)
        self.assertEqual(store.binary_bytes, len(expected.getvalue()))
        self.assertEqual(store.binary_digest, hashlib.sha256(expected.getvalue()).hexdigest())
        self.assertEqual(store.digest, logical_hash)
        self.assertLessEqual(store.max_write_bytes, 256)
        self.assertTrue(self.files[0].closed)

    def test_sink_accepts_byte_buffers_and_counts_typed_views_in_bytes(self):
        with contextlib.redirect_stdout(io.StringIO()), patch("builtins.open", side_effect=self.read_only_open):
            writer = self.helper.SimulationStore().begin_calibration_atomic()
        chunks = (b"abc", bytearray(b"def"), memoryview(b"ghi"), memoryview(array("f", [1.25, 2.5])))
        try:
            for chunk in chunks:
                self.assertEqual(writer.write(chunk), len(bytes(chunk)))
            expected = b"".join(bytes(chunk) for chunk in chunks)
            self.assertEqual(writer.byte_count, len(expected))
            self.assertEqual(writer.hash.digest(), hashlib.sha256(expected).digest())
            with self.assertRaises(TypeError):
                writer.write("not a binary buffer")
            for chunk in (bytes(257), bytearray(257), memoryview(bytes(257)), memoryview(array("f", [0] * 65))):
                with self.subTest(kind=type(chunk).__name__), self.assertRaises(ValueError):
                    writer.write(chunk)
            self.assertEqual(writer.byte_count, len(expected))
        finally:
            writer.abort()

    def test_codec_failure_closes_reader_and_never_claims_success(self):
        output = io.StringIO()
        with patch.object(self.helper, "dump_calibration", side_effect=MemoryError("codec allocation")), \
                contextlib.redirect_stdout(output), patch("builtins.open", side_effect=self.read_only_open), \
                self.assertRaises(RuntimeError):
            self.helper.run(worst_case=True)
        self.assertNotIn("CAL_SIM_PASS", output.getvalue())
        self.assertTrue(all(stream.closed for stream in self.files))
        self.assertEqual(self.pins[-2:], [(21, 0), (20, 0)])

    def test_json_mode_is_rejected_before_loading_or_touching_hardware(self):
        with self.assertRaisesRegex(ValueError, "binary persistence only"):
            self.helper.run(binary=False)
        self.assertEqual(self.loads, 0)
        self.assertEqual(self.pins, [])


if __name__ == "__main__":
    unittest.main()
