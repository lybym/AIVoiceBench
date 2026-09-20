"""Fixed-mode stimulus freeze → serve end-to-end contract (Issue #98, PRD-F020).

The provider-level tests prove the V3 WebSocket adapter emits and validates MP3.
They cannot prove the *Fixed Case Runner* freezes that MP3 under a name and
Content-Type that describe it — which is exactly how a `.wav`-named MP3 stimulus
with an `audio/wav` header could pass green provider tests. This module closes
that gap through the real production path:

    VoiceTestManager.synthesize_phrase  ->  phrase['audio_path']
      -> VoiceTestManager.get_audio_path  ->  GET /api/voice-test/sessions/{id}/audio/{i}

No network is used: the TTS provider is a real ``VolcengineUnidirectionalTTSProvider``
driven by the injected transport, so the whole chain (adapter, runner, HTTP
response) is exercised without Volcengine credentials.
"""

import asyncio
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from fastapi.testclient import TestClient

from aivoicebench import api
from aivoicebench.model_settings import RunProviders
from aivoicebench.providers import ProviderFailure
from aivoicebench.voice_test import (DEFAULT_TTS_STIMULUS_FORMAT, TTS_STIMULUS_EXTENSIONS,
                                     VoiceTestManager, tts_media_type_for_path,
                                     tts_stimulus_extension)
from aivoicebench.volcengine_tts_ws import (AUDIT_ENDPOINT_UNIDIRECTIONAL,
                                           ENDPOINT_UNIDIRECTIONAL,
                                           EVENT_FINISH_SESSION,
                                           EVENT_SESSION_FINISHED,
                                           VolcengineUnidirectionalTTSProvider)

from tests.test_tts_v3_websocket import (FakeServer, FakeTTSLegacyProvider,
                                         audio_frame, mp3_frame, server_frame)


def mp3_stream(frames=2):
    return b''.join(mp3_frame() for _ in range(frames))


