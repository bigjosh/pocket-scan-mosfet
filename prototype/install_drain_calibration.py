"""Commit an already-reviewed drain-only candidate; preserve gate and settings.

Run after installing the matching model.py current scale. The candidate is
staged as drain-calibration.candidate.json; EXPECTED_OLD_CALIBRATION_SHA256
must match the saved calibration backed up before these calibration tests.
This helper never drives either output or changes scan settings.
"""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import hashlib
import binascii
import json
import os
import model
import storage


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(512)
            if not block:
                break
            value.update(block)
    return binascii.hexlify(value.digest()).decode()


expected = globals().get('EXPECTED_OLD_CALIBRATION_SHA256')
assert expected and digest('calibration.json') == expected, 'Saved calibration changed during measurement'
settings_digest = digest('settings.json')
old = storage.load_json('calibration.json')
candidate = storage.load_json('drain-calibration.candidate.json')
settings = storage.load_settings()
assert candidate['channels']['gate'] == old['channels']['gate'], 'Gate calibration must be preserved'
assert candidate['metadata']['drain_shunt_calibration']['reference_load_ohm'] == 9980
assert 0.5 < model.DRAIN_SHUNT_OHM < 1.5, 'Unexpected reviewed current scale'
assert model.DRAIN_CURRENT_LIMIT_A == 0.005, 'Do not silently change the current limit'
model.validate_calibration(candidate, settings)
model.validate_scan_calibration(candidate, settings)
storage.save_json_atomic('calibration.json', candidate)
saved = storage.load_json('calibration.json')
model.validate_calibration(saved, settings)
assert saved['channels']['gate'] == old['channels']['gate']
assert digest('settings.json') == settings_digest
os.remove('drain-calibration.candidate.json')
print('DRAIN_CALIBRATION_INSTALLED', json.dumps({
    'drain_shunt_ohm': model.DRAIN_SHUNT_OHM,
    'drain_current_limit_a': model.DRAIN_CURRENT_LIMIT_A,
    'drain_points': len(saved['channels']['drain']['points']),
    'gate_calibration_preserved': True, 'settings_preserved': True,
    'old_calibration_sha256': expected, 'new_calibration_sha256': digest('calibration.json'),
    'both_pwm_pins_low': True}))
