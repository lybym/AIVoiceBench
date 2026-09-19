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
  workbench cannot present an unconfirmed role as a result. Their resolved
  geometry is still published per record as ``resolved_span`` for audit, but no
  region is created, so the browser cannot navigate to it;
- a transcript segment's ``region_id`` is resolved from the persisted *fusion*
  cross-reference (``asr_segment_id`` → ``acoustic_segment_id``), because ASR
  utterance ids and acoustic segment ids are independent evidence namespaces;
- a document that exists but cannot be read is published as an explicit
  ``unreadable`` gap in ``unavailable`` and reflected in ``evidence_integrity``,
  never silently degraded into "this evidence is empty";
- references a metric or finding declared but that no persisted evidence
  satisfies are reported under ``abstentions.unresolved_references``.

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

#: The persisted documents this projection consumes. They are what "this Run has a
#: reviewable evidence chain" means, so the absence of *all* of them is an absent
#: workbench rather than an empty one.
EVIDENCE_DOCUMENTS = (
    'acoustic-segments', 'speaker-assignments', 'timeline', 'turns', 'metrics',
    'findings', 'transcript', 'fused-segments', 'alignment', 'role-review',
)

#: Manifest stage statuses that mean "the stage was never attempted", as opposed to
#: a stage that ran and abstained or failed. The two are reported separately: a
#: reviewer must be able to tell "not needed yet" from "expected but missing".
STAGE_NOT_RUN = ('pending',)
STAGE_FAILED = ('failed', 'error', 'failure')


def _stage_kind(status):
    if status in STAGE_NOT_RUN:
        return 'not_run'
    if status in STAGE_FAILED:
        return 'failed'
    return 'incomplete'

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


#: Read outcomes. "A stage produced nothing" and "the document cannot be read"
#: must never collapse into the same value: the first is a legitimate empty stage,
#: the second is a broken evidence chain a reviewer has to be told about.
READ_OK = 'ok'
READ_MISSING = 'missing'
READ_UNREADABLE = 'unreadable'


def _read_checked(path):
    """Return ``(payload, status)`` for one JSON object document."""
    path = Path(path)
    if not path.is_file():
        return {}, READ_MISSING
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}, READ_UNREADABLE
    if not isinstance(payload, dict):
        return {}, READ_UNREADABLE
    return payload, READ_OK


def _read(path):
    return _read_checked(path)[0]


def analysis_root(directory):
    """Resolve the Run's current AnalysisRevision directory.

    Mirrors the resolution the API and the report writer use: a unified
    ``recording_import`` Run keeps every AnalysisRevision under
    ``analysis/<analysis_id>``, while a legacy ``web-analysis`` Run has a single
    flat directory. Returns ``(root, manifest, unified)``.

    A directory that carries no readable ``manifest.json`` is **not** a Run with an
    empty workbench: ``ValueError`` is raised so the endpoint can answer 404 instead
    of serving a fabricated "three tracks, provisional view" document for a path
    that never held evidence. The same applies to a unified manifest without an
    ``analysis_id`` or without the AnalysisRevision directory it points at.
    """
    directory = Path(directory)
    manifest, status = _read_checked(directory / 'manifest.json')
    if status != READ_OK:
        raise ValueError(f'Run manifest is {status}: no evidence to project')
    if manifest.get('workflow') == 'recording_import' and not (directory / 'web-analysis').exists():
        analysis_id = str(manifest.get('analysis_id') or '')
        if not analysis_id:
            raise ValueError('Run manifest carries no analysis_id')
        root = directory / 'analysis' / analysis_id
        if not root.is_dir():
            raise ValueError('AnalysisRevision directory is missing')
        return root, manifest, True
    if (directory / 'web-analysis').is_dir():
        return directory / 'web-analysis', manifest, False
    return directory, manifest, False


