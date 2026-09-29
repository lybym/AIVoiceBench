"""Issue #24 acceptance: deterministic Turns/Events from attributed speaker evidence.

The Issue requires that role-dependent Turns and Canonical Events are built only
from temporal + confirmed-role evidence, that every abstention state stays
explicit, that the association is reproducible from the same evidence, and that
the canonical timeline carries valid evidence references with audio-relative
timing.

These tests drive the real ``fuse`` → ``apply_speakers`` → ``build_turns`` →
``detect_events`` → ``generate_timeline`` chain on synthetic documents, so they
need no audio file, no provider and no network. They complement
``test_role_review.py`` (the manual gate) and ``test_alignment.py`` (the overlap
policy) by pinning the *consequences* of a decision at the turn/event layer.

Evidence boundary: anonymous clusters and explicit user roles are synthetic. A
passing test here proves the software contract, never recognition or role
accuracy.
"""

import unittest

from aivoicebench.fusion import (
    fuse, apply_speakers, build_turns, detect_events, generate_timeline,
)
from aivoicebench.validation import timeline_errors, turns_errors


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
    """spans: (speaker_id, start_ms, end_ms, confidence) tuples."""
    reverse = {local: str(label) for label, local in (native or {}).items()}
    return {
        'document_id': 'DIAR-1',
        'processor': {'provider': 'test', 'model': 'test', 'api_version': None, 'config': {}},
        'status': 'complete',
        'speaker_segments': [
            {'segment_id': f'SPK-{i:04d}', 'speaker_id': sid, 'start_ms': start, 'end_ms': end,
             'confidence': conf, 'native_speaker_id': reverse.get(sid)}
            for i, (sid, start, end, conf) in enumerate(spans)],
    }


def attribution(mapping):
    return {
        'document_id': 'ATTR-1',
        'status': 'complete',
        'attributions': [
            {'speaker_id': sid, 'role': role, 'confidence': 1.0,
             'method': 'explicit_evidence', 'confidence_basis': 'explicit_user_evidence'}
            for sid, role in mapping.items()],
    }


def build(spans, mapping, segments, transcript_doc=None):
    """Run the full chain and return (fused, turns, events, evidence, timeline)."""
    fused = fuse(acoustic(segments), transcript_doc)
    apply_speakers(fused, diarization(spans), attribution(mapping))
    turns = build_turns(fused)
    events, evidence, status, reason = detect_events(fused, turns)
    timeline = generate_timeline(fused, turns, events, evidence, status, reason)
    return fused, turns, events, evidence, timeline


def build_without_roles(segments, transcript_doc=None):
    """Run the chain with clustering but no role evidence at all."""
    fused = fuse(acoustic(segments), transcript_doc)
    turns = build_turns(fused)
    events, evidence, status, reason = detect_events(fused, turns)
    timeline = generate_timeline(fused, turns, events, evidence, status, reason)
    return fused, turns, events, evidence, timeline


