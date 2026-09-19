"""Evidence Workbench projection for the Recording Analysis Web/CLI workbench.

PRD-F013/F014 with PRD-N001/N002/N006. The browser Evidence Workbench renders
waveform, Regions and Timeline with wavesurfer.js, but it is a *reviewer
interface*: every coordinate it draws must come from persisted AIVoiceBench
evidence, and it must never become a measurement producer.

This module is the single place that turns a Run's current AnalysisRevision into
that reviewer-facing document. It is deliberately a **projection**, not a
recomputation:

- acoustic/speaker/turn/event regions are the persisted documents verbatim;
- a metric or finding region is the envelope of the evidence intervals that the
  metric/finding itself references (``evidence_ids`` → ``event_ids`` →
  ``turn_ids``, in that fixed precedence). That is coordinate resolution over
  stored evidence, never a re-derivation of a value;
- no metric value, event, turn or role is computed here, and a value that the
  pipeline abstained on stays ``null`` so the UI can show ``N/A`` instead of a
  fabricated zero;
- when the manual speaker-role gate is not ``complete_review`` the
  role-dependent tracks are **omitted** and listed under ``unavailable``, so the
  workbench cannot present an unconfirmed role as a result.

The document is fully deterministic for a given revision (``document_id`` is a
hash of the revision's region identity), so re-reading a Run never produces a
different workbench and no UUID is invented per request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = '1.0.0'

#: Reviewer tracks, in draw order, with the evidence class each one belongs to.
#: ``deterministic`` is the Canonical engine's evidence, ``semantic`` is the
#: structured Judge's, and a human role decision is reported per region through
#: ``role_basis`` rather than by inventing a fourth track.
TRACKS = (
    ('acoustic', '声学边界', 'deterministic'),
    ('speaker', '说话人聚类', 'deterministic'),
    ('turn', '对话轮次', 'deterministic'),
    ('event', '事件时间线', 'deterministic'),
    ('metric', '指标证据', 'deterministic'),
    ('finding', 'Findings', 'semantic'),
)
TRACK_LABELS = {track_id: (label, evidence_class) for track_id, label, evidence_class in TRACKS}

#: Tracks whose meaning depends on a confirmed tester/device role. They are only
#: published once the user's manual role decision set is complete.
ROLE_DEPENDENT_TRACKS = ('turn', 'metric', 'finding')

#: Reporting artifacts describe the evidence chain; they are not part of it. They
#: are excluded from provenance so that a report can never list itself as its own
#: source — which would also make re-writing a revision's report change its bytes.
REPORT_ARTIFACT_KINDS = ('report_json', 'report_markdown')

#: Non-secret fields of a provider invocation that may enter the workbench. The
#: invocation document's ``config``/``endpoint`` never do: the browser must not
#: receive request parameters or anything that could carry a credential.
_INVOCATION_FIELDS = ('invocation_id', 'operation_id', 'attempt', 'provider', 'model',
                      'processor_version', 'api_version', 'prompt_version', 'status',
                      'failure_code', 'latency_ms', 'started_at', 'finished_at')

#: Non-secret fields of the Run's model configuration snapshot.
#: ``credential_env`` is deliberately excluded: an env-var reference is not a
#: secret value, but a browser-facing document has no reason to carry one.
_ROUTE_FIELDS = ('provider', 'model', 'protocol', 'enabled')


def _read(path):
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_list(path):
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def analysis_root(directory):
    """Resolve the Run's current AnalysisRevision directory.

    Mirrors the resolution the API and the report writer use: a unified
    ``recording_import`` Run keeps every AnalysisRevision under
    ``analysis/<analysis_id>``, while a legacy ``web-analysis`` Run has a single
    flat directory. Returns ``(root, manifest, unified)``.
    """
    directory = Path(directory)
    manifest = _read(directory / 'manifest.json')
    if manifest.get('workflow') == 'recording_import' and not (directory / 'web-analysis').exists():
        return directory / 'analysis' / str(manifest.get('analysis_id') or ''), manifest, True
    if (directory / 'web-analysis').is_dir():
        return directory / 'web-analysis', manifest, False
    return directory, manifest, False


def read_revision_document(root, name, unified):
    """Return ``(envelope, data)`` for a registered document.

    A stage envelope carries its payload in ``data``; a separately published
    document (alignment, role review, report) *is* its own payload. Distinguishing
    them by the envelope's ``kind`` member keeps a published document from being
    silently unwrapped into an empty dict.
    """
    root = Path(root)
    if name.endswith('.json'):
        document = _read(root / name)
    else:
        document = _read(root / f'{name}.json')
    if not document:
        return {}, {}
    if unified and 'kind' in document:
        data = document.get('data')
        return document, data if isinstance(data, dict) else {}
    return document, document


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _span(item):
    """Persisted audio-relative interval of one evidence item, or ``None``.

    A single instant (``end_ms == start_ms``) is a legitimate zero-length region;
    a reversed or non-numeric interval is not evidence and is skipped rather than
    repaired.
    """
    if not isinstance(item, dict):
        return None
    start = _number(item.get('start_ms'))
    end = _number(item.get('end_ms'))
    if start is None or end is None or end < start:
        return None
    return start, end


def _processor(value):
    """Normalise a document ``processor`` member without inventing one."""
    if isinstance(value, dict):
        return {'name': value.get('name'), 'version': value.get('version')}
    if isinstance(value, str):
        name, _, version = value.partition(':')
        return {'name': name, 'version': version or None}
    return None


def _envelope_of(spans):
    """The single interval covering every referenced evidence span."""
    return min(start for start, _ in spans), max(end for _, end in spans)


class _RegionBuilder:
    """Accumulates regions and the evidence index they are resolved against."""

    def __init__(self, gate_complete):
        self.gate_complete = gate_complete
        self.regions = []
        self._by_id = {}

    def add(self, region):
        if region['region_id'] in self._by_id:
            raise ValueError('Duplicate workbench region id: ' + region['region_id'])
        self._by_id[region['region_id']] = region
        self.regions.append(region)
        return region['region_id']

    def resolved_region(self, region_id):
        return self._by_id.get(region_id)


def _region(track_id, region_id, kind, label, span, *, detail, source, role=None,
            role_basis=None, status=None, confidence=None, uncertainty=None,
            uncertain=False, envelope_of=None, provisional=False):
    label_track, evidence_class = TRACK_LABELS[track_id]
    start, end = span
    return {
        'region_id': region_id,
        'track_id': track_id,
        'kind': kind,
        'label': label,
        'start_ms': start,
        'end_ms': end,
        # Render-ready seconds: a unit conversion of the persisted milliseconds,
        # performed once here so the browser never re-derives a boundary.
        'start_sec': start / 1000.0,
        'end_sec': end / 1000.0,
        'role': role,
        'role_basis': role_basis,
        'evidence_class': evidence_class,
        'status': status,
        'confidence': confidence,
        'uncertainty': uncertainty,
        'uncertain': bool(uncertain),
        'provisional': bool(provisional),
        'envelope_of': envelope_of,
        'source': source,
        'detail': detail,
    }


def _confidence(value):
    number = _number(value)
    return number


def build_workbench(directory):
    """Project one Run's current AnalysisRevision into the reviewer document."""
    from .alignment import explain_metric_gap
    from .role_review import build_role_review

    directory = Path(directory).resolve()
    root, manifest, unified = analysis_root(directory)
    run_id = manifest.get('run_id') or directory.name
    analysis_id = manifest.get('analysis_id')

    role_review = read_revision_document(root, 'role-review', unified)[1] or build_role_review(directory)
    gate_status = role_review.get('status') or 'awaiting_role_review'
    gate_reason = role_review.get('reason') or 'Role review state is unavailable for this Run'
    gate_complete = gate_status == 'complete_review'
    decisions = dict(((role_review.get('revision') or {}).get('decisions')) or {})

    fused = read_revision_document(root, 'fused-segments', unified)[1]
    alignment = read_revision_document(root, 'alignment', unified)[1]
    transcript = read_revision_document(root, 'transcript', unified)[1]
    turns_doc = read_revision_document(root, 'turns', unified)[1]
    timeline = read_revision_document(root, 'timeline', unified)[1]
    metrics_doc = read_revision_document(root, 'metrics', unified)[1]
    findings_doc = read_revision_document(root, 'findings', unified)[1]

    builder = _RegionBuilder(gate_complete)
    evidence_spans = {}
    event_spans = {}
    turn_spans = {}
    turn_regions = {}

    # --- deterministic evidence regions -------------------------------------
    acoustic_envelope, acoustic = read_revision_document(root, 'acoustic-segments', unified)
    acoustic_processor = _processor(acoustic_envelope.get('processor'))
    for index, segment in enumerate(acoustic.get('segments') or []):
        span = _span(segment)
        if span is None:
            continue
        segment_id = str(segment.get('segment_id') or f'#{index}')
        builder.add(_region(
            'acoustic', f'acoustic:{segment_id}', 'acoustic_segment',
            str(segment.get('segment_id') or f'acoustic {index + 1}'), span,
            detail=segment, status=segment.get('status'),
            confidence=_confidence(segment.get('confidence')),
            source={'document': 'acoustic-segments.json', 'document_id': segment.get('document_id'),
                    'processor': acoustic_processor, 'artifact_ref': None,
                    'evidence_ids': [], 'event_ids': [], 'metric_ids': [], 'turn_ids': []}))

    diarization_envelope, diarization = read_revision_document(root, 'speaker-assignments', unified)
    diarization_processor = _processor(diarization_envelope.get('processor'))
    for index, speaker in enumerate(diarization.get('speaker_segments') or []):
        span = _span(speaker)
        if span is None:
            continue
        speaker_id = str(speaker.get('speaker_id') or f'#{index}')
        # A cluster boundary is machine (deterministic) evidence; the *role* is a
        # human decision, so it is reported separately and never guessed.
        role = decisions.get(speaker_id) if gate_complete else None
        builder.add(_region(
            'speaker', f'speaker:{speaker_id}:{index}', 'speaker_segment',
            f'{speaker_id} · {speaker.get("native_speaker_id") or "未标注"}', span,
            detail=speaker, role=role,
            role_basis='user_review' if role else None,
            status=speaker.get('status') or 'observed',
            confidence=_confidence(speaker.get('speaker_confidence')),
            uncertainty=None if role else '说话人角色尚未由用户确认',
            uncertain=role in (None, 'unknown'),
            provisional=not gate_complete,
            source={'document': 'speaker-assignments.json',
                    'document_id': diarization_envelope.get('document_id') or diarization.get('document_id'),
                    'processor': diarization_processor, 'artifact_ref': None,
                    'evidence_ids': [], 'event_ids': [], 'metric_ids': [], 'turn_ids': []}))

    for index, segment in enumerate(timeline.get('evidence') or []):
        span = _span(segment)
        if span is None:
            continue
        evidence_id = str(segment.get('evidence_id') or f'#{index}')
        evidence_spans[evidence_id] = span

    for index, event in enumerate(timeline.get('events') or []):
        span = _span(event)
        if span is None:
            continue
        event_id = str(event.get('event_id') or f'#{index}')
        event_spans[event_id] = span
        builder.add(_region(
            'event', f'event:{event_id}', 'event', str(event.get('type') or event_id), span,
            detail=event, status=event.get('status') or 'observed',
            confidence=_confidence(event.get('confidence')),
            source={'document': 'timeline.json', 'document_id': timeline.get('document_id'),
                    'processor': _processor(timeline.get('processor')), 'artifact_ref': None,
                    'evidence_ids': list(event.get('evidence_ids') or []),
                    'event_ids': [event_id], 'metric_ids': [], 'turn_ids': []}))

    for index, turn in enumerate(turns_doc.get('turns') or []):
        turn_id = str(turn.get('turn_id') or f'#{index}')
        ends = [value for value in (
            _number(turn.get('tester_speech_start_ms')), _number(turn.get('tester_speech_end_ms')),
            _number(turn.get('device_speech_start_ms')), _number(turn.get('device_speech_end_ms')))
            if value is not None]
        span = _span(turn)
        if span is None and ends:
            span = (min(ends), max(ends))
        if span is None:
            continue
        turn_spans[turn_id] = span
        if 'turn' in ROLE_DEPENDENT_TRACKS and not gate_complete:
            continue
        turn_regions[turn_id] = builder.add(_region(
            'turn', f'turn:{turn_id}', 'turn', turn_id, span,
            detail=turn, status=turn.get('status') or 'observed',
            source={'document': 'turns.json', 'document_id': turns_doc.get('document_id'),
                    'processor': _processor(turns_doc.get('processor')), 'artifact_ref': None,
                    'evidence_ids': [], 'event_ids': [], 'metric_ids': [], 'turn_ids': [turn_id]}))

    # --- deterministic / semantic regions that resolve their own span --------
    def resolve_ids(ids, index):
        return [index[item] for item in ids if item in index]

    metric_regions = {}
    metrics = []
    for index, metric in enumerate(metrics_doc.get('metrics') or []):
        metric_id = str(metric.get('metric_id') or f'#{index}')
        spans = resolve_ids(metric.get('evidence_ids') or [], evidence_spans)
        origin = 'evidence'
        if not spans:
            spans = resolve_ids(metric.get('event_ids') or [], event_spans)
            origin = 'event'
        if not spans and metric.get('turn_id') in turn_spans:
            spans = [turn_spans[metric['turn_id']]]
            origin = 'turn'
        region_id = None
        if spans and gate_complete:
            span = _envelope_of(spans)
            region_id = builder.add(_region(
                'metric', f'metric:{metric_id}', 'metric',
                f'{metric.get("name") or metric_id} = {metric.get("value")}', span,
                detail=metric, status=metric.get('status'),
                confidence=_confidence(metric.get('confidence')),
                uncertainty=metric.get('reason') if metric.get('value') is None else None,
                uncertain=metric.get('value') is None,
                envelope_of=len(spans),
                source={'document': 'metrics.json', 'document_id': metrics_doc.get('document_id'),
                        'processor': _processor(metrics_doc.get('processor')), 'artifact_ref': None,
                        'evidence_ids': list(metric.get('evidence_ids') or []),
                        'event_ids': list(metric.get('event_ids') or []),
                        'metric_ids': [metric_id],
                        'turn_ids': [metric['turn_id']] if metric.get('turn_id') else []}))
        metric_regions[metric_id] = region_id
        metrics.append({
            'metric_id': metric_id, 'name': metric.get('name'), 'value': metric.get('value'),
            'unit': metric.get('unit'), 'status': metric.get('status'), 'reason': metric.get('reason'),
            'turn_id': metric.get('turn_id'), 'region_id': region_id, 'span_origin': origin,
            'evidence_ids': list(metric.get('evidence_ids') or []),
            'provisional': not gate_complete,
        })

    findings = []
    for index, finding in enumerate(findings_doc.get('findings') or []):
        finding_id = str(finding.get('finding_id') or f'#{index}')
        spans = resolve_ids(finding.get('evidence_ids') or [], evidence_spans)
        origin = 'evidence'
        if not spans:
            spans = resolve_ids(finding.get('event_ids') or [], event_spans)
            origin = 'event'
        if not spans:
            for metric_id in finding.get('metric_ids') or []:
                region = builder.resolved_region(metric_regions.get(str(metric_id)) or '')
                if region is not None:
                    spans.append((region['start_ms'], region['end_ms']))
            if spans:
                origin = 'metric'
        if not spans:
            spans = resolve_ids(finding.get('turn_ids') or [], turn_spans)
            if spans:
                origin = 'turn'
        region_id = None
        if spans and gate_complete:
            span = _envelope_of(spans)
            region_id = builder.add(_region(
                'finding', f'finding:{finding_id}', 'finding',
                str(finding.get('title') or finding_id), span,
                detail=finding, status=finding.get('status'),
                confidence=_confidence(finding.get('confidence')),
                uncertainty=finding.get('attribution_status') if finding.get('confidence') is None else None,
                uncertain=bool(finding.get('requires_log_verification')),
                envelope_of=len(spans),
                source={'document': 'findings.json', 'document_id': findings_doc.get('document_id'),
                        'processor': _processor(findings_doc.get('processor')), 'artifact_ref': None,
                        'evidence_ids': list(finding.get('evidence_ids') or []),
                        'event_ids': list(finding.get('event_ids') or []),
                        'metric_ids': list(finding.get('metric_ids') or []),
                        'turn_ids': list(finding.get('turn_ids') or [])}))
        findings.append({
            'finding_id': finding_id, 'title': finding.get('title'),
            'severity': finding.get('severity'), 'status': finding.get('status'),
            'confidence': finding.get('confidence'), 'description': finding.get('description'),
            'suspected_layers': list(finding.get('suspected_layers') or []),
            'requires_log_verification': bool(finding.get('requires_log_verification')),
            'human_review': finding.get('human_review') or {},
            'region_id': region_id, 'span_origin': origin,
            'evidence_ids': list(finding.get('evidence_ids') or []),
            'event_ids': list(finding.get('event_ids') or []),
            'metric_ids': list(finding.get('metric_ids') or []),
            'turn_ids': list(finding.get('turn_ids') or []),
            'provisional': not gate_complete,
        })

    # --- availability, abstentions and provenance ---------------------------
    stages = manifest.get('stages') or {}
    unavailable = [{'stage': name, 'status': stage.get('status'), 'reason': stage.get('reason')}
                   for name, stage in sorted(stages.items())
                   if name != 'report' and stage.get('status') != 'complete']
    if not gate_complete:
        for track_id in ROLE_DEPENDENT_TRACKS:
            unavailable.append({
                'stage': track_id, 'status': 'insufficient_evidence',
                'reason': f'人工角色确认未完成（{gate_status}）：{gate_reason}'})

    artifacts = [item for item in (manifest.get('artifacts') or [])
                 if item.get('kind') not in REPORT_ARTIFACT_KINDS]
    invocations = []
    for artifact in artifacts:
        if artifact.get('kind') != 'provider_invocation':
            continue
        document = read_revision_document(directory, artifact['path'], False)[0]
        payload = document.get('data') if 'kind' in document else document
        payload = payload if isinstance(payload, dict) else {}
        invocations.append({key: payload.get(key) for key in _INVOCATION_FIELDS
                            if key in payload} | {'artifact_ref': artifact.get('artifact_id')})
    configuration = next((artifact for artifact in artifacts
                          if artifact.get('kind') == 'model_configuration'
                          and str(artifact.get('path', '')).startswith(
                              (Path('analysis') / str(analysis_id)).as_posix())), None)
    routes = {}
    if configuration is not None:
        snapshot = _read(directory / configuration['path'])
        for route, value in (snapshot.get('routes') or {}).items():
            if isinstance(value, dict):
                routes[route] = {key: value.get(key) for key in _ROUTE_FIELDS if key in value}

    audio_artifact = next((item for item in artifacts if item.get('kind') == 'normalized_audio'), None)
    metadata = next((item for item in artifacts if item.get('kind') == 'audio_metadata'), None)
    normalized = _read(directory / metadata['path']).get('normalized') if metadata else {}
    normalized = normalized if isinstance(normalized, dict) else {}
    source = fused.get('source') if isinstance(fused.get('source'), dict) else {}
    duration_ms = _number(normalized.get('duration_ms'))
    if duration_ms is None:
        duration_ms = _number(source.get('duration_ms'))
    sample_rate = normalized.get('sample_rate') if isinstance(normalized.get('sample_rate'), int) else None
    if sample_rate is None:
        sample_rate = source.get('sample_rate') if isinstance(source.get('sample_rate'), int) else None

    metrics_gap = explain_metric_gap(
        {'segments': fused.get('segments') or []}, timeline, {'metrics': metrics_doc.get('metrics') or []},
        alignment)

    revision = role_review.get('revision') or None
    document_basis = json.dumps([region['region_id'] for region in builder.regions],
                                ensure_ascii=False, sort_keys=True)
    document_id = 'WORKBENCH-' + hashlib.sha256(
        f'{analysis_id}|{document_basis}'.encode('utf-8')).hexdigest()[:16]

    return {
        'schema_version': SCHEMA_VERSION,
        'document_id': document_id,
        'run_id': run_id,
        'analysis_id': analysis_id,
        'gate': {
            'status': gate_status,
            'reason': gate_reason,
            'role_dependent_available': gate_complete,
            'view_kind': 'role_confirmed' if gate_complete else 'provisional',
        },
        'revision': None if revision is None else {
            'analysis_id': revision.get('analysis_id'),
            'revision_index': revision.get('revision_index'),
            'revision_id': revision.get('revision_id'),
            'reviewer': revision.get('reviewer'),
            'reason': revision.get('reason'),
            'created_at': revision.get('created_at'),
            'mapping_sha256': revision.get('mapping_sha256'),
            'previous_revision_ref': revision.get('previous_revision_ref'),
            'decisions': revision.get('decisions') or {},
        },
        'revision_history': role_review.get('history') or [],
        'revision_diff': role_review.get('diff') or {},
        'audio': {
            'artifact_id': (audio_artifact or {}).get('artifact_id'),
            'kind': (audio_artifact or {}).get('kind'),
            'sha256': (audio_artifact or {}).get('sha256'),
            'duration_ms': duration_ms,
            'sample_rate': sample_rate,
            'url': f'/api/runs/{run_id}/audio',
        },
        'tracks': [{'track_id': track_id, 'label': label, 'evidence_class': evidence_class}
                   for track_id, label, evidence_class in TRACKS
                   if track_id not in ROLE_DEPENDENT_TRACKS or gate_complete],
        'regions': builder.regions,
        'metrics': metrics,
        'findings': findings,
        'events': [{'event_id': event.get('event_id'), 'type': event.get('type'),
                    'start_ms': _number(event.get('start_ms')), 'end_ms': _number(event.get('end_ms')),
                    'source': event.get('source'), 'confidence': _confidence(event.get('confidence')),
                    'turn_id': event.get('turn_id'),
                    'evidence_ids': list(event.get('evidence_ids') or []),
                    'region_id': next((region['region_id'] for region in builder.regions
                                       if region['region_id'] == f'event:{event.get("event_id")}'), None)}
                   for event in (timeline.get('events') or [])],
        'turns': [{'turn_id': turn.get('turn_id'), 'region_id': turn_regions.get(str(turn.get('turn_id'))),
                   'has_interruption': bool(turn.get('has_interruption')),
                   'has_overlap': bool(turn.get('has_overlap')),
                   'start_ms': turn_spans.get(str(turn.get('turn_id')), (None, None))[0],
                   'end_ms': turn_spans.get(str(turn.get('turn_id')), (None, None))[1]}
                  for turn in (turns_doc.get('turns') or [])],
        'transcript': [{'segment_id': segment.get('segment_id'),
                        'start_ms': _number(segment.get('start_ms')),
                        'end_ms': _number(segment.get('end_ms')),
                        'text': segment.get('text'),
                        'speaker_id': segment.get('speaker_id'),
                        'speaker_role': segment.get('speaker_role'),
                        'timestamp_source': segment.get('timestamp_source')}
                       for segment in (transcript.get('segments') or [])],
        'unavailable': unavailable,
        'abstentions': {
            'metrics_gap': metrics_gap,
            'findings': list(findings_doc.get('abstentions') or []),
            'rejected_findings': list(findings_doc.get('rejected') or []),
            'stages': [item for item in unavailable if item['stage'] in (manifest.get('stages') or {})],
        },
        'provenance': {
            'run_id': run_id,
            'analysis_id': analysis_id,
            'artifacts': [{'artifact_id': item.get('artifact_id'), 'kind': item.get('kind'),
                           'sha256': item.get('sha256'), 'processor': item.get('processor')}
                          for item in artifacts],
            'provider_invocations': invocations,
            'model_configuration': {'artifact_ref': (configuration or {}).get('artifact_id'),
                                    'routes': routes},
            'policies': [value for value in (
                {'document': 'alignment.json', 'policy': alignment.get('policy')} if alignment.get('policy') else None,
                {'document': 'speaker-assignments.json', 'policy': diarization.get('scope', {}).get('policy')}
                if isinstance(diarization.get('scope'), dict) and diarization['scope'].get('policy') else None,
            ) if value],
            'processors': sorted({str(item.get('processor')) for item in artifacts
                                  if item.get('processor')}),
        },
    }
