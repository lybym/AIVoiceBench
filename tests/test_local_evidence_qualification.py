"""Issue #115: role evidence is qualified per interval, never for the whole recording.

These tests hold the two boundaries the Issue separates:

* an interval whose cluster carries a confirmed tester/device role must produce its
  canonical events and eligible metrics even when other intervals are unresolved;
* an interval that is human-decided `unknown`, undecided, conflicting or covered by no
  speaker span must produce no event, keep its acoustic evidence, and be published as
  its own auditable gap.

Everything here is in-memory: no provider, no file, no private recording. The real
authorized recording stays in `tests/test_real_run_downstream.py`.
"""

import copy
import json
import unittest

from aivoicebench.alignment import explain_metric_gap, role_evidence_category
from aivoicebench.diarization import attribute_speakers
from aivoicebench.fusion import (apply_speakers, build_turns, detect_events, fuse,
                                 generate_timeline)
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.validation import schema_errors, timeline_errors

RUN_ID = 'RUN-115'
CASE_ID = 'CASE-115'
DURATION_MS = 60000.0


def acoustic_doc(rows, duration=DURATION_MS):
    return {
        'schema_version': '1.0.0',
        'document_id': 'ACOUSTIC-115',
        'source': {'sha256': 'b' * 64, 'duration_ms': duration},
        'status': 'complete',
        'reason': None,
        'segments': [{'segment_id': f'ASEG-{index:04d}', 'start_ms': start, 'end_ms': end,
                      'confidence': 0.9, 'uncertainty_ms': 30.0}
                     for index, (start, end) in enumerate(rows)],
    }


def diarization_doc(spans):
    """spans: (speaker_id, start_ms, end_ms, confidence)."""
    return {
        'document_id': 'DIAR-115',
        'processor': {'provider': 'fixture', 'model': 'fixture', 'api_version': None,
                      'config': {}},
        'status': 'complete',
        'speaker_segments': [{'segment_id': f'SPK-{index:04d}', 'speaker_id': speaker_id,
                              'start_ms': start, 'end_ms': end, 'confidence': confidence,
                              'native_speaker_id': None}
                             for index, (speaker_id, start, end, confidence) in enumerate(spans)],
    }


def build(rows, spans, mapping, *, human_role_review=True, duration=DURATION_MS):
    """Run the real producer chain and return every document it published."""
    fused = fuse(acoustic_doc(rows, duration))
    diar = diarization_doc(spans)
    attribution_doc = attribute_speakers(diar, explicit_mapping=mapping,
                                        human_role_review=human_role_review)
    apply_speakers(fused, diar, attribution_doc)
    turns = build_turns(fused)
    events, evidence, status, reason = detect_events(fused, turns, run_id=RUN_ID,
                                                     case_id=CASE_ID)
    timeline = generate_timeline(fused, turns, events, evidence, status, reason,
                                 run_id=RUN_ID, case_id=CASE_ID)
    return {'fused': fused, 'attribution': attribution_doc, 'turns': turns,
            'events': events, 'evidence': evidence, 'status': status, 'reason': reason,
            'timeline': timeline}


# The shape of the private Issue #115 Run: two tester clusters, two device clusters,
# one cluster the reviewer deliberately kept `unknown`, and acoustic segments that no
# speaker span covers.
MIXED_ROWS = [(1000, 2000), (5000, 6000), (9000, 10000)]
MIXED_SPANS = [('speaker_0', 1000, 2000, 0.9), ('speaker_2', 5000, 6000, 0.9),
               ('speaker_1', 9000, 10000, 0.9)]
MIXED_MAPPING = {'speaker_0': 'tester', 'speaker_1': 'device', 'speaker_2': 'unknown'}


def gap_codes(gap):
    return {reason['code']: reason['count'] for reason in gap['reasons']}


