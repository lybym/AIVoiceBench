"""Fusion ↔ diarization adaptation tests.

An acoustic segment that overlaps several speaker clusters must be split at the
cluster boundaries, or abstain when the clusters conflict. It must never be
blanket-assigned to whichever cluster happens to overlap the most.

Speaker clustering and role attribution stay separate: a known cluster is not a
known person.
"""

import unittest

from aivoicebench.fusion import fuse, apply_speakers
from aivoicebench.validation import schema_errors


def acoustic(segments, duration_ms=10000, sha='a' * 64):
    return {
        'schema_version': '1.0.0',
        'document_id': 'ACOUSTIC-1',
        'source': {'sha256': sha, 'duration_ms': duration_ms},
        'status': 'complete',
        'reason': None,
        'segments': segments,
    }


def acoustic_segment(segment_id, start, end):
    return {
        'segment_id': segment_id, 'start_ms': start, 'end_ms': end,
        'confidence': 0.9, 'uncertainty_ms': 30.0,
    }


def diarization(spans, native=None):
    """spans: list of (speaker_id, start_ms, end_ms, confidence).

    native maps a provider-native speaker label to the local cluster it resolved
    to, so tests can exercise utterance-level speaker evidence.
    """
    reverse = {local: str(label) for label, local in (native or {}).items()}
    return {
        'document_id': 'DIAR-1',
        'processor': {'provider': 'test', 'model': 'test', 'api_version': None, 'config': {}},
        'status': 'complete',
        'speaker_segments': [
            {'segment_id': f'SPK-{i:04d}', 'speaker_id': sid, 'start_ms': start, 'end_ms': end,
             'confidence': conf, 'native_speaker_id': reverse.get(sid)}
            for i, (sid, start, end, conf) in enumerate(spans)
        ],
    }


def attribution(mapping):
    return {
        'document_id': 'ATTR-1',
        'status': 'complete',
        'attributions': [
            {'speaker_id': sid, 'role': role, 'confidence': 1.0, 'method': 'explicit_evidence'}
            for sid, role in mapping.items()
        ],
    }


def transcript(doc, segments):
    doc['transcript_id'] = 'TSR-1'
    doc['segments'] = segments
    return doc


