"""Tests for the LLM Harness: provider fixture, Judge, and structured output.

The Judge no longer authors timing. Every test below therefore checks either a
*selection* from measured anchors or an explicit abstention; a result that
invents a millisecond, cites nothing, or claims a cause without device logs must
fail its own contract. The `mock` provider is a software fixture — no test here
claims a real model evaluation.
"""

import json
import tempfile
import unittest
from pathlib import Path

from aivoicebench.llm import (
    FILLER_PATTERNS, LLMJudge, MockLLMProvider, judge_pipeline,
)
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.validation import judge_document_errors, judge_result_errors
from tests.judge_fixture import (
    RUN_ID, dialogue_timeline, fused_document, turns_document,
)


class MockProviderTests(unittest.TestCase):
    """The fixture may only select anchors; it may never author a timestamp."""

    def setUp(self):
        self.provider = MockLLMProvider()
        self.timeline = dialogue_timeline()
        self.anchors = [
            {'anchor_id': event['event_id'], 'event_type': event['type'],
             'start_ms': event['start_ms'], 'end_ms': event['end_ms'],
             'source': 'acoustic_boundary', 'usable': True}
            for event in self.timeline['events'] if event['source'] != 'derived'
        ]

    def test_filler_patterns_are_declared(self):
        self.assertTrue(FILLER_PATTERNS)
        filler, meaningful = self.provider._strip_filler('嗯……好的，让我看看。南京今天晴')
        self.assertTrue(filler)
        self.assertEqual(meaningful, '南京今天晴')

    def test_meaningful_response_selects_a_measured_anchor(self):
        raw, invocation = self.provider.complete('', '', 'meaningful_response', {
            'text': '嗯……好的，让我看看。南京今天天气晴朗。',
            'device_speech_start_ms': 3480,
            'device_speech_end_ms': 4020,
            'anchors': self.anchors,
        })
        self.assertEqual(raw['status'], 'observed')
        self.assertEqual(invocation.status, 'success')
        selection = raw['anchor_refs']
        self.assertEqual(len(selection), 1)
        self.assertEqual(selection[0]['role'], 'meaningful_start')
        self.assertIn(selection[0]['anchor_id'], [anchor['anchor_id'] for anchor in self.anchors])
        # The provider returns no millisecond of its own.
        self.assertNotIn('meaningful_response_start_ms', raw)

    def test_meaningful_response_without_a_boundary_abstains(self):
        raw, _invocation = self.provider.complete('', '', 'meaningful_response', {
            'text': '嗯……好的，让我看看。南京今天天气晴朗。',
            'device_speech_start_ms': 3480,
            'device_speech_end_ms': 4020,
            'anchors': [],
        })
        self.assertEqual(raw['status'], 'insufficient_evidence')
        self.assertIn('cannot become an acoustic timestamp', raw['reason'])

    def test_meaningful_response_all_filler_and_no_text_abstain(self):
        for text in ('嗯……好的', '', None):
            raw, _invocation = self.provider.complete('', '', 'meaningful_response', {
                'text': text, 'device_speech_start_ms': 3480,
                'device_speech_end_ms': 4020, 'anchors': self.anchors})
            self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_feedback_interval_needs_two_measured_boundaries(self):
        raw, _invocation = self.provider.complete('', '', 'feedback_detection', {
            'text': '嗯……好的，让我看看。南京今天',
            'device_speech_start_ms': 3480,
            'anchors': [self.anchors[2]],
        })
        self.assertEqual(raw['status'], 'insufficient_evidence')
        self.assertIn('two measured boundaries', raw['reason'])

        raw, _invocation = self.provider.complete('', '', 'feedback_detection', {
            'text': '嗯……好的，让我看看。南京今天',
            'device_speech_start_ms': 3480,
            'anchors': self.anchors[2:],
        })
        self.assertEqual(raw['status'], 'observed')
        self.assertEqual({item['role'] for item in raw['anchor_refs']},
                         {'feedback_start', 'feedback_end'})

    def test_feedback_without_filler_abstains(self):
        raw, _invocation = self.provider.complete('', '', 'feedback_detection', {
            'text': '南京今天', 'device_speech_start_ms': 3480, 'anchors': self.anchors})
        self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_intent_classification(self):
        for text, label in (('今天天气怎么样？', 'weather_query'),
                            ('现在几点', 'time_query'),
                            ('播放音乐', 'media_playback')):
            raw, _invocation = self.provider.complete('', '', 'intent', {'tester_text': text})
            self.assertEqual(raw['intent_label'], label)
        raw, _invocation = self.provider.complete('', '', 'intent', {'tester_text': ''})
        self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_conversation_quality_uses_observed_metrics_only(self):
        raw, _invocation = self.provider.complete('', '', 'conversation_quality', {
            'metrics': [{'name': 'first_speech_latency_ms', 'value': 500, 'status': 'observed'}],
            'events': [], 'evidence_refs': ['EVD-DEVICE-START'],
            'event_refs': ['EVT-DEVICE-START']})
        self.assertEqual(raw['status'], 'observed')
        self.assertGreater(raw['score'], 0.7)

        raw, _invocation = self.provider.complete('', '', 'conversation_quality', {
            'metrics': [{'name': 'first_speech_latency_ms', 'value': 3000, 'status': 'observed'}],
            'events': [], 'evidence_refs': ['EVD-DEVICE-START'],
            'event_refs': ['EVT-DEVICE-START']})
        self.assertLess(raw['score'], 0.7)

    def test_finding_candidate_cites_the_metric_it_used(self):
        metric = {'name': 'first_speech_latency_ms', 'value': 3000, 'status': 'observed',
                  'evidence_ids': ['EVD-DEVICE-START'], 'event_ids': ['EVT-DEVICE-START']}
        raw, _invocation = self.provider.complete('', '', 'finding_candidate',
                                                  {'metrics': [metric], 'events': []})
        self.assertEqual(raw['decision'], 'high_latency')
        self.assertEqual(raw['finding_severity'], 'medium')
        self.assertEqual(raw['suspected_layer'], 'llm')
        self.assertTrue(raw['requires_log_verification'])
        self.assertIsNotNone(raw['attribution_confidence'])
        self.assertEqual(raw['evidence_refs'], ['EVD-DEVICE-START'])
        self.assertEqual(raw['event_refs'], ['EVT-DEVICE-START'])

    def test_finding_candidate_for_false_endpoint_cites_the_event(self):
        event = {'type': 'possible_false_endpoint', 'event_id': 'EVT-FEP',
                 'evidence_ids': ['EVD-FEP']}
        raw, _invocation = self.provider.complete('', '', 'finding_candidate',
                                                  {'metrics': [], 'events': [event]})
        self.assertEqual(raw['decision'], 'false_endpoint_detected')
        self.assertEqual(raw['suspected_layer'], 'endpoint')
        self.assertEqual(raw['event_refs'], ['EVT-FEP'])

    def test_no_candidate_when_nothing_is_wrong(self):
        raw, _invocation = self.provider.complete('', '', 'finding_candidate', {
            'metrics': [{'name': 'first_speech_latency_ms', 'value': 500,
                         'status': 'observed'}], 'events': []})
        self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_semantic_verdict_needs_a_citable_reference(self):
        context = {'tester_text': '今天天气怎么样', 'device_text': '北京今天晴'}
        raw, _invocation = self.provider.complete('', '', 'semantic_response', dict(context))
        self.assertEqual(raw['status'], 'insufficient_evidence')
        self.assertIn('without a citation is not evidence', raw['reason'])

        raw, _invocation = self.provider.complete(
            '', '', 'semantic_response',
            dict(context, evidence_refs=['EVD-DEVICE-START'],
                 event_refs=['EVT-DEVICE-START']))
        self.assertEqual(raw['status'], 'observed')
        self.assertIsInstance(raw['semantic_decision'], bool)


class LLMJudgeTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.fused = fused_document()
        self.metrics = compute_timeline_metrics(self.timeline)
        self.judge = LLMJudge(MockLLMProvider())
        self.document = self.judge.evaluate(self.fused, self.turns, self.metrics,
                                           self.timeline, run_id=RUN_ID)

    def test_per_turn_dimensions_are_judged_for_each_turn(self):
        dimensions = [result['dimension'] for result in self.document['results']]
        self.assertEqual(dimensions.count('intent'), 1)
        self.assertEqual(dimensions.count('meaningful_response'), 1)
        self.assertEqual(dimensions.count('semantic_response'), 1)
        self.assertEqual(dimensions.count('feedback_detection'), 1)
        # Run-level judgments happen once.
        self.assertEqual(dimensions.count('conversation_quality'), 1)
        self.assertEqual(dimensions.count('finding_candidate'), 1)

    def test_every_result_validates_and_carries_provenance(self):
        self.assertEqual(judge_document_errors(self.document, self.timeline, self.turns), [])
        for result in self.document['results']:
            self.assertEqual(judge_result_errors(result), [])
            self.assertEqual(result['run_id'], RUN_ID)
            self.assertTrue(result['judge_profile']['profile_id'])
            self.assertTrue(result['prompt_version'])
            self.assertEqual(result['provider'], 'mock')

    def test_meaningful_response_reports_the_selected_anchor_millisecond(self):
        result = next(item for item in self.document['results']
                      if item['dimension'] == 'meaningful_response')
        self.assertEqual(result['status'], 'observed')
        anchor = result['anchor_refs'][0]
        self.assertEqual(result['meaningful_response_start_ms'], anchor['start_ms'])

    def test_intent_abstains_when_the_turn_has_no_tester_text(self):
        fused = fused_document()
        for segment in fused['segments']:
            if segment['speaker_role'] == 'tester':
                segment['text'] = None
        document = LLMJudge(MockLLMProvider()).evaluate(
            fused, self.turns, self.metrics, self.timeline, run_id=RUN_ID)
        result = next(item for item in document['results'] if item['dimension'] == 'intent')
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['abstention_reason'],
                         'No tester transcript available for intent classification')

    def test_no_device_segments_abstains_with_a_reason(self):
        turns = turns_document()
        turns['turns'][0]['device_segment_ids'] = []
        turns['turns'][0]['device_speech_start_ms'] = None
        turns['turns'][0]['device_speech_end_ms'] = None
        document = LLMJudge(MockLLMProvider()).evaluate(
            self.fused, turns, self.metrics, self.timeline, run_id=RUN_ID)
        for dimension in ('meaningful_response', 'feedback_detection'):
            result = next(item for item in document['results'] if item['dimension'] == dimension)
            self.assertEqual(result['status'], 'insufficient_evidence')
            self.assertTrue(result['abstention_reason'])

    def test_invocations_record_metadata_and_raw_output(self):
        self.assertTrue(self.document['invocations'])
        for invocation in self.document['invocations']:
            self.assertEqual(invocation['provider'], 'mock')
            self.assertEqual(invocation['model'], 'mock-llm-v1')
            self.assertIsNotNone(invocation['latency_ms'])
            self.assertEqual(invocation['status'], 'success')
            self.assertTrue(invocation['invocation_id'].startswith('CALL-'))
            self.assertIsNotNone(invocation['raw_response'])
            self.assertEqual(len(invocation['raw_response_sha256']), 64)

    def test_suspected_layer_requires_log_verification(self):
        result = next(item for item in self.document['results']
                      if item['dimension'] == 'finding_candidate')
        self.assertEqual(result['status'], 'observed')
        self.assertTrue(result['requires_log_verification'])
        self.assertIsNotNone(result['attribution_confidence'])

    def test_evaluate_all_backward_compatible_entry_point(self):
        results, invocations = LLMJudge(MockLLMProvider()).evaluate_all(
            self.fused, self.turns, self.metrics, RUN_ID, self.timeline)
        self.assertEqual(len(results), len(self.document['results']))
        self.assertEqual(len(invocations), len(self.document['invocations']))

    def test_judge_pipeline_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, document in (('fused-segments.json', self.fused),
                                   ('turns.json', self.turns),
                                   ('metrics.json', self.metrics),
                                   ('timeline.json', self.timeline)):
                (root / name).write_text(json.dumps(document, ensure_ascii=False),
                                         encoding='utf-8')
            written = judge_pipeline(root / 'fused-segments.json', root / 'turns.json',
                                     root / 'metrics.json', root / 'out',
                                     MockLLMProvider(), timeline_path=root / 'timeline.json',
                                     run_id=RUN_ID)
            self.assertTrue((root / 'out' / 'judge-results.json').is_file())
            raw = json.loads((root / 'out' / 'judge-raw.json').read_text(encoding='utf-8'))
            self.assertEqual(raw['invocations'], written['invocations'])


