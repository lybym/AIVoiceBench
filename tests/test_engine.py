import copy
import unittest

from aivoicebench.engine import aggregate_latency, evaluate
from aivoicebench.validation import ROOT, load_document, metric_errors


class DeterministicEngine(unittest.TestCase):
    def setUp(self):
        self.case = load_document(ROOT / 'examples/test-case.example.yaml')
        self.case['metrics'] += ['e2e_first_audio_latency_ms', 'overlap_duration_ms', 'timeout_rate']
        self.timeline = load_document(ROOT / 'examples/timeline.example.json')

    def results(self):
        return {item['name']: item for item in evaluate(self.case, self.timeline)}

    def test_latency_pause_overlap_and_completed_response(self):
        result = self.results()
        self.assertEqual(result['e2e_first_audio_latency_ms']['value'], 1380)
        self.assertEqual(result['false_endpoint']['value'], False)
        self.assertEqual(result['false_endpoint']['status'], 'pass')
        self.assertEqual(result['overlap_duration_ms']['value'], 0)
        self.assertEqual(result['timeout_rate']['value'], 0)
        for metric in result.values():
            self.assertEqual(metric['execution_kind'], 'synthetic')
            self.assertEqual(metric_errors(metric, self.timeline), [])

    def test_false_endpoint_is_failure_not_internal_cause(self):
        self.timeline = load_document(ROOT / 'examples/findings/defect-timeline.json')
        result = self.results()
        self.assertTrue(result['false_endpoint']['value'])
        self.assertEqual(result['false_endpoint']['status'], 'fail')
        self.assertEqual(result['e2e_first_audio_latency_ms']['status'], 'not_applicable')

    def test_missing_full_pause_coverage_does_not_pass(self):
        self.timeline['evidence'] = [item for item in self.timeline['evidence'] if item['evidence_id'] != 'EVD-OBSERVATION']
        self.assertEqual(self.results()['false_endpoint']['status'], 'insufficient_evidence')

    def test_partial_timeline_never_zero_fills(self):
        self.timeline['status'] = 'partial'
        self.timeline['gaps'] = [dict(reason='Capture lost', required_evidence='Device output', start_ms=0, end_ms=7500)]
        for metric in self.results().values():
            self.assertEqual(metric['status'], 'insufficient_evidence')
            self.assertIsNone(metric['value'])

    def test_uncalibrated_times_cannot_calculate_latency(self):
        self.timeline['tracks'][1]['sync'] = dict(status='uncalibrated', offset_ms=None, uncertainty_ms=None, max_drift_ppm=None, calibration_id=None)
        self.assertIsNone(self.results()['e2e_first_audio_latency_ms']['value'])

    def test_barge_stops_old_response(self):
        case = load_document(ROOT / 'examples/test-cases/barge-in.json')
        case['metrics'].append('barge_in_stop_latency_ms')
        timeline = load_document(ROOT / 'examples/timelines/barge-in.json')
        results = {item['name']: item for item in evaluate(case, timeline)}
        self.assertEqual(results['barge_in_stop_latency_ms']['value'], 200)
        self.assertIsNone(results['barge_in_success']['value'])

    def test_real_import_requires_verified_artifact_root(self):
        self.timeline['execution_kind'] = 'imported'
        for metric in self.results().values():
            self.assertEqual(metric['status'], 'insufficient_evidence')
            self.assertIn('Artifact root', metric['reason'])

    def test_wrong_case_version_rejected(self):
        self.case['case_version'] = '9.0.0'
        with self.assertRaisesRegex(ValueError, 'identity/version'):
            self.results()

    def test_timing_uncertainty_blocks_threshold(self):
        self.case['expected']['assertions'].append(dict(assertion_id='LAT', evaluator='deterministic',
            metric_id='e2e_first_audio_latency_ms', operator='lte', value=1385))
        self.timeline['tracks'][1]['sync']['uncertainty_ms'] = 10
        self.assertEqual(self.results()['e2e_first_audio_latency_ms']['status'], 'insufficient_evidence')

    def test_aggregation_preserves_counts_and_rejects_duplicates(self):
        sample = self.results()['e2e_first_audio_latency_ms']
        second = copy.deepcopy(sample)
        second.update(metric_id='other', value=None, status='insufficient_evidence', reason='missing')
        second['aggregation'].update(sample_count=0, excluded_count=1)
        result = aggregate_latency([sample, second])
        self.assertEqual(result['P95'], 1380)
        self.assertEqual((result['sample_count'], result['excluded_count']), (1, 1))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            aggregate_latency([sample, sample])

    def test_external_asr_cannot_be_device_CER(self):
        self.case['metrics'].append('asr_cer')
        self.case['expected']['reference_text'] = '你好'
        self.assertEqual(self.results()['asr_cer']['status'], 'insufficient_evidence')

    def test_asr_estimates_not_promoted_to_acoustic_boundaries(self):
        self.timeline['events'][0]['source'] = 'asr'
        self.assertEqual(self.results()['e2e_first_audio_latency_ms']['status'], 'insufficient_evidence')


if __name__ == '__main__':
    unittest.main()
