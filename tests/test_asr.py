from pathlib import Path
import tempfile
import unittest
import json
import wave

from aivoicebench.asr import ProviderOutput, normalize_vosk, prepare_channel, transcribe_file
from aivoicebench.validation import load_document, transcript_errors


class FixtureProvider:
    """Synthetic provider unit fixture; no model or hardware call."""
    def __init__(self, messages=None):
        self.messages = messages if messages is not None else [json.dumps({'text': '你好', 'result': [
            {'word': '你好', 'start': 0.01, 'end': 0.08, 'conf': 0.8}], 'vendor_extra': 'preserved'})]

    def transcribe(self, source):
        return ProviderOutput(dict(provider='fixture-only', library_version='test', model_id='fixture',
            model_version='test', model_sha256='0' * 64, config={'synthetic': True}), self.messages)

    def normalize(self, messages, duration):
        return normalize_vosk(messages, duration)


class TimestampedASR(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(bytes(3200))

    def test_raw_preserved_and_timestamp_scope(self):
        provider = FixtureProvider()
        path, transcript = transcribe_file(self.source, provider, self.root / 'results', 'room_mix')
        self.assertEqual(transcript_errors(transcript), [])
        self.assertEqual(load_document(path / 'raw-provider.json')['messages'], provider.messages)
        self.assertEqual(transcript['measurement_scope'], 'external_asr')
        self.assertEqual(transcript['time_mapping']['status'], 'unmapped')
        self.assertIsNone(transcript['segments'][0]['timestamp_confidence'])
        self.assertEqual(transcript['segments'][0]['words'][0]['recognition_confidence'], 0.8)
        self.assertEqual(transcript['segments'][0]['start_ms'], 10)

    def test_text_without_timestamps_is_partial(self):
        _, transcript = transcribe_file(self.source, FixtureProvider(['{"text":"no timings"}']), self.root / 'results')
        self.assertEqual(transcript['status'], 'partial')
        self.assertEqual(transcript['segments'], [])
        self.assertTrue(transcript['gaps'])

    def test_no_speech_is_empty_not_fabricated(self):
        _, transcript = transcribe_file(self.source, FixtureProvider(['{"text":""}']), self.root / 'results')
        self.assertEqual(transcript['segments'], [])
        self.assertEqual(transcript['status'], 'complete')

    def test_bad_provider_timing_and_confidence(self):
        for field, value in [('start', -1), ('end', 10), ('conf', 2), ('start', float('nan'))]:
            message = {'text': 'x', 'result': [{'word': 'x', 'start': 0.01, 'end': 0.02, 'conf': 0.8}]}
            message['result'][0][field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    normalize_vosk([json.dumps(message)], 100)

    def test_malformed_provider_shapes(self):
        for message in ['[]', '{"result":"bad"}', '{"result":[42]}']:
            with self.assertRaises(ValueError):
                normalize_vosk([message], 100)

    def test_failure_keeps_audit(self):
        output = self.root / 'results'
        with self.assertRaises(ValueError):
            transcribe_file(self.source, FixtureProvider(['{"result":"bad"}']), output)
        path = next(output.iterdir())
        self.assertTrue((path / 'raw-provider.json').exists())
        self.assertTrue((path / 'error.json').exists())
        self.assertFalse((path / 'transcript.json').exists())

    def test_stereo_channel_must_be_explicit(self):
        with wave.open(str(self.source), 'wb') as audio:
            audio.setparams((2, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x01\x00\x02\x00' * 1600)
        with self.assertRaisesRegex(ValueError, 'explicit --channel'):
            prepare_channel(self.source, self.root / 'mono.wav')
        duration, channel = prepare_channel(self.source, self.root / 'mono.wav', 2)
        self.assertEqual((duration, channel), (100, 2))
        with wave.open(str(self.root / 'mono.wav'), 'rb') as audio:
            self.assertEqual(audio.readframes(1600), b'\x02\x00' * 1600)

    def test_run_case_binding_is_paired(self):
        with self.assertRaisesRegex(ValueError, 'together'):
            transcribe_file(self.source, FixtureProvider(), self.root, run_id='R')

    def test_shared_validator_catches_vendor_boundary_errors(self):
        _, transcript = transcribe_file(self.source, FixtureProvider(), self.root / 'results')
        transcript['segments'][0]['end_ms'] = 200
        self.assertIn('out of source bounds', '\n'.join(transcript_errors(transcript)))

    def test_input_snapshot_is_independent(self):
        path, transcript = transcribe_file(self.source, FixtureProvider(), self.root / 'results')
        self.source.write_bytes(b'changed')
        self.assertNotEqual((path / 'source.wav').read_bytes(), self.source.read_bytes())


if __name__ == '__main__':
    unittest.main()
