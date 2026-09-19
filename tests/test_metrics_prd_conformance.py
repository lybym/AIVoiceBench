"""PRD-M001..M010 conformance tests for the canonical metric engine.

Each test names the Issue #25 acceptance criterion it proves. Nothing here uses
audio, a provider or the network: the engine consumes canonical events only.
"""

import copy
import unittest

from aivoicebench import metrics
from aivoicebench.formulas import word_error_rate
from aivoicebench.metrics import (DEFINITION_VERSION, LEGACY_DEFINITION_VERSION, PRD_REFS_V4,
                                 PRD_REFS_V3, aggregate_metrics, compute_timeline_metrics,
                                 definition_versions_for, latency_percentiles,
                                 migrate_metric_document, migrate_prd_ref, prd_ref_for)
from aivoicebench.validation import metric_errors, schema_errors

PRD_IDS = tuple(f'PRD-M{index:03d}' for index in range(1, 11))


def make_event(eid, etype, start, end=None, turn_id='TURN-0001', response_id='RESP-0001',
               source='audio_signal', evidence_ids=None, uncertainty_ms=None, confidence=0.8):
    return {
        'schema_version': '2.0.0', 'event_id': eid, 'run_id': 'RUN-t', 'case_id': 'CASE-t',
        'turn_id': turn_id, 'response_id': response_id, 'type': etype,
        'start_ms': start, 'end_ms': start if end is None else end,
        'source': source, 'observation_scope': 'black_box', 'confidence': confidence,
        'confidence_source': 'acoustic_boundary' if source == 'audio_signal' else None,
        'uncertainty_ms': uncertainty_ms, 'evidence_ids': evidence_ids or ['EV-001'],
    }


