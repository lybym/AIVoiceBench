"""Issue #10 acceptance tests for the structured Judge contract.

Covers the acceptance criteria that do not require a real provider:

* every accepted Judge result validates and references existing evidence;
* raw provider output and the validated result are both preserved with
  provider/model/prompt/version provenance;
* invented or unreferenced timestamps, missing evidence references, malformed
  output, unsupported role claims and deterministic-value fabrication are
  rejected;
* insufficient evidence abstains explicitly instead of forcing a verdict;
* no long-lived credential can reach the artifact.

The recorded fixtures are synthetic. Nothing here is a real recording, a real
model call or a hardware measurement.
"""

import copy
import json
import unittest
from unittest.mock import Mock, patch

from aivoicebench.llm import LLMJudge, MockLLMProvider, PROMPT_VERSIONS, SYSTEM_PROMPTS
from aivoicebench.llm_provider import OpenAICompatibleProvider
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.semantic_evidence import (
    CRITERIA_VERSION, SEMANTIC_CRITERIA, build_judge_profile, judge_profile_id,
    resolve_anchors, semantic_evidence_records, turn_anchors, turn_evidence,
)
from aivoicebench.validation import (
    credential_errors, judge_document_errors, judge_result_errors,
    semantic_evidence_errors, timeline_errors, turns_errors,
)
from tests.judge_fixture import (
    CASE_ID, RUN_ID, dialogue_timeline, fused_document, make_event, make_evidence,
    make_timeline, turns_document,
)


class ProviderOutputEnvelopeTests(unittest.TestCase):
    """The prompt and strict parser agree on the provider's minimum output."""

    def setUp(self):
        self.provider = OpenAICompatibleProvider(
            'fixture', 'https://example.invalid', 'fixture', api_key='fixture-only')
        self.context = {'evidence_refs': ['EVD-1'], 'event_refs': []}

    def test_every_judge_prompt_requests_the_same_json_envelope(self):
        self.assertEqual(set(SYSTEM_PROMPTS), set(PROMPT_VERSIONS))
        for dimension, prompt in SYSTEM_PROMPTS.items():
            with self.subTest(dimension=dimension):
                self.assertIn('valid JSON object', prompt)
                for field in ('decision', 'reason', 'status', 'confidence'):
                    self.assertIn('"' + field + '"', prompt)
                self.assertIn('insufficient_evidence', prompt)
                self.assertIn('evidence_refs', prompt)

    def test_observed_dimension_prompts_name_their_extra_contract(self):
        required = {
            'meaningful_response': ('meaningful_start', 'anchor_refs'),
            'feedback_detection': ('feedback_type', 'feedback_start', 'feedback_end'),
            'intent': ('intent_label',),
            'conversation_quality': ('score',),
            'finding_candidate': ('finding_severity', 'suspected_layer',
                                  'attribution_confidence', 'requires_log_verification'),
            'semantic_response': ('semantic_decision',),
            'barge_in_compliance': ('semantic_decision',),
        }
        for dimension, fields in required.items():
            with self.subTest(dimension=dimension):
                for field in fields:
                    self.assertIn(field, SYSTEM_PROMPTS[dimension])

    def test_complete_accepts_a_cited_structured_result(self):
        raw_text = ('{"decision":"semantic_decision","reason":"Supplied reply answers '
                    'the request","status":"observed","confidence":0.8,'
                    '"semantic_decision":true,"evidence_refs":["EVD-1"]}')
        self.provider._call_api = lambda *_args, **_kwargs: raw_text
        result, invocation = self.provider.complete(
            SYSTEM_PROMPTS['semantic_response'], '{}', 'semantic_response', self.context)
        self.assertEqual(invocation.status, 'success')
        self.assertEqual(invocation.raw_response, raw_text)
        self.assertIs(result['semantic_decision'], True)
        self.assertEqual(result['confidence'], 0.8)

    def test_missing_envelope_field_and_non_json_fail_closed_with_raw_retained(self):
        valid = {'decision': 'semantic_decision', 'reason': 'Cited reply',
                 'status': 'observed', 'confidence': 0.8,
                 'semantic_decision': True, 'evidence_refs': ['EVD-1']}
        samples = [('non_json', 'The reply is relevant.')]
        samples.extend((f'missing_{field}', json.dumps(
            {key: value for key, value in valid.items() if key != field}))
            for field in ('decision', 'reason', 'status', 'confidence'))
        for name, raw_text in samples:
            with self.subTest(name=name):
                self.provider._call_api = lambda *_args, **_kwargs: raw_text
                result, invocation = self.provider.complete(
                    SYSTEM_PROMPTS['semantic_response'], '{}', 'semantic_response',
                    self.context)
                self.assertEqual(invocation.status, 'failed')
                self.assertEqual(invocation.failure_code, 'invalid_output')
                self.assertEqual(invocation.raw_response, raw_text)
                self.assertEqual(result['status'], 'insufficient_evidence')
                self.assertEqual(result['decision'], 'unknown')

    def test_empty_envelope_text_and_out_of_range_numbers_still_fail_closed(self):
        base = {'decision': 'cited', 'reason': 'Synthetic citation',
                'status': 'observed', 'confidence': 0.8, 'evidence_refs': ['EVD-1']}
        cases = (
            ('semantic_response', {'decision': ''}),
            ('semantic_response', {'reason': '  '}),
            ('semantic_response', {'status': ''}),
            ('semantic_response', {'confidence': -0.1}),
            ('semantic_response', {'confidence': 1.1}),
            ('conversation_quality', {'score': -0.1}),
            ('conversation_quality', {'score': 1.1}),
        )
        for dimension, changes in cases:
            with self.subTest(dimension=dimension, changes=changes):
                response = dict(base, **changes)
                if dimension == 'semantic_response':
                    response['semantic_decision'] = True
                raw = json.dumps(response)
                self.provider._call_api = lambda *_args, **_kwargs: raw
                result, invocation = self.provider.complete(
                    SYSTEM_PROMPTS[dimension], '{}', dimension, self.context)
                self.assertEqual(invocation.status, 'failed')
                self.assertEqual(invocation.failure_code, 'invalid_output')
                self.assertEqual(invocation.raw_response, raw)
                self.assertEqual(result['status'], 'insufficient_evidence')


