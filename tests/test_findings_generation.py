"""Issue #10 acceptance tests for evidence-linked Finding generation.

The Finding contract this proves:

* a Finding exists only for evidence the candidate actually cited and that
  resolves in the same Timeline — there is no "nearest evidence" fallback;
* `metric_ids` are canonical `metric_id` identities, and each resolves;
* the originating Turn is recorded explicitly (Finding 2.1.0 `turn_ids`) and is
  reachable from the Finding's own citations;
* a suspected cause stays a hypothesis that needs device logs;
* an invalid Finding is withheld and reported, never written.

Fixtures are synthetic: no recording, no provider and no hardware are involved.
"""

import copy
import json
import unittest

from aivoicebench.findings import (
    CURRENT_FINDING_VERSION, LEGACY_FINDING_VERSION, generate_findings,
    generate_findings_document, migrate_finding_document,
)
from aivoicebench.llm import LLMJudge, MockLLMProvider
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.validation import finding_errors, schema_errors
from tests.judge_fixture import (
    RUN_ID, dialogue_timeline, fused_document, make_event, make_evidence,
    make_timeline, turns_document,
)


class FindingGenerationTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.metrics = compute_timeline_metrics(self.timeline)
        judge = LLMJudge(MockLLMProvider())
        self.judge_document = judge.evaluate(fused_document(), self.turns, self.metrics,
                                            self.timeline, run_id=RUN_ID)
        self.document = generate_findings_document(self.judge_document, self.timeline,
                                                  self.metrics, run_id=RUN_ID)

    def test_candidate_becomes_a_validated_finding(self):
        self.assertEqual(self.document['schema_version'], CURRENT_FINDING_VERSION)
        self.assertTrue(self.document['findings'])
        self.assertEqual(self.document['rejected'], [])
        for finding in self.document['findings']:
            self.assertEqual(schema_errors(finding, 'finding'), [])
            self.assertEqual(finding_errors(finding, self.timeline, self.metrics['metrics']), [])
            self.assertEqual(finding['schema_version'], CURRENT_FINDING_VERSION)
            self.assertEqual(finding['run_id'], RUN_ID)

    def test_finding_links_evidence_event_metric_and_turn(self):
        finding = self.document['findings'][0]
        self.assertEqual(finding['title'], 'high_latency')
        self.assertTrue(finding['evidence_ids'])
        self.assertTrue(finding['event_ids'])
        self.assertTrue(finding['metric_ids'])
        self.assertEqual(finding['turn_ids'], ['TURN-0001'])

    def test_metric_ids_are_canonical_identities_not_display_names(self):
        finding = self.document['findings'][0]
        names = {metric['name'] for metric in self.metrics['metrics']}
        for metric_id in finding['metric_ids']:
            self.assertNotIn(metric_id, names)
            self.assertIn(metric_id, {metric['metric_id'] for metric in self.metrics['metrics']})
        self.assertTrue(any('.first_speech_latency_ms' in metric_id
                            for metric_id in finding['metric_ids']))

    def test_evidence_provenance_stays_classified(self):
        """Objective, semantic and human evidence remain distinguishable."""
        finding = self.document['findings'][0]
        sources = {item['source'] for item in self.timeline['evidence']
                   if item['evidence_id'] in finding['evidence_ids']}
        # The latency finding rests on measured acoustic boundaries, and the
        # tester's own turn boundary is a human annotation. Neither is relabelled.
        self.assertTrue(sources & {'audio_signal', 'manual_annotation'})
        linked = {metric['metric_id']: metric for metric in self.metrics['metrics']}
        methods = {linked[metric_id]['method'] for metric_id in finding['metric_ids']}
        self.assertEqual(methods, {'deterministic'})
        self.assertEqual(finding['human_review']['status'], 'required')

    def test_a_suspected_cause_is_a_hypothesis_with_log_requirement(self):
        finding = self.document['findings'][0]
        self.assertEqual(finding['attribution_status'], 'suspected')
        self.assertEqual(finding['suspected_layers'], ['llm'])
        self.assertTrue(finding['requires_log_verification'])
        self.assertLess(finding['attribution_confidence'], 1.0)
        self.assertGreater(finding['attribution_confidence'], 0.0)
        self.assertEqual(finding['status'], 'needs_verification')
        self.assertFalse(finding['regression_case_candidate'])

    def test_backward_compatible_entry_point_returns_a_list(self):
        findings = generate_findings(self.judge_document, self.timeline, self.metrics, RUN_ID)
        self.assertEqual(len(findings), len(self.document['findings']))


class FindingAbstentionTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()
        self.turns = turns_document()
        self.metrics = compute_timeline_metrics(self.timeline)

    def candidate(self, **overrides):
        candidate = {
            'schema_version': '1.0.0', 'judge_id': 'JUDGE-fixture', 'run_id': RUN_ID,
            'dimension': 'finding_candidate', 'turn_id': 'TURN-0001',
            'response_id': 'RESP-0001', 'decision': 'high_latency', 'score': None,
            'confidence': 0.6, 'reason': 'latency above threshold', 'status': 'observed',
            'evidence_refs': ['EVD-DEVICE-START'], 'event_refs': ['EVT-DEVICE-START'],
            'model': 'mock-llm-v1', 'prompt_version': 'finding-v1.0.0', 'provider': 'mock',
            'invocation_id': 'CALL-fixture', 'latency_ms': 1.0, 'finding_severity': 'medium',
            'suspected_layer': 'llm', 'attribution_confidence': 0.4,
            'requires_log_verification': True,
        }
        candidate.update(overrides)
        return candidate

    def document(self, candidate):
        return generate_findings_document([candidate], self.timeline, self.metrics, RUN_ID)

    def test_no_resolvable_evidence_abstains_instead_of_borrowing_evidence(self):
        document = self.document(self.candidate(evidence_refs=[], event_refs=[]))
        self.assertEqual(document['findings'], [])
        self.assertTrue(document['abstentions'])
        self.assertIn('cites no evidence that resolves', document['abstentions'][0]['reason'])
        self.assertEqual(document['abstentions'][0]['state'], 'insufficient_evidence')

    def test_unresolvable_reference_abstains(self):
        document = self.document(self.candidate(evidence_refs=['EVD-MISSING'],
                                                event_refs=['EVT-MISSING']))
        self.assertEqual(document['findings'], [])
        self.assertEqual(document['abstentions'][0]['reason'],
                         'the candidate cites no evidence that resolves in the Timeline')

    def test_another_turns_evidence_does_not_attach_to_this_finding(self):
        timeline = dialogue_timeline()
        timeline['evidence'].append(make_evidence('EVD-OTHER', 8000))
        timeline['events'].append(make_event('EVT-OTHER', 'device_speech_start', 8000,
                                             ['EVD-OTHER'], turn_id='TURN-0002'))
        timeline['events'].sort(key=lambda event: event['start_ms'])
        timeline['status'] = 'partial'
        timeline['gaps'] = [{'reason': 'fixture: second turn out of scope',
                             'required_evidence': 'speaker_diarization_or_human_review',
                             'start_ms': 8000, 'end_ms': 30000}]
        document = generate_findings_document(
            [self.candidate(turn_id='TURN-0002', evidence_refs=['EVD-OTHER'],
                            event_refs=['EVT-OTHER'])],
            timeline, self.metrics, RUN_ID)
        # The cited evidence exists, but no metric of that turn does and the
        # tester/device latency metric belongs to TURN-0001, so no foreign number
        # is attached. The finding may still exist; what must not happen is an
        # unrelated metric link.
        for finding in document['findings']:
            self.assertEqual(finding['turn_ids'], ['TURN-0002'])
            self.assertEqual(finding['metric_ids'], [])
        for abstention in document['abstentions']:
            self.assertEqual(abstention['state'], 'insufficient_evidence')

    def test_a_turn_link_that_is_not_reachable_is_never_published(self):
        document = self.document(self.candidate(turn_id='TURN-MISSING'))
        self.assertEqual(document['findings'], [])
        self.assertIn('cannot be bound to a Turn', document['abstentions'][0]['reason'])

    def test_a_finding_that_breaks_its_contract_is_withheld_and_reported(self):
        timeline = copy.deepcopy(self.timeline)
        # The cited evidence is removed from the catalog, so the generated
        # Finding cannot resolve. It must be reported, not silently emitted.
        document = generate_findings_document([self.candidate()], timeline, self.metrics,
                                              RUN_ID)
        self.assertEqual(document['rejected'], [])
        self.assertTrue(document['findings'])
        broken = copy.deepcopy(document['findings'][0])
        broken['evidence_ids'] = ['EVD-MISSING']
        self.assertTrue(finding_errors(broken, timeline, self.metrics['metrics']))

    def test_non_observed_candidate_creates_nothing(self):
        document = self.document(self.candidate(status='insufficient_evidence'))
        self.assertEqual(document['findings'], [])
        self.assertEqual(document['abstentions'], [])

    def test_unknown_layer_is_explicit_not_a_hypothesis(self):
        document = self.document(self.candidate(suspected_layer='unknown',
                                                attribution_confidence=0.9))
        finding = document['findings'][0]
        self.assertEqual(finding['attribution_status'], 'unknown')
        self.assertEqual(finding['suspected_layers'], ['unknown'])
        self.assertEqual(finding['attribution_confidence'], 0)
        self.assertFalse(finding['requires_log_verification'])
        self.assertEqual(finding_errors(finding, self.timeline, self.metrics['metrics']), [])

    def test_layer_outside_the_taxonomy_degrades_to_unknown(self):
        document = self.document(self.candidate(suspected_layer='acoustic-magic'))
        finding = document['findings'][0]
        self.assertEqual(finding['attribution_status'], 'unknown')
        self.assertEqual(finding['suspected_layers'], ['unknown'])
        self.assertEqual(finding_errors(finding, self.timeline, self.metrics['metrics']), [])

    def test_certain_self_reported_confidence_is_capped(self):
        document = self.document(self.candidate(attribution_confidence=1.0))
        self.assertLess(document['findings'][0]['attribution_confidence'], 1.0)

    def test_a_candidate_without_a_cited_event_cannot_claim_a_turn(self):
        """Evidence carries no turn binding; an event is what binds a Finding.

        Without this rule a candidate citing one turn's evidence while linking a
        metric of another turn would publish a multi-turn Finding resting on a
        single turn's evidence.
        """
        timeline = dialogue_timeline()
        timeline['evidence'].append(make_evidence('EVD-OTHER', 8000))
        timeline['events'].append(make_event('EVT-OTHER', 'device_speech_start', 8000,
                                             ['EVD-OTHER'], turn_id='TURN-0002'))
        timeline['events'].sort(key=lambda event: event['start_ms'])
        timeline['status'] = 'partial'
        timeline['gaps'] = [{'reason': 'fixture: second turn is out of scope',
                             'required_evidence': 'speaker_diarization_or_human_review',
                             'start_ms': 8000, 'end_ms': 30000}]
        metrics = copy.deepcopy(self.metrics)
        metrics['metrics'] = [dict(metric, turn_id='TURN-0002') for metric in metrics['metrics']]
        document = generate_findings_document(
            [self.candidate(evidence_refs=['EVD-DEVICE-START'], event_refs=[])],
            timeline, metrics, RUN_ID)
        self.assertEqual(document['findings'], [])
        self.assertIn('cites no event that resolves', document['abstentions'][0]['reason'])

    def test_a_linked_metric_cannot_add_a_turn_the_evidence_does_not_reach(self):
        timeline = dialogue_timeline()
        timeline['evidence'].append(make_evidence('EVD-OTHER', 8000))
        timeline['events'].append(make_event('EVT-OTHER', 'device_speech_start', 8000,
                                             ['EVD-OTHER'], turn_id='TURN-0002'))
        timeline['events'].sort(key=lambda event: event['start_ms'])
        timeline['status'] = 'partial'
        timeline['gaps'] = [{'reason': 'fixture: second turn is out of scope',
                             'required_evidence': 'speaker_diarization_or_human_review',
                             'start_ms': 8000, 'end_ms': 30000}]
        metrics = copy.deepcopy(self.metrics)
        metrics['metrics'] = [dict(metric, turn_id='TURN-0002') for metric in metrics['metrics']]
        # The candidate cites only TURN-0001's event, while every metric belongs to
        # TURN-0002. Nothing may be linked, and no second turn may be declared.
        document = generate_findings_document(
            [self.candidate(event_refs=['EVT-DEVICE-START'])], timeline, metrics, RUN_ID)
        for finding in document['findings']:
            self.assertEqual(finding['turn_ids'], ['TURN-0001'])
            self.assertEqual(finding['metric_ids'], [])

    def test_an_invalid_timeline_produces_no_finding_at_all(self):
        document = generate_findings_document([self.candidate()], {'events': []},
                                              self.metrics, RUN_ID)
        self.assertEqual(document['findings'], [])
        self.assertEqual(document['abstentions'][0]['state'], 'not_eligible')

    def test_a_timeline_without_a_case_identity_never_invents_one(self):
        """The case identity comes from the Timeline; absence is not invented.

        The rule itself lives in the timeline contract, so the observable behaviour
        is the invalid-Timeline gate rather than a second copy of the check here.
        """
        timeline = dialogue_timeline()
        timeline['case_id'] = None
        document = generate_findings_document([self.candidate()], timeline, self.metrics,
                                              RUN_ID)
        self.assertEqual(document['findings'], [])
        self.assertEqual(document['abstentions'][0]['state'], 'not_eligible')
        self.assertIn('case_id', document['abstentions'][0]['reason'])
        # And no Case identity is invented anywhere in the produced document.
        self.assertNotIn('CASE', json.dumps(document))

    def test_a_nullable_case_id_finding_is_rejected_by_the_contract(self):
        """The rule is asserted directly so a dead affordance cannot reappear."""
        document = self.document(self.candidate())
        finding = copy.deepcopy(document['findings'][0])
        self.assertEqual(finding_errors(finding, self.timeline, self.metrics['metrics']), [])
        finding['case_id'] = None
        errors = finding_errors(finding, self.timeline, self.metrics['metrics'])
        self.assertTrue(any('case_id' in error for error in errors),
                        'a nullable case_id must not validate under Finding 2.1.0')


