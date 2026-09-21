"""Manual speaker-role review: an append-only human decision over anonymous clusters.

Recording Analysis must not use a language model to decide who is the tester and
who is the device. ASR produces anonymous ``speaker_N`` clusters; a human listens
to the audio, reads the transcript and records an explicit decision for every
cluster. Only a saved, complete decision unlocks role-dependent Turns, Timeline,
metrics and the final report.

This module owns three things:

1. The **review surface** — what the user needs in order to decide, per cluster:
   representative intervals, transcript snippets and a playback range.
2. The **revision store** — one immutable JSON file per saved decision set. A new
   save never overwrites an earlier one, so a later disagreement stays auditable.
3. The **gate state** — ``awaiting_role_review | incomplete_review |
   complete_review``, plus the diff against the previous revision.

``unknown`` is a valid, deliberate decision: it means "I reviewed this cluster and
cannot say". It is not the same as "not yet reviewed", and the two states are
never conflated.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .runner import write_json

SCHEMA_VERSION = '1.0.0'
PROCESSOR_NAME = 'speaker_role_review'
PROCESSOR_VERSION = '1.0.0'
REVISION_KIND = 'speaker-role-mapping'
REVIEW_KIND = 'speaker-role-review'
REVIEW_STATUSES = ('awaiting_role_review', 'incomplete_review', 'complete_review')
ROLES = ('tester', 'device', 'unknown')
REVISION_PATTERN = re.compile(r'^role-mapping-REV-(\d{4})\.json$')
MAX_INTERVALS = 5
MAX_SNIPPETS = 5


class RoleReviewError(ValueError):
    """A safe, user-facing role-review failure; never carries credentials."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def review_dir(directory):
    return Path(directory) / 'role-review'


def _snippets(segments, transcript_segments):
    """Transcript text for one cluster, tied to the utterance that produced it."""
    out = []
    for speaker in segments:
        index = speaker.get('raw_utterance_index')
        if index is None or not 0 <= index < len(transcript_segments):
            continue
        utterance = transcript_segments[index]
        text = (utterance or {}).get('text')
        if not text:
            continue
        out.append({
            'asr_segment_id': utterance.get('segment_id'),
            'start_ms': round(float(utterance['start_ms']), 3),
            'end_ms': round(float(utterance['end_ms']), 3),
            'text': text,
        })
    return out[:MAX_SNIPPETS]


def _intervals(segments):
    """Longest representative intervals, in time order, for listening to a cluster."""
    ordered = sorted(segments, key=lambda item: (-(item['end_ms'] - item['start_ms']),
                                                 item['start_ms']))
    picked = sorted(ordered[:MAX_INTERVALS], key=lambda item: item['start_ms'])
    return [{
        'segment_id': item.get('segment_id'),
        'start_ms': round(float(item['start_ms']), 3),
        'end_ms': round(float(item['end_ms']), 3),
        'duration_ms': round(float(item['end_ms'] - item['start_ms']), 3),
        'timestamp_source': item.get('timestamp_source', 'provider_utterance_estimate'),
    } for item in picked]


def build_review_clusters(diarization_doc, transcript_doc=None):
    """Describe every anonymous cluster with the evidence a reviewer needs."""
    speaker_segments = list((diarization_doc or {}).get('speaker_segments') or [])
    transcript_segments = list((transcript_doc or {}).get('segments') or [])
    grouped = {}
    for speaker in speaker_segments:
        grouped.setdefault(speaker['speaker_id'], []).append(speaker)

    clusters = []
    for speaker_id, segments in sorted(grouped.items()):
        intervals = _intervals(segments)
        clusters.append({
            'speaker_id': speaker_id,
            'native_speaker_id': segments[0].get('native_speaker_id'),
            'segment_count': len(segments),
            'speech_ms': round(sum(max(0.0, item['end_ms'] - item['start_ms']) for item in segments), 3),
            'span_start_ms': round(min(item['start_ms'] for item in segments), 3),
            'span_end_ms': round(max(item['end_ms'] for item in segments), 3),
            'representative_intervals': intervals,
            'playback': ({'start_ms': min(item['start_ms'] for item in segments),
                          'end_ms': max(item['end_ms'] for item in segments)} if segments else None),
            'transcript_snippets': _snippets(segments, transcript_segments),
        })
    return clusters


def _revision_paths(directory):
    folder = review_dir(directory)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        match = REVISION_PATTERN.match(path.name)
        if match and path.is_file():
            found.append((int(match.group(1)), path))
    return [path for _, path in sorted(found)]


def load_revisions(directory):
    """Every saved decision set, oldest first. A corrupt file is reported, not skipped."""
    revisions = []
    for path in _revision_paths(directory):
        revisions.append(json.loads(path.read_text(encoding='utf-8')))
    return revisions