class JudgeResponseFormatRequestTests(unittest.TestCase):
    """Inspect the actual Chat Completions request sent for each Judge dimension."""

    @staticmethod
    def request_format(dimension, *, model='qwen3.8-flash',
                       endpoint='https://dashscope.aliyuncs.com/compatible-mode/v1'):
        provider = OpenAICompatibleProvider(
            'aliyun', endpoint, model, api_key='fixture-only')
        raw = json.dumps({'decision': 'unknown', 'reason': 'Synthetic evidence is insufficient',
                          'status': 'insufficient_evidence', 'confidence': 0.1})
        with patch('requests.post') as post:
            post.return_value = Mock()
            post.return_value.json.return_value = {
                'choices': [{'message': {'content': raw}}]}
            result, invocation = provider.complete(
                SYSTEM_PROMPTS[dimension], '{}', dimension, {})
            assert invocation.status == 'success', result
            assert post.call_count == 1
            return post.call_args.kwargs['json']['response_format']

    def test_verified_bailian_model_sends_strict_dimension_schema(self):
        envelope = {'decision', 'reason', 'status', 'confidence',
                    'evidence_refs', 'event_refs'}
        dimension_fields = {
            'meaningful_response': {'anchor_refs'},
            'feedback_detection': {'anchor_refs', 'feedback_type'},
            'intent': {'intent_label'},
            'conversation_quality': {'score'},
            'finding_candidate': {'finding_severity', 'suspected_layer',
                                  'attribution_confidence', 'requires_log_verification'},
            'semantic_response': {'semantic_decision'},
            'barge_in_compliance': {'semantic_decision'},
        }
        for dimension, extra in dimension_fields.items():
            with self.subTest(dimension=dimension):
                response_format = self.request_format(dimension)
                self.assertEqual(response_format['type'], 'json_schema')
                self.assertTrue(response_format['json_schema']['strict'])
                schema = response_format['json_schema']['schema']
                self.assertEqual(schema['type'], 'object')
                self.assertIs(schema['additionalProperties'], False)
                self.assertEqual(set(schema['properties']), envelope | extra)
                optional = ({'semantic_decision'} if dimension in (
                    'semantic_response', 'barge_in_compliance') else set())
                self.assertEqual(set(schema['required']), (envelope | extra) - optional)
                self.assertEqual(schema['properties']['status']['enum'],
                                 ['observed', 'low_confidence', 'insufficient_evidence'])
                self.assertEqual(schema['properties']['confidence'], {'type': 'number'})
        self.assertEqual(self.request_format(
            'intent', model='qwen3.8-flash-2026-09-01')['type'], 'json_schema')

    def test_anchor_schema_allows_only_role_and_supplied_id_fields(self):
        for dimension, roles in (
                ('meaningful_response', {'meaningful_start'}),
                ('feedback_detection', {'feedback_start', 'feedback_end'})):
            with self.subTest(dimension=dimension):
                schema = self.request_format(dimension)['json_schema']['schema']
                selection = schema['properties']['anchor_refs']
                self.assertEqual(selection['type'], 'array')
                self.assertEqual(selection['items']['type'], 'object')
                self.assertIs(selection['items']['additionalProperties'], False)
                self.assertEqual(set(selection['items']['properties']),
                                 {'role', 'anchor_id'})
                self.assertEqual(set(selection['items']['required']),
                                 {'role', 'anchor_id'})
                self.assertEqual(set(selection['items']['properties']['role']['enum']),
                                 roles)
        for dimension in ('intent', 'conversation_quality', 'finding_candidate',
                          'semantic_response', 'barge_in_compliance'):
            with self.subTest(dimension=dimension):
                schema = self.request_format(dimension)['json_schema']['schema']
                self.assertNotIn('anchor_refs', schema['properties'])

    def test_observed_dimension_fields_have_constrained_schema_types(self):
        schemas = {dimension: self.request_format(dimension)['json_schema']['schema']
                   for dimension in ('intent', 'conversation_quality',
                                     'feedback_detection', 'finding_candidate',
                                     'semantic_response')}
        self.assertEqual(schemas['intent']['properties']['intent_label']['type'],
                         ['string', 'null'])
        score = schemas['conversation_quality']['properties']['score']
        self.assertEqual(score, {'type': ['number', 'null']})
        self.assertIn('ack', schemas['feedback_detection']['properties']['feedback_type']['enum'])
        finding = schemas['finding_candidate']['properties']
        self.assertIn('high', finding['finding_severity']['enum'])
        self.assertIn('llm', finding['suspected_layer']['enum'])
        self.assertEqual(finding['attribution_confidence']['type'], ['number', 'null'])
        self.assertEqual(finding['requires_log_verification']['type'], 'boolean')
        semantic = schemas['semantic_response']
        self.assertEqual(semantic['properties']['semantic_decision']['type'], 'boolean')
        self.assertNotIn('semantic_decision', semantic['required'])
        self.assertIs(semantic['additionalProperties'], False)

    def test_other_compatible_models_keep_json_object_request_mode(self):
        cases = (
            ('qwen-plus', 'https://dashscope.aliyuncs.com/compatible-mode/v1'),
            ('qwen3.8-flash', 'https://example.invalid/compatible-mode/v1'),
        )
        for model, endpoint in cases:
            with self.subTest(model=model, endpoint=endpoint):
                self.assertEqual(self.request_format(
                    'semantic_response', model=model, endpoint=endpoint),
                    {'type': 'json_object'})


