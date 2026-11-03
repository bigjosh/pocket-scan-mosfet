"""Read-only installed calibration verification after MicroPython JSON rounding."""
import gc
import struct

differences = 0
maximum_ulps = 0
maximum_deltas = {}
for role in ('drain', 'gate'):
    saved_channel = installed['channels'][role]
    source_channel = gain_candidate['channels'][role]
    assert saved_channel['shunt_ohm'] == source_channel['shunt_ohm']
    assert saved_channel['capability'] == source_channel['capability']
    assert len(saved_channel['points']) == len(source_channel['points'])
    for index, (saved_point, source_point) in enumerate(zip(saved_channel['points'], source_channel['points'])):
        assert sorted(saved_point) == sorted(source_point)
        for key in saved_point:
            saved, source = saved_point[key], source_point[key]
            if saved != source:
                assert isinstance(saved, float) and isinstance(source, float)
                ulps = abs(struct.unpack('<I', struct.pack('<f', saved))[0] -
                           struct.unpack('<I', struct.pack('<f', source))[0])
                delta = abs(saved - source)
                maximum_ulps = max(maximum_ulps, ulps)
                maximum_deltas[key] = max(maximum_deltas.get(key, 0.0), delta)
                differences += 1
print('FLOAT_ROUNDTRIP_DELTAS', json.dumps({'fields': differences, 'max_float32_ulps': maximum_ulps,
      'max_absolute_delta_by_field': maximum_deltas}))
assert installed['metadata'] == gain_candidate['metadata']
assert gain_hash('settings.json') == gain_settings_hash
assert {name: gain_hash('scans/' + name) for name in os.listdir('scans')} == gain_original_scans
assert model.DRAIN_CURRENT_LIMIT_A == 0.005 and model.GATE_CURRENT_LIMIT_A == 0.0001
# Calibration/verification are complete; discard their unused RAM-only history
# before asking ujson to materialize the serialized representation once.
del gain_old_calibration
del gain_verify_results
del engine
gc.collect()
normalized = json.loads(json.dumps(gain_candidate))
assert installed == normalized
del normalized
gc.collect()
print('GAIN_INSTALL_VERIFIED', json.dumps({'normalized_json_roundtrip_equal': True,
      'settings_unchanged': True, 'scan_files_unchanged': True, 'current_limits_unchanged': True,
      'calibration_sha256': gain_hash('calibration.json'), 'free_heap_bytes': gc.mem_free()}))
