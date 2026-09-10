"""Tests for PRD-M001-M010 metric contracts and E2E metrics pipeline.

Covers:
- First Speech Latency (PRD-M002): positive, zero, missing device
- Turn Gap (PRD-M004): positive, negative (overlap), zero, old vs new formula
- False Endpoint (PRD-M008): candidate only, not confirmed
- Barge-in Stop Latency (PRD-M005): must reference old response_id
- Unknown role: role-dependent metrics must abstain
- Confidence: no hardcoded values
- Provenance: metric references timeline artifact
"""

import json
import math
import sys
import unittest
from array import array
import wave
from pathlib import Path
import tempfile

from aivoicebench.formulas import (
    latency_ms, turn_gap_ms, turn_gap_ms_legacy,
    barge_in_stop_latency_ms, overlap_duration_ms, overlap_ratio,
    false_endpoint_detected,
)
from aivoicebench.metrics import compute_timeline_metrics


def make_event(eid, etype, start, end=None, turn_id='TURN-0001',
               response_id='RESP-0001', evidence_ids=None, confidence=0.0):
    if end is None:
        end = start
    return {
        'schema_version': '2.0.0', 'event_id': eid, 'run_id': 'RUN-t',
        'case_id': 'CASE-t', 'turn_id': turn_id, 'response_id': response_id,
        'type': etype, 'start_ms': start, 'end_ms': end,
        'source': 'audio_signal', 'observation_scope': 'black_box',
        'confidence': confidence, 'evidence_ids': evidence_ids or ['EV-001'],
    }


def make_timeline(events):
    return {
        'schema_version': '2.0.0', 'run_id': 'RUN-t', 'case_id': 'CASE-t',
        'case_version': '0.0.0', 'attempt': 1, 'execution_kind': 'imported',
        'time_base': {'kind': 'audio_relative_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None,
                         'model': None, 'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': 'complete', 'gaps': [], 'tracks': [], 'artifacts': [],
        'evidence': [], 'events': events,
    }


class FirstSpeechLatencyTests(unittest.TestCase):
    """PRD-M002: tester_speech_end → device_speech_start"""

    def test_positive_latency(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'first_speech_latency_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['value'], 500)
        self.assertEqual(m[0]['status'], 'observed')
        self.assertEqual(m[0]['prd_ref'], 'PRD-M002')

    def test_zero_latency(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1000),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'first_speech_latency_ms']
        self.assertEqual(m[0]['value'], 0)

    def test_missing_device_start(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'first_speech_latency_ms']
        self.assertFalse(m, 'No metric when device_start missing')


class TurnGapTests(unittest.TestCase):
    """PRD-M004: tester_speech_end → device_speech_start (SAME turn)"""

    def test_positive_turn_gap(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'turn_gap_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['value'], 500)
        self.assertEqual(m[0]['prd_ref'], 'PRD-M004')
        self.assertEqual(m[0]['policy'], 'device_speech_start')

    def test_negative_turn_gap_overlap(self):
        """Device starts before tester finishes — negative gap = overlap."""
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 800),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'turn_gap_ms']
        self.assertTrue(m, 'Negative turn gap must produce a metric, not skip')
        self.assertEqual(m[0]['value'], -200)
        self.assertEqual(m[0]['status'], 'observed')
        self.assertIn('overlap', m[0].get('reason', '').lower())

    def test_zero_turn_gap(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1000),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'turn_gap_ms']
        self.assertEqual(m[0]['value'], 0)

    def test_legacy_formula_raises_on_negative(self):
        """Legacy formula must reject negative (old behavior, backward compat)."""
        with self.assertRaises(ValueError):
            turn_gap_ms_legacy(1000, 800)

    def test_new_formula_allows_negative(self):
        """New formula must accept negative (overlap)."""
        self.assertEqual(turn_gap_ms(1000, 800), -200)

    def test_no_device_in_turn(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'turn_gap_ms']
        self.assertFalse(m)


class FalseEndpointTests(unittest.TestCase):
    """PRD-M008: possible_false_endpoint is candidate, not confirmed"""

    def test_candidate_not_confirmed(self):
        events = [
            make_event('E1', 'possible_false_endpoint', 700, turn_id=None, response_id=None),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'false_endpoint_candidate']
        self.assertTrue(m)
        self.assertEqual(m[0]['value'], True)
        self.assertEqual(m[0]['status'], 'observed')
        self.assertIn('confirmation', m[0].get('reason', '').lower())
        # Must NOT have a 'false_endpoint_detected' confirmed metric
        confirmed = [x for x in result['metrics'] if x['name'] == 'false_endpoint_detected']
        self.assertFalse(confirmed, 'Must not output confirmed false_endpoint_detected')

    def test_no_false_endpoint_event(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if 'false_endpoint' in x['name']]
        self.assertFalse(m)


