"""Deterministic alignment between acoustic segments and ASR speaker spans.

Acoustic boundaries and ASR speaker spans are **independent evidence**. This
module records how they overlap; it never replaces one with the other, and it
never copies the nearest speaker or role merely to populate a metric.

Contract:

- Every acoustic segment gets exactly one alignment entry with an explicit
  ``state``: ``unmatched`` | ``single_cluster`` | ``multi_cluster`` | ``conflict``.
- Matches keep both source intervals, the signed boundary offsets, the overlap
  interval, both overlap ratios and the provider cluster identity.
- ``conflict`` means two differently-labelled clusters claim the same instant.
  That is a clustering contradiction, not a close call, so the segment abstains.
- Coverage is reported with its denominator: unmatched acoustic duration,
  unmatched ASR duration and per-cluster coverage are all explicit, so a low
  number can be investigated instead of being read as "no speech".
- Low-energy segments are reported as a distribution, because a quiet device
  response is a plausible cause of a missing acoustic/speaker overlap and must be
  measured rather than assumed.

No role inference happens here. ``tester``/``device`` remain user decisions.
"""

import math
import uuid

SCHEMA_VERSION = '1.0.0'
PROCESSOR_NAME = 'acoustic_speaker_alignment'
PROCESSOR_VERSION = '1.2.0'

# The canonical policy. `min_overlap_ms` is 0.0 so that any positive overlap is a
# candidate, which is exactly the rule fusion already applies; the remaining
# values are diagnostic thresholds and never change which cluster wins.
DEFAULT_POLICY = {
    'policy_version': '1.0.0',
    'min_overlap_ms': 0.0,
    'boundary_drift_tolerance_ms': 120.0,
    'low_energy_percentile': 25.0,
    'overlap_basis': 'asr_provider_utterance_estimate',
    'note': ('Acoustic boundaries and ASR speaker spans stay independent. '
             'Alignment records overlap/coverage only; it never assigns a '
             'tester/device role and never substitutes provider timing for the '
             'acoustic boundary.'),
}


