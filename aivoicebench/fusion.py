"""Segment fusion, speaker attribution, turn building, and event detection.

This module bridges acoustic segments (#23) and optional ASR transcripts (#7/#22)
to produce fused segments with speaker roles, conversational turns, detected
events, and an auto-generated EventTimeline.

Key principles (user directive section #3):
- Acoustic timing is distinct from ASR timing; both are preserved.
- Speaker attribution is a hypothesis, not ground truth, unless human-verified.
- Events carry source, confidence, and evidence references.
- Insufficient evidence yields insufficient_evidence, never invented timing.
- Acoustic ordering alone never assigns tester/device roles.
"""

from dataclasses import dataclass, field
import uuid

from .runner import write_json

DEFAULT_TIMEOUT_MS = 5000.0
DEFAULT_FALSE_ENDPOINT_MS = 300.0
DEFAULT_OVERLAP_MIN_MS = 50.0


@dataclass
class FusedSegment:
    start_ms: float
    end_ms: float
    speaker_role: str  # tester | device | unknown
    speaker_confidence: float
    speaker_source: str  # heuristic | diarization | llm | manual
    timing_source: str  # acoustic | asr | fused
    text: str | None = None
    acoustic_segment_id: str | None = None
    asr_segment_id: str | None = None

    def to_dict(self, segment_id):
        return {
            'segment_id': segment_id,
            'start_ms': round(self.start_ms, 3),
            'end_ms': round(self.end_ms, 3),
            'speaker_role': self.speaker_role,
            'speaker_confidence': round(self.speaker_confidence, 4),
            'speaker_source': self.speaker_source,
            'timing_source': self.timing_source,
            'text': self.text,
            'acoustic_segment_id': self.acoustic_segment_id,
            'asr_segment_id': self.asr_segment_id,
        }


@dataclass
class Turn:
    turn_id: str
    tester_segment_ids: list = field(default_factory=list)
    device_segment_ids: list = field(default_factory=list)
    response_id: str | None = None
    start_ms: float = 0.0
    end_ms: float = 0.0
    tester_speech_start_ms: float | None = None
    tester_speech_end_ms: float | None = None
    device_speech_start_ms: float | None = None
    device_speech_end_ms: float | None = None
    has_interruption: bool = False
    has_overlap: bool = False


def fuse(acoustic_doc, transcript_doc=None):
    """Fuse acoustic segments with optional ASR transcript and attribute speakers.

    Returns a FusedSegments 1.0.0 document.
    Retains unknown roles until a separate attribution processor supplies evidence.
    """
    acoustic_segments = acoustic_doc.get('segments', [])
    duration_ms = acoustic_doc.get('source', {}).get('duration_ms', 0)
    audio_sha = acoustic_doc.get('source', {}).get('sha256', '0' * 64)
    acoustic_doc_id = acoustic_doc.get('document_id')

    if not acoustic_segments:
        return {
            'schema_version': '1.0.0',
            'document_id': 'FUSED-' + uuid.uuid4().hex,
            'source': {
                'acoustic_document_id': acoustic_doc_id,
                'transcript_document_id': transcript_doc.get('transcript_id') if transcript_doc else None,
                'audio_sha256': audio_sha,
                'duration_ms': round(duration_ms, 3),
            },
            'attribution': {
                'strategy': 'none',
                'provider': None,
                'confidence': 0.0,
                'note': 'No acoustic segments to attribute',
            },
            'status': 'insufficient_evidence',
            'reason': 'Acoustic segmentation produced no segments',
            'segments': [],
        }

    # Build ASR lookup: map overlapping time ranges to text
    asr_segments = []
    transcript_doc_id = None
    if transcript_doc and transcript_doc.get('segments'):
        transcript_doc_id = transcript_doc.get('transcript_id')
        for seg in transcript_doc['segments']:
            asr_segments.append({
                'segment_id': seg.get('segment_id'),
                'start_ms': seg['start_ms'],
                'end_ms': seg['end_ms'],
                'text': seg.get('text', ''),
            })

    def find_asr_overlap(start, end):
        """Find ASR segment that overlaps with the given time range."""
        best = None
        best_overlap = 0
        for asr in asr_segments:
            overlap = min(end, asr['end_ms']) - max(start, asr['start_ms'])
            if overlap > best_overlap:
                best_overlap = overlap
                best = asr
        return best

    fused = []
    for i, aseg in enumerate(acoustic_segments):
        # Acoustic segments alone do not identify tester/device roles.
        role = 'unknown'
        a_start = aseg['start_ms']
        a_end = aseg['end_ms']
        asr = find_asr_overlap(a_start, a_end)
        timing = 'acoustic'
        text = None
        asr_id = None
        if asr and asr.get('text'):
            text = asr['text']
            asr_id = asr.get('segment_id')
            timing = 'fused'
        fused.append(FusedSegment(
            start_ms=a_start, end_ms=a_end,
            speaker_role=role,
            speaker_confidence=0.0,
            speaker_source='acoustic',
            timing_source=timing,
            text=text,
            acoustic_segment_id=aseg.get('segment_id'),
            asr_segment_id=asr_id,
        ))

    return {
        'schema_version': '1.0.0',
        'document_id': 'FUSED-' + uuid.uuid4().hex,
        'source': {
            'acoustic_document_id': acoustic_doc_id,
            'transcript_document_id': transcript_doc_id,
            'audio_sha256': audio_sha,
            'duration_ms': round(duration_ms, 3),
        },
        'attribution': {
            'strategy': 'none',
            'provider': None,
            'confidence': 0.0,
            'note': 'No role evidence; diarization or human attribution is required',
        },
        'status': 'partial',
        'reason': 'Speaker roles are unresolved',
        'segments': [seg.to_dict(f'FSEG-{i:04d}') for i, seg in enumerate(fused)],
    }


