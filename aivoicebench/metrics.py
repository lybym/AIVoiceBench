"""Compute canonical MetricResult 3.0.0 from an auto-generated EventTimeline.

This module is the single deterministic metric producer for imported recordings.
It emits canonical MetricResult documents — not a parallel lightweight format.

PRD-M001..M010 mapping:
- M001 feedback_latency_ms             insufficient_evidence without semantic/audio evidence
- M002 first_speech_latency_ms         tester_speech_end -> device_speech_start
- M003 meaningful_response_latency_ms  insufficient_evidence without ASR+LLM
- M004 turn_gap_ms                     tester_speech_end -> device_speech_start (signed)
- M005 barge_in_stop_latency_ms        interrupt_start -> interrupted old response end
- M006 barge_in_new_intent_latency_ms  insufficient_evidence without semantic intent
- M007 barge_in_success                insufficient_evidence without all components
- M008 false_endpoint_candidate        candidate only, never confirmed
- M009 overlap_duration_ms / overlap_ratio
- M010 timeout / asr_cer / asr_wer / statistics

Status semantics:
- observed              value is measured from valid evidence
- not_applicable        the structure makes the metric meaningless (e.g. orphan device turn)
- insufficient_evidence the metric should be measurable but required evidence is missing
"""

from . import formulas

DEFINITION_VERSION = '3.0.0'
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


def _evidence_ids(*events):
    ids = []
    for e in events:
        if isinstance(e, dict):
            ids.extend(e.get('evidence_ids', []))
    return list(dict.fromkeys(ids))


def _event_ids(*events):
    ids = []
    for e in events:
        if isinstance(e, dict) and 'event_id' in e:
            ids.append(e['event_id'])
    return list(dict.fromkeys(ids))


def _metric(timeline, name, prd_ref, *, value=None, unit='ms', status='observed',
            turn_id=None, response_id=None, events=(), reason=None, policy=None,
            confidence=None, confidence_source=None, uncertainty_ms=None,
            measurement_scope='black_box', method='deterministic'):
    """Build one canonical MetricResult 3.0.0 document."""
    run_id = timeline.get('run_id') or 'RUN-auto'
    case_id = timeline.get('case_id') or None
    execution_kind = timeline.get('execution_kind') or 'imported'
    samples = 1 if status == 'observed' else 0
    return {
        'schema_version': '3.0.0',
        'metric_id': f'{run_id}.{name}' + (f'.{turn_id}' if turn_id else ''),
        'run_id': run_id,
        'case_id': case_id,
        'analysis_id': timeline.get('analysis_id'),
        'prd_ref': prd_ref,
        'turn_id': turn_id,
        'response_id': response_id,
        'name': name,
        'definition_version': DEFINITION_VERSION,
        'policy': policy,
        'policy_version': METRIC_POLICY_VERSION if policy else None,
        'execution_kind': execution_kind,
        'value': value,
        'unit': unit,
        'status': status,
        'reason': reason if reason is not None else (
            'Measured from timeline events' if status == 'observed' else 'Not reported'),
        'measurement_scope': measurement_scope,
        'method': method,
        'confidence': confidence,
        'confidence_source': confidence_source,
        'uncertainty_ms': uncertainty_ms,
        'evidence_ids': _evidence_ids(*events),
        'event_ids': _event_ids(*events),
        'aggregation': {
            'kind': 'single',
            'sample_count': samples,
            'total_count': 1,
            'excluded_count': 1 - samples,
            'algorithm': 'single',
            'input_metric_ids': [],
        },
    }