class SingleOverlapTests(unittest.TestCase):
    def test_single_overlap_assigns_cluster_with_evidence(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([('speaker_0', 900, 2100, 0.8)]))
        seg = fused['segments'][0]
        self.assertEqual(seg['speaker_id'], 'speaker_0')
        self.assertEqual(seg['speaker_evidence'], 'single_overlap')
        self.assertEqual(seg['segment_origin'], 'acoustic_segment')
        self.assertEqual(seg['speaker_candidates'], [{'speaker_id': 'speaker_0', 'overlap_ms': 1000.0}])
        # Clustering confidence is only what the provider actually reported.
        self.assertEqual(seg['speaker_cluster_confidence'], 0.8)

    def test_cluster_without_role_evidence_stays_unknown(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([('speaker_0', 900, 2100, 0.8)]))
        seg = fused['segments'][0]
        self.assertEqual(seg['speaker_role'], 'unknown')
        self.assertIsNone(seg['role_attribution_confidence'])

    def test_speaker_0_is_never_assumed_to_be_tester(self):
        """Explicit mapping says speaker_0 is the device; fusion must honour it."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([('speaker_0', 900, 2100, 0.8)]),
                       attribution({'speaker_0': 'device'}))
        self.assertEqual(fused['segments'][0]['speaker_role'], 'device')

    def test_no_clustering_evidence_leaves_speaker_null(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, None, None)
        seg = fused['segments'][0]
        self.assertIsNone(seg['speaker_id'])
        self.assertEqual(seg['speaker_evidence'], 'none')
        self.assertEqual(seg['speaker_candidates'], [])
        self.assertIn('No speaker clustering evidence', fused['attribution']['note'])

    def test_segment_without_overlap_is_left_unassigned(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 5000, 6000)]))
        apply_speakers(fused, diarization([('speaker_0', 0, 1000, 0.8)]))
        seg = fused['segments'][0]
        self.assertIsNone(seg['speaker_id'])
        self.assertEqual(seg['speaker_evidence'], 'none')
        self.assertIsNone(seg['speaker_cluster_confidence'])


class MultiSpeakerSplitTests(unittest.TestCase):
    def spans(self):
        return [('speaker_0', 1000, 1500, 0.8), ('speaker_1', 1500, 2000, 0.7)]

    def test_acoustic_segment_spanning_two_clusters_is_split(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization(self.spans()))
        segments = fused['segments']
        self.assertEqual(len(segments), 2, 'the acoustic segment must split at the cluster boundary')
        self.assertEqual([s['speaker_id'] for s in segments], ['speaker_0', 'speaker_1'])
        self.assertEqual([s['segment_origin'] for s in segments], ['speaker_split', 'speaker_split'])
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in segments], [(1000.0, 1500.0), (1500.0, 2000.0)])
        self.assertEqual({s['speaker_evidence'] for s in segments}, {'split_multiple_speakers'})
        self.assertEqual({s['acoustic_segment_id'] for s in segments}, {'ASEG-0001'})

    def test_split_boundary_sources_are_traced_per_edge(self):
        """A provider-estimated edge must not inherit the acoustic confidence."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization(self.spans()))
        first, second = fused['segments']
        # First piece: start is still the acoustic edge, end is the provider boundary.
        self.assertEqual(first['start_boundary_source'], 'acoustic_segment')
        self.assertEqual(first['end_boundary_source'], 'provider_speaker_estimate')
        self.assertIsNone(first['acoustic_boundary_confidence'])
        self.assertIsNone(first['acoustic_uncertainty_ms'])
        # Last piece: start is the provider boundary, end is still the acoustic edge.
        self.assertEqual(second['start_boundary_source'], 'provider_speaker_estimate')
        self.assertEqual(second['end_boundary_source'], 'acoustic_segment')
        self.assertIsNone(second['acoustic_boundary_confidence'])
        self.assertIsNone(second['acoustic_uncertainty_ms'])

    def test_unsplit_segment_keeps_acoustic_boundary_evidence(self):
        """When no split happens both edges are acoustic, so confidence survives."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([('speaker_0', 900, 2100, 0.8)]))
        seg = fused['segments'][0]
        self.assertEqual(seg['start_boundary_source'], 'acoustic_segment')
        self.assertEqual(seg['end_boundary_source'], 'acoustic_segment')
        self.assertEqual(seg['acoustic_boundary_confidence'], 0.9)
        self.assertEqual(seg['acoustic_uncertainty_ms'], 30.0)

    def test_provider_estimates_not_required_for_both_edges(self):
        """A cluster boundary may coincide with the acoustic start."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 1600, 0.8), ('speaker_1', 1600, 2000, 0.7)]))
        first = fused['segments'][0]
        self.assertEqual(first['start_boundary_source'], 'acoustic_segment')
        self.assertEqual(first['end_boundary_source'], 'provider_speaker_estimate')

    def test_split_is_not_a_blanket_assignment(self):
        """The whole segment must not be handed to the largest overlap."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2200)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 1200, 0.8), ('speaker_1', 1200, 2200, 0.7)]))
        segments = fused['segments']
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]['speaker_id'], 'speaker_0')
        self.assertEqual(segments[0]['end_ms'], 1200.0)
        # The dominant (1000 ms) cluster does not swallow the short cluster.
        self.assertEqual(segments[1]['speaker_id'], 'speaker_1')
        self.assertEqual(segments[1]['end_ms'], 2200.0)

    def test_roles_follow_each_sub_segment(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization(self.spans()),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        self.assertEqual([s['speaker_role'] for s in fused['segments']], ['tester', 'device'])
        self.assertEqual(fused['status'], 'complete')

    def test_utterance_spanning_speakers_is_not_attributed_by_length(self):
        """Without speaker evidence on the utterance, the text is withheld, not guessed.

        Both sub-segments are 500 ms here, so "give it to the longest piece" was a
        coin flip. The utterance provably spans two speakers, so it belongs to
        neither.
        """
        doc = transcript({'transcript_id': 'TSR-1'}, [
            {'segment_id': 'ASR-0001', 'text': '你好', 'start_ms': 1000, 'end_ms': 2000}])
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]), doc)
        apply_speakers(fused, diarization(self.spans()))
        self.assertEqual([s['text'] for s in fused['segments']], [None, None])
        self.assertEqual({s['text_attribution'] for s in fused['segments']},
                         {'ambiguous_spans_speakers'})
        # The utterance is preserved for review instead of being dropped or copied.
        withheld = fused['unattributed_texts']
        self.assertEqual(len(withheld), 1)
        self.assertEqual(withheld[0]['text'], '你好')
        self.assertEqual(withheld[0]['asr_segment_id'], 'ASR-0001')
        self.assertEqual((withheld[0]['start_ms'], withheld[0]['end_ms']), (1000.0, 2000.0))
        # Provenance of the utterance is still reachable from every sub-segment.
        self.assertEqual({s['asr_segment_id'] for s in fused['segments']}, {'ASR-0001'})

    def test_text_follows_the_utterance_speaker_label(self):
        """When the service labeled the utterance, its text follows that speaker."""
        doc = transcript({'transcript_id': 'TSR-1'}, [
            {'segment_id': 'ASR-0001', 'text': '你好', 'start_ms': 1000, 'end_ms': 2000,
             'speaker_id': '7'}])
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]), doc)
        # Native label '7' resolved to speaker_1, whose piece is the shorter one.
        apply_speakers(fused, diarization(self.spans(), native={'7': 'speaker_1'}))
        segments = fused['segments']
        by_speaker = {s['speaker_id']: s for s in segments}
        self.assertEqual(by_speaker['speaker_1']['text'], '你好')
        self.assertEqual(by_speaker['speaker_1']['text_attribution'], 'utterance_speaker')
        self.assertIsNone(by_speaker['speaker_0']['text'])
        self.assertEqual(fused['unattributed_texts'], [])

    def test_text_assigned_when_utterance_sits_inside_one_piece(self):
        doc = transcript({'transcript_id': 'TSR-1'}, [
            {'segment_id': 'ASR-0001', 'text': '好', 'start_ms': 1050, 'end_ms': 1200}])
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]), doc)
        apply_speakers(fused, diarization(self.spans()))
        first, second = fused['segments']
        self.assertEqual(first['text'], '好')
        self.assertEqual(first['text_attribution'], 'contained_in_sub_segment')
        self.assertIsNone(second['text'])
        self.assertEqual(fused['unattributed_texts'], [])

    def test_asr_speaker_evidence_is_kept_on_the_segment(self):
        doc = transcript({'transcript_id': 'TSR-1'}, [
            {'segment_id': 'ASR-0001', 'text': '你好', 'start_ms': 1000, 'end_ms': 2000,
             'speaker_id': '7'}])
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]), doc)
        seg = fused['segments'][0]
        self.assertEqual(seg['asr_speaker_id'], '7')
        self.assertEqual((seg['asr_start_ms'], seg['asr_end_ms']), (1000.0, 2000.0))
        self.assertEqual(seg['text_attribution'], 'none')

    def test_unsplit_segment_marks_text_attribution(self):
        doc = transcript({'transcript_id': 'TSR-1'}, [
            {'segment_id': 'ASR-0001', 'text': '你好', 'start_ms': 1000, 'end_ms': 2000}])
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]), doc)
        apply_speakers(fused, diarization([('speaker_0', 900, 2100, 0.8)]))
        self.assertEqual(fused['segments'][0]['text_attribution'], 'single_segment')

    def test_same_speaker_on_both_sides_is_not_split(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 1500, 0.8), ('speaker_0', 1500, 2000, 0.8)]))
        self.assertEqual(len(fused['segments']), 1)
        self.assertEqual(fused['segments'][0]['speaker_evidence'], 'single_overlap')

    def test_three_speakers_split_into_three(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2500)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 1500, 0.8), ('speaker_1', 1500, 2000, 0.7),
            ('speaker_2', 2000, 2500, 0.6)]))
        segments = fused['segments']
        self.assertEqual(len(segments), 3)
        self.assertEqual([s['speaker_id'] for s in segments],
                         ['speaker_0', 'speaker_1', 'speaker_2'])
        self.assertEqual({s['speaker_cluster_confidence'] for s in segments}, {0.6, 0.7, 0.8})

    def test_gap_between_clusters_inside_acoustic_segment(self):
        """A hole between clusters inside one acoustic segment must not be filled."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 3000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 1400, 0.8), ('speaker_1', 2000, 2400, 0.7)]))
        segments = fused['segments']
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in segments],
                         [(1000.0, 1400.0), (2000.0, 2400.0)])
        self.assertEqual(sum(s['end_ms'] - s['start_ms'] for s in segments), 800.0)


