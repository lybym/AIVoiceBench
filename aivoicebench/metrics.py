"""Compute expanded latency metrics from an auto-generated EventTimeline.

This module extracts metric values from timeline events produced by the
fusion pipeline (#24). It works with a bare EventTimeline — no scripted
TestCase is required, unlike the deterministic engine (#8).

Metrics computed (deterministic):
- PRD-M002 first_speech_latency_ms: tester_speech_end → device_speech_start
- PRD-M004 turn_gap_ms: tester_speech_end → device_speech_start (same turn)
  Policy: device_speech_start (deterministic). Future: meaningful_response_start.
  Allows negative values (overlap). Policy version recorded.
- PRD-M009 overlap_duration_ms / overlap_ratio
- PRD-M005 barge_in_stop_latency_ms: interrupt_start → old device_speech_end
  Must reference old response_id.
- PRD-M008 false_endpoint: candidate status (not confirmed)

Metrics marked insufficient_evidence (need LLM/audio analysis):
- PRD-M001 feedback_latency_ms
- PRD-M003 meaningful_response_latency_ms
- PRD-M006 barge_in_new_intent_latency_ms (needs semantic intent association)
- PRD-M007 barge_in_success (needs all 4 components + observation window)
"""

from . import formulas

METRIC_POLICY_VERSION = '1.0.0'


def _group_events_by_turn(events):
    turns = {}
    for event in events:
        turn_id = event.get('turn_id')
        if turn_id is not None:
            turns.setdefault(turn_id, []).append(event)
    for turn_id in turns:
        turns[turn_id].sort(key=lambda e: e['start_ms'])
    return turns


def _find_event(events, event_type, turn_events=None):
    source = turn_events if turn_events is not None else events
    for e in source:
        if e.get('type') == event_type:
            return e
    return None


def _find_all(events, event_type, turn_events=None):
    source = turn_events if turn_events is not None else events
    return [e for e in source if e.get('type') == event_type]


