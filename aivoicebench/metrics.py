"""The single deterministic metric producer for canonical EventTimelines.

PRD refs follow the **current** PRD metric decomposition
(`docs/prd/metric-requirements.md`, in force since PRD 1.5.0):

- PRD-M001 First Speech Latency
- PRD-M002 Endpoint / Response End
- PRD-M003 Semantic Response
- PRD-M004 Turn Gap (signed)
- PRD-M005 Barge-in Stop Latency
- PRD-M006 Barge-in Semantic Compliance
- PRD-M007 False Endpoint (candidate and confirmed are separate results)
- PRD-M008 ASR / Transcript Quality
- PRD-M009 Overlap
- PRD-M010 Coverage / Outcome

`definition_version` 3.0.0 used an earlier decomposition of the same
`PRD-M001..M010` range. That table is retained in `PRD_REFS_BY_DEFINITION` and
`migrate_metric_document` so a historical MetricResult document stays readable and
has an explicit migration. New output is always `DEFINITION_VERSION`.

Design rules enforced here:

- One producer. No `recording_*` / `live_*` / `offline_*` parallel formula.
- A metric is only as valid as its eligible evidence. Missing, ambiguous or
  ineligible evidence is an explicit result state (`insufficient_evidence`,
  `not_applicable`, `invalid`) — never coerced to 0, false or success.
- `source='asr'` is a transcript estimate, never an acoustic boundary. PRD-M002
  therefore never treats an ASR final timestamp as the response end; in the real
  pipeline its candidate always comes from the acoustic `device_speech_end`,
  because fusion's own `response_end` carries `source='derived'` and is filtered
  out by the acoustic-boundary gate.
- Unknown speaker role, missing acoustic boundary, missing semantic evidence and
  failed artifact integrity all abstain explicitly with a reason.
"""

from . import formulas

SCHEMA_VERSION = '3.0.0'
DEFINITION_VERSION = '4.0.0'
LEGACY_DEFINITION_VERSION = '3.0.0'
METRIC_POLICY_VERSION = '1.0.0'

# Sources that may establish an acoustic measurement boundary. ASR timestamps,
# scheduler intent and device logs are evidence, but not acoustic boundaries.
ACOUSTIC_BOUNDARY_SOURCES = ('audio_signal', 'manual_annotation')

# Current PRD decomposition (PRD >= 1.5.0). PRD-M007 owns two names: a candidate
# and a separately-gated confirmation.
PRD_REFS_V4 = {
    'first_speech_latency_ms': 'PRD-M001',
    'response_end_candidate_ms': 'PRD-M002',
    'semantic_response': 'PRD-M003',
    'turn_gap_ms': 'PRD-M004',
    'barge_in_stop_latency_ms': 'PRD-M005',
    'barge_in_semantic_compliance': 'PRD-M006',
    'false_endpoint_candidate': 'PRD-M007',
    'false_endpoint_confirmed': 'PRD-M007',
    'asr_cer': 'PRD-M008',
    'asr_wer': 'PRD-M008',
    'overlap_duration_ms': 'PRD-M009',
    'overlap_ratio': 'PRD-M009',
    'coverage': 'PRD-M010',
}

# Historical decomposition carried by MetricResult documents that already say
# definition_version 3.0.0. Never used for new output; used to read and migrate.
PRD_REFS_V3 = {
    'feedback_latency_ms': 'PRD-M001',
    'first_speech_latency_ms': 'PRD-M002',
    'meaningful_response_latency_ms': 'PRD-M003',
    'turn_gap_ms': 'PRD-M004',
    'barge_in_stop_latency_ms': 'PRD-M005',
    'barge_in_new_intent_latency_ms': 'PRD-M006',
    'barge_in_success': 'PRD-M007',
    'false_endpoint_candidate': 'PRD-M008',
    'false_endpoint_rate': 'PRD-M008',
    'overlap_duration_ms': 'PRD-M009',
    'overlap_ratio': 'PRD-M009',
    'timeout': 'PRD-M010',
    'timeout_rate': 'PRD-M010',
    'asr_cer': 'PRD-M010',
    'asr_wer': 'PRD-M010',
}

PRD_REFS_BY_DEFINITION = {DEFINITION_VERSION: PRD_REFS_V4, LEGACY_DEFINITION_VERSION: PRD_REFS_V3}

# Names the current PRD decomposition no longer defines. They stay emitted for
# continuity with historical documents, always with prd_ref null and a reason.
LEGACY_METRIC_NAMES = (
    'feedback_latency_ms',
    'meaningful_response_latency_ms',
    'barge_in_new_intent_latency_ms',
    'barge_in_success',
)

SEMANTIC_KINDS = ('semantic_response', 'barge_in_compliance', 'false_endpoint_confirmation')

# Per-turn metrics that are a *formal measurement* for PRD-M010: each one closes a
# real interval, so an observed value means a quantity was actually measured.
# PRD-M002 `response_end_candidate_ms` is deliberately absent: it is an endpoint
# candidate position, observable on a turn that produced no latency/gap/barge-in
# measurement at all, and counting it as measured let a run with no formal
# measurement report `coverage` observed 1.0 and a `complete` metrics envelope.
FORMAL_MEASUREMENT_NAMES = ('first_speech_latency_ms', 'turn_gap_ms',
                            'barge_in_stop_latency_ms')


def prd_ref_for(name, definition_version=DEFINITION_VERSION):
    """Resolve the PRD requirement id a metric name carries under a definition."""
    return PRD_REFS_BY_DEFINITION.get(definition_version, {}).get(name)


def definition_versions_for(prd_ref, name):
    """Definition versions in which `name` carries `prd_ref`.

    A PRD id alone is ambiguous across definition versions: PRD-M002 is First
    Speech Latency under 3.0.0 and Endpoint/Response End under 4.0.0. The metric
    name disambiguates, which is why it is required.
    """
    return sorted(version for version, table in PRD_REFS_BY_DEFINITION.items()
                  if table.get(name) == prd_ref)


def migrate_prd_ref(prd_ref, name, from_version=LEGACY_DEFINITION_VERSION,
                    to_version=DEFINITION_VERSION):
    """Map a historical PRD ref onto the current decomposition by metric name.

    A PRD label is not ambiguous in itself; the defect being handled is that the
    *same label* was attached to different metric names by two PRD revisions, so a
    stored `prd_ref` cannot be re-read without the name it was paired with.
    Returns (prd_ref, status) with status in `mapped`, `unchanged`,
    `no_current_requirement` (the name is legacy-only) or `unknown_name`.
    """
    if prd_ref_for(name, from_version) != prd_ref:
        return None, 'unknown_name'
    target = prd_ref_for(name, to_version)
    if target is None:
        return None, 'no_current_requirement'
    return target, ('unchanged' if target == prd_ref else 'mapped')