class ConflictingOverlapTests(unittest.TestCase):
    def test_conflicting_clusters_abstain(self):
        """Two different clusters claiming the same instant is a contradiction."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 2000, 0.8), ('speaker_1', 1200, 1800, 0.7)]))
        segments = fused['segments']
        self.assertEqual(len(segments), 1, 'a contradiction must not be split into invented boundaries')
        seg = segments[0]
        self.assertIsNone(seg['speaker_id'])
        self.assertEqual(seg['speaker_role'], 'unknown')
        self.assertEqual(seg['speaker_source'], 'ambiguous')
        self.assertEqual(seg['speaker_evidence'], 'ambiguous_overlap')
        self.assertIsNone(seg['speaker_cluster_confidence'])
        self.assertEqual([c['speaker_id'] for c in seg['speaker_candidates']],
                         ['speaker_0', 'speaker_1'])

    def test_abstention_marks_fusion_partial(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 2000, 0.8), ('speaker_1', 1200, 1800, 0.7)]))
        self.assertEqual(fused['status'], 'partial')
        self.assertIn('no speaker cluster', fused['reason'])

    def test_abstention_never_borrows_a_role(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 2000, 0.8), ('speaker_1', 1200, 1800, 0.7)]),
            attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        seg = fused['segments'][0]
        self.assertEqual(seg['speaker_role'], 'unknown')
        self.assertIsNone(seg['role_attribution_confidence'])


class TimelineValidityTests(unittest.TestCase):
    """Blockers found while validating a real imported timeline end to end."""

    def test_gap_events_cite_evidence_covering_the_gap(self):
        """A silence event must not cite a neighbouring speech segment's evidence."""
        from aivoicebench.fusion import build_turns, detect_events, generate_timeline
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 500, 1000),
                               acoustic_segment('ASEG-0002', 1500, 2000)]))
        apply_speakers(fused, diarization([('speaker_0', 500, 1000, 0.8),
                                          ('speaker_1', 1500, 2000, 0.7)]),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        silences = [e for e in events if e['type'] == 'silence']
        self.assertEqual(len(silences), 1)
        gap = silences[0]
        self.assertEqual((gap['start_ms'], gap['end_ms']), (1000.0, 1500.0))
        by_id = {item['evidence_id']: item for item in evidence}
        covering = [by_id[key] for key in gap['evidence_ids']]
        self.assertTrue(any(item['start_ms'] <= gap['start_ms'] and item['end_ms'] >= gap['end_ms']
                            for item in covering),
                        'gap evidence must cover the gap it is cited for')
        # A derived interval publishes no confidence number of its own.
        for item in covering:
            self.assertEqual(item['source'], 'derived')
            self.assertIsNone(item['confidence'])

    def test_timeline_and_events_carry_the_run_identity(self):
        from aivoicebench.fusion import build_turns, detect_events, generate_timeline
        from aivoicebench.validation import timeline_errors
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 500, 1000),
                               acoustic_segment('ASEG-0002', 1500, 2000)]))
        apply_speakers(fused, diarization([('speaker_0', 500, 1000, 0.8),
                                          ('speaker_1', 1500, 2000, 0.7)]),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(
            fused, turns, run_id='RUN-real', case_id='CASE-auto')
        timeline = generate_timeline(fused, turns, events, evidence, status, reason,
                                     run_id='RUN-real', case_id='CASE-auto',
                                     execution_kind='synthetic')
        self.assertEqual(timeline['run_id'], 'RUN-real')
        self.assertEqual(timeline['execution_kind'], 'synthetic')
        self.assertTrue(all(e['run_id'] == 'RUN-real' for e in events))
        self.assertEqual(timeline_errors(timeline), [])

    def test_timeout_is_the_observed_no_response_window(self):
        """PRD-F008 requires a complete observation window, so a timeout is an interval."""
        from aivoicebench.fusion import build_turns, detect_events, generate_timeline
        from aivoicebench.validation import timeline_errors
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 500, 1000),
                               acoustic_segment('ASEG-0002', 7000, 8000)]))
        apply_speakers(fused, diarization([('speaker_0', 500, 1000, 0.8),
                                          ('speaker_1', 7000, 8000, 0.7)]),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns, timeout_ms=5000)
        timeouts = [e for e in events if e['type'] == 'timeout']
        self.assertEqual(len(timeouts), 1)
        self.assertGreater(timeouts[0]['end_ms'], timeouts[0]['start_ms'])
        timeline = generate_timeline(fused, turns, events, evidence, status, reason)
        self.assertEqual(timeline_errors(timeline), [])


