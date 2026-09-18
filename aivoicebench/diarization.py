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
    """A speech segment attributed to a speaker cluster.

    speaker_id is the local normalized ID; native_speaker_id keeps the raw
    provider label. Boundary timing comes from provider utterance estimates and
    is NOT a precise acoustic onset. Confidence is null unless the provider
    actually supplies a clustering confidence.
    """
    speaker_id: str
    start_ms: float
    end_ms: float
    confidence: float | None = None
    native_speaker_id: str | None = None
    timestamp_source: str = 'provider_utterance_estimate'
    raw_message_index: int | None = None
    raw_utterance_index: int | None = None
    evidence_refs: list = field(default_factory=list)

    def to_dict(self, segment_id):
        return {
            'segment_id': segment_id,
            'speaker_id': self.speaker_id,
            'native_speaker_id': self.native_speaker_id,
            'start_ms': round(self.start_ms, 3),
            'end_ms': round(self.end_ms, 3),
            'confidence': round(self.confidence, 4) if self.confidence is not None else None,
            'timing_source': 'audio_relative_ms',
            'timestamp_source': self.timestamp_source,
            'source': 'diarization',
            'raw_message_index': self.raw_message_index,
            'raw_utterance_index': self.raw_utterance_index,
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
    scope: dict | None = None

    def to_dict(self):
        return {
            'schema_version': '1.0.0',
            'document_id': 'DIAR-' + uuid.uuid4().hex,
            'source': self.source,
            'scope': self.scope or {'recording_sha256': self.source.get('audio_sha256', '0' * 64),
                                    'invocation_id': (self.invocation or {}).get('invocation_id'),
                                    'analysis_id': None, 'native_response_sha256': None},
            'processor': self.processor,
            'invocation': self.invocation,
            'status': self.status,
            'reason': self.reason,
            'speaker_segments': [seg.to_dict(f'SPK-{i:04d}')
                                 for i, seg in enumerate(self.speaker_segments)],
        }


class ASRNativeDiarizationProvider:
    """Derive speaker clusters from an already-completed ASR native response.

    This provider performs NO cloud call. It normalizes speaker labels that the
    configured ASR call already returned, so one recognition submission yields
    both a Transcript and speaker segments without a second upload or billing.

    Product contract:
    - Local speaker IDs are namespaced per recording/invocation scope. speaker_0
      from one recording is never assumed to be the same person as speaker_0
      from another.
    - native_speaker_id keeps the raw provider label.
    - Unknown/missing labels stay unknown. No sequential fill-in, no alternating
      assignment, no assumption of exactly two speakers.
    - Confidence is null unless the provider actually returns it. Recognition
      text confidence is never reused as clustering confidence.
    - Boundary times are provider utterance estimates, not precise acoustic onsets.
    """

    def __init__(self, provider_name='volcengine', model='bigmodel',
                 model_version='service-managed', api_version='v3',
                 resource_id='volc.bigasr.auc_turbo',
                 contract_status='interface_contract_pending'):
        self.provider = provider_name
        self.model = model
        self.model_version = model_version
        self.api_version = api_version
        self.resource_id = resource_id
        # Whether the *request contract* for speaker separation is verified is a
        # different fact from whether a real call returned labels. This provider
        # only reads labels that a verified request already returned, so as of this
        # revision the contract stays pending and must be reported as such.
        self.contract_status = contract_status

    def diarize_from_transcript(self, transcript, *, audio_sha256, invocation_id=None,
                                native_response_sha256=None, analysis_id=None,
                                source_name='transcript'):
        """Build speaker segments from a normalized Transcript's speaker_id field.

        `transcript` is a Transcript 1.x document (or its envelope `data`).
        Returns a DiarizationResult. No network activity occurs.
        """
        segments_in = transcript.get('segments', []) if isinstance(transcript, dict) else []
        duration_ms = 0
        if isinstance(transcript, dict):
            duration_ms = transcript.get('source', {}).get('duration_ms', 0) or 0

        # Namespace local speaker IDs to this recording scope so cross-recording
        # identity cannot be accidentally merged.
        scope_prefix = (audio_sha256 or '0' * 64)[:12]
        native_to_local = {}
        segments = []
        unknown_count = 0

        for index, seg in enumerate(segments_in):
            native = seg.get('speaker_id')
            native_text = str(native) if native is not None and str(native).strip() else None
            if native_text is None:
                unknown_count += 1
                local = f'{scope_prefix}:unknown'
            else:
                if native_text not in native_to_local:
                    native_to_local[native_text] = f'{scope_prefix}:speaker_{len(native_to_local)}'
                local = native_to_local[native_text]
            # Speaker clustering confidence is not supplied by this ASR contract.
            # Recognition/timestamp confidence must not be reused here.
            segments.append(SpeakerSegment(
                speaker_id=local,
                native_speaker_id=native_text,
                start_ms=seg['start_ms'],
                end_ms=seg['end_ms'],
                confidence=None,
                timestamp_source='provider_utterance_estimate',
                raw_message_index=seg.get('raw_message_index'),
                raw_utterance_index=index,
                evidence_refs=[],
            ))

        source = {'audio_sha256': audio_sha256, 'duration_ms': round(float(duration_ms), 3)}
        scope = {
            'recording_sha256': audio_sha256,
            'invocation_id': invocation_id,
            'analysis_id': analysis_id,
            'native_response_sha256': native_response_sha256,
        }
        labeled = [s for s in segments if s.native_speaker_id is not None]
        processor = {
            'provider': self.provider,
            'model': self.model,
            'api_version': self.api_version,
            'config': {'derivation': 'asr_native_speaker_labels',
                       'resource_id': self.resource_id,
                       'source_document': source_name,
                       'cloud_call_performed': False,
                       'interface_contract_status': self.contract_status,
                       'labeled_utterances_observed': bool(labeled),
                       'note': 'Derived from an existing ASR native response; no additional request. '
                               'Whether the service needs an explicit request property to enable '
                               'speaker separation is unverified, so this reads labels only when the '
                               'verified request happened to return them.'},
        }
        invocation = {
            'invocation_id': invocation_id,
            'latency_ms': 0.0,
            'status': 'derived_from_existing_asr_invocation',
        }

        if not segments:
            return DiarizationResult(
                speaker_segments=[], status='insufficient_evidence',
                reason='ASR native response contained no timestamped utterances to cluster',
                source=source, processor=processor, invocation=invocation, scope=scope)

        if not labeled:
            # Speaker information was absent, or the verified request did not ask
            # for it and the request contract for asking is still unverified.
            return DiarizationResult(
                speaker_segments=[], status='insufficient_evidence',
                reason='ASR native response carried no speaker labels; the request contract for '
                       'enabling speaker separation is unverified (interface_contract_pending), '
                       'so no unverified request property was sent',
                source=source, processor=processor, invocation=invocation, scope=scope)

        reason = None
        if unknown_count:
            reason = (f'{unknown_count} utterance(s) had no speaker label and remain unknown; '
                      'no sequential or alternating assignment was applied')
        return DiarizationResult(
            speaker_segments=segments, status='complete' if not unknown_count else 'partial',
            reason=reason, source=source, processor=processor,
            invocation=invocation, scope=scope)


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

    Attribution authority:
    1. Explicit user/human evidence (persisted revision target)
    2. Otherwise unknown; LLM role assignment is not used by Recording Analysis

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
                'confidence_basis': 'explicit_user_evidence' if role != 'unknown' else 'none',
                'method': 'explicit_evidence' if role != 'unknown' else 'none',
                'evidence_refs': [],
                'provider': None,
                'model': None,
                'reason': f'Explicitly mapped by user' if role != 'unknown' else 'No explicit evidence for this speaker',
                'needs_review': role == 'unknown',
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
        'reason': 'Awaiting manual speaker-role review. '
                  'Requires an explicit user mapping. '
                  'NOT inferred from speaker order or count.',
        'attributions': [{
            'speaker_id': sid,
            'role': 'unknown',
            'confidence': 0.0,
            'confidence_basis': 'none',
            'method': 'none',
            'evidence_refs': [],
            'provider': None,
            'model': None,
            'reason': 'No attribution evidence',
            'needs_review': False,
        } for sid in speaker_ids],
    }


