"""One-shot upload ownership, acknowledgement and flash-free scan lifecycle."""
import copy
import json
import unittest
from unittest.mock import patch

from firmware import model
from firmware.ram_scan import PackedScanBuffer
from firmware.ram_payload import PayloadReader
from firmware.ram_cloud import RamCloudManager, UPLOAD_ERROR


URL = 'https://script.google.com/macros/s/test-receiver/exec'


class Store:
    def __init__(self):
        self.value = {'version': 1, 'url': URL, 'enabled': True}
        self.writes = []

    def load_json(self, path):
        return copy.deepcopy(self.value)

    def save_json_atomic(self, path, value):
        self.writes.append((path, value))


class Transport:
    def __init__(self):
        self.starts, self.closes = 0, 0
        self.ack = None
        self.fault = False
        self.scan = None

    def start(self, url, scan, scan_id, sha256, count):
        self.starts += 1
        self.scan = scan
        self.expected = {'ok': True, 'scan_id': scan_id, 'sha256': sha256,
                         'point_count': count, 'sheet_id': 123}

    def step(self):
        if self.fault:
            raise RuntimeError('Private network detail ' + URL)
        return self.ack

    def close(self):
        self.closes += 1
        self.scan = None


def scan_settings():
    result = dict(model.DEFAULT_SETTINGS)
    result.update(drain_start_uv=0, drain_stop_uv=0, gate_start_uv=0, gate_stop_uv=0, ina_average_count=4)
    return result


def point():
    return {'index': 0, 'curve': 0, 'requested_vds': 0.0, 'requested_vgs': 0.0,
            'vds': 0.001, 'vgs': 0.002, 'id_a': 1e-6, 'ig_a': -1e-6,
            'acquisition_blocks_completed': 1, 'drain_voltage_limited': False, 'gate_voltage_limited': False}