def migrate_metric_document(metric, to_version=DEFINITION_VERSION):
    """Return a migrated copy of a stored MetricResult; never mutates the input.

    A metric whose name has no requirement in the target definition keeps its
    original `prd_ref` and gains an explicit migration status, so an old value is
    never silently reinterpreted under a new requirement id.
    """
    migrated = dict(metric)
    name, prd_ref = metric.get('name'), metric.get('prd_ref')
    target, status = migrate_prd_ref(prd_ref, name, metric.get('definition_version', LEGACY_DEFINITION_VERSION),
                                     to_version)
    migrated['definition_version'] = to_version
    migrated['migration'] = {'from_definition_version': metric.get('definition_version'),
                             'from_prd_ref': prd_ref, 'status': status}
    if status in ('mapped', 'unchanged'):
        migrated['prd_ref'] = target
    return migrated


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
            ids.extend(e.get('evidence_ids') or ())
    return list(dict.fromkeys(ids))


def _event_ids(*events):
    ids = []
    for e in events:
        if isinstance(e, dict) and 'event_id' in e:
            ids.append(e['event_id'])
    return list(dict.fromkeys(ids))


def _is_acoustic_boundary(event):
    """An acoustic boundary needs a signal/manual source, not an ASR estimate."""
    return bool(event) and event.get('source') in ACOUSTIC_BOUNDARY_SOURCES


def _boundary_gap_reason(timeline):
    for gap in timeline.get('gaps') or ():
        if isinstance(gap, dict) and gap.get('reason'):
            return str(gap['reason'])
    return None


def _role_identity_established(turn_events):
    """Tester/device identity needs an acoustic boundary for each role."""
    tester = (_find_event(turn_events, 'tester_speech_start') or
              _find_event(turn_events, 'tester_speech_end'))
    device = (_find_event(turn_events, 'device_speech_start') or
              _find_event(turn_events, 'device_speech_end') or
              _find_event(turn_events, 'response_end'))
    return _is_acoustic_boundary(tester) and _is_acoustic_boundary(device)


def _semantic_record(semantic_evidence, kind, turn_id):
    """Return an eligible constrained-semantic-evidence record, or None.

    Eligibility is structural: kind match, boolean decision, declared criterion
    id/version, judge profile and at least one evidence or event reference. A
    record missing any of these is not evidence and cannot become a value.
    """
    for record in semantic_evidence or ():
        if not isinstance(record, dict) or record.get('kind') != kind:
            continue
        if record.get('turn_id') not in (turn_id, None):
            continue
        if type(record.get('decision')) is not bool:
            continue
        if not record.get('criterion_id') or not record.get('criterion_version'):
            continue
        if not record.get('judge_profile'):
            continue
        if not (record.get('evidence_ids') or record.get('event_ids')):
            continue
        return record
    return None


def _integrity_state(integrity):
    if not isinstance(integrity, dict) or integrity.get('status') not in ('valid', 'invalid'):
        return 'unchecked', None
    return integrity['status'], integrity.get('reason') or 'Artifact integrity is invalid'


def _sample_key(index, *events):
    """A stable per-sample discriminator for names emitted more than once per turn.

    A turn can legitimately carry several samples of the same measurement (one
    `overlap_duration_ms` per overlap pair, one `barge_in_stop_latency_ms` per
    interruption). The event id is preferred because it is stable evidence; the
    ordinal is the fallback for producers that omit it. Callers must not reuse a
    key for two samples of the same name in one turn, because `metric_id` is the
    identity `aggregate_metrics` counts on.
    """
    for event in events:
        event_id = event.get('event_id') if isinstance(event, dict) else None
        if event_id:
            return str(event_id)
    return f'sample-{index + 1:02d}'


def _metric(timeline, name, *, value=None, unit='ms', status='observed', turn_id=None,
            response_id=None, events=(), reason=None, policy=None, policy_version=None,
            confidence=None, confidence_source=None, uncertainty_ms=None,
            measurement_scope='black_box', method='deterministic', judge_profile=None,
            aggregation=None, prd_ref='derive', sample_key=None):
    """Build one canonical MetricResult document under the current definition.

    `sample_key` disambiguates names that a single turn can produce several
    samples of; without it two samples of the same name in one turn would share a
    `metric_id` and could never be aggregated.
    """
    run_id = timeline.get('run_id') or 'RUN-auto'
    if prd_ref == 'derive':
        prd_ref = prd_ref_for(name)
    measured = status in ('observed', 'pass', 'fail')
    metric_id = f'{run_id}.{name}' + (f'.{turn_id}' if turn_id else '')
    if sample_key is not None:
        metric_id += f'.{sample_key}'
    metric = {
        'schema_version': SCHEMA_VERSION,
        'metric_id': metric_id,
        'run_id': run_id,
        'case_id': timeline.get('case_id') or None,
        'analysis_id': timeline.get('analysis_id'),
        'prd_ref': prd_ref,
        'turn_id': turn_id,
        'response_id': response_id,
        'name': name,
        'definition_version': DEFINITION_VERSION,
        'policy': policy,
        'policy_version': (policy_version or METRIC_POLICY_VERSION) if policy else None,
        'execution_kind': timeline.get('execution_kind') or 'imported',
        'value': value,
        'unit': unit,
        'status': status,
        'reason': reason if reason is not None else (
            'Measured from timeline events' if measured else 'Not reported'),
        'measurement_scope': measurement_scope,
        'method': method,
        'confidence': confidence,
        'confidence_source': confidence_source,
        'uncertainty_ms': uncertainty_ms,
        'evidence_ids': _evidence_ids(*events),
        'event_ids': _event_ids(*events),
        'aggregation': aggregation or {
            'kind': 'single',
            'sample_count': 1 if measured else 0,
            'total_count': 1,
            'excluded_count': 0 if measured else 1,
            'algorithm': 'single',
            'input_metric_ids': [],
        },
    }
    if judge_profile is not None:
        metric['judge_profile'] = judge_profile
    return metric


