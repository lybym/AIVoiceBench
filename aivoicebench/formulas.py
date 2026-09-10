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


def turn_gap_ms(tester_speech_end_ms, device_speech_start_ms):
    """Turn Gap: tester final speech end → device effective start.

    PRD-M004: direction is tester_end → device_start (NOT device_end → next_tester_start).
    Allows negative values (overlap/barge-in): device_start < tester_end means
    the device started speaking before the tester finished.
    Negative values indicate overlap, not an error.

    Policy version: device_speech_start (deterministic).
    Future: meaningful_response_start policy for semantic effective start.
    Different policies must NOT be silently mixed in aggregation.
    """
    start, end = _finite(tester_speech_end_ms), _finite(device_speech_start_ms)
    if start < 0 or end < 0:
        raise ValueError('Turn gap requires nonnegative boundaries')
    return end - start  # May be negative (overlap)


def turn_gap_ms_legacy(device_speech_end_ms, next_tester_speech_start_ms):
    """Legacy Turn Gap formula: device_end → next_tester_start.

    Kept for backward compatibility. PRD-M004 direction has been corrected
    to tester_end → device_start. This legacy function must not be used
    for new PRD-M004 computation. Old results using this formula must
    declare their policy version explicitly.
    """
    start, end = _finite(device_speech_end_ms), _finite(next_tester_speech_start_ms)
    if start < 0 or end < 0:
        raise ValueError('Turn gap requires nonnegative boundaries')
    if end < start:
        raise ValueError('Legacy turn gap requires ordered boundaries; overlap is not a gap')
    return end - start


def barge_in_stop_latency_ms(interrupt_start_ms, device_speech_end_ms):
    """Time from tester interruption start to device speech stop.

    PRD-M005: Must be associated with the OLD response_id.
    The device_speech_end must belong to the response being interrupted,
    not an arbitrary subsequent device speech.
    """
    start, end = _finite(interrupt_start_ms), _finite(device_speech_end_ms)
    if start < 0 or end < 0:
        raise ValueError('Barge-in stop latency requires nonnegative boundaries')
    # Allow device_end < interrupt_start only if there's evidence the device
    # stopped before the interruption — but typically end >= start
    return end - start


def overlap_duration_ms(overlap_start_ms, overlap_end_ms):
    """Duration of a single overlap interval."""
    start, end = _finite(overlap_start_ms), _finite(overlap_end_ms)
    if start < 0 or end < start:
        raise ValueError('Overlap duration requires valid ordered interval')
    return end - start


def overlap_ratio(overlap_duration_ms_val, device_speech_duration_ms):
    """Fraction of device speech that overlaps with tester speech."""
    overlap, total = _finite(overlap_duration_ms_val), _finite(device_speech_duration_ms)
    if overlap < 0 or total <= 0:
        raise ValueError('Overlap ratio requires nonnegative overlap and positive device duration')
    if overlap > total:
        raise ValueError('Overlap cannot exceed device speech duration')
    return overlap / total


def false_endpoint_detected(events):
    """Boolean: did the timeline contain a possible_false_endpoint event?"""
    if not isinstance(events, (list, tuple)):
        raise ValueError('Events must be a list')
    return any(isinstance(e, dict) and e.get('type') == 'possible_false_endpoint' for e in events)


def feedback_latency_status():
    """Feedback latency requires detecting non-speech feedback (filler, cue, tone).

    Pure signal processing cannot reliably distinguish feedback from noise.
    This metric needs acoustic pattern matching or LLM analysis.
    Returns insufficient_evidence, not a guessed time.
    """
    return {'status': 'insufficient_evidence',
            'reason': 'Feedback detection requires acoustic pattern matching or LLM analysis; '
                       'pure signal timing cannot distinguish filler/cue from noise'}


def meaningful_response_latency_status():
    """Meaningful response latency requires semantic analysis to locate the first
    information-bearing point in the device response.

    This cannot be computed from timing alone; it needs ASR + LLM.
    Returns insufficient_evidence, not a guessed time.
    """
    return {'status': 'insufficient_evidence',
            'reason': 'Meaningful response boundary requires ASR + LLM semantic analysis; '
                       'timing alone cannot identify the first information-bearing point'}
