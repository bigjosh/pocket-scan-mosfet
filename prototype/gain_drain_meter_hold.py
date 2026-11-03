"""RAM-only, fixed 5% drain PWM hold with a ten-minute automatic shutdown."""
if globals().get('_confirm_open_gain_hold') is not True:
    raise RuntimeError('Open fixture confirmation required')
del _confirm_open_gain_hold
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


def checked(drain_max):
    pair = instrument.read_pair()
    for role, resistance, limit in (
            ('drain', model.DRAIN_SHUNT_OHM, model.DRAIN_CURRENT_LIMIT_A),
            ('gate', model.GATE_SHUNT_OHM, model.GATE_CURRENT_LIMIT_A)):
        row = pair[role]
        assert abs(row['shunt_v'] / resistance) <= limit, role + ' raw current over limit'
        maximum = drain_max if role == 'drain' else 0.05
        assert -0.02 <= row['terminal_v'] <= maximum, role + ' outside meter-hold envelope'
    return pair


try:
    instrument = Instrument()
    time.sleep_ms(500)
    checked(0.05)
    instrument.set_duty('drain', 0.05)
    started = time.ticks_ms()
    time.sleep_ms(500)
    count = 0
    sums = [0, 0, 0, 0]
    last_report = started
    first_report = True
    while time.ticks_diff(time.ticks_ms(), started) < 600000:
        pair = checked(1.6)
        for offset, role in enumerate(('drain', 'gate')):
            sums[offset * 2] += pair[role]['bus_code']
            sums[offset * 2 + 1] += pair[role]['shunt_code']
        count += 1
        now = time.ticks_ms()
        if (first_report and count >= 100) or time.ticks_diff(now, last_report) >= 10000:
            readings = {}
            for offset, role in enumerate(('drain', 'gate')):
                bus_v = sums[offset * 2] / count * 0.00125
                shunt_v = sums[offset * 2 + 1] / count * 0.0000025
                readings[role] = {'bus_v': bus_v, 'shunt_v': shunt_v,
                                  'terminal_v': bus_v - shunt_v}
            print('DRAIN_HOLD', json.dumps({'duty': 0.05, 'gate_duty': 0,
                  'elapsed_ms': time.ticks_diff(now, started),
                  'automatic_timeout_ms': 600000, 'samples': count, 'readings': readings}))
            first_report = False
            last_report = now
            count, sums = 0, [0, 0, 0, 0]
        time.sleep_ms(4)
    print('HOLD_AUTOMATIC_TIMEOUT')
except KeyboardInterrupt:
    print('HOLD_CANCELLED')
finally:
    if instrument is not None:
        instrument.close()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print('OUTPUTS_DISABLED')
