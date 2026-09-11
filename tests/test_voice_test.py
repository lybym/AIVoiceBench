"""Tests for the Active Voice Test session manager and TTS provider.

These are software tests only: the TTS provider uses a mock transport, and the
session manager is tested without real audio playback or microphone capture.
Real device testing requires a physical AI device, speakers, and a microphone.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from aivoicebench.voice_test import VoiceTestManager, VoiceTestSession
from aivoicebench.volcengine_tts import VolcengineTTSProvider, UnavailableTTSProvider
from aivoicebench.cloud_transport import HTTPReply
from aivoicebench.providers import ProviderFailure
from aivoicebench.model_settings import RunProviders


class MockTTSProvider:
    """A deterministic TTS stand-in for testing without API keys."""

    def __init__(self, root, *args, **kwargs):
        self.root = Path(root)
        self.calls = []

    def synthesize(self, text, destination):
        self.calls.append(text)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write a minimal valid WAV header + silence
        import wave
        with wave.open(str(destination), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b'\x00\x00' * 1600)  # 0.1s of silence
        import hashlib
        data = destination.read_bytes()
        return {
            'path': str(destination),
            'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data),
            'format': 'wav',
            'sample_rate': 16000,
            'text': text,
        }


class VoiceTestSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manager = VoiceTestManager(self.root)
        # Inject a mock TTS provider
        self.manager._providers = RunProviders(tts=lambda root: MockTTSProvider(root))

    def test_create_fixed_session(self):
        session = self.manager.create_session('fixed', phrases=['你好', '今天天气怎么样'])
        self.assertEqual(session.mode, 'fixed')
        self.assertEqual(len(session.phrases), 2)
        self.assertEqual(session.phrases[0]['text'], '你好')
        self.assertEqual(session.status, 'created')

    def test_synthesize_all_generates_audio(self):
        session = self.manager.create_session('fixed', phrases=['你好', '测试'])
        results = self.manager.synthesize_all(session.session_id)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['status'], 'ready')
        self.assertIsNotNone(results[0]['audio_path'])
        # Audio file exists
        audio_path = self.root / results[0]['audio_path']
        self.assertTrue(audio_path.is_file())

    def test_start_requires_ready_phrases(self):
        session = self.manager.create_session('fixed', phrases=['你好'])
        with self.assertRaises(ValueError):
            self.manager.start(session.session_id)

    def test_start_after_synthesis(self):
        session = self.manager.create_session('fixed', phrases=['你好'])
        self.manager.synthesize_all(session.session_id)
        self.manager.start(session.session_id)
        self.assertEqual(session.status, 'running')
        self.assertEqual(session.current_phrase_index, 0)

    def test_stop(self):
        session = self.manager.create_session('fixed', phrases=['你好'])
        self.manager.synthesize_all(session.session_id)
        self.manager.start(session.session_id)
        self.manager.stop(session.session_id, reason='user_stop')
        self.assertEqual(session.status, 'stopped')
        self.assertEqual(session.stop_reason, 'user_stop')

    def test_get_audio_path_fixed(self):
        session = self.manager.create_session('fixed', phrases=['你好'])
        self.manager.synthesize_all(session.session_id)
        audio = self.manager.get_audio_path(session.session_id, 0)
        self.assertIsNotNone(audio)
        self.assertTrue(audio.is_file())

    def test_get_audio_path_not_ready(self):
        session = self.manager.create_session('fixed', phrases=['你好'])
        audio = self.manager.get_audio_path(session.session_id, 0)
        self.assertIsNone(audio)

    def test_synthesize_text_free_mode(self):
        session = self.manager.create_session('free', goal='测试天气查询')
        result = self.manager.synthesize_text(session.session_id, '你好，今天天气怎么样')
        self.assertEqual(result['text'], '你好，今天天气怎么样')
        audio_path = self.root / result['path']
        self.assertTrue(audio_path.is_file())

    def test_no_tts_provider_raises(self):
        manager = VoiceTestManager(self.root)
        manager._providers = RunProviders()  # No TTS
        session = manager.create_session('fixed', phrases=['你好'])
        with self.assertRaises(ProviderFailure):
            manager.synthesize_all(session.session_id)

    def test_to_dict(self):
        session = self.manager.create_session('fixed', phrases=['你好'], device='Test Device')
        d = session.to_dict()
        self.assertEqual(d['session_id'], session.session_id)
        self.assertEqual(d['mode'], 'fixed')
        self.assertEqual(d['device'], 'Test Device')
        self.assertEqual(len(d['phrases']), 1)


class TTSProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_unavailable_provider_raises(self):
        provider = UnavailableTTSProvider()
        with self.assertRaises(ProviderFailure):
            provider.synthesize('test', self.root / 'out.wav')

    def test_missing_key_raises(self):
        provider = VolcengineTTSProvider(self.root, '')
        with self.assertRaises(ProviderFailure):
            provider.synthesize('test', self.root / 'out.wav')

    def test_synthesize_with_mock_transport(self):
        """The TTS provider calls the transport and saves the returned audio."""
        import base64
        import hashlib

        # Create a minimal WAV file as the "synthesized audio"
        import wave
        wav_buf = b''
        import io
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b'\x00\x00' * 1600)
        wav_bytes = buf.getvalue()

        mock_transport = MagicMock()
        mock_transport.request.return_value = HTTPReply(
            200, {'Content-Type': 'application/json'},
            json.dumps({'code': '3000', 'data': base64.b64encode(wav_bytes).decode()}).encode())

        provider = VolcengineTTSProvider(
            self.root, 'test-key', transport=mock_transport,
            voice_type='BV001_streaming')
        dest = self.root / 'phrase-0001.wav'
        result = provider.synthesize('你好', dest)

        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), wav_bytes)
        self.assertEqual(result['text'], '你好')
        self.assertEqual(result['voice_type'], 'BV001_streaming')
        # The transport was called exactly once
        mock_transport.request.assert_called_once()

    def test_synthesize_raw_audio_response(self):
        """If the response is raw audio (content-type audio/), use it directly."""
        import wave
        import io
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b'\x01\x02' * 1600)
        wav_bytes = buf.getvalue()

        mock_transport = MagicMock()
        mock_transport.request.return_value = HTTPReply(
            200, {'Content-Type': 'audio/wav'}, wav_bytes)

        provider = VolcengineTTSProvider(
            self.root, 'test-key', transport=mock_transport)
        dest = self.root / 'raw.wav'
        result = provider.synthesize('test', dest)

        self.assertEqual(dest.read_bytes(), wav_bytes)

    def test_error_response_raises(self):
        mock_transport = MagicMock()
        mock_transport.request.return_value = HTTPReply(
            200, {'Content-Type': 'application/json'},
            json.dumps({'code': '9999', 'message': 'Invalid voice'}).encode())

        provider = VolcengineTTSProvider(
            self.root, 'test-key', transport=mock_transport)
        with self.assertRaises(ProviderFailure):
            provider.synthesize('test', self.root / 'err.wav')


if __name__ == '__main__':
    unittest.main()