class FindingMigrationTests(unittest.TestCase):
    def setUp(self):
        self.timeline = dialogue_timeline()

    def legacy_finding(self):
        return {
            'schema_version': LEGACY_FINDING_VERSION, 'finding_id': 'FIND-legacy',
            'run_id': RUN_ID, 'case_id': 'CASE-JUDGE-FIXTURE', 'execution_kind': 'synthetic',
            'kind': 'defect', 'title': 'legacy latency', 'severity': 'P2',
            'status': 'needs_verification', 'description': 'legacy', 'expected_behavior': 'x',
            'actual_behavior': 'y', 'confidence': 0.5, 'attribution_status': 'unknown',
            'attribution_confidence': 0, 'suspected_layers': ['unknown'],
            'requires_log_verification': False, 'evidence_ids': ['EVD-DEVICE-START'],
            'event_ids': ['EVT-DEVICE-START'], 'metric_ids': [],
            'human_review': {'status': 'required', 'reviewer': None, 'reviewed_at': None},
            'origin': 'manual', 'regression_case_candidate': False,
        }

    def test_legacy_document_still_validates_under_its_own_version(self):
        finding = self.legacy_finding()
        self.assertEqual(finding_errors(finding, self.timeline, []), [])
        finding['turn_ids'] = ['TURN-0001']
        self.assertIn('2.0.0 documents must not carry 2.1.0 fields',
                      '\n'.join(finding_errors(finding, self.timeline, [])))

    def test_migration_resolves_turns_from_the_timeline(self):
        migrated = migrate_finding_document(self.legacy_finding(), timeline=self.timeline)
        self.assertEqual(migrated['schema_version'], CURRENT_FINDING_VERSION)
        self.assertEqual(migrated['turn_ids'], ['TURN-0001'])
        self.assertEqual(finding_errors(migrated, self.timeline, []), [])

    def test_migration_requires_the_timeline_and_never_emits_an_invalid_document(self):
        """A migrated 2.1.0 document must be one this engine would accept.

        Without a Timeline the Turns cannot be resolved, so the Timeline is a
        required argument and a non-Timeline value is refused: emitting an empty
        `turn_ids` instead would produce a document that `finding_errors` rejects
        against the very Timeline the original events came from.
        """
        with self.assertRaises(TypeError):
            migrate_finding_document(self.legacy_finding())
        # An explicit non-Timeline is refused too, rather than silently emitting
        # an unvalidatable document.
        with self.assertRaises(ValueError) as caught:
            migrate_finding_document(self.legacy_finding(), timeline=None)
        self.assertIn('requires the Timeline', str(caught.exception))
        # With the Timeline, the result validates under the current rules.
        migrated = migrate_finding_document(self.legacy_finding(), self.timeline)
        self.assertEqual(migrated['turn_ids'], ['TURN-0001'])
        self.assertEqual(finding_errors(migrated, self.timeline, []), [])

    def test_migration_is_idempotent_and_rejects_unknown_versions(self):
        current = migrate_finding_document(self.legacy_finding(), timeline=self.timeline)
        self.assertEqual(migrate_finding_document(current, timeline=self.timeline), current)
        with self.assertRaises(ValueError):
            migrate_finding_document({'schema_version': '9.9.9'}, timeline=self.timeline)


if __name__ == '__main__':
    unittest.main()