def latest_revision(directory):
    revisions = load_revisions(directory)
    return revisions[-1] if revisions else None


def current_cluster_ids(directory):
    """This Run's anonymous speaker clusters, in first-seen order.

    Read from the *current* AnalysisRevision's own diarization document: a role
    decision can only be validated against the clusters that revision actually
    preserved. Returns ``[]`` when there is no readable document, which the caller
    reports as insufficient evidence rather than as an empty-but-satisfied review.
    """
    manifest_path = Path(directory) / 'manifest.json'
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    analysis_root = Path(directory) / 'analysis' / (manifest.get('analysis_id') or '')
    document = _read_document(analysis_root / 'speaker-assignments.json')
    return list(dict.fromkeys(segment['speaker_id']
                              for segment in (document or {}).get('speaker_segments') or []))


def validate_decisions(decisions, cluster_ids):
    """Reject an incomplete or invented mapping before it can become evidence.

    Every cluster needs an explicit decision. ``unknown`` is an explicit decision;
    omitting a cluster is not. This is what makes ``unknown`` and "not reviewed"
    distinguishable in the stored revision.
    """
    if not isinstance(decisions, dict):
        raise RoleReviewError('Role mapping must be an object of speaker_id → tester/device/unknown')
    known = list(cluster_ids)
    if not known:
        raise RoleReviewError('This Run has no speaker clusters to review')
    missing = [cluster for cluster in known if cluster not in decisions]
    if missing:
        raise RoleReviewError('Every speaker cluster needs an explicit decision; missing: '
                              + ', '.join(missing))
    unknown_ids = [key for key in decisions if key not in known]
    if unknown_ids:
        raise RoleReviewError('Role mapping references clusters that do not exist in this Run: '
                              + ', '.join(unknown_ids))
    resolved = {}
    for cluster in known:
        role = decisions[cluster]
        if role not in ROLES:
            raise RoleReviewError(f'Role for {cluster} must be one of tester/device/unknown')
        resolved[cluster] = role
    return resolved


