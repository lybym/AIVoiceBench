"""Tests for expanded latency metrics and timeline metric computation."""

import unittest

from aivoicebench.formulas import (
    latency_ms, turn_gap_ms, barge_in_stop_latency_ms, overlap_duration_ms,
    overlap_ratio, false_endpoint_detected, feedback_latency_status,
    meaningful_response_latency_status, barge_success,
)
from aivoicebench.metrics import compute_timeline_metrics


def make_event(eid, etype, start, end=None, turn_id='TURN-0001', response_id=None,
               evidence_ids=None):
    if end is None:
        end = start
    return {
        'schema_version': '2.0.0', 'event_id': eid, 'run_id': 'RUN-t', 'case_id': 'CASE-t',
        'turn_id': turn_id, 'response_id': response_id, 'type': etype,
        'start_ms': start, 'end_ms': end, 'source': 'audio_signal',
        'observation_scope': 'black_box', 'confidence': 0.7,
        'evidence_ids': evidence_ids or ['EV-test'],
    }


def make_timeline(events):
    return {
        'schema_version': '2.0.0', 'run_id': 'RUN-t', 'case_id': 'CASE-t',
        'case_version': '0.0.0', 'attempt': 1, 'execution_kind': 'imported',
        'time_base': {'kind': 'run_monotonic_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None,
                         'model': None, 'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': 'complete', 'gaps': [], 'tracks': [], 'artifacts': [],
        'evidence': [], 'events': events,
    }


class FormulaTests(unittest.TestCase):
    def test_latency_ms_basic(self):
        self.assertEqual(latency_ms(1000, 1500), 500)

    def test_latency_ms_overlap_raises(self):
        with self.assertRaises(ValueError):
            latency_ms(1500, 1000)

    def test_turn_gap_ms(self):
        self.assertEqual(turn_gap_ms(2000, 3000), 1000)

    def test_turn_gap_negative_raises(self):
        with self.assertRaises(ValueError):
            turn_gap_ms(-1, 1000)

    def test_turn_gap_overlap_raises(self):
        with self.assertRaises(ValueError):
            turn_gap_ms(2000, 1000)

    def test_barge_in_stop_latency_ms(self):
        self.assertEqual(barge_in_stop_latency_ms(1800, 2000), 200)

    def test_barge_in_stop_latency_reversed_raises(self):
        with self.assertRaises(ValueError):
            barge_in_stop_latency_ms(2000, 1800)

    def test_overlap_duration_ms(self):
        self.assertEqual(overlap_duration_ms(1500, 1800), 300)

    def test_overlap_duration_invalid_raises(self):
        with self.assertRaises(ValueError):
            overlap_duration_ms(1800, 1500)

    def test_overlap_ratio(self):
        self.assertAlmostEqual(overlap_ratio(300, 1000), 0.3)

    def test_overlap_ratio_exceeds_raises(self):
        with self.assertRaises(ValueError):
            overlap_ratio(1500, 1000)

    def test_overlap_ratio_zero_device_raises(self):
        with self.assertRaises(ValueError):
            overlap_ratio(100, 0)

    def test_false_endpoint_detected_true(self):
        events = [{'type': 'possible_false_endpoint'}, {'type': 'silence'}]
        self.assertTrue(false_endpoint_detected(events))

    def test_false_endpoint_detected_false(self):
        events = [{'type': 'silence'}, {'type': 'tester_speech_start'}]
        self.assertFalse(false_endpoint_detected(events))

    def test_false_endpoint_empty(self):
        self.assertFalse(false_endpoint_detected([]))

    def test_feedback_latency_is_insufficient(self):
        result = feedback_latency_status()
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIn('LLM', result['reason'])

    def test_meaningful_response_latency_is_insufficient(self):
        result = meaningful_response_latency_status()
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIn('LLM', result['reason'])

    def test_barge_success_both_true(self):
        self.assertTrue(barge_success(True, True))

    def test_barge_success_one_false(self):
        self.assertFalse(barge_success(True, False))

    def test_barge_success_missing(self):
        self.assertIsNone(barge_success(True, None))


class ComputeMetricsTests(unittest.TestCase):
    def test_basic_turn_metrics(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'tester_speech_end', 1000, turn_id='TURN-0001'),
            make_event('E3', 'device_speech_start', 1500, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E4', 'device_speech_end', 2000, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E5', 'response_start', 1500, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E6', 'response_end', 2000, turn_id='TURN-0001', response_id='RESP-0001'),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        self.assertEqual(result['status'], 'observed')
        names = [m['name'] for m in result['metrics']]
        self.assertIn('first_speech_latency_ms', names)
        self.assertIn('feedback_latency_ms', names)
        self.assertIn('meaningful_response_latency_ms', names)

        latency = [m for m in result['metrics'] if m['name'] == 'first_speech_latency_ms'][0]
        self.assertEqual(latency['value'], 500)
        self.assertEqual(latency['unit'], 'ms')
        self.assertEqual(latency['status'], 'observed')
        self.assertEqual(latency['turn_id'], 'TURN-0001')

    def test_two_turns_computes_turn_gap(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'tester_speech_end', 1000, turn_id='TURN-0001'),
            make_event('E3', 'device_speech_start', 1500, turn_id='TURN-0001'),
            make_event('E4', 'device_speech_end', 2000, turn_id='TURN-0001'),
            make_event('E5', 'tester_speech_start', 3500, turn_id='TURN-0002'),
            make_event('E6', 'tester_speech_end', 4000, turn_id='TURN-0002'),
            make_event('E7', 'device_speech_start', 4500, turn_id='TURN-0002'),
            make_event('E8', 'device_speech_end', 5000, turn_id='TURN-0002'),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        gaps = [m for m in result['metrics'] if m['name'] == 'turn_gap_ms']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['value'], 1500)

    def test_overlap_metrics(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'tester_speech_end', 1200, turn_id='TURN-0001'),
            make_event('E3', 'device_speech_start', 1000, turn_id='TURN-0001'),
            make_event('E4', 'device_speech_end', 2000, turn_id='TURN-0001'),
            make_event('E5', 'overlap_start', 1000, turn_id='TURN-0001'),
            make_event('E6', 'overlap_end', 1200, turn_id='TURN-0001'),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        overlaps = [m for m in result['metrics'] if m['name'] == 'overlap_duration_ms']
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]['value'], 200)
        ratios = [m for m in result['metrics'] if m['name'] == 'overlap_ratio']
        self.assertEqual(len(ratios), 1)
        self.assertAlmostEqual(ratios[0]['value'], 0.2)  # 200/1000

    def test_barge_in_stop_latency(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'tester_speech_end', 1000, turn_id='TURN-0001'),
            make_event('E3', 'device_speech_start', 1200, turn_id='TURN-0001'),
            make_event('E4', 'interrupt_start', 1800, turn_id='TURN-0001'),
            make_event('E5', 'device_speech_end', 2000, turn_id='TURN-0001'),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        barges = [m for m in result['metrics'] if m['name'] == 'barge_in_stop_latency_ms']
        self.assertEqual(len(barges), 1)
        self.assertEqual(barges[0]['value'], 200)

    def test_false_endpoint_metric(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'tester_speech_end', 700, turn_id='TURN-0001'),
            make_event('E3', 'possible_false_endpoint', 700, turn_id='TURN-0001'),
            make_event('E4', 'device_speech_start', 750, turn_id='TURN-0001'),
            make_event('E5', 'device_speech_end', 1500, turn_id='TURN-0001'),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        feps = [m for m in result['metrics'] if m['name'] == 'false_endpoint_detected']
        self.assertEqual(len(feps), 1)
        self.assertTrue(feps[0]['value'])

    def test_insufficient_feedback_and_meaningful_response(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001'),
            make_event('E2', 'tester_speech_end', 1000, turn_id='TURN-0001'),
            make_event('E3', 'device_speech_start', 1500, turn_id='TURN-0001'),
            make_event('E4', 'device_speech_end', 2000, turn_id='TURN-0001'),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        fb = [m for m in result['metrics'] if m['name'] == 'feedback_latency_ms']
        self.assertEqual(len(fb), 1)
        self.assertEqual(fb[0]['status'], 'insufficient_evidence')
        mr = [m for m in result['metrics'] if m['name'] == 'meaningful_response_latency_ms']
        self.assertEqual(len(mr), 1)
        self.assertEqual(mr[0]['status'], 'insufficient_evidence')

    def test_empty_timeline_is_insufficient(self):
        timeline = make_timeline([])
        result = compute_timeline_metrics(timeline)
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_metrics_have_evidence_refs(self):
        events = [
            make_event('E1', 'tester_speech_start', 500, turn_id='TURN-0001', evidence_ids=['EV-1']),
            make_event('E2', 'tester_speech_end', 1000, turn_id='TURN-0001', evidence_ids=['EV-1']),
            make_event('E3', 'device_speech_start', 1500, turn_id='TURN-0001', evidence_ids=['EV-2']),
            make_event('E4', 'device_speech_end', 2000, turn_id='TURN-0001', evidence_ids=['EV-2']),
        ]
        timeline = make_timeline(events)
        result = compute_timeline_metrics(timeline)
        latency = [m for m in result['metrics'] if m['name'] == 'first_speech_latency_ms'][0]
        self.assertIn('EV-1', latency['evidence_ids'])
        self.assertIn('EV-2', latency['evidence_ids'])


if __name__ == '__main__':
    unittest.main()