class FusionSchemaTests(unittest.TestCase):
    def test_split_document_is_schema_valid(self):
        doc = transcript({'transcript_id': 'TSR-1'}, [
            {'segment_id': 'ASR-0001', 'text': '你好', 'start_ms': 1000, 'end_ms': 2000}])
        fused = fuse(acoustic([
            acoustic_segment('ASEG-0001', 1000, 2000),
            acoustic_segment('ASEG-0002', 3000, 3500),
        ]), doc)
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 1500, 0.8), ('speaker_1', 1500, 2000, 0.7),
            ('speaker_1', 3000, 3500, 0.7)]),
            attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        errors = schema_errors(fused, 'fused-segments')
        self.assertEqual(errors, [], f'schema errors: {errors}')

    def test_abstained_document_is_schema_valid(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, diarization([
            ('speaker_0', 1000, 2000, 0.8), ('speaker_1', 1200, 1800, 0.7)]))
        errors = schema_errors(fused, 'fused-segments')
        self.assertEqual(errors, [], f'schema errors: {errors}')

    def test_unclustered_document_is_schema_valid(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0001', 1000, 2000)]))
        apply_speakers(fused, None, None)
        errors = schema_errors(fused, 'fused-segments')
        self.assertEqual(errors, [], f'schema errors: {errors}')


if __name__ == '__main__':
    unittest.main()
