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
    speaker_id: str | None = None  # from diarization: speaker_0, speaker_1, ...
    speaker_cluster_confidence: float | None = None  # confidence of clustering
    role_attribution_confidence: float | None = None  # confidence of role mapping
    acoustic_boundary_confidence: float | None = None  # confidence of acoustic onset/offset
    acoustic_uncertainty_ms: float | None = None  # acoustic boundary uncertainty
    speaker_source: str = 'acoustic'  # acoustic | diarization | ambiguous | heuristic | llm | manual
    timing_source: str = 'acoustic'  # acoustic | asr | fused
    text: str | None = None
    acoustic_segment_id: str | None = None
    asr_segment_id: str | None = None
    # How the speaker_id was obtained, kept as reviewable evidence.
    speaker_evidence: str = 'none'  # single_overlap | split_multiple_speakers | ambiguous_overlap | none
    speaker_candidates: list = field(default_factory=list)  # [{speaker_id, overlap_ms}]
    segment_origin: str = 'acoustic_segment'  # acoustic_segment | speaker_split

    def to_dict(self, segment_id):
        return {
            'segment_id': segment_id,
            'start_ms': round(self.start_ms, 3),
            'end_ms': round(self.end_ms, 3),
            'speaker_role': self.speaker_role,
            'speaker_id': self.speaker_id,
            'speaker_cluster_confidence': round(self.speaker_cluster_confidence, 4) if self.speaker_cluster_confidence is not None else None,
            'role_attribution_confidence': round(self.role_attribution_confidence, 4) if self.role_attribution_confidence is not None else None,
            'acoustic_boundary_confidence': round(self.acoustic_boundary_confidence, 4) if self.acoustic_boundary_confidence is not None else None,
            'acoustic_uncertainty_ms': round(self.acoustic_uncertainty_ms, 3) if self.acoustic_uncertainty_ms is not None else None,
            'speaker_source': self.speaker_source,
            'timing_source': self.timing_source,
            'text': self.text,
            'acoustic_segment_id': self.acoustic_segment_id,
            'asr_segment_id': self.asr_segment_id,
            'speaker_evidence': self.speaker_evidence,
            'speaker_candidates': [
                {'speaker_id': c['speaker_id'], 'overlap_ms': round(c['overlap_ms'], 3)}
                for c in self.speaker_candidates
            ],
            'segment_origin': self.segment_origin,
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
    # Interruption identity: which old response was interrupted and by which tester segment.
    # PRD-M005 requires binding barge-in stop latency to the interrupted old response_id.
    interrupted_response_id: str | None = None
    interrupting_segment_ids: list = field(default_factory=list)


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
            speaker_role='unknown',
            speaker_id=None,
            speaker_cluster_confidence=None,
            role_attribution_confidence=None,
            acoustic_boundary_confidence=aseg.get('confidence'),
            acoustic_uncertainty_ms=aseg.get('uncertainty_ms'),
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


def _role_lookup(attribution_doc):
    """Map speaker_id → {role, confidence, method} from a source attribution document."""
    lookup = {}
    for attr in (attribution_doc or {}).get('attributions') or []:
        lookup[attr['speaker_id']] = {
            'role': attr.get('role', 'unknown'),
            'confidence': attr.get('confidence'),
            'method': attr.get('method'),
        }
    return lookup


def apply_speakers(fused_doc, diarization_doc=None, attribution_doc=None):
    """Attach diarization speaker clusters and attribution roles to fused segments.

    Speaker clustering (which segments share a speaker) and source attribution
    (which speaker is the tester) are separate evidence. This function only
    copies evidence that exists; it never infers a role from speaker order,
    speaker count, or which cluster spoke first.

    When one acoustic segment overlaps several speaker clusters, the segment is
    split at the cluster boundaries so each part keeps its own speaker evidence.
    If the overlapping clusters conflict in time (mutually exclusive labels for
    the same instant), the segment abstains: speaker_id stays null and the
    candidates are recorded for review. A whole segment is never blanket-assigned
    to "the most overlapping" speaker.

    Mutates and returns fused_doc.
    """
    segments = fused_doc.get('segments', [])
    speaker_segments = (diarization_doc or {}).get('speaker_segments') or []
    roles = _role_lookup(attribution_doc)

    if not speaker_segments:
        fused_doc['attribution']['note'] = (
            'No speaker clustering evidence; speaker_id remains null. '
            'Role attribution requires diarization or human review, and is never '
            'inferred from acoustic order.')
        return fused_doc

    fused_doc['attribution']['strategy'] = 'diarization_provider'
    fused_doc['attribution']['provider'] = (diarization_doc.get('processor') or {}).get('provider')
    fused_doc['attribution']['note'] = (
        'Fused with diarization + source attribution. speaker_id from diarization, '
        'speaker_role from attribution. Multi-speaker acoustic segments are split or '
        'abstained, never assigned to the largest overlap.')

    output = []
    abstained = 0
    for seg in segments:
        start, end = seg['start_ms'], seg['end_ms']
        if end <= start:
            output.append(seg)
            continue

        clipped = []
        for speaker in speaker_segments:
            overlap = min(end, speaker['end_ms']) - max(start, speaker['start_ms'])
            if overlap > 0:
                clipped.append({
                    'start_ms': max(start, speaker['start_ms']),
                    'end_ms': min(end, speaker['end_ms']),
                    'speaker_id': speaker['speaker_id'],
                    'confidence': speaker.get('confidence'),
                })
        if not clipped:
            output.append(seg)
            continue

        candidates = {}
        for item in clipped:
            key = item['speaker_id']
            candidates[key] = max(candidates.get(key, 0.0), item['end_ms'] - item['start_ms'])
        seg['speaker_candidates'] = [
            {'speaker_id': key, 'overlap_ms': value}
            for key, value in sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        if len(candidates) == 1:
            speaker_id = next(iter(candidates))
            _assign_speaker(seg, speaker_id, clipped[0]['confidence'], roles, 'single_overlap')
            output.append(seg)
            continue

        # Conflicting clusters: two differently labeled speaker segments claim the
        # same instant. That is a clustering contradiction, not a close call.
        ordered = sorted(clipped, key=lambda item: (item['start_ms'], item['end_ms']))
        conflict = any(
            ordered[i]['end_ms'] > ordered[i + 1]['start_ms']
            and ordered[i]['speaker_id'] != ordered[i + 1]['speaker_id']
            for i in range(len(ordered) - 1)
        )
        if conflict:
            abstained += 1
            seg['speaker_id'] = None
            seg['speaker_role'] = 'unknown'
            seg['speaker_cluster_confidence'] = None
            seg['role_attribution_confidence'] = None
            seg['speaker_source'] = 'ambiguous'
            seg['speaker_evidence'] = 'ambiguous_overlap'
            output.append(seg)
            continue

        # Non-conflicting: split the acoustic segment at cluster boundaries.
        output.extend(_split_by_speaker(seg, ordered, roles))
        continue

    fused_doc['segments'] = output
    unclustered = sum(1 for seg in output if seg.get('speaker_id') is None)
    unroled = sum(1 for seg in output if seg.get('speaker_role') == 'unknown')
    if not unclustered and not unroled:
        fused_doc['status'] = 'complete'
        fused_doc['reason'] = None
    else:
        fused_doc['status'] = 'partial'
        notes = []
        if unclustered:
            notes.append(f'{unclustered} segment(s) have no speaker cluster')
        if abstained:
            notes.append(f'{abstained} abstained on conflicting speaker overlaps')
        if unroled:
            notes.append(f'{unroled} segment(s) have no role evidence')
        fused_doc['reason'] = '; '.join(notes)
    return fused_doc


def _assign_speaker(seg, speaker_id, confidence, roles, evidence):
    seg['speaker_id'] = speaker_id
    seg['speaker_source'] = 'diarization'
    seg['speaker_cluster_confidence'] = confidence
    seg['speaker_evidence'] = evidence
    role = roles.get(speaker_id)
    if role and role['role'] != 'unknown':
        seg['speaker_role'] = role['role']
        seg['role_attribution_confidence'] = role['confidence']
    else:
        # No role evidence: the cluster is known, the person is not.
        seg['speaker_role'] = 'unknown'
        seg['role_attribution_confidence'] = None


def _split_by_speaker(seg, ordered, roles):
    """Split one fused segment into per-speaker sub-segments.

    The ASR text is kept on the longest sub-segment only, so the same utterance
    cannot be counted twice downstream. Every sub-segment keeps the parent
    acoustic_segment_id and asr_segment_id so provenance still resolves.
    """
    pieces = []
    for item in ordered:
        if pieces and pieces[-1]['speaker_id'] == item['speaker_id'] and pieces[-1]['end_ms'] >= item['start_ms']:
            pieces[-1]['end_ms'] = max(pieces[-1]['end_ms'], item['end_ms'])
        else:
            pieces.append({'start_ms': item['start_ms'], 'end_ms': item['end_ms'],
                           'speaker_id': item['speaker_id'], 'confidence': item['confidence']})
    longest = max(range(len(pieces)), key=lambda i: pieces[i]['end_ms'] - pieces[i]['start_ms'])
    results = []
    for index, piece in enumerate(pieces):
        sub = dict(seg)
        sub['start_ms'] = piece['start_ms']
        sub['end_ms'] = piece['end_ms']
        sub['text'] = seg.get('text') if index == longest else None
        sub['segment_origin'] = 'speaker_split'
        sub['speaker_candidates'] = [{'speaker_id': piece['speaker_id'],
                                      'overlap_ms': piece['end_ms'] - piece['start_ms']}]
        _assign_speaker(sub, piece['speaker_id'], piece['confidence'], roles, 'split_multiple_speakers')
        results.append(sub)
    return results


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
                    # Bind interruption identity: which old response was interrupted,
                    # and which tester segment did the interrupting (PRD-M005).
                    current_turn['interrupted_response_id'] = current_turn.get('response_id')
                    current_turn['interrupting_segment_ids'] = [seg_id]
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
                    'interrupted_response_id': None,
                    'interrupting_segment_ids': [],
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
                    'interrupted_response_id': None,
                    'interrupting_segment_ids': [],
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
        'interrupted_response_id': t.get('interrupted_response_id'),
        'interrupting_segment_ids': list(t.get('interrupting_segment_ids', [])),
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
            'time_base': 'audio_relative_ms',
            'start_ms': seg['start_ms'],
            'end_ms': seg['end_ms'],
            'source': 'audio_signal',
            'confidence': seg.get('role_attribution_confidence') if seg.get('role_attribution_confidence') is not None
                else seg.get('speaker_cluster_confidence'),
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
        # Acoustic boundary confidence comes from acoustic segmentation, NOT from
        # speaker clustering or role attribution. Those are different dimensions.
        acoustic_conf = seg.get('acoustic_boundary_confidence')
        acoustic_source = 'acoustic_boundary' if acoustic_conf is not None else None
        acoustic_uncertainty = seg.get('acoustic_uncertainty_ms')

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
                                 turn_id, response_id, 'audio_signal', acoustic_conf, [ev_id],
                                 acoustic_source, acoustic_uncertainty))
            event_num += 1
            events.append(_event(f'EVT-{event_num + 1:04d}', 'tester_speech_end', end, end,
                                 turn_id, response_id, 'audio_signal', acoustic_conf, [ev_id],
                                 acoustic_source, acoustic_uncertainty))
            event_num += 1
        elif role == 'device':
            events.append(_event(f'EVT-{event_num + 1:04d}', 'device_speech_start', start, start,
                                 turn_id, response_id, 'audio_signal', acoustic_conf, [ev_id],
                                 acoustic_source, acoustic_uncertainty))
            event_num += 1
            events.append(_event(f'EVT-{event_num + 1:04d}', 'device_speech_end', end, end,
                                 turn_id, response_id, 'audio_signal', acoustic_conf, [ev_id],
                                 acoustic_source, acoustic_uncertainty))
            event_num += 1
            # Response start/end for the first device segment in a turn
            if turn_id and response_id:
                t = turn_lookup.get(turn_id)
                if t and sid == t['device_segment_ids'][0]:
                    events.append(_event(f'EVT-{event_num + 1:04d}', 'response_start', start, start,
                                         turn_id, response_id, 'derived', acoustic_conf, [ev_id],
                                         acoustic_source, acoustic_uncertainty))
                    event_num += 1
                if t and sid == t['device_segment_ids'][-1]:
                    events.append(_event(f'EVT-{event_num + 1:04d}', 'response_end', end, end,
                                         turn_id, response_id, 'derived', acoustic_conf, [ev_id],
                                         acoustic_source, acoustic_uncertainty))
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
                                     None, None, 'derived', None, [eid]))
                event_num += 1
            else:
                events.append(_event(f'EVT-{event_num + 1:04d}', 'silence', gap_start, gap_end,
                                     None, None, 'derived', None, [eid]))
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
                                             t['turn_id'], t.get('response_id'), 'derived', None, ev_ids))
                        event_num += 1
                        events.append(_event(f'EVT-{event_num + 1:04d}', 'overlap_end', overlap_end, overlap_end,
                                             t['turn_id'], t.get('response_id'), 'derived', None, ev_ids))
                        event_num += 1
        if t['has_interruption']:
            # PRD-M005: interrupt_start must bind to the interrupted OLD response_id,
            # and be timestamped at the interrupting tester segment (not the original tester).
            old_response_id = t.get('interrupted_response_id') or t.get('response_id')
            for sid in t.get('interrupting_segment_ids', []):
                seg = next((s for s in segments if s['segment_id'] == sid), None)
                if seg is None:
                    continue
                ev_ids = [ev_map[sid]]
                events.append(_event(f'EVT-{event_num + 1:04d}', 'interrupt_start',
                                     seg['start_ms'], seg['start_ms'],
                                     t['turn_id'], old_response_id, 'derived', None, ev_ids))
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
                                 None, None, 'derived', None, ev_ids))
            event_num += 1

    # Sort events by start_ms
    events.sort(key=lambda e: e['start_ms'])

    status = 'complete' if events else 'insufficient_evidence'
    reason = None if events else 'No events could be detected'
    return events, evidence, status, reason


def _event(event_id, event_type, start_ms, end_ms, turn_id, response_id,
           source, confidence, evidence_ids, confidence_source=None,
           uncertainty_ms=None):
    """Build a schema-compliant Event 2.0.0 dict.

    confidence stays None when no defensible value exists. Unknown is not zero.
    confidence_source records which dimension the number belongs to
    (acoustic_boundary / speaker_cluster / role_attribution / semantic_event).
    """
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
        'confidence': round(confidence, 4) if confidence is not None else None,
        'confidence_source': confidence_source,
        'uncertainty_ms': uncertainty_ms,
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
        'time_base': {'kind': 'audio_relative_ms', 'origin': 'first_decoded_sample'},
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
