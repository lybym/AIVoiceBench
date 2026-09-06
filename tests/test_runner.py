from pathlib import Path
import tempfile
import unittest
import wave

from aivoicebench.runner import digest, inspect_audio, run_case, run_input, write_json
from aivoicebench.validation import ROOT, load_document, metric_errors, schema_errors, timeline_errors


class LocalPreparationRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = load_document(ROOT / 'examples/test-case.example.yaml')
        self.case['repeat'] = 1
        self.case_path = self.root / 'case.json'
        self.output = self.root / 'runs'
        for index, asset in enumerate(self.case['stimulus']['assets']):
            path = self.root / asset['path']
            path.parent.mkdir(exist_ok=True)
            # Test-only PCM, not human speech or captured HIL evidence.
            with wave.open(str(path), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes((1000 + index).to_bytes(2, 'little', signed=True) * 1600)
            asset['sha256'] = digest(path)
        write_json(self.case_path, self.case)

    def test_exact_composition_and_normalized_outputs(self):
        directory, manifest = run_case(self.case_path, self.output, dry_run=True)
        self.assertEqual(manifest['status'], 'prepared')
        self.assertEqual(schema_errors(manifest, 'run-manifest'), [])
        with wave.open(str(directory / 'stimulus.wav'), 'rb') as audio:
            self.assertEqual(audio.getnframes(), 16000)
            raw = audio.readframes(16000)
        self.assertEqual(raw[3200:28800], bytes(25600))
        self.assertEqual(raw[:3200], (1000).to_bytes(2, 'little', signed=True) * 1600)
        self.assertEqual(raw[28800:], (1001).to_bytes(2, 'little', signed=True) * 1600)
        timeline = load_document(directory / 'timeline.json')
        self.assertEqual(timeline_errors(timeline), [])
        self.assertEqual(timeline['events'], [])
        self.assertEqual(timeline['execution_kind'], 'dry_run')
        for metric in load_document(directory / 'metrics.json'):
            self.assertEqual(metric_errors(metric, timeline), [])
            self.assertIsNone(metric['value'])
        self.assertEqual(load_document(directory / 'findings.json'), [])
        for artifact in manifest['artifacts']:
            self.assertEqual(digest(directory / artifact['path']), artifact['sha256'])

    def test_missing_assets_recorded_as_blocked(self):
        self.case['stimulus']['assets'][0]['path'] = 'missing.wav'
        write_json(self.case_path, self.case)
        directory, manifest = run_case(self.case_path, self.output, dry_run=True)
        self.assertEqual(manifest['status'], 'blocked')
        self.assertIn('Missing file', '\n'.join(manifest['blockers']))
        self.assertFalse((directory / 'stimulus.wav').exists())

    def test_hash_mismatch(self):
        self.case['stimulus']['assets'][0]['sha256'] = '0' * 64
        write_json(self.case_path, self.case)
        _, manifest = run_case(self.case_path, self.output, dry_run=True)
        self.assertIn('SHA-256 differs', '\n'.join(manifest['blockers']))

    def test_hardware_mode_does_not_fake_capture(self):
        _, manifest = run_case(self.case_path, self.output)
        self.assertEqual(manifest['requested_execution'], 'hardware')
        self.assertEqual(manifest['execution_kind'], 'dry_run')
        self.assertEqual(manifest['status'], 'blocked')
        self.assertIn('no playback or recording', '\n'.join(manifest['blockers']))

    def test_repeated_runs_do_not_overwrite(self):
        first, _ = run_case(self.case_path, self.output, dry_run=True)
        previous = (first / 'manifest.json').read_bytes()
        second, _ = run_case(self.case_path, self.output, dry_run=True)
        self.assertNotEqual(first, second)
        self.assertEqual((first / 'manifest.json').read_bytes(), previous)

    def test_suite_and_repeat(self):
        self.case['repeat'] = 2
        write_json(self.case_path, self.case)
        suite = self.root / 'suite.json'
        write_json(suite, dict(schema_version='1.0.0', suite_id='TEST', suite_version='1.0.0', cases=['case.json']))
        result = run_input(suite, self.output, dry_run=True)
        self.assertEqual([manifest['attempt'] for _, manifest in result], [1, 2])

    def test_invalid_suite_before_side_effects(self):
        bad = self.root / 'bad.json'
        bad.write_text('{}')
        suite = self.root / 'suite.json'
        write_json(suite, dict(schema_version='1.0.0', suite_id='TEST', suite_version='1.0.0', cases=['case.json', 'bad.json']))
        with self.assertRaises(ValueError):
            run_input(suite, self.output, dry_run=True)
        self.assertFalse(self.output.exists())

    def test_path_escape_rejected(self):
        self.case['stimulus']['assets'][0]['path'] = '../private.wav'
        write_json(self.case_path, self.case)
        with self.assertRaisesRegex(ValueError, 'relative path'):
            run_case(self.case_path, self.output, dry_run=True)
        self.assertFalse(self.output.exists())

    def test_bad_format_and_truncated_audio(self):
        path = self.root / 'bad.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(bytes(6400))
        with self.assertRaisesRegex(ValueError, 'mono'):
            inspect_audio(path, 1000)
        source = self.root / self.case['stimulus']['assets'][0]['path']
        path.write_bytes(source.read_bytes()[:-10])
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            inspect_audio(path, 1000)

    def test_composed_duration_bounded(self):
        self.case['stimulus']['segments'][1]['sample_count'] = 16000 * 61
        write_json(self.case_path, self.case)
        directory, manifest = run_case(self.case_path, self.output, dry_run=True)
        self.assertIn('exceeds case_timeout', '\n'.join(manifest['blockers']))
        self.assertFalse((directory / 'stimulus.wav').exists())

    def test_qa_numeric_values(self):
        checks, _ = inspect_audio(self.root / self.case['stimulus']['assets'][0]['path'], 1000)
        self.assertEqual(checks['sample_count'], 1600)
        self.assertEqual(checks['duration_ms'], 100)
        self.assertEqual(checks['peak'], 1000 / 32768)
        self.assertEqual(checks['rms'], 1000 / 32768)
        self.assertEqual(checks['dc_offset'], 1000 / 32768)
        self.assertEqual(checks['clipped_samples'], 0)

    def test_unknown_metric_no_partial_directory(self):
        self.case['metrics'].append('unsupported')
        write_json(self.case_path, self.case)
        with self.assertRaisesRegex(ValueError, 'Unsupported metric'):
            run_case(self.case_path, self.output, dry_run=True)
        self.assertFalse(self.output.exists())

    def test_device_profile_rejects_unrelated_config(self):
        with self.assertRaisesRegex(ValueError, 'Device profile accepts only'):
            run_case(self.case_path, self.output, dry_run=True, device_profile={'unrelated': 'value'})
        self.assertFalse(self.output.exists())

    def test_endurance_preparation_is_bounded(self):
        self.case['case_timeout_ms'] = 1000000000
        write_json(self.case_path, self.case)
        _, manifest = run_case(self.case_path, self.output, dry_run=True)
        self.assertIn('future streaming adapter', '\n'.join(manifest['blockers']))

    def test_cli_codes_and_outputs(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from aivoicebench.__main__ import main
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(['run', str(self.case_path), '--dry-run', '--output', str(self.output)]), 0)
            self.assertEqual(main(['run', str(self.case_path), '--output', str(self.output)]), 2)
        self.assertIn('no hardware measurement', output.getvalue())


if __name__ == '__main__':
    unittest.main()
