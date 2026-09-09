import copy
import tempfile
import unittest
from pathlib import Path

from aivoicebench.__main__ import main
from aivoicebench.validation import ROOT, case_errors, load_document, schema_errors


class TestCaseContract(unittest.TestCase):
    def setUp(self):
        self.vad = load_document(ROOT / 'examples/test-case.example.yaml')
        self.barge = load_document(ROOT / 'examples/test-cases/barge-in.json')
        self.context = load_document(ROOT / 'examples/test-cases/context.json')
        self.explore = load_document(ROOT / 'examples/test-cases/exploratory.json')

    def invalid(self, case, message):
        self.assertIn(message, '\n'.join(case_errors(case)))

    def test_all_examples(self):
        paths = [ROOT / 'examples/test-case.example.yaml', *sorted((ROOT / 'examples/test-cases').glob('*.json'))]
        self.assertEqual(len(paths), 6)
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(case_errors(load_document(path)), [])

    def test_pause_exactly_800_ms(self):
        pause = self.vad['stimulus']['segments'][1]
        self.assertEqual(pause['sample_count'] * 1000 / pause['sample_rate_hz'], 800)

    def test_rejects_empty_stimulus_for_every_mode(self):
        for case in (self.vad, self.barge, self.context, self.explore):
            case['stimulus'] = {}
            self.invalid(case, 'required property')

    def test_modes_cannot_mix(self):
        self.vad['stimulus']['trigger'] = self.barge['stimulus']['trigger']
        self.invalid(self.vad, 'Additional properties')

    def test_required_interactive_trigger(self):
        del self.barge['stimulus']['trigger']
        self.invalid(self.barge, "'trigger' is a required property")

    def test_bad_trigger_event_and_offset(self):
        for key, value in [('event', 'tester_speech_end'), ('offset_ms', -1), ('offset_ms', 0.5), ('on_timeout', 'continue'), ('require_active_device_speech', False)]:
            case = copy.deepcopy(self.barge)
            case['stimulus']['trigger'][key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(schema_errors(case))

    def test_trigger_deadline(self):
        self.barge['stimulus']['trigger']['offset_ms'] = 15000
        self.invalid(self.barge, 'less than timeout_ms')

    def test_trigger_within_case_deadline(self):
        self.barge['case_timeout_ms'] = 100
        self.invalid(self.barge, 'exceeds case_timeout_ms')

    def test_no_live_tts_without_asset(self):
        del self.vad['stimulus']['segments'][0]['asset_id']
        self.invalid(self.vad, 'asset_id')

    def test_bad_segment(self):
        for segment in [{'type': 'unknown'}, {'type': 'silence', 'duration_ms': 800}, {'type': 'silence', 'sample_count': 1.1, 'sample_rate_hz': 16000}]:
            case = copy.deepcopy(self.vad)
            case['stimulus']['segments'][1] = segment
            self.assertTrue(schema_errors(case))

    def test_no_undeclared_asset(self):
        self.vad['stimulus']['segments'][0]['asset_id'] = 'missing'
        self.invalid(self.vad, 'undeclared asset missing')

    def test_duplicate_assets(self):
        self.vad['stimulus']['assets'].append(self.vad['stimulus']['assets'][0])
        self.invalid(self.vad, 'asset_id must be unique')

    def test_no_silence_only_stimulus(self):
        self.vad['stimulus']['segments'] = [self.vad['stimulus']['segments'][1]]
        self.invalid(self.vad, 'at least one audio')

    def test_asset_paths_stay_local(self):
        for path in ['../secret.wav', 'C:/audio.wav', '/tmp/audio.wav', '..\\secret.wav', '\\\\server\\audio.wav', 'audio.wav:stream']:
            self.vad['stimulus']['assets'][0]['path'] = path
            self.invalid(self.vad, 'relative path')

    def test_asset_format_and_hash(self):
        self.vad['stimulus']['assets'][0]['format']['channels'] = 2
        self.invalid(self.vad, '1 was expected')
        self.vad['stimulus']['assets'][0]['sha256'] = 'bad'
        self.invalid(self.vad, 'does not match')

    def test_unique_turn_ids(self):
        self.context['stimulus']['turns'][1]['turn_id'] = 'turn_1'
        self.invalid(self.context, 'turn_id must be unique')

    def test_at_least_two_turns(self):
        self.context['stimulus']['turns'].pop()
        self.invalid(self.context, 'too short')

    def test_turn_deadline(self):
        self.context['case_timeout_ms'] = 100
        self.invalid(self.context, 'exceeds case_timeout_ms')

    def test_exploration_bounded_and_not_golden(self):
        self.explore['stimulus']['agent']['max_duration_ms'] = 60001
        self.invalid(self.explore, 'exceeds case_timeout_ms')
        self.explore['golden_set'] = {'set_id': 'golden', 'version': '1.0.0'}
        self.assertTrue(schema_errors(self.explore))

    def test_expected_not_ad_hoc(self):
        self.vad['expected'] = {'device_must_not_speak': True}
        self.invalid(self.vad, 'assertions')

    def test_assertions_link_metrics(self):
        self.vad['expected']['assertions'][0]['metric_id'] = 'missing'
        self.invalid(self.vad, 'must appear in metrics')

    def test_assertion_ids_unique(self):
        self.vad['expected']['assertions'] *= 2
        self.invalid(self.vad, 'assertion_id must be unique')

    def test_ordered_comparison_is_numeric(self):
        self.vad['expected']['assertions'][0]['operator'] = 'lte'
        self.invalid(self.vad, 'requires a number')

    def test_versions_are_explicit(self):
        self.vad['schema_version'] = '1.0.0'
        self.invalid(self.vad, '2.0.0')

    def test_duplicate_yaml_keys_and_unsafe_tags(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.yaml'
            path.write_text('mode: fixed_audio\nmode: interactive\n')
            with self.assertRaisesRegex(ValueError, 'Duplicate key'):
                load_document(path)
            path.write_text('!!python/object/apply:os.system [echo unsafe]')
            with self.assertRaises(yaml.YAMLError):
                load_document(path)

    def test_cli_invalid_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text('{}')
            self.assertEqual(main(['validate', str(path)]), 1)


if __name__ == '__main__':
    unittest.main()