def _counts(metrics):
    counts = {'total': len(metrics), 'observed': 0, 'pass': 0, 'fail': 0,
              'not_applicable': 0, 'insufficient_evidence': 0, 'invalid': 0, 'abstained': 0}
    for metric in metrics:
        status = metric.get('status')
        if status in counts:
            counts[status] += 1
    counts['abstained'] = (counts['not_applicable'] + counts['insufficient_evidence']
                           + counts['invalid'])
    return counts


def metric_status(metric):
    """The status a run derives from one metric document.

    A metric only counts as an outcome when it carries a status *and* an actual
    value; an `observed` document with a null value is not a measurement result.
    """
    status = metric.get('status')
    if status in ('observed', 'pass', 'fail') and metric.get('value') is not None:
        return 'observed'
    if status == 'invalid':
        return 'invalid'
    return None


def run_status(metrics, timeline=None):
    """The single rule for the overall status of a metric document set.

    Both calling surfaces (`import_pipeline`/CLI via `compute_timeline_metrics` and
    the classic `pipeline._integrate_llm_metrics`) must derive the same status from
    the same documents; two different rules would break the pipeline-invariance
    claim of AC2.
    """
    derived = {metric_status(metric) for metric in metrics}
    if 'observed' in derived:
        return 'observed'
    if 'invalid' in derived:
        return 'invalid'
    if timeline is not None and timeline.get('status') in ('invalid', 'blocked'):
        return timeline['status']
    return 'insufficient_evidence'


def _global_state(metrics, timeline):
    return run_status(metrics, timeline)


def _invalid_metrics(timeline, reason):
    """Every name the current decomposition defines, plus the legacy records.

    An invalid-integrity run measured nothing, so a reader must not find PRD
    requirements silently absent from the document set and read them as evaluated.
    The legacy continuity names keep their `prd_ref: null` contract and their own
    reason; they are emitted here so the invalid envelope has the same name
    coverage as a normal run.
    """
    prd_names = sorted(PRD_REFS_V4)
    # Mirror the unit/scope the name carries in a normal run, so the invalid
    # envelope is schema-valid for every name it declares. `method` is the
    # deterministic integrity determination itself: no judge ran, so no semantic
    # name may claim `llm_judge` and carry a judge profile it never used.
    specs = {
        'first_speech_latency_ms': ('ms', 'black_box', 'deterministic'),
        'response_end_candidate_ms': ('ms', 'black_box', 'deterministic'),
        'semantic_response': ('boolean', 'black_box', 'deterministic'),
        'turn_gap_ms': ('ms', 'black_box', 'deterministic'),
        'barge_in_stop_latency_ms': ('ms', 'black_box', 'deterministic'),
        'barge_in_semantic_compliance': ('boolean', 'black_box', 'deterministic'),
        'false_endpoint_candidate': ('boolean', 'black_box', 'deterministic'),
        'false_endpoint_confirmed': ('boolean', 'black_box', 'composite'),
        'asr_cer': ('ratio', 'white_box', 'ground_truth_comparison'),
        'asr_wer': ('ratio', 'white_box', 'ground_truth_comparison'),
        'overlap_duration_ms': ('ms', 'black_box', 'deterministic'),
        'overlap_ratio': ('ratio', 'black_box', 'deterministic'),
    }
    metrics = []
    for name in prd_names:
        if name == 'coverage':
            continue
        unit, scope, method = specs[name]
        metrics.append(_metric(
            timeline, name, status='invalid', value=None, unit=unit,
            measurement_scope=scope, method=method, reason=reason,
            policy='artifact_integrity'))
    abstained = len(metrics)
    metrics.append(_metric(
        timeline, 'coverage', status='invalid', value=None, unit='ratio',
        policy='artifact_integrity', reason=reason,
        aggregation={'kind': 'rate', 'sample_count': 0, 'total_count': 0, 'excluded_count': 0,
                     'algorithm': 'eligible_ratio', 'input_metric_ids': [],
                     'invalid_count': 0, 'abstained_count': abstained}))
    metrics.extend(_legacy_records(timeline, integrity_reason=reason))
    return metrics


def compute_timeline_metrics(timeline, *, semantic_evidence=None, device_transcript=None,
                             reference_transcript=None, integrity=None, planned_units=None):
    """Compute canonical MetricResults from one EventTimeline.

    Pipeline-invariant: the same canonical event values and policy produce the
    same metric values whether the caller is the import pipeline, the CLI or the
    classic pipeline. Only provenance (run/analysis identity) differs.

    Args:
        semantic_evidence: constrained semantic/judge records, each with `kind`
            (`semantic_response` | `barge_in_compliance` |
            `false_endpoint_confirmation`), `turn_id`, boolean `decision`,
            `criterion_id`, `criterion_version`, `judge_profile` and evidence or
            event references. Anything less is not evidence.
        device_transcript: {'text', 'source', 'artifact_id', 'evidence_ids'}; the
            source must be 'device_internal' because external ASR output is not
            the device's own ASR.
        reference_transcript: {'text', 'reference_id'} eligible reference text.
        integrity: {'status': 'valid'|'invalid', 'reason': ...} artifact integrity
            established by the caller. Invalid integrity yields `invalid` results.
        planned_units: planned measurement units for PRD-M010 coverage, when a
            plan exists.

    Returns:
        {'status', 'definition_version', 'metrics': [MetricResult...], 'counts': {...}}
    """
    events = timeline.get('events') or []
    if not events:
        return {'status': 'insufficient_evidence', 'reason': 'No events in timeline',
                'definition_version': DEFINITION_VERSION, 'metrics': [], 'counts': _counts([])}

    integrity_status, integrity_reason = _integrity_state(integrity)
    if integrity_status == 'invalid':
        metrics = _invalid_metrics(timeline, integrity_reason)
        return {'status': 'invalid', 'reason': integrity_reason,
                'definition_version': DEFINITION_VERSION, 'metrics': metrics,
                'counts': _counts(metrics)}

    turn_groups = _group_events_by_turn(events)
    turn_ids = sorted(turn_groups.keys())
    metrics = []

    for turn_id in turn_ids:
        turn_events = turn_groups[turn_id]
        tester_end = _find_event(turn_events, 'tester_speech_end')
        device_start = _find_event(turn_events, 'device_speech_start')
        device_end = _find_event(turn_events, 'device_speech_end')
        response_end_event = _find_event(turn_events, 'response_end')
        response_id = None
        for candidate in (device_start, device_end, response_end_event):
            if candidate and candidate.get('response_id'):
                response_id = candidate['response_id']
                break

        tester_present = any(e.get('type') in ('tester_speech_start', 'tester_speech_end')
                             for e in turn_events)
        metrics.append(_first_speech_latency(timeline, turn_id, response_id,
                                             tester_end, device_start))
        metrics.append(_response_end_metric(timeline, turn_id, response_id, tester_present,
                                            tester_end, device_end, response_end_event,
                                            turn_events))
        metrics.append(_semantic_metric(timeline, 'semantic_response', 'PRD-M003',
                                        'semantic_response', turn_id, response_id,
                                        device_start is not None or device_end is not None,
                                        semantic_evidence,
                                        'Turn has no device response to evaluate for semantic coverage'))
        metrics.append(_turn_gap(timeline, turn_id, response_id, tester_end, device_start))
        metrics.extend(_barge_in_stop(timeline, events, turn_id, response_id, turn_events))
        metrics.append(_semantic_metric(timeline, 'barge_in_semantic_compliance', 'PRD-M006',
                                        'barge_in_compliance', turn_id, response_id,
                                        bool(_find_all(turn_events, 'interrupt_start')),
                                        semantic_evidence,
                                        'No interruption in this turn; barge-in compliance is not applicable'))
        metrics.extend(_overlap_metrics(timeline, turn_id, response_id, turn_events))

    metrics.extend(_false_endpoint_metrics(timeline, events, semantic_evidence, turn_groups))
    metrics.extend(_transcript_quality_metrics(timeline, device_transcript, reference_transcript))
    metrics.append(_coverage_metric(timeline, turn_ids, events, metrics, planned_units))
    metrics.extend(_legacy_records(timeline))

    return {'status': _global_state(metrics, timeline), 'definition_version': DEFINITION_VERSION,
            'metrics': metrics, 'counts': _counts(metrics)}