def mp3_tts_provider(stream):
    """A real V3 unidirectional provider whose transport is scripted."""
    server = FakeServer(script_for_event={
        EVENT_FINISH_SESSION: lambda frame: [
            audio_frame(stream[:max(len(stream) // 2, 1)]),
            audio_frame(stream[max(len(stream) // 2, 1):]),
            server_frame(EVENT_SESSION_FINISHED)]})

    def factory(root):
        return VolcengineUnidirectionalTTSProvider(
            root, 'test-key', endpoint=ENDPOINT_UNIDIRECTIONAL,
            audit_endpoint=AUDIT_ENDPOINT_UNIDIRECTIONAL,
            model='tts-model', resource_id='volc.service_type.test',
            voice_type='BV700_test', sample_rate=48000, timeout=10,
            transport=server)
    return factory, server


class FixedStimulusFreezeServeTests(unittest.TestCase):
    """The frozen stimulus name, bytes and Content-Type must agree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stream = mp3_stream(frames=2)

    def _manager(self, factory):
        manager = VoiceTestManager(self.root)
        manager._providers = RunProviders(tts=factory)
        return manager

    # ------------------------------------------------------------ unit paths

    def test_extension_follows_the_transport_format(self):
        self.assertEqual(tts_stimulus_extension(FakeTTSLegacyProvider('wav')), '.wav')
        self.assertEqual(tts_stimulus_extension(FakeTTSLegacyProvider('mp3')), '.mp3')
        # A provider that declares nothing resolves to the *declared* product
        # default (Issue #98: Active TTS is MP3), not to an inlined magic string.
        self.assertEqual(tts_stimulus_extension(object()),
                         f'.{DEFAULT_TTS_STIMULUS_FORMAT}')
        self.assertEqual(DEFAULT_TTS_STIMULUS_FORMAT, 'mp3')
        # A format that *is* declared but unknown is refused, never guessed: the
        # naming/MIME must always be traceable to a real container.
        with self.assertRaises(ProviderFailure):
            tts_stimulus_extension(FakeTTSLegacyProvider('flac'))
        with self.assertRaises(ProviderFailure):
            tts_stimulus_extension(object(), 'ogg')

    def test_media_type_follows_the_stored_container(self):
        self.assertEqual(tts_media_type_for_path(self.root / 'a.mp3'), 'audio/mpeg')
        self.assertEqual(tts_media_type_for_path(self.root / 'a.wav'), 'audio/wav')
        self.assertEqual(tts_media_type_for_path(self.root / 'a.bin'),
                         'application/octet-stream')

    def test_probe_order_prefers_the_fixed_target(self):
        self.assertEqual(TTS_STIMULUS_EXTENSIONS[0], '.mp3')

    # ------------------------------------------------- end-to-end freeze path

    def test_fixed_synthesis_freezes_an_mp3_named_asset(self):
        factory, server = mp3_tts_provider(self.stream)
        manager = self._manager(factory)
        session = manager.create_session('fixed', phrases=['固定话术'])
        manager.synthesize_all(session.session_id)
        phrase = session.phrases[0]
        self.assertEqual(phrase['audio_format'], 'mp3')
        self.assertTrue(phrase['audio_path'].endswith('.mp3'),
                        f'stimulus must be named for its container: {phrase["audio_path"]}')
        frozen = manager.get_audio_path(session.session_id, 0)
        self.assertIsNotNone(frozen)
        self.assertEqual(frozen.suffix, '.mp3')
        # The bytes really are the provider's MP3, not a re-encoded copy.
        self.assertEqual(frozen.read_bytes(), self.stream)
        self.assertEqual(frozen.read_bytes()[:2], b'\xff\xfb')
        self.assertEqual(len(server.frames), 3)

    def test_get_audio_path_still_finds_a_legacy_wav_stimulus(self):
        """Migration period: a WAV asset frozen by the SSE route must still play."""
        manager = self._manager(lambda root: None)
        session = manager.create_session('fixed', phrases=['legacy'])
        legacy = session.directory / 'phrase-0000.wav'
        legacy.write_bytes(b'RIFF....WAVE')
        session.phrases[0].update({'audio_path': str(legacy.relative_to(self.root)),
                                   'status': 'ready'})
        resolved = manager.get_audio_path(session.session_id, 0)
        self.assertEqual(resolved, legacy)

    def test_get_audio_path_finds_an_mp3_free_turn_asset(self):
        manager = self._manager(lambda root: None)
        session = manager.create_session('free', phrases=[])
        asset = session.directory / 'free-turn-0000.mp3'
        asset.write_bytes(self.stream)
        self.assertEqual(manager.get_audio_path(session.session_id, 0), asset)
        self.assertIsNone(manager.get_audio_path(session.session_id, 1))

    def test_mixed_container_session_resolves_each_phrase_by_its_own_path(self):
        """Migration reality: a session may hold a legacy WAV and a new MP3.

        The fixed branch must keep resolving each phrase through its recorded
        path, so a partially re-synthesised session still plays both containers
        correctly instead of assuming one extension for the whole session.
        """
        manager = self._manager(lambda root: None)
        session = manager.create_session('fixed', phrases=['legacy', 'current'])
        legacy = session.directory / 'phrase-0000.wav'
        current = session.directory / 'phrase-0001.mp3'
        legacy.write_bytes(b'RIFF0000WAVEfmt ')
        current.write_bytes(self.stream)
        session.phrases[0].update({'audio_path': str(legacy.relative_to(self.root)),
                                   'status': 'ready', 'audio_format': 'wav'})
        session.phrases[1].update({'audio_path': str(current.relative_to(self.root)),
                                   'status': 'ready', 'audio_format': 'mp3'})
        first = manager.get_audio_path(session.session_id, 0)
        second = manager.get_audio_path(session.session_id, 1)
        self.assertEqual(first, legacy)
        self.assertEqual(second, current)
        # Each is served with the Content-Type of its own container.
        self.assertEqual(tts_media_type_for_path(first), 'audio/wav')
        self.assertEqual(tts_media_type_for_path(second), 'audio/mpeg')
        # And the session is considered playable without any new synthesis.
        self.assertTrue(manager._fixed_audio_ready(session))

    def test_second_synthesis_run_reuses_the_frozen_asset(self):
        """AC #1: a repeat must play the frozen stimulus, not re-synthesise it."""
        factory, server = mp3_tts_provider(self.stream)
        manager = self._manager(factory)
        session = manager.create_session('fixed', phrases=['固定话术'])
        manager.synthesize_all(session.session_id)
        first_path = manager.get_audio_path(session.session_id, 0)
        first_bytes = first_path.read_bytes()
        calls_after_first = len(server.frames)
        inode_mtime = first_path.stat().st_mtime_ns
        # A second run over the same session finds every phrase already 'ready'.
        manager.synthesize_all(session.session_id)
        self.assertEqual(len(server.frames), calls_after_first,
                         'a repeat run must not place another synthesis request')
        self.assertEqual(manager.get_audio_path(session.session_id, 0), first_path)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(first_path.stat().st_mtime_ns, inode_mtime,
                         'the frozen asset must not be rewritten')

    # ------------------------------------------------------- HTTP serve path

    def test_audio_endpoint_serves_mp3_with_mp3_content_type(self):
        factory, _server = mp3_tts_provider(self.stream)
        manager = self._manager(factory)
        session = manager.create_session('fixed', phrases=['固定话术'])
        manager.synthesize_all(session.session_id)
        with unittest.mock.patch.object(api, '_voice_test_manager', manager):
            client = TestClient(api.app)
            response = client.get(
                f'/api/voice-test/sessions/{session.session_id}/audio/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'audio/mpeg')
        self.assertEqual(response.content, self.stream)

    def test_audio_endpoint_still_serves_legacy_wav_correctly(self):
        manager = self._manager(lambda root: None)
        session = manager.create_session('fixed', phrases=['legacy'])
        legacy = session.directory / 'phrase-0000.wav'
        legacy.write_bytes(b'RIFF0000WAVEfmt ')
        session.phrases[0].update({'audio_path': str(legacy.relative_to(self.root)),
                                   'status': 'ready'})
        with unittest.mock.patch.object(api, '_voice_test_manager', manager):
            client = TestClient(api.app)
            response = client.get(
                f'/api/voice-test/sessions/{session.session_id}/audio/0')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'audio/wav')

    def test_play_instruction_points_at_the_mp3_asset(self):
        """The control message and the served container must not disagree."""
        factory, _server = mp3_tts_provider(self.stream)
        manager = self._manager(factory)
        session = manager.create_session('fixed', phrases=['固定话术'])
        manager.synthesize_all(session.session_id)
        self.assertTrue(session.phrases[0]['audio_path'].endswith('.mp3'))
        served = manager.get_audio_path(session.session_id, 0)
        self.assertEqual(tts_media_type_for_path(served), 'audio/mpeg')


if __name__ == '__main__':
    unittest.main()