class NonAlternatingOrderTests(unittest.TestCase):
    """Issue #24 AC4: the same role speaking twice must not create phantom turns.

    The turn builder must follow the recorded evidence, not an alternating
    tester/device assumption. Two tester segments in a row are one turn with two
    segments; two device segments in a row are one response with two segments.
    """

    def test_consecutive_tester_segments_form_one_turn(self):
        _, turns, events, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9), ('speaker_0', 1100, 1500, 0.9),
             ('speaker_1', 2000, 2500, 0.9)],
            {'speaker_0': 'tester', 'speaker_1': 'device'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 1100, 1500),
             acoustic_segment('ASEG-0002', 2000, 2500)])

        self.assertEqual(turns['status'], 'complete')
        self.assertEqual(len(turns['turns']), 1)
        turn = turns['turns'][0]
        self.assertEqual(len(turn['tester_segment_ids']), 2)
        self.assertEqual(len(turn['device_segment_ids']), 1)
        self.assertEqual(turn['tester_speech_start_ms'], 500)
        self.assertEqual(turn['tester_speech_end_ms'], 1500)
        self.assertEqual(turn['response_id'], 'RESP-0001')
        # One response, not two: an alternating assumption would invent a second.
        self.assertEqual(sum(1 for e in events if e['type'] == 'response_start'), 1)
        self.assertEqual(timeline_errors(timeline), [])

    def test_consecutive_device_segments_are_one_response(self):
        _, turns, events, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9),
             ('speaker_1', 1700, 2100, 0.9), ('speaker_0', 2500, 3000, 0.9)],
            {'speaker_0': 'tester', 'speaker_1': 'device'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 1200, 1600),
             acoustic_segment('ASEG-0002', 1700, 2100),
             acoustic_segment('ASEG-0003', 2500, 3000)])

        self.assertEqual(len(turns['turns']), 2)
        first = turns['turns'][0]
        self.assertEqual(len(first['device_segment_ids']), 2)
        self.assertEqual(first['response_id'], 'RESP-0001')
        self.assertEqual(first['device_speech_start_ms'], 1200)
        self.assertEqual(first['device_speech_end_ms'], 2100)
        self.assertEqual(sum(1 for e in events if e['type'] == 'response_start'), 1)
        self.assertEqual(sum(1 for e in events if e['type'] == 'response_end'), 1)
        self.assertEqual(timeline_errors(timeline), [])

    def test_device_speaking_first_is_an_orphan_response_not_a_tester_turn(self):
        """A device-first run must not be relabelled as if the tester spoke first."""
        _, turns, _, _, timeline = build(
            [('speaker_1', 500, 1000, 0.9), ('speaker_0', 2000, 2500, 0.9)],
            {'speaker_0': 'tester', 'speaker_1': 'device'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 2000, 2500)])

        first = turns['turns'][0]
        self.assertEqual(first['tester_segment_ids'], [])
        self.assertIsNone(first['tester_speech_start_ms'])
        self.assertEqual(len(first['device_segment_ids']), 1)
        # Turn ids stay contiguous in document order even though the first turn
        # begins on device speech.
        self.assertEqual([t['turn_id'] for t in turns['turns']], ['TURN-0001', 'TURN-0002'])
        self.assertEqual(timeline_errors(timeline), [])

    def test_turn_and_response_ids_are_contiguous_and_unique(self):
        _, turns, _, _, _ = build(
            [('speaker_1', 500, 1000, 0.9), ('speaker_0', 1200, 1600, 0.9),
             ('speaker_1', 1800, 2200, 0.9)],
            {'speaker_0': 'tester', 'speaker_1': 'device'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 1200, 1600),
             acoustic_segment('ASEG-0002', 1800, 2200)])

        turn_ids = [t['turn_id'] for t in turns['turns']]
        self.assertEqual(turn_ids, ['TURN-%04d' % i for i in range(1, len(turn_ids) + 1)])
        self.assertEqual(len(turn_ids), len(set(turn_ids)))
        response_ids = [t['response_id'] for t in turns['turns'] if t['response_id']]
        self.assertEqual(response_ids, ['RESP-%04d' % i for i in range(1, len(response_ids) + 1)])
        self.assertEqual(turns_errors(turns), [])


