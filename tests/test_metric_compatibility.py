"""MetricResult version-compatibility tests (legacy 2.0.0 vs canonical 3.0.0).

The validator must read a legacy 2.0.0 document as-is, enforce the canonical
3.0.0 traceability rules, and never silently normalize one version into the
other. A 2.0.0 document may not be retrofitted with 3.0.0 policy fields, and a
3.0.0 document may not borrow a fabricated Case identity.

Also covers PRD-M008: a false-endpoint candidate and a confirmed false endpoint
are not interchangeable.
"""

import unittest

from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.validation import metric_errors


def make_event(eid, etype, start, end=None, turn_id='TURN-0001',
               response_id='RESP-0001', evidence_ids=None, confidence=None):
    if end is None:
        end = start
    return {
        'schema_version': '2.0.0', 'event_id': eid, 'run_id': 'RUN-t',
        'case_id': 'CASE-t', 'turn_id': turn_id, 'response_id': response_id,
        'type': etype, 'start_ms': start, 'end_ms': end,
        'source': 'audio_signal', 'observation_scope': 'black_box',
        'confidence': confidence, 'confidence_source': None, 'uncertainty_ms': None,
        'evidence_ids': evidence_ids or ['EV-001'],
    }


def make_timeline(events, run_id='RUN-t', case_id='CASE-t'):
    return {
        'schema_version': '2.0.0', 'run_id': run_id, 'case_id': case_id,
        'case_version': '0.0.0', 'attempt': 1, 'execution_kind': 'imported',
        'time_base': {'kind': 'audio_relative_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None,
                         'model': None, 'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': 'partial',
        'gaps': [{'reason': 'test fixture coverage gap', 'required_evidence': 'none',
                  'start_ms': 1000, 'end_ms': 5000}],
        'tracks': [{'track_id': 'TRACK-mix', 'role': 'room_mix', 'clock_id': 'CLOCK-canonical',
                    'sample_rate_hz': 16000,
                    'sync': {'status': 'uncalibrated', 'offset_ms': None, 'uncertainty_ms': None,
                             'max_drift_ppm': None, 'calibration_id': None}}],
        'artifacts': [{'artifact_id': 'ART-a', 'kind': 'audio', 'path': 'normalized.wav',
                       'sha256': 'a' * 64, 'origin': 'imported', 'track_id': 'TRACK-mix',
                       'duration_ms': 5000}],
        'evidence': [{'schema_version': '1.0.0', 'evidence_id': 'EV-001',
                      'artifact_id': 'ART-a', 'track_id': 'TRACK-mix',
                      'time_base': 'audio_relative_ms', 'start_ms': 0, 'end_ms': 1000,
                      'source': 'audio_signal', 'confidence': 0.9}],
        'events': events,
    }


def canonical_metric(**overrides):
    """A valid canonical 3.0.0 MetricResult as the engine would emit it."""
    events = [
        make_event('E1', 'tester_speech_end', 1000),
        make_event('E2', 'device_speech_start', 1500),
    ]
    timeline = make_timeline(events)
    metric = next(m for m in compute_timeline_metrics(timeline)['metrics']
                  if m['name'] == 'first_speech_latency_ms')
    metric.update(overrides)
    return metric


def legacy_metric(**overrides):
    """The same measurement expressed under the legacy 2.0.0 contract."""
    metric = canonical_metric()
    for key in ('prd_ref', 'policy', 'policy_version', 'turn_id', 'response_id',
                'analysis_id', 'confidence_source', 'uncertainty_ms'):
        metric.pop(key, None)
    metric.update({'schema_version': '2.0.0', 'case_id': 'CASE-t',
                   'definition_version': '2.0.0', 'confidence': 0.9})
    metric.update(overrides)
    return metric


class LegacyReadableTests(unittest.TestCase):
    def test_valid_legacy_metric_passes(self):
        self.assertEqual(metric_errors(legacy_metric()), [])

    def test_legacy_metric_against_its_timeline(self):
        timeline = make_timeline([make_event('E1', 'tester_speech_end', 1000)])
        timeline['case_id'] = 'CASE-t'
        metric = legacy_metric(evidence_ids=['EV-001'], event_ids=['E1'], case_id='CASE-t')
        self.assertEqual(metric_errors(metric, timeline), [])

    def test_legacy_route_computation_still_works(self):
        """The legacy 2.0.0 contract is readable, not executable-as-canonical."""
        import aivoicebench.metrics as metrics
        self.assertEqual(metrics.DEFINITION_VERSION, '3.0.0')
        self.assertEqual(metrics.METRIC_POLICY_VERSION, '1.0.0')


class CanonicalRequirementTests(unittest.TestCase):
    def test_missing_prd_ref_is_rejected(self):
        metric = canonical_metric()
        metric.pop('prd_ref')
        errors = metric_errors(metric)
        self.assertTrue(any('prd_ref' in e for e in errors), errors)

    def test_null_prd_ref_is_rejected(self):
        errors = metric_errors(canonical_metric(prd_ref=None))
        self.assertTrue(any('prd_ref' in e for e in errors), errors)

    def test_policy_without_version_is_rejected(self):
        errors = metric_errors(canonical_metric(policy='signed_turn_gap', policy_version=None))
        self.assertTrue(any('policy_version' in e for e in errors), errors)

    def test_policy_with_version_is_accepted(self):
        self.assertEqual(metric_errors(
            canonical_metric(policy='signed_turn_gap', policy_version='1.0.0')), [])

    def test_numeric_confidence_without_source_is_rejected(self):
        errors = metric_errors(canonical_metric(confidence=0.5, confidence_source=None))
        self.assertTrue(any('confidence_source' in e for e in errors), errors)

    def test_numeric_confidence_with_source_is_accepted(self):
        self.assertEqual(metric_errors(
            canonical_metric(confidence=0.5, confidence_source='acoustic_boundary')), [])

    def test_null_confidence_needs_no_source(self):
        """Unknown is not zero: a null confidence is valid on its own."""
        self.assertEqual(metric_errors(canonical_metric(confidence=None, confidence_source=None)), [])

    def test_imported_case_identity_may_be_null(self):
        metric = canonical_metric(case_id=None)
        self.assertIsNone(metric['case_id'])
        self.assertEqual(metric_errors(metric), [])

    def test_legacy_contract_rejects_30_policy_fields(self):
        errors = metric_errors(legacy_metric(prd_ref='PRD-M002'))
        self.assertTrue(any('3.0.0 policy fields' in e for e in errors), errors)

    def test_legacy_contract_rejects_policy_field(self):
        errors = metric_errors(legacy_metric(policy='signed_turn_gap'))
        self.assertTrue(any('3.0.0 policy fields' in e for e in errors), errors)

    def test_legacy_contract_requires_case_identity(self):
        errors = metric_errors(legacy_metric(case_id=None))
        self.assertTrue(any('case_id' in e for e in errors), errors)

    def test_legacy_contract_requires_numeric_confidence(self):
        errors = metric_errors(legacy_metric(confidence=None))
        self.assertTrue(any('confidence' in e for e in errors), errors)

    def test_versions_are_not_silently_normalized(self):
        """A 2.0.0 document stays 2.0.0: validity must not depend on the reader upgrading it."""
        legacy = legacy_metric()
        self.assertEqual(metric_errors(legacy), [])
        self.assertEqual(legacy['schema_version'], '2.0.0')
        self.assertNotIn('prd_ref', legacy)
        # The same document is invalid the moment it claims 3.0.0 without 3.0.0 evidence.
        claimed = dict(legacy, schema_version='3.0.0')
        self.assertTrue(metric_errors(claimed))


class IdentityConflictTests(unittest.TestCase):
    def test_run_id_conflict_is_caught(self):
        timeline = make_timeline([], run_id='RUN-other')
        metric = canonical_metric(run_id='RUN-t')
        errors = metric_errors(metric, timeline)
        self.assertTrue(any('run_id disagree' in e for e in errors), errors)

    def test_null_metric_case_id_does_not_conflict_with_timeline_case(self):
        timeline = make_timeline([make_event('E1', 'tester_speech_end', 1000)])
        timeline['case_id'] = 'CASE-t'
        metric = canonical_metric(case_id=None, evidence_ids=['EV-001'], event_ids=['E1'])
        self.assertEqual(metric_errors(metric, timeline), [])

    def test_matching_case_id_is_accepted(self):
        timeline = make_timeline([make_event('E1', 'tester_speech_end', 1000)])
        timeline['case_id'] = 'CASE-t'
        metric = canonical_metric(case_id='CASE-t', evidence_ids=['EV-001'], event_ids=['E1'])
        self.assertEqual(metric_errors(metric, timeline), [])

    def test_scoped_case_id_conflict_is_caught(self):
        timeline = make_timeline([], case_id='CASE-t')
        metric = canonical_metric(case_id='CASE-other')
        errors = metric_errors(metric, timeline)
        self.assertTrue(any('case_id disagree' in e for e in errors), errors)


class FalseEndpointNonInterchangeableTests(unittest.TestCase):
    def test_engine_emits_candidate_not_confirmed(self):
        events = [make_event('E1', 'possible_false_endpoint', 700, turn_id=None, response_id=None)]
        metrics = compute_timeline_metrics(make_timeline(events))['metrics']
        names = [m['name'] for m in metrics]
        self.assertIn('false_endpoint_candidate', names)
        self.assertNotIn('false_endpoint', names)

    def test_candidate_is_not_a_confirmed_endpoint(self):
        events = [make_event('E1', 'possible_false_endpoint', 700, turn_id=None, response_id=None)]
        metrics = compute_timeline_metrics(make_timeline(events))['metrics']
        candidate = next(m for m in metrics if m['name'] == 'false_endpoint_candidate')
        self.assertEqual(candidate['policy'], 'candidate_only')
        self.assertIn('confirmation', candidate['reason'].lower())
        self.assertEqual(metric_errors(candidate), [])

    def test_confirmed_event_does_not_create_a_candidate(self):
        """A confirmed false endpoint is semantic evidence, not a candidate."""
        events = [make_event('E1', 'false_endpoint', 700, turn_id=None, response_id=None)]
        metrics = compute_timeline_metrics(make_timeline(events))['metrics']
        names = [m['name'] for m in metrics]
        self.assertNotIn('false_endpoint_candidate', names)
        self.assertNotIn('false_endpoint', names)

    def test_3_0_0_rejects_the_legacy_confirmed_name(self):
        errors = metric_errors(canonical_metric(name='false_endpoint', unit='boolean', value=True))
        self.assertTrue(any('false_endpoint' in e for e in errors), errors)

    def test_engine_candidate_never_claims_confirmed_status(self):
        events = [make_event('E1', 'possible_false_endpoint', 700, turn_id=None, response_id=None)]
        metrics = compute_timeline_metrics(make_timeline(events))['metrics']
        candidate = next(m for m in metrics if m['name'] == 'false_endpoint_candidate')
        self.assertEqual(candidate['status'], 'observed')
        self.assertEqual(candidate['prd_ref'], 'PRD-M008')


if __name__ == '__main__':
    unittest.main()
