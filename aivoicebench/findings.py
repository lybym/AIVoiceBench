"""Finding generation from judge results, metrics, and timeline events.

Converts LLM finding candidates (#10) into Finding 2.0.0 documents with
evidence links, suspected layers, and human-review requirements. Findings
are candidates — not confirmed defects — until human review.

Evidence-first: every finding links to timeline evidence (audio time ranges)
and the events/metrics that triggered it.
"""

import uuid

from .runner import write_json

SEVERITY_MAP = {
    'info': (None, 'observation'),      # no severity, kind=observation
    'low': ('P3', 'defect'),
    'medium': ('P2', 'defect'),
    'high': ('P1', 'defect'),
    'critical': ('P0', 'defect'),
}


def generate_findings(judge_results, timeline, metrics_result, run_id='RUN-auto'):
    """Generate Finding 2.0.0 documents from judge results.

    Only finding_candidate dimension results with status=observed become
    findings. All others are skipped (no_finding → no finding created).

    Returns a list of Finding 2.0.0 dicts.
    """
    findings = []
    evidence = {e['evidence_id']: e for e in timeline.get('evidence', [])}
    events = timeline.get('events', [])
    metrics = metrics_result.get('metrics', [])

    # Collect finding candidates from judge results
    candidates = [r for r in judge_results
                  if r.get('dimension') == 'finding_candidate'
                  and r.get('status') == 'observed']

    for i, candidate in enumerate(candidates):
        severity_str = candidate.get('finding_severity', 'info')
        severity, kind = SEVERITY_MAP.get(severity_str, (None, 'observation'))
        suspected = candidate.get('suspected_layer', 'unknown')

        # Gather evidence from metrics that triggered this finding
        evidence_ids = []
        event_ids = []
        metric_ids = []

        # Link events related to this finding
        for e in events:
            eid = e.get('event_id')
            if eid and _event_relevant(e, candidate):
                event_ids.append(eid)
                evidence_ids.extend(e.get('evidence_ids', []))

        # Link metrics
        for m in metrics:
            if _metric_relevant(m, candidate):
                mid = m.get('name', '')
                if mid:
                    metric_ids.append(mid)

        # Deduplicate
        evidence_ids = list(dict.fromkeys(evidence_ids))
        event_ids = list(dict.fromkeys(event_ids))
        metric_ids = list(dict.fromkeys(metric_ids))

        # Ensure at least one evidence (schema requires minItems: 1)
        if not evidence_ids:
            # Fall back to first evidence in timeline
            if evidence:
                evidence_ids = [next(iter(evidence))]

        if not evidence_ids:
            continue  # Cannot create a finding without evidence

        attribution_status = 'suspected' if suspected != 'unknown' else 'unknown'
        requires_log = candidate.get('requires_log_verification', True)

        finding = {
            'schema_version': '2.0.0',
            'finding_id': 'FIND-' + uuid.uuid4().hex[:12],
            'run_id': run_id,
            'case_id': timeline.get('case_id', 'CASE-auto'),
            'execution_kind': timeline.get('execution_kind', 'imported'),
            'kind': kind,
            'title': candidate.get('decision', 'Untitled finding'),
            'severity': severity,
            'status': 'needs_verification',
            'description': candidate.get('reason', 'No description provided'),
            'expected_behavior': 'AI device should respond correctly within acceptable latency and without errors',
            'actual_behavior': candidate.get('reason', 'See description'),
            'confidence': candidate.get('confidence', 0.5),
            'attribution_status': attribution_status,
            'attribution_confidence': candidate.get('attribution_confidence', 0.0 if attribution_status == 'unknown' else 0.4),
            'suspected_layers': [suspected],
            'requires_log_verification': requires_log,
            'evidence_ids': evidence_ids,
            'event_ids': event_ids,
            'metric_ids': metric_ids,
            'human_review': {
                'status': 'required' if kind == 'defect' else 'not_required',
                'reviewer': None,
                'reviewed_at': None,
            },
            'origin': 'manual',
            'regression_case_candidate': False,
        }
        findings.append(finding)

    return findings


def _event_relevant(event, candidate):
    """Check if a timeline event is relevant to a finding candidate."""
    decision = candidate.get('decision', '')
    etype = event.get('type', '')

    if 'false_endpoint' in decision and etype == 'possible_false_endpoint':
        return True
    if 'overlap' in decision and 'overlap' in etype:
        return True
    if 'latency' in decision and etype in ('tester_speech_end', 'device_speech_start', 'response_start'):
        return True
    if 'interrupt' in decision and 'interrupt' in etype:
        return True
    return False


def _metric_relevant(metric, candidate):
    """Check if a metric is relevant to a finding candidate."""
    decision = candidate.get('decision', '')
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

    # Handle both {results: [...]} and [...] formats
    if isinstance(judge_data, dict) and 'results' in judge_data:
        judge_results = judge_data['results']
    elif isinstance(judge_data, list):
        judge_results = judge_data
    else:
        judge_results = [judge_data]

    findings = generate_findings(judge_results, timeline, metrics_result, run_id)

    out_path = None
    if output is not None:
        out_path = Path(output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_path, {'schema_version': '1.0.0', 'findings': findings})

    return findings, out_path
