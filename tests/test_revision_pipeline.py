"""Tests for human revision contract and unified pipeline."""

import json
import tempfile
import unittest
from pathlib import Path

from aivoicebench.revision import RevisionStore
from aivoicebench.pipeline import run_full_pipeline, _integrate_llm_metrics


class RevisionStoreTests(unittest.TestCase):
    def test_add_and_retrieve_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RevisionStore(tmp)
            rev = store.add_revision('segment', 'FSEG-0000', 'speaker_role',
                                     'tester', 'device', 'reviewer1', 'misidentified')
            self.assertTrue(rev['revision_id'].startswith('REV-'))
            revs = store.get_revisions_for('segment', 'FSEG-0000')
            self.assertEqual(len(revs), 1)
            self.assertEqual(revs[0]['revised_value'], 'device')
            self.assertEqual(revs[0]['original_value'], 'tester')

    def test_original_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RevisionStore(tmp)
            store.add_revision('segment', 'FSEG-0000', 'speaker_role',
                               'tester', 'device', 'r1', 'fix')
            doc = {'segment_id': 'FSEG-0000', 'speaker_role': 'tester', 'start_ms': 500}
            revised = store.apply_revisions([doc], 'segment', 'segment_id')
            self.assertEqual(doc['speaker_role'], 'tester')  # original unchanged
            self.assertEqual(revised[0]['speaker_role'], 'device')  # revised
            self.assertEqual(revised[0]['_original_speaker_role'], 'tester')

    def test_multiple_revisions_same_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RevisionStore(tmp)
            store.add_revision('event', 'EVT-0001', 'start_ms', 1000, 1050, 'r1', 'timing')
            store.add_revision('event', 'EVT-0001', 'confidence', 0.7, 0.9, 'r1', 'recheck')
            revs = store.get_revisions_for('event', 'EVT-0001')
            self.assertEqual(len(revs), 2)

    def test_confirm_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RevisionStore(tmp)
            rev = store.confirm_finding('FIND-001', 'reviewer1', confirmed=True, notes='verified')
            self.assertEqual(rev['revised_value'], 'confirmed')
            self.assertEqual(rev['original_value'], 'needs_verification')

    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store1 = RevisionStore(tmp)
            store1.add_revision('turn', 'TURN-0001', 'has_interruption', False, True, 'r1', 'observed')
            store2 = RevisionStore(tmp)
            revs = store2.get_revisions_for('turn', 'TURN-0001')
            self.assertEqual(len(revs), 1)
            self.assertEqual(revs[0]['revised_value'], True)

    def test_apply_to_fused_segments_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RevisionStore(tmp)
            store.add_revision('segment', 'FSEG-0001', 'speaker_role', 'device', 'tester', 'r1', 'wrong role')
            doc = {
                'segments': [
                    {'segment_id': 'FSEG-0000', 'speaker_role': 'tester'},
                    {'segment_id': 'FSEG-0001', 'speaker_role': 'device'},
                ]
            }
            revised = store.apply_revisions(doc, 'segment', 'segment_id')
            self.assertEqual(revised['segments'][1]['speaker_role'], 'tester')
            self.assertEqual(revised['segments'][1]['_original_speaker_role'], 'device')
            self.assertTrue(len(revised['_applied_revisions']) > 0)


class LLMIntegrationTests(unittest.TestCase):
    def test_meaningful_response_resolves_insufficient(self):
        metrics = {'status': 'insufficient_evidence', 'metrics': [
            {'name': 'meaningful_response_latency_ms', 'value': None, 'unit': 'ms',
             'status': 'insufficient_evidence', 'turn_id': 'TURN-0001'},
            {'name': 'feedback_latency_ms', 'value': None, 'unit': 'ms',
             'status': 'insufficient_evidence', 'turn_id': 'TURN-0001'},
        ]}
        judge_results = [
            {'dimension': 'meaningful_response', 'status': 'observed',
             'meaningful_response_start_ms': 2346, 'confidence': 0.6,
             'turn_id': 'TURN-0001', 'evidence_refs': ['EV-1']},
            {'dimension': 'feedback_detection', 'status': 'observed',
             'feedback_type': 'filler', 'feedback_start_ms': 1500,
             'feedback_end_ms': 2346, 'confidence': 0.6},
        ]
        result = _integrate_llm_metrics(metrics, judge_results)
        mr = [m for m in result['metrics'] if m['name'] == 'meaningful_response_latency_ms'][0]
        self.assertEqual(mr['status'], 'insufficient_evidence')
        self.assertIsNone(mr['value'])
        fb = [m for m in result['metrics'] if m['name'] == 'feedback_latency_ms'][0]
        self.assertEqual(fb['status'], 'insufficient_evidence')
        self.assertIsNone(fb['value'])


if __name__ == '__main__':
    unittest.main()
