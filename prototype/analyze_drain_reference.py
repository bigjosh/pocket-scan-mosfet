"""Fit an effective drain shunt using loaded and matching open-reference logs."""
import argparse
import json
import math
from pathlib import Path
import statistics


def read_points(path):
    text = path.read_text(encoding='utf-8-sig')
    assert 'REFERENCE_COMPLETE' in text and 'BOTH_PWM_PINS_LOW' in text
    result = []
    for line in text.splitlines():
        if not line.startswith('REFERENCE_POINT '):
            continue
        raw = json.loads(line.split(' ', 1)[1])
        batches = []
        for sums in raw['batches']:
            bus = sums[0] / raw['samples_per_batch'] * 0.00125
            shunt = sums[1] / raw['samples_per_batch'] * 2.5e-6
            batches.append({'terminal_v': bus - shunt, 'shunt_v': shunt})
        result.append({'index': raw['index'], 'target_v': raw['target_v'],
                       'fixture': raw['fixture'], 'batches': batches,
                       'terminal_v': statistics.mean(p['terminal_v'] for p in batches),
                       'shunt_v': statistics.mean(p['shunt_v'] for p in batches),
                       'shunt_mean_se_v': statistics.stdev(p['shunt_v'] for p in batches) / math.sqrt(len(batches))})
    assert len(result) == 13
    return result


def baseline_knots(points):
    grouped = {}
    for point in points:
        grouped.setdefault(point['target_v'], []).append(point)
    return sorted((statistics.mean(p['terminal_v'] for p in group),
                   statistics.mean(p['shunt_v'] for p in group))
                  for group in grouped.values())


def interpolate(knots, voltage):
    # Matched endpoint output varies slightly; clamp at most 10mV, never
    # extrapolate an offset slope beyond the measured reference range.
    if voltage <= knots[0][0]:
        assert knots[0][0] - voltage <= 0.010
        return knots[0][1]
    if voltage >= knots[-1][0]:
        assert voltage - knots[-1][0] <= 0.010
        return knots[-1][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if voltage <= x1:
            return y0 + (voltage - x0) * (y1 - y0) / (x1 - x0)
    raise AssertionError('No baseline bracket')


def fit(rows):
    x = [r['reference_current_a'] for r in rows]
    y = [r['corrected_shunt_v'] for r in rows]
    xm, ym = statistics.mean(x), statistics.mean(y)
    sxx = sum((v - xm) ** 2 for v in x)
    resistance = sum((a - xm) * (b - ym) for a, b in zip(x, y)) / sxx
    intercept = ym - resistance * xm
    residuals = [b - intercept - resistance * a for a, b in zip(x, y)]
    rms = math.sqrt(sum(v * v for v in residuals) / len(rows))
    slope_se = math.sqrt(sum(v * v for v in residuals) / (len(rows) - 2) / sxx)
    return {'points': len(rows), 'effective_shunt_ohm': resistance,
            'intercept_v': intercept, 'residual_rms_v': rms,
            'residual_max_abs_v': max(abs(v) for v in residuals),
            'slope_standard_error_ohm': slope_se,
            'slope_standard_error_excludes_systematic_errors': True}


def analyze(loaded, opened, load_ohm=9980, calibration=None):
    assert all(p['fixture'] == '9980_ohm' for p in loaded)
    assert all(p['fixture'] == 'open' for p in opened)
    knots = ([(p['terminal_v'], p['shunt_v']) for p in calibration['channels']['drain']['points']]
             if calibration is not None else baseline_knots(opened))
    rows = []
    for point in loaded:
        if point['target_v'] < 0.5:
            continue
        offset = interpolate(knots, point['terminal_v'])
        rows.append({'index': point['index'], 'target_v': point['target_v'],
                     'terminal_v': point['terminal_v'],
                     'reference_current_a': point['terminal_v'] / load_ohm,
                     'raw_shunt_v': point['shunt_v'], 'baseline_v': offset,
                     'corrected_shunt_v': point['shunt_v'] - offset,
                     'loaded_shunt_mean_se_v': point['shunt_mean_se_v']})
    result = {'reference_load_ohm': load_ohm, 'fit': fit(rows),
              'ascending_fit': fit([r for r in rows if r['index'] <= 7]),
              'descending_fit': fit([r for r in rows if r['index'] > 7]),
              'open_baseline_knots': knots, 'loaded_points': rows,
              'qualification': 'Effective current scale, not an exact four-wire resistance. Reference/DMM uncertainty, ADC gain and offset drift are not included in regression error.'}
    scale = result['fit']['effective_shunt_ohm']
    errors = [r['corrected_shunt_v'] / scale - r['reference_current_a'] for r in rows]
    result['scale_only_current_error_a'] = {
        'minimum': min(errors), 'maximum': max(errors), 'mean': statistics.mean(errors),
        'fitted_intercept_not_subtracted': True}
    result['baseline_source'] = 'full_open_calibration' if calibration is not None else 'matching_open_sweep'
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('loaded', type=Path)
    parser.add_argument('opened', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--baseline-calibration', type=Path)
    parser.add_argument('--additional-loaded', type=Path)
    args = parser.parse_args()
    loaded = read_points(args.loaded)
    if args.additional_loaded:
        loaded += read_points(args.additional_loaded)
    calibration = (json.loads(args.baseline_calibration.read_text(encoding='utf-8-sig'))
                   if args.baseline_calibration else None)
    result = analyze(loaded, read_points(args.opened), calibration=calibration)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({name: result[name] for name in ('fit', 'ascending_fit', 'descending_fit')}))
