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
import unittest

from aivoicebench.llm import LLMJudge, MockLLMProvider
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