class ConfirmedAndUnknownCoexistTests(unittest.TestCase):
    """AC1: a confirmed interval is not erased by an unresolved neighbour."""

    def setUp(self):
        self.case = build(MIXED_ROWS, MIXED_SPANS, MIXED_MAPPING)

    def test_confirmed_intervals_still_produce_their_events(self):
        events = self.case['events']
        self.assertTrue(events, 'a recording with confirmed tester/device roles must '
                                'not publish an empty EventTimeline')
        types = {(event['type'], event['start_ms']) for event in events}
        self.assertIn(('tester_speech_start', 1000), types)
        self.assertIn(('device_speech_start', 9000), types)
        self.assertEqual(self.case['status'], 'partial')
        self.assertEqual(timeline_errors(self.case['timeline']), [])

    def test_the_unknown_interval_produces_no_event(self):
        for event in self.case['events']:
            self.assertFalse(5000 <= event['start_ms'] <= 6000,
                             f'no event may be claimed inside an unattributed interval: {event}')

    def test_the_withheld_interval_is_published_as_its_own_gap(self):
        gaps = self.case['timeline']['gaps']
        self.assertEqual(len(gaps), 1, 'one auditable gap per withheld interval')
        self.assertEqual((gaps[0]['start_ms'], gaps[0]['end_ms']), (5000.0, 6000.0))
        self.assertEqual(gaps[0]['required_evidence'], 'new_human_role_decision')
        self.assertIn('no confirmed speaker role', gaps[0]['reason'])
        # The cause is named, not merely "an unknown exists somewhere".
        self.assertIn('saved review', gaps[0]['reason'])

    def test_same_cause_gaps_do_not_bridge_a_confirmed_interval(self):
        """Each withheld interval stays local when confirmed speech lies between it."""
        case = build(
            [(1000, 2000), (3000, 4000), (5000, 6000)],
            [('speaker_0', 1000, 2000, 0.9), ('speaker_1', 3000, 4000, 0.9),
             ('speaker_0', 5000, 6000, 0.9)],
            {'speaker_0': 'unknown', 'speaker_1': 'tester'})

        self.assertTrue(any(event['type'] == 'tester_speech_start'
                            and event['start_ms'] == 3000 for event in case['events']))
        gaps = case['timeline']['gaps']
        self.assertEqual([(gap['start_ms'], gap['end_ms']) for gap in gaps],
                         [(1000.0, 2000.0), (5000.0, 6000.0)])
        self.assertTrue(all(gap['required_evidence'] == 'new_human_role_decision'
                            for gap in gaps))
        self.assertEqual(timeline_errors(case['timeline']), [])

    def test_eligible_metrics_are_observed_for_the_attributed_window(self):
        metrics = compute_timeline_metrics(self.case['timeline'])
        self.assertGreater(metrics['counts']['observed'], 0,
                           'the attributed interval must yield observed metrics')
        self.assertNotEqual(metrics['status'], 'insufficient_evidence')

    def test_acoustic_evidence_of_the_unknown_interval_is_preserved(self):
        segments = self.case['fused']['segments']
        unknown = [s for s in segments if 5000 <= s['start_ms'] <= 6000]
        self.assertTrue(unknown)
        for segment in unknown:
            self.assertEqual(segment['speaker_role'], 'unknown')
            self.assertEqual(role_evidence_category(segment), 'human_declared_unknown')
        covered = {(item['start_ms'], item['end_ms']) for item in self.case['evidence']}
        self.assertIn((5000.0, 6000.0), covered,
                      'withholding a role event must not delete the acoustic evidence')

    def test_documents_stay_schema_valid_without_a_persistence_change(self):
        self.assertEqual(schema_errors(self.case['fused'], 'fused-segments'), [])
        self.assertEqual(schema_errors(self.case['attribution'], 'source-attribution'), [])
        self.assertEqual(schema_errors(self.case['timeline'], 'event-timeline'), [])

    def test_turns_still_skip_the_unattributed_segment(self):
        turn_segment_ids = {sid for turn in self.case['turns']['turns']
                            for sid in turn['tester_segment_ids'] + turn['device_segment_ids']}
        unknown_ids = {s['segment_id'] for s in self.case['fused']['segments']
                       if 5000 <= s['start_ms'] <= 6000}
        self.assertFalse(turn_segment_ids & unknown_ids)


