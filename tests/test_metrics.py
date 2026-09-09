import unittest

from aivoicebench.formulas import barge_success, character_error_rate, eligible_rate, latency_ms, overlap_ms, percentiles
from aivoicebench.validation import ROOT, load_document, metric_errors


class MetricContract(unittest.TestCase):
    def setUp(self):
        self.metric = load_document(ROOT / 'examples/metrics.example.json')
        self.timeline = load_document(ROOT / 'examples/timeline.example.json')

    def invalid(self, message):
        self.assertIn(message, '\n'.join(metric_errors(self.metric, self.timeline)))

    def test_examples(self):
        self.assertEqual(metric_errors(self.metric, self.timeline), [])
        self.assertEqual(metric_errors(load_document(ROOT / 'examples/metrics/insufficient.json'), load_document(ROOT / 'examples/timelines/blocked.json')), [])

    def test_no_fabricated_value_when_insufficient(self):
        self.metric.update(status='insufficient_evidence', reason='missing')
        self.invalid('null')

    def test_pass_requires_policy_and_consistency(self):
        self.metric['status'] = 'pass'
        self.invalid('threshold')
        self.metric['threshold'] = dict(operator='lte', value=1000, policy_id='example-only', policy_version='1.0.0', source='case')
        self.invalid('disagrees with threshold')
        self.metric['status'] = 'fail'
        self.assertEqual(metric_errors(self.metric, self.timeline), [])

    def test_evidence_required_for_measured_result(self):
        self.metric['evidence_ids'] = []
        self.invalid('non-empty')

    def test_unknown_refs(self):
        self.metric['evidence_ids'] = ['missing']
        self.invalid('unknown timeline evidence')

    def test_event_evidence_link(self):
        self.metric['evidence_ids'].pop()
        self.invalid('event evidence must be included')

    def test_sample_accounting(self):
        self.metric['aggregation']['total_count'] = 5
        self.invalid('must equal total_count')

    def test_percentile_metadata(self):
        self.metric['aggregation'].update(kind='percentile', algorithm='R7')
        self.invalid('required only for percentile')
        self.metric['aggregation']['percentile'] = 95
        self.invalid('link every eligible input')

    def test_boolean_rate_uses_ratio_and_input_references(self):
        self.metric.update(name='false_endpoint_rate', value=0.5, unit='ratio')
        self.metric['aggregation'] = dict(kind='rate', algorithm='eligible_ratio', sample_count=2,
                                          total_count=3, excluded_count=1, input_metric_ids=['m1', 'm2'])
        self.assertEqual(metric_errors(self.metric, self.timeline), [])
        self.metric['aggregation']['input_metric_ids'].pop()
        self.invalid('link every eligible input')

    def test_synthetic_not_measured(self):
        self.metric['execution_kind'] = 'hardware'
        self.invalid('execution_kind disagree')

    def test_internal_latency_cannot_use_blackbox(self):
        self.metric['name'] = 'internal_llm_latency_ms'
        self.invalid('white_box')
        self.metric['measurement_scope'] = 'white_box'
        self.invalid('requires device log evidence')

    def test_asr_cer_not_external_asr(self):
        self.metric.update(name='asr_cer', unit='ratio', value=0.1, method='ground_truth_comparison', measurement_scope='white_box')
        self.invalid('requires device log evidence')

    def test_cross_clock_uncalibrated(self):
        self.timeline['tracks'][1]['clock_id'] = 'other'
        self.timeline['tracks'][1]['sync'] = dict(status='uncalibrated', offset_ms=None, uncertainty_ms=None, max_drift_ppm=None, calibration_id=None)
        self.invalid('uncalibrated cross-clock')

    def test_llm_cannot_compute_numeric_timing(self):
        self.metric.update(method='llm_judge', judge_profile='judge-v1')
        self.invalid('deterministic')


class ReferenceFormulas(unittest.TestCase):
    def test_latency_and_overlap_precondition(self):
        self.assertEqual(latency_ms(4100, 5480), 1380)
        with self.assertRaises(ValueError):
            latency_ms(2000, 1900)

    def test_percentiles_R7(self):
        result = percentiles([100, 200, 300, 400, None])
        self.assertEqual(result['sample_count'], 4)
        self.assertEqual(result['excluded_count'], 1)
        for name, expected in {'P50': 250, 'P90': 370, 'P95': 385, 'P99': 397}.items():
            self.assertAlmostEqual(result[name], expected)

    def test_empty_single_and_invalid_percentiles(self):
        self.assertIsNone(percentiles([])['P95'])
        self.assertEqual(percentiles([12])['P99'], 12)
        for bad in [float('nan'), float('inf'), True]:
            with self.assertRaises(ValueError):
                percentiles([bad])

    def test_cer_and_empty_reference(self):
        self.assertEqual(character_error_rate('小米', '小明')['value'], 0.5)
        self.assertEqual(character_error_rate('猫', '小花猫')['value'], 2)
        self.assertIsNone(character_error_rate('', 'a')['value'])
        self.assertEqual(character_error_rate('é', 'e\u0301')['value'], 0)
        self.assertEqual(character_error_rate('AB 12', 'ab12')['edits'], 3)

    def test_overlap_union_no_double_count(self):
        self.assertEqual(overlap_ms([(0, 100), (50, 150)], [(75, 125), (100, 200)]), 75)
        self.assertEqual(overlap_ms([(0, 100)], [(100, 200)]), 0)
        self.assertEqual(overlap_ms([], [(0, 100)]), 0)
        with self.assertRaises(ValueError):
            overlap_ms([(10, 0)], [])

    def test_eligible_rate_and_denominator(self):
        result = eligible_rate([True, False, None])
        self.assertEqual(result['value'], 0.5)
        self.assertEqual(result['sample_count'], 2)
        self.assertEqual(result['excluded_count'], 1)
        self.assertIsNone(eligible_rate([None])['value'])
        with self.assertRaises(ValueError):
            eligible_rate([1])

    def test_barge_requires_both_components(self):
        self.assertTrue(barge_success(True, True))
        self.assertFalse(barge_success(True, False))
        self.assertIsNone(barge_success(True, None))
        self.assertIsNone(barge_success(False, None))


if __name__ == '__main__':
    unittest.main()
