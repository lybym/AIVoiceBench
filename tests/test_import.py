"""Synthetic media fixtures exercise real codecs; none represent a tested terminal."""

from array import array
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from aivoicebench.asr import ProviderOutput, normalize_vosk, prepare_channel
from aivoicebench.audio_processing import AudioProcessingError, FFmpegAudioProcessor, canonical_qa
from aivoicebench.import_artifacts import ImportRun, recording_run_errors
from aivoicebench.import_pipeline import import_recording
from aivoicebench.runner import digest
from aivoicebench.validation import ROOT, load_document, schema_errors, transcript_errors


def tone(path, seconds=1, rate=16000, channels=1):
    samples = array('h', (int(4000 * math.sin(2 * math.pi * 440 * index / rate))
                         for index in range(rate)))
    if channels == 2:
        samples = array('h', (value for x in samples for value in (x, x // 2)))
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        for _ in range(seconds):
            output.writeframes(samples.tobytes())


class FakeASR:
    def transcribe(self, source):
        return ProviderOutput({'provider': 'synthetic-fixture', 'library_version': 'test',
            'model_id': 'synthetic', 'model_version': 'test', 'model_sha256': '0' * 64,
            'config': {'synthetic': True}}, [json.dumps({'text': '你好', 'result': [
                {'word': '你好', 'start': 0.1, 'end': 0.2, 'conf': 0.8}]})])

    def normalize(self, messages, duration):
        return normalize_vosk(messages, duration)


class ImportFailureAndContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_source_is_durable_failed_run(self):
        directory, manifest = import_recording(self.root / 'missing.wav', self.root / 'runs')
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['stages']['ingestion']['status'], 'failed')
        self.assertEqual(manifest['stages']['report']['status'], 'complete')
        self.assertTrue((directory / 'analysis' / manifest['analysis_id'] / 'report.md').exists())
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def test_reject_profile_secrets_before_side_effects(self):
        with self.assertRaises(ValueError):
            import_recording('missing.wav', self.root / 'runs', profile={'api_key': 'canary-secret'})
        self.assertFalse((self.root / 'runs').exists())

    def test_missing_converter_keeps_original_and_failure_outputs(self):
        path = self.root / 'test.wav'
        tone(path)
        directory, manifest = import_recording(path, self.root / 'runs',
            audio_processor=FFmpegAudioProcessor(ffmpeg='does-not-exist-1234'), synthetic=True)
        self.assertEqual(manifest['stages']['ingestion']['status'], 'complete')
        self.assertEqual(manifest['stages']['normalization']['status'], 'failed')
        self.assertEqual(digest(directory / 'original/source.wav'), digest(path))
        self.assertEqual(manifest['stages']['timeline']['status'], 'insufficient_evidence')
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def test_unknown_extension_retains_failure_report(self):
        path = self.root / 'not-a-recording.txt'
        path.write_text('x')
        directory, manifest = import_recording(path, self.root / 'runs')
        self.assertEqual(manifest['status'], 'failed')
        self.assertTrue((directory / 'analysis' / manifest['analysis_id'] / 'report.json').exists())

    def test_outputs_are_immutable(self):
        run = ImportRun(self.root / 'runs', synthetic=True)
        run.envelope('timeline', 'pending', 'Awaiting automatic events')
        with self.assertRaisesRegex(ValueError, 'immutable'):
            run.envelope('timeline', 'complete', 'not actually run', {})

    def test_missing_evidence_cannot_have_fabricated_data(self):
        run = ImportRun(self.root / 'runs', synthetic=True)
        with self.assertRaisesRegex(ValueError, 'Invalid analysis'):
            run.envelope('metrics', 'insufficient_evidence', 'missing', [{'value': 0}])

    def test_preserve_legacy_run_schema(self):
        legacy = json.loads((ROOT / 'schemas/run-manifest.schema.json').read_text())
        self.assertEqual(legacy['properties']['requested_execution']['enum'], ['dry_run', 'hardware'])
        self.assertEqual(legacy['properties']['execution_kind']['const'], 'dry_run')

    def test_manifest_references_reject_missing_parent(self):
        run = ImportRun(self.root / 'runs')
        path = run.analysis / 'test.txt'
        path.write_text('evidence')
        with self.assertRaisesRegex(ValueError, 'Unknown parent'):
            run.register(path, 'test', ['missing'])
        run.register(path, 'test')
        run.manifest['artifacts'][0]['parent_artifact_ids'] = [run.manifest['artifacts'][0]['artifact_id']]
        self.assertTrue(recording_run_errors(run.manifest, run.directory))

    def test_run_view_reports_no_qa_for_a_run_that_never_measured_audio(self):
        """A Run without QA must say so instead of implying zero measurements."""
        from fastapi.testclient import TestClient
        from aivoicebench import api
        root = self.root / 'api-runs'
        source = self.root / 'missing.wav'  # ingestion fails before any canonical audio exists
        directory, manifest = import_recording(source, root)
        with patch.object(api, 'OUTPUT_ROOT', root):
            with TestClient(api.app) as client:
                view = client.get('/api/runs/' + manifest['run_id']).json()
        self.assertEqual(manifest['stages']['audio_qa']['status'], 'insufficient_evidence')
        self.assertEqual(view['audio_qa'], {})
        self.assertTrue((directory / 'analysis' / manifest['analysis_id'] / 'audio-qa.json').is_file())

    def test_empty_media_is_a_durable_failed_run_not_a_missing_run(self):
        """A zero-byte recording keeps its Run and names the refusal reason."""
        for suffix in ('.wav', '.mp3', '.m4a'):
            with self.subTest(suffix=suffix):
                source = self.root / ('empty' + suffix)
                source.write_bytes(b'')
                directory, manifest = import_recording(source, self.root / ('runs' + suffix), synthetic=True)
                self.assertTrue(directory.is_dir(), 'the refused import must still produce a Run')
                self.assertEqual(manifest['status'], 'failed')
                self.assertEqual(manifest['stages']['ingestion']['status'], 'failed')
                self.assertIn('nonempty', manifest['stages']['ingestion']['reason'])
                self.assertIsNone(manifest['original_sha256'])
                self.assertEqual([a for a in manifest['artifacts'] if a['kind'] == 'original_recording'], [])
                self.assertEqual(manifest['stages']['audio_qa']['status'], 'insufficient_evidence')
                # The reason and the report survive on disk, so a restart can read them.
                stored = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                self.assertIn('nonempty', stored['stages']['ingestion']['reason'])
                self.assertTrue((directory / 'analysis' / manifest['analysis_id'] / 'report.md').is_file())
                self.assertEqual(recording_run_errors(manifest, directory), [])

    def test_unsupported_extension_is_named_before_any_original_is_retained(self):
        """An unsupported container must be named in the Run, not become an empty success."""
        unsupported = self.root / 'notes.txt'
        unsupported.write_text('not audio')
        directory, manifest = import_recording(unsupported, self.root / 'unsupported-runs')
        self.assertEqual(manifest['status'], 'failed')
        self.assertEqual(manifest['stages']['ingestion']['status'], 'failed')
        self.assertIn('WAV, MP3 and M4A', manifest['stages']['ingestion']['reason'])
        self.assertIsNone(manifest['original_sha256'])
        self.assertEqual([a for a in manifest['artifacts'] if a['kind'] == 'original_recording'], [])
        stored = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertIn('WAV, MP3 and M4A', stored['stages']['ingestion']['reason'])
        self.assertTrue((directory / 'analysis' / manifest['analysis_id'] / 'report.md').is_file())
        self.assertEqual(recording_run_errors(manifest, directory), [])


class ActualCodecImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg, cls.ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not cls.ffmpeg or not cls.ffprobe:
            if os.environ.get('AIVOICEBENCH_REQUIRE_MEDIA_TESTS') == '1':
                raise RuntimeError('CI requires real FFmpeg/FFprobe codec tests')
            raise unittest.SkipTest('FFmpeg/FFprobe not installed; actual codec tests not performed')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='导入 space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '测试 & source.wav'
        tone(self.source)

    def _import(self, source=None, output=None, **kwargs):
        return import_recording(source or self.source, output or (self.root / 'runs'), synthetic=True, **kwargs)

    def _encode(self, suffix):
        target = self.root / ('codec' + suffix)
        subprocess.run([self.ffmpeg, '-nostdin', '-v', 'error', '-i', str(self.source),
                        '-c:a', 'libmp3lame' if suffix == '.mp3' else 'aac', '-n', str(target)],
                       check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return target

    def test_real_wav_mp3_m4a_conversion_and_provenance(self):
        tone(self.source, rate=44100, channels=2)
        for path in [self.source, self._encode('.mp3'), self._encode('.m4a')]:
            with self.subTest(format=path.suffix):
                before = digest(path)
                directory, manifest = self._import(path)
                self.assertEqual(manifest['stages']['normalization']['status'], 'complete')
                self.assertEqual(digest(path), before)
                self.assertEqual(manifest['original_sha256'], before)
                self.assertEqual(recording_run_errors(manifest, directory), [])
                original = next(x for x in manifest['artifacts'] if x['kind'] == 'original_recording')
                normalized = next(x for x in manifest['artifacts'] if x['kind'] == 'normalized_audio')
                self.assertEqual(normalized['parent_artifact_ids'], [original['artifact_id']])
                metadata_path = next(x for x in manifest['artifacts'] if x['kind'] == 'audio_metadata')
                metadata = load_document(directory / metadata_path['path'])
                self.assertEqual(metadata['source']['sample_rate_hz'], 44100)
                self.assertEqual(metadata['source']['channels'], 2)
                self.assertEqual(metadata['normalized']['sample_rate_hz'], 16000)
                self.assertEqual(metadata['normalized']['channels'], 1)
                self.assertEqual(metadata['mapping']['source_alignment_status'], 'needs_review')
                self.assertIsNone(metadata['mapping']['source_alignment_uncertainty_ms'])
                self.assertLess(abs(metadata['normalized']['duration_ms'] - 1000), 100)
                self.assertFalse(metadata['processor']['config']['network_protocols_enabled'])

    def test_canonical_waveform_preserves_samples_without_gain_or_trimming(self):
        directory, manifest = self._import()
        path = next(x['path'] for x in manifest['artifacts'] if x['kind'] == 'normalized_audio')
        with wave.open(str(self.source), 'rb') as old, wave.open(str(directory / path), 'rb') as new:
            self.assertEqual(old.readframes(16000), new.readframes(16000))

    def test_20_minute_conversion_and_asr_preparation(self):
        tone(self.source, seconds=1200)
        directory, manifest = self._import()
        self.assertEqual(manifest['stages']['normalization']['status'], 'complete')
        artifact = next(x for x in manifest['artifacts'] if x['kind'] == 'normalized_audio')
        qa = canonical_qa(directory / artifact['path'])
        self.assertEqual(qa['sample_count'], 1200 * 16000)
        duration, channel = prepare_channel(directory / artifact['path'], self.root / 'asr-prepared.wav',
                                             max_duration_ms=1800000)
        self.assertEqual((duration, channel), (1200000, 1))

    def test_asr_is_bound_by_envelope_without_fabricated_case(self):
        directory, manifest = self._import(asr_provider_factory=FakeASR)
        self.assertIsNone(manifest['case_ref'])
        self.assertEqual(manifest['stages']['asr']['status'], 'complete')
        artifact = next(x for x in manifest['artifacts'] if x['kind'] == 'transcript')
        value = load_document(directory / artifact['path'])
        self.assertEqual(value['run_id'], manifest['run_id'])
        self.assertIn(value['data_artifact_ref'], value['artifact_refs'])
        self.assertEqual(transcript_errors(value['data']), [])
        segment = value['data']['segments'][0]
        self.assertIsNone(segment['speaker_id'])
        self.assertIsNone(segment['timestamp_confidence'])
        timeline = next(x for x in manifest['artifacts'] if x['kind'] == 'timeline')
        self.assertIsNone(load_document(directory / timeline['path'])['data'])

    def test_failed_asr_does_not_destroy_run_or_leak_exception_secret(self):
        class BrokenASR(FakeASR):
            def transcribe(self, source):
                raise RuntimeError('canary-api-secret')
        directory, manifest = self._import(asr_provider_factory=BrokenASR)
        self.assertEqual(manifest['stages']['asr']['status'], 'failed')
        self.assertEqual(manifest['stages']['normalization']['status'], 'complete')
        self.assertEqual(manifest['stages']['report']['status'], 'complete')
        for path in directory.rglob('*.json'):
            self.assertNotIn('canary-api-secret', path.read_text(encoding='utf-8'))
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def test_corrupt_source_preserved_and_failed_converter_reported(self):
        self.source.write_bytes(b'RIFFbroken-format')
        directory, manifest = self._import()
        self.assertEqual(manifest['stages']['normalization']['status'], 'failed')
        self.assertEqual((directory / 'original/source.wav').read_bytes(), self.source.read_bytes())
        self.assertTrue(manifest['stages']['normalization']['output_artifact_ids'])
        self.assertEqual(manifest['stages']['report']['status'], 'complete')

    def test_truncated_wav_is_not_success(self):
        raw = self.source.read_bytes()
        self.source.write_bytes(raw[:-10000])
        _, manifest = self._import()
        self.assertEqual(manifest['stages']['normalization']['status'], 'failed')

    def test_reimport_does_not_overwrite_previous_analysis(self):
        first, a = self._import()
        old = (first / 'manifest.json').read_bytes()
        second, b = self._import()
        self.assertNotEqual(first, second)
        self.assertEqual(a['original_sha256'], b['original_sha256'])
        self.assertNotEqual(a['analysis_id'], b['analysis_id'])
        self.assertEqual((first / 'manifest.json').read_bytes(), old)

    def test_no_asr_does_not_claim_empty_success_or_findings(self):
        directory, manifest = self._import(profile={'device': 'A|<script>alert(1)</script>', 'supplier': '测试供应商'})
        self.assertEqual(manifest['status'], 'partial')
        for kind in ('transcript', 'timeline', 'metrics', 'judge-results', 'findings'):
            path = next(x for x in manifest['artifacts'] if x['kind'] == kind)
            value = load_document(directory / path['path'])
            self.assertEqual(schema_errors(value, 'analysis-output'), [])
            self.assertIsNone(value['data'])
        report = load_document(directory / 'analysis' / manifest['analysis_id'] / 'report.json')
        self.assertEqual(report['device_performance'], 'insufficient_evidence')
        self.assertEqual(report['conclusions'], [])
        self.assertNotIn('<script>', (directory / 'analysis' / manifest['analysis_id'] / 'report.md').read_text(encoding='utf-8'))

    def test_manifest_validator_detects_tamper_and_path_escape(self):
        directory, manifest = self._import()
        original = next(x for x in manifest['artifacts'] if x['kind'] == 'original_recording')
        (directory / original['path']).write_bytes(b'changed')
        self.assertTrue(recording_run_errors(manifest, directory))
        original['path'] = '../outside.wav'
        self.assertTrue(recording_run_errors(manifest, directory))

    def test_unsupported_channel_layout_with_a_supported_extension_is_a_persisted_failure(self):
        """Media the import cannot map must fail inside the Run, not disappear."""
        target = self.root / 'multichannel.wav'
        with wave.open(str(target), 'wb') as output:
            output.setparams((6, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\x10\x00' * 16000 * 6)
        directory, manifest = self._import(target)
        self.assertEqual(manifest['stages']['ingestion']['status'], 'complete')
        self.assertEqual(manifest['stages']['normalization']['status'], 'failed')
        self.assertIn('mono/stereo', manifest['stages']['normalization']['reason'])
        # The original stays byte-identical and the verdict is readable after a restart.
        self.assertEqual((directory / 'original/source.wav').read_bytes(), target.read_bytes())
        stored = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['stages']['normalization']['reason'],
                         manifest['stages']['normalization']['reason'])
        self.assertEqual(manifest['stages']['report']['status'], 'complete')
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def _silence(self, seconds=2, rate=16000, channels=1):
        path = self.root / 'silence.wav'
        with wave.open(str(path), 'wb') as output:
            output.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
            for _ in range(seconds):
                output.writeframes(b'\x00\x00' * rate * channels)
        return path

    def _qa(self, directory, manifest, kind='audio-qa'):
        artifact = next(x for x in manifest['artifacts'] if x['kind'] == kind)
        return load_document(directory / artifact['path'])

    def test_audio_qa_reports_conditions_without_claiming_accuracy(self):
        directory, manifest = self._import()
        envelope = self._qa(directory, manifest)
        self.assertEqual(schema_errors(envelope, 'analysis-output'), [])
        self.assertEqual(envelope['status'], 'complete')
        self.assertIn('no acceptance threshold configured', envelope['reason'])
        self.assertIn('no recognition or measurement accuracy is claimed', envelope['reason'])
        data = envelope['data']
        conditions = {item['condition_id']: item for item in data['conditions']}
        self.assertEqual(conditions['decodable_canonical_audio']['status'], 'met')
        self.assertEqual(conditions['nonempty_signal']['status'], 'unassessed')
        self.assertIn('not a quality, accuracy or acceptance result',
                      conditions['nonempty_signal']['limitation'])
        self.assertFalse(any(item['status'] == 'pass' for item in data['conditions']))
        # Format/duration/channel/sample-rate facts are present and are not a verdict.
        self.assertEqual((data['container'], data['encoding'], data['channels'], data['sample_rate_hz']),
                         ('wav', 'PCM_S16LE', 1, 16000))
        self.assertGreater(data['duration_ms'], 0)
        self.assertFalse(data['all_silent'])
        # The envelope points at the canonical measurement document it reports on.
        self.assertEqual(envelope['data_artifact_ref'],
                         next(x['artifact_id'] for x in manifest['artifacts'] if x['kind'] == 'normalized_audio'))

    def test_silent_recording_abstains_in_qa_and_keeps_its_measurements(self):
        directory, manifest = self._import(self._silence())
        stage = manifest['stages']['audio_qa']
        self.assertIn('nonempty_signal', stage['reason'])
        envelope = self._qa(directory, manifest)
        self.assertEqual(envelope['status'], 'insufficient_evidence')
        self.assertIsNone(envelope['data'], 'abstaining QA must not present measurements as a result')
        # The measurements are still published as their own registered document.
        conditions_artifact = next(x for x in manifest['artifacts'] if x['kind'] == 'audio_qa_conditions')
        self.assertIn(conditions_artifact['artifact_id'], envelope['artifact_refs'])
        published = load_document(directory / conditions_artifact['path'])
        self.assertEqual(published['status'], 'insufficient_evidence')
        self.assertEqual(published['reason'], envelope['reason'])
        self.assertEqual(published['recording_sha256'], manifest['original_sha256'])
        self.assertTrue(published['measurements']['all_silent'])
        self.assertEqual(published['measurements']['peak'], 0)
        self.assertEqual(published['measurements']['rms'], 0)
        self.assertEqual(published['measurements']['conditions'], self._measurements(
            directory, manifest)['conditions'])
        # Silence is not a crash: ingestion, the report and the Run integrity all survive.
        self.assertEqual(manifest['status'], 'partial')
        self.assertEqual(manifest['stages']['ingestion']['status'], 'complete')
        self.assertEqual(manifest['stages']['report']['status'], 'complete')
        self.assertTrue((directory / 'analysis' / manifest['analysis_id'] / 'report.md').is_file())
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def _measurements(self, directory, manifest):
        metadata = next(x for x in manifest['artifacts'] if x['kind'] == 'audio_metadata')
        return load_document(directory / metadata['path'])['normalized']

    def test_run_stays_readable_after_a_restart_from_disk_alone(self):
        """Only the persisted Run is reused: no in-memory process state must be required."""
        runs = self.root / 'restart-runs'
        directory, manifest = self._import(output=runs)
        del directory, manifest  # a restart keeps nothing but the files on disk
        directories = sorted(path for path in runs.iterdir() if path.name.startswith('RUN-'))
        self.assertEqual(len(directories), 1)
        stored = json.loads((directories[0] / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(recording_run_errors(stored, directories[0]), [])
        self.assertEqual(stored['stages']['audio_qa']['status'], 'complete')
        for kind in ('audio-qa', 'audio_metadata', 'normalized_audio', 'original_recording',
                     'report_json', 'report_markdown'):
            artifact = next(x for x in stored['artifacts'] if x['kind'] == kind)
            path = directories[0] / artifact['path']
            self.assertTrue(path.is_file(), f'{kind} must still exist after a restart')
            self.assertEqual(digest(path), artifact['sha256'])
        qa = load_document(directories[0] / next(x['path'] for x in stored['artifacts']
                                                 if x['kind'] == 'audio-qa'))
        self.assertEqual(qa['run_id'], stored['run_id'])
        self.assertEqual(qa['analysis_id'], stored['analysis_id'])

    def test_cli_partial_exit_is_two(self):
        result = subprocess.run([sys.executable, '-m', 'aivoicebench', 'import', str(self.source),
                                 '--synthetic', '--output', str(self.root / 'cli')],
                                cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'PARTIAL', result.stdout)


if __name__ == '__main__':
    unittest.main()