class NothingAttributedTests(unittest.TestCase):
    """The whole-recording abstention survives, but only when nothing is attributable."""

    def test_no_saved_decision_at_all_still_abstains_everywhere(self):
        case = build(MIXED_ROWS, MIXED_SPANS, None)
        self.assertEqual(case['events'], [])
        self.assertEqual(case['status'], 'insufficient_evidence')
        self.assertIn('no fused segment carries a confirmed', case['reason'])
        # Naming the causes is required even here: "an unknown exists" is not a report.
        self.assertIn('no saved role decision', case['reason'])
        gaps = case['timeline']['gaps']
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]['start_ms'], gaps[0]['end_ms']), (0, DURATION_MS))
        self.assertEqual(gaps[0]['required_evidence'], 'speaker_diarization_or_human_review')
        self.assertEqual(compute_timeline_metrics(case['timeline'])['status'],
                         'insufficient_evidence')

    def test_every_cluster_declared_unknown_abstains_and_is_not_called_a_pending_review(self):
        case = build([(1000, 2000), (3000, 4000)],
                     [('speaker_0', 1000, 2000, 0.9), ('speaker_1', 3000, 4000, 0.9)],
                     {'speaker_0': 'unknown', 'speaker_1': 'unknown'})
        self.assertEqual(case['events'], [])
        self.assertEqual(case['status'], 'insufficient_evidence')
        for segment in case['fused']['segments']:
            self.assertEqual(segment['speaker_role'], 'unknown')
            self.assertIsNone(segment['role_attribution_confidence'])
            self.assertEqual(role_evidence_category(segment), 'human_declared_unknown')
        codes = gap_codes(explain_metric_gap(case['fused'], case['timeline'],
                                             {'metrics': []}))
        self.assertIn('roles_human_declared_unknown', codes)
        self.assertNotIn('roles_not_confirmed', codes,
                         'a finished decision must not be reported as an open gate')
        self.assertNotIn('roles_awaiting_human_decision', codes)


class SilenceAndTimeoutRemainEvidenceTests(unittest.TestCase):
    """A no-speech window is only claimed between intervals the recording attributes."""

    def test_no_silence_or_timeout_is_claimed_across_an_unattributed_interval(self):
        case = build(MIXED_ROWS, MIXED_SPANS, MIXED_MAPPING)
        self.assertEqual([e for e in case['events']
                          if e['type'] in ('silence', 'timeout')], [],
                         'an unattributed neighbour means the surrounding window was never established')

    def test_a_confirmed_tester_to_device_window_still_produces_its_timeout(self):
        case = build([(1000, 2000), (9000, 10000)],
                     [('speaker_0', 1000, 2000, 0.9), ('speaker_1', 9000, 10000, 0.9)],
                     {'speaker_0': 'tester', 'speaker_1': 'device'})
        timeouts = [e for e in case['events'] if e['type'] == 'timeout']
        self.assertEqual(len(timeouts), 1)
        self.assertEqual((timeouts[0]['start_ms'], timeouts[0]['end_ms']), (2000.0, 9000.0))
        self.assertEqual(case['status'], 'complete')
        self.assertEqual(case['timeline']['gaps'], [])
        self.assertEqual(timeline_errors(case['timeline']), [])


class MissingSpeakerSpanTests(unittest.TestCase):
    """AC1/AC3: unmatched acoustic segments keep their own cause and gap."""

    def setUp(self):
        self.case = build([(1000, 2000), (3000, 3500)],
                          [('speaker_0', 1000, 2000, 0.9)], {'speaker_0': 'tester'})

    def test_the_covered_interval_still_produces_events(self):
        types = {event['type'] for event in self.case['events']}
        self.assertIn('tester_speech_start', types)
        self.assertEqual(self.case['status'], 'partial')

    def test_the_unmatched_interval_is_a_gap_of_its_own(self):
        gaps = self.case['timeline']['gaps']
        self.assertEqual(len(gaps), 1)
        self.assertEqual((gaps[0]['start_ms'], gaps[0]['end_ms']), (3000.0, 3500.0))
        self.assertEqual(gaps[0]['required_evidence'], 'overlapping_speaker_span')
        self.assertEqual(timeline_errors(self.case['timeline']), [])

    def test_metrics_gap_names_the_missing_span_not_a_pending_review(self):
        codes = gap_codes(explain_metric_gap(self.case['fused'], self.case['timeline'],
                                             {'metrics': []}))
        self.assertEqual(codes['segments_without_speaker_span'], 1)
        self.assertNotIn('roles_awaiting_human_decision', codes)
        self.assertNotIn('roles_human_declared_unknown', codes)


