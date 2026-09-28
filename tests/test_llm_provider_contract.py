"""HTTP request contract for the OpenAI-compatible Judge provider."""

import json
import unittest
from unittest.mock import Mock, patch

from aivoicebench.llm import LLMJudge
from aivoicebench.llm_provider import OpenAICompatibleProvider
from aivoicebench.validation import judge_document_errors
from tests.judge_fixture import RUN_ID, dialogue_timeline


class ProviderRequestContractTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider(
            'aliyun', 'https://example.invalid/compatible-mode/v1',
            'qwen3.8-flash', api_key='test-only',
        )

    @patch('requests.post')
    def test_judge_requests_json_object_and_preserves_raw_response(self, post):
        raw = json.dumps({
            'decision': 'relevant', 'reason': 'The answer addresses the question',
            'status': 'observed', 'confidence': 0.8, 'evidence_refs': ['E1'],
            'semantic_decision': True,
        })
        post.return_value = Mock()
        post.return_value.json.return_value = {'choices': [{'message': {'content': raw}}]}

        result, invocation = self.provider.complete(
            'Return only a JSON object', 'Evidence E1', 'semantic_response',
            {'evidence_refs': ['E1']},
        )

        self.assertEqual(post.call_args.kwargs['json']['response_format'],
                         {'type': 'json_object'})
        self.assertEqual(result['decision'], 'relevant')
        self.assertEqual(invocation.status, 'success')
        self.assertEqual(invocation.raw_response, raw)

    @patch('requests.post')
    def test_voice_agent_raw_request_retains_its_independent_contract(self, post):
        raw = '{"action":"stop","text":"","reason":"done"}'
        post.return_value = Mock()
        post.return_value.json.return_value = {'choices': [{'message': {'content': raw}}]}

        result, invocation = self.provider.complete_raw(
            'Return only a JSON object for the voice agent', 'Stop now',
        )

        self.assertNotIn('response_format', post.call_args.kwargs['json'])
        self.assertEqual(result, raw)
        self.assertEqual(invocation.status, 'success')

    @patch('requests.post')
    def test_json_missing_envelope_fields_is_preserved_and_rejected(self, post):
        raw = '{"semantic_decision":true}'
        post.return_value = Mock()
        post.return_value.json.return_value = {'choices': [{'message': {'content': raw}}]}

        result, invocation = self.provider.complete(
            'Return only a JSON object', 'Evidence E1', 'semantic_response',
            {'evidence_refs': ['E1']},
        )

        self.assertEqual(post.call_count, 1)
        self.assertEqual(invocation.status, 'failed')
        self.assertEqual(invocation.failure_code, 'invalid_output')
        self.assertEqual(invocation.raw_response, raw)
        self.assertEqual(result['status'], 'insufficient_evidence')

    @patch('requests.post')
    def test_missing_finding_attribution_abstains_without_invalidating_artifact(self, post):
        timeline = dialogue_timeline()
        evidence_id = timeline['evidence'][0]['evidence_id']
        raw = json.dumps({
            'decision': 'possible_issue', 'reason': 'A cited event suggests a problem',
            'status': 'observed', 'confidence': 0.8,
            'evidence_refs': [evidence_id],
            'finding_severity': 'medium', 'suspected_layer': 'llm',
            # A named suspected layer cannot acquire invented confidence later.
        })
        post.return_value = Mock()
        post.return_value.json.return_value = {'choices': [{'message': {'content': raw}}]}

        judge = LLMJudge(self.provider)
        result = judge._judge('finding_candidate', None, None,
                              {'metrics': [], 'events': timeline['events']},
                              run_id=RUN_ID, timeline=timeline)
        document = judge.judge_document([result], run_id=RUN_ID)

        self.assertEqual(post.call_count, 1)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['suspected_layer'])
        self.assertEqual(len(document['abstentions']), 1)
        self.assertEqual(len(document['invocations']), 1)
        self.assertEqual(document['invocations'][0]['status'], 'failed')
        self.assertEqual(document['invocations'][0]['failure_code'], 'invalid_output')
        self.assertEqual(document['invocations'][0]['raw_response'], raw)
        self.assertEqual(judge_document_errors(document, timeline), [])

    @patch('requests.post')
    def test_observed_suspected_layer_needs_explicit_valid_attribution(self, post):
        base = {
            'decision': 'possible_issue', 'reason': 'Cites E1',
            'status': 'observed', 'confidence': 0.8,
            'evidence_refs': ['E1'], 'finding_severity': 'medium',
            'suspected_layer': 'llm', 'requires_log_verification': True,
        }
        variants = (
            {}, {'attribution_confidence': None},
            {'attribution_confidence': '0.4'},
            {'attribution_confidence': float('nan')},
            {'attribution_confidence': 0.4, 'requires_log_verification': False},
            {'attribution_confidence': 0.4, 'requires_log_verification': None},
        )
        for fields in variants:
            with self.subTest(fields=fields):
                raw = json.dumps(dict(base, **fields))
                post.return_value = Mock()
                post.return_value.json.return_value = {
                    'choices': [{'message': {'content': raw}}]}
                result, invocation = self.provider.complete(
                    'Return only JSON', 'Evidence E1', 'finding_candidate',
                    {'evidence_refs': ['E1']},
                )
                self.assertEqual(invocation.status, 'failed')
                self.assertEqual(invocation.failure_code, 'invalid_output')
                self.assertEqual(invocation.raw_response, raw)
                self.assertEqual(result['status'], 'insufficient_evidence')

    @patch('requests.post')
    def test_observed_dimension_fields_are_required_before_acceptance(self, post):
        base = {'decision': 'observed', 'reason': 'Cites E1', 'status': 'observed',
                'confidence': 0.8, 'evidence_refs': ['E1']}
        cases = (
            ('semantic_response', {'semantic_decision': None}),
            ('intent', {'intent_label': None}),
            ('feedback_detection', {'feedback_type': None}),
            ('feedback_detection', {'feedback_type': 'invented_category'}),
            ('feedback_detection', {'feedback_type': 'ack', 'anchor_refs': []}),
            ('meaningful_response', {'anchor_refs': []}),
            ('conversation_quality', {'score': None}),
            ('finding_candidate', {'finding_severity': 'medium',
                                   'suspected_layer': None}),
        )
        for dimension, extra in cases:
            with self.subTest(dimension=dimension):
                raw = json.dumps(dict(base, **extra))
                post.return_value = Mock()
                post.return_value.json.return_value = {
                    'choices': [{'message': {'content': raw}}]}
                result, invocation = self.provider.complete(
                    'Return only JSON', 'Evidence E1', dimension,
                    {'evidence_refs': ['E1']},
                )
                self.assertEqual(invocation.status, 'failed')
                self.assertEqual(invocation.failure_code, 'invalid_output')
                self.assertEqual(invocation.raw_response, raw)
                self.assertEqual(result['status'], 'insufficient_evidence')

    def test_observed_anchor_dimensions_accept_only_supplied_selections(self):
        context = {'evidence_refs': [], 'event_refs': [], 'anchors': [
            {'anchor_id': 'EV-START'}, {'anchor_id': 'EV-END'},
        ]}
        base = {'decision': 'detected', 'reason': 'Selected measured anchors',
                'status': 'observed', 'confidence': 0.8}
        cases = (
            ('meaningful_response', [
                {'role': 'meaningful_start', 'anchor_id': 'EV-START'},
            ], {}),
            ('feedback_detection', [
                {'role': 'feedback_start', 'anchor_id': 'EV-START'},
                {'role': 'feedback_end', 'anchor_id': 'EV-END'},
            ], {'feedback_type': 'ack'}),
        )
        for dimension, anchors, extra in cases:
            with self.subTest(dimension=dimension):
                result = self.provider._parse_response(
                    json.dumps(dict(base, anchor_refs=anchors, **extra)),
                    dimension, context,
                )
                self.assertEqual(result['status'], 'observed')
                self.assertEqual(result['anchor_refs'], anchors)


if __name__ == '__main__':
    unittest.main()
