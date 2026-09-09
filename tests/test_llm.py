"""Tests for the LLM Harness: provider, judge, structured output."""

import unittest

from aivoicebench.llm import (
    MockLLMProvider, LLMJudge, judge_pipeline,
    FILLER_PATTERNS, PROMPT_VERSIONS, SYSTEM_PROMPTS,
)
from aivoicebench.validation import judge_result_errors


def make_fused_doc(segments):
    """Build a minimal FusedSegments document."""
    return {
        'schema_version': '1.0.0', 'document_id': 'FUSED-test',
        'source': {'acoustic_document_id': 'ACOUSTIC-test', 'transcript_document_id': None,
                   'audio_sha256': 'a' * 64, 'duration_ms': 5000},
        'attribution': {'strategy': 'alternating_heuristic', 'provider': None,
                        'confidence': 0.5, 'note': 'test'},
        'status': 'complete', 'reason': None,
        'segments': [{'segment_id': f'FSEG-{i:04d}', 'start_ms': s[0], 'end_ms': s[1],
                       'speaker_role': s[2], 'speaker_confidence': 0.5,
                       'speaker_source': 'heuristic', 'timing_source': 'acoustic',
                       'text': s[3] if len(s) > 3 else None,
                       'acoustic_segment_id': f'SEG-{i:04d}', 'asr_segment_id': None}
                      for i, s in enumerate(segments)],
    }


def make_turns_doc(turns):
    """Build a minimal Turns document."""
    return {
        'schema_version': '1.0.0', 'document_id': 'TURNS-test',
        'fused_document_id': 'FUSED-test', 'status': 'complete', 'reason': None,
        'turns': turns,
    }


def make_turn(turn_id, tester_ids, device_ids, tester_start, tester_end,
              device_start, device_end, response_id='RESP-0001'):
    return {
        'turn_id': turn_id, 'tester_segment_ids': tester_ids,
        'device_segment_ids': device_ids, 'response_id': response_id,
        'start_ms': tester_start, 'end_ms': device_end,
        'tester_speech_start_ms': tester_start, 'tester_speech_end_ms': tester_end,
        'device_speech_start_ms': device_start, 'device_speech_end_ms': device_end,
        'has_interruption': False, 'has_overlap': False,
    }


class MockProviderTests(unittest.TestCase):
    def test_meaningful_response_strips_filler(self):
        provider = MockLLMProvider()
        context = {
            'text': '嗯……好的，让我看看。南京今天天气晴朗，气温25度。',
            'device_speech_start_ms': 1500,
            'device_speech_end_ms': 3500,
        }
        raw, inv = provider.complete('', '', 'meaningful_response', context)
        self.assertEqual(raw['status'], 'observed')
        self.assertIsNotNone(raw['meaningful_response_start_ms'])
        # Filler takes some proportion of the response
        self.assertGreater(raw['meaningful_response_start_ms'], 1500)
        self.assertLess(raw['meaningful_response_start_ms'], 3500)
        self.assertEqual(inv.provider, 'mock')
        self.assertEqual(inv.status, 'success')

    def test_meaningful_response_no_filler(self):
        provider = MockLLMProvider()
        context = {
            'text': '南京今天天气晴朗，气温25度。',
            'device_speech_start_ms': 1500,
            'device_speech_end_ms': 2500,
        }
        raw, inv = provider.complete('', '', 'meaningful_response', context)
        self.assertEqual(raw['status'], 'observed')
        self.assertAlmostEqual(raw['meaningful_response_start_ms'], 1500, places=1)

    def test_meaningful_response_all_filler(self):
        provider = MockLLMProvider()
        context = {
            'text': '嗯……好的',
            'device_speech_start_ms': 1500,
            'device_speech_end_ms': 2500,
        }
        raw, inv = provider.complete('', '', 'meaningful_response', context)
        self.assertEqual(raw['status'], 'insufficient_evidence')
        self.assertIsNone(raw['meaningful_response_start_ms'])

    def test_meaningful_response_no_text(self):
        provider = MockLLMProvider()
        context = {'text': None, 'device_speech_start_ms': 1500, 'device_speech_end_ms': 2500}
        raw, inv = provider.complete('', '', 'meaningful_response', context)
        self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_feedback_detection(self):
        provider = MockLLMProvider()
        context = {
            'text': '嗯……好的，让我看看。南京今天',
            'device_speech_start_ms': 1500,
            'device_speech_end_ms': 3500,
        }
        raw, inv = provider.complete('', '', 'feedback_detection', context)
        self.assertEqual(raw['status'], 'observed')
        self.assertIsNotNone(raw['feedback_type'])
        self.assertIsNotNone(raw['feedback_start_ms'])
        self.assertIsNotNone(raw['feedback_end_ms'])

    def test_feedback_no_filler(self):
        provider = MockLLMProvider()
        context = {'text': '南京今天', 'device_speech_start_ms': 1500, 'device_speech_end_ms': 2500}
        raw, inv = provider.complete('', '', 'feedback_detection', context)
        self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_intent_classification(self):
        provider = MockLLMProvider()
        raw, inv = provider.complete('', '', 'intent', {'tester_text': '今天天气怎么样？'})
        self.assertEqual(raw['status'], 'observed')
        self.assertEqual(raw['intent_label'], 'weather_query')

    def test_intent_no_text(self):
        provider = MockLLMProvider()
        raw, inv = provider.complete('', '', 'intent', {'tester_text': ''})
        self.assertEqual(raw['status'], 'insufficient_evidence')

    def test_conversation_quality(self):
        provider = MockLLMProvider()
        context = {'metrics': [{'name': 'first_speech_latency_ms', 'value': 500, 'status': 'observed'}],
                   'events': []}
        raw, inv = provider.complete('', '', 'conversation_quality', context)
        self.assertEqual(raw['status'], 'observed')
        self.assertIsNotNone(raw['score'])
        self.assertGreater(raw['score'], 0.7)

    def test_conversation_quality_high_latency(self):
        provider = MockLLMProvider()
        context = {'metrics': [{'name': 'first_speech_latency_ms', 'value': 3000, 'status': 'observed'}],
                   'events': []}
        raw, inv = provider.complete('', '', 'conversation_quality', context)
        self.assertLess(raw['score'], 0.7)

    def test_finding_candidate_latency(self):
        provider = MockLLMProvider()
        context = {'metrics': [{'name': 'first_speech_latency_ms', 'value': 3000, 'status': 'observed'}],
                   'events': []}
        raw, inv = provider.complete('', '', 'finding_candidate', context)
        self.assertEqual(raw['decision'], 'high_latency')
        self.assertEqual(raw['finding_severity'], 'medium')
        self.assertEqual(raw['suspected_layer'], 'llm')
        self.assertTrue(raw['requires_log_verification'])
        self.assertIsNotNone(raw['attribution_confidence'])

    def test_finding_candidate_false_endpoint(self):
        provider = MockLLMProvider()
        context = {'metrics': [], 'events': [{'type': 'possible_false_endpoint'}]}
        raw, inv = provider.complete('', '', 'finding_candidate', context)
        self.assertEqual(raw['decision'], 'false_endpoint_detected')
        self.assertEqual(raw['suspected_layer'], 'endpoint')

    def test_finding_candidate_none(self):
        provider = MockLLMProvider()
        context = {'metrics': [{'name': 'first_speech_latency_ms', 'value': 500, 'status': 'observed'}],
                   'events': []}
        raw, inv = provider.complete('', '', 'finding_candidate', context)
        self.assertEqual(raw['status'], 'insufficient_evidence')