def resolve_policy(overrides=None):
    """Return the canonical alignment policy, optionally overridden explicitly.

    An override is recorded in the output document, so a non-default comparison
    can never be mistaken for the canonical measurement policy.
    """
    policy = dict(DEFAULT_POLICY)
    for key, value in (overrides or {}).items():
        if key not in DEFAULT_POLICY:
            raise ValueError('Unknown alignment policy field: ' + str(key))
        if key in ('note', 'overlap_basis', 'policy_version'):
            raise ValueError('Alignment identity fields cannot be overridden: ' + key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('Alignment policy values must be finite and non-negative: ' + key)
        if key == 'low_energy_percentile' and not 0 <= value <= 100:
            raise ValueError('low_energy_percentile must be within 0-100')
        policy[key] = value
    policy['overrides'] = {key: value for key, value in sorted((overrides or {}).items())}
    return policy


def _overlap(start, end, other_start, other_end):
    """Return the intersection interval of two spans, or None when disjoint."""
    begin, finish = max(start, other_start), min(end, other_end)
    if finish <= begin:
        return None
    return begin, finish, finish - begin


def _ratio(part, whole):
    if whole <= 0:
        return None
    return round(min(1.0, part / whole), 4)


def align_speaker_spans(acoustic_doc, diarization_doc, *, transcript_doc=None, policy=None):
    """Align every acoustic segment with the ASR speaker spans that overlap it.

    ``acoustic_doc`` is an AcousticSegments document, ``diarization_doc`` a
    DiarizationResult document (or its envelope ``data``). Returns an alignment
    document; it performs no I/O, no clock read and no provider call, so the same
    inputs always produce the same output.
    """
    policy = resolve_policy(policy)
    acoustic_segments = list((acoustic_doc or {}).get('segments') or [])
    speaker_segments = list((diarization_doc or {}).get('speaker_segments') or [])

    native_labels = {}
    for speaker in speaker_segments:
        native = speaker.get('native_speaker_id')
        if native is not None:
            native_labels.setdefault(speaker['speaker_id'], str(native))

    # Per-cluster totals come from the speaker spans themselves, so a cluster's
    # coverage denominator is provider evidence rather than an acoustic guess.
    cluster_speech = {}
    for speaker in speaker_segments:
        span = max(0.0, speaker['end_ms'] - speaker['start_ms'])
        entry = cluster_speech.setdefault(speaker['speaker_id'], {'speaker_speech_ms': 0.0, 'matched_ms': 0.0, 'segments': 0})
        entry['speaker_speech_ms'] += span
        entry['segments'] += 1

    alignments = []
    matched_acoustic_ms = 0.0
    unmatched_acoustic_ms = 0.0
    conflicted_acoustic_ms = 0.0
    drift_values = []
    # The low-energy diagnostic is an *energy* diagnostic. A model-based provider
    # (for example Silero VAD) reports per-frame speech probabilities, not RMS, so
    # its segments carry no energy evidence at all. Treating that missing value as
    # "0.0" would silently mark every model-produced boundary as low energy and
    # hand back a misleading quiet-device diagnosis; instead those segments are
    # excluded from the distribution and reported explicitly.
    energy_values = {}
    for index, seg in enumerate(acoustic_segments):
        value = (seg.get('frame_stats') or {}).get('mean_rms')
        energy_values[index] = float(value) if isinstance(value, (int, float)) else None
    measured_energies = sorted(value for value in energy_values.values() if value is not None)
    low_energy_threshold = (_percentile(measured_energies, policy['low_energy_percentile'])
                            if measured_energies else 0.0)

    for index, seg in enumerate(acoustic_segments):
        seg_energy = energy_values[index]
        start, end = seg['start_ms'], seg['end_ms']
        span_ms = max(0.0, end - start)
        matches = []
        for speaker in speaker_segments:
            hit = _overlap(start, end, speaker['start_ms'], speaker['end_ms'])
            if hit is None:
                continue
            overlap_start, overlap_end, overlap_ms = hit
            if overlap_ms <= policy['min_overlap_ms']:
                continue
            matches.append({
                'speaker_id': speaker['speaker_id'],
                'native_speaker_id': speaker.get('native_speaker_id'),
                'speaker_segment_id': speaker.get('segment_id'),
                'asr_start_ms': round(speaker['start_ms'], 3),
                'asr_end_ms': round(speaker['end_ms'], 3),
                'overlap_start_ms': round(overlap_start, 3),
                'overlap_end_ms': round(overlap_end, 3),
                'overlap_ms': round(overlap_ms, 3),
                'overlap_ratio_of_acoustic': _ratio(overlap_ms, span_ms),
                'overlap_ratio_of_speaker': _ratio(overlap_ms, speaker['end_ms'] - speaker['start_ms']),
                # Signed offsets: positive means the provider span starts later.
                'start_offset_ms': round(speaker['start_ms'] - start, 3),
                'end_offset_ms': round(speaker['end_ms'] - end, 3),
                'cluster_confidence': speaker.get('confidence'),
                'timestamp_source': speaker.get('timestamp_source', 'provider_utterance_estimate'),
            })
        matches.sort(key=lambda item: (item['overlap_start_ms'], item['overlap_end_ms'], item['speaker_id']))

        by_cluster = {}
        for item in matches:
            key = item['speaker_id']
            by_cluster[key] = max(by_cluster.get(key, 0.0), item['overlap_ms'])
        candidates = [{'speaker_id': key, 'overlap_ms': round(value, 3)}
                      for key, value in sorted(by_cluster.items(), key=lambda kv: (-kv[1], kv[0]))]

        state, matched_speaker_id = _classify(matches, candidates)
        boundary_drift_ms = None
        if matched_speaker_id is not None:
            boundary_drift_ms = min(
                abs(item['start_offset_ms']) + abs(item['end_offset_ms'])
                for item in matches if item['speaker_id'] == matched_speaker_id)
            drift_values.append(boundary_drift_ms)

        if state == 'unmatched':
            unmatched_acoustic_ms += span_ms
        elif state == 'conflict':
            conflicted_acoustic_ms += span_ms
        else:
            matched_acoustic_ms += sum(item['overlap_ms'] for item in matches)
            for item in matches:
                cluster = cluster_speech.setdefault(
                    item['speaker_id'],
                    {'speaker_speech_ms': 0.0, 'matched_ms': 0.0, 'segments': 0})
                cluster['matched_ms'] += item['overlap_ms']

        alignments.append({
            'acoustic_segment_id': seg.get('segment_id'),
            'acoustic_start_ms': round(start, 3),
            'acoustic_end_ms': round(end, 3),
            'acoustic_speech_ms': round(span_ms, 3),
            'acoustic_confidence': seg.get('confidence'),
            'acoustic_uncertainty_ms': seg.get('uncertainty_ms'),
            'acoustic_mean_rms': seg_energy,
            'acoustic_peak_rms': (seg.get('frame_stats') or {}).get('peak_rms'),
            # `false` here means "measured and not low", never "not measured".
            # Unmeasured energy is reported by diagnostics.low_energy.energy_evidence.
            'low_energy': bool(seg_energy is not None
                               and seg_energy <= low_energy_threshold),
            'speaker_matches': matches,
            'speaker_candidates': candidates,
            'distinct_cluster_count': len(candidates),
            'state': state,
            'matched_speaker_id': matched_speaker_id,
            'boundary_drift_ms': round(boundary_drift_ms, 3) if boundary_drift_ms is not None else None,
        })

    speaker_speech_ms = sum(value['speaker_speech_ms'] for value in cluster_speech.values())
    matched_speaker_ms = sum(min(value['matched_ms'], value['speaker_speech_ms'])
                             for value in cluster_speech.values())
    covered = sum(max(0.0, seg['end_ms'] - seg['start_ms']) for seg in acoustic_segments)
    low_energy_ids = {item['acoustic_segment_id'] for item in alignments if item['low_energy']}
    low_energy_ms = sum(item['acoustic_speech_ms'] for item in alignments if item['low_energy'])
    low_energy_unmatched_ms = sum(item['acoustic_speech_ms'] for item in alignments
                                  if item['low_energy'] and item['state'] == 'unmatched')
    energy_evidence = [{'acoustic_segment_id': item['acoustic_segment_id'],
                        'mean_rms': item['acoustic_mean_rms']}
                       for item in alignments]

    conflicts = [item['acoustic_segment_id'] for item in alignments if item['state'] == 'conflict']
    unmatched = [item['acoustic_segment_id'] for item in alignments if item['state'] == 'unmatched']
    status, reason = _status(speaker_segments, acoustic_segments, unmatched, conflicts)

    return {
        'schema_version': SCHEMA_VERSION,
        'document_id': 'ALIGN-' + uuid.uuid4().hex,
        'policy': policy,
        'processor': {'name': PROCESSOR_NAME, 'version': PROCESSOR_VERSION,
                      'deterministic': True, 'network_access': False},
        'sources': {
            'acoustic_document_id': (acoustic_doc or {}).get('document_id'),
            'speaker_document_id': (diarization_doc or {}).get('document_id'),
            'transcript_document_id': (transcript_doc or {}).get('transcript_id'),
            'audio_sha256': (acoustic_doc or {}).get('source', {}).get('sha256'),
            'acoustic_processor': (acoustic_doc or {}).get('processor'),
            'speaker_processor': (diarization_doc or {}).get('processor'),
        },
        'status': status,
        'reason': reason,
        'alignments': alignments,
        'diagnostics': {
            'acoustic_segment_count': len(acoustic_segments),
            'speaker_span_count': len(speaker_segments),
            'cluster_count': len(cluster_speech),
            'acoustic_speech_ms': round(covered, 3),
            'matched_acoustic_ms': round(matched_acoustic_ms, 3),
            'unmatched_acoustic_ms': round(unmatched_acoustic_ms, 3),
            'unmatched_acoustic_ratio': _ratio(unmatched_acoustic_ms, covered),
            'conflicted_acoustic_ms': round(conflicted_acoustic_ms, 3),
            'speaker_speech_ms': round(speaker_speech_ms, 3),
            'covered_speaker_ms': round(matched_speaker_ms, 3),
            'unmatched_speaker_ms': round(max(0.0, speaker_speech_ms - matched_speaker_ms), 3),
            'unmatched_speaker_ratio': _ratio(max(0.0, speaker_speech_ms - matched_speaker_ms), speaker_speech_ms),
            'unmatched_acoustic_segment_ids': unmatched,
            'conflicted_acoustic_segment_ids': conflicts,
            'per_cluster': [{
                'speaker_id': key,
                'native_speaker_id': native_labels.get(key),
                'speaker_speech_ms': round(value['speaker_speech_ms'], 3),
                'matched_ms': round(min(value['matched_ms'], value['speaker_speech_ms']), 3),
                'coverage_ratio': _ratio(min(value['matched_ms'], value['speaker_speech_ms']),
                                         value['speaker_speech_ms']),
                'speaker_span_count': value['segments'],
            } for key, value in sorted(cluster_speech.items())],
            'low_energy': {
                'percentile': policy['low_energy_percentile'],
                'threshold_mean_rms': (round(low_energy_threshold, 8)
                                       if measured_energies else None),
                'segment_count': len(low_energy_ids),
                'speech_ms': round(low_energy_ms, 3),
                'unmatched_ms': round(low_energy_unmatched_ms, 3),
                'unmatched_ratio': _ratio(low_energy_unmatched_ms, low_energy_ms),
                'energy_evidence': energy_evidence,
                'note': ('Low-energy coverage is a measurement input for quiet device '
                         'responses. It does not change the canonical metric policy. '
                         'A segment whose energy_evidence mean_rms is null produced no '
                         'energy measurement (for example a model-based boundary '
                         'provider), so it is excluded from the low-energy distribution '
                         'and is not evidence of a quiet device.'),
            },
            'boundary_drift_ms': {
                'matched_segment_count': len(drift_values),
                'tolerance_ms': policy['boundary_drift_tolerance_ms'],
                'median_ms': round(_percentile(sorted(drift_values), 50), 3) if drift_values else None,
                'max_ms': round(max(drift_values), 3) if drift_values else None,
                'beyond_tolerance_count': sum(1 for value in drift_values
                                              if value > policy['boundary_drift_tolerance_ms']),
            },
        },
    }


def _classify(matches, candidates):
    """Decide one acoustic segment's alignment state, deterministically."""
    if not matches:
        return 'unmatched', None
    if len(candidates) == 1:
        return 'single_cluster', candidates[0]['speaker_id']
    conflict = any(
        matches[i]['overlap_end_ms'] > matches[i + 1]['overlap_start_ms']
        and matches[i]['speaker_id'] != matches[i + 1]['speaker_id']
        for i in range(len(matches) - 1)
    )
    if conflict:
        return 'conflict', None
    return 'multi_cluster', None


def _status(speaker_segments, acoustic_segments, unmatched, conflicts):
    if not speaker_segments:
        return 'insufficient_evidence', (
            'No ASR speaker spans are available; acoustic segments keep no speaker '
            'evidence and alignment cannot be claimed')
    if not acoustic_segments:
        return 'insufficient_evidence', 'No acoustic segments are available to align'
    if unmatched or conflicts:
        notes = []
        if unmatched:
            notes.append(f'{len(unmatched)} acoustic segment(s) have no overlapping speaker span')
        if conflicts:
            notes.append(f'{len(conflicts)} acoustic segment(s) overlap conflicting clusters')
        return 'partial', '; '.join(notes)
    return 'complete', None


def _percentile(sorted_values, pct):
    """R7 linear interpolation percentile on a pre-sorted list."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]
    position = (n - 1) * pct / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def alignment_for(fused_doc, alignment_doc):
    """Index one alignment document by acoustic segment id."""
    return {item['acoustic_segment_id']: item
            for item in (alignment_doc or {}).get('alignments', [])}


# The four reasons a fused segment may not carry role-dependent evidence, plus the
# one reason it may. `fusion.detect_events` withholds events per segment using this
# vocabulary and `generate_timeline` names the same cause on its gap, so an interval
# that produced nothing can always be traced to the evidence that is missing.
ROLE_EVIDENCE_CATEGORIES = ('confirmed', 'cluster_conflict', 'no_speaker_span',
                            'human_declared_unknown', 'awaiting_decision')


def role_evidence_category(segment):
    """State why one fused segment may (or may not) support role-dependent analysis.

    Before Issue #115 a reader could only ask "is this segment unknown", which merged
    four different statements: a human decided it has no attributable role, nobody
    has been asked yet, no speaker span overlaps it, or conflicting clusters claimed
    the same instant. Those need different follow-up — and only the first is already
    answered — so the distinction is made here once and reused by every projection.
    """
    if segment.get('speaker_role') in ('tester', 'device'):
        return 'confirmed'
    if segment.get('speaker_evidence') == 'ambiguous_overlap':
        # Conflicting clusters: the segment abstained and owns no speaker id.
        return 'cluster_conflict'
    if segment.get('speaker_id') is None:
        return 'no_speaker_span'
    if (segment.get('role_attribution') or {}).get('method') == 'human_attribution':
        return 'human_declared_unknown'
    return 'awaiting_decision'


def explain_metric_gap(fused_doc, timeline_doc=None, metrics_doc=None, alignment_doc=None):
    """State why role-dependent metrics are unavailable, with counts.

    A blank metric list must be explainable from evidence instead of looking like
    a UI defect. Every reason names the stage that withheld the evidence and the
    number of segments it withheld.
    """
    segments = list((fused_doc or {}).get('segments') or [])
    metrics = list((metrics_doc or {}).get('metrics') or [])
    observed = [m for m in metrics if m.get('status') == 'observed' and m.get('value') is not None]
    if observed:
        return {'status': 'observed', 'observed_metric_count': len(observed),
                'reasons': [], 'segment_count': len(segments)}

    unclustered = [s for s in segments if s.get('speaker_id') is None]
    ambiguous = [s for s in segments if s.get('speaker_evidence') == 'ambiguous_overlap']
    # Issue #115: "the role is not tester/device" is four different statements, and
    # only some of them name a pending action. A reviewer who explicitly answered
    # `unknown` has finished, so counting those segments under `roles_not_confirmed`
    # told the operator the role gate was still open after they had closed it. One
    # classification pass over the withheld segments keeps every code consistent with
    # the others instead of re-deriving the cause with a different predicate.
    withheld = [role_evidence_category(s) for s in segments
                if s.get('speaker_role') not in ('tester', 'device')]
    causes = {category: sum(1 for cause in withheld if cause == category)
              for category in ROLE_EVIDENCE_CATEGORIES}
    # Everything a review has *not* already answered keeps the existing umbrella code,
    # so a consumer that only watches `roles_not_confirmed` still sees a non-zero count
    # whenever a decision is genuinely outstanding or the alignment is missing.
    not_confirmed = len(withheld) - causes['human_declared_unknown']
    diagnostics = (alignment_doc or {}).get('diagnostics') or {}

    reasons = []
    if not segments:
        reasons.append({'code': 'no_fused_segments',
                        'count': 0,
                        'detail': 'Acoustic/acoustic fusion produced no segments, so no turn or metric can exist.'})
    if unclustered:
        reasons.append({'code': 'segments_without_speaker_span',
                        'count': len(unclustered),
                        'detail': ('These acoustic segments have no overlapping ASR speaker span; '
                                   'see alignment diagnostics for unmatched duration and low-energy coverage.')})
    if ambiguous:
        reasons.append({'code': 'ambiguous_speaker_overlap',
                        'count': len(ambiguous),
                        'detail': 'Conflicting clusters claimed the same instant, so these segments abstained.'})
    if causes['human_declared_unknown']:
        reasons.append({'code': 'roles_human_declared_unknown',
                        'count': causes['human_declared_unknown'],
                        'detail': ('A saved human review decided that these clusters have no attributable '
                                   'role, so their intervals stay unmeasured by design. This is a finished '
                                   'decision, not an open review item: `complete_review` only states that '
                                   'every cluster was answered and never means a role-dependent metric '
                                   'passed.')})
    if causes['awaiting_decision']:
        reasons.append({'code': 'roles_awaiting_human_decision',
                        'count': causes['awaiting_decision'],
                        'detail': ('These segments belong to a known cluster that has no saved tester/device '
                                   'decision yet; role-dependent turns, timeline and metrics abstain until a '
                                   'human mapping is saved.')})
    if not_confirmed:
        reasons.append({'code': 'roles_not_confirmed',
                        'count': not_confirmed,
                        'detail': ('Role-dependent analysis cannot use these segments: the cause is the '
                                   'missing speaker span, the conflicting clusters or the decision that has '
                                   'not been saved above. Counts are per cause, so one segment can appear '
                                   'under more than one code; segments a review already decided to leave '
                                   'unknown are counted separately.')})
    if timeline_doc is not None and not (timeline_doc or {}).get('events'):
        reasons.append({'code': 'no_timeline_events',
                        'count': 0,
                        'detail': ('No canonical event was produced for any interval, so the metric engine '
                                   'had no observation window. Each cause above names which intervals were '
                                   'withheld.')})
    if not reasons:
        reasons.append({'code': 'metrics_not_eligible',
                        'count': len(metrics),
                        'detail': 'Every metric abstained as insufficient_evidence or not_applicable; see each metric reason.'})

    return {
        'status': 'insufficient_evidence',
        'observed_metric_count': 0,
        'segment_count': len(segments),
        'unmatched_acoustic_ms': diagnostics.get('unmatched_acoustic_ms'),
        'unmatched_speaker_ms': diagnostics.get('unmatched_speaker_ms'),
        'reasons': reasons,
    }
