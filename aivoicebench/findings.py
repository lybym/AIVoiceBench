"""Finding generation from judge results, metrics, and timeline events.

Converts LLM finding candidates (#10) into Finding documents with evidence
links, suspected layers, and human-review requirements. Findings are candidates
— not confirmed defects — until human review.

Evidence-first rules this module enforces:

* a candidate becomes a Finding only from the evidence/event references it
  actually cited and that resolve in the supplied Timeline. There is no
  "nearest evidence" fallback: an unsubstantiated candidate is recorded as an
  abstention, not published as a Finding;
* linked MetricResults are linked by their canonical ``metric_id``, never by
  display name, and are resolved against the supplied metrics document;
* the originating Turn is recorded explicitly (Finding 2.1.0 ``turn_ids``) and
  must agree with the linked events and metrics;
* a suspected cause stays a hypothesis: a named layer always carries
  ``requires_log_verification`` and a non-certain ``attribution_confidence``;
* every emitted Finding is validated with ``finding_errors`` before it is
  published. An invalid Finding is dropped and reported, never written.
"""

import uuid

from .runner import write_json
from .validation import finding_errors, timeline_errors

CURRENT_FINDING_VERSION = '2.1.0'
LEGACY_FINDING_VERSION = '2.0.0'

SEVERITY_MAP = {
    'info': (None, 'observation'),      # no severity, kind=observation
    'low': ('P3', 'defect'),
    'medium': ('P2', 'defect'),
    'high': ('P1', 'defect'),
    'critical': ('P0', 'defect'),
}

#: Attribution taxonomy. A layer outside this set is not a hypothesis the product
#: accepts, so it degrades to an explicit unknown instead of being published.
SUSPECTED_LAYERS = (
    'wake_word', 'vad', 'endpoint', 'asr', 'aec', 'network', 'llm', 'prompt',
    'context', 'memory', 'agent', 'tool', 'tts', 'safety', 'persona', 'unknown',
)


def generate_findings(judge_results, timeline, metrics_result, run_id='RUN-auto'):
    """Backward-compatible entry point returning only the Finding documents."""
    document = generate_findings_document(judge_results, timeline, metrics_result, run_id)
    return document['findings']


def generate_findings_document(judge_results, timeline, metrics_result, run_id=None,
                               analysis_id=None):
    """Build the Finding document set plus an explicit record of what abstained.

    Returns ``{'schema_version', 'run_id', 'analysis_id', 'findings',
    'abstentions', 'rejected'}``. ``abstentions`` lists candidates that could not
    become Findings and why; ``rejected`` lists Findings that failed contract
    validation and were therefore withheld.
    """
    if isinstance(judge_results, dict):
        judge_results = judge_results.get('results') or []
    findings, abstentions, rejected = [], [], []
    timeline_problems = timeline_errors(timeline) if isinstance(timeline, dict) else ['no Timeline supplied']
    if timeline_problems:
        return {
            'schema_version': CURRENT_FINDING_VERSION,
            'run_id': run_id, 'analysis_id': analysis_id,
            'findings': [],
            'abstentions': [{'dimension': 'finding_candidate', 'turn_id': None, 'response_id': None,
                             'state': 'not_eligible',
                             'reason': 'The Timeline is not valid: ' + timeline_problems[0],
                             'invocation_id': None}],
            'rejected': [],
        }
    run_id = timeline.get('run_id') or run_id or 'RUN-auto'
    case_id = timeline.get('case_id')
    execution_kind = timeline.get('execution_kind')
    metrics = [metric for metric in (metrics_result.get('metrics') if isinstance(metrics_result, dict)
                                    else metrics_result) or []
               if isinstance(metric, dict) and metric.get('metric_id')]
    metrics_by_id = {metric['metric_id']: metric for metric in metrics}
    evidence_by_id = {item['evidence_id']: item for item in timeline.get('evidence') or []}
    events_by_id = {item['event_id']: item for item in timeline.get('events') or []}

    candidates = [result for result in judge_results
                  if result.get('dimension') == 'finding_candidate'
                  and result.get('status') == 'observed']

    for candidate in candidates:
        evidence_ids, event_ids = _cited_references(candidate, evidence_by_id, events_by_id)
        if not evidence_ids:
            abstentions.append(_abstention(
                candidate, 'insufficient_evidence',
                'the candidate cites no evidence that resolves in the Timeline'))
            continue
        metric_ids = _linked_metric_ids(candidate, metrics_by_id, events_by_id, event_ids)
        turn_ids = _turn_ids(candidate, event_ids, metric_ids, events_by_id, metrics_by_id)
        if not turn_ids:
            abstentions.append(_abstention(
                candidate, 'insufficient_evidence',
                'the candidate cannot be bound to a Turn through its cited events or metrics'))
            continue
        finding = _build_finding(candidate, run_id, case_id, execution_kind,
                                 evidence_ids, event_ids, metric_ids, turn_ids,
                                 analysis_id=analysis_id)
        problems = finding_errors(finding, timeline, metrics)
        if problems:
            rejected.append({
                'finding_id': finding['finding_id'],
                'dimension': candidate.get('dimension'),
                'turn_id': candidate.get('turn_id'),
                'state': 'invalid',
                'reason': '; '.join(problems),
                'invocation_id': candidate.get('invocation_id'),
            })
            continue
        findings.append(finding)

    return {
        'schema_version': CURRENT_FINDING_VERSION,
        'run_id': run_id,
        'analysis_id': analysis_id,
        'findings': findings,
        'abstentions': abstentions,
        'rejected': rejected,
    }


