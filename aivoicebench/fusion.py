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
    # How the role was decided and what its confidence number means, carried down to
    # every downstream result so a model proposal is never mistaken for human evidence.
    role_attribution: dict | None = None
    timing_source: str = 'acoustic'  # acoustic | asr | fused
    text: str | None = None
    acoustic_segment_id: str | None = None
    asr_segment_id: str | None = None
    # How the speaker_id was obtained, kept as reviewable evidence.
    speaker_evidence: str = 'none'  # single_overlap | split_multiple_speakers | ambiguous_overlap | none
    speaker_candidates: list = field(default_factory=list)  # [{speaker_id, overlap_ms}]
    segment_origin: str = 'acoustic_segment'  # acoustic_segment | speaker_split
    # ASR utterance evidence: which utterance supplied the text, over what interval,
    # and which speaker label the recognition service itself reported for it.
    asr_start_ms: float | None = None
    asr_end_ms: float | None = None
    asr_speaker_id: str | None = None
    # Text attribution stays explicit so no reader assumes the text belongs to this
    # segment's speaker. 'ambiguous_spans_speakers' means the utterance crossed a
    # speaker boundary and was therefore withheld from every sub-segment.
    text_attribution: str = 'none'  # none | single_segment | utterance_speaker | contained_in_sub_segment | ambiguous_spans_speakers
    # Boundary provenance per edge. A split introduces provider-estimated boundaries,
    # which must not inherit the acoustic segmenter's confidence or uncertainty.
    start_boundary_source: str = 'acoustic_segment'  # acoustic_segment | provider_speaker_estimate
    end_boundary_source: str = 'acoustic_segment'  # acoustic_segment | provider_speaker_estimate

    def to_dict(self, segment_id):
        return {
            'segment_id': segment_id,
            'start_ms': round(self.start_ms, 3),
            'end_ms': round(self.end_ms, 3),
            'speaker_role': self.speaker_role,
            'speaker_id': self.speaker_id,
            'speaker_cluster_confidence': round(self.speaker_cluster_confidence, 4) if self.speaker_cluster_confidence is not None else None,
            'role_attribution_confidence': round(self.role_attribution_confidence, 4) if self.role_attribution_confidence is not None else None,
            'role_attribution': self.role_attribution,
            'acoustic_boundary_confidence': round(self.acoustic_boundary_confidence, 4) if self.acoustic_boundary_confidence is not None else None,
            'acoustic_uncertainty_ms': round(self.acoustic_uncertainty_ms, 3) if self.acoustic_uncertainty_ms is not None else None,
            'speaker_source': self.speaker_source,
            'timing_source': self.timing_source,
            'text': self.text,
            'acoustic_segment_id': self.acoustic_segment_id,
            'asr_segment_id': self.asr_segment_id,
            'asr_start_ms': round(self.asr_start_ms, 3) if self.asr_start_ms is not None else None,
            'asr_end_ms': round(self.asr_end_ms, 3) if self.asr_end_ms is not None else None,
            'asr_speaker_id': self.asr_speaker_id,
            'text_attribution': self.text_attribution,
            'start_boundary_source': self.start_boundary_source,
            'end_boundary_source': self.end_boundary_source,
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

    # Build ASR lookup: map overlapping time ranges to text.
    # The ASR utterance keeps its own speaker label and interval, because that is
    # the only defensible basis for saying WHO said a piece of text. A fused
    # segment's boundaries come from acoustic segmentation and may later be split
    # at diarization boundaries; text is never re-attributed by segment length.
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
                'speaker_id': seg.get('speaker_id'),
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
        asr_start = asr_end = None
        asr_speaker = None
        if asr and asr.get('text'):
            text = asr['text']
            asr_id = asr.get('segment_id')
            asr_start, asr_end = asr['start_ms'], asr['end_ms']
            asr_speaker = asr.get('speaker_id')
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
            asr_start_ms=asr_start,
            asr_end_ms=asr_end,
            asr_speaker_id=asr_speaker,
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
        'unattributed_texts': [],
        'segments': [seg.to_dict(f'FSEG-{i:04d}') for i, seg in enumerate(fused)],
    }