def _boundary_confidence(*events):
    values = [e.get('confidence') for e in events
              if isinstance(e, dict) and e.get('confidence_source') in (None, 'acoustic_boundary')
              and e.get('confidence') is not None]
    return min(values) if values else None


def _boundary_uncertainty(*events):
    values = [e.get('uncertainty_ms') for e in events
              if isinstance(e, dict) and e.get('uncertainty_ms') is not None]
    return sum(values) if values else None


def _first_speech_latency(timeline, turn_id, response_id, tester_end, device_start):
    """PRD-M001: tester speech end -> first device speech onset, acoustic only."""
    policy = 'tester_end_to_device_start'
    if tester_end is not None and device_start is not None:
        if not (_is_acoustic_boundary(tester_end) and _is_acoustic_boundary(device_start)):
            return _metric(
                timeline, 'first_speech_latency_ms', status='insufficient_evidence',
                turn_id=turn_id, response_id=response_id, events=(tester_end, device_start),
                policy=policy,
                reason='A tester-end or device-onset boundary is an ASR or control estimate, not '
                       'an acoustic boundary; first speech latency needs acoustic boundaries')
        try:
            return _metric(
                timeline, 'first_speech_latency_ms',
                value=formulas.latency_ms(tester_end['start_ms'], device_start['start_ms']),
                turn_id=turn_id, response_id=response_id, events=(tester_end, device_start),
                policy=policy, confidence=_boundary_confidence(tester_end, device_start),
                confidence_source='acoustic_boundary',
                uncertainty_ms=_boundary_uncertainty(tester_end, device_start))
        except ValueError:
            return _metric(
                timeline, 'first_speech_latency_ms', status='not_applicable', turn_id=turn_id,
                response_id=response_id, events=(tester_end, device_start), policy=policy,
                reason='Device onset precedes tester speech end; overlap is not latency')
    if tester_end is not None:
        return _metric(
            timeline, 'first_speech_latency_ms', status='insufficient_evidence', turn_id=turn_id,
            events=(tester_end,), policy=policy,
            reason='Tester utterance has no associated device onset evidence')
    return _metric(
        timeline, 'first_speech_latency_ms', status='not_applicable', turn_id=turn_id, policy=policy,
        reason='Turn has no tester utterance; no response expected')


def _turn_gap(timeline, turn_id, response_id, tester_end, device_start):
    """PRD-M004: signed tester end -> device start; negative means overlap."""
    policy = 'device_speech_start'
    if tester_end is not None and device_start is not None:
        value = formulas.turn_gap_ms(tester_end['start_ms'], device_start['start_ms'])
        note = None if value >= 0 else 'Negative gap: device started before tester finished (overlap)'
        return _metric(
            timeline, 'turn_gap_ms', value=value, turn_id=turn_id, response_id=response_id,
            events=(tester_end, device_start), policy=policy,
            reason=note or 'Measured tester speech end to device speech start',
            confidence=_boundary_confidence(tester_end, device_start),
            confidence_source='acoustic_boundary',
            uncertainty_ms=_boundary_uncertainty(tester_end, device_start))
    if tester_end is not None:
        return _metric(
            timeline, 'turn_gap_ms', status='insufficient_evidence', turn_id=turn_id,
            events=(tester_end,), policy=policy,
            reason='Tester utterance has no associated device onset evidence')
    return _metric(
        timeline, 'turn_gap_ms', status='not_applicable', turn_id=turn_id, policy=policy,
        reason='Turn has no tester utterance')