def build_turns(fused_doc):
    """Group fused segments into conversational turns.

    A turn pairs tester speech with the following device response.
    Handles interruptions: if tester speaks during device speech,
    the current turn is closed and a new turn begins.
    """
    segments = fused_doc.get('segments', [])
    fused_doc_id = fused_doc.get('document_id')

    if not segments:
        return {
            'schema_version': '1.0.0',
            'document_id': 'TURNS-' + uuid.uuid4().hex,
            'fused_document_id': fused_doc_id,
            'status': 'insufficient_evidence',
            'reason': 'No fused segments to build turns',
            'turns': [],
        }

    turns = []
    current_turn = None
    turn_num = 0
    response_num = 0

    for seg in segments:
        role = seg['speaker_role']
        seg_id = seg['segment_id']
        start = seg['start_ms']
        end = seg['end_ms']

        if role == 'tester':
            if current_turn is not None:
                # Check if this tester segment overlaps with device speech → interruption
                if current_turn.get('device_speech_end_ms') and start < current_turn['device_speech_end_ms']:
                    current_turn['has_interruption'] = True
                    current_turn['has_overlap'] = True
                    turns.append(_finalize_turn(current_turn))
                    turn_num += 1
                    current_turn = None
                elif current_turn.get('device_speech_start_ms') is None:
                    # Same turn, another tester segment
                    current_turn['tester_segment_ids'].append(seg_id)
                    current_turn['tester_speech_end_ms'] = end
                    current_turn['end_ms'] = end
                    continue
                else:
                    # Previous turn complete, start new one
                    turns.append(_finalize_turn(current_turn))
                    turn_num += 1
                    current_turn = None

            if current_turn is None:
                response_num += 1
                current_turn = {
                    'turn_id': f'TURN-{turn_num + 1:04d}',
                    'tester_segment_ids': [seg_id],
                    'device_segment_ids': [],
                    'response_id': None,
                    'start_ms': start,
                    'end_ms': end,
                    'tester_speech_start_ms': start,
                    'tester_speech_end_ms': end,
                    'device_speech_start_ms': None,
                    'device_speech_end_ms': None,
                    'has_interruption': False,
                    'has_overlap': False,
                }

        elif role == 'device':
            if current_turn is None:
                # Device speech without preceding tester — orphan response
                turn_num += 1
                response_num += 1
                current_turn = {
                    'turn_id': f'TURN-{turn_num + 1:04d}',
                    'tester_segment_ids': [],
                    'device_segment_ids': [seg_id],
                    'response_id': f'RESP-{response_num:04d}',
                    'start_ms': start,
                    'end_ms': end,
                    'tester_speech_start_ms': None,
                    'tester_speech_end_ms': None,
                    'device_speech_start_ms': start,
                    'device_speech_end_ms': end,
                    'has_interruption': False,
                    'has_overlap': False,
                }
            else:
                if current_turn['device_speech_start_ms'] is None:
                    current_turn['response_id'] = f'RESP-{response_num:04d}'
                    current_turn['device_speech_start_ms'] = start
                current_turn['device_segment_ids'].append(seg_id)
                current_turn['device_speech_end_ms'] = end
                current_turn['end_ms'] = end

                # Check for overlap with tester speech
                if (current_turn['tester_speech_end_ms'] is not None and
                        start < current_turn['tester_speech_end_ms']):
                    current_turn['has_overlap'] = True
        else:
            # unknown role — skip for turn building
            pass

    if current_turn is not None:
        turns.append(_finalize_turn(current_turn))

    return {
        'schema_version': '1.0.0',
        'document_id': 'TURNS-' + uuid.uuid4().hex,
        'fused_document_id': fused_doc_id,
        'status': 'complete' if turns else 'insufficient_evidence',
        'reason': None if turns else 'No turns could be built',
        'turns': turns,
    }