class SyntheticProviderJudgePathTests(unittest.TestCase):
    """Provider-shaped JSON must survive parsing, Judge grounding and artifact validation."""

    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.turn = self.turns['turns'][0]
        self.provider = OpenAICompatibleProvider(
            'fixture', 'https://example.invalid', 'fixture', api_key='fixture-only')

    def judge_raw(self, dimension, raw_text, *, role=None):
        self.provider._call_api = lambda *_args, **_kwargs: raw_text
        judge = LLMJudge(self.provider)
        turn = self.turn if role else None
        result = judge._judge(
            dimension, turn, self.turn.get('response_id') if turn else None,
            {'text': 'synthetic response', 'tester_text': 'synthetic request',
             'metrics': [], 'events': self.timeline['events']},
            run_id=RUN_ID, timeline=self.timeline, role=role,
        )
        document = judge.judge_document([result], run_id=RUN_ID)
        self.assertEqual(judge_document_errors(document, self.timeline, self.turns), [])
        return result, document['invocations'][0]

    def test_role_and_id_anchor_selection_validates_through_full_judge_path(self):
        device_anchor = next(anchor for anchor in turn_anchors(
            self.turn, self.timeline, role='device') if anchor['usable'])
        raw = json.dumps({
            'decision': 'meaningful_content_located',
            'reason': 'The supplied response starts with content at this boundary',
            'status': 'observed', 'confidence': 0.8,
            'anchor_refs': [{'role': 'meaningful_start',
                             'anchor_id': device_anchor['anchor_id']}],
        })

        result, invocation = self.judge_raw('meaningful_response', raw, role='device')

        self.assertEqual(invocation['status'], 'success')
        self.assertEqual(invocation['raw_response'], raw)
        self.assertEqual(result['status'], 'observed')
        self.assertEqual(result['meaningful_response_start_ms'], device_anchor['start_ms'])
        self.assertEqual(result['anchor_refs'][0]['anchor_id'], device_anchor['anchor_id'])
        self.assertTrue(result['evidence_refs'])

    def test_bad_anchor_shapes_fail_closed_and_preserve_raw_text(self):
        device_anchor = next(anchor for anchor in turn_anchors(
            self.turn, self.timeline, role='device') if anchor['usable'])
        base = {'decision': 'meaningful_content_located', 'reason': 'Selected boundary',
                'status': 'observed', 'confidence': 0.8}
        selections = (
            [device_anchor['anchor_id']],
            [{'role': 'meaningful_start', 'anchor_id': device_anchor['anchor_id'],
              'start_ms': device_anchor['start_ms'],
              'end_ms': device_anchor['end_ms']}],
            [{'role': 'meaningful_start', 'anchor_id': device_anchor['anchor_id'],
              'source': device_anchor['source']}],
        )
        for selection in selections:
            with self.subTest(selection=selection):
                raw = json.dumps(dict(base, anchor_refs=selection))
                result, invocation = self.judge_raw(
                    'meaningful_response', raw, role='device')
                self.assertEqual(invocation['status'], 'failed')
                self.assertEqual(invocation['failure_code'], 'invalid_output')
                self.assertEqual(invocation['raw_response'], raw)
                self.assertEqual(result['status'], 'insufficient_evidence')
                self.assertEqual(result['decision'], 'unknown')

    def test_observed_intent_and_quality_require_their_result_fields(self):
        cases = (
            ('intent', 'tester', {'decision': 'classified', 'event_refs': [
                next(anchor['anchor_id'] for anchor in turn_anchors(
                    self.turn, self.timeline, role='tester') if anchor['usable'])]}),
            ('conversation_quality', None, {'decision': 'acceptable', 'event_refs': [
                self.timeline['events'][0]['event_id']]}),
        )
        for dimension, role, extras in cases:
            with self.subTest(dimension=dimension):
                raw = json.dumps(dict(
                    {'reason': 'A cited synthetic event supports this judgment',
                     'status': 'observed', 'confidence': 0.8}, **extras))
                result, invocation = self.judge_raw(dimension, raw, role=role)
                self.assertEqual(invocation['status'], 'failed')
                self.assertEqual(invocation['failure_code'], 'invalid_output')
                self.assertEqual(invocation['raw_response'], raw)
                self.assertEqual(result['status'], 'insufficient_evidence')

    def test_cited_low_confidence_semantic_result_without_boolean_abstains(self):
        evidence_id = self.timeline['evidence'][0]['evidence_id']
        raw = json.dumps({
            'decision': 'uncertain',
            'reason': 'The supplied transcript supports only an uncertain interpretation',
            'status': 'low_confidence', 'confidence': 0.3,
            'evidence_refs': [evidence_id],
        })

        result, invocation = self.judge_raw('semantic_response', raw)

        self.assertEqual(invocation['status'], 'success')
        self.assertEqual(invocation['raw_response'], raw)
        self.assertEqual(result['status'], 'low_confidence')
        self.assertIsNone(result['semantic_decision'])
        self.assertTrue(result['abstention_reason'])

    @patch('requests.post')
    def test_json_object_semantic_verdict_cannot_substitute_anchor_for_citation(self, post):
        anchor = next(item for item in turn_anchors(self.turn, self.timeline)
                      if item['usable'])
        evidence_id = self.timeline['evidence'][0]['evidence_id']
        base = {'decision': 'semantic_decision', 'reason': 'Synthetic cited judgment',
                'status': 'observed', 'confidence': 0.8, 'semantic_decision': True}
        cases = (
            (dict(base, anchor_refs=[{'role': 'meaningful_start',
                                      'anchor_id': anchor['anchor_id']}]), False),
            (dict(base, evidence_refs=[evidence_id]), True),
        )
        for response, accepted in cases:
            with self.subTest(accepted=accepted):
                raw = json.dumps(response)
                post.return_value = Mock()
                post.return_value.json.return_value = {
                    'choices': [{'message': {'content': raw}}]}
                judge = LLMJudge(self.provider)
                result = judge._judge(
                    'semantic_response', self.turn, self.turn.get('response_id'),
                    {'tester_text': 'synthetic request',
                     'device_text': 'synthetic response'},
                    run_id=RUN_ID, timeline=self.timeline)
                document = judge.judge_document([result], run_id=RUN_ID)
                self.assertEqual(post.call_args.kwargs['json']['response_format'],
                                 {'type': 'json_object'})
                invocation = document['invocations'][0]
                self.assertEqual(invocation['raw_response'], raw)
                self.assertEqual(judge_document_errors(
                    document, self.timeline, self.turns), [])
                if accepted:
                    self.assertEqual(invocation['status'], 'success')
                    self.assertEqual(result['status'], 'observed')
                    self.assertIs(result['semantic_decision'], True)
                    self.assertIn(evidence_id, result['evidence_refs'])
                else:
                    self.assertEqual(invocation['status'], 'failed')
                    self.assertEqual(invocation['failure_code'], 'invalid_output')
                    self.assertEqual(result['status'], 'insufficient_evidence')
                    self.assertIsNone(result['semantic_decision'])
                    self.assertEqual(result['decision'], 'unknown')