def _response_end_metric(timeline, turn_id, response_id, tester_started, tester_end,
                         device_end, response_end_event, turn_events):
    """PRD-M002: a response-end candidate with its uncertainty, never an ASR final.

    `device_speech_end` is an acoustic boundary; fusion also emits a `response_end`
    derived event. Only an acoustic boundary can supply the candidate, so in the
    real pipeline it always comes from the acoustic `device_speech_end`; a
    `derived` `response_end` is filtered out and any ASR final abstains.

    A response end belongs to a *response*, so the request has to have completed:
    the turn needs a `tester_speech_end`, not merely a `tester_speech_start`. A
    turn where the tester utterance is still open has no request whose answer
    could end, and calling a bare device boundary a "formal measurement" there is
    what let a pipeline with no measurement look complete.
    """
    policy = 'acoustic_boundary_candidate'
    if not tester_started and tester_end is None:
        return _metric(
            timeline, 'response_end_candidate_ms', status='not_applicable', turn_id=turn_id,
            response_id=response_id, policy=policy,
            reason='Turn has no tester utterance; there is no response whose end could be measured')
    if tester_end is None:
        return _metric(
            timeline, 'response_end_candidate_ms', status='insufficient_evidence', turn_id=turn_id,
            response_id=response_id, policy=policy,
            reason='The tester utterance has no end boundary, so the request never completed and '
                   'there is no response whose end could be measured')
    boundaries = [e for e in (device_end, response_end_event) if e is not None]
    acoustic = [e for e in boundaries if _is_acoustic_boundary(e)]
    if acoustic:
        boundary = acoustic[0]
        return _metric(
            timeline, 'response_end_candidate_ms', value=boundary['start_ms'], turn_id=turn_id,
            response_id=response_id, events=(boundary,), policy=policy,
            confidence=boundary.get('confidence'), confidence_source='acoustic_boundary',
            uncertainty_ms=boundary.get('uncertainty_ms'),
            reason='Response-end candidate on the audio_relative_ms time base; it is a candidate '
                   'with its own uncertainty, not a confirmed end')
    asr_boundaries = [e for e in turn_events
                      if e.get('type') in ('device_speech_end', 'response_end', 'asr_segment')
                      and e.get('source') == 'asr']
    if asr_boundaries:
        return _metric(
            timeline, 'response_end_candidate_ms', status='insufficient_evidence', turn_id=turn_id,
            response_id=response_id, events=tuple(asr_boundaries), policy=policy,
            reason='The only response-end evidence is an ASR final timestamp; an ASR final is a '
                   'transcript estimate, not the acoustic response-end truth')
    has_response = any(e.get('type') in ('device_speech_start', 'device_speech_end', 'response_start')
                       for e in turn_events)
    if not has_response:
        return _metric(
            timeline, 'response_end_candidate_ms', status='not_applicable', turn_id=turn_id,
            response_id=response_id, policy=policy,
            reason='No device response was observed for this turn; there is no response end')
    return _metric(
        timeline, 'response_end_candidate_ms', status='insufficient_evidence', turn_id=turn_id,
        response_id=response_id, policy=policy,
        reason='No acoustic response-end boundary evidence is available for this turn')


def _semantic_metric(timeline, name, prd_ref, kind, turn_id, response_id, applicable,
                     semantic_evidence, not_applicable_reason):
    policy = 'constrained_semantic_evidence'
    if not applicable:
        # No judgment was performed, so this is a deterministic eligibility
        # decision; a judge profile is required only for a performed judgment.
        return _metric(timeline, name, prd_ref=prd_ref, status='not_applicable', unit='boolean',
                       turn_id=turn_id, response_id=response_id,
                       reason=not_applicable_reason, policy=policy)
    record = _semantic_record(semantic_evidence, kind, turn_id)
    if record is None:
        return _metric(
            timeline, name, prd_ref=prd_ref, status='insufficient_evidence', unit='boolean',
            turn_id=turn_id, response_id=response_id, policy=policy,
            reason='Constrained semantic evidence is required and was not supplied: a semantic '
                   'judgment needs a boolean decision, criterion id/version, a judge profile and '
                   'evidence references; timing alone cannot establish it')
    selected = [e for e in timeline.get('events') or ()
                if e.get('event_id') in (record.get('event_ids') or ())]
    selected.append({'evidence_ids': list(record.get('evidence_ids') or ())})
    return _metric(
        timeline, name, prd_ref=prd_ref, value=record['decision'], unit='boolean',
        method='llm_judge', status='observed', turn_id=turn_id, response_id=response_id,
        events=tuple(selected), policy=policy, judge_profile=record['judge_profile'],
        confidence=record.get('confidence'), confidence_source='semantic_event',
        reason=f"Semantic decision from criterion {record['criterion_id']}"
               f"@{record['criterion_version']}")


def _barge_in_stop(timeline, events, turn_id, response_id, turn_events):
    """PRD-M005: interruption onset -> end of the interrupted old response."""
    policy = 'interrupt_start_to_old_response_end'
    interrupt_starts = _find_all(turn_events, 'interrupt_start')
    if not interrupt_starts:
        return [_metric(
            timeline, 'barge_in_stop_latency_ms', status='not_applicable', turn_id=turn_id,
            response_id=response_id, policy=policy,
            reason='No interruption in this turn; there is no interrupted response to stop')]
    results = []
    for index, interrupt in enumerate(interrupt_starts):
        sample_key = _sample_key(index, interrupt)
        interrupted_response = interrupt.get('response_id')
        old_end = None
        if interrupted_response is not None:
            for candidate in _find_all(events, 'device_speech_end'):
                if candidate.get('response_id') == interrupted_response:
                    old_end = candidate
                    break
        if old_end is None:
            results.append(_metric(
                timeline, 'barge_in_stop_latency_ms', status='insufficient_evidence',
                turn_id=turn_id, response_id=interrupted_response, events=(interrupt,),
                policy=policy, sample_key=sample_key,
                reason='No device_speech_end is bound to the interrupted response_id; the '
                       'interrupted old response cannot be identified'))
            continue
        try:
            results.append(_metric(
                timeline, 'barge_in_stop_latency_ms',
                value=formulas.barge_in_stop_latency_ms(interrupt['start_ms'], old_end['start_ms']),
                turn_id=turn_id, response_id=interrupted_response, events=(interrupt, old_end),
                policy=policy, sample_key=sample_key))
        except ValueError:
            results.append(_metric(
                timeline, 'barge_in_stop_latency_ms', status='not_applicable', turn_id=turn_id,
                response_id=interrupted_response, events=(interrupt, old_end), policy=policy,
                sample_key=sample_key,
                reason='Old response ended before interruption; no stop latency to measure'))
    return results


def _overlap_metrics(timeline, turn_id, response_id, turn_events):
    """PRD-M009: overlap of tester/device speech, or an explicit abstention.

    Every branch returns at least one document. A turn whose role identity and
    intervals exist but whose intersection cannot be formed must abstain
    explicitly; returning nothing would let PRD-M010 read an unmeasurable turn as
    an evaluated one (AC1 asks for an eligibility path, not a silent omission).
    """
    starts = _find_all(turn_events, 'overlap_start')
    ends = _find_all(turn_events, 'overlap_end')
    if starts and ends:
        device_start = _find_event(turn_events, 'device_speech_start')
        device_end = _find_event(turn_events, 'device_speech_end')
        results = []
        for index, (overlap_start, overlap_end) in enumerate(zip(starts, ends)):
            results.extend(_overlap_pair(timeline, turn_id, response_id, overlap_start,
                                        overlap_end, device_start, device_end,
                                        sample_key=_sample_key(index, overlap_start)))
        return results
    tester = _speech_intervals(turn_events, 'tester')
    device = _speech_intervals(turn_events, 'device')
    if tester and device:
        total = formulas.overlap_ms(tester, device)
        device_duration = sum(end - start for start, end in device)
        selected = tuple(e for e in turn_events if e.get('type') in
                         ('tester_speech_start', 'tester_speech_end',
                          'device_speech_start', 'device_speech_end'))
        results = [_metric(
            timeline, 'overlap_duration_ms', value=total, turn_id=turn_id, response_id=response_id,
            events=selected, policy='speech_interval_union_intersection',
            reason='Intersection of the tester and device speech interval unions')]
        if device_duration > 0:
            results.append(_metric(
                timeline, 'overlap_ratio',
                value=round(formulas.overlap_ratio(total, device_duration), 4), unit='ratio',
                turn_id=turn_id, response_id=response_id, events=selected,
                policy='speech_interval_union_intersection',
                reason='Overlap over the declared same-turn device speech duration denominator'))
        return results
    if not _role_identity_established(turn_events):
        reason = ('Tester/device speech identity cannot be established from the recording, so '
                  'overlap cannot be attributed to a channel')
        gap = _boundary_gap_reason(timeline)
        if gap:
            reason = f'{reason} (timeline gap: {gap})'
        return [_metric(
            timeline, 'overlap_duration_ms', status='insufficient_evidence', turn_id=turn_id,
            response_id=response_id, events=tuple(turn_events), policy='speech_interval_union_intersection',
            reason=reason)]
    if tester or device:
        # Both roles are identified and at least one has intervals, but the
        # intersection is not formable: the other role has no closing boundary, or
        # its end precedes its start. That is missing evidence, not a zero overlap.
        missing = 'device' if tester else 'tester'
        return [_metric(
            timeline, 'overlap_duration_ms', status='insufficient_evidence', turn_id=turn_id,
            response_id=response_id, events=tuple(turn_events),
            policy='speech_interval_union_intersection',
            reason=f'Overlap is declared but not measurable: the {missing} speech interval set has '
                   'no closed interval to intersect (a missing end boundary or an end at/before its '
                   'start), so no overlap intersection exists')]
    return [_metric(
        timeline, 'overlap_duration_ms', status='not_applicable', turn_id=turn_id,
        response_id=response_id, events=tuple(turn_events),
        policy='speech_interval_union_intersection',
        reason='Overlap is not applicable: neither role has a bounded speech interval in this turn, '
               'so no overlap window was declared')]


def _overlap_pair(timeline, turn_id, response_id, overlap_start, overlap_end,
                  device_start, device_end, sample_key=None):
    results = []
    try:
        duration = formulas.overlap_duration_ms(overlap_start['start_ms'], overlap_end['start_ms'])
        results.append(_metric(
            timeline, 'overlap_duration_ms', value=duration, turn_id=turn_id,
            response_id=response_id, events=(overlap_start, overlap_end),
            policy='overlap_event_pair', sample_key=sample_key))
        if device_start and device_end:
            device_duration = device_end['start_ms'] - device_start['start_ms']
            if device_duration > 0:
                results.append(_metric(
                    timeline, 'overlap_ratio',
                    value=round(formulas.overlap_ratio(duration, device_duration), 4), unit='ratio',
                    turn_id=turn_id, response_id=response_id, events=(overlap_start, overlap_end),
                    policy='overlap_event_pair', sample_key=sample_key))
    except ValueError as error:
        results.append(_metric(
            timeline, 'overlap_duration_ms', status='insufficient_evidence', turn_id=turn_id,
            response_id=response_id, events=(overlap_start, overlap_end),
            policy='overlap_event_pair', reason=str(error), sample_key=sample_key))
    return results


def _speech_intervals(events, role):
    starts = _find_all(events, f'{role}_speech_start')
    ends = {}
    for event in _find_all(events, f'{role}_speech_end'):
        ends[event.get('response_id') or event.get('turn_id')] = event
    intervals = []
    for start in starts:
        end = ends.get(start.get('response_id') or start.get('turn_id'))
        if end and end['start_ms'] >= start['start_ms']:
            intervals.append((start['start_ms'], end['start_ms']))
    return intervals


def _false_endpoint_metrics(timeline, events, semantic_evidence, turn_groups):
    """PRD-M007: a candidate is never rendered as a confirmed defect."""
    results = []
    for event in _find_all(events, 'possible_false_endpoint'):
        turn_id = event.get('turn_id')
        results.append(_metric(
            timeline, 'false_endpoint_candidate', value=True, unit='boolean', status='observed',
            turn_id=turn_id, events=(event,), policy='candidate_only',
            reason='Observed a false-endpoint candidate; confirmation additionally requires tester '
                   'continuation evidence, a device response inside the intra-utterance pause, the '
                   'full observation window and semantic/manual verification'))
        missing = _false_endpoint_confirmation_gaps(timeline, event, turn_groups, semantic_evidence)
        if missing:
            results.append(_metric(
                timeline, 'false_endpoint_confirmed', status='insufficient_evidence', unit='boolean',
                method='composite', turn_id=turn_id, events=(event,),
                policy='confirmation_requires_full_evidence',
                reason='False-endpoint confirmation is not established: ' + '; '.join(missing)))
            continue
        record = _semantic_record(semantic_evidence, 'false_endpoint_confirmation', turn_id)
        results.append(_metric(
            timeline, 'false_endpoint_confirmed', value=True, unit='boolean', method='composite',
            turn_id=turn_id, events=(event,), policy='confirmation_requires_full_evidence',
            judge_profile=record['judge_profile'], confidence=record.get('confidence'),
            confidence_source='semantic_event',
            reason=f"Confirmed by criterion {record['criterion_id']}"
                   f"@{record['criterion_version']} with tester continuation and in-pause device "
                   'response evidence over the full observation window'))
    return results


def _false_endpoint_confirmation_gaps(timeline, event, turn_groups, semantic_evidence):
    missing = []
    turn_id = event.get('turn_id')
    turn_events = turn_groups.get(turn_id) or []
    if not any(e.get('type') == 'tester_speech_start' and e['start_ms'] >= event['start_ms']
               for e in turn_events):
        missing.append('no tester continuation evidence after the candidate')
    if not any(e.get('type') == 'device_speech_end' and e['start_ms'] >= event['start_ms']
               for e in turn_events):
        missing.append('no device response end evidence inside the intra-utterance pause')
    if not any(e.get('type') in ('planned_pause_start', 'planned_pause_end') for e in turn_events):
        missing.append('the full observation window is not covered')
    if timeline.get('status') != 'complete':
        missing.append('the timeline is not complete, so the observation window is not established')
    if _semantic_record(semantic_evidence, 'false_endpoint_confirmation', turn_id) is None:
        missing.append('no semantic or manual confirmation record was supplied')
    return missing


def _transcript_quality_metrics(timeline, device_transcript, reference_transcript):
    """PRD-M008: only against an eligible reference; external ASR is not device truth."""
    names = ('asr_cer', 'asr_wer')
    policy = 'nfc_v1'
    reference = (reference_transcript or {}).get('text')
    if not reference:
        return [_metric(
            timeline, name, status='not_applicable', unit='ratio', method='ground_truth_comparison',
            measurement_scope='white_box', policy=policy,
            reason='An eligible nonempty reference transcript is required before transcript quality '
                   'can be computed for the device') for name in names]
    text = str((device_transcript or {}).get('text') or '').strip()
    source = (device_transcript or {}).get('source')
    evidence = [key for key in (device_transcript or {}).get('evidence_ids') or ()
                if isinstance(key, str)]
    if not text or source != 'device_internal' or not evidence:
        if not text:
            detail = ('A device-internal transcript is required; an external ASR transcript '
                      'annotates the recording and cannot populate the device metric')
        elif source != 'device_internal':
            detail = (f"Transcript source '{source}' is not the device-internal transcript; "
                      'external ASR output must never be reported as the device ASR quality')
        else:
            detail = ('The device transcript must reference its evidence artifact; a measurement '
                      'without an evidence reference is not reportable')
        return [_metric(
            timeline, name, status='insufficient_evidence', unit='ratio',
            method='ground_truth_comparison', measurement_scope='white_box', policy=policy,
            reason=detail) for name in names]
    cer = formulas.character_error_rate(reference, text)
    wer = formulas.word_error_rate(reference, text)
    events = ({'evidence_ids': evidence},)
    # `metric_id` is the identity an aggregate links through, so the micro CER
    # sample has to link itself; a `micro` aggregate with `sample_count` 1 and no
    # linked metric id violates the metric contract's own invariant.
    cer_metric = _metric(
        timeline, 'asr_cer', value=cer['value'], unit='ratio',
        method='ground_truth_comparison', measurement_scope='white_box', events=events,
        policy=policy,
        reason=f"Character error rate over {cer['reference_characters']} eligible reference "
               f"characters ({cer['normalization']}); {cer['edits']} edits")
    cer_metric['aggregation'] = {
        'kind': 'micro', 'sample_count': 1, 'total_count': 1, 'excluded_count': 0,
        'algorithm': 'micro_cer', 'input_metric_ids': [cer_metric['metric_id']]}
    return [
        cer_metric,
        _metric(
            timeline, 'asr_wer', value=wer['value'], unit='ratio',
            method='ground_truth_comparison', measurement_scope='white_box', events=events,
            policy=policy,
            reason=f"Word error rate over {wer['reference_words']} eligible reference words "
                   f"({wer['normalization']}); {wer['edits']} edits"),
    ]


def _coverage_metric(timeline, turn_ids, events, metrics, planned_units):
    """PRD-M010: planned / attempted / observed / measured / abstained stay distinct.

    The measured unit is a **formal measurement**, not "the turn has some observed
    metric". PRD-M002 `response_end_candidate_ms` is an endpoint *candidate* whose
    gate is only "a completed tester request plus one acoustic device boundary" — it
    becomes observed on a timeline that produced no latency, gap or barge-in
    measurement at all. Counting it as measured made `coverage` report an observed
    1.0 (and the metrics envelope `complete`) for a run with no formal measurement,
    which is the defect this rule exists to prevent. Only the per-turn measurements
    that close a real interval qualify: M001 first speech latency, M004 turn gap and
    M005 barge-in stop latency.
    """
    attempted = len(turn_ids)
    measured_turns = {}  # turn_id -> representative eligible metric id (deterministic)
    for metric in sorted(metrics, key=lambda m: (m.get('name') or '', m.get('metric_id') or '')):
        if metric.get('name') not in FORMAL_MEASUREMENT_NAMES:
            continue
        if metric.get('status') not in ('observed', 'pass', 'fail'):
            continue
        turn_id = metric.get('turn_id')
        if not turn_id or not metric.get('metric_id'):
            continue
        measured_turns.setdefault(turn_id, metric['metric_id'])
    measured = len(measured_turns)
    invalid = len({m.get('turn_id') for m in metrics
                   if m.get('status') == 'invalid' and m.get('turn_id')})
    abstained = attempted - measured
    input_metric_ids = [measured_turns[turn_id] for turn_id in sorted(measured_turns)]
    if attempted == 0:
        return _metric(
            timeline, 'coverage', status='not_applicable', unit='ratio',
            policy='measurement_coverage',
            aggregation={'kind': 'rate', 'sample_count': 0, 'total_count': 0, 'excluded_count': 0,
                         'algorithm': 'eligible_ratio', 'input_metric_ids': [],
                         'invalid_count': 0, 'abstained_count': 0},
            reason='No measurement unit was attempted in this timeline')
    counts = {'kind': 'rate', 'sample_count': measured, 'total_count': attempted,
              'excluded_count': abstained, 'algorithm': 'eligible_ratio',
              'input_metric_ids': input_metric_ids,
              'invalid_count': invalid, 'abstained_count': abstained}
    if planned_units is not None:
        counts['planned_count'] = planned_units
    plan_note = ('no plan was supplied, so planned coverage is not reported and a control or '
                 'transport success is never substituted for measurement coverage'
                 if planned_units is None else f'{planned_units} planned')
    link_note = ('each measured turn contributes its representative measurement id, so the linked '
                 'metric ids stay one per measured turn'
                 if input_metric_ids else 'no formal measurement was produced, so no input metric '
                 'is linked')
    if measured == 0:
        # No formal measurement was produced. The denominator is retained and the
        # outcome stays an explicit abstention rather than an observed 0-rate that
        # would let a pipeline with no measured value look like a completed one.
        return _metric(
            timeline, 'coverage', status='insufficient_evidence', value=None, unit='ratio',
            policy='measurement_coverage', events=tuple(events), aggregation=counts,
            reason=f'No attempted turn produced an eligible formal measurement (an observed '
                   f'endpoint candidate is not a measurement); the denominator is retained '
                   f'({attempted} attempted, 0 measured); {link_note}; {plan_note}')
    detail = f'Measured {measured} of {attempted} attempted turns; {abstained} abstained'
    if invalid:
        detail += f'; {invalid} invalid'
    detail += f'; {link_note}; {plan_note}'
    return _metric(
        timeline, 'coverage', value=measured / attempted, unit='ratio',
        policy='measurement_coverage', events=tuple(events), aggregation=counts, reason=detail)