def read_revision_document_checked(root, name, unified):
    """Return ``(envelope, data, status)`` for a registered document.

    A stage envelope carries its payload in ``data``; a separately published
    document (alignment, role review, report) *is* its own payload. Distinguishing
    them by the envelope's ``kind`` member keeps a published document from being
    silently unwrapped into an empty dict.

    ``status`` separates an absent document (a stage that produced nothing) from one
    that exists but cannot be parsed, so the caller can publish the second as an
    explicit gap instead of an empty evidence set.
    """
    root = Path(root)
    filename = name if name.endswith('.json') else f'{name}.json'
    document, status = _read_checked(root / filename)
    if status != READ_OK:
        return {}, {}, status
    if unified and 'kind' in document:
        data = document.get('data')
        return document, data if isinstance(data, dict) else {}, READ_OK
    return document, document, READ_OK


def read_revision_document(root, name, unified):
    """Return ``(envelope, data)`` for a registered document, ignoring its status."""
    envelope, data, _ = read_revision_document_checked(root, name, unified)
    return envelope, data


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

    # A document's processor is recorded on the *artifact* that published it, not on
    # the stage envelope: `analysis-output.schema.json` is `additionalProperties:
    # false` and an envelope has no `processor` member, so reading it from the
    # envelope can only ever yield null.
    processors_by_document = {}
    for artifact in (manifest.get('artifacts') or []):
        filename = Path(str(artifact.get('path') or '')).name
        if filename and artifact.get('processor'):
            processors_by_document.setdefault(filename, artifact['processor'])

    def document_processor(name):
        return _processor(processors_by_document.get(name))

    present = []
    unreadable = []

    def load(name):
        """Read one revision document, recording its integrity for the projection."""
        envelope, data, status = read_revision_document_checked(root, name, unified)
        if status == READ_OK:
            present.append(name)
        elif status == READ_UNREADABLE:
            unreadable.append(name)
        return envelope, data

    role_review = load('role-review')[1] or build_role_review(directory)
    gate_status = role_review.get('status') or 'awaiting_role_review'
    gate_reason = role_review.get('reason') or 'Role review state is unavailable for this Run'
    gate_complete = gate_status == 'complete_review'
    decisions = dict(((role_review.get('revision') or {}).get('decisions')) or {})

    fused = load('fused-segments')[1]
    alignment = load('alignment')[1]
    transcript = load('transcript')[1]
    turns_doc = load('turns')[1]
    timeline = load('timeline')[1]
    metrics_doc = load('metrics')[1]
    findings_doc = load('findings')[1]

    if not present and not (manifest.get('artifacts') or []):
        # A Run whose directory holds neither evidence nor a single registered artifact
        # has nothing to review: answering 404 is what the endpoint contract promises,
        # and an empty document would instead read as "reviewed, found nothing".
        raise ValueError('Run carries no readable evidence and no registered artifacts')

    builder = _RegionBuilder(gate_complete)
    evidence_spans = {}
    event_spans = {}
    turn_spans = {}
    turn_regions = {}

    # --- deterministic evidence regions -------------------------------------
    acoustic_envelope, acoustic = load('acoustic-segments')
    acoustic_processor = document_processor('acoustic-segments.json')
    acoustic_region_by_segment = {}
    for index, segment in enumerate(acoustic.get('segments') or []):
        span = _span(segment)
        if span is None:
            continue
        segment_id = str(segment.get('segment_id') or f'#{index}')
        acoustic_region_by_segment[segment_id] = builder.add(_region(
            'acoustic', f'acoustic:{segment_id}', 'acoustic_segment',
            str(segment.get('segment_id') or f'acoustic {index + 1}'), span,
            detail=segment, status=segment.get('status'),
            confidence=_confidence(segment.get('confidence')),
            source={'document': 'acoustic-segments.json', 'document_id': segment.get('document_id'),
                    'processor': acoustic_processor, 'artifact_ref': None,
                    'evidence_ids': [], 'event_ids': [], 'metric_ids': [], 'turn_ids': []}))

    diarization_envelope, diarization = load('speaker-assignments')
    diarization_processor = document_processor('speaker-assignments.json')
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
                    'processor': document_processor('timeline.json'), 'artifact_ref': None,
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
                    'processor': document_processor('turns.json'), 'artifact_ref': None,
                    'evidence_ids': [], 'event_ids': [], 'metric_ids': [], 'turn_ids': [turn_id]}))

    # --- deterministic / semantic regions that resolve their own span --------
    def resolve_ids(ids, index):
        return [index[item] for item in ids if item in index]

    #: References a persisted record declared but that no published evidence
    #: satisfies. They are reported as an explicit gap: silently dropping them would
    #: let a record appear to rest on evidence it never actually cited.
    unresolved_refs = []

    def declared_gaps(record_kind, record_id, references):
        for field, ids, index in references:
            missing = [item for item in ids if item not in index]
            if missing:
                unresolved_refs.append({
                    'record': record_kind, 'record_id': record_id, 'field': field,
                    'references': missing,
                    'reason': f'{record_kind} {record_id} cites {field} entries that are not in this '
                              'revision; they cannot be drawn or verified',
                })

    def resolved_span(span):
        if span is None:
            return None
        return {'start_ms': span[0], 'end_ms': span[1],
                'start_sec': span[0] / 1000.0, 'end_sec': span[1] / 1000.0}

    metric_regions = {}
    metrics = []
    for index, metric in enumerate(metrics_doc.get('metrics') or []):
        metric_id = str(metric.get('metric_id') or f'#{index}')
        declared_gaps('metric', metric_id, (
            ('evidence_ids', list(metric.get('evidence_ids') or []), evidence_spans),
            ('event_ids', list(metric.get('event_ids') or []), event_spans)))
        spans = resolve_ids(metric.get('evidence_ids') or [], evidence_spans)
        origin = 'evidence'
        if not spans:
            spans = resolve_ids(metric.get('event_ids') or [], event_spans)
            origin = 'event'
        if not spans and metric.get('turn_id') in turn_spans:
            spans = [turn_spans[metric['turn_id']]]
            origin = 'turn'
        envelope = _envelope_of(spans) if spans else None
        region_id = None
        if spans and gate_complete:
            span = envelope
            region_id = builder.add(_region(
                'metric', f'metric:{metric_id}', 'metric',
                f'{metric.get("name") or metric_id} = {metric.get("value")}', span,
                detail=metric, status=metric.get('status'),
                confidence=_confidence(metric.get('confidence')),
                uncertainty=metric.get('reason') if metric.get('value') is None else None,
                uncertain=metric.get('value') is None,
                envelope_of=len(spans),
                source={'document': 'metrics.json', 'document_id': metrics_doc.get('document_id'),
                        'processor': document_processor('metrics.json'), 'artifact_ref': None,
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
            # The interval the metric's own references resolve to, published even
            # while the role gate withholds the region: a reviewer can audit the
            # resolved geometry without the browser being able to navigate to a
            # role-dependent interval that is not yet confirmed.
            'resolved_span': None if gate_complete else resolved_span(envelope),
        })

    findings = []
    for index, finding in enumerate(findings_doc.get('findings') or []):
        finding_id = str(finding.get('finding_id') or f'#{index}')
        declared_gaps('finding', finding_id, (
            ('evidence_ids', list(finding.get('evidence_ids') or []), evidence_spans),
            ('event_ids', list(finding.get('event_ids') or []), event_spans),
            ('turn_ids', list(finding.get('turn_ids') or []), turn_spans)))
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
        envelope = _envelope_of(spans) if spans else None
        region_id = None
        if spans and gate_complete:
            span = envelope
            region_id = builder.add(_region(
                'finding', f'finding:{finding_id}', 'finding',
                str(finding.get('title') or finding_id), span,
                detail=finding, status=finding.get('status'),
                confidence=_confidence(finding.get('confidence')),
                uncertainty=finding.get('attribution_status') if finding.get('confidence') is None else None,
                uncertain=bool(finding.get('requires_log_verification')),
                envelope_of=len(spans),
                source={'document': 'findings.json', 'document_id': findings_doc.get('document_id'),
                        'processor': document_processor('findings.json'), 'artifact_ref': None,
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
            'resolved_span': None if gate_complete else resolved_span(envelope),
        })

    # --- availability, abstentions and provenance ---------------------------
    stages = manifest.get('stages') or {}
    unavailable = [{'stage': name, 'status': stage.get('status'),
                    'kind': _stage_kind(stage.get('status')), 'reason': stage.get('reason')}
                   for name, stage in sorted(stages.items())
                   if name != 'report' and stage.get('status') != 'complete']
    # A document that exists but cannot be parsed is a *different* failure from a
    # stage that produced nothing: the evidence is there but unreadable, so the
    # chain for this revision is incomplete and the reviewer must be told.
    for name in unreadable:
        unavailable.append({
            'stage': name, 'status': READ_UNREADABLE, 'kind': 'failed',
            'document': f'{name}.json',
            'reason': f'{name}.json exists but cannot be read as JSON; its evidence is missing from '
                      'this projection, so the evidence chain for this revision is incomplete'})
    if not gate_complete:
        for track_id in ROLE_DEPENDENT_TRACKS:
            unavailable.append({
                'stage': track_id, 'status': 'insufficient_evidence', 'kind': 'incomplete',
                'reason': f'人工角色确认未完成（{gate_status}）：{gate_reason}；'
                          '该轨道不发布 region，解析出的区间仅以 resolved_span 供审计'})

    # --- transcript ↔ region linkage ----------------------------------------
    # A transcript segment id and an acoustic segment id are *different evidence
    # namespaces* (``ASR-####`` versus ``SEG-*``): the ASR span and the acoustic
    # boundary stay independent evidence, so no naming convention can join them. The
    # only persisted statement that they describe the same audio is the fused
    # segment's cross-reference, so the link is resolved here, once, from that
    # cross-reference and published as a `region_id` the browser only has to trust.
    acoustic_ids_by_asr = {}
    for segment in (fused.get('segments') or []):
        asr_id = segment.get('asr_segment_id')
        acoustic_id = segment.get('acoustic_segment_id')
        if not asr_id or not acoustic_id:
            continue
        acoustic_ids_by_asr.setdefault(str(asr_id), []).append(str(acoustic_id))

    def transcript_link(segment_id):
        candidates = []
        for acoustic_id in acoustic_ids_by_asr.get(str(segment_id), []):
            region_id = acoustic_region_by_segment.get(acoustic_id)
            if region_id and region_id not in candidates:
                candidates.append(region_id)
        if len(candidates) == 1:
            return candidates[0], candidates, 'fused_acoustic_segment'
        if len(candidates) > 1:
            # One ASR utterance fused into several acoustic segments: the browser must
            # not silently pick one, so the candidates are published and the row stays
            # unlinked until a reviewer resolves it.
            return None, candidates, 'ambiguous'
        return None, [], 'unresolved'

    transcript_rows = []
    for segment in (transcript.get('segments') or []):
        region_id, candidates, basis = transcript_link(segment.get('segment_id'))
        transcript_rows.append({
            'segment_id': segment.get('segment_id'),
            'region_id': region_id,
            'region_ids': candidates,
            'region_basis': basis,
            'start_ms': _number(segment.get('start_ms')),
            'end_ms': _number(segment.get('end_ms')),
            'text': segment.get('text'),
            'speaker_id': segment.get('speaker_id'),
            'speaker_role': segment.get('speaker_role'),
            'timestamp_source': segment.get('timestamp_source'),
        })

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
    # The identity covers everything the browser navigates on: the published regions and
    # the transcript → region links. A revision whose fusion cross-reference changes
    # therefore gets its own workbench id even when the region set happens to be equal.
    document_basis = json.dumps({
        'regions': [region['region_id'] for region in builder.regions],
        'transcript_links': [[row['segment_id'], row['region_id']] for row in transcript_rows],
    }, ensure_ascii=False, sort_keys=True)
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
        'transcript': transcript_rows,
        'unavailable': unavailable,
        'evidence_integrity': {
            'status': 'incomplete' if unreadable else 'ok',
            'readable_documents': [f'{name}.json' for name in present],
            'unreadable_documents': [f'{name}.json' for name in unreadable],
        },
        'abstentions': {
            'metrics_gap': metrics_gap,
            'findings': list(findings_doc.get('abstentions') or []),
            'rejected_findings': list(findings_doc.get('rejected') or []),
            # The increment over `unavailable`: stages that were attempted and
            # abstained or failed, i.e. that were expected to conclude and did not.
            # Stages that simply have not run yet (`kind: not_run`) are reported in
            # `unavailable` only, so "not reached" is never read as "abstained".
            'stages': [item for item in unavailable
                       if item['stage'] in (manifest.get('stages') or {})
                       and item.get('kind') in ('incomplete', 'failed')],
            'unresolved_references': unresolved_refs,
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