class ConflictTests(unittest.TestCase):
    """Conflicting clusters abstain for their own interval and never borrow a role."""

    def setUp(self):
        self.case = build([(1000, 2000), (5000, 6000)],
                          [('speaker_0', 1000, 2000, 0.9), ('speaker_1', 1200, 1800, 0.9),
                           ('speaker_2', 5000, 6000, 0.9)],
                          {'speaker_0': 'tester', 'speaker_1': 'device',
                           'speaker_2': 'tester'})

    def test_conflicting_segment_keeps_no_speaker_and_no_role(self):
        conflicted = [s for s in self.case['fused']['segments'] if s['start_ms'] == 1000][0]
        self.assertIsNone(conflicted['speaker_id'])
        self.assertEqual(conflicted['speaker_role'], 'unknown')
        self.assertEqual(role_evidence_category(conflicted), 'cluster_conflict')

    def test_the_conflict_does_not_erase_the_attributed_interval(self):
        events = self.case['events']
        self.assertTrue(events)
        self.assertEqual({(e['type'], e['start_ms']) for e in events},
                         {('tester_speech_start', 5000), ('tester_speech_end', 6000)})

    def test_the_conflict_is_reported_with_its_own_cause(self):
        gaps = self.case['timeline']['gaps']
        self.assertEqual((gaps[0]['start_ms'], gaps[0]['end_ms']), (1000.0, 2000.0))
        self.assertEqual(gaps[0]['required_evidence'], 'unambiguous_speaker_alignment')
        codes = gap_codes(explain_metric_gap(self.case['fused'], self.case['timeline'],
                                             {'metrics': []}))
        self.assertEqual(codes['ambiguous_speaker_overlap'], 1)


class MetricGapCauseTests(unittest.TestCase):
    """AC3: four different statements must not collapse into one code."""

    def test_declared_unknown_is_separated_from_an_open_review_item(self):
        case = build(MIXED_ROWS, MIXED_SPANS, MIXED_MAPPING)
        gap = explain_metric_gap(case['fused'], case['timeline'], {'metrics': []})
        codes = gap_codes(gap)
        self.assertEqual(codes['roles_human_declared_unknown'], 1)
        self.assertNotIn('roles_awaiting_human_decision', codes)
        self.assertNotIn('roles_not_confirmed', codes,
                         'a completed review must not be reported as "nobody decided"')
        detail = next(reason['detail'] for reason in gap['reasons']
                      if reason['code'] == 'roles_human_declared_unknown')
        self.assertIn('complete_review', detail)

    def test_an_undecided_cluster_is_still_reported_as_awaiting_a_decision(self):
        case = build(MIXED_ROWS, MIXED_SPANS, {'speaker_0': 'tester', 'speaker_1': 'device'},
                     human_role_review=False)
        codes = gap_codes(explain_metric_gap(case['fused'], case['timeline'],
                                             {'metrics': []}))
        self.assertEqual(codes['roles_awaiting_human_decision'], 1)
        self.assertEqual(codes['roles_not_confirmed'], 1)
        self.assertNotIn('roles_human_declared_unknown', codes)

    def test_the_gap_never_names_a_role_it_has_not_been_given(self):
        """An abstention must not read as though a role had been decided.

        `metrics_gap` states what is missing, so it may only describe roles as pending
        or human-owned. Naming a concrete role here would leak an assignment the
        evidence does not support, which is the same boundary `tests.test_alignment`
        pins for the blank-metrics explanation. The two new codes this Issue adds are
        the ones that can most easily drift, so both the answered and the pending
        review states are checked.
        """
        cases = (('answered review', MIXED_MAPPING, True, 'roles_human_declared_unknown'),
                 ('pending review', {'speaker_0': 'tester', 'speaker_1': 'device'}, False,
                  'roles_awaiting_human_decision'))
        for label, mapping, human_role_review, expected in cases:
            with self.subTest(label):
                case = build(MIXED_ROWS, MIXED_SPANS, mapping,
                             human_role_review=human_role_review)
                gap = explain_metric_gap(case['fused'], case['timeline'], {'metrics': []})
                self.assertIn(expected, gap_codes(gap))
                serialized = json.dumps(gap, ensure_ascii=False).lower()
                self.assertNotIn('tester', serialized)
                self.assertNotIn('device', serialized)
                self.assertNotIn('speaker_role', serialized)

    def test_the_umbrella_code_still_counts_an_unmatched_segment(self):
        case = build([(1000, 2000), (3000, 3500)],
                     [('speaker_0', 1000, 2000, 0.9)], {'speaker_0': 'tester'},
                     human_role_review=False)
        codes = gap_codes(explain_metric_gap(case['fused'], case['timeline'],
                                             {'metrics': []}))
        self.assertEqual(codes['roles_not_confirmed'], 1)
        self.assertEqual(codes['segments_without_speaker_span'], 1)