def _finalize_turn(t):
    """Convert internal turn dict to schema-compliant output."""
    return {
        'turn_id': t['turn_id'],
        'tester_segment_ids': t['tester_segment_ids'],
        'device_segment_ids': t['device_segment_ids'],
        'response_id': t['response_id'],
        'start_ms': round(t['start_ms'], 3),
        'end_ms': round(t['end_ms'], 3),
        'tester_speech_start_ms': round(t['tester_speech_start_ms'], 3) if t['tester_speech_start_ms'] is not None else None,
        'tester_speech_end_ms': round(t['tester_speech_end_ms'], 3) if t['tester_speech_end_ms'] is not None else None,
        'device_speech_start_ms': round(t['device_speech_start_ms'], 3) if t['device_speech_start_ms'] is not None else None,
        'device_speech_end_ms': round(t['device_speech_end_ms'], 3) if t['device_speech_end_ms'] is not None else None,
        'has_interruption': t['has_interruption'],
        'has_overlap': t['has_overlap'],
    }


def detect_events(fused_doc, turns_doc, *, timeout_ms=DEFAULT_TIMEOUT_MS,
                  false_endpoint_ms=DEFAULT_FALSE_ENDPOINT_MS,
                  overlap_min_ms=DEFAULT_OVERLAP_MIN_MS):
    """Detect events from fused segments and turns.

    Returns a list of event dicts (Event 2.0.0 schema).
    Events carry source=audio_signal or derived, confidence, and evidence refs.
    """
    segments = fused_doc.get('segments', [])
    turns = turns_doc.get('turns', [])
    duration_ms = fused_doc.get('source', {}).get('duration_ms', 0)

    if not segments:
        return [], [], 'insufficient_evidence', 'No fused segments to detect events from'

    if any(seg.get('speaker_role') == 'unknown' for seg in segments):
        return [], [], 'insufficient_evidence', 'Speaker attribution is unresolved'

    events = []
    evidence = []
    event_num = 0
    evidence_num = 0

    # Create one evidence snippet per segment (acoustic timing)
    ev_map = {}  # segment_id -> evidence_id
    for seg in segments:
        eid = f'EV-{evidence_num + 1:04d}'
        evidence.append({
            'schema_version': '1.0.0',
            'evidence_id': eid,
            'artifact_id': 'ART-audio',
            'track_id': 'TRACK-mix',
            'time_base': 'run_monotonic_ms',
            'start_ms': seg['start_ms'],
            'end_ms': seg['end_ms'],
            'source': 'audio_signal',
            'confidence': seg.get('speaker_confidence', 0.5),
        })
        ev_map[seg['segment_id']] = eid
        evidence_num += 1

    # Build a lookup from turn_id to turn
    turn_lookup = {t['turn_id']: t for t in turns}

    for seg in segments:
        role = seg['speaker_role']
        sid = seg['segment_id']
        start = seg['start_ms']
        end = seg['end_ms']
        ev_id = ev_map[sid]

        # Find the turn this segment belongs to
        turn_id = None
        response_id = None
        for t in turns:
            if sid in t['tester_segment_ids'] or sid in t['device_segment_ids']:
                turn_id = t['turn_id']
                response_id = t.get('response_id')
                break

        if role == 'tester':
            events.append(_event(f'EVT-{event_num + 1:04d}', 'tester_speech_start', start, start,
                                 turn_id, response_id, 'audio_signal', 0.7, [ev_id]))
            event_num += 1
            events.append(_event(f'EVT-{event_num + 1:04d}', 'tester_speech_end', end, end,
                                 turn_id, response_id, 'audio_signal', 0.7, [ev_id]))
            event_num += 1
        elif role == 'device':
            events.append(_event(f'EVT-{event_num + 1:04d}', 'device_speech_start', start, start,
                                 turn_id, response_id, 'audio_signal', 0.7, [ev_id]))
            event_num += 1
            events.append(_event(f'EVT-{event_num + 1:04d}', 'device_speech_end', end, end,
                                 turn_id, response_id, 'audio_signal', 0.7, [ev_id]))
            event_num += 1
            # Response start/end for the first device segment in a turn
            if turn_id and response_id:
                t = turn_lookup.get(turn_id)
                if t and sid == t['device_segment_ids'][0]:
                    events.append(_event(f'EVT-{event_num + 1:04d}', 'response_start', start, start,
                                         turn_id, response_id, 'derived', 0.6, [ev_id]))
                    event_num += 1
                if t and sid == t['device_segment_ids'][-1]:
                    events.append(_event(f'EVT-{event_num + 1:04d}', 'response_end', end, end,
                                         turn_id, response_id, 'derived', 0.6, [ev_id]))
                    event_num += 1

    # Detect silence between segments
    for i in range(1, len(segments)):
        gap_start = segments[i - 1]['end_ms']
        gap_end = segments[i]['start_ms']
        if gap_end > gap_start + 1:
            sid = segments[i - 1]['segment_id']
            eid = ev_map[sid]
            # Check if it's a timeout
            gap_duration = gap_end - gap_start
            if gap_duration >= timeout_ms:
                events.append(_event(f'EVT-{event_num + 1:04d}', 'timeout', gap_start, gap_end,
                                     None, None, 'derived', 0.8, [eid]))
                event_num += 1
            else:
                events.append(_event(f'EVT-{event_num + 1:04d}', 'silence', gap_start, gap_end,
                                     None, None, 'derived', 0.9, [eid]))
                event_num += 1

    # Detect overlap and interruption from turns
    for t in turns:
        if t['has_overlap']:
            # Find overlapping tester/device segments
            tester_segs = [s for s in segments if s['segment_id'] in t['tester_segment_ids']]
            device_segs = [s for s in segments if s['segment_id'] in t['device_segment_ids']]
            for ts in tester_segs:
                for ds in device_segs:
                    overlap_start = max(ts['start_ms'], ds['start_ms'])
                    overlap_end = min(ts['end_ms'], ds['end_ms'])
                    if overlap_end - overlap_start >= overlap_min_ms:
                        ev_ids = [ev_map[ts['segment_id']], ev_map[ds['segment_id']]]
                        events.append(_event(f'EVT-{event_num + 1:04d}', 'overlap_start', overlap_start, overlap_start,
                                             t['turn_id'], t.get('response_id'), 'derived', 0.8, ev_ids))
                        event_num += 1
                        events.append(_event(f'EVT-{event_num + 1:04d}', 'overlap_end', overlap_end, overlap_end,
                                             t['turn_id'], t.get('response_id'), 'derived', 0.8, ev_ids))
                        event_num += 1
        if t['has_interruption']:
            # The tester segment that interrupted device speech
            tester_segs = [s for s in segments if s['segment_id'] in t['tester_segment_ids']]
            for ts in tester_segs:
                ev_ids = [ev_map[ts['segment_id']]]
                events.append(_event(f'EVT-{event_num + 1:04d}', 'interrupt_start', ts['start_ms'], ts['start_ms'],
                                     t['turn_id'], None, 'derived', 0.7, ev_ids))
                event_num += 1

    # Detect possible false endpoint: short tester segment followed by immediate device response
    for i, seg in enumerate(segments):
        if (seg['speaker_role'] == 'tester' and
                seg['end_ms'] - seg['start_ms'] < false_endpoint_ms and
                i + 1 < len(segments) and
                segments[i + 1]['speaker_role'] == 'device' and
                segments[i + 1]['start_ms'] - seg['end_ms'] < 200):
            ev_ids = [ev_map[seg['segment_id']]]
            events.append(_event(f'EVT-{event_num + 1:04d}', 'possible_false_endpoint', seg['end_ms'], seg['end_ms'],
                                 None, None, 'derived', 0.5, ev_ids))
            event_num += 1

    # Sort events by start_ms
    events.sort(key=lambda e: e['start_ms'])

    status = 'complete' if events else 'insufficient_evidence'
    reason = None if events else 'No events could be detected'
    return events, evidence, status, reason