def save_revision(directory, decisions, reviewer, *, reason='', analysis_id=None,
                  recording_sha256=None, diarization_document_id=None, evidence_refs=()):
    """Append one immutable human decision set and return it.

    The file name carries the revision index, so an earlier decision can never be
    overwritten and two saves cannot silently collapse into one.
    """
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        raise RoleReviewError('A reviewer identity is required to save a role decision')
    if not isinstance(reason, str) or len(reason) > 2000:
        raise RoleReviewError('Review reason must be text up to 2000 characters')
    revisions = load_revisions(directory)
    previous = revisions[-1] if revisions else None
    payload = {
        'schema_version': SCHEMA_VERSION,
        'revision_id': 'REV-' + uuid.uuid4().hex[:12],
        'revision_index': len(revisions) + 1,
        'run_id': Path(directory).name,
        'analysis_id': analysis_id,
        'recording_sha256': recording_sha256,
        'diarization_document_id': diarization_document_id,
        'cluster_ids': sorted(decisions),
        'decisions': dict(sorted(decisions.items())),
        'reviewer': reviewer.strip(),
        'reason': reason.strip(),
        'created_at': utc_now(),
        'previous_revision_ref': previous['revision_id'] if previous else None,
        'evidence_refs': list(evidence_refs),
        'method': 'human_attribution',
        'confidence_basis': 'human_review',
        'note': ('User-supplied cluster roles. Anonymous clusters are never mapped '
                 'automatically, and a later change creates a new revision instead of '
                 'overwriting this one.'),
    }
    payload['mapping_sha256'] = hashlib.sha256(
        json.dumps(payload['decisions'], sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    folder = review_dir(directory)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'role-mapping-REV-{payload["revision_index"]:04d}.json'
    # Immutable: `write_json` truncates, so guard the name explicitly.
    if path.exists():
        raise RoleReviewError('A role revision already exists at this index; refusing to overwrite')
    write_json(path, payload)
    return path, payload


def mapping_diff(previous, current):
    """What changed between two saved decision sets."""
    previous = previous or {}
    current = current or {}
    changed = [{'speaker_id': key, 'from': previous[key], 'to': current[key]}
               for key in sorted(current) if key in previous and previous[key] != current[key]]
    added = [{'speaker_id': key, 'to': current[key]} for key in sorted(current) if key not in previous]
    removed = [{'speaker_id': key, 'from': previous[key]} for key in sorted(previous) if key not in current]
    unchanged = [key for key in sorted(current) if previous.get(key) == current[key]]
    return {'changed': changed, 'added': added, 'removed': removed, 'unchanged': unchanged}


def review_status(cluster_ids, decisions):
    """Gate state for a Run's cluster set against the latest saved decisions."""
    if not cluster_ids:
        return ('awaiting_role_review',
                'No anonymous speaker clusters to review; role-dependent stages cannot run')
    decisions = decisions or {}
    if not decisions:
        return ('awaiting_role_review',
                'Anonymous speaker clusters are waiting for an explicit user role decision; '
                'no LLM proposal is used')
    unresolved = [cluster for cluster in cluster_ids if cluster not in decisions]
    if unresolved:
        return ('incomplete_review',
                f'{len(unresolved)} cluster(s) still have no user decision; '
                'role-dependent stages remain blocked')
    return ('complete_review',
            'All clusters have a user decision; role-dependent stages may run')


def build_role_review(directory, diarization_doc=None, transcript_doc=None, manifest=None):
    """Assemble the review surface, the gate state and the revision history.

    ``manifest`` lets a caller that already holds the Run's manifest in memory (a
    rebuild whose on-disk checkpoint is deliberately still the *source* revision
    until its new AnalysisRevision is complete) state which analysis this surface
    describes. Without it the Run's own ``manifest.json`` is read, which is what
    every read-side projection wants (Issue #113 review).
    """
    read_manifest = manifest
    if read_manifest is None:
        manifest_path = Path(directory) / 'manifest.json'
        read_manifest = json.loads(manifest_path.read_text(encoding='utf-8')) \
            if manifest_path.is_file() else {}
    analysis_root = Path(directory) / 'analysis' / (read_manifest.get('analysis_id') or '')
    if diarization_doc is None:
        diarization_doc = _read_document(analysis_root / 'speaker-assignments.json')
    if transcript_doc is None:
        transcript_doc = _read_document(analysis_root / 'transcript.json')

    clusters = build_review_clusters(diarization_doc, transcript_doc)
    cluster_ids = [cluster['speaker_id'] for cluster in clusters]
    revisions = load_revisions(directory)
    current = revisions[-1] if revisions else None
    decisions = dict(current['decisions']) if current else {}
    previous = revisions[-2] if len(revisions) > 1 else None

    status, reason = review_status(cluster_ids, decisions)
    for cluster in clusters:
        cluster['decision'] = decisions.get(cluster['speaker_id'])
        cluster['decision_basis'] = 'user_review' if cluster['decision'] else None
        cluster['revision_ref'] = current['revision_id'] if cluster['decision'] else None

    return {
        'schema_version': SCHEMA_VERSION,
        'review_id': 'ROLEREVIEW-' + uuid.uuid4().hex,
        'run_id': Path(directory).name,
        'analysis_id': read_manifest.get('analysis_id'),
        'processor': {'name': PROCESSOR_NAME, 'version': PROCESSOR_VERSION,
                      'llm_role_inference': False},
        'scope': {
            'audio_sha256': (diarization_doc or {}).get('scope', {}).get('recording_sha256')
                            or (diarization_doc or {}).get('source', {}).get('audio_sha256'),
            'diarization_document_id': (diarization_doc or {}).get('document_id'),
            'invocation_id': (diarization_doc or {}).get('scope', {}).get('invocation_id'),
        },
        'status': status,
        'reason': reason,
        'roles': list(ROLES),
        'awaiting_decision_for': [cluster['speaker_id'] for cluster in clusters
                                  if cluster['speaker_id'] not in decisions],
        'unknown_clusters': [cluster_id for cluster_id, role in sorted(decisions.items())
                             if role == 'unknown'],
        'clusters': clusters,
        'revision': _revision_summary(current) if current else None,
        'history': [_revision_summary(item) for item in revisions],
        'diff': mapping_diff(previous['decisions'] if previous else None, decisions),
        'gate': {
            'role_dependent_stages_blocked': status != 'complete_review',
            'note': ('Saving a complete decision set creates a new AnalysisRevision and reruns '
                     'attribution through metrics and the report. Until then only provisional '
                     'import/diagnostic state may be shown.'),
        },
    }


def _revision_summary(revision):
    if revision is None:
        return None
    return {'revision_id': revision['revision_id'],
            'revision_index': revision['revision_index'],
            'reviewer': revision['reviewer'],
            'reason': revision['reason'],
            'created_at': revision['created_at'],
            'mapping_sha256': revision['mapping_sha256'],
            'decisions': revision['decisions'],
            'analysis_id': revision.get('analysis_id')}


def _read_document(path):
    if not Path(path).is_file():
        return {}
    payload = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(payload, dict) and 'data' in payload and 'kind' in payload:
        return payload.get('data') or {}
    return payload if isinstance(payload, dict) else {}