class ObservedDimensionDocumentTests(unittest.TestCase):
    """A full Judge artifact enforces dimension fields after provider parsing."""

    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.fused = fused_document()
        metrics = compute_timeline_metrics(self.timeline)
        self.document = LLMJudge(MockLLMProvider()).evaluate(
            self.fused, self.turns, metrics, self.timeline, run_id=RUN_ID)

    def test_valid_observed_dimensions_pass_document_validation(self):
        self.assertEqual(judge_document_errors(self.document, self.timeline, self.turns), [])
        observed = {item['dimension'] for item in self.document['results']
                    if item['status'] == 'observed'}
        self.assertTrue({'meaningful_response', 'feedback_detection', 'intent',
                         'finding_candidate', 'semantic_response'} <= observed)

    def test_missing_observed_fields_invalidate_full_document(self):
        cases = (
            ('meaningful_response', 'meaningful_response_start_ms'),
            ('feedback_detection', 'feedback_type'),
            ('feedback_detection', 'feedback_start_ms'),
            ('feedback_detection', 'feedback_end_ms'),
            ('intent', 'intent_label'),
            ('finding_candidate', 'finding_severity'),
            ('finding_candidate', 'suspected_layer'),
            ('finding_candidate', 'attribution_confidence'),
            ('finding_candidate', 'requires_log_verification'),
            ('semantic_response', 'semantic_decision'),
        )
        for dimension, field in cases:
            with self.subTest(dimension=dimension, field=field):
                document = copy.deepcopy(self.document)
                result = next(item for item in document['results']
                              if item['dimension'] == dimension)
                result[field] = None
                errors = judge_document_errors(document, self.timeline, self.turns)
                self.assertTrue(errors, f'{dimension}.{field} unexpectedly accepted')
                self.assertTrue(any(field in error for error in errors), errors)