class LLMJudgeTests(unittest.TestCase):
    def test_evaluate_all(self):
        fused = make_fused_doc([
            (500, 1000, 'tester', '今天天气怎么样？'),
            (1500, 3000, 'device', '嗯……好的，让我看看。南京今天天气晴朗。'),
        ])
        turns = make_turns_doc([
            make_turn('TURN-0001', ['FSEG-0000'], ['FSEG-0001'], 500, 1000, 1500, 3000)
        ])
        metrics = {'status': 'observed', 'metrics': [
            {'name': 'first_speech_latency_ms', 'value': 500, 'status': 'observed',
             'turn_id': 'TURN-0001', 'evidence_ids': [], 'event_ids': []}
        ]}
        judge = LLMJudge(MockLLMProvider())
        results, invocations = judge.evaluate_all(fused, turns, metrics)
        # 3 per-turn (intent, meaningful_response, feedback) + quality + finding = 5
        self.assertEqual(len(results), 5)
        self.assertTrue(len(invocations) > 0)
        # Check meaningful response result
        mr = [r for r in results if r['dimension'] == 'meaningful_response'][0]
        self.assertEqual(mr['status'], 'observed')
        self.assertIsNotNone(mr['meaningful_response_start_ms'])
        self.assertGreater(mr['meaningful_response_start_ms'], 1500)

    def test_judge_result_validates(self):
        fused = make_fused_doc([
            (500, 1000, 'tester', '今天天气怎么样？'),
            (1500, 3000, 'device', '嗯……好的，让我看看。南京今天天气晴朗。'),
        ])
        turns = make_turns_doc([
            make_turn('TURN-0001', ['FSEG-0000'], ['FSEG-0001'], 500, 1000, 1500, 3000)
        ])
        metrics = {'status': 'observed', 'metrics': [
            {'name': 'first_speech_latency_ms', 'value': 500, 'status': 'observed',
             'turn_id': 'TURN-0001', 'evidence_ids': [], 'event_ids': []}
        ]}
        judge = LLMJudge(MockLLMProvider())
        results, _ = judge.evaluate_all(fused, turns, metrics)
        for r in results:
            errors = judge_result_errors(r)
            self.assertEqual(errors, [], f'Validation errors for {r["dimension"]}: {errors}')

    def test_no_device_segments_is_insufficient(self):
        fused = make_fused_doc([(500, 1000, 'tester', '你好')])
        turns = make_turns_doc([
            make_turn('TURN-0001', ['FSEG-0000'], [], 500, 1000, None, None, None)
        ])
        metrics = {'status': 'insufficient_evidence', 'metrics': []}
        judge = LLMJudge(MockLLMProvider())
        results, _ = judge.evaluate_all(fused, turns, metrics)
        mr = [r for r in results if r['dimension'] == 'meaningful_response'][0]
        self.assertEqual(mr['status'], 'insufficient_evidence')

    def test_invocation_records_metadata(self):
        fused = make_fused_doc([
            (500, 1000, 'tester', '今天天气怎么样？'),
            (1500, 3000, 'device', '南京今天天气晴朗。'),
        ])
        turns = make_turns_doc([
            make_turn('TURN-0001', ['FSEG-0000'], ['FSEG-0001'], 500, 1000, 1500, 3000)
        ])
        metrics = {'status': 'observed', 'metrics': [
            {'name': 'first_speech_latency_ms', 'value': 500, 'status': 'observed',
             'turn_id': 'TURN-0001', 'evidence_ids': [], 'event_ids': []}
        ]}
        judge = LLMJudge(MockLLMProvider())
        _, invocations = judge.evaluate_all(fused, turns, metrics)
        for inv in invocations:
            self.assertEqual(inv['provider'], 'mock')
            self.assertEqual(inv['model'], 'mock-llm-v1')
            self.assertIsNotNone(inv['latency_ms'])
            self.assertEqual(inv['status'], 'success')
            self.assertTrue(inv['invocation_id'].startswith('CALL-'))

    def test_suspected_layer_requires_log_verification(self):
        """Finding candidates with suspected_layer must have requires_log_verification=True."""
        fused = make_fused_doc([
            (500, 1000, 'tester', '今天天气怎么样？'),
            (1500, 3000, 'device', '嗯……好的，让我看看。南京今天天气晴朗。'),
        ])
        turns = make_turns_doc([
            make_turn('TURN-0001', ['FSEG-0000'], ['FSEG-0001'], 500, 1000, 1500, 3000)
        ])
        metrics = {'status': 'observed', 'metrics': [
            {'name': 'first_speech_latency_ms', 'value': 3000, 'status': 'observed',
             'turn_id': 'TURN-0001', 'evidence_ids': [], 'event_ids': []}
        ]}
        judge = LLMJudge(MockLLMProvider())
        results, _ = judge.evaluate_all(fused, turns, metrics)
        fc = [r for r in results if r['dimension'] == 'finding_candidate'][0]
        if fc.get('suspected_layer'):
            self.assertTrue(fc['requires_log_verification'])
            self.assertIsNotNone(fc['attribution_confidence'])


