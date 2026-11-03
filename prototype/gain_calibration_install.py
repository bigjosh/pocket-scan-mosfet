"""Atomically install the reviewed and spot-verified RAM calibration candidate."""
assert globals().get('_confirm_reviewed_gain_install') is True, 'Technical review required before install'
del _confirm_reviewed_gain_install
assert globals().get('gain_verification_complete') is True
assert len(gain_verify_results) == 18
from machine import Pin
from archive import calibration_hash
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
try:
    assert gain_hash('settings.json') == gain_settings_hash
    assert gain_hash('calibration.json') == gain_calibration_hash
    assert {name: gain_hash('scans/' + name) for name in os.listdir('scans')} == gain_original_scans
    model.validate_calibration(gain_candidate, gain_original_settings)
    for role in ('drain', 'gate'):
        assert gain_candidate['channels'][role]['shunt_ohm'] == gain_old_calibration['channels'][role]['shunt_ohm']
    assert gain_candidate['metadata']['drain_shunt_calibration'] == gain_old_calibration['metadata']['drain_shunt_calibration']
    assert model.DRAIN_CURRENT_LIMIT_A == 0.005 and model.GATE_CURRENT_LIMIT_A == 0.0001
    expected_serialized_hash = calibration_hash(gain_candidate)
    storage.save_json_atomic('calibration.json', gain_candidate)
    installed = storage.load_json('calibration.json')
    model.validate_calibration(installed, gain_original_settings)
    # MicroPython's JSON reader may round a float by one ULP. Verify the
    # exact bytes intended by its writer without allocating a whole string.
    assert gain_hash('calibration.json') == expected_serialized_hash
    assert gain_hash('settings.json') == gain_settings_hash
    assert {name: gain_hash('scans/' + name) for name in os.listdir('scans')} == gain_original_scans
    print('GAIN_CAL_INSTALLED', json.dumps({'sha256': gain_hash('calibration.json'),
          'previous_sha256': gain_calibration_hash, 'settings_unchanged': True,
          'scan_files_unchanged': True, 'shunt_scales_and_provenance_preserved': True,
          'drain_current_limit_a': model.DRAIN_CURRENT_LIMIT_A,
          'gate_current_limit_a': model.GATE_CURRENT_LIMIT_A}))
finally:
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print('OUTPUTS_DISABLED')
