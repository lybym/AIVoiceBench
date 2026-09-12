"""Per-mode capability precheck contract (PRD-F020/F021/F023).

The precheck is not a query convenience: it is enforced at ``start``. These tests
drive the real HTTP and WebSocket surface and prove that

* a missing capability refuses the run **before** any model/TTS/ASR call,
* neither the HTTP start endpoint nor the control socket can bypass it,
* fixed mode reuses already-generated audio and only needs TTS when it must
  synthesize,
* the Streaming ASR path is not blocked by File ASR's Signed-URL publication
  requirement, and
* the whole-turn fallback is only available when the operator asks for it.

The providers are deterministic stand-ins: software verification only, no cloud
call and no audio device.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from aivoicebench import api  # noqa: E402
from aivoicebench.model_settings import RunProviders  # noqa: E402
from aivoicebench.voice_test import VoiceTestManager  # noqa: E402
from browser_server import ScriptedFileASR, ScriptedJudge, ScriptedStreamingProvider, SyntheticTTS  # noqa: E402

FORBIDDEN_SUBSTRINGS = ('api_key', 'api-key', 'secret', 'http://', 'https://', 'wss://',
                        'AIVOICEBENCH_AUDIO', 'signed', 'signature', 'authorization',
                        'credential_value', 'X-Api')


class CapabilityPrecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output_root = self.root / 'out'
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.tts = SyntheticTTS(self.output_root)
        self.streaming = ScriptedStreamingProvider()
        self.file_asr = ScriptedFileASR()
        self.manager = VoiceTestManager(self.output_root)
        self.manager._providers = RunProviders(
            tts=lambda root: self.tts, judge=ScriptedJudge(),
            streaming_asr=lambda root: self.streaming, asr=lambda root: self.file_asr)
        self.patches = [patch.object(api, '_voice_test_manager', self.manager),
                        patch.object(api, 'OUTPUT_ROOT', self.output_root)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------- helpers

    def without(self, **roles):
        """The injected provider set, with the named roles removed."""
        original = self.manager._providers
        self.manager._providers = RunProviders(
            tts=None if roles.get('tts') else original.tts,
            judge=None if roles.get('judge') else original.judge,
            streaming_asr=None if roles.get('streaming_asr') else original.streaming_asr,
            asr=None if roles.get('asr') else original.asr)
        self.addCleanup(self._restore, original)

    def _restore(self, original):
        self.manager._providers = original

    def create_free(self, **extra):
        body = {'mode': 'free', 'goal': '能力预检', 'max_turns': 3}
        body.update(extra)
        response = self.client.post('/api/voice-test/sessions', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['session_id']

    def create_fixed(self, phrases=('你好',), synthesize=True, **extra):
        body = {'mode': 'fixed', 'phrases': list(phrases), 'device': 'precheck'}
        body.update(extra)
        created = self.client.post('/api/voice-test/sessions', json=body)
        self.assertEqual(created.status_code, 200, created.text)
        session_id = created.json()['session_id']
        if synthesize:
            synth = self.client.post(f'/api/voice-test/sessions/{session_id}/synthesize')
            self.assertEqual(synth.status_code, 200, synth.text)
        return session_id

    def report(self, mode, session_id=None, capture_mode=None):
        query = []
        if session_id:
            query.append(f'session_id={session_id}')
        if capture_mode:
            query.append(f'capture_mode={capture_mode}')
        suffix = ('?' + '&'.join(query)) if query else ''
        response = self.client.get(f'/api/voice-test/capabilities/{mode}{suffix}')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # --------------------------------------------------------------- tests

    def test_report_is_configuration_only_and_carries_no_secrets(self):
        report = self.report('free')
        self.assertTrue(report['ready'], report)
        self.assertEqual(report['connectivity'], 'not_probed')
        self.assertIn('不发起付费探测', report['note'])
        blob = json.dumps(report, ensure_ascii=False)
        for forbidden in FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, blob.lower(), forbidden)

    def test_unknown_mode_is_rejected(self):
        response = self.client.get('/api/voice-test/capabilities/unknown')
        self.assertEqual(response.status_code, 400)

    def test_fixed_mode_reuses_existing_audio_without_needing_tts(self):
        session_id = self.create_fixed()
        self.without(tts=True)
        report = self.report('fixed', session_id=session_id)
        self.assertTrue(report['ready'], report)
        self.assertFalse(report['write_audio_required'])
        self.assertNotIn('tts', report['missing'])
        # The run itself still starts: fixed control does not need TTS once the
        # frozen audio exists.
        started = self.client.post(f'/api/voice-test/sessions/{session_id}/start')
        self.assertEqual(started.status_code, 200, started.text)

    def test_fixed_mode_requires_tts_when_audio_must_be_generated(self):
        session_id = self.create_fixed(synthesize=False)
        self.without(tts=True)
        report = self.report('fixed', session_id=session_id)
        self.assertFalse(report['ready'])
        self.assertEqual(report['missing'], ['tts'])
        self.assertTrue(report['write_audio_required'])

    def test_free_streaming_does_not_require_file_asr_publication(self):
        """A run on the Streaming ASR path is not blocked by the File ASR route."""
        self.without(asr=True)
        report = self.report('free', capture_mode='streaming')
        self.assertTrue(report['ready'], report)
        self.assertEqual(report['capture_mode'], 'streaming')
        self.assertNotIn('asr', report['missing'])
        self.assertNotIn('asr_publication', report['missing'])
        self.assertNotIn('turn_file_recorder',
                         [item['item'] for item in report['browser_checks']])

    def test_free_auto_never_silently_falls_back_to_turn_file(self):
        self.without(streaming_asr=True)
        report = self.report('free', capture_mode='auto')
        self.assertFalse(report['ready'])
        self.assertEqual(report['missing'], ['streaming_asr'])
        self.assertEqual(report['capture_mode'], 'unavailable')
        # The downgrade path is still offered as an explicit operator choice.
        downgraded = self.report('free', capture_mode='turn_file')
        self.assertTrue(downgraded['ready'], downgraded)
        self.assertEqual(downgraded['capture_mode'], 'turn_file')

    def test_free_turn_file_requires_file_asr_and_its_publication_rule(self):
        self.without(streaming_asr=True, asr=True)
        report = self.report('free', capture_mode='turn_file')
        self.assertFalse(report['ready'])
        self.assertEqual(report['missing'], ['asr'])

    def test_turn_file_publication_is_only_required_when_the_adapter_needs_it(self):
        self.without(streaming_asr=True)
        doc = {'readiness': {'tts': 'configured', 'judge': 'configured', 'asr': 'configured',
                             'streaming_asr': 'not_configured'},
               'asr_publication_configured': False}
        with patch.object(self.manager, '_settings_document', lambda: doc):
            report = self.report('free', capture_mode='turn_file')
        self.assertFalse(report['ready'])
        self.assertIn('asr_publication', report['missing'])

    def test_missing_asr_refuses_both_start_paths_without_any_call(self):
        """Acceptance A: no ASR at all -> refusal, and zero provider calls."""
        self.without(streaming_asr=True, asr=True)
        session_id = self.create_free()
        report = self.report('free')
        self.assertFalse(report['ready'])
        self.assertIn('streaming_asr', report['missing'])

        # 1. The HTTP start endpoint.
        blocked = self.client.post(f'/api/voice-test/sessions/{session_id}/start')
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()['detail']['reason'], 'capability_precheck_failed')

        # 2. The control socket. It must answer with an explicit refusal rather
        #    than opening a play instruction or waiting forever.
        with self.client.websocket_connect(f'/api/voice-test/sessions/{session_id}/ws') as ws:
            ws.send_json({'type': 'start'})
            message = ws.receive_json()
            self.assertEqual(message['type'], 'blocked')
            self.assertIn('streaming_asr', message['missing'])

        session = self.manager.get_session(session_id)
        self.assertEqual(session.run_index, 0)
        self.assertEqual(session.turns, [])
        self.assertEqual(session.provider_calls, {'tts': 0, 'llm': 0, 'asr': 0})
        self.assertEqual(self.tts.calls, [])
        self.assertEqual(self.streaming.sessions, [])
        self.assertEqual(self.file_asr.calls, [])
        record = self.manager.load_record(session_id)
        kinds = [event['kind'] for event in record['events']]
        self.assertIn('session_start_refused', kinds)
        self.assertNotIn('session_started', kinds)
        self.assertNotIn('play_issued', kinds)

    def test_capability_is_reenforced_by_the_manager_not_only_the_endpoint(self):
        """Calling the manager directly must not bypass the precheck either."""
        from aivoicebench.voice_test import CapabilityError
        self.without(streaming_asr=True, asr=True)
        session_id = self.create_free()
        with self.assertRaises(CapabilityError) as caught:
            self.manager.start(session_id)
        self.assertIn('streaming_asr', caught.exception.report['missing'])
        session = self.manager.get_session(session_id)
        self.assertEqual(session.status, 'created')
        self.assertEqual(session.provider_calls, {'tts': 0, 'llm': 0, 'asr': 0})

    def test_losing_the_llm_refuses_free_mode_but_not_fixed_mode(self):
        session_free = self.create_free()
        self.without(judge=True)
        report = self.report('free')
        self.assertFalse(report['ready'])
        self.assertEqual(report['missing'], ['judge'])
        started = self.client.post(f'/api/voice-test/sessions/{session_free}/start')
        self.assertEqual(started.status_code, 409)

        fixed = self.create_fixed()
        self.assertEqual(self.client.post(f'/api/voice-test/sessions/{fixed}/start').status_code,
                         200)

    def test_capture_mode_is_validated(self):
        # An unknown capture_mode cannot even create a session.
        rejected = self.client.post('/api/voice-test/sessions',
                                    json={'mode': 'free', 'goal': 'x', 'capture_mode': 'sideways'})
        self.assertEqual(rejected.status_code, 400, rejected.text)
        session_id = self.create_free()
        response = self.client.get(
            f'/api/voice-test/capabilities/free?session_id={session_id}&capture_mode=sideways')
        self.assertEqual(response.status_code, 400)
        response = self.client.get('/api/voice-test/capabilities/sideways')
        self.assertEqual(response.status_code, 400)

    def test_unknown_session_is_404(self):
        self.assertEqual(
            self.client.get('/api/voice-test/capabilities/free?session_id=VT-nope').status_code,
            404)


if __name__ == '__main__':
    unittest.main()