class ManualRevisionProvenanceTests(unittest.TestCase):
    """AC4: a saved revision's `unknown` is an answered decision, kept as provenance."""

    def test_declared_unknown_publishes_human_provenance_without_a_role(self):
        segment = build(MIXED_ROWS, MIXED_SPANS, MIXED_MAPPING)['fused']['segments'][1]
        self.assertEqual(segment['speaker_role'], 'unknown')
        self.assertIsNone(segment['role_attribution_confidence'])
        self.assertEqual(segment['role_attribution']['method'], 'human_attribution')
        self.assertEqual(segment['role_attribution']['basis'], 'human_review')
        self.assertFalse(segment['role_attribution']['needs_review'])

    def test_an_undecided_cluster_publishes_no_provenance(self):
        segments = build(MIXED_ROWS, MIXED_SPANS, {'speaker_0': 'tester', 'speaker_1': 'device'},
                         human_role_review=False)['fused']['segments']
        undecided = [s for s in segments if s['speaker_id'] == 'speaker_2'][0]
        self.assertEqual(undecided['speaker_role'], 'unknown')
        self.assertIsNone(undecided['role_attribution'])

    def test_attribution_document_records_the_two_states_differently(self):
        diar = diarization_doc(MIXED_SPANS)
        decided = attribute_speakers(diar, explicit_mapping=MIXED_MAPPING,
                                     human_role_review=True)
        pending = attribute_speakers(diar, explicit_mapping={'speaker_0': 'tester',
                                                             'speaker_1': 'device'},
                                     human_role_review=True)
        by_speaker = {a['speaker_id']: a for a in decided['attributions']}
        self.assertEqual(by_speaker['speaker_2']['method'], 'human_attribution')
        self.assertFalse(by_speaker['speaker_2']['needs_review'])
        self.assertEqual(schema_errors(decided, 'source-attribution'), [])
        self.assertEqual(schema_errors(pending, 'source-attribution'), [])
        pending_by_speaker = {a['speaker_id']: a for a in pending['attributions']}
        self.assertEqual(pending_by_speaker['speaker_2']['method'], 'none')
        self.assertTrue(pending_by_speaker['speaker_2']['needs_review'])

    def test_the_issue_shape_of_five_clusters_two_roles_one_unknown_produces_events(self):
        """The reported case: 5 clusters, 2 tester, 2 device, 1 deliberate unknown."""
        rows = [(1000, 2000), (3000, 4000), (5000, 6000), (7000, 8000), (9000, 10000)]
        spans = [('speaker_0', 1000, 2000, 0.9), ('speaker_1', 3000, 4000, 0.9),
                 ('speaker_2', 5000, 6000, 0.9), ('speaker_3', 7000, 8000, 0.9),
                 ('speaker_4', 9000, 10000, 0.9)]
        mapping = {'speaker_0': 'tester', 'speaker_1': 'tester', 'speaker_2': 'unknown',
                   'speaker_3': 'device', 'speaker_4': 'device'}
        case = build(rows, spans, mapping)
        self.assertTrue(case['events'])
        self.assertEqual(case['status'], 'partial')
        self.assertEqual([(g['start_ms'], g['end_ms']) for g in case['timeline']['gaps']],
                         [(5000.0, 6000.0)])
        self.assertGreater(compute_timeline_metrics(case['timeline'])['counts']['observed'], 0)


class RecomputationSafetyTests(unittest.TestCase):
    """AC4: qualifying evidence must not rewrite or re-derive the machine evidence."""

    def test_repeated_detection_is_deterministic_and_does_not_mutate_the_source(self):
        case = build(MIXED_ROWS, MIXED_SPANS, MIXED_MAPPING)
        before = copy.deepcopy(case['fused'])
        second_events, second_evidence, second_status, second_reason = detect_events(
            case['fused'], case['turns'], run_id=RUN_ID, case_id=CASE_ID)
        second_timeline = generate_timeline(case['fused'], case['turns'], second_events,
                                           second_evidence, second_status, second_reason,
                                           run_id=RUN_ID, case_id=CASE_ID)
        self.assertEqual(json.dumps(second_timeline['events'], sort_keys=True),
                         json.dumps(case['timeline']['events'], sort_keys=True))
        self.assertEqual(json.dumps(second_timeline['gaps'], sort_keys=True),
                         json.dumps(case['timeline']['gaps'], sort_keys=True))
        self.assertEqual(case['fused'], before,
                         'event detection must not rewrite the fused evidence it reads')


if __name__ == '__main__':
    unittest.main()