def make_timeline(events, status='complete', gaps=()):
    return {
        'schema_version': '2.0.0', 'run_id': 'RUN-t', 'case_id': 'CASE-t',
        'analysis_id': 'ANALYSIS-t', 'case_version': '0.0.0', 'attempt': 1,
        'execution_kind': 'imported',
        'time_base': {'kind': 'audio_relative_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None, 'model': None,
                         'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': status, 'gaps': list(gaps), 'tracks': [], 'artifacts': [], 'evidence': [],
        'events': events,
    }


def find(metrics_list, name):
    return [m for m in metrics_list if m['name'] == name]


def semantic(kind, decision, turn_id='TURN-0001', **overrides):
    record = {'kind': kind, 'turn_id': turn_id, 'decision': decision,
              'criterion_id': 'CRIT-0001', 'criterion_version': '1.0.0',
              'judge_profile': 'JUDGE-0001', 'confidence': 0.7, 'evidence_ids': ['EV-001'],
              'event_ids': [], 'rationale': 'test record'}
    record.update(overrides)
    return record


class PrdCoverageTests(unittest.TestCase):
    """AC1: every PRD-M001..M010 has a deterministic or eligibility path."""

    def test_every_current_prd_id_has_at_least_one_metric_name(self):
        mapped = set(PRD_REFS_V4.values())
        for prd_id in PRD_IDS:
            self.assertIn(prd_id, mapped, f'{prd_id} has no metric name under {DEFINITION_VERSION}')

    def test_every_legacy_prd_id_has_at_least_one_metric_name(self):
        mapped = set(PRD_REFS_V3.values())
        for prd_id in PRD_IDS:
            self.assertIn(prd_id, mapped, f'{prd_id} has no metric name under {LEGACY_DEFINITION_VERSION}')

    def test_prd_refs_are_the_current_decomposition(self):
        self.assertEqual(prd_ref_for('first_speech_latency_ms'), 'PRD-M001')
        self.assertEqual(prd_ref_for('response_end_candidate_ms'), 'PRD-M002')
        self.assertEqual(prd_ref_for('semantic_response'), 'PRD-M003')
        self.assertEqual(prd_ref_for('turn_gap_ms'), 'PRD-M004')
        self.assertEqual(prd_ref_for('barge_in_stop_latency_ms'), 'PRD-M005')
        self.assertEqual(prd_ref_for('barge_in_semantic_compliance'), 'PRD-M006')
        self.assertEqual(prd_ref_for('false_endpoint_candidate'), 'PRD-M007')
        self.assertEqual(prd_ref_for('false_endpoint_confirmed'), 'PRD-M007')
        self.assertEqual(prd_ref_for('asr_cer'), 'PRD-M008')
        self.assertEqual(prd_ref_for('asr_wer'), 'PRD-M008')
        self.assertEqual(prd_ref_for('overlap_duration_ms'), 'PRD-M009')
        self.assertEqual(prd_ref_for('overlap_ratio'), 'PRD-M009')
        self.assertEqual(prd_ref_for('coverage'), 'PRD-M010')

    def test_legacy_names_carry_no_current_prd_ref(self):
        for name in metrics.LEGACY_METRIC_NAMES:
            self.assertIsNone(prd_ref_for(name), name)

    def test_emitted_documents_are_schema_valid(self):
        events = [
            make_event('E1', 'tester_speech_start', 400),
            make_event('E2', 'tester_speech_end', 1000),
            make_event('E3', 'device_speech_start', 1500),
            make_event('E4', 'device_speech_end', 2000, uncertainty_ms=30.0),
            make_event('E5', 'possible_false_endpoint', 700, turn_id=None, response_id=None),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        names = {m['name'] for m in result['metrics']}
        for prd_id in PRD_IDS:
            self.assertTrue(names & {n for n, r in PRD_REFS_V4.items() if r == prd_id}, prd_id)
        for metric in result['metrics']:
            self.assertEqual(schema_errors(metric, 'metric'), [],
                             f'{metric["name"]}: {schema_errors(metric, "metric")}')
            self.assertEqual(metric['definition_version'], DEFINITION_VERSION)
            self.assertEqual(metric['prd_ref'], prd_ref_for(metric['name']))


class CallingSurfaceTests(unittest.TestCase):
    """AC2: the same fixture and policy give the same values from any surface."""

    def build(self):
        return make_timeline([
            make_event('E1', 'tester_speech_start', 400),
            make_event('E2', 'tester_speech_end', 1000),
            make_event('E3', 'device_speech_start', 1500),
            make_event('E4', 'device_speech_end', 2000, uncertainty_ms=30.0),
            make_event('E5', 'interrupt_start', 1600, response_id='RESP-0001'),
        ])

    @staticmethod
    def values(result):
        return sorted((m['name'], m['turn_id'], m['value'], m['status'], m['unit'],
                       m['prd_ref'], m['policy'], m['uncertainty_ms'])
                      for m in result['metrics'])

    def test_import_scoping_does_not_change_values(self):
        """The import pipeline copies the timeline and rebinds run identity."""
        timeline = self.build()
        direct = compute_timeline_metrics(copy.deepcopy(timeline))
        scoped = dict(timeline)
        scoped['run_id'] = 'RUN-import-1'
        scoped['analysis_id'] = 'ANALYSIS-import-1'
        scoped['case_id'] = None
        from_import = compute_timeline_metrics(scoped)
        self.assertEqual(self.values(direct), self.values(from_import))
        self.assertNotEqual(direct['metrics'][0]['run_id'], from_import['metrics'][0]['run_id'])

    def test_repeated_computation_is_identical(self):
        timeline = self.build()
        first = compute_timeline_metrics(timeline)
        second = compute_timeline_metrics(timeline)
        self.assertEqual(first, second)

    def test_verdict_and_counts_are_stable(self):
        result = compute_timeline_metrics(self.build())
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['counts']['total'], len(result['metrics']))
        self.assertEqual(result['counts']['abstained'],
                         result['counts']['not_applicable']
                         + result['counts']['insufficient_evidence']
                         + result['counts']['invalid'])


class AbstentionTests(unittest.TestCase):
    """AC3: unknown role, missing boundary, missing semantic evidence, invalid integrity."""

    def test_non_acoustic_boundary_abstains(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500, source='asr'),
        ]
        metric = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                      'first_speech_latency_ms')[0]
        self.assertEqual(metric['status'], 'insufficient_evidence')
        self.assertIsNone(metric['value'])
        self.assertIn('acoustic', metric['reason'])

    def test_unknown_role_timeline_abstains_with_the_gap_reason(self):
        """With no confirmed role there is no tester/device speech event at all."""
        gaps = [{'reason': 'Speaker attribution is unresolved',
                 'required_evidence': 'speaker_diarization_or_human_review',
                 'start_ms': 0, 'end_ms': 1000}]
        events = [make_event('E1', 'silence', 0, end=1000, turn_id='TURN-0001',
                             response_id=None, source='derived')]
        result = compute_timeline_metrics(make_timeline(events, status='partial', gaps=gaps))
        overlap = find(result['metrics'], 'overlap_duration_ms')[0]
        self.assertEqual(overlap['status'], 'insufficient_evidence')
        self.assertIn('identity cannot be established', overlap['reason'])
        self.assertIn('Speaker attribution is unresolved', overlap['reason'])
        self.assertFalse(any(m['status'] == 'observed' and m['name'] == 'turn_gap_ms'
                             for m in result['metrics']))

    def test_missing_semantic_evidence_abstains_with_a_reason(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        for name, prd_ref in (('semantic_response', 'PRD-M003'),
                              ('barge_in_semantic_compliance', 'PRD-M006')):
            metric = find(result['metrics'], name)[0]
            self.assertEqual(metric['prd_ref'], prd_ref)
            self.assertIn(metric['status'], ('insufficient_evidence', 'not_applicable'))
            self.assertIsNone(metric['value'])
            self.assertTrue(metric['reason'])

    def test_invalid_artifact_integrity_marks_results_invalid(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(
            make_timeline(events),
            integrity={'status': 'invalid', 'reason': 'Artifact SHA-256 mismatch'})
        self.assertEqual(result['status'], 'invalid')
        self.assertTrue(result['metrics'])
        for metric in result['metrics']:
            self.assertEqual(metric['status'], 'invalid')
            self.assertIsNone(metric['value'])
            self.assertEqual(metric['aggregation']['sample_count'], 0)
            self.assertEqual(schema_errors(metric, 'metric'), [])
        self.assertGreater(result['counts']['invalid'], 0)

    def test_valid_integrity_does_not_change_values(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        unchecked = compute_timeline_metrics(make_timeline(events))
        checked = compute_timeline_metrics(make_timeline(events), integrity={'status': 'valid'})
        self.assertEqual(unchecked['metrics'], checked['metrics'])


class EndpointResponseEndTests(unittest.TestCase):
    """AC4: PRD-M002 never treats an ASR final as the acoustic response end."""

    def test_acoustic_response_end_is_a_candidate_with_uncertainty(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
            make_event('E3', 'device_speech_end', 2400, uncertainty_ms=25.0, confidence=0.6),
        ]
        metric = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                      'response_end_candidate_ms')[0]
        self.assertEqual(metric['prd_ref'], 'PRD-M002')
        self.assertEqual(metric['status'], 'observed')
        self.assertEqual(metric['value'], 2400)
        self.assertEqual(metric['uncertainty_ms'], 25.0)
        self.assertEqual(metric['confidence_source'], 'acoustic_boundary')
        self.assertIn('candidate', metric['reason'])

    def test_asr_final_is_not_the_acoustic_response_end(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
            make_event('E3', 'asr_segment', 2400, source='asr'),
        ]
        metric = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                      'response_end_candidate_ms')[0]
        self.assertEqual(metric['status'], 'insufficient_evidence')
        self.assertIsNone(metric['value'])
        self.assertIn('ASR final', metric['reason'])

    def test_turn_without_a_response_is_not_applicable(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        metric = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                      'response_end_candidate_ms')[0]
        self.assertEqual(metric['status'], 'not_applicable')
        self.assertIsNone(metric['value'])


class FalseEndpointSeparationTests(unittest.TestCase):
    """AC5: PRD-M007 keeps candidate and confirmed states separate."""

    def candidate_events(self):
        return [
            make_event('E1', 'possible_false_endpoint', 700, turn_id=None, response_id=None),
            make_event('E2', 'device_speech_end', 800),
            make_event('E3', 'tester_speech_start', 900),
            make_event('E4', 'planned_pause_start', 600),
            make_event('E5', 'planned_pause_end', 950),
        ]

    def test_candidate_observed_and_confirmation_abstains(self):
        result = compute_timeline_metrics(make_timeline(self.candidate_events()))
        self.assertEqual(find(result['metrics'], 'false_endpoint_candidate')[0]['value'], True)
        confirmed = find(result['metrics'], 'false_endpoint_confirmed')[0]
        self.assertEqual(confirmed['status'], 'insufficient_evidence')
        self.assertIsNone(confirmed['value'])
        self.assertIn('semantic or manual confirmation', confirmed['reason'])

    def test_full_evidence_confirms_and_records_the_judge_profile(self):
        events = self.candidate_events()
        events[0]['turn_id'] = 'TURN-0001'
        events[1]['turn_id'] = 'TURN-0001'
        events[2]['turn_id'] = 'TURN-0001'
        events[3]['turn_id'] = 'TURN-0001'
        events[4]['turn_id'] = 'TURN-0001'
        result = compute_timeline_metrics(
            make_timeline(events),
            semantic_evidence=[semantic('false_endpoint_confirmation', True)])
        confirmed = find(result['metrics'], 'false_endpoint_confirmed')[0]
        self.assertEqual(confirmed['status'], 'observed')
        self.assertEqual(confirmed['value'], True)
        self.assertEqual(confirmed['judge_profile'], 'JUDGE-0001')
        self.assertEqual(confirmed['prd_ref'], 'PRD-M007')

    def test_single_heuristic_never_confirms(self):
        """Tighter than a heuristic: the candidate alone stays a candidate."""
        result = compute_timeline_metrics(make_timeline([self.candidate_events()[0]]))
        self.assertEqual(find(result['metrics'], 'false_endpoint_candidate')[0]['status'],
                         'observed')
        self.assertEqual(find(result['metrics'], 'false_endpoint_confirmed')[0]['status'],
                         'insufficient_evidence')


class TranscriptQualityTests(unittest.TestCase):
    """AC6: PRD-M008 needs an eligible reference and never borrows external ASR."""

    def events(self):
        return [make_event('E1', 'tester_speech_end', 1000),
                make_event('E2', 'device_speech_start', 1500)]

    def test_without_reference_it_is_not_applicable(self):
        result = compute_timeline_metrics(make_timeline(self.events()))
        for name in ('asr_cer', 'asr_wer'):
            metric = find(result['metrics'], name)[0]
            self.assertEqual(metric['status'], 'not_applicable')
            self.assertEqual(metric['prd_ref'], 'PRD-M008')
            self.assertIsNone(metric['value'])

    def test_external_asr_never_populates_the_device_metric(self):
        result = compute_timeline_metrics(
            make_timeline(self.events()),
            reference_transcript={'text': '打开客厅的灯', 'reference_id': 'REF-1'},
            device_transcript={'text': '打开客厅的灯', 'source': 'external_asr',
                               'evidence_ids': ['EV-001']})
        for name in ('asr_cer', 'asr_wer'):
            metric = find(result['metrics'], name)[0]
            self.assertEqual(metric['status'], 'insufficient_evidence')
            self.assertIsNone(metric['value'])
            self.assertIn('external ASR', metric['reason'])

    def test_missing_device_transcript_abstains(self):
        result = compute_timeline_metrics(
            make_timeline(self.events()),
            reference_transcript={'text': '打开客厅的灯', 'reference_id': 'REF-1'})
        metric = find(result['metrics'], 'asr_cer')[0]
        self.assertEqual(metric['status'], 'insufficient_evidence')
        self.assertIn('device-internal transcript', metric['reason'])

    def test_device_internal_transcript_with_evidence_is_observed(self):
        result = compute_timeline_metrics(
            make_timeline(self.events()),
            reference_transcript={'text': '打开客厅的灯', 'reference_id': 'REF-1'},
            device_transcript={'text': '打开客厅的灯', 'source': 'device_internal',
                               'artifact_id': 'ART-1', 'evidence_ids': ['EV-001']})
        cer = find(result['metrics'], 'asr_cer')[0]
        wer = find(result['metrics'], 'asr_wer')[0]
        self.assertEqual(cer['status'], 'observed')
        self.assertEqual(cer['value'], 0.0)
        self.assertEqual(cer['measurement_scope'], 'white_box')
        self.assertEqual(wer['value'], 0.0)
        self.assertEqual(schema_errors(cer, 'metric'), [])
        self.assertEqual(schema_errors(wer, 'metric'), [])

    def test_transcript_without_evidence_is_not_reportable(self):
        result = compute_timeline_metrics(
            make_timeline(self.events()),
            reference_transcript={'text': '打开客厅的灯', 'reference_id': 'REF-1'},
            device_transcript={'text': '打开客厅的灯', 'source': 'device_internal'})
        metric = find(result['metrics'], 'asr_cer')[0]
        self.assertEqual(metric['status'], 'insufficient_evidence')
        self.assertIn('evidence reference', metric['reason'])

    def test_word_error_rate_matches_its_definition(self):
        self.assertEqual(word_error_rate('打开 客厅 的 灯', '打开 客厅 的 灯')['value'], 0.0)
        self.assertEqual(word_error_rate('打开 客厅 的 灯', '打开 厨房 的 灯')['value'], 0.25)
        self.assertEqual(word_error_rate('', 'anything')['status'], 'not_applicable')


class OverlapIdentityTests(unittest.TestCase):
    """AC7: PRD-M009 abstains when tester/device identity is not established."""

    def test_mixed_recording_without_roles_abstains(self):
        events = [
            make_event('E1', 'device_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'device_speech_end', 1500, turn_id='TURN-0001'),
        ]
        metric = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                      'overlap_duration_ms')[0]
        self.assertEqual(metric['prd_ref'], 'PRD-M009')
        self.assertEqual(metric['status'], 'insufficient_evidence')
        self.assertIsNone(metric['value'])
        self.assertIn('identity cannot be established', metric['reason'])

    def test_interval_union_overlap_is_measured_when_both_roles_exist(self):
        events = [
            make_event('E1', 'tester_speech_start', 400),
            make_event('E2', 'tester_speech_end', 1200),
            make_event('E3', 'device_speech_start', 1000),
            make_event('E4', 'device_speech_end', 2000),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        duration = find(result['metrics'], 'overlap_duration_ms')[0]
        ratio = find(result['metrics'], 'overlap_ratio')[0]
        self.assertEqual(duration['value'], 200)
        self.assertEqual(ratio['value'], 0.2)
        self.assertEqual(ratio['unit'], 'ratio')

    def test_absent_overlap_events_are_not_assumed_zero_for_a_missing_detector(self):
        """Only acoustic intervals of both roles may produce a measured overlap."""
        events = [
            make_event('E1', 'tester_speech_start', 400),
            make_event('E2', 'tester_speech_end', 1200),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        overlap = find(result['metrics'], 'overlap_duration_ms')
        self.assertFalse([m for m in overlap if m['status'] == 'observed'])


class AggregationTests(unittest.TestCase):
    """AC8: denominators, excluded/invalid/abstained counts and percentiles."""

    def metric(self, name, value, status='observed', unit='ms', sample=0):
        return {'schema_version': '3.0.0', 'metric_id': f'RUN-t.{name}.S{sample}',
                'run_id': 'RUN-t', 'case_id': None, 'analysis_id': None,
                'prd_ref': prd_ref_for(name), 'turn_id': None, 'response_id': None, 'name': name,
                'definition_version': DEFINITION_VERSION, 'policy': 'test_policy',
                'policy_version': '1.0.0', 'execution_kind': 'imported', 'value': value,
                'unit': unit, 'status': status, 'reason': 'test',
                'measurement_scope': 'black_box', 'method': 'deterministic', 'confidence': None,
                'confidence_source': None, 'uncertainty_ms': None,
                'evidence_ids': [f'EV-{sample:03d}'], 'event_ids': [],
                'aggregation': {'kind': 'single', 'sample_count': 1 if status == 'observed' else 0,
                                'total_count': 1, 'excluded_count': 0 if status == 'observed' else 1,
                                'algorithm': 'single', 'input_metric_ids': []}}

    def test_percentile_aggregate_uses_r7_and_keeps_the_denominator(self):
        metrics = [self.metric('turn_gap_ms', value, sample=index)
                   for index, value in enumerate((100, 200, 300, 400))]
        metrics.append(self.metric('turn_gap_ms', None, status='insufficient_evidence', sample=4))
        metrics.append(self.metric('turn_gap_ms', None, status='invalid', sample=5))
        container = aggregate_metrics(metrics, name='turn_gap_ms', percentile=90)
        self.assertEqual(container['value'], 370)
        self.assertEqual(container['aggregation']['percentile'], 90)
        self.assertEqual(container['aggregation']['sample_count'], 4)
        self.assertEqual(container['aggregation']['total_count'], 6)
        self.assertEqual(container['aggregation']['excluded_count'], 2)
        self.assertEqual(container['aggregation']['abstained_count'], 1)
        self.assertEqual(container['aggregation']['invalid_count'], 1)
        self.assertEqual(container['aggregation']['algorithm'], 'R7')
        self.assertEqual(len(container['aggregation']['input_metric_ids']), 4)
        # Only the eligible samples' evidence is carried forward.
        self.assertEqual(container['evidence_ids'], ['EV-000', 'EV-001', 'EV-002', 'EV-003'])
        self.assertEqual(schema_errors(container, 'metric'), [])

    def test_reported_percentile_semantics_are_the_r7_example(self):
        metrics = [self.metric('turn_gap_ms', value, sample=index)
                   for index, value in enumerate((100, 200, 300, 400))]
        stats = latency_percentiles(metrics)
        self.assertEqual([stats['P50'], stats['P90'], stats['P95'], stats['P99']],
                         [250, 370, 385, 397])
        self.assertEqual(stats['sample_count'], 4)
        self.assertEqual(stats['algorithm'], 'R7')

    def test_all_ineligible_keeps_the_denominator_and_abstains(self):
        metrics = [self.metric('turn_gap_ms', None, status='insufficient_evidence')]
        container = aggregate_metrics(metrics, name='turn_gap_ms', percentile=90)
        self.assertEqual(container['status'], 'insufficient_evidence')
        self.assertIsNone(container['value'])
        self.assertEqual(container['aggregation']['total_count'], 1)
        self.assertEqual(container['aggregation']['sample_count'], 0)
        self.assertEqual(schema_errors(container, 'metric'), [])

    def test_percentile_aggregate_requires_millisecond_samples(self):
        metrics = [self.metric('coverage', 0.5, unit='ratio')]
        with self.assertRaises(ValueError):
            aggregate_metrics(metrics, name='coverage', percentile=90)

    def test_incompatible_samples_are_rejected(self):
        metrics = [self.metric('turn_gap_ms', 100)]
        mixed = dict(metrics[0])
        mixed['execution_kind'] = 'synthetic'
        with self.assertRaises(ValueError):
            aggregate_metrics(metrics + [mixed], name='turn_gap_ms')

    def test_duplicate_metric_ids_are_rejected(self):
        metric = self.metric('turn_gap_ms', 100)
        with self.assertRaises(ValueError):
            aggregate_metrics([metric, dict(metric)], name='turn_gap_ms')


class CoverageOutcomeTests(unittest.TestCase):
    """PRD-M010: planned/attempted/observed/abstained stay distinct."""

    def test_coverage_reports_attempted_and_measured(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000, turn_id='TURN-0001'),
            make_event('E2', 'device_speech_start', 1500, turn_id='TURN-0001'),
            make_event('E3', 'tester_speech_end', 3000, turn_id='TURN-0002'),
        ]
        result = compute_timeline_metrics(make_timeline(events), planned_units=4)
        coverage = find(result['metrics'], 'coverage')[0]
        self.assertEqual(coverage['prd_ref'], 'PRD-M010')
        self.assertEqual(coverage['status'], 'observed')
        self.assertEqual(coverage['value'], 0.5)
        self.assertEqual(coverage['aggregation']['kind'], 'rate')
        self.assertEqual(coverage['aggregation']['sample_count'], 1)
        self.assertEqual(coverage['aggregation']['total_count'], 2)
        self.assertEqual(coverage['aggregation']['planned_count'], 4)
        self.assertEqual(schema_errors(coverage, 'metric'), [])

    def test_no_plan_is_reported_rather_than_substituted(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        coverage = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                        'coverage')[0]
        self.assertNotIn('planned_count', coverage['aggregation'])
        self.assertIn('control or transport success is never substituted', coverage['reason'])


class DefinitionCompatibilityTests(unittest.TestCase):
    """AC9: historical MetricResults stay readable and migrate explicitly."""

    def test_historical_document_is_still_schema_valid(self):
        historical = {
            'schema_version': '3.0.0', 'metric_id': 'RUN-old.first_speech_latency_ms.TURN-0001',
            'run_id': 'RUN-old', 'case_id': None, 'analysis_id': None, 'prd_ref': 'PRD-M002',
            'turn_id': 'TURN-0001', 'response_id': 'RESP-0001', 'name': 'first_speech_latency_ms',
            'definition_version': '3.0.0', 'policy': 'tester_end_to_device_start',
            'policy_version': '1.0.0', 'execution_kind': 'imported', 'value': 500, 'unit': 'ms',
            'status': 'observed', 'reason': 'Measured from timeline events',
            'measurement_scope': 'black_box', 'method': 'deterministic', 'confidence': None,
            'confidence_source': None, 'uncertainty_ms': None, 'evidence_ids': ['EV-001'],
            'event_ids': ['E1'],
            'aggregation': {'kind': 'single', 'sample_count': 1, 'total_count': 1,
                            'excluded_count': 0, 'algorithm': 'single', 'input_metric_ids': []},
        }
        self.assertEqual(schema_errors(historical, 'metric'), [])
        # The historical id keeps the historical meaning under its own definition.
        self.assertEqual(prd_ref_for('first_speech_latency_ms', '3.0.0'), 'PRD-M002')
        self.assertEqual(definition_versions_for('PRD-M002', 'first_speech_latency_ms'),
                         ['3.0.0'])
        self.assertEqual(definition_versions_for('PRD-M001', 'first_speech_latency_ms'),
                         ['4.0.0'])

    def test_migration_is_explicit_and_never_mutates_the_input(self):
        historical = {'name': 'first_speech_latency_ms', 'prd_ref': 'PRD-M002',
                      'definition_version': '3.0.0', 'value': 500}
        original = copy.deepcopy(historical)
        migrated = migrate_metric_document(historical)
        self.assertEqual(historical, original)
        self.assertEqual(migrated['prd_ref'], 'PRD-M001')
        self.assertEqual(migrated['definition_version'], '4.0.0')
        self.assertEqual(migrated['migration']['status'], 'mapped')
        self.assertEqual(migrated['migration']['from_prd_ref'], 'PRD-M002')
        self.assertEqual(migrated['value'], 500)

    def test_legacy_only_name_keeps_its_historical_reference(self):
        prd_ref, status = migrate_prd_ref('PRD-M001', 'feedback_latency_ms')
        self.assertIsNone(prd_ref)
        self.assertEqual(status, 'no_current_requirement')
        migrated = migrate_metric_document({'name': 'feedback_latency_ms', 'prd_ref': 'PRD-M001',
                                           'definition_version': '3.0.0'})
        # Never silently reinterpreted as the new PRD-M001 (First Speech Latency).
        self.assertEqual(migrated['prd_ref'], 'PRD-M001')
        self.assertEqual(migrated['migration']['status'], 'no_current_requirement')

    def test_unknown_name_is_reported_as_unknown(self):
        prd_ref, status = migrate_prd_ref('PRD-M001', 'not_a_metric_name')
        self.assertIsNone(prd_ref)
        self.assertEqual(status, 'unknown_name')

    def test_validation_rejects_a_prd_ref_the_definition_does_not_assign(self):
        events = [make_event('E1', 'tester_speech_end', 1000),
                  make_event('E2', 'device_speech_start', 1500)]
        result = compute_timeline_metrics(make_timeline(events))
        metric = find(result['metrics'], 'first_speech_latency_ms')[0]
        metric['prd_ref'] = 'PRD-M002'
        errors = metric_errors(metric)
        self.assertTrue(any('silently reinterprets' in e for e in errors), errors)

    def test_validation_rejects_a_legacy_name_without_its_reason(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        metric = find(result['metrics'], 'feedback_latency_ms')[0]
        # An abstention needs a reason through the schema; a measured legacy record
        # without a PRD reference must still explain itself.
        without_reason = dict(metric)
        without_reason.pop('reason')
        self.assertTrue(any('reason' in e for e in metric_errors(without_reason)),
                        metric_errors(without_reason))
        measured = dict(without_reason, status='observed', value=0.0, evidence_ids=['EV-001'],
                        aggregation={'kind': 'single', 'sample_count': 1, 'total_count': 1,
                                     'excluded_count': 0, 'algorithm': 'single',
                                     'input_metric_ids': []})
        errors = metric_errors(measured)
        self.assertTrue(any('legacy metric without a PRD reference' in e for e in errors), errors)


if __name__ == '__main__':
    unittest.main()