class UnknownRoleTests(unittest.TestCase):
    """Issue #24 AC3: a deliberate/absent role stays explicit at the turn layer."""

    def test_mixed_mapping_keeps_the_unknown_cluster_out_of_turns(self):
        """A resolved cluster produces turns; an `unknown` one produces nothing."""
        fused, turns, events, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9)],
            {'speaker_0': 'tester', 'speaker_1': 'unknown'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 1200, 1600)])

        unknown = [s for s in fused['segments'] if s['speaker_id'] == 'speaker_1'][0]
        self.assertEqual(unknown['speaker_role'], 'unknown')
        self.assertEqual([t['speaker_role'] for t in fused['segments']
                          if t['speaker_id'] == 'speaker_1'], ['unknown'])
        # The unknown segment is not silently absorbed into a turn or an event.
        turn_ids = [sid for t in turns['turns']
                    for sid in t['tester_segment_ids'] + t['device_segment_ids']]
        self.assertNotIn(unknown['segment_id'], turn_ids)
        # Its existence is not hidden: the documents stay explicitly incomplete.
        self.assertEqual(fused['status'], 'partial')
        self.assertEqual(turns['status'], 'partial')
        self.assertIn('no role evidence', turns['reason'])
        self.assertEqual(timeline['status'], 'partial')
        self.assertEqual(timeline_errors(timeline), [])

    def test_all_unknown_cannot_produce_turns_or_events(self):
        fused, turns, events, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9)],
            {'speaker_0': 'unknown', 'speaker_1': 'unknown'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 1200, 1600)])

        self.assertEqual(turns['turns'], [])
        self.assertEqual(turns['status'], 'insufficient_evidence')
        self.assertEqual(events, [])
        self.assertEqual(timeline['status'], 'partial')
        self.assertTrue(timeline['gaps'])
        self.assertEqual(timeline_errors(timeline), [])

    def test_cluster_without_role_evidence_never_becomes_a_speaker(self):
        """Clustering alone identifies a cluster, never a person."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0000', 500, 1000)]))
        apply_speakers(fused, diarization([('speaker_0', 500, 1000, 0.9)]))
        segment = fused['segments'][0]
        self.assertEqual(segment['speaker_id'], 'speaker_0')
        self.assertEqual(segment['speaker_role'], 'unknown')
        self.assertIsNone(segment['role_attribution_confidence'])


class MissingAndAmbiguousEvidenceTests(unittest.TestCase):
    """Issue #24 AC3: missing labels and conflicts stay explicit, never forced."""

    def test_segment_without_any_cluster_stays_unmatched(self):
        """A segment no cluster covers is left unmatched rather than given a neighbour."""
        fused = fuse(acoustic([acoustic_segment('ASEG-0000', 500, 1000),
                               acoustic_segment('ASEG-0001', 6000, 6500)]))
        apply_speakers(fused, diarization([('speaker_0', 500, 1000, 0.9)]),
                       attribution({'speaker_0': 'tester'}))

        unmatched = [s for s in fused['segments'] if s['acoustic_segment_id'] == 'ASEG-0001'][0]
        self.assertIsNone(unmatched['speaker_id'])
        self.assertEqual(unmatched['speaker_role'], 'unknown')
        self.assertEqual(unmatched['speaker_candidates'], [])
        # The covered segment still gets its role; the gap is not redistributed.
        covered = [s for s in fused['segments'] if s['acoustic_segment_id'] == 'ASEG-0000'][0]
        self.assertEqual(covered['speaker_role'], 'tester')
        self.assertEqual(fused['status'], 'partial')

    def test_timeline_locates_the_uncovered_interval_instead_of_abstaining_globally(self):
        """A partially attributed recording keeps its events and names what it withheld.

        Issue #24 fixed the opposite error — a partial event set presented as
        `complete` — and made event detection deliberately all-or-nothing: one segment
        without a confirmed role emptied the whole event set. Issue #115 replaced that
        guard with interval-local qualification, so the interval that does carry a
        confirmed role keeps its canonical events, the unmatched interval is published
        as its own auditable gap, and `complete` is still refused while any segment
        carries no confirmed role.
        """
        _, turns, events, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9)],
            {'speaker_0': 'tester'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 6000, 6500)])

        self.assertEqual(turns['status'], 'partial')
        self.assertTrue(turns['turns'])
        self.assertEqual({(event['type'], event['start_ms']) for event in events},
                         {('tester_speech_start', 500), ('tester_speech_end', 1000)})
        self.assertEqual(timeline['status'], 'partial')
        self.assertEqual([(gap['start_ms'], gap['end_ms']) for gap in timeline['gaps']],
                         [(6000.0, 6500.0)])
        self.assertEqual(timeline['gaps'][0]['required_evidence'], 'overlapping_speaker_span')
        self.assertEqual(timeline_errors(timeline), [])

    def test_partial_timeline_keeps_its_gap_when_events_exist(self):
        """A `partial` timeline always explains itself through `gaps`.

        `score_timeline`/`metric_errors` consumers treat `complete` as full coverage, so
        a timeline that cannot claim complete coverage must publish the gap that says
        why. This builds that exact state: events are present, yet the timeline is not
        complete.
        """
        fused = fuse(acoustic([acoustic_segment('ASEG-0000', 500, 1000)]))
        turns = {'turns': [{'turn_id': 'TURN-0001'}]}
        timeline = generate_timeline(fused, turns, [{'event_id': 'EVT-1'}], [], 'partial',
                                     'one segment had no confirmed role')
        self.assertEqual(timeline['status'], 'partial')
        self.assertEqual(len(timeline['gaps']), 1)
        self.assertEqual(timeline['gaps'][0]['reason'], 'one segment had no confirmed role')
        self.assertEqual(timeline['gaps'][0]['end_ms'], 10000)

    def test_missing_utterance_label_does_not_break_the_chain(self):
        """A transcript utterance with no speaker label is carried as evidence only.

        The text must not be attributed to a speaker by position or length, and the
        acoustic cluster still decides who spoke.
        """
        transcript_doc = {
            'transcript_id': 'TSR-1',
            'segments': [{'segment_id': 'ASR-0000', 'text': '你好', 'start_ms': 500,
                          'end_ms': 1000, 'speaker_id': None}],
        }
        fused, turns, _, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9)],
            {'speaker_0': 'tester'},
            [acoustic_segment('ASEG-0000', 500, 1000)],
            transcript_doc=transcript_doc)

        segment = fused['segments'][0]
        self.assertEqual(segment['speaker_role'], 'tester')
        self.assertIsNone(segment['asr_speaker_id'])
        # Text follows the single acoustic segment, which is a real containment fact.
        self.assertEqual(segment['text_attribution'], 'single_segment')
        self.assertEqual(turns['turns'][0]['tester_segment_ids'], [segment['segment_id']])
        self.assertEqual(timeline_errors(timeline), [])

    def test_conflicting_clusters_abstain_and_never_borrow_a_role(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0000', 1000, 2000)]))
        apply_speakers(fused,
                       diarization([('speaker_0', 1000, 2000, 0.9),
                                    ('speaker_1', 1200, 1800, 0.9)]),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        segment = fused['segments'][0]
        self.assertIsNone(segment['speaker_id'])
        self.assertEqual(segment['speaker_role'], 'unknown')
        self.assertEqual(segment['speaker_evidence'], 'ambiguous_overlap')
        self.assertEqual(segment['speaker_source'], 'ambiguous')
        self.assertIsNone(segment['role_attribution_confidence'])

    def test_ambiguous_overlap_candidates_are_recorded(self):
        fused = fuse(acoustic([acoustic_segment('ASEG-0000', 1000, 2000)]))
        apply_speakers(fused,
                       diarization([('speaker_0', 1000, 1500, 0.9),
                                    ('speaker_1', 1500, 2000, 0.9)]),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        # A clean two-cluster split is materialised, not abstained.
        self.assertEqual([s['speaker_role'] for s in fused['segments']], ['tester', 'device'])
        self.assertEqual({s['speaker_evidence'] for s in fused['segments']},
                         {'split_multiple_speakers'})


class ReproducibilityTests(unittest.TestCase):
    """Issue #24 AC5: the same evidence and roles always give the same association."""

    def setUp(self):
        self.spans = [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9),
                      ('speaker_0', 2000, 2500, 0.9)]
        self.mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
        self.segments = [acoustic_segment('ASEG-0000', 500, 1000),
                         acoustic_segment('ASEG-0001', 1200, 1600),
                         acoustic_segment('ASEG-0002', 2000, 2500)]

    def _association(self):
        _, turns, events, evidence, _ = build(self.spans, self.mapping, self.segments)
        # Document identity is a per-run uuid and is deliberately excluded; what must
        # reproduce is the association, the timing and the provenance.
        return {
            'turns': turns['turns'],
            'turns_status': turns['status'],
            'events': [{k: v for k, v in event.items()} for event in events],
            'evidence': evidence,
        }

    def test_association_is_identical_across_runs(self):
        first = self._association()
        second = self._association()
        self.assertEqual(first, second)

    def test_association_is_stable_when_input_order_is_the_same(self):
        """Repeated builds from one evidence set keep turn/response identity stable."""
        ids = set()
        for _ in range(3):
            _, turns, _, _, _ = build(self.spans, self.mapping, self.segments)
            ids.add(tuple((t['turn_id'], t['response_id'],
                           tuple(t['tester_segment_ids']),
                           tuple(t['device_segment_ids'])) for t in turns['turns']))
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids.pop(), (('TURN-0001', 'RESP-0001', ('FSEG-0000',), ('FSEG-0001',)),
                                     ('TURN-0002', None, ('FSEG-0002',), ())))

    def test_timeline_is_byte_stable_for_identical_identity(self):
        import json

        def persist():
            fused, turns, events, evidence, status, reason = None, None, None, None, None, None
            fused = fuse(acoustic(self.segments))
            apply_speakers(fused, diarization(self.spans), attribution(self.mapping))
            turns = build_turns(fused)
            events, evidence, status, reason = detect_events(
                fused, turns, run_id='RUN-fixed', case_id='CASE-fixed')
            timeline = generate_timeline(fused, turns, events, evidence, status, reason,
                                         run_id='RUN-fixed', case_id='CASE-fixed')
            return json.dumps(timeline, sort_keys=True, ensure_ascii=False)

        self.assertEqual(persist(), persist())


class CompleteRoleMappingTests(unittest.TestCase):
    """Issue #24 AC2: a complete human mapping yields real Turns and Canonical Events.

    This is the same chain ``import_pipeline._run_role_dependent_chain`` runs once a
    saved mapping unlocks role-dependent analysis. The gate itself and the revision
    store are covered by ``test_role_review.py``; here the assertion is on the
    *content* that the unlocked chain must produce.
    """

    def test_complete_mapping_produces_tester_and_device_turns_and_events(self):
        fused, turns, events, _, timeline = build(
            [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9)],
            {'speaker_0': 'tester', 'speaker_1': 'device'},
            [acoustic_segment('ASEG-0000', 500, 1000),
             acoustic_segment('ASEG-0001', 1200, 1600)])

        self.assertEqual(fused['status'], 'complete')
        self.assertEqual([s['speaker_role'] for s in fused['segments']], ['tester', 'device'])
        self.assertEqual(turns['status'], 'complete')
        self.assertIsNone(turns['reason'])
        turn = turns['turns'][0]
        self.assertEqual(turn['turn_id'], 'TURN-0001')
        self.assertEqual(turn['response_id'], 'RESP-0001')
        self.assertEqual((turn['tester_speech_start_ms'], turn['tester_speech_end_ms']),
                         (500, 1000))
        self.assertEqual((turn['device_speech_start_ms'], turn['device_speech_end_ms']),
                         (1200, 1600))

        types = [event['type'] for event in events]
        self.assertIn('tester_speech_start', types)
        self.assertIn('response_start', types)
        self.assertIn('response_end', types)
        self.assertTrue(all(event['turn_id'] == 'TURN-0001' for event in events
                            if event['turn_id'] is not None))
        self.assertEqual(timeline['status'], 'complete')
        self.assertEqual(timeline['gaps'], [])
        self.assertEqual(timeline_errors(timeline), [])

    def test_confirmed_roles_are_not_inferred_from_cluster_order(self):
        """Swapping which cluster is the tester swaps the roles, not the labels."""
        spans = [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9)]
        segments = [acoustic_segment('ASEG-0000', 500, 1000),
                    acoustic_segment('ASEG-0001', 1200, 1600)]

        _, forward, _, _, _ = build(spans, {'speaker_0': 'tester', 'speaker_1': 'device'},
                                    segments)
        _, reverse, _, _, _ = build(spans, {'speaker_0': 'device', 'speaker_1': 'tester'},
                                    segments)

        self.assertEqual(forward['turns'][0]['tester_segment_ids'], ['FSEG-0000'])
        self.assertEqual(forward['turns'][0]['device_segment_ids'], ['FSEG-0001'])

        # The same acoustic evidence now yields the opposite association: the device
        # speaks first and the tester answers, because the human mapping -- not the
        # cluster index and not the speaking order -- decides who is who.
        first, second = reverse['turns']
        self.assertEqual(first['tester_segment_ids'], [])
        self.assertEqual(first['device_segment_ids'], ['FSEG-0000'])
        self.assertEqual(second['tester_segment_ids'], ['FSEG-0001'])
        self.assertEqual(second['device_segment_ids'], [])


