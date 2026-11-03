"""Measure drain reference/open sweeps; never overwrite a calibration.

Run in RAM after confirming the DUT is removed and the gate output is open.
Set DRAIN_REFERENCE_FIXTURE to '9980_ohm' or 'open' for the actual fixture.
Both outputs are zero on exit. All currents must be calculated on the host
from raw measurements, not the obsolete installed drain shunt calibration.
"""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import time
import json
import network
import model
import storage
from hardware import Instrument

fixture = globals().get('DRAIN_REFERENCE_FIXTURE')
assert fixture in ('9980_ohm', 'open'), 'Confirm the actual reference fixture'
settings = storage.load_settings()
assert settings['driver_supply_uv'] == 5000000 and settings['source_voltage_uv'] == 0
calibration = storage.load_json('calibration.json')
channel = calibration['channels']['drain']
targets = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.4, 3.0, 2.0, 1.0, 0.5, 0.0)
batch_count, samples_per_batch = 10, 50
instrument = None


def guarded_read():
    pair = instrument.read_pair()
    drain, gate = pair['drain'], pair['gate']
    # Independent raw limits: no use of the old 7.3-ohm current scale.
    if not -0.02 <= drain['terminal_v'] <= 3.85 or abs(drain['shunt_v']) > 0.002:
        raise RuntimeError('Drain reference fixture outside bounded test envelope')
    if abs(gate['terminal_v']) > 0.050 or abs(gate['shunt_v']) > 0.005:
        raise RuntimeError('Gate output must stay near zero')
    return pair


def mean_voltage(count=16):
    total = 0.0
    for _ in range(count):
        total += guarded_read()['drain']['terminal_v']
        time.sleep_ms(4)
    return total / count


try:
    network.WLAN(network.STA_IF).active(False)
    network.WLAN(network.AP_IF).active(False)
    instrument = Instrument()
    instrument.zero()
    time.sleep_ms(500)
    assert abs(mean_voltage()) < 0.05, 'Drain did not return to zero'
    print('REFERENCE_BEGIN', json.dumps({'fixture': fixture, 'load_ohm': 9980 if fixture == '9980_ohm' else None,
          'batches': batch_count, 'samples_per_batch': samples_per_batch,
          'sample_pause_ms': 4, 'settle_ms': 500, 'identity': instrument.identity,
          'gate_held_zero': True, 'calibration_not_modified': True}))
    for index, target in enumerate(targets):
        duty = model.duty_for_voltage(channel, target)
        for attempt in range(4):
            assert 0.0 <= duty <= model.calibration_duty_limit(settings)
            instrument.set_duty('drain', duty)
            applied_duty = duty
            time.sleep_ms(500)
            actual = mean_voltage()
            if target == 0 or abs(target - actual) < 0.015:
                break
            duty = max(0.0, min(model.calibration_duty_limit(settings),
                               duty + (target - actual) / model.PWM_FULL_SCALE_V))
        assert abs(target - actual) < 0.05, 'Reference setpoint was not reached'
        batches = []
        # Integer sums preserve precision on RP2's float32 MicroPython build.
        for batch in range(batch_count):
            sums = [0, 0, 0, 0]
            minimum, maximum = 32767, -32768
            for sample in range(samples_per_batch):
                pair = guarded_read()
                values = (pair['drain']['bus_code'], pair['drain']['shunt_code'],
                          pair['gate']['bus_code'], pair['gate']['shunt_code'])
                for offset in range(4):
                    sums[offset] += values[offset]
                minimum = min(minimum, values[1])
                maximum = max(maximum, values[1])
                time.sleep_ms(4)
            batches.append(sums + [minimum, maximum])
        print('REFERENCE_POINT', json.dumps({'index': index, 'fixture': fixture,
              'target_v': target, 'duty': applied_duty, 'samples_per_batch': samples_per_batch,
              'columns': ['drain_bus_sum', 'drain_shunt_sum', 'gate_bus_sum', 'gate_shunt_sum',
                          'drain_shunt_min', 'drain_shunt_max'], 'batches': batches}))
    print('REFERENCE_COMPLETE', fixture)
finally:
    try:
        if instrument is not None:
            instrument.close()
    finally:
        Pin(20, Pin.OUT, value=0)
        Pin(21, Pin.OUT, value=0)
        print('BOTH_PWM_PINS_LOW')