def _event(event_id, event_type, start_ms, end_ms, turn_id, response_id,
           source, confidence, evidence_ids):
    """Build a schema-compliant Event 2.0.0 dict."""
    return {
        'schema_version': '2.0.0',
        'event_id': event_id,
        'run_id': 'RUN-auto',
        'case_id': 'CASE-auto',
        'turn_id': turn_id,
        'response_id': response_id,
        'type': event_type,
        'start_ms': round(start_ms, 3),
        'end_ms': round(end_ms, 3),
        'source': source,
        'observation_scope': 'black_box',
        'confidence': round(confidence, 4),
        'evidence_ids': evidence_ids,
    }


def generate_timeline(fused_doc, turns_doc, events, evidence, status, reason):
    """Build an EventTimeline 2.0.0 from detected events and evidence."""
    duration_ms = fused_doc.get('source', {}).get('duration_ms', 0)
    audio_sha = fused_doc.get('source', {}).get('audio_sha256', '0' * 64)

    timeline = {
        'schema_version': '2.0.0',
        'run_id': 'RUN-auto',
        'case_id': 'CASE-auto',
        'case_version': '0.0.0',
        'attempt': 1,
        'execution_kind': 'imported',
        'time_base': {'kind': 'run_monotonic_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {
            'device': None, 'hardware': None, 'firmware': None,
            'model': None, 'prompt': None, 'environment': None,
            'test_assets': {
                'golden_set_id': None, 'golden_set_version': None,
                'case_sha256': None, 'asset_sha256': {},
            },
        },
        'status': status if status != 'insufficient_evidence' else 'partial',
        'gaps': [{
            'reason': reason or 'Automatic event detection did not produce complete coverage',
            'required_evidence': 'speaker_diarization_or_human_review',
            'start_ms': 0,
            'end_ms': round(duration_ms, 3),
        }] if status == 'insufficient_evidence' or not events else [],
        'tracks': [{
            'track_id': 'TRACK-mix',
            'role': 'room_mix',
            'clock_id': 'CLOCK-canonical',
            'sample_rate_hz': 16000,
            'sync': {
                'status': 'uncalibrated',
                'offset_ms': None,
                'uncertainty_ms': None,
                'max_drift_ppm': None,
                'calibration_id': None,
            },
        }],
        'artifacts': [{
            'artifact_id': 'ART-audio',
            'kind': 'audio',
            'path': fused_doc.get('source', {}).get('acoustic_document_id', 'unknown') or 'unknown',
            'sha256': audio_sha,
            'origin': 'imported',
            'track_id': 'TRACK-mix',
            'duration_ms': round(duration_ms, 3),
        }],
        'evidence': evidence,
        'events': events,
    }
    return timeline


