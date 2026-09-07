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
        self.assertTrue((directory / 'report.md').exists())
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
        self.assertTrue((directory / 'report.json').exists())

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

    def _import(self, source=None, **kwargs):
        return import_recording(source or self.source, self.root / 'runs', synthetic=True, **kwargs)

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
        report = load_document(directory / 'report.json')
        self.assertEqual(report['device_performance'], 'insufficient_evidence')
        self.assertEqual(report['conclusions'], [])
        self.assertNotIn('<script>', (directory / 'report.md').read_text(encoding='utf-8'))

    def test_manifest_validator_detects_tamper_and_path_escape(self):
        directory, manifest = self._import()
        original = next(x for x in manifest['artifacts'] if x['kind'] == 'original_recording')
        (directory / original['path']).write_bytes(b'changed')
        self.assertTrue(recording_run_errors(manifest, directory))
        original['path'] = '../outside.wav'
        self.assertTrue(recording_run_errors(manifest, directory))

    def test_cli_partial_exit_is_two(self):
        result = subprocess.run([sys.executable, '-m', 'aivoicebench', 'import', str(self.source),
                                 '--synthetic', '--output', str(self.root / 'cli')],
                                cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr.decode('utf-8', errors='replace'))
        self.assertIn(b'PARTIAL', result.stdout)


if __name__ == '__main__':
    unittest.main()