class JudgeResultValidationTests(unittest.TestCase):
    """Schema-level rules for one JudgeResult document."""

    def base(self, **overrides):
        document = {
            'schema_version': '1.0.0', 'judge_id': 'JUDGE-test', 'run_id': 'RUN-t',
            'dimension': 'meaningful_response', 'turn_id': 'TURN-0001',
            'response_id': 'RESP-0001', 'decision': 'located', 'score': None,
            'confidence': 0.6, 'reason': 'r', 'requires_log_verification': False,
            'evidence_refs': [], 'event_refs': [], 'model': 'm', 'prompt_version': 'v',
            'provider': 'p', 'invocation_id': 'CALL-t', 'latency_ms': 1,
            'status': 'observed', 'meaningful_response_start_ms': 2000.0,
        }
        document.update(overrides)
        return document

    def test_valid_meaningful_response(self):
        self.assertEqual(judge_result_errors(self.base()), [])

    def test_missing_meaningful_start_rejected(self):
        document = self.base()
        del document['meaningful_response_start_ms']
        self.assertTrue(any('meaningful_response_start_ms' in error
                            for error in judge_result_errors(document)))

    def test_anchor_must_match_the_reported_millisecond(self):
        document = self.base(anchor_refs=[{'role': 'meaningful_start', 'anchor_id': 'EVT-1',
                                           'start_ms': 1500.0, 'end_ms': 1500.0,
                                           'source': 'acoustic_boundary'}])
        errors = judge_result_errors(document)
        self.assertTrue(any('must equal the selected anchor' in error for error in errors))
        document['meaningful_response_start_ms'] = 1500.0
        self.assertEqual(judge_result_errors(document), [])

    def test_duplicate_anchor_role_rejected(self):
        anchor = {'role': 'meaningful_start', 'anchor_id': 'EVT-1', 'start_ms': 2000.0,
                  'end_ms': 2000.0, 'source': 'acoustic_boundary'}
        document = self.base(anchor_refs=[anchor, dict(anchor, anchor_id='EVT-2')])
        self.assertTrue(any('at most once' in error for error in judge_result_errors(document)))

    def test_unknown_anchor_member_rejected_by_schema(self):
        document = self.base(anchor_refs=[{'role': 'meaningful_start', 'anchor_id': 'EVT-1',
                                           'start_ms': 2000.0, 'end_ms': 2000.0,
                                           'source': 'x', 'milliseconds': 1}])
        self.assertTrue(judge_result_errors(document))

    def test_suspected_layer_requires_verification(self):
        document = self.base(dimension='finding_candidate', turn_id=None, response_id=None,
                             decision='high_latency', meaningful_response_start_ms=None,
                             finding_severity='medium', suspected_layer='llm',
                             attribution_confidence=0.4,
                             requires_log_verification=False)
        self.assertTrue(any('requires_log_verification' in error
                            for error in judge_result_errors(document)))

    def test_semantic_verdict_requires_criterion_and_citation(self):
        document = self.base(dimension='semantic_response', meaningful_response_start_ms=None,
                             semantic_decision=True, criterion_id='CRIT-WRONG',
                             criterion_version='1.0.0',
                             judge_profile={'profile_id': 'JUDGE-x', 'provider': 'p',
                                            'model': 'm', 'prompt_version': 'v',
                                            'criteria_version': '1.0.0'})
        errors = judge_result_errors(document)
        self.assertTrue(any('must cite evidence' in error for error in errors))
        self.assertTrue(any('declared criterion' in error for error in errors))
        document['evidence_refs'] = ['EVD-DEVICE-START']
        document['criterion_id'] = 'CRIT-SEMANTIC-RESPONSE'
        self.assertEqual(judge_result_errors(document), [])

    def test_abstaining_semantic_verdict_must_state_why_and_carry_no_decision(self):
        document = self.base(dimension='semantic_response', meaningful_response_start_ms=None,
                             semantic_decision=True, criterion_id='CRIT-SEMANTIC-RESPONSE',
                             criterion_version='1.0.0',
                             judge_profile={'profile_id': 'JUDGE-x', 'provider': 'p',
                                            'model': 'm', 'prompt_version': 'v',
                                            'criteria_version': '1.0.0'},
                             status='insufficient_evidence')
        errors = judge_result_errors(document)
        self.assertTrue(any('must not carry a decision' in error for error in errors))
        self.assertTrue(any('must state why' in error for error in errors))

    def test_an_unavailable_provider_cannot_claim_an_observed_verdict(self):
        document = self.base(dimension='semantic_response', meaningful_response_start_ms=None,
                             semantic_decision=True, criterion_id='CRIT-SEMANTIC-RESPONSE',
                             criterion_version='1.0.0', evidence_refs=['EVD-DEVICE-START'],
                             judge_profile={'profile_id': 'JUDGE-unavailable',
                                            'provider': 'unavailable', 'model': 'none',
                                            'prompt_version': 'v',
                                            'criteria_version': '1.0.0'})
        errors = judge_result_errors(document)
        self.assertTrue(any('unavailable provider cannot produce an observed verdict' in error
                            for error in errors))


if __name__ == '__main__':
    unittest.main()