def compute_timeline_metrics(timeline):
    """Extract metric values from an EventTimeline 2.0.0.

    Returns dict with: status, metrics (list of metric dicts).
    Each metric has: name, prd_ref, value, unit, status, turn_id,
    evidence_ids, event_ids, policy_version, reason.
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

        # PRD-M002: First Speech Latency: tester_speech_end → device_speech_start
        tester_end = _find_event(events, 'tester_speech_end', turn_events)
        device_start = _find_event(events, 'device_speech_start', turn_events)
        if tester_end and device_start:
            try:
                value = formulas.latency_ms(tester_end['start_ms'], device_start['start_ms'])
                metrics.append(_metric('first_speech_latency_ms', 'PRD-M002', value, 'ms',
                                       'observed', turn_id, tester_end, device_start,
                                       policy='tester_end_to_device_start'))
            except ValueError as e:
                metrics.append(_metric('first_speech_latency_ms', 'PRD-M002', None, 'ms',
                                       'insufficient_evidence', turn_id, tester_end, device_start,
                                       reason=str(e)))

        # PRD-M004: Turn Gap: tester_speech_end → device_speech_start (SAME turn)
        # Allows negative values (overlap). Policy: device_speech_start.
        if tester_end and device_start:
            try:
                value = formulas.turn_gap_ms(tester_end['start_ms'], device_start['start_ms'])
                metrics.append(_metric('turn_gap_ms', 'PRD-M004', value, 'ms',
                                       'observed', turn_id, tester_end, device_start,
                                       policy='device_speech_start',
                                       reason=None if value >= 0 else 'Negative: device started before tester finished (overlap)'))
            except ValueError as e:
                metrics.append(_metric('turn_gap_ms', 'PRD-M004', None, 'ms',
                                       'insufficient_evidence', turn_id, tester_end, device_start,
                                       reason=str(e)))

        # PRD-M009: Overlap duration and ratio
        overlap_starts = _find_all(events, 'overlap_start', turn_events)
        overlap_ends = _find_all(events, 'overlap_end', turn_events)
        device_end = _find_event(events, 'device_speech_end', turn_events)
        if overlap_starts and overlap_ends and device_start and device_end:
            for os_, oe_ in zip(overlap_starts, overlap_ends):
                try:
                    dur = formulas.overlap_duration_ms(os_['start_ms'], oe_['start_ms'])
                    metrics.append(_metric('overlap_duration_ms', 'PRD-M009', dur, 'ms',
                                           'observed', turn_id, os_, oe_))
                    device_dur = device_end['start_ms'] - device_start['start_ms']
                    if device_dur > 0:
                        ratio = formulas.overlap_ratio(dur, device_dur)
                        metrics.append(_metric('overlap_ratio', 'PRD-M009', round(ratio, 4), 'ratio',
                                               'observed', turn_id, os_, oe_))
                except ValueError as e:
                    metrics.append(_metric('overlap_duration_ms', 'PRD-M009', None, 'ms',
                                           'insufficient_evidence', turn_id, os_, oe_,
                                           reason=str(e)))

        # PRD-M005: Barge-in Stop Latency: interrupt_start → old device_speech_end
        # Must reference old response_id
        interrupt_starts = _find_all(events, 'interrupt_start', turn_events)
        if interrupt_starts and device_end:
            for is_ in interrupt_starts:
                # The device_speech_end must be from the SAME turn (old response)
                metrics.append(_metric('barge_in_stop_latency_ms', 'PRD-M005',
                                       device_end['start_ms'] - is_['start_ms'], 'ms',
                                       'observed', turn_id, is_, device_end,
                                       policy='interrupt_start_to_old_response_end'))

    # PRD-M008: False Endpoint — candidate only, not confirmed
    fe_events = _find_all(events, 'possible_false_endpoint')
    for fe in fe_events:
        metrics.append(_metric('false_endpoint_candidate', 'PRD-M008', True, 'boolean',
                               'observed', fe.get('turn_id'), fe, fe,
                               policy='candidate_only',
                               reason='possible_false_endpoint event detected; '
                                      'confirmation requires semantic evidence that user had not finished'))

    # PRD-M001: Feedback Latency — insufficient_evidence (needs LLM/audio)
    fb_status = formulas.feedback_latency_status()
    metrics.append(_metric('feedback_latency_ms', 'PRD-M001', None, 'ms',
                           'insufficient_evidence', None,
                           reason=fb_status['reason']))

    # PRD-M003: Meaningful Response Latency — insufficient_evidence (needs LLM)
    mr_status = formulas.meaningful_response_latency_status()
    metrics.append(_metric('meaningful_response_latency_ms', 'PRD-M003', None, 'ms',
                           'insufficient_evidence', None,
                           reason=mr_status['reason']))

    # PRD-M006: Barge-in New Intent Latency — insufficient_evidence (needs semantic)
    metrics.append(_metric('barge_in_new_intent_latency_ms', 'PRD-M006', None, 'ms',
                           'insufficient_evidence', None,
                           reason='New intent association requires semantic evidence; '
                                  'cannot use arbitrary next device speech'))

    # PRD-M007: Barge-in Success — insufficient_evidence (needs all 4 components)
    metrics.append(_metric('barge_in_success', 'PRD-M007', None, 'boolean',
                           'insufficient_evidence', None,
                           reason='Barge-in success requires: old response stopped, '
                                  'new input accepted, new intent answered, '
                                  'no return to old answer within observation window. '
                                  'Missing semantic evidence.'))

    observed = [m for m in metrics if m['status'] == 'observed']
    status = 'observed' if observed else 'insufficient_evidence'
    return {'status': status, 'metrics': metrics,
            'total_count': len(metrics),
            'observed_count': len(observed),
            'insufficient_count': len(metrics) - len(observed)}


def _metric(name, prd_ref, value, unit, status, turn_id, *evidence_events,
            reason=None, policy=None):
    evidence_ids = []
    event_ids = []
    for e in evidence_events:
        if e and isinstance(e, dict):
            evidence_ids.extend(e.get('evidence_ids', []))
            if 'event_id' in e:
                event_ids.append(e['event_id'])
    evidence_ids = list(dict.fromkeys(evidence_ids))
    event_ids = list(dict.fromkeys(event_ids))
    return {
        'name': name,
        'prd_ref': prd_ref,
        'value': value,
        'unit': unit,
        'status': status,
        'turn_id': turn_id,
        'evidence_ids': evidence_ids,
        'event_ids': event_ids,
        'policy_version': METRIC_POLICY_VERSION,
        'policy': policy,
        'reason': reason,
    }