class RamCloudTests(unittest.TestCase):
    def setUp(self):
        self.store, self.transport = Store(), Transport()
        self.buffer = PackedScanBuffer(capacity=1)
        self.manager = RamCloudManager(self.buffer, self.transport, self.store,
            device_id='face', random_bytes=lambda size: b'\x01' * size,
            utc_provider=lambda: '2026-11-02T00:00:00Z')

    def prepare(self):
        self.manager.begin_scan(scan_settings(), 0)
        self.manager.record_point(point())
        self.manager.finish_scan(100)
        for unused in range(100):
            self.manager.tick(101, online=False)
            if self.manager.state != 'preparing':
                break
        self.assertEqual(self.manager.state, 'waiting_wifi')

    def test_progress_stages_follow_preparation_transport_and_exact_ack(self):
        self.manager.begin_scan(scan_settings(), 0)
        self.manager.record_point(point())
        self.manager.finish_scan(100)
        self.assertEqual(self.manager.upload_stage, 0)
        while self.manager.state == 'preparing':
            self.manager.tick(101)
        self.assertEqual(self.manager.upload_stage, 1)
        self.assertEqual(self.manager.upload_total, self.manager.scan.wire_size)
        self.transport.upload_stage, self.transport.payload_sent = 1, 0
        self.manager.tick(102, online=True)
        self.transport.upload_stage = 2
        self.transport.payload_sent = self.manager.upload_total // 2
        self.manager.tick(103, online=True)
        self.assertEqual(self.manager.upload_stage, 2)
        self.assertEqual(self.manager.upload_sent, self.manager.upload_total // 2)
        self.transport.upload_stage = 3
        self.transport.payload_sent = self.manager.upload_total
        self.manager.tick(104, online=True)
        self.assertEqual(self.manager.upload_stage, 3)
        self.assertEqual(self.manager.state, 'uploading')
        self.assertEqual(self.manager.upload_sent, self.manager.upload_total)
        self.transport.ack = dict(self.transport.expected)
        self.manager.tick(105, online=True)
        self.assertEqual(self.manager.upload_stage, 4)
        self.assertEqual(self.manager.state, 'complete')

    def test_corrected_scan_passes_compact_metadata_without_baseline_file_reads(self):
        channel = {'min_bus_uv': 0, 'max_bus_uv': 17000000, 'zero_duty_u16': 0,
                   'zero_residual_uv': 0, 'zero_valid': True}
        correction = {'version': 1, 'sha256': 'a' * 64, 'lookup': 'linear_endpoint_hold',
                      'drain': dict(channel), 'gate': dict(channel)}
        with patch('builtins.open', side_effect=AssertionError('scan filesystem access')):
            self.manager.begin_scan(scan_settings(), 0, current_correction=correction)
            self.manager.record_point(point())
            self.manager.finish_scan(100)
            while self.manager.state == 'preparing':
                self.manager.tick(101)
            reader = PayloadReader(self.manager.scan)
            body = bytearray()
            while True:
                chunk = reader.read(256)
                if not chunk:
                    break
                body.extend(chunk)
            reader.close()
        value = json.loads(body)
        self.assertEqual(value['schema_version'], 5)
        self.assertEqual(value['current_correction'], correction)
        self.assertEqual(self.store.writes, [])

    def test_failure_preserves_actual_stage_and_sent_count_including_bad_ack(self):
        for stage in (1, 2, 3):
            with self.subTest(stage=stage):
                self.setUp()
                self.prepare()
                self.manager.tick(102, online=True)
                self.transport.upload_stage = stage
                sent = self.manager.upload_total if stage == 3 else self.manager.upload_total // 2
                self.transport.payload_sent = sent
                if stage == 3:
                    self.transport.ack = dict(self.transport.expected, point_count=0)
                else:
                    self.transport.fault = True
                self.manager.tick(103, online=True)
                self.assertEqual(self.manager.state, 'error')
                self.assertEqual(self.manager.upload_stage, stage)
                self.assertEqual(self.manager.upload_sent, sent)
                self.assertEqual(len(self.buffer), 1)

    def test_full_cycle_is_flash_free_and_requires_exact_ack_before_success(self):
        with patch('builtins.open', side_effect=AssertionError('filesystem used')):
            self.prepare()
            self.assertEqual(self.transport.starts, 0)
            self.manager.tick(102, online=True)
            self.assertEqual(self.manager.state, 'uploading')
            self.manager.tick(103, online=True)
            self.assertEqual(self.manager.state, 'uploading')
            self.transport.ack = self.transport.expected
            self.manager.tick(104, online=True)
            self.assertEqual(self.manager.state, 'complete')
            self.assertEqual(len(self.buffer), 1)
            self.manager.discard_scan()
            self.assertEqual(len(self.buffer), 0)
        self.assertEqual(self.store.writes, [])

    def test_each_mismatched_or_forged_ack_fails_without_retry_or_local_loss(self):
        for field, value in (('ok', 1), ('scan_id', 'face-other'), ('sha256', '0' * 64),
                             ('point_count', 0), ('point_count', True), ('sheet_id', True),
                             ('sheet_id', -1)):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.prepare()
                self.manager.tick(102, online=True)
                self.transport.ack = dict(self.transport.expected)
                self.transport.ack[field] = value
                self.manager.tick(103, online=True)
                self.assertEqual(self.manager.state, 'error')
                for now in (200, 100000, 10000000):
                    self.manager.tick(now, online=True)
                self.assertEqual(self.transport.starts, 1)
                self.assertEqual(len(self.buffer), 1)
                self.assertEqual(self.manager.status['message'], UPLOAD_ERROR)
                self.assertEqual(self.store.writes, [])

    def test_disconnect_and_transport_errors_keep_only_ram_until_discard(self):
        for disconnected in (True, False):
            self.setUp()
            self.prepare()
            self.manager.tick(102, online=True)
            self.transport.fault = not disconnected
            self.manager.tick(103, online=not disconnected)
            self.assertEqual(self.manager.state, 'error')
            self.assertNotIn(URL, repr(self.manager.status))
            self.assertEqual(len(self.buffer), 1)
            self.manager.discard_scan()
            self.assertFalse(self.buffer.frozen)
            self.assertEqual(len(self.buffer), 0)
            self.assertEqual(self.store.writes, [])

    def test_closing_a_payload_reader_cannot_discard_owner_data(self):
        self.prepare()
        scan = self.manager.scan
        reader = PayloadReader(scan)
        reader.read(23)
        reader.close()
        self.assertEqual(scan.point_count, 1)
        retry = PayloadReader(scan)
        body = bytearray()
        while True:
            chunk = retry.read(256)
            if not chunk:
                break
            body.extend(chunk)
        self.assertEqual(json.loads(body)['schema_version'], 4)
        self.assertEqual(len(self.buffer), 1)

    def test_cancel_after_seal_discards_without_any_upload(self):
        self.prepare()
        self.manager.discard_scan()
        self.assertEqual(self.transport.starts, 0)
        self.assertIsNone(self.manager.scan)
        with self.assertRaisesRegex(RuntimeError, 'Restart'):
            self.manager.begin_scan(scan_settings(), 200)

    def test_only_explicit_config_edit_writes_a_file(self):
        self.manager.save_config(URL, True)
        self.assertEqual(len(self.store.writes), 1)
        self.prepare()
        with self.assertRaises(RuntimeError):
            self.manager.save_config(URL, True)
        self.assertEqual(len(self.store.writes), 1)

    def test_missing_receiver_stops_preparation_before_any_points(self):
        self.manager._url = ''
        with self.assertRaisesRegex(ValueError, 'Settings'):
            self.manager.begin_scan(scan_settings(), 0)
        self.assertEqual(len(self.buffer), 0)
        self.assertEqual(self.transport.starts, 0)

    def test_manager_has_no_calibration_cache_or_methods(self):
        self.assertFalse(hasattr(self.manager, "prepare_calibration"))
        self.assertFalse(hasattr(self.manager, "invalidate_calibration"))
        self.manager.begin_scan(scan_settings(), 0)
        self.assertEqual("recording", self.manager.state)
