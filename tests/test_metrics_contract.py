"""Tests for PRD-M001-M010 canonical MetricResult 3.0.0 metrics.

Verifies:
- First Speech Latency (PRD-M002)
- Turn Gap signed semantics (PRD-M004): positive, negative, zero
- False Endpoint candidate vs confirmed (PRD-M008)
- Barge-in stop bound to interrupted old response_id (PRD-M005)
- not_applicable vs insufficient_evidence distinction
- Canonical MetricResult schema validity
- Correct abstention without semantic evidence
"""

import unittest

from aivoicebench.formulas import (
    latency_ms, turn_gap_ms, turn_gap_ms_legacy,
    barge_in_stop_latency_ms, overlap_duration_ms, overlap_ratio,
)
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.validation import schema_errors


def make_event(eid, etype, start, end=None, turn_id='TURN-0001',
               response_id='RESP-0001', evidence_ids=None, confidence=None):
    if end is None:
        end = start
    return {
        'schema_version': '2.0.0', 'event_id': eid, 'run_id': 'RUN-t',
        'case_id': 'CASE-t', 'turn_id': turn_id, 'response_id': response_id,
        'type': etype, 'start_ms': start, 'end_ms': end,
        'source': 'audio_signal', 'observation_scope': 'black_box',
        'confidence': confidence, 'confidence_source': None, 'uncertainty_ms': None,
        'evidence_ids': evidence_ids or ['EV-001'],
    }


