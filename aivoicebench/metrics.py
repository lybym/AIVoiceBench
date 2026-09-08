"""Compute expanded latency metrics from an auto-generated EventTimeline.

This module extracts metric values from timeline events produced by the
fusion pipeline (#24). It works with a bare EventTimeline — no scripted
TestCase is required, unlike the deterministic engine (#8) which evaluates
against Case assertions.

Metrics computed (deterministic):
- first_speech_latency_ms: tester_speech_end → device_speech_start
- turn_gap_ms: device_speech_end → next tester_speech_start
- overlap_duration_ms: overlap_start → overlap_end
- overlap_ratio: overlap / device_speech_duration
- barge_in_stop_latency_ms: interrupt_start → device_speech_end
- false_endpoint: boolean (possible_false_endpoint event present)

Metrics marked insufficient_evidence (need LLM/audio analysis):
- feedback_latency_ms: needs non-speech feedback detection
- meaningful_response_latency_ms: needs semantic analysis
"""

from . import formulas


def _group_events_by_turn(events):
    """Group events by turn_id, preserving chronological order."""
    turns = {}
    for event in events:
        turn_id = event.get('turn_id')
        if turn_id is not None:
            turns.setdefault(turn_id, []).append(event)
    for turn_id in turns:
        turns[turn_id].sort(key=lambda e: e['start_ms'])
    return turns


def _find_event(events, event_type, turn_events=None):
    """Find the first event of a given type in a turn or globally."""
    source = turn_events if turn_events is not None else events
    for e in source:
        if e.get('type') == event_type:
            return e
    return None


def _find_all(events, event_type, turn_events=None):
    """Find all events of a given type in a turn or globally."""
    source = turn_events if turn_events is not None else events
    return [e for e in source if e.get('type') == event_type]


def compute_timeline_metrics(timeline):
    """Extract metric values from an EventTimeline 2.0.0.

    Returns a list of metric dicts with: name, value, unit, status,
    turn_id, evidence_ids, event_ids, reason.
    """
    events = timeline.get('events', [])
    if not events:
        return {'status': 'insufficient_evidence', 'reason': 'No events in timeline',
                'metrics': []}

    turn_groups = _group_events_by_turn(events)
    turn_ids = sorted(turn_groups.keys())
    metrics = []

    for turn_id in turn_ids:
        turn_events = turn_groups[turn_id]

        # First speech latency: tester_speech_end → device_speech_start
        tester_end = _find_event(events, 'tester_speech_end', turn_events)
        device_start = _find_event(events, 'device_speech_start', turn_events)
        if tester_end and device_start:
            try:
                value = formulas.latency_ms(tester_end['start_ms'], device_start['start_ms'])
                metrics.append(_metric('first_speech_latency_ms', value, 'ms', 'observed',
                                       turn_id, tester_end, device_start))
            except ValueError as e:
                metrics.append(_metric('first_speech_latency_ms', None, 'ms', 'insufficient_evidence',
                                       turn_id, tester_end, device_start, str(e)))

        # Turn gap: device_speech_end → next tester_speech_start
        device_end = _find_event(events, 'device_speech_end', turn_events)
        turn_idx = turn_ids.index(turn_id)
        if device_end and turn_idx + 1 < len(turn_ids):
            next_turn_id = turn_ids[turn_idx + 1]
            next_turn_events = turn_groups[next_turn_id]
            next_tester_start = _find_event(events, 'tester_speech_start', next_turn_events)
            if next_tester_start:
                try:
                    value = formulas.turn_gap_ms(device_end['start_ms'], next_tester_start['start_ms'])
                    metrics.append(_metric('turn_gap_ms', value, 'ms', 'observed',
                                           turn_id, device_end, next_tester_start))
                except ValueError as e:
                    metrics.append(_metric('turn_gap_ms', None, 'ms', 'insufficient_evidence',
                                           turn_id, device_end, next_tester_start, str(e)))

        # Overlap duration and ratio
        overlap_starts = _find_all(events, 'overlap_start', turn_events)
        overlap_ends = _find_all(events, 'overlap_end', turn_events)
        if overlap_starts and overlap_ends and device_start and device_end:
            for i, (os_, oe_) in enumerate(zip(overlap_starts, overlap_ends)):
                try:
                    dur = formulas.overlap_duration_ms(os_['start_ms'], oe_['start_ms'])
                    metrics.append(_metric('overlap_duration_ms', dur, 'ms', 'observed',
                                           turn_id, os_, oe_))
                    device_dur = device_end['start_ms'] - device_start['start_ms']
                    if device_dur > 0:
                        ratio = formulas.overlap_ratio(dur, device_dur)
                        metrics.append(_metric('overlap_ratio', round(ratio, 4), 'ratio', 'observed',
                                               turn_id, os_, oe_))
                except ValueError as e:
                    metrics.append(_metric('overlap_duration_ms', None, 'ms', 'insufficient_evidence',
                                           turn_id, os_, oe_, str(e)))

        # Barge-in stop latency: interrupt_start → device_speech_end
        interrupt_starts = _find_all(events, 'interrupt_start', turn_events)
        if interrupt_starts and device_end:
            for is_ in interrupt_starts:
                try:
                    value = formulas.barge_in_stop_latency_ms(is_['start_ms'], device_end['start_ms'])
                    metrics.append(_metric('barge_in_stop_latency_ms', value, 'ms', 'observed',
                                           turn_id, is_, device_end))
                except ValueError as e:
                    metrics.append(_metric('barge_in_stop_latency_ms', None, 'ms', 'insufficient_evidence',
                                           turn_id, is_, device_end, str(e)))

    # False endpoint: global boolean
    fe_detected = formulas.false_endpoint_detected(events)
    fe_events = _find_all(events, 'possible_false_endpoint')
    if fe_events:
        for fe in fe_events:
            metrics.append(_metric('false_endpoint_detected', fe_detected, 'boolean', 'observed',
                                   fe.get('turn_id'), fe, fe))

    # Feedback latency: insufficient_evidence (needs acoustic/LLM)
    fb_status = formulas.feedback_latency_status()
    metrics.append({'name': 'feedback_latency_ms', 'value': None, 'unit': 'ms',
                    'status': fb_status['status'], 'turn_id': None,
                    'evidence_ids': [], 'event_ids': [], 'reason': fb_status['reason']})

    # Meaningful response latency: insufficient_evidence (needs LLM)
    mr_status = formulas.meaningful_response_latency_status()
    metrics.append({'name': 'meaningful_response_latency_ms', 'value': None, 'unit': 'ms',
                    'status': mr_status['status'], 'turn_id': None,
                    'evidence_ids': [], 'event_ids': [], 'reason': mr_status['reason']})

    observed = [m for m in metrics if m['status'] == 'observed']
    status = 'observed' if observed else 'insufficient_evidence'
    return {'status': status, 'metrics': metrics}


def _metric(name, value, unit, status, turn_id, *evidence_events, reason=None):
    """Build a metric result dict."""
    evidence_ids = []
    event_ids = []
    for e in evidence_events:
        if e and 'evidence_ids' in e:
            evidence_ids.extend(e['evidence_ids'])
        if e and 'event_id' in e:
            event_ids.append(e['event_id'])
    # Deduplicate while preserving order
    evidence_ids = list(dict.fromkeys(evidence_ids))
    event_ids = list(dict.fromkeys(event_ids))
    return {
        'name': name,
        'value': value,
        'unit': unit,
        'status': status,
        'turn_id': turn_id,
        'evidence_ids': evidence_ids,
        'event_ids': event_ids,
        'reason': reason,
    }