def _role_lookup(attribution_doc):
    """Map speaker_id → role evidence from a source attribution document.

    Carries how the role was decided and what its confidence number means, so a
    downstream reader can tell an explicit mapping from an uncalibrated model
    proposal without re-reading the attribution artifact.
    """
    lookup = {}
    for attr in (attribution_doc or {}).get('attributions') or []:
        lookup[attr['speaker_id']] = {
            'role': attr.get('role', 'unknown'),
            'confidence': attr.get('confidence'),
            'confidence_basis': attr.get('confidence_basis') or (
                'explicit_user_evidence' if attr.get('method') == 'explicit_evidence'
                else 'human_review' if attr.get('method') == 'human_attribution'
                else 'uncalibrated_model_self_report' if attr.get('method') == 'semantic_attribution'
                else 'none'),
            'method': attr.get('method'),
            'provider': attr.get('provider'),
            'model': attr.get('model'),
            'prompt_version': attr.get('prompt_version'),
            'invocation_id': attr.get('invocation_id'),
            'needs_review': attr.get('needs_review', False),
        }
    return lookup


def apply_speakers(fused_doc, diarization_doc=None, attribution_doc=None, alignment_doc=None):
    """Attach diarization speaker clusters and attribution roles to fused segments.

    Speaker clustering (which segments share a speaker) and source attribution
    (which speaker is the tester) are separate evidence. This function only
    copies evidence that exists; it never infers a role from speaker order,
    speaker count, or which cluster spoke first.

    The acoustic↔speaker-span decision is delegated to the deterministic
    alignment policy in ``alignment.py``: every acoustic segment is classified as
    ``unmatched``, ``single_cluster``, ``multi_cluster`` or ``conflict`` from its
    overlap/coverage facts. This function only materialises that decision, so one
    policy owns the overlap rule instead of two implementations drifting apart.

    When one acoustic segment overlaps several clusters, the segment is split at
    the cluster boundaries so each part keeps its own speaker evidence. If the
    overlapping clusters conflict in time (mutually exclusive labels for the same
    instant), the segment abstains: speaker_id stays null and the candidates are
    recorded for review. A whole segment is never blanket-assigned to "the most
    overlapping" speaker, and the ASR text is never handed to a speaker because
    that speaker's piece happened to be the longest.

    ``alignment_doc`` lets the caller pass the alignment computed against the real
    acoustic document (which carries frame statistics for the low-energy
    diagnostics). When omitted, an equivalent alignment is derived from the fused
    segments so direct callers still use the same policy.

    Mutates and returns fused_doc.
    """
    from .alignment import align_speaker_spans, alignment_for

    segments = fused_doc.get('segments', [])
    speaker_segments = (diarization_doc or {}).get('speaker_segments') or []
    roles = _role_lookup(attribution_doc)
    # Native provider labels are only meaningful inside this speaker output scope.
    native_to_local = {}
    for speaker in speaker_segments:
        native = speaker.get('native_speaker_id')
        if native is not None:
            native_to_local[str(native)] = speaker['speaker_id']

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

    if alignment_doc is None:
        alignment_doc = align_speaker_spans(
            {'document_id': None,
             'source': {'sha256': fused_doc.get('source', {}).get('audio_sha256')},
             'segments': [{'segment_id': seg.get('acoustic_segment_id') or seg['segment_id'],
                           'start_ms': seg['start_ms'], 'end_ms': seg['end_ms'],
                           'confidence': seg.get('acoustic_boundary_confidence'),
                           'uncertainty_ms': seg.get('acoustic_uncertainty_ms')}
                          for seg in segments]},
            diarization_doc)
    entries = alignment_for(fused_doc, alignment_doc)
    speaker_order = {speaker.get('segment_id'): index
                     for index, speaker in enumerate(speaker_segments)}

    output = []
    abstained = 0
    unattributed = list(fused_doc.get('unattributed_texts') or [])
    for seg in segments:
        start, end = seg['start_ms'], seg['end_ms']
        if end <= start:
            output.append(seg)
            continue

        entry = entries.get(seg.get('acoustic_segment_id') or seg.get('segment_id'))
        if entry is None or entry['state'] == 'unmatched':
            output.append(seg)
            continue

        seg['speaker_candidates'] = [{'speaker_id': candidate['speaker_id'],
                                      'overlap_ms': candidate['overlap_ms']}
                                     for candidate in entry['speaker_candidates']]
        # Rebuild the overlapping spans in speaker-output order: when one cluster
        # covers the segment through several spans, the assignment keeps the
        # provider confidence of that cluster's first reported span.
        spans = sorted(({'start_ms': item['overlap_start_ms'], 'end_ms': item['overlap_end_ms'],
                         'speaker_id': item['speaker_id'], 'confidence': item['cluster_confidence'],
                         'alignment': item} for item in entry['speaker_matches']),
                       key=lambda item: speaker_order.get(item['alignment']['speaker_segment_id'],
                                                          len(speaker_order)))

        if entry['state'] == 'single_cluster':
            speaker_id = entry['matched_speaker_id']
            _assign_speaker(seg, speaker_id, spans[0]['confidence'], roles, 'single_overlap')
            if seg.get('text'):
                seg['text_attribution'] = 'single_segment'
            output.append(seg)
            continue

        # Conflicting clusters: two differently labeled speaker segments claim the
        # same instant. That is a clustering contradiction, not a close call.
        if entry['state'] == 'conflict':
            abstained += 1
            seg['speaker_id'] = None
            seg['speaker_role'] = 'unknown'
            seg['speaker_cluster_confidence'] = None
            seg['role_attribution_confidence'] = None
            seg['speaker_source'] = 'ambiguous'
            seg['speaker_evidence'] = 'ambiguous_overlap'
            # The segment spans contradictory clusters, so its text cannot be
            # attributed to a speaker either. Withhold it rather than guess.
            if seg.get('text'):
                seg['text_attribution'] = 'ambiguous_spans_speakers'
                unattributed.append(_unattributed(seg))
                seg['text'] = None
            output.append(seg)
            continue

        # Non-conflicting: split the acoustic segment at cluster boundaries.
        ordered = sorted(spans, key=lambda item: (item['start_ms'], item['end_ms'],
                                                  item['speaker_id']))
        pieces, attribution, text = _split_by_speaker(seg, ordered, roles, native_to_local)
        if text and attribution == 'ambiguous_spans_speakers':
            unattributed.append(_unattributed(seg))
        output.extend(pieces)
        continue

    fused_doc['segments'] = output
    fused_doc['unattributed_texts'] = unattributed
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


