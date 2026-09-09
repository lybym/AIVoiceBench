"""Reference formulas, not an audio event detector or a hardware metric engine."""

import math
import unicodedata


def _finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('Expected finite numeric input')
    return value


def latency_ms(tester_end_ms, response_start_ms):
    start, end = _finite(tester_end_ms), _finite(response_start_ms)
    if start < 0 or end < start:
        raise ValueError('Latency requires nonnegative ordered boundaries; overlap is not latency')
    return end - start


def percentiles(values):
    """R7 linear interpolation; missing observations excluded, denominator retained."""
    samples = sorted(_finite(value) for value in values if value is not None)
    count = len(samples)
    result = {'sample_count': count, 'total_count': len(values), 'excluded_count': len(values) - count,
              'algorithm': 'R7', 'status': 'observed' if count else 'insufficient_evidence'}
    for percentile in (50, 90, 95, 99):
        if not count:
            result[f'P{percentile}'] = None
        else:
            position = (count - 1) * percentile / 100
            lower = math.floor(position)
            upper = math.ceil(position)
            result[f'P{percentile}'] = samples[lower] + (samples[upper] - samples[lower]) * (position - lower)
    return result


def character_error_rate(reference, hypothesis):
    """NFC Unicode code points, case/whitespace/punctuation preserved (nfc-v1)."""
    reference, hypothesis = unicodedata.normalize('NFC', reference), unicodedata.normalize('NFC', hypothesis)
    if not reference:
        return {'value': None, 'edits': None, 'reference_characters': 0, 'status': 'not_applicable', 'normalization': 'nfc-v1'}
    row = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        next_row = [i]
        for j, right in enumerate(hypothesis, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (left != right)))
        row = next_row
    return {'value': row[-1] / len(reference), 'edits': row[-1], 'reference_characters': len(reference),
            'status': 'observed', 'normalization': 'nfc-v1'}


def _union(intervals):
    checked = []
    for start, end in intervals:
        _finite(start)
        _finite(end)
        if start < 0 or end < start:
            raise ValueError('Invalid interval')
        checked.append((start, end))
    merged = []
    for start, end in sorted(checked):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def overlap_ms(tester_intervals, device_intervals):
    """Intersection of interval unions; duplicate/overlapping segments count once."""
    left, right = _union(tester_intervals), _union(device_intervals)
    i = j = 0
    total = 0
    while i < len(left) and j < len(right):
        total += max(0, min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return total


def eligible_rate(observations):
    if any(value is not None and type(value) is not bool for value in observations):
        raise ValueError('Rates accept bool or missing (None) observations')
    eligible = [value for value in observations if value is not None]
    return {'value': sum(eligible) / len(eligible) if eligible else None,
            'sample_count': len(eligible), 'total_count': len(observations),
            'excluded_count': len(observations) - len(eligible),
            'status': 'observed' if eligible else 'insufficient_evidence'}


def barge_success(stop_success, new_response_success):
    if any(value is not None and type(value) is not bool for value in (stop_success, new_response_success)):
        raise ValueError('Barge success requires boolean or missing components')
    if stop_success is None or new_response_success is None:
        return None
    return stop_success and new_response_success
