"""Native integer geometry regression. Real output pins stay low throughout."""
from machine import Pin
import gc
import json
import model
import storage
from engine import Engine

Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)


class NoDrive:
    identity = {}

    def __init__(self):
        self.duties = {'drain': 0.0, 'gate': 0.0}

    def set_duty(self, role, fraction):
        self.duties[role] = fraction  # Simulated only, never PWM.

    def zero(self):
        self.duties = {'drain': 0.0, 'gate': 0.0}

    def read_pair(self):
        return {role: {'terminal_v': duty * model.PWM_FULL_SCALE_V,
                       'bus_v': duty * model.PWM_FULL_SCALE_V, 'shunt_v': 0.0}
                for role, duty in self.duties.items()}


for value in (-36000000, -20000001, -1, 0, 1, 2000001, 20000001, 72000000):
    assert model.parse_voltage_uv(model.format_voltage_uv(value)) == value
assert len(model.sweep_uv(0, 1300000, 100000)) == 14
assert model.sweep_uv(0, 1300000, 100000)[-1] == 1300000
assert list(model.sweep_uv(20000000, 20000003, 1)) == [20000000, 20000001, 20000002, 20000003]
settings = dict(model.DEFAULT_SETTINGS)
settings.update(drain_stop_uv=1550000, drain_step_uv=25000,
                gate_stop_uv=1625000, gate_step_uv=25000)
try:
    model.validate_settings(settings)
except ValueError:
    pass
else:
    raise AssertionError('4158-point scan accepted')
settings.update(gate_stop_uv=1575000)
assert model.scan_point_count(settings) == 4032
settings.update(drain_stop_uv=630000, drain_step_uv=10000,
                gate_stop_uv=630000, gate_step_uv=10000)
assert model.scan_point_count(settings) == 4096
print('MICROVOLT_GEOMETRY_PASS', 'exact endpoints, high-rail 1uV, 4032/4096/4158 limits')

saved = storage.load_settings()
assert saved['version'] == 2
assert all(isinstance(saved[field + '_uv'], int) for field in model.VOLTAGE_FIELDS)
assert storage.load_json('settings.json') == saved
print('MICROVOLT_SETTINGS_PASS', json.dumps(saved))

settings = dict(model.DEFAULT_SETTINGS)
settings.update(drain_stop_uv=1300000, drain_step_uv=100000,
                gate_start_uv=0, gate_stop_uv=0, source_voltage_uv=1000000,
                batches=2, samples_per_batch=1)
calibration = {'version': 1, 'metadata': {'driver_supply_v': 5.0, 'pwm_hz': model.PWM_HZ},
               'channels': {role: {'shunt_ohm': resistance,
                   'points': [{'terminal_v': voltage, 'shunt_v': 0.0,
                               'duty': voltage / model.PWM_FULL_SCALE_V}
                              for voltage in (0.0, 1.5, 3.0)]}
                 for role, resistance in (('drain', model.DRAIN_SHUNT_OHM),
                                           ('gate', model.GATE_SHUNT_OHM))}}
engine = Engine(NoDrive())
engine.start_scan(settings, calibration)
count, final = 0, None
for now in range(0, 30000, 4):
    for kind, data in engine.tick(now):
        if kind == 'point':
            count += 1
            final = data
    if engine.state not in ('scanning', 'calibrating'):
        break
assert engine.state == 'complete', engine.error
assert count == 14
assert abs(final['requested_vds'] - 1.3) < 0.000001
assert abs(final['vds'] - 1.3) < 0.00001
assert engine._drains[-1] == 1300000
print('MICROVOLT_ENGINE_PASS', count, final['requested_vds'], final['vds'])

settings['driver_supply_uv'] = 20000001
engine.start_calibration(settings)
assert engine.calibration['metadata']['driver_supply_uv'] == 20000001
calibration['metadata'].update(engine.calibration['metadata'])
model.validate_calibration(calibration, settings)
settings['driver_supply_uv'] = 20000000
try:
    model.validate_calibration(calibration, settings)
except ValueError:
    pass
else:
    raise AssertionError('Changed rail accepted')
engine.cancel()
print('MICROVOLT_CALIBRATION_IDENTITY_PASS')
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
print('MICROVOLT_NATIVE_DONE', 'outputs low; no physical drive or upload')