def _unattributed(seg):
    """Record a withheld utterance so it stays reviewable instead of silently lost."""
    return {
        'asr_segment_id': seg.get('asr_segment_id'),
        'acoustic_segment_id': seg.get('acoustic_segment_id'),
        'start_ms': seg.get('asr_start_ms'),
        'end_ms': seg.get('asr_end_ms'),
        'text': seg.get('text'),
        'reason': 'Utterance spans more than one speaker cluster; it is not '
                  'attributed to any speaker without further evidence',
    }


# Confidence bases that may be reported as a role confidence number. An uncalibrated
# model self-report is NOT one of them: it is recorded as evidence, never as a
# calibrated accuracy that downstream results could read as measured performance.
CALIBRATED_ROLE_BASES = ('explicit_user_evidence', 'human_review')


def _assign_speaker(seg, speaker_id, confidence, roles, evidence):
    seg['speaker_id'] = speaker_id
    seg['speaker_source'] = 'diarization'
    seg['speaker_cluster_confidence'] = confidence
    seg['speaker_evidence'] = evidence
    role = roles.get(speaker_id)
    if role and role['role'] != 'unknown':
        seg['speaker_role'] = role['role']
        basis = role.get('confidence_basis')
        # Only calibrated evidence may publish a role confidence number.
        seg['role_attribution_confidence'] = (role['confidence']
                                              if basis in CALIBRATED_ROLE_BASES else None)
        seg['role_attribution'] = {
            'method': role.get('method'),
            'basis': basis,
            'provider': role.get('provider'),
            'model': role.get('model'),
            'prompt_version': role.get('prompt_version'),
            'invocation_id': role.get('invocation_id'),
            'needs_review': bool(role.get('needs_review')),
            'reported_confidence': role.get('confidence') if basis not in CALIBRATED_ROLE_BASES else None,
        }
    else:
        # No role evidence: the cluster is known, the person is not.
        seg['speaker_role'] = 'unknown'
        seg['role_attribution_confidence'] = None
        seg['role_attribution'] = None


