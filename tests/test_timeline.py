import copy
import unittest

from aivoicebench.validation import ROOT, load_document, schema_errors, timeline_errors


class TimelineContract(unittest.TestCase):
    def setUp(self):
        self.timeline = load_document(ROOT / 'examples/timeline.example.json')

    def invalid(self, message):
        self.assertIn(message, '\n'.join(timeline_errors(self.timeline)))

    def test_examples(self):
        for path in [ROOT / 'examples/timeline.example.json', *sorted((ROOT / 'examples/timelines').glob('*.json'))]:
            with self.subTest(path=path):
                self.assertEqual(timeline_errors(load_document(path)), [])

    def test_no_evidence_is_explicitly_blocked(self):
        self.timeline.update(status='blocked', events=[], evidence=[], artifacts=[], tracks=[], gaps=[{
            'reason': 'Capture unavailable', 'required_evidence': 'Device output', 'start_ms': 0, 'end_ms': 0}])
        self.assertEqual(timeline_errors(self.timeline), [])
        self.timeline['gaps'] = []
        self.invalid('should be non-empty')

    def test_dangling_references(self):
        self.timeline['events'][0]['evidence_ids'] = ['missing']
        self.invalid('unknown evidence_id')
        self.timeline['evidence'][0]['artifact_id'] = 'missing'
        self.invalid('unknown artifact_id')

    def test_evidence_requires_time(self):
        del self.timeline['evidence'][0]['start_ms']
        self.invalid('start_ms')

    def test_time_inversions_and_artifact_bounds(self):
        self.timeline['evidence'][0]['end_ms'] = 999
        self.invalid('end_ms precedes start_ms')
        self.timeline['evidence'][0]['end_ms'] = 30000
        self.invalid('outside artifact')

    def test_evidence_must_cover_event(self):
        self.timeline['events'][0].update(start_ms=500, end_ms=500)
        self.invalid('cover the event interval')

    def test_track_mismatch(self):
        self.timeline['evidence'][0]['track_id'] = 'device_output'
        self.invalid('track_id disagrees')

    def test_ids_unique(self):
        event = copy.deepcopy(self.timeline['events'][0])
        event['confidence'] = 0.5
        self.timeline['events'].append(event)
        self.invalid('event_id must be unique')

    def test_run_identity(self):
        self.timeline['events'][0]['run_id'] = 'other'
        self.invalid('identity mismatch')

    def test_ordering(self):
        self.timeline['events'].reverse()
        self.invalid('nondecreasing')

    def test_tied_timestamps_allowed(self):
        self.assertEqual(self.timeline['events'][1]['start_ms'], self.timeline['events'][2]['start_ms'])
        self.assertEqual(timeline_errors(self.timeline), [])

    def test_end_needs_start_in_complete_timeline(self):
        self.timeline['events'].pop(0)
        self.invalid('end has no associated start')

    def test_open_interval_needs_partial_gap(self):
        self.timeline['events'].pop()
        self.invalid('unclosed intervals')
        self.timeline['status'] = 'partial'
        self.timeline['gaps'] = [{'reason': 'Capture ended', 'required_evidence': 'Response end', 'start_ms': 7500, 'end_ms': 7500}]
        self.assertEqual(timeline_errors(self.timeline), [])

    def test_response_association_required(self):
        self.timeline['events'][-1]['response_id'] = None
        self.invalid('response_id')

    def test_confidence_not_defaulted(self):
        del self.timeline['events'][0]['confidence']
        self.invalid('confidence')

    def test_whitebox_cannot_be_audio_source(self):
        self.timeline['events'][0]['observation_scope'] = 'white_box'
        self.invalid('device_log')

    def test_device_log_requires_log_artifact(self):
        self.timeline['events'][0].update(source='device_log', observation_scope='white_box')
        self.invalid('device log evidence')
        self.timeline['evidence'][0]['source'] = 'device_log'
        self.invalid('requires device_log artifact')

    def test_synthetic_cannot_be_hardware(self):
        self.timeline['execution_kind'] = 'hardware'
        self.invalid('synthetic artifact')
        self.invalid('synthetic calibration')

    def test_uncalibrated_is_representable(self):
        self.timeline['tracks'][0]['sync'] = dict(status='uncalibrated', offset_ms=None, uncertainty_ms=None, max_drift_ppm=None, calibration_id=None)
        self.assertEqual(timeline_errors(self.timeline), [])

    def test_hardware_needs_snapshot(self):
        self.timeline['execution_kind'] = 'hardware'
        self.timeline['run_snapshot']['firmware'] = None
        self.invalid('firmware')

    def test_nonfinite_values_rejected(self):
        self.timeline['events'][0]['start_ms'] = float('nan')
        self.invalid('finite')

    def test_custom_and_asr_payload_requirements(self):
        event = self.timeline['events'][0]
        event['type'] = 'custom'
        self.assertTrue(schema_errors(event, 'event'))
        event['payload'] = {'custom_name': 'annotation'}
        self.assertEqual(schema_errors(event, 'event'), [])
        event['type'] = 'asr_segment'
        self.assertTrue(schema_errors(event, 'event'))


if __name__ == '__main__':
    unittest.main()
