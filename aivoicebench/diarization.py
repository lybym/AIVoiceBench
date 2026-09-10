"""Diarization provider abstraction and source attribution.

Diarization answers: "which speech segments belong to the same speaker?"
It outputs speaker IDs/clusters (speaker_0, speaker_1, ...), NOT roles.

Source Attribution answers: "which speaker is the tester, which is the device?"
It maps speaker_id → tester/device/unknown using evidence, never by time order.

Design principles (PRD 1.1.0):
- No first-speaker inference
- No alternating heuristic
- No "two speakers = speaker_0 is tester"
- Unknown is the default until valid evidence exists
- Diarization and role attribution are separate processors
- Both preserve provider/model/invocation evidence
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import time
import uuid
from pathlib import Path
from typing import Protocol

from .runner import write_json


@dataclass
class SpeakerSegment:
    """A speech segment attributed to a speaker cluster."""
    speaker_id: str
    start_ms: float
    end_ms: float
    confidence: float | None = None
    evidence_refs: list = field(default_factory=list)

    def to_dict(self, segment_id):
        return {
            'segment_id': segment_id,
            'speaker_id': self.speaker_id,
            'start_ms': round(self.start_ms, 3),
            'end_ms': round(self.end_ms, 3),
            'confidence': round(self.confidence, 4) if self.confidence is not None else None,
            'timing_source': 'audio_relative_ms',
            'source': 'diarization',
            'evidence_refs': list(self.evidence_refs),
        }


@dataclass
class DiarizationResult:
    speaker_segments: list  # list[SpeakerSegment]
    status: str
    reason: str | None
    source: dict
    processor: dict
    invocation: dict | None = None

    def to_dict(self):
        return {
            'schema_version': '1.0.0',
            'document_id': 'DIAR-' + uuid.uuid4().hex,
            'source': self.source,
            'processor': self.processor,
            'invocation': self.invocation,
            'status': self.status,
            'reason': self.reason,
            'speaker_segments': [seg.to_dict(f'SPK-{i:04d}')
                                 for i, seg in enumerate(self.speaker_segments)],
        }


class MockDiarizationProvider:
    """Mock diarization for testing without cloud API.

    Assigns alternating speaker IDs to acoustic segments.
    This is speaker CLUSTERING only — it does NOT assign tester/device roles.
    For testing purposes, speaker_0 and speaker_1 are just two distinct clusters.
    """

    def __init__(self, provider_name="mock_diarization", model="mock-diar-v1"):
        self.provider = provider_name
        self.model = model

    def diarize(self, audio_path, acoustic_segments=None):
        """Cluster acoustic segments into speaker groups.

        If acoustic_segments is provided, uses their timing to assign speakers.
        Without acoustic segments, returns insufficient_evidence.
        """
        inv_id = 'CALL-' + uuid.uuid4().hex
        started = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        start_time = time.monotonic()

        if not acoustic_segments:
            result = DiarizationResult(
                speaker_segments=[], status='insufficient_evidence',
                reason='No acoustic segments to cluster',
                source=self._source(audio_path),
                processor=self._processor(),
                invocation=self._invocation(inv_id, started, start_time, 'success'))
            return result

        # Assign alternating speaker IDs: speaker_0, speaker_1, speaker_0, ...
        # This is CLUSTERING only — does NOT mean speaker_0 = tester
        segments = []
        for i, aseg in enumerate(acoustic_segments):
            speaker_id = f'speaker_{i % 2}'
            segments.append(SpeakerSegment(
                speaker_id=speaker_id,
                start_ms=aseg['start_ms'],
                end_ms=aseg['end_ms'],
                confidence=0.7,  # mock confidence for clustering quality
                evidence_refs=[],
            ))

        invocation = self._invocation(inv_id, started, start_time, 'success')
        result = DiarizationResult(
            speaker_segments=segments, status='complete',
            reason=None,
            source=self._source(audio_path, acoustic_segments),
            processor=self._processor(),
            invocation=invocation)
        return result

    def _source(self, audio_path, acoustic_segments=None):
        from .runner import digest
        sha = '0' * 64
        if audio_path:
            try:
                sha = digest(audio_path)
            except OSError:
                sha = '0' * 64
        duration = 0
        if acoustic_segments:
            duration = max(s['end_ms'] for s in acoustic_segments) if acoustic_segments else 0
        return {'audio_sha256': sha, 'duration_ms': round(duration, 3)}

    def _processor(self):
        return {
            'provider': self.provider,
            'model': self.model,
            'api_version': None,
            'config': {'method': 'alternating_clustering',
                       'note': 'Mock: clusters by segment order. Real provider should use voice features.'}
        }

    def _invocation(self, inv_id, started, start_time, status):
        return {
            'invocation_id': inv_id,
            'latency_ms': round((time.monotonic() - start_time) * 1000, 3),
            'status': status,
        }


def attribute_speakers(diarization_doc, acoustic_doc=None, transcript_doc=None,
                       explicit_mapping=None):
    """Map speaker_id → tester/device/unknown using evidence.

    This is the Source Attribution step, separate from diarization.

    Attribution levels:
    1. Explicit evidence (explicit_mapping, ExecutionRun link, Frozen Audio match)
    2. Semantic attribution (LLM, not implemented here)
    3. Human attribution (revision, not implemented here)

    Without any evidence, ALL speakers remain 'unknown'.
    NEVER infers from first-speaker order or speaker count.
    """
    from .validation import schema_errors

    speaker_segments = diarization_doc.get('speaker_segments', [])
    speaker_ids = list(dict.fromkeys(s['speaker_id'] for s in speaker_segments))

    if not speaker_ids:
        return {
            'schema_version': '1.0.0',
            'document_id': 'ATTR-' + uuid.uuid4().hex,
            'speaker_segments_ref': diarization_doc.get('document_id'),
            'status': 'insufficient_evidence',
            'reason': 'No speaker segments to attribute',
            'attributions': [],
        }

    # Level 1: Explicit evidence mapping
    if explicit_mapping:
        attributions = []
        for sid in speaker_ids:
            role = explicit_mapping.get(sid, 'unknown')
            conf = 1.0 if role != 'unknown' else 0.0
            attributions.append({
                'speaker_id': sid,
                'role': role,
                'confidence': conf,
                'method': 'explicit_evidence' if role != 'unknown' else 'none',
                'evidence_refs': [],
                'provider': None,
                'model': None,
                'reason': f'Explicitly mapped by user' if role != 'unknown' else 'No explicit evidence for this speaker',
            })
        status = 'complete' if all(a['role'] != 'unknown' for a in attributions) else 'partial'
        reason = None if status == 'complete' else 'Some speakers have no role evidence'
        return {
            'schema_version': '1.0.0',
            'document_id': 'ATTR-' + uuid.uuid4().hex,
            'speaker_segments_ref': diarization_doc.get('document_id'),
            'status': status,
            'reason': reason,
            'attributions': attributions,
        }

    # No evidence available — all speakers remain unknown
    # Status is 'partial' (not insufficient_evidence) because we DO have speaker IDs,
    # we just don't have role evidence. The attribution data is included.
    return {
        'schema_version': '1.0.0',
        'document_id': 'ATTR-' + uuid.uuid4().hex,
        'speaker_segments_ref': diarization_doc.get('document_id'),
        'status': 'partial',
        'reason': 'No role attribution evidence available. '
                  'Requires explicit mapping, semantic attribution, or human review. '
                  'NOT inferred from speaker order or count.',
        'attributions': [{
            'speaker_id': sid,
            'role': 'unknown',
            'confidence': 0.0,
            'method': 'none',
            'evidence_refs': [],
            'provider': None,
            'model': None,
            'reason': 'No attribution evidence',
        } for sid in speaker_ids],
    }