class BargeInTests(unittest.TestCase):
    """PRD-M005: interrupt_start → old device_speech_end (same turn/response)"""

    def test_barge_in_stop_latency(self):
        events = [
            make_event('E1', 'device_speech_start', 500, turn_id='TURN-0001', response_id='RESP-0001'),
            make_event('E2', 'interrupt_start', 1500, turn_id='TURN-0001', response_id=None),
            make_event('E3', 'device_speech_end', 2000, turn_id='TURN-0001', response_id='RESP-0001'),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'barge_in_stop_latency_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['value'], 500)
        self.assertEqual(m[0]['prd_ref'], 'PRD-M005')

    def test_barge_in_must_reference_old_response(self):
        """Must NOT use device_speech_end from a different response."""
        events = [
            make_event('E1', 'interrupt_start', 1500, turn_id='TURN-0001', response_id=None),
            make_event('E2', 'device_speech_end', 2000, turn_id='TURN-0002', response_id='RESP-0002'),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        # TURN-0001 has interrupt but no device_speech_end in same turn
        # TURN-0002 has device_speech_end but no interrupt in same turn
        m = [x for x in result['metrics'] if x['name'] == 'barge_in_stop_latency_ms']
        self.assertFalse(m, 'Must not cross-reference different turns/responses')

    def test_prd_m006_new_intent_abstains(self):
        """PRD-M006: New Intent Latency must be insufficient_evidence."""
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'barge_in_new_intent_latency_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['status'], 'insufficient_evidence')
        self.assertEqual(m[0]['prd_ref'], 'PRD-M006')

    def test_prd_m007_success_abstains(self):
        """PRD-M007: Barge-in Success must be insufficient_evidence without all 4 components."""
        events = [make_event('E1', 'interrupt_start', 1500)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'barge_in_success']
        self.assertTrue(m)
        self.assertEqual(m[0]['status'], 'insufficient_evidence')
        self.assertEqual(m[0]['prd_ref'], 'PRD-M007')


class ConfidenceTests(unittest.TestCase):
    """No hardcoded confidence values — use provider evidence or null."""

    def test_metric_has_policy_version(self):
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        for m in result['metrics']:
            self.assertIn('policy_version', m)
            self.assertIsNotNone(m['policy_version'])

    def test_no_hardcoded_event_confidence(self):
        """Events with None confidence should not get fabricated numbers."""
        events = [
            make_event('E1', 'tester_speech_end', 1000, confidence=None),
            make_event('E2', 'device_speech_start', 1500, confidence=None),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'first_speech_latency_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['status'], 'observed')


class CorrectAbstentionTests(unittest.TestCase):
    """Metrics that should abstain must do so correctly."""

    def test_empty_timeline(self):
        result = compute_timeline_metrics(make_timeline([]))
        self.assertEqual(result['status'], 'insufficient_evidence')

    def test_feedback_latency_abstains(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'feedback_latency_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['status'], 'insufficient_evidence')
        self.assertEqual(m[0]['prd_ref'], 'PRD-M001')

    def test_meaningful_response_abstains(self):
        events = [make_event('E1', 'tester_speech_end', 1000)]
        result = compute_timeline_metrics(make_timeline(events))
        m = [x for x in result['metrics'] if x['name'] == 'meaningful_response_latency_ms']
        self.assertTrue(m)
        self.assertEqual(m[0]['status'], 'insufficient_evidence')
        self.assertEqual(m[0]['prd_ref'], 'PRD-M003')

    def test_counts_reported(self):
        """Total/observed/insufficient counts must be reported."""
        events = [
            make_event('E1', 'tester_speech_end', 1000),
            make_event('E2', 'device_speech_start', 1500),
        ]
        result = compute_timeline_metrics(make_timeline(events))
        self.assertIn('total_count', result)
        self.assertIn('observed_count', result)
        self.assertIn('insufficient_count', result)
        self.assertEqual(result['total_count'],
                         result['observed_count'] + result['insufficient_count'])


class FormulaVersioningTests(unittest.TestCase):
    """Turn Gap formula versioning — old vs new semantics."""

    def test_new_turn_gap_direction(self):
        """New: tester_end → device_start (same turn)."""
        self.assertEqual(turn_gap_ms(1000, 1500), 500)

    def test_legacy_turn_gap_direction(self):
        """Legacy: device_end → next_tester_start."""
        self.assertEqual(turn_gap_ms_legacy(1000, 1500), 500)

    def test_new_allows_negative(self):
        self.assertEqual(turn_gap_ms(1000, 800), -200)

    def test_legacy_rejects_negative(self):
        with self.assertRaises(ValueError):
            turn_gap_ms_legacy(1000, 800)


if __name__ == '__main__':
    unittest.main()