def _split_by_speaker(seg, ordered, roles, native_to_local):
    """Split one fused segment into per-speaker sub-segments.

    Text attribution is evidence-based, never positional or length-based:

    1. If the recognition service itself labeled the utterance with a speaker,
       the text goes to that speaker's sub-segment (`utterance_speaker`).
    2. Else if the utterance interval falls entirely inside one sub-segment, the
       text goes there (`contained_in_sub_segment`).
    3. Else the utterance provably crosses a speaker boundary, so it is withheld
       from every sub-segment (`ambiguous_spans_speakers`) and recorded in the
       document-level `unattributed_texts` for human review. It is never copied to
       several speakers.

    Boundary provenance is tracked per edge: a sub-segment edge that comes from the
    provider's speaker estimate must not inherit the acoustic segmenter's
    confidence or uncertainty.
    """
    pieces = []
    for item in ordered:
        if pieces and pieces[-1]['speaker_id'] == item['speaker_id'] and pieces[-1]['end_ms'] >= item['start_ms']:
            pieces[-1]['end_ms'] = max(pieces[-1]['end_ms'], item['end_ms'])
        else:
            pieces.append({'start_ms': item['start_ms'], 'end_ms': item['end_ms'],
                           'speaker_id': item['speaker_id'], 'confidence': item['confidence']})

    parent_start, parent_end = seg['start_ms'], seg['end_ms']
    text = seg.get('text')
    asr_start, asr_end = seg.get('asr_start_ms'), seg.get('asr_end_ms')
    native = seg.get('asr_speaker_id')
    owner_index = None
    attribution = 'none'
    if text:
        # 1. Trust the service's own label for this utterance when it resolves here.
        local = native_to_local.get(str(native)) if native is not None else None
        if local is not None:
            for index, piece in enumerate(pieces):
                if piece['speaker_id'] == local:
                    owner_index, attribution = index, 'utterance_speaker'
                    break
        # 2. Otherwise require the utterance to sit inside exactly one sub-segment.
        if owner_index is None and asr_start is not None and asr_end is not None:
            inside = [index for index, piece in enumerate(pieces)
                      if asr_start >= piece['start_ms'] and asr_end <= piece['end_ms']]
            if len(inside) == 1:
                owner_index, attribution = inside[0], 'contained_in_sub_segment'
        if owner_index is None:
            attribution = 'ambiguous_spans_speakers'

    results = []
    for index, piece in enumerate(pieces):
        sub = dict(seg)
        sub['start_ms'] = piece['start_ms']
        sub['end_ms'] = piece['end_ms']
        sub['text'] = text if index == owner_index else None
        sub['text_attribution'] = attribution if text else 'none'
        sub['segment_origin'] = 'speaker_split'
        sub['speaker_candidates'] = [{'speaker_id': piece['speaker_id'],
                                      'overlap_ms': piece['end_ms'] - piece['start_ms']}]
        # Boundary provenance: only an edge that is still the acoustic edge may
        # carry acoustic confidence and uncertainty.
        sub['start_boundary_source'] = ('acoustic_segment' if piece['start_ms'] <= parent_start
                                        else 'provider_speaker_estimate')
        sub['end_boundary_source'] = ('acoustic_segment' if piece['end_ms'] >= parent_end
                                      else 'provider_speaker_estimate')
        if (sub['start_boundary_source'] != 'acoustic_segment'
                or sub['end_boundary_source'] != 'acoustic_segment'):
            sub['acoustic_boundary_confidence'] = None
            sub['acoustic_uncertainty_ms'] = None
        _assign_speaker(sub, piece['speaker_id'], piece['confidence'], roles, 'split_multiple_speakers')
        results.append(sub)
    return results, attribution, text


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
                  overlap_min_ms=DEFAULT_OVERLAP_MIN_MS,
                  run_id='RUN-auto', case_id='CASE-auto'):
    """Detect events from fused segments and turns.

    Returns a list of event dicts (Event 2.0.0 schema).
    Events carry source=audio_signal or derived, confidence, and evidence refs.

    `run_id`/`case_id` must be the identity of the Run the events belong to. The
    defaults exist only for standalone CLI use; an imported Run passes its own
    identity so the timeline, its events and its metrics agree.
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
        # Confidence stays None when no defensible number exists. A semantic role
        # proposal publishes no role confidence, so it must not become 0.0 here.
        role_conf = seg.get('role_attribution_confidence')
        cluster_conf = seg.get('speaker_cluster_confidence')
        if role_conf is not None:
            confidence, confidence_source = role_conf, 'role_attribution'
        elif cluster_conf is not None:
            confidence, confidence_source = cluster_conf, 'speaker_cluster'
        else:
            confidence, confidence_source = None, 'none'
        evidence.append({
            'schema_version': '1.0.0',
            'evidence_id': eid,
            'artifact_id': 'ART-audio',
            'track_id': 'TRACK-mix',
            'time_base': 'audio_relative_ms',
            'start_ms': seg['start_ms'],
            'end_ms': seg['end_ms'],
            'source': 'audio_signal',
            'confidence': confidence,
            'confidence_source': confidence_source,
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

    # Detect silence between segments. A gap is a derived interval, so it gets its own
    # evidence snippet covering the gap. Citing a neighbouring speech segment would
    # not cover the interval the event claims, and the timeline contract rejects it.
    for i in range(1, len(segments)):
        gap_start = segments[i - 1]['end_ms']
        gap_end = segments[i]['start_ms']
        if gap_end > gap_start + 1:
            eid = f'EV-{evidence_num + 1:04d}'
            evidence.append({
                'schema_version': '1.0.0',
                'evidence_id': eid,
                'artifact_id': 'ART-audio',
                'track_id': 'TRACK-mix',
                'time_base': 'audio_relative_ms',
                'start_ms': round(gap_start, 3),
                'end_ms': round(gap_end, 3),
                # Derived from two acoustic boundaries rather than measured as a
                # signal, so it publishes no confidence number of its own.
                'source': 'derived',
                'confidence': None,
                'confidence_source': 'none',
            })
            evidence_num += 1
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

    # Stamp the owning Run identity on every event. Events and the timeline they
    # belong to must agree, otherwise the timeline is not a valid measurement record.
    for event in events:
        event['run_id'] = run_id
        event['case_id'] = case_id

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


def generate_timeline(fused_doc, turns_doc, events, evidence, status, reason,
                      run_id='RUN-auto', case_id='CASE-auto', execution_kind='imported'):
    """Build an EventTimeline 2.0.0 from detected events and evidence.

    The Run identity is a parameter, not a placeholder: an imported Run must be able
    to validate its own timeline, and metrics computed from it must agree.
    """
    duration_ms = fused_doc.get('source', {}).get('duration_ms', 0)
    audio_sha = fused_doc.get('source', {}).get('audio_sha256', '0' * 64)

    timeline = {
        'schema_version': '2.0.0',
        'run_id': run_id,
        'case_id': case_id,
        'case_version': '0.0.0',
        'attempt': 1,
        'execution_kind': execution_kind,
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