class FixtureIntegrity(unittest.TestCase):
    """The fixture must itself be canonical, or the tests prove nothing."""

    def test_timeline_and_turns_are_valid(self):
        timeline = dialogue_timeline()
        self.assertEqual(timeline_errors(timeline), [])
        self.assertEqual(turns_errors(turns_document()), [])

    def test_anchors_come_from_measured_events_only(self):
        timeline = dialogue_timeline()
        anchors = turn_anchors(turns_document()['turns'][0], timeline)
        self.assertEqual([anchor['anchor_id'] for anchor in anchors],
                         ['EVT-TESTER-START', 'EVT-TESTER-END', 'EVT-DEVICE-START',
                          'EVT-DEVICE-END'])

    def test_derived_boundaries_are_not_anchors(self):
        evidence = [make_evidence('EVD-DERIVED', 1200)]
        events = [make_event('EVT-DERIVED', 'response_start', 1200, ['EVD-DERIVED'],
                             source='derived')]
        # A derived interval is unclosed, so the timeline is `partial` with a gap
        # rather than a complete observation.
        timeline = make_timeline(events, evidence, status='partial',
                                 gaps=[{'reason': 'fixture: derived interval unclosed',
                                        'required_evidence': 'acoustic_boundary',
                                        'start_ms': 1200, 'end_ms': 30000}])
        self.assertEqual(timeline_errors(timeline), [])
        anchors = turn_anchors({'turn_id': 'TURN-0001'}, timeline)
        self.assertFalse(anchors[0]['usable'])
        resolved, problems = resolve_anchors(
            [{'role': 'meaningful_start', 'anchor_id': 'EVT-DERIVED'}], anchors)
        self.assertEqual(resolved, [])
        self.assertTrue(any('no measured evidence' in problem for problem in problems))


class JudgeRunTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.fused = fused_document()
        self.metrics = compute_timeline_metrics(self.timeline)
        self.judge = LLMJudge(MockLLMProvider())
        self.document = self.judge.evaluate(self.fused, self.turns, self.metrics, self.timeline,
                                          run_id=RUN_ID)

    def result(self, dimension):
        return next(item for item in self.document['results']
                    if item['dimension'] == dimension)

    def test_artifact_and_every_result_validate(self):
        self.assertEqual(judge_document_errors(self.document, self.timeline, self.turns), [])
        for result in self.document['results']:
            self.assertEqual(judge_result_errors(result), [],
                             f'{result["dimension"]}: {judge_result_errors(result)}')

    def test_raw_output_and_provenance_are_preserved_side_by_side(self):
        self.assertTrue(self.document['invocations'])
        for invocation in self.document['invocations']:
            self.assertEqual(invocation['status'], 'success')
            self.assertIsNotNone(invocation['raw_response'])
            self.assertEqual(len(invocation['raw_response_sha256']), 64)
            self.assertEqual(invocation['provider'], 'mock')
            self.assertEqual(invocation['model'], 'mock-llm-v1')
            self.assertTrue(invocation['prompt_version'])
        profile = self.document['judge_profile']
        self.assertEqual(profile['criteria_version'], CRITERIA_VERSION)
        self.assertTrue(profile['prompt_versions'])

    def test_both_canonical_semantic_dimensions_are_judged(self):
        judged = {result['dimension'] for result in self.document['results']}
        # barge_in_compliance is only judged when the turn has an interruption; a
        # missing judgment must never be readable as compliance.
        self.assertEqual(judged & set(SEMANTIC_CRITERIA), {'semantic_response'})
        for dimension in SEMANTIC_CRITERIA:
            if dimension not in judged:
                continue
            result = self.result(dimension)
            self.assertEqual(result['criterion_id'], SEMANTIC_CRITERIA[dimension]['criterion_id'])
            self.assertEqual(result['criterion_version'],
                             SEMANTIC_CRITERIA[dimension]['criterion_version'])
            self.assertEqual(result['judge_profile']['profile_id'],
                             judge_profile_id('mock', 'mock-llm-v1', CRITERIA_VERSION))
            self.assertIn(result['status'], ('observed', 'insufficient_evidence'))
            if result['status'] == 'observed':
                self.assertIsInstance(result['semantic_decision'], bool)
                self.assertTrue(result['evidence_refs'])
            else:
                self.assertIsNone(result['semantic_decision'])
                self.assertTrue(result['abstention_reason'])

    def test_timing_comes_from_a_selected_anchor_not_from_an_estimate(self):
        meaningful = self.result('meaningful_response')
        self.assertEqual(meaningful['status'], 'observed')
        anchor_ids = {anchor['anchor_id'] for anchor in
                      turn_anchors(self.turns['turns'][0], self.timeline)}
        self.assertEqual(len(meaningful['anchor_refs']), 1)
        selected = meaningful['anchor_refs'][0]
        self.assertIn(selected['anchor_id'], anchor_ids)
        self.assertEqual(meaningful['meaningful_response_start_ms'], selected['start_ms'])
        self.assertIn(selected['anchor_id'], meaningful['event_refs'])
        self.assertTrue(meaningful['evidence_refs'])

    def test_feedback_interval_uses_two_distinct_measured_boundaries(self):
        feedback = self.result('feedback_detection')
        self.assertEqual(feedback['status'], 'observed')
        roles = {anchor['role'] for anchor in feedback['anchor_refs']}
        self.assertEqual(roles, {'feedback_start', 'feedback_end'})
        self.assertGreater(feedback['feedback_end_ms'], feedback['feedback_start_ms'])
        self.assertEqual(feedback['feedback_start_ms'],
                         next(a['start_ms'] for a in feedback['anchor_refs']
                              if a['role'] == 'feedback_start'))

    def test_conversation_quality_and_finding_candidate_are_run_level(self):
        self.assertIsNone(self.result('conversation_quality')['turn_id'])
        self.assertTrue(self.result('finding_candidate')['evidence_refs'])

    def test_barge_in_compliance_is_only_judged_when_an_interruption_exists(self):
        self.assertEqual([item for item in self.document['results']
                          if item['dimension'] == 'barge_in_compliance'], [])
        timeline = dialogue_timeline(interruption=True)
        turns = turns_document(interruption=True)
        judge = LLMJudge(MockLLMProvider())
        document = judge.evaluate(fused_document(interruption=True), turns,
                                  compute_timeline_metrics(timeline), timeline, run_id=RUN_ID)
        result = next(item for item in document['results']
                      if item['dimension'] == 'barge_in_compliance')
        self.assertEqual(judge_result_errors(result), [])
        self.assertEqual(result['turn_id'], 'TURN-0001')


