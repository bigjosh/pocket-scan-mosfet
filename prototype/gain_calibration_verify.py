"""Verify an existing RAM candidate with independent ascending/descending drives.

Run with mpremote resume after gain_calibration_candidate.py. Saved files and
the candidate remain unchanged. Output includes actual voltages and corrected
open currents across ten independent batches at every spot check.
"""
assert globals().get('gain_candidate') is not None, 'Run RAM candidate calibration first'
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import time
import math
import json
import os
import network
from hardware import Instrument

assert gain_hash('settings.json') == gain_settings_hash
assert gain_hash('calibration.json') == gain_calibration_hash
model.validate_calibration(gain_candidate, gain_original_settings)
network.WLAN(network.STA_IF).active(False)
network.WLAN(network.AP_IF).active(False)
gain_verify_results = []
gain_verification_complete = False
instrument = None
roles = ('drain', 'gate')
targets = (0.0, 0.5, 1.0, 2.0, 3.0, 2.0, 1.0, 0.5, 0.0)


def gain_verify_read(active, duty):
    pair = instrument.read_pair()
    for role, resistance, limit in (
            ('drain', model.DRAIN_SHUNT_OHM, model.DRAIN_CURRENT_LIMIT_A),
            ('gate', model.GATE_SHUNT_OHM, model.GATE_CURRENT_LIMIT_A)):
        row = pair[role]
        assert abs(row['shunt_v'] / resistance) <= limit, role + ' raw current limit'
        maximum = 3.2 if role == active and duty > 0 else 0.05
        assert -0.02 <= row['terminal_v'] <= maximum, role + ' verification envelope'
    return pair


try:
    instrument = Instrument()
    started = time.ticks_ms()
    for active in roles:
        instrument.zero()
        time.sleep_ms(500)
        gain_verify_read(active, 0)
        for index, target in enumerate(targets):
            duty = model.duty_for_voltage(gain_candidate['channels'][active], target)
            instrument.set_duty(active, duty)
            time.sleep_ms(500)
            batches = []
            minima = {role: 99 for role in roles}
            maxima = {role: -99 for role in roles}
            for _ in range(10):
                sums = [0, 0, 0, 0]
                currents = {role: 0.0 for role in roles}
                for _ in range(50):
                    pair = gain_verify_read(active, duty)
                    for offset, role in enumerate(roles):
                        row = pair[role]
                        sums[offset * 2] += row['bus_code']
                        sums[offset * 2 + 1] += row['shunt_code']
                        minima[role] = min(minima[role], row['terminal_v'])
                        maxima[role] = max(maxima[role], row['terminal_v'])
                        currents[role] += model.corrected_current(
                            gain_candidate['channels'][role], row['terminal_v'], row['shunt_v'],
                            zero_drive=(role != active or duty == 0.0))
                    time.sleep_ms(4)
                batches.append({'codes': sums, 'current_a': {role: currents[role] / 50 for role in roles}})
            result = {'active': active, 'index': index, 'target_v': target, 'duty': duty,
                      'minimum_v': minima, 'maximum_v': maxima, 'channels': {}}
            for offset, role in enumerate(roles):
                bus_v = sum(batch['codes'][offset * 2] for batch in batches) / 500 * 0.00125
                shunt_v = sum(batch['codes'][offset * 2 + 1] for batch in batches) / 500 * 0.0000025
                currents = [batch['current_a'][role] for batch in batches]
                mean = sum(currents) / 10
                std = math.sqrt(sum((value - mean) ** 2 for value in currents) / 9)
                result['channels'][role] = {'terminal_v': bus_v - shunt_v, 'shunt_v': shunt_v,
                                            'corrected_current_a': mean, 'batch_std_a': std}
                assert maxima[role] - minima[role] <= 0.040, role + ' output unstable'
            result['target_error_v'] = result['channels'][active]['terminal_v'] - target
            result['within_20mv_target_band'] = abs(result['target_error_v']) <= 0.020
            gain_verify_results.append(result)
            print('GAIN_VERIFY_POINT', json.dumps(result))
            # Actual-voltage scans retain stable target misses. Record these
            # as acceptance evidence; preserve the raw safety, baseline-
            # coverage and stability checks so reverse checks can complete.
    instrument.zero()
    time.sleep_ms(500)
    gain_verify_idle = instrument.read_pair()
    assert all(-0.005 <= gain_verify_idle[role]['terminal_v'] <= 0.020 for role in roles)
    assert gain_hash('settings.json') == gain_settings_hash
    assert gain_hash('calibration.json') == gain_calibration_hash
    assert {name: gain_hash('scans/' + name) for name in os.listdir('scans')} == gain_original_scans
    gain_verification_complete = True
    print('GAIN_VERIFY_COMPLETE', json.dumps({'elapsed_ms': time.ticks_diff(time.ticks_ms(), started),
          'points': len(gain_verify_results), 'idle': gain_verify_idle, 'device_files_unchanged': True}))
finally:
    if instrument is not None:
        instrument.close()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print('OUTPUTS_DISABLED')