class CanonicalTimelineTests(unittest.TestCase):
    """Issue #24 AC6: canonical events cite valid evidence with audio-relative timing."""

    spans = [('speaker_0', 500, 1000, 0.9), ('speaker_1', 1200, 1600, 0.9)]
    mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
    segments = [acoustic_segment('ASEG-0000', 500, 1000),
                acoustic_segment('ASEG-0001', 1200, 1600)]

    def test_every_event_resolves_to_evidence_covering_its_interval(self):
        _, _, events, evidence, timeline = build(self.spans, self.mapping, self.segments)
        self.assertEqual(timeline_errors(timeline), [])
        by_id = {item['evidence_id']: item for item in evidence}
        self.assertTrue(events)
        for event in events:
            self.assertTrue(event['evidence_ids'])
            for key in event['evidence_ids']:
                self.assertIn(key, by_id)
                item = by_id[key]
                self.assertLessEqual(item['start_ms'], event['start_ms'])
                self.assertGreaterEqual(item['end_ms'], event['end_ms'])

    def test_timeline_time_base_is_audio_relative(self):
        _, _, _, evidence, timeline = build(self.spans, self.mapping, self.segments)
        self.assertEqual(timeline['time_base']['kind'], 'audio_relative_ms')
        for item in evidence:
            self.assertEqual(item['time_base'], 'audio_relative_ms')

    def test_acoustic_timing_evidence_never_borrows_a_role_confidence(self):
        """An acoustic-timing snippet must report acoustic quality, not role quality.

        A human role mapping is confident about *who*, never about *when*. Publishing
        its confidence on an acoustic boundary would present role evidence as
        measurement accuracy.
        """
        _, _, _, evidence, _ = build(self.spans, self.mapping, self.segments)
        for segment in evidence:
            self.assertNotEqual(segment['confidence_source'], 'role_attribution')
            self.assertNotEqual(segment['confidence_source'], 'speaker_cluster')
            if segment['source'] == 'audio_signal':
                self.assertEqual(segment['confidence_source'], 'acoustic_boundary')
                self.assertEqual(segment['confidence'], 0.9)

    def test_split_boundary_reports_no_acoustic_confidence(self):
        """A provider-estimated split edge must not inherit acoustic confidence."""
        spans = [('speaker_0', 1000, 1500, 0.9), ('speaker_1', 1500, 2000, 0.9)]
        fused = fuse(acoustic([acoustic_segment('ASEG-0000', 1000, 2000)]))
        apply_speakers(fused, diarization(spans),
                       attribution({'speaker_0': 'tester', 'speaker_1': 'device'}))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        self.assertEqual([s['start_boundary_source'] for s in fused['segments']],
                         ['acoustic_segment', 'provider_speaker_estimate'])
        # Neither sub-segment may claim the acoustic segmenter's boundary confidence.
        for item in evidence:
            if item['source'] == 'audio_signal':
                self.assertIsNone(item['confidence'])
                self.assertEqual(item['confidence_source'], 'none')
        timeline = generate_timeline(fused, turns, events, evidence, status, reason)
        self.assertEqual(timeline_errors(timeline), [])

    def test_events_and_timeline_share_the_run_identity(self):
        fused = fuse(acoustic(self.segments))
        apply_speakers(fused, diarization(self.spans), attribution(self.mapping))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(
            fused, turns, run_id='RUN-24', case_id='CASE-24')
        timeline = generate_timeline(fused, turns, events, evidence, status, reason,
                                     run_id='RUN-24', case_id='CASE-24')
        self.assertEqual(timeline['run_id'], 'RUN-24')
        self.assertTrue(events)
        for event in events:
            self.assertEqual(event['run_id'], 'RUN-24')
            self.assertEqual(event['case_id'], 'CASE-24')
        self.assertEqual(timeline_errors(timeline), [])


if __name__ == '__main__':
    unittest.main()