def compute_timeline_metrics(timeline):
    """Compute canonical metrics from an EventTimeline.

    Returns:
        {
          'status': 'observed' | 'insufficient_evidence',
          'metrics': [MetricResult 3.0.0, ...],
          'counts': {total, observed, not_applicable, insufficient_evidence, pass, fail},
        }
    """
    events = timeline.get('events', [])
    if not events:
        return {'status': 'insufficient_evidence', 'reason': 'No events in timeline',
                'metrics': [], 'counts': _counts([])}

    turn_groups = _group_events_by_turn(events)
    turn_ids = sorted(turn_groups.keys())
    metrics = []

    for turn_id in turn_ids:
        turn_events = turn_groups[turn_id]
        tester_start = _find_event(events, 'tester_speech_start', turn_events)
        tester_end = _find_event(events, 'tester_speech_end', turn_events)
        device_start = _find_event(events, 'device_speech_start', turn_events)
        device_end = _find_event(events, 'device_speech_end', turn_events)
        response_id = None
        if device_start:
            response_id = device_start.get('response_id')
        elif device_end:
            response_id = device_end.get('response_id')

        has_tester = tester_end is not None
        has_device = device_start is not None

        # --- PRD-M002 First Speech Latency: tester_end -> device_start ---
        if has_tester and has_device:
            try:
                value = formulas.latency_ms(tester_end['start_ms'], device_start['start_ms'])
                metrics.append(_metric(timeline, 'first_speech_latency_ms', 'PRD-M002',
                                       value=value, status='observed', turn_id=turn_id,
                                       response_id=response_id,
                                       events=(tester_end, device_start),
                                       policy='tester_end_to_device_start'))
            except ValueError:
                # Device onset before tester end is overlap, not negative latency.
                metrics.append(_metric(timeline, 'first_speech_latency_ms', 'PRD-M002',
                                       status='not_applicable', turn_id=turn_id,
                                       response_id=response_id,
                                       events=(tester_end, device_start),
                                       reason='Device onset precedes tester speech end; overlap is not latency',
                                       policy='tester_end_to_device_start'))
        elif has_tester and not has_device:
            metrics.append(_metric(timeline, 'first_speech_latency_ms', 'PRD-M002',
                                   status='insufficient_evidence', turn_id=turn_id,
                                   events=(tester_end,),
                                   reason='Tester utterance has no associated device onset evidence',
                                   policy='tester_end_to_device_start'))
        else:
            metrics.append(_metric(timeline, 'first_speech_latency_ms', 'PRD-M002',
                                   status='not_applicable', turn_id=turn_id,
                                   reason='Turn has no tester utterance; no response expected',
                                   policy='tester_end_to_device_start'))

        # --- PRD-M004 Turn Gap: tester_end -> device_start (SIGNED) ---
        if has_tester and has_device:
            value = formulas.turn_gap_ms(tester_end['start_ms'], device_start['start_ms'])
            note = None if value >= 0 else 'Negative gap: device started before tester finished (overlap)'
            metrics.append(_metric(timeline, 'turn_gap_ms', 'PRD-M004',
                                   value=value, status='observed', turn_id=turn_id,
                                   response_id=response_id,
                                   events=(tester_end, device_start),
                                   reason=note or 'Measured tester speech end to device speech start',
                                   policy='device_speech_start'))
        elif has_tester:
            metrics.append(_metric(timeline, 'turn_gap_ms', 'PRD-M004',
                                   status='insufficient_evidence', turn_id=turn_id,
                                   events=(tester_end,),
                                   reason='Tester utterance has no associated device onset evidence',
                                   policy='device_speech_start'))
        else:
            metrics.append(_metric(timeline, 'turn_gap_ms', 'PRD-M004',
                                   status='not_applicable', turn_id=turn_id,
                                   reason='Turn has no tester utterance',
                                   policy='device_speech_start'))

        # --- PRD-M009 Overlap duration/ratio ---
        overlap_starts = _find_all(events, 'overlap_start', turn_events)
        overlap_ends = _find_all(events, 'overlap_end', turn_events)
        if overlap_starts and overlap_ends:
            for os_, oe_ in zip(overlap_starts, overlap_ends):
                try:
                    dur = formulas.overlap_duration_ms(os_['start_ms'], oe_['start_ms'])
                    metrics.append(_metric(timeline, 'overlap_duration_ms', 'PRD-M009',
                                           value=dur, status='observed', turn_id=turn_id,
                                           response_id=response_id, events=(os_, oe_)))
                    if device_start and device_end:
                        device_dur = device_end['start_ms'] - device_start['start_ms']
                        if device_dur > 0:
                            ratio = formulas.overlap_ratio(dur, device_dur)
                            metrics.append(_metric(timeline, 'overlap_ratio', 'PRD-M009',
                                                   value=round(ratio, 4), unit='ratio',
                                                   status='observed', turn_id=turn_id,
                                                   response_id=response_id, events=(os_, oe_)))
                except ValueError as e:
                    metrics.append(_metric(timeline, 'overlap_duration_ms', 'PRD-M009',
                                           status='insufficient_evidence', turn_id=turn_id,
                                           events=(os_, oe_), reason=str(e)))

        # --- PRD-M005 Barge-in Stop Latency (bound to interrupted old response) ---
        interrupt_starts = _find_all(events, 'interrupt_start', turn_events)
        if interrupt_starts:
            for is_ in interrupt_starts:
                interrupted_response = is_.get('response_id')
                # Find the device_speech_end bound to the SAME interrupted response
                old_end = None
                for cand in _find_all(events, 'device_speech_end'):
                    if interrupted_response is not None and cand.get('response_id') == interrupted_response:
                        old_end = cand
                        break
                if old_end is None:
                    metrics.append(_metric(timeline, 'barge_in_stop_latency_ms', 'PRD-M005',
                                           status='insufficient_evidence', turn_id=turn_id,
                                           response_id=interrupted_response, events=(is_,),
                                           reason='No device_speech_end bound to the interrupted response_id',
                                           policy='interrupt_start_to_old_response_end'))
                    continue
                try:
                    value = formulas.barge_in_stop_latency_ms(is_['start_ms'], old_end['start_ms'])
                    metrics.append(_metric(timeline, 'barge_in_stop_latency_ms', 'PRD-M005',
                                           value=value, status='observed', turn_id=turn_id,
                                           response_id=interrupted_response,
                                           events=(is_, old_end),
                                           policy='interrupt_start_to_old_response_end'))
                except ValueError:
                    # Old response already ended before the interruption — not a barge-in.
                    metrics.append(_metric(timeline, 'barge_in_stop_latency_ms', 'PRD-M005',
                                           status='not_applicable', turn_id=turn_id,
                                           response_id=interrupted_response,
                                           events=(is_, old_end),
                                           reason='Old response ended before interruption; no stop latency to measure',
                                           policy='interrupt_start_to_old_response_end'))

    # --- PRD-M008 False Endpoint: candidate only ---
    for fe in _find_all(events, 'possible_false_endpoint'):
        metrics.append(_metric(timeline, 'false_endpoint_candidate', 'PRD-M008',
                               value=True, unit='boolean', status='observed',
                               turn_id=fe.get('turn_id'), events=(fe,),
                               reason='Observed a false-endpoint candidate; confirmation requires '
                                      'semantic evidence that the tester had not finished',
                               policy='candidate_only'))

    # --- Metrics that require semantic evidence: explicit abstention ---
    fb = formulas.feedback_latency_status()
    metrics.append(_metric(timeline, 'feedback_latency_ms', 'PRD-M001',
                           status='insufficient_evidence', reason=fb['reason']))
    mr = formulas.meaningful_response_latency_status()
    metrics.append(_metric(timeline, 'meaningful_response_latency_ms', 'PRD-M003',
                           status='insufficient_evidence', reason=mr['reason']))
    metrics.append(_metric(timeline, 'barge_in_new_intent_latency_ms', 'PRD-M006',
                           status='insufficient_evidence',
                           reason='New intent association requires semantic evidence; '
                                  'an arbitrary later device onset is not the new-intent response'))
    metrics.append(_metric(timeline, 'barge_in_success', 'PRD-M007',
                           status='insufficient_evidence', unit='boolean',
                           method='composite',
                           reason='Barge-in success requires: old response stopped, new input accepted, '
                                  'new intent answered, and no return to the old answer within the '
                                  'observation window. Semantic evidence is missing.'))

    counts = _counts(metrics)
    status = 'observed' if counts['observed'] else 'insufficient_evidence'
    return {'status': status, 'metrics': metrics, 'counts': counts}


def _counts(metrics):
    counts = {'total': len(metrics), 'observed': 0, 'pass': 0, 'fail': 0,
              'not_applicable': 0, 'insufficient_evidence': 0}
    for m in metrics:
        st = m.get('status')
        if st in counts:
            counts[st] += 1
    return counts