def _cited_references(candidate, evidence_by_id, events_by_id):
    """Resolve the references the candidate itself cited.

    A selected event contributes its own evidence, which keeps the
    "event evidence must be included in finding evidence_ids" invariant true by
    construction instead of by a later guess.
    """
    evidence_ids, event_ids = [], []
    for reference in candidate.get('event_refs') or ():
        event = events_by_id.get(reference)
        if event is None:
            continue
        event_ids.append(reference)
        evidence_ids.extend(event.get('evidence_ids') or [])
    for reference in candidate.get('evidence_refs') or ():
        evidence_ids.append(reference)
    evidence_ids = [reference for reference in dict.fromkeys(evidence_ids)
                    if reference in evidence_by_id]
    return evidence_ids, list(dict.fromkeys(event_ids))


def _linked_metric_ids(candidate, metrics_by_id, events_by_id, event_ids):
    """Canonical metric_ids that the candidate's own decision is about.

    A metric is linked only when

    * its name matches the finding decision,
    * it actually measured something (`observed`/`pass`/`fail`); a metric that
      abstained cannot support a defect claim, and
    * it belongs to a Turn the candidate's cited events belong to.

    Linking a metric from an unrelated turn would make one turn's defect look
    measured by another turn's numbers.
    """
    decision = candidate.get('decision') or ''
    candidate_turns = {events_by_id[event_id].get('turn_id') for event_id in event_ids
                       if events_by_id[event_id].get('turn_id')}
    linked = []
    for metric_id, metric in metrics_by_id.items():
        if not _metric_relevant(metric, decision):
            continue
        if metric.get('status') not in ('observed', 'pass', 'fail'):
            continue
        if candidate_turns and metric.get('turn_id') not in candidate_turns:
            continue
        linked.append(metric_id)
    return sorted(linked)


def _turn_ids(candidate, event_ids, metric_ids, events_by_id, metrics_by_id):
    """Turns the Finding is actually bound to.

    The candidate's own turn is only accepted when it is also reachable from the
    cited events or linked metrics; otherwise the claim would not be traceable.
    """
    reachable = {events_by_id[event_id].get('turn_id') for event_id in event_ids
                 if events_by_id[event_id].get('turn_id')}
    reachable |= {metrics_by_id[metric_id].get('turn_id') for metric_id in metric_ids
                  if metrics_by_id[metric_id].get('turn_id')}
    claimed = candidate.get('turn_id')
    if claimed and claimed not in reachable:
        return []
    if claimed:
        reachable.add(claimed)
    return sorted(turn for turn in reachable if turn)