class JudgeRejectionTests(unittest.TestCase):
    """Each rejection path must abstain explicitly — never silently pass."""

    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.fused = fused_document()
        self.metrics = compute_timeline_metrics(self.timeline)

    def document_with(self, provider):
        judge = LLMJudge(provider)
        return judge.evaluate(self.fused, self.turns, self.metrics, self.timeline, run_id=RUN_ID)

    def test_model_authored_milliseconds_never_become_a_result(self):
        class TimestampAuthoring(MockLLMProvider):
            def complete(self, system_prompt, user_prompt, dimension, context):
                raw, invocation = super().complete(system_prompt, user_prompt, dimension, context)
                if dimension == 'meaningful_response':
                    raw = dict(raw, meaningful_response_start_ms=2345.0)
                return raw, invocation

        document = self.document_with(TimestampAuthoring())
        result = next(item for item in document['results']
                      if item['dimension'] == 'meaningful_response')
        self.assertEqual(result['status'], 'observed')
        self.assertNotEqual(result['meaningful_response_start_ms'], 2345.0)
        self.assertEqual(judge_document_errors(document, self.timeline, self.turns), [])

    def test_anchor_for_an_unknown_boundary_is_rejected(self):
        class InventedAnchor(MockLLMProvider):
            def complete(self, system_prompt, user_prompt, dimension, context):
                raw, invocation = super().complete(system_prompt, user_prompt, dimension, context)
                if dimension == 'meaningful_response':
                    raw = dict(raw, anchor_refs=[{'role': 'meaningful_start',
                                                  'anchor_id': 'EVT-INVENTED'}],
                               event_refs=['EVT-INVENTED'])
                return raw, invocation

        document = self.document_with(InventedAnchor())
        result = next(item for item in document['results']
                      if item['dimension'] == 'meaningful_response')
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['meaningful_response_start_ms'])
        self.assertIn('EVT-INVENTED', ' '.join(item['reason'] for item in document['abstentions']))

    def test_citation_of_another_turn_is_refused(self):
        timeline = dialogue_timeline()
        other_evidence = make_evidence('EVD-OTHER', 8000)
        timeline['evidence'].append(other_evidence)
        timeline['events'].append(make_event('EVT-OTHER', 'tester_speech_start', 8000,
                                             ['EVD-OTHER'], turn_id='TURN-0002'))
        timeline['events'].sort(key=lambda event: event['start_ms'])
        timeline['status'] = 'partial'
        timeline['gaps'] = [{'reason': 'fixture: a second turn is out of scope here',
                             'required_evidence': 'speaker_diarization_or_human_review',
                             'start_ms': 8000, 'end_ms': 30000}]
        self.assertEqual(timeline_errors(timeline), [])

        class CrossTurnCitation(MockLLMProvider):
            def complete(self, system_prompt, user_prompt, dimension, context):
                raw, invocation = super().complete(system_prompt, user_prompt, dimension, context)
                if dimension == 'semantic_response':
                    raw = dict(raw, evidence_refs=['EVD-OTHER'], event_refs=['EVT-OTHER'])
                return raw, invocation

        judge = LLMJudge(CrossTurnCitation())
        document = judge.evaluate(self.fused, self.turns, self.metrics, timeline, run_id=RUN_ID)
        self.assertEqual(judge_document_errors(document, timeline, self.turns), [])
        result = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        # The harness drops a foreign citation rather than publishing it, so the
        # judgment abstains instead of borrowing another turn's evidence.
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['evidence_refs'], [])

    def test_a_failing_invocation_cannot_produce_an_observed_result(self):
        class FailingProvider(MockLLMProvider):
            def complete(self, system_prompt, user_prompt, dimension, context):
                raw, invocation = super().complete(system_prompt, user_prompt, dimension, context)
                invocation.status = 'failed'
                return raw, invocation

        document = self.document_with(FailingProvider())
        self.assertTrue(all(result['status'] != 'observed' for result in document['results']))
        self.assertEqual(judge_document_errors(document, self.timeline, self.turns), [])

    def test_no_timeline_means_no_semantic_verdict(self):
        judge = LLMJudge(MockLLMProvider())
        document = judge.evaluate(self.fused, self.turns, self.metrics, None, run_id=RUN_ID)
        result = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertIsNone(result['semantic_decision'])
        self.assertTrue(document['abstentions'])

    def test_unavailable_provider_abstains_and_is_not_an_observed_verdict(self):
        judge = LLMJudge()
        document = judge.evaluate(self.fused, self.turns, self.metrics, self.timeline,
                                  run_id=RUN_ID)
        self.assertTrue(all(result['status'] != 'observed' for result in document['results']))
        result = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        self.assertEqual(result['abstention_reason'], 'No semantic provider configured')

    def test_suspected_layer_always_requires_device_logs(self):
        class CertainCause(MockLLMProvider):
            def complete(self, system_prompt, user_prompt, dimension, context):
                raw, invocation = super().complete(system_prompt, user_prompt, dimension, context)
                if dimension == 'finding_candidate' and raw.get('suspected_layer'):
                    raw = dict(raw, requires_log_verification=False,
                               attribution_confidence=1.0)
                return raw, invocation

        document = self.document_with(CertainCause())
        result = next(item for item in document['results']
                      if item['dimension'] == 'finding_candidate')
        self.assertTrue(result['requires_log_verification'])
        self.assertLess(result['attribution_confidence'], 1.0)
        self.assertEqual(judge_document_errors(document, self.timeline, self.turns), [])


class SemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.fused = fused_document()
        self.metrics = compute_timeline_metrics(self.timeline)
        self.judge = LLMJudge(MockLLMProvider())
        self.document = self.judge.evaluate(self.fused, self.turns, self.metrics, self.timeline,
                                            run_id=RUN_ID)
        self.records, self.abstentions = semantic_evidence_records(
            self.document, self.timeline, self.turns)

    def test_records_are_eligible_and_validate(self):
        self.assertTrue(self.records)
        self.assertEqual(semantic_evidence_errors(self.records, self.timeline, self.turns), [])
        for record in self.records:
            self.assertIsInstance(record['decision'], bool)
            self.assertTrue(record['judge_profile'])
            self.assertTrue(record['evidence_ids'] or record['event_ids'])
            self.assertTrue(record['criterion_id'] and record['criterion_version'])

    def test_records_drive_prd_m003_and_m006(self):
        result = compute_timeline_metrics(self.timeline, semantic_evidence=self.records)
        semantic = [metric for metric in result['metrics'] if metric['name'] == 'semantic_response']
        self.assertTrue(semantic)
        self.assertEqual(semantic[0]['status'], 'observed')
        self.assertEqual(semantic[0]['prd_ref'], 'PRD-M003')
        self.assertEqual(semantic[0]['method'], 'llm_judge')
        self.assertEqual(semantic[0]['confidence_source'], 'semantic_event')
        self.assertEqual(semantic[0]['judge_profile'],
                         judge_profile_id('mock', 'mock-llm-v1', CRITERIA_VERSION))
        self.assertEqual(semantic[0]['evidence_ids'],
                         list(self.records[0]['evidence_ids']))

    def test_barge_in_compliance_reaches_prd_m006_end_to_end(self):
        """PRD-M006 is driven from an interruption event to an observed value."""
        timeline = dialogue_timeline(interruption=True)
        turns = turns_document(interruption=True)
        metrics_result = compute_timeline_metrics(timeline)
        judge = LLMJudge(MockLLMProvider())
        document = judge.evaluate(fused_document(interruption=True), turns, metrics_result,
                                  timeline, run_id=RUN_ID)
        self.assertIn('barge_in_compliance',
                      {result['dimension'] for result in document['results']})
        self.assertEqual(judge_document_errors(document, timeline, turns), [])

        records, abstentions = semantic_evidence_records(document, timeline, turns)
        compliance = [record for record in records
                      if record['kind'] == 'barge_in_compliance']
        self.assertTrue(compliance, f'no M006 record; abstentions: {abstentions}')
        self.assertIsInstance(compliance[0]['decision'], bool)
        self.assertEqual(compliance[0]['criterion_id'], 'CRIT-BARGE-IN-COMPLIANCE')
        self.assertEqual(semantic_evidence_errors(records, timeline, turns), [])

        result = compute_timeline_metrics(timeline, semantic_evidence=records)
        metric = next(item for item in result['metrics']
                      if item['name'] == 'barge_in_semantic_compliance')
        self.assertEqual(metric['status'], 'observed')
        self.assertEqual(metric['prd_ref'], 'PRD-M006')
        self.assertIsInstance(metric['value'], bool)
        self.assertEqual(metric['method'], 'llm_judge')
        self.assertTrue(metric['event_ids'])

    def test_compliance_is_not_requested_without_an_interruption_event(self):
        """One predicate decides M006 applicability for both Judge and metric."""
        timeline = dialogue_timeline(interruption=True)
        turns = turns_document(interruption=True)
        # `has_interruption` remains true, but the event the metric needs is gone.
        timeline['events'] = [event for event in timeline['events']
                              if not event['type'].startswith('interrupt')]
        timeline['evidence'] = [item for item in timeline['evidence']
                                if item['evidence_id'] not in ('EVD-INTERRUPT',
                                                               'EVD-INTERRUPT-END')]
        self.assertEqual(timeline_errors(timeline), [])
        judge = LLMJudge(MockLLMProvider())
        document = judge.evaluate(fused_document(interruption=True), turns,
                                  compute_timeline_metrics(timeline), timeline, run_id=RUN_ID)
        self.assertNotIn('barge_in_compliance',
                         {result['dimension'] for result in document['results']})
        records, _abstentions = semantic_evidence_records(document, timeline, turns)
        self.assertFalse([record for record in records
                          if record['kind'] == 'barge_in_compliance'])
        metric = next(item for item in
                      compute_timeline_metrics(timeline, semantic_evidence=records)['metrics']
                      if item['name'] == 'barge_in_semantic_compliance')
        self.assertEqual(metric['status'], 'not_applicable')

    def test_an_ineligible_record_abstains_with_a_reason(self):
        broken = copy.deepcopy(self.records[0])
        broken['criterion_version'] = '9.9.9'
        document = copy.deepcopy(self.document)
        target = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        target['criterion_version'] = '9.9.9'
        records, abstentions = semantic_evidence_records(document, self.timeline, self.turns)
        self.assertEqual(records, [])
        self.assertTrue(any('criterion_version' in item['reason'] for item in abstentions))
        errors = semantic_evidence_errors([broken], self.timeline, self.turns)
        self.assertTrue(any('criterion_version' in error for error in errors))

    def test_engine_abstains_when_no_record_is_supplied(self):
        result = compute_timeline_metrics(self.timeline)
        semantic = [metric for metric in result['metrics'] if metric['name'] == 'semantic_response']
        self.assertEqual(semantic[0]['status'], 'insufficient_evidence')
        self.assertIn('Constrained semantic evidence is required', semantic[0]['reason'])

    def test_duplicate_dimension_per_turn_is_refused(self):
        document = copy.deepcopy(self.document)
        target = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        document['results'].append(copy.deepcopy(target))
        records, abstentions = semantic_evidence_records(document, self.timeline, self.turns)
        self.assertTrue(any('duplicate judgment' in item['reason'] for item in abstentions))


class JudgeArtifactIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        judge = LLMJudge(MockLLMProvider())
        self.document = judge.evaluate(fused_document(), self.turns,
                                       compute_timeline_metrics(self.timeline),
                                       self.timeline, run_id=RUN_ID)

    def test_unresolvable_invocation_is_rejected(self):
        document = copy.deepcopy(self.document)
        document['results'][0]['invocation_id'] = 'CALL-missing'
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('must resolve to a recorded invocation' in error for error in errors))

    def test_missing_evidence_reference_is_rejected(self):
        document = copy.deepcopy(self.document)
        target = next(item for item in document['results']
                      if item['dimension'] == 'finding_candidate')
        target['evidence_refs'] = ['EVD-DOES-NOT-EXIST']
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('unknown timeline evidence' in error for error in errors))

    def test_unknown_turn_reference_is_rejected(self):
        document = copy.deepcopy(self.document)
        document['results'][0]['turn_id'] = 'TURN-MISSING'
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('unknown turn' in error for error in errors))

    def test_an_empty_turns_document_still_enforces_turn_resolution(self):
        """A supplied Turns document declaring none is not "no document"."""
        document = copy.deepcopy(self.document)
        target = next(item for item in document['results']
                      if item['turn_id'] is not None)
        errors = judge_document_errors(document, self.timeline, {'turns': []})
        self.assertTrue(any('unknown turn' in error for error in errors))
        # Without a Turns document at all the reference cannot be resolved, so the
        # check is skipped rather than reporting a false failure.
        self.assertEqual([error for error in judge_document_errors(document, self.timeline)
                          if 'unknown turn' in error], [])
        self.assertTrue(target['turn_id'])

    def test_raw_output_hash_must_match(self):
        document = copy.deepcopy(self.document)
        document['invocations'][0]['raw_response_sha256'] = '0' * 64
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('does not match the preserved raw output' in error for error in errors))

    def test_successful_invocation_must_preserve_raw_output(self):
        document = copy.deepcopy(self.document)
        document['invocations'][0]['raw_response'] = None
        document['invocations'][0]['raw_response_sha256'] = None
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('must preserve its raw output' in error for error in errors))

    def test_credential_material_is_never_accepted(self):
        document = copy.deepcopy(self.document)
        # A credential-shaped value in an otherwise allowed field, and the
        # credential-key scanner itself.
        document['judge_profile']['endpoint'] = 'Bearer sk-abcdefghijklmnop'
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('credential-shaped' in error for error in errors))
        self.assertEqual(credential_errors({'a': 1}), [])
        self.assertTrue(credential_errors({'authorization': 'Bearer abcdefghijkl'}))
        self.assertTrue(credential_errors({'apiKey': 'value'}))
        self.assertEqual(credential_errors({'api_key': ''}), [])

    def test_semantic_verdict_needs_its_criterion_and_citation(self):
        document = copy.deepcopy(self.document)
        target = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        target['evidence_refs'] = []
        target['event_refs'] = []
        errors = judge_result_errors(target)
        self.assertTrue(any('must cite evidence' in error for error in errors))

    def test_artifact_criteria_version_must_match_the_adapter(self):
        document = copy.deepcopy(self.document)
        document['criteria_version'] = '0.9.0'
        document['judge_profile']['criteria_version'] = '0.9.0'
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('the adapter contract is' in error for error in errors))

    def test_profile_criteria_version_must_agree_with_the_artifact(self):
        document = copy.deepcopy(self.document)
        document['judge_profile']['criteria_version'] = '0.9.0'
        errors = judge_document_errors(document, self.timeline, self.turns)
        self.assertTrue(any('must agree with the artifact criteria_version' in error
                            for error in errors))

    def test_a_result_profile_from_another_criteria_version_is_not_eligible(self):
        """An adapter-version change must not silently keep old judgments eligible."""
        document = copy.deepcopy(self.document)
        target = next(item for item in document['results']
                      if item['dimension'] == 'semantic_response')
        target['judge_profile']['criteria_version'] = '0.9.0'
        records, abstentions = semantic_evidence_records(document, self.timeline, self.turns)
        self.assertFalse([record for record in records if record['kind'] == 'semantic_response'])
        self.assertTrue(any('criteria_version disagrees with the adapter contract' in item['reason']
                            for item in abstentions))


if __name__ == '__main__':
    unittest.main()