def make_timeline(events, run_id='RUN-t', case_id=None):
    return {
        'schema_version': '2.0.0', 'run_id': run_id, 'case_id': case_id,
        'analysis_id': 'ANALYSIS-t',
        'case_version': '0.0.0', 'attempt': 1, 'execution_kind': 'imported',
        'time_base': {'kind': 'audio_relative_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None,
                         'model': None, 'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': 'complete', 'gaps': [], 'tracks': [], 'artifacts': [],
        'evidence': [], 'events': events,
    }


def find(metrics, name):
    return [m for m in metrics if m['name'] == name]


class CanonicalSchemaTests(unittest.TestCase):
    """Every emitted metric must validate against the canonical schema."""

    def test_all_metrics_schema_valid(self):
        events = [
            make_event('E1', 'tester_speech_start', 500),
            make_event('E2', 'tester_speech_end', 1000),
            make_event('E3', 'device_speech_start', 1500),
            make_event('E4', 'device_speech_end', 2000),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        self.assertTrue(result['metrics'])
        for m in result['metrics']:
            errors = schema_errors(m, 'metric')
            self.assertEqual(errors, [], f'{m["name"]} schema errors: {errors}')

    def test_metric_has_prd_ref_and_policy(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        fs = find(result['metrics'], 'first_speech_latency_ms')[0]
        self.assertEqual(fs['prd_ref'], 'PRD-M002')
        self.assertEqual(fs['schema_version'], '3.0.0')
        self.assertIsNotNone(fs['policy'])
        self.assertIsNotNone(fs['policy_version'])

    def test_counts_by_status(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        counts = result['counts']
        self.assertEqual(counts['total'], len(result['metrics']))
        self.assertGreater(counts['observed'], 0)
        self.assertEqual(counts['total'],
                         counts['observed'] + counts['not_applicable'] + counts['insufficient_evidence'])


class FirstSpeechLatencyTests(unittest.TestCase):
    def test_positive(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'first_speech_latency_ms')[0]
        self.assertEqual(m['value'], 500)
        self.assertEqual(m['status'], 'observed')

    def test_zero(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1000),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'first_speech_latency_ms')[0]
        self.assertEqual(m['value'], 0)
        self.assertEqual(m['status'], 'observed')

    def test_no_device_is_insufficient(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'first_speech_latency_ms')[0]
        self.assertEqual(m['status'], 'insufficient_evidence')
        self.assertIsNone(m['value'])

    def test_orphan_device_is_not_applicable(self):
        """Device speaks first with no tester utterance → not_applicable."""
        events = [
            make_event('E1', 'device_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'device_speech_end', 1000, turn_id='TURN-0001'),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'first_speech_latency_ms')[0]
        self.assertEqual(m['status'], 'not_applicable')


class TurnGapTests(unittest.TestCase):
    def test_positive(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'], 'turn_gap_ms')[0]
        self.assertEqual(m['value'], 500)
        self.assertEqual(m['status'], 'observed')
        self.assertEqual(m['policy'], 'device_speech_start')

    def test_negative_overlap(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 800),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'], 'turn_gap_ms')[0]
        self.assertEqual(m['value'], -200)
        self.assertEqual(m['status'], 'observed')
        self.assertIn('overlap', m['reason'].lower())

    def test_zero(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1000),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'], 'turn_gap_ms')[0]
        self.assertEqual(m['value'], 0)

    def test_negative_schema_valid(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 800),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'], 'turn_gap_ms')[0]
        self.assertEqual(schema_errors(m, 'metric'), [])

    def test_legacy_formula_still_rejects_negative(self):
        with self.assertRaises(ValueError):
            turn_gap_ms_legacy(1000, 800)

    def test_negative_first_speech_is_not_applicable(self):
        """Device before tester end → overlap; latency is not_applicable."""
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 800),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'first_speech_latency_ms')[0]
        self.assertEqual(m['status'], 'not_applicable')
        self.assertIsNone(m['value'])


class FalseEndpointTests(unittest.TestCase):
    def test_candidate_only(self):
        events = [make_event('E1', 'possible_false_endpoint', 700, turn_id=None, response_id=None)]
        metrics = compute_timeline_metrics(make_timeline(events))['metrics']
        cand = find(metrics, 'false_endpoint_candidate')
        self.assertTrue(cand)
        self.assertEqual(cand[0]['value'], True)
        self.assertEqual(cand[0]['policy'], 'candidate_only')
        self.assertIn('confirmation', cand[0]['reason'].lower())
        # No confirmed false_endpoint
        self.assertFalse(find(metrics, 'false_endpoint'))


class BargeInTests(unittest.TestCase):
    def test_stop_latency_bound_to_old_response(self):
        events = [
            make_event('E1', 'device_speech_start', 500, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E2', 'device_speech_end', 2000, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E3', 'interrupt_start', 1500, turn_id='TURN-0001', response_id='RESP-0001'),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'barge_in_stop_latency_ms')[0]
        self.assertEqual(m['value'], 500)
        self.assertEqual(m['status'], 'observed')
        self.assertEqual(m['response_id'], 'RESP-0001')

    def test_wrong_response_not_used(self):
        """A later different response must not satisfy the interrupted response."""
        events = [
            make_event('E1', 'interrupt_start', 1500, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E2', 'device_speech_end', 2000, turn_id='TURN-0002', response_id='RESP-0002'),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'barge_in_stop_latency_ms')[0]
        self.assertEqual(m['status'], 'insufficient_evidence')
        self.assertIsNone(m['value'])

    def test_old_response_ended_before_interrupt_is_not_applicable(self):
        events = [
            make_event('E1', 'device_speech_end', 1000, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E2', 'interrupt_start', 1500, turn_id='TURN-0001', response_id='RESP-0001'),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'barge_in_stop_latency_ms')[0]
        self.assertEqual(m['status'], 'not_applicable')
        self.assertIsNone(m['value'])

    def test_new_intent_abstains(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'barge_in_new_intent_latency_ms')[0]
        self.assertEqual(m['status'], 'insufficient_evidence')
        self.assertEqual(m['prd_ref'], 'PRD-M006')

    def test_barge_in_success_abstains(self):
        events = [make_event('E1', 'interrupt_start', 1500)]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'barge_in_success')[0]
        self.assertEqual(m['status'], 'insufficient_evidence')
        self.assertEqual(m['prd_ref'], 'PRD-M007')


class SemanticAbstentionTests(unittest.TestCase):
    def test_feedback_abstains(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'feedback_latency_ms')[0]
        self.assertEqual(m['status'], 'insufficient_evidence')
        self.assertEqual(m['prd_ref'], 'PRD-M001')

    def test_meaningful_response_abstains(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'meaningful_response_latency_ms')[0]
        self.assertEqual(m['status'], 'insufficient_evidence')
        self.assertEqual(m['prd_ref'], 'PRD-M003')

    def test_empty_timeline(self):
        result = compute_timeline_metrics(make_timeline([]))
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['metrics'], [])


class DefaultAggregationTests(unittest.TestCase):
    def test_observed_aggregation(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'turn_gap_ms')[0]
        agg = m['aggregation']
        self.assertEqual(agg['kind'], 'single')
        self.assertEqual(agg['sample_count'], 1)
        self.assertEqual(agg['total_count'], 1)
        self.assertEqual(agg['excluded_count'], 0)

    def test_insufficient_aggregation(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        m = find(compute_timeline_metrics(make_timeline(events))['metrics'],
                 'turn_gap_ms')[0]
        agg = m['aggregation']
        self.assertEqual(agg['sample_count'], 0)
        self.assertEqual(agg['total_count'], 1)
        self.assertEqual(agg['excluded_count'], 1)

    def test_barge_in_negative_schema_rejected(self):
        """barge_in_stop_latency_ms must not accept a negative value."""
        negative = {
            'schema_version': '3.0.0', 'metric_id': 'RUN-t.barge_in_stop_latency_ms.TURN-0001',
            'run_id': 'RUN-t', 'case_id': None, 'analysis_id': None,
            'prd_ref': 'PRD-M005', 'turn_id': 'TURN-0001', 'response_id': 'RESP-0001',
            'name': 'barge_in_stop_latency_ms', 'definition_version': '3.0.0',
            'policy': 'interrupt_start_to_old_response_end', 'policy_version': '1.0.0',
            'execution_kind': 'imported', 'value': -500, 'unit': 'ms', 'status': 'observed',
            'reason': 'negative must be rejected', 'measurement_scope': 'black_box',
            'method': 'deterministic', 'confidence': None, 'confidence_source': None,
            'uncertainty_ms': None, 'evidence_ids': ['EV-001'], 'event_ids': ['E1'],
            'aggregation': {'kind': 'single', 'sample_count': 1, 'total_count': 1,
                            'excluded_count': 0, 'algorithm': 'single', 'input_metric_ids': []},
        }
        errors = schema_errors(negative, 'metric')
        self.assertTrue(any('minimum' in e or 'less than' in e for e in errors),
                        f'negative barge-in must be rejected, got: {errors}')


if __name__ == '__main__':
    unittest.main()