def _build_finding(candidate, run_id, case_id, execution_kind, evidence_ids, event_ids,
                   metric_ids, turn_ids, analysis_id=None):
    severity_str = candidate.get('finding_severity') or 'info'
    severity, kind = SEVERITY_MAP.get(severity_str, (None, 'observation'))
    layer = candidate.get('suspected_layer')
    if layer not in SUSPECTED_LAYERS or layer == 'unknown' or layer is None:
        attribution_status, suspected_layers, attribution_confidence = 'unknown', ['unknown'], 0.0
    else:
        attribution_status, suspected_layers = 'suspected', [layer]
        confidence = candidate.get('attribution_confidence')
        if type(confidence) not in (int, float) or not 0 < confidence <= 1:
            confidence = 0.0
        # A hypothesis is never certain: a model self-report of 1.0 is capped, and
        # `requires_log_verification` stays true until device logs exist.
        attribution_confidence = min(float(confidence), 0.99)
    confidence = candidate.get('confidence')
    if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
        confidence = 0.0
    return {
        'schema_version': CURRENT_FINDING_VERSION,
        'finding_id': 'FIND-' + uuid.uuid4().hex[:12],
        'run_id': run_id,
        'analysis_id': analysis_id,
        'case_id': case_id,
        'execution_kind': execution_kind,
        'kind': kind,
        'title': candidate.get('decision') or 'Untitled finding',
        'severity': severity,
        'status': 'needs_verification',
        'description': candidate.get('reason') or 'No description provided',
        'expected_behavior': 'The device response should satisfy the stated criterion',
        'actual_behavior': candidate.get('reason') or 'See description',
        'confidence': confidence,
        'attribution_status': attribution_status,
        'attribution_confidence': attribution_confidence,
        'suspected_layers': suspected_layers,
        'requires_log_verification': attribution_status == 'suspected',
        'evidence_ids': evidence_ids,
        'event_ids': event_ids,
        'metric_ids': metric_ids,
        'turn_ids': turn_ids,
        'human_review': {
            'status': 'required' if (kind == 'defect' or attribution_status == 'suspected')
                      else 'not_required',
            'reviewer': None,
            'reviewed_at': None,
        },
        'origin': 'manual',
        'regression_case_candidate': False,
    }


def _abstention(candidate, state, reason):
    return {
        'dimension': candidate.get('dimension'),
        'turn_id': candidate.get('turn_id'),
        'response_id': candidate.get('response_id'),
        'state': state,
        'reason': reason,
        'invocation_id': candidate.get('invocation_id'),
    }


def migrate_finding_document(finding, to_version=CURRENT_FINDING_VERSION, timeline=None):
    """Re-emit a Finding document under the current contract.

    A 2.0.0 document has no ``turn_ids``. They are resolved from the Timeline when
    one is supplied and the Finding's own linked events/metrics; when they cannot
    be resolved the list is empty rather than guessed, so a migrated document
    never claims a Turn the original evidence does not establish.
    """
    if not isinstance(finding, dict):
        raise ValueError('Finding must be an object')
    version = finding.get('schema_version')
    if version == to_version:
        return dict(finding)
    if to_version != CURRENT_FINDING_VERSION or version != LEGACY_FINDING_VERSION:
        raise ValueError(f'No finding migration from {version!r} to {to_version!r}')
    migrated = dict(finding)
    migrated['schema_version'] = CURRENT_FINDING_VERSION
    migrated.setdefault('turn_ids', _migrated_turn_ids(finding, timeline))
    return migrated


def _migrated_turn_ids(finding, timeline):
    if not isinstance(timeline, dict):
        return []
    events = {item.get('event_id'): item for item in timeline.get('events') or []}
    turns = {events[event_id].get('turn_id') for event_id in finding.get('event_ids') or []
             if event_id in events and events[event_id].get('turn_id')}
    return sorted(turns)


def _metric_relevant(metric, decision):
    """Whether this metric name is what the candidate decision is about."""
    name = metric.get('name', '')
    if 'latency' in decision and 'latency' in name:
        return True
    if 'overlap' in decision and 'overlap' in name:
        return True
    if 'false_endpoint' in decision and 'false_endpoint' in name:
        return True
    return False


def generate_findings_from_files(judge_path, timeline_path, metrics_path,
                                 output=None, run_id='RUN-auto'):
    """Read JSON files and generate findings.

    Returns (findings, output_path_or_none).
    """
    import json
    from pathlib import Path

    judge_data = json.loads(Path(judge_path).read_text(encoding='utf-8'))
    timeline = json.loads(Path(timeline_path).read_text(encoding='utf-8'))
    metrics_result = json.loads(Path(metrics_path).read_text(encoding='utf-8'))

    document = generate_findings_document(judge_data, timeline, metrics_result, run_id)

    out_path = None
    if output is not None:
        out_path = Path(output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_path, document)

    return document['findings'], out_path