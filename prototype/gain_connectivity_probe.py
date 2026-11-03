"""Independent low-duty response check after an explicitly confirmed open fixture.

Runs in RAM; does not modify calibration, settings or saved scans. Each channel
is swept up/down while the other stays at zero. The test is deliberately below
the normal output envelope and retains the production raw-current limits.
"""
if globals().get('_confirm_open_gain_probe') is not True:
    raise RuntimeError('Confirm both outputs open, probes removed and shunts unbridged')
del _confirm_open_gain_probe

from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import json
import time
import network
import storage
import model
from hardware import Instrument

settings = storage.load_settings()
assert settings['driver_supply_uv'] == 5000000 and settings['source_voltage_uv'] == 0
network.WLAN(network.STA_IF).active(False)
network.WLAN(network.AP_IF).active(False)
instrument = None
roles = ('drain', 'gate')
duties = (0.0, 0.002, 0.005, 0.01, 0.02, 0.01, 0.005, 0.002, 0.0)


def read_checked(active, duty):
    pair = instrument.read_pair()
    for role, resistance, limit in (
            ('drain', model.DRAIN_SHUNT_OHM, model.DRAIN_CURRENT_LIMIT_A),
            ('gate', model.GATE_SHUNT_OHM, model.GATE_CURRENT_LIMIT_A)):
        row = pair[role]
        assert abs(row['shunt_v'] / resistance) <= limit, role + ' raw current over limit'
        maximum = 1.5 if role == active and duty > 0 else 0.05
        assert -0.02 <= row['terminal_v'] <= maximum, role + ' outside connectivity envelope'
    return pair


try:
    instrument = Instrument()
    print('GAIN_PROBE_BEGIN', json.dumps({'identity': instrument.identity,
          'batches': 4, 'samples_per_batch': 25, 'settle_ms': 500,
          'sample_pause_ms': 4, 'maximum_duty': 0.02}))
    for active in roles:
        instrument.zero()
        time.sleep_ms(500)
        read_checked(active, 0)
        for index, duty in enumerate(duties):
            instrument.set_duty(active, duty)
            time.sleep_ms(500)
            batches = []
            minimum = {role: 99 for role in roles}
            maximum = {role: -99 for role in roles}
            for _ in range(4):
                sums = [0, 0, 0, 0]
                for _ in range(25):
                    pair = read_checked(active, duty)
                    for offset, role in enumerate(roles):
                        row = pair[role]
                        sums[2 * offset] += row['bus_code']
                        sums[2 * offset + 1] += row['shunt_code']
                        minimum[role] = min(minimum[role], row['terminal_v'])
                        maximum[role] = max(maximum[role], row['terminal_v'])
                    time.sleep_ms(4)
                batches.append(sums)
            print('GAIN_PROBE_POINT', json.dumps({'active': active, 'index': index,
                  'duty': duty, 'columns': ['drain_bus_sum', 'drain_shunt_sum',
                                          'gate_bus_sum', 'gate_shunt_sum'],
                  'batches': batches, 'minimum_v': minimum, 'maximum_v': maximum}))
    print('GAIN_PROBE_COMPLETE')
finally:
    if instrument is not None:
        instrument.close()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print('OUTPUTS_DISABLED')
