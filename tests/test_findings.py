import copy
import unittest

from aivoicebench.validation import ROOT, finding_errors, load_document, schema_errors


class FindingEvidenceContract(unittest.TestCase):
    def setUp(self):
        self.finding = load_document(ROOT / 'examples/findings/exploratory-defect.json')
        self.timeline = load_document(ROOT / 'examples/findings/defect-timeline.json')

    def invalid(self, message):
        self.assertIn(message, '\n'.join(finding_errors(self.finding, self.timeline)))

    def test_positive_defect_and_observation(self):
        self.assertEqual(finding_errors(self.finding, self.timeline), [])
        self.assertEqual(finding_errors(load_document(ROOT / 'examples/finding.example.json'),
                                      load_document(ROOT / 'examples/timeline.example.json'),
                                      [load_document(ROOT / 'examples/metrics.example.json')]), [])

    def test_evidence_is_first_class(self):
        for evidence in self.timeline['evidence']:
            self.assertEqual(schema_errors(evidence, 'evidence'), [])

    def test_confirmed_defect_cannot_lose_evidence(self):
        self.finding['evidence_ids'] = []
        self.invalid('non-empty')

    def test_dangling_artifact(self):
        self.timeline['evidence'][0]['artifact_id'] = 'missing'
        self.invalid('unknown artifact_id')

    def test_unknown_evidence_and_event(self):
        self.finding['evidence_ids'] = ['missing']
        self.finding['event_ids'] = ['missing']
        self.invalid('unknown timeline evidence')
        self.invalid('unknown timeline event')

    def test_nonempty_snippet_for_defect(self):
        self.finding['evidence_ids'].remove('EVD-OBSERVATION')
        self.invalid('nonempty evidence snippet')

    def test_metric_reference_resolution(self):
        self.finding['metric_ids'] = ['missing']
        self.invalid('unknown supplied metric')

    def test_observation_has_no_defect_severity(self):
        self.finding.update(kind='observation', regression_case_candidate=False)
        del self.finding['regression']
        self.invalid('null')

    def test_suspected_cause_needs_log_verification(self):
        self.finding['requires_log_verification'] = False
        self.invalid('True was expected')

    def test_unknown_attribution_is_explicit(self):
        self.finding['attribution_status'] = 'unknown'
        self.invalid('unknown')
        self.finding.update(suspected_layers=['unknown'], attribution_confidence=0)
        self.assertEqual(finding_errors(self.finding, self.timeline), [])

    def test_cause_verification_requires_logs_and_review(self):
        self.finding.update(attribution_status='verified', requires_log_verification=False)
        self.invalid('requires device log evidence')
        self.invalid('requires human review')

    def test_safety_confirmation_needs_human(self):
        self.finding.update(severity='P0', suspected_layers=['safety'])
        self.invalid('human_review')
        self.finding['human_review']['status'] = 'required'
        self.invalid('human_review')
        self.finding['human_review'] = dict(status='approved', reviewer='fixture-reviewer', reviewed_at='2026-09-06T10:00:00Z')
        self.assertEqual(finding_errors(self.finding, self.timeline), [])

    def test_approved_review_needs_identity_and_date(self):
        self.finding['human_review']['status'] = 'approved'
        self.invalid('reviewer')
        self.finding['human_review'].update(reviewer='fixture', reviewed_at='not-a-date')
        self.invalid('date-time')

    def test_unconfirmed_exploration_not_candidate(self):
        self.finding['status'] = 'needs_verification'
        self.invalid('confirmed')

    def test_frozen_requires_versions_and_linked_case(self):
        self.finding['regression']['state'] = 'frozen'
        self.invalid('case_version')
        self.finding['regression'].update(case_id='VAD-PAUSE-800', case_version='2.0.0', golden_set_id='fixture-golden', golden_set_version='1.0.0')
        self.invalid('requires the linked TestCase')
        case = load_document(ROOT / 'examples/test-case.example.yaml')
        case['golden_set'] = dict(set_id='fixture-golden', version='1.0.0')
        self.assertEqual(finding_errors(self.finding, self.timeline, regression_case=case), [])
        case['case_version'] = '3.0.0'
        self.assertIn('Case identity/version mismatch', '\n'.join(finding_errors(self.finding, self.timeline, regression_case=case)))

    def test_run_kind_mismatch(self):
        self.finding['execution_kind'] = 'hardware'
        self.invalid('execution_kind disagree')

    def test_attribution_taxonomy_closed(self):
        self.finding['suspected_layers'] = ['acoustic-magic']
        self.invalid('not one of')


if __name__ == '__main__':
    unittest.main()