def analyze_segments(acoustic_path, transcript_path=None, output=None, *,
                      timeout_ms=DEFAULT_TIMEOUT_MS,
                      false_endpoint_ms=DEFAULT_FALSE_ENDPOINT_MS):
    """Run the full fusion → turns → events → timeline pipeline.

    Reads acoustic-segments JSON and optional transcript JSON.
    Returns (fused_doc, turns_doc, timeline_doc).
    """
    import json
    from pathlib import Path

    acoustic_doc = json.loads(Path(acoustic_path).read_text(encoding='utf-8'))
    transcript_doc = None
    if transcript_path:
        transcript_doc = json.loads(Path(transcript_path).read_text(encoding='utf-8'))

    fused_doc = fuse(acoustic_doc, transcript_doc)
    turns_doc = build_turns(fused_doc)
    events, evidence, status, reason = detect_events(
        fused_doc, turns_doc, timeout_ms=timeout_ms, false_endpoint_ms=false_endpoint_ms)
    timeline_doc = generate_timeline(fused_doc, turns_doc, events, evidence, status, reason)

    if output is not None:
        out = Path(output).resolve()
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / 'fused-segments.json', fused_doc)
        write_json(out / 'turns.json', turns_doc)
        write_json(out / 'timeline.json', timeline_doc)

    return fused_doc, turns_doc, timeline_doc