class JudgeResultValidationTests(unittest.TestCase):
    def test_valid_meaningful_response(self):
        doc = {
            'schema_version': '1.0.0', 'judge_id': 'JUDGE-test', 'run_id': 'RUN-t',
            'dimension': 'meaningful_response', 'turn_id': 'TURN-0001', 'response_id': 'RESP-0001',
            'decision': 'meaningful_content_located', 'score': None, 'confidence': 0.6,
            'reason': 'Filler stripped', 'requires_log_verification': False,
            'evidence_refs': [], 'event_refs': [],
            'model': 'mock-llm-v1', 'prompt_version': 'meaningful-v1.0.0',
            'provider': 'mock', 'invocation_id': 'CALL-test', 'latency_ms': 5.0,
            'status': 'observed', 'meaningful_response_start_ms': 2000.0,
        }
        self.assertEqual(judge_result_errors(doc), [])

    def test_missing_meaningful_start_rejected(self):
        doc = {
            'schema_version': '1.0.0', 'judge_id': 'JUDGE-t', 'run_id': 'RUN-t',
            'dimension': 'meaningful_response', 'turn_id': 'T1', 'response_id': 'R1',
            'decision': 'located', 'score': None, 'confidence': 0.6, 'reason': 'r',
            'requires_log_verification': False, 'evidence_refs': [], 'event_refs': [],
            'model': 'm', 'prompt_version': 'v', 'provider': 'p',
            'invocation_id': 'c', 'latency_ms': 1, 'status': 'observed',
        }
        errors = judge_result_errors(doc)
        self.assertTrue(any('meaningful_response_start_ms' in e for e in errors))

    def test_suspected_layer_requires_verification(self):
        doc = {
            'schema_version': '1.0.0', 'judge_id': 'JUDGE-t', 'run_id': 'RUN-t',
            'dimension': 'finding_candidate', 'turn_id': None, 'response_id': None,
            'decision': 'high_latency', 'score': None, 'confidence': 0.6, 'reason': 'r',
            'requires_log_verification': False, 'evidence_refs': [], 'event_refs': [],
            'model': 'm', 'prompt_version': 'v', 'provider': 'p',
            'invocation_id': 'c', 'latency_ms': 1, 'status': 'observed',
            'finding_severity': 'medium', 'suspected_layer': 'llm',
            'attribution_confidence': 0.4,
        }
        errors = judge_result_errors(doc)
        self.assertTrue(any('requires_log_verification' in e for e in errors))


if __name__ == '__main__':
    unittest.main()
