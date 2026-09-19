"""A real producer barge-in Timeline feeds the Judge through to PRD-M006.

The other M006 test starts from a hand-written Timeline, which is how a producer
defect can stay invisible: a fixture can contain shapes no producer emits. This
test starts from the actual event detector (`build_turns` -> `detect_events` ->
`generate_timeline`) on a fused document, asserts the produced Timeline is itself
valid, and only then drives the constrained semantic record into
`barge_in_semantic_compliance` (PRD-M006, `prd_ref == 'PRD-M006'`).

No audio, provider or hardware is involved: the fused segments are synthetic and
the semantic provider is the software fixture.
"""

import unittest

from aivoicebench.findings import generate_findings_document
from aivoicebench.fusion import build_turns, detect_events, generate_timeline
from aivoicebench.llm import LLMJudge, MockLLMProvider
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.semantic_evidence import has_interrupt_evidence, semantic_evidence_records
from aivoicebench.validation import (
    judge_document_errors, semantic_evidence_errors, timeline_errors, turns_errors,
)

RUN_ID = 'RUN-BARGE-IN-PRODUCER'
CASE_ID = 'CASE-BARGE-IN-PRODUCER'

BASE_SEGMENT = {
    'speaker_cluster_confidence': 0.9, 'role_attribution_confidence': 0.9,
    'role_attribution': 'explicit', 'acoustic_boundary_confidence': 0.8,
    'acoustic_uncertainty_ms': None, 'speaker_source': 'asr', 'timing_source': 'acoustic',
    'speaker_evidence': [], 'speaker_candidates': [], 'segment_origin': 'fused',
    'asr_start_ms': None, 'asr_end_ms': None, 'asr_speaker_id': None,
    'text_attribution': 'asr', 'start_boundary_source': 'audio_signal',
    'end_boundary_source': 'audio_signal', 'asr_segment_id': None,
}


def fused_segment(segment_id, start_ms, end_ms, role, text, cluster):
    return dict(BASE_SEGMENT, segment_id=segment_id, start_ms=start_ms, end_ms=end_ms,
                speaker_role=role, speaker_id=cluster, text=text,
                acoustic_segment_id=segment_id.replace('FSEG', 'SEG'))


def barge_in_fused_document():
    """Tester asks, the device answers, and the tester interrupts with a new intent."""
    return {
        'schema_version': '1.0.0', 'document_id': 'FUSED-barge-in',
        'source': {'acoustic_document_id': 'ACOUSTIC-barge-in',
                   'transcript_document_id': None, 'audio_sha256': '2' * 64,
                   'duration_ms': 30000},
        'attribution': {'strategy': 'explicit_user_mapping', 'provider': None,
                        'confidence': 0.9, 'note': 'fixture roles are explicit'},
        'status': 'complete', 'reason': None, 'unattributed_texts': [],
        'segments': [
            fused_segment('FSEG-0000', 500, 1000, 'tester', '今天天气怎么样', 'cluster-0'),
            fused_segment('FSEG-0001', 1500, 5000, 'device',
                          '嗯好的让我看看南京今天晴', 'cluster-1'),
            fused_segment('FSEG-0002', 2500, 3000, 'tester', '不对我想听北京今天天气', 'cluster-0'),
        ],
    }


class ProducerBargeInTests(unittest.TestCase):
    def setUp(self):
        self.fused = barge_in_fused_document()
        self.turns = build_turns(self.fused)
        events, evidence, status, reason = detect_events(
            self.fused, self.turns, run_id=RUN_ID, case_id=CASE_ID)
        self.events = events
        self.timeline = generate_timeline(self.fused, self.turns, events, evidence, status,
                                          reason, run_id=RUN_ID, case_id=CASE_ID)

    def test_the_producer_emits_a_paired_interrupt_interval(self):
        """An interruption must not leave the Timeline invalid."""
        interrupt_events = [event for event in self.events
                            if event['type'].startswith('interrupt')]
        self.assertEqual([event['type'] for event in interrupt_events],
                         ['interrupt_start', 'interrupt_end'])
        self.assertEqual(interrupt_events[0]['start_ms'], 2500)
        self.assertEqual(interrupt_events[1]['start_ms'], 3000)
        self.assertEqual(turns_errors(self.turns), [])
        self.assertEqual(timeline_errors(self.timeline), [],
                         'the producer emitted a barge-in Timeline that is not valid')

    def test_producer_timeline_drives_prd_m006_to_an_observed_value(self):
        self.assertTrue(has_interrupt_evidence(self.turns['turns'][0], self.timeline))
        metrics_result = compute_timeline_metrics(self.timeline)
        judge = LLMJudge(MockLLMProvider())
        document = judge.evaluate(self.fused, self.turns, metrics_result, self.timeline,
                                  run_id=RUN_ID)
        self.assertEqual(judge_document_errors(document, self.timeline, self.turns), [])

        records, abstentions = semantic_evidence_records(document, self.timeline, self.turns)
        compliance = [record for record in records
                      if record['kind'] == 'barge_in_compliance']
        self.assertTrue(compliance, f'no M006 record; abstentions: {abstentions}')
        self.assertEqual(compliance[0]['criterion_id'], 'CRIT-BARGE-IN-COMPLIANCE')
        self.assertIsInstance(compliance[0]['decision'], bool)
        self.assertEqual(semantic_evidence_errors(records, self.timeline, self.turns), [])

        result = compute_timeline_metrics(self.timeline, semantic_evidence=records)
        metric = next(item for item in result['metrics']
                      if item['name'] == 'barge_in_semantic_compliance')
        self.assertEqual(metric['status'], 'observed')
        self.assertEqual(metric['prd_ref'], 'PRD-M006')
        self.assertIsInstance(metric['value'], bool)
        self.assertEqual(metric['judge_profile'], compliance[0]['judge_profile'])
        self.assertTrue(metric['event_ids'])

    def test_a_finding_from_the_producer_timeline_stays_turn_bound(self):
        metrics_result = compute_timeline_metrics(self.timeline)
        judge = LLMJudge(MockLLMProvider())
        document = judge.evaluate(self.fused, self.turns, metrics_result, self.timeline,
                                  run_id=RUN_ID)
        findings_document = generate_findings_document(document, self.timeline,
                                                       metrics_result, RUN_ID)
        self.assertEqual(findings_document['rejected'], [])
        for finding in findings_document['findings']:
            self.assertEqual(finding['turn_ids'], ['TURN-0001'])
            self.assertTrue(finding['event_ids'])


if __name__ == '__main__':
    unittest.main()