def _legacy_records(timeline, integrity_reason=None):
    """Metric names the current PRD decomposition no longer defines.

    They stay in the output so historical documents and downstream readers keep
    their names, but they carry no PRD ref and are never computed into a new value.
    Under an invalid-integrity run they take the `invalid` status too: the whole
    envelope is invalid, and a legacy name must not be the one record that looks
    like an ordinary abstention in an otherwise invalid document set.
    """
    continuity_reason = (
        'Legacy metric: the current PRD-M001..M010 decomposition no longer defines this '
        'requirement. It is retained for continuity with historical MetricResult '
        'documents, is never computed as a new value, and PRD-M003/M006 semantic '
        'requirements require constrained semantic evidence instead')
    records = []
    for name, unit, method in (('feedback_latency_ms', 'ms', 'deterministic'),
                               ('meaningful_response_latency_ms', 'ms', 'deterministic'),
                               ('barge_in_new_intent_latency_ms', 'ms', 'deterministic'),
                               ('barge_in_success', 'boolean', 'composite')):
        if integrity_reason is not None:
            records.append(_metric(
                timeline, name, status='invalid', value=None, unit=unit, method=method,
                prd_ref=None, policy='artifact_integrity',
                reason=f'{integrity_reason}; this legacy continuity name is invalid for the same '
                       'run and carries no measurement'))
            continue
        records.append(_metric(
            timeline, name, status='insufficient_evidence', unit=unit, method=method, prd_ref=None,
            reason=continuity_reason))
    return records


def latency_percentiles(metrics):
    """R7 percentile statistics over the eligible samples of `metrics`.

    Returns the `formulas.percentiles` shape (P50/P90/P95/P99 plus
    sample/total/excluded counts). This is the reporting view; the schema-valid
    MetricResult form is `aggregate_metrics(..., percentile=...)`.
    """
    eligible = [m for m in metrics if m['status'] in ('observed', 'pass', 'fail')
                and type(m['value']) in (int, float)]
    return formulas.percentiles([m['value'] for m in eligible])


def aggregate_metrics(metrics, *, name, percentile=None):
    """Aggregate compatible single-sample results into one MetricResult.

    Reports the denominator, excluded/invalid/abstained counts, the R7 algorithm
    and the eligible input metric ids. It never zero-fills, never averages away an
    abstention, and carries the input evidence forward.
    """
    eligible = [m for m in metrics if m['status'] in ('observed', 'pass', 'fail')]
    invalid = [m for m in metrics if m['status'] == 'invalid']
    abstained = [m for m in metrics if m['status'] in ('not_applicable', 'insufficient_evidence')]
    if metrics:
        keys = {(m['name'], m['definition_version'], m['measurement_scope'], m['execution_kind'])
                for m in metrics}
        if len({m['metric_id'] for m in metrics}) != len(metrics):
            raise ValueError('Duplicate metric samples cannot be counted twice')
        if len(keys) != 1 or any(m['aggregation']['kind'] != 'single' for m in metrics):
            raise ValueError('Only compatible single-sample results may be aggregated')
        if percentile is not None and metrics[0]['unit'] != 'ms':
            raise ValueError('A latency percentile aggregate requires millisecond samples')
    total = len(metrics)
    kind = 'percentile' if percentile is not None else 'rate'
    evidence_ids = list(dict.fromkeys(key for m in eligible for key in m['evidence_ids']))
    aggregation = {
        'kind': kind,
        'sample_count': len(eligible),
        'total_count': total,
        'excluded_count': total - len(eligible),
        'algorithm': 'R7' if percentile is not None else 'eligible_ratio',
        'input_metric_ids': [m['metric_id'] for m in eligible],
        'invalid_count': len(invalid),
        'abstained_count': len(abstained),
    }
    if percentile is not None:
        aggregation['percentile'] = percentile
    container = {
        'schema_version': SCHEMA_VERSION,
        'metric_id': f'{name}.aggregate',
        'run_id': metrics[0]['run_id'] if metrics else 'RUN-aggregate',
        'case_id': metrics[0]['case_id'] if metrics else None,
        'analysis_id': None,
        'prd_ref': prd_ref_for(name),
        'turn_id': None,
        'response_id': None,
        'name': name,
        'definition_version': DEFINITION_VERSION,
        'policy': 'aggregate_compatible_single_samples',
        'policy_version': METRIC_POLICY_VERSION,
        'execution_kind': metrics[0]['execution_kind'] if metrics else 'synthetic',
        'value': None,
        'unit': metrics[0]['unit'] if metrics else 'ratio',
        'status': 'observed' if eligible else 'insufficient_evidence',
        'reason': f'{len(eligible)} eligible of {total} observations; {len(abstained)} abstained and '
                  f'{len(invalid)} invalid are excluded and reported, never zero-filled',
        'measurement_scope': metrics[0]['measurement_scope'] if metrics else 'black_box',
        'method': 'deterministic',
        'confidence': None,
        'confidence_source': None,
        'uncertainty_ms': None,
        'evidence_ids': evidence_ids,
        'event_ids': [],
        'aggregation': aggregation,
    }
    if percentile is None:
        container['unit'] = 'ratio'
        if eligible:
            container['value'] = sum(1 for m in eligible if m['value'] is True) / len(eligible)
        else:
            container['evidence_ids'] = []
            container['reason'] = 'No eligible observation to aggregate; the denominator is retained'
        return container
    container['unit'] = 'ms'
    if eligible:
        container['value'] = latency_percentiles(metrics)[f'P{percentile}']
    else:
        container['evidence_ids'] = []
        container['reason'] = 'No eligible observation to aggregate; the denominator is retained'
    return container
