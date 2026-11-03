"""Build a new open-output calibration in RAM; never overwrite device files.

Requires the current open-fixture confirmation. The production Engine drives,
checks, averages and discovers each ceiling; saved scan settings remain intact.
Exported JSON records reconstruct a host-reviewable candidate. The same object
remains in gain_candidate for subsequent `mpremote resume` verification.
"""
if globals().get('_confirm_open_gain_calibration') is not True:
    raise RuntimeError('Confirm open outputs, no probes and unbridged shunts')
del _confirm_open_gain_calibration
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import os
import time
import json
import hashlib
import binascii
import network
import storage
import model
from hardware import Instrument
from engine import Engine


def gain_hash(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(256)
            if not block:
                break
            sha.update(block)
    return binascii.hexlify(sha.digest()).decode()


gain_original_settings = storage.load_settings()
assert gain_original_settings['driver_supply_uv'] == 5000000
assert gain_original_settings['source_voltage_uv'] == 0
gain_settings_hash = gain_hash('settings.json')
gain_calibration_hash = gain_hash('calibration.json')
gain_original_scans = {name: gain_hash('scans/' + name) for name in os.listdir('scans')}
gain_old_calibration = storage.load_json('calibration.json')
gain_settings = dict(gain_original_settings)
gain_settings.update(batches=10, samples_per_batch=50)
model.validate_settings(gain_settings)
network.WLAN(network.STA_IF).active(False)
network.WLAN(network.AP_IF).active(False)
gain_candidate = None
instrument = None
try:
    instrument = Instrument()
    engine = Engine(instrument)
    engine.start_calibration(gain_settings)
    started = last_report = time.ticks_ms()
    print('GAIN_CAL_BEGIN', json.dumps({'settings_sha256': gain_settings_hash,
          'prior_calibration_sha256': gain_calibration_hash, 'batches': 10,
          'samples_per_batch': 50, 'device_file_writes': False,
          'identity': instrument.identity}))
    while engine.state == 'calibrating':
        engine.tick(time.ticks_ms())
        now = time.ticks_ms()
        if time.ticks_diff(now, started) > 600000:
            engine.cancel()
            raise RuntimeError('Calibration exceeded ten-minute bound')
        if time.ticks_diff(now, last_report) >= 10000:
            print('GAIN_CAL_PROGRESS', json.dumps({'percent': int(engine.progress * 100),
                  'elapsed_ms': time.ticks_diff(now, started), 'index': engine._index}))
            last_report = now
        time.sleep_ms(1)
    assert engine.state == 'complete', engine.error or engine.state
    candidate = engine.calibration
    # Preserve the historical scale calculation exactly. Its gate-preserved
    # annotation describes that earlier scale experiment, not this new map.
    if 'drain_shunt_calibration' in gain_old_calibration['metadata']:
        candidate['metadata']['drain_shunt_calibration'] = gain_old_calibration['metadata']['drain_shunt_calibration']
    candidate['metadata']['drive_baseline_calibration'] = {
        'date': '2026-11-02', 'method': 'Production Engine open-output sweep after feedback layout change',
        'fixture': 'Both outputs open, shunts unbridged, DUT and probes removed',
        'both_channel_drive_maps_and_baselines_replaced': True,
        'shunt_scales_unchanged': True, 'prior_calibration_sha256': gain_calibration_hash,
        'feedback_resistors_nominal_ohm': {'drain': 68000, 'gate': 68000},
        'ground_resistors_reported_ohm': {'drain': 10070, 'gate': 10010},
        'historical_shunt_metadata_retained': True,
    }
    model.validate_calibration(candidate, gain_settings)
    assert gain_hash('settings.json') == gain_settings_hash
    assert gain_hash('calibration.json') == gain_calibration_hash
    assert {name: gain_hash('scans/' + name) for name in os.listdir('scans')} == gain_original_scans
    instrument.zero()
    time.sleep_ms(500)
    idle = instrument.read_pair()
    assert all(-0.005 <= idle[role]['terminal_v'] <= 0.020 for role in ('drain', 'gate')), 'Zero return outside normal tolerance'
    gain_candidate = candidate
    print('GAIN_CAL_METADATA', json.dumps(candidate['metadata']))
    for role in ('drain', 'gate'):
        channel = candidate['channels'][role]
        print('GAIN_CAL_CHANNEL', json.dumps({'role': role, 'shunt_ohm': channel['shunt_ohm'],
              'capability': channel['capability'], 'point_count': len(channel['points'])}))
        for index, point in enumerate(channel['points']):
            print('GAIN_CAL_POINT', json.dumps({'role': role, 'index': index, 'point': point}))
    print('GAIN_CAL_CANDIDATE_READY', json.dumps({'elapsed_ms': time.ticks_diff(time.ticks_ms(), started),
          'idle': idle, 'settings_unchanged': True, 'installed_calibration_unchanged': True,
          'scan_files_unchanged': True, 'device_file_writes': False}))
finally:
    if instrument is not None:
        instrument.close()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print('OUTPUTS_DISABLED')
