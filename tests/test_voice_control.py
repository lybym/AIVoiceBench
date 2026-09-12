"""Control-loop tests for the Active Voice Test WebSocket protocol (PRD-F020).

These extend the existing HTTP/WebSocket coverage in ``test_voice_test.py`` with
the reliability rules introduced for fixed-dialogue control:

- stopping really stops, and late/duplicate events cannot advance the run again;
- a no-response timeout is recorded as "no response observed", never as the end
  of a response;
- every run leaves a minimal execution record, and a restart does not erase the
  previous run's failure;
- failures exit instead of hanging.

Everything here drives the real HTTP + WebSocket surface of ``aivoicebench.api``
with a deterministic TTS stand-in and a script that plays the part of the
browser (reporting playback and VAD events). No cloud credentials, no audio
device, no physical AI device.
"""

import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aivoicebench import api
from aivoicebench.model_settings import RunProviders
from aivoicebench.voice_test import VoiceTestManager


class MockTTSProvider:
    """Deterministic TTS stand-in: a valid silent WAV per phrase."""

    def __init__(self, root, *args, **kwargs):
        self.root = Path(root)
        self.calls = []

    def synthesize(self, text, destination):
        self.calls.append(text)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b'\x00\x00' * 1600)  # 0.1 s
        data = destination.read_bytes()
        return {
            'path': str(destination),
            'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data),
            'format': 'wav',
            'sample_rate': 16000,
            'text': text,
        }


class ControlLoopTestCase(unittest.TestCase):
    """Shared harness: real app, deterministic TTS, scripted browser."""

    phrases = ['你好，我想问一下', '南京明天天气怎么样', '北京呢']
    no_response_timeout_ms = 2500

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output_root = self.root / 'out'
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.tts = MockTTSProvider(self.output_root)
        self.manager = VoiceTestManager(self.output_root)
        self.manager._providers = RunProviders(tts=lambda root: self.tts)

        for item in (patch.object(api, '_voice_test_manager', self.manager),
                     patch.object(api, 'OUTPUT_ROOT', self.output_root)):
            item.start()
            self.addCleanup(item.stop)

        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------- helpers

    def ws_url(self, session_id):
        return f'/api/voice-test/sessions/{session_id}/ws'

    def create_fixed(self, phrases=None, **extra):
        body = {'mode': 'fixed', 'phrases': phrases or self.phrases, 'device': 'fixture'}
        body.update(extra)
        response = self.client.post('/api/voice-test/sessions', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        session_id = response.json()['session_id']
        synthesized = self.client.post(f'/api/voice-test/sessions/{session_id}/synthesize')
        self.assertEqual(synthesized.status_code, 200, synthesized.text)
        return session_id

    def session_state(self, session_id):
        return self.manager.get_session(session_id)

    def record(self, session_id):
        response = self.client.get(f'/api/voice-test/sessions/{session_id}/execution-record')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def events_of(record, kind):
        return [e for e in record['events'] if e['kind'] == kind]

    @staticmethod
    def current_run(record):
        return [run for run in record['runs'] if run['current']][0]

    def start_run(self, ws):
        """start -> first play message."""
        ws.send_json({'type': 'start'})
        return ws.receive_json()

    def suspect_response(self, ws, turn_id, *, expect_play=None):
        """Play the part of the browser: playback finished, response observed.

        ``device_speech_start`` is acknowledged with 'listening'; the response
        end then produces either the next play (``expect_play``) or completion.
        """
        ws.send_json({'type': 'playback_started', 'turn_id': turn_id})
        ws.send_json({'type': 'device_speech_start', 'turn_id': turn_id})
        listening = ws.receive_json()
        assert listening['type'] == 'listening', listening
        ws.send_json({'type': 'device_speech_end', 'turn_id': turn_id})
        if expect_play is None:
            return None
        return ws.receive_json()


class FixedRunControlTests(ControlLoopTestCase):
    def test_three_phrase_run_advances_once_per_observed_response(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            first = self.start_run(ws)
            self.assertEqual((first['type'], first['phrase_index']), ('play', 0))
            self.assertTrue(first['turn_id'])

            second = self.suspect_response(ws, first['turn_id'], expect_play=True)
            self.assertEqual((second['type'], second['phrase_index']), ('play', 1))
            self.assertNotEqual(second['turn_id'], first['turn_id'])

            third = self.suspect_response(ws, second['turn_id'], expect_play=True)
            self.assertEqual((third['type'], third['phrase_index']), ('play', 2))

            done = self.suspect_response(ws, third['turn_id'], expect_play=True)
            self.assertEqual((done['type'], done['reason']), ('complete', 'all_phrases_done'))

        state = self.session_state(session_id)
        self.assertEqual(state.status, 'completed')
        self.assertEqual(state.stop_reason, 'all_phrases_done')
        self.assertEqual([t.closure_reason for t in state.turns],
                         ['observation_speech_end'] * 3)
        self.assertTrue(all(t.closed for t in state.turns))

    def test_execution_record_answers_who_what_and_why(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            self.suspect_response(ws, play['turn_id'], expect_play=True)

        record = self.record(session_id)
        self.assertEqual(record['record_version'], '1.1.0')
        self.assertTrue(record['notes'])
        self.assertEqual(record['session_id'], session_id)

        run = self.current_run(record)
        self.assertEqual(run['run_index'], 1)
        self.assertEqual(len(run['turns']), 2)

        turn = run['turns'][0]
        # which text and which audio asset
        self.assertEqual(turn['text'], self.phrases[0])
        self.assertTrue(turn['audio_path'])
        self.assertTrue(turn['audio_sha256'])
        self.assertIn('/audio/0', turn['audio_url'])
        # the play instruction the server issued ...
        self.assertTrue(turn['play_issued_at'])
        # ... is distinct from what the browser reported about playback
        self.assertTrue(turn['playback_started_at'])
        self.assertEqual(turn['phase'], 'observed')
        # what was observed, and on what basis
        self.assertEqual(turn['observation'], 'speech_end')
        self.assertEqual(turn['observation_basis'], 'browser_vad_rms')
        # why the turn closed
        self.assertTrue(turn['closure_reason'])
        self.assertTrue(turn['closed'])

        self.assertEqual(len(self.events_of(record, 'play_issued')), 2)
        self.assertEqual(len(self.events_of(record, 'playback_started')), 1)
        self.assertEqual(len(self.events_of(record, 'observation_speech_start')), 1)
        self.assertEqual(len(self.events_of(record, 'observation_speech_end')), 1)
        self.assertEqual(self.events_of(record, 'playback_started')[0]['source'], 'browser')
        self.assertEqual(self.events_of(record, 'play_issued')[0]['source'], 'server')

        # the record lives inside the session directory
        self.assertTrue((self.output_root / 'voice-test' / session_id
                         / 'execution-record.json').is_file())

    def test_record_does_not_claim_measured_latency_or_speaker_identity(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            self.suspect_response(ws, play['turn_id'], expect_play=True)

        record = self.record(session_id)
        blob = json.dumps(record, ensure_ascii=False)
        for forbidden in ('latency_ms', 'response_latency', 'speaker_role', 'speaker_identity'):
            self.assertNotIn(forbidden, blob)
        start_event = self.events_of(record, 'observation_speech_start')[0]
        self.assertEqual(start_event['detail']['observation_basis'], 'browser_vad_rms')
        self.assertIn('suspected', start_event['detail']['note'])

    def test_duplicate_and_late_events_do_not_advance_twice(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            first = self.start_run(ws)
            second = self.suspect_response(ws, first['turn_id'], expect_play=True)
            self.assertEqual(second['phrase_index'], 1)

            for stale in ({'type': 'device_speech_end', 'turn_id': first['turn_id']},
                          {'type': 'device_speech_start', 'turn_id': first['turn_id']},
                          {'type': 'device_speech_end', 'turn_id': 'T999'},
                          {'type': 'device_speech_end'},
                          {'type': 'observation_timeout', 'turn_id': first['turn_id']}):
                ws.send_json(stale)
                ignored = ws.receive_json()
                self.assertEqual(ignored['type'], 'ignored', stale)

            # the run advanced exactly once, not six times
            state = self.session_state(session_id)
            self.assertEqual(state.current_phrase_index, 1)
            self.assertEqual(len(state.turns), 2)
            self.assertEqual(state.awaiting_turn_id, second['turn_id'])

    def test_playback_failure_marks_the_run_failed_and_stays_recoverable(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            ws.send_json({'type': 'playback_failed', 'turn_id': play['turn_id'],
                          'reason': 'audio_error'})
            failed = ws.receive_json()
            self.assertEqual(failed['type'], 'failed')
            self.assertEqual(failed['detail'], 'audio_error')

            # the same connection can restart the run
            restarted = self.start_run(ws)
            self.assertEqual((restarted['type'], restarted['phrase_index']), ('play', 0))
            self.assertNotEqual(restarted['turn_id'], play['turn_id'])
            live = self.session_state(session_id)
            self.assertEqual(live.status, 'running')
            self.assertEqual(live.run_index, 2)

        record = self.record(session_id)
        failed_event = self.events_of(record, 'playback_failed')[0]
        self.assertEqual(failed_event['source'], 'browser')
        self.assertEqual(failed_event['detail']['reason'], 'audio_error')
        # the restart did not erase the failed run
        archived = [run for run in record['runs'] if not run['current']]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]['status'], 'failed')
        self.assertEqual(archived[0]['turns'][0]['status'], 'failed')
        self.assertIn('playback_failed', archived[0]['turns'][0]['closure_reason'])

    def test_stop_is_idempotent_and_ignores_late_events_on_a_stopped_session(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            ws.send_json({'type': 'stop'})
            stopped = ws.receive_json()
            self.assertEqual((stopped['type'], stopped['reason']), ('stopped', 'user_stop'))

        state = self.session_state(session_id)
        self.assertEqual(state.status, 'stopped')
        self.assertEqual(state.stop_reason, 'user_stop')
        self.assertEqual(state.current_phrase_index, 0)
        turn = state.turns[0]
        self.assertTrue(turn.closed)
        self.assertEqual(turn.status, 'cancelled')
        self.assertEqual(turn.closure_reason, 'user_stop')
        self.assertIsNone(state.awaiting_turn_id)

        # a late observation for the cancelled turn must not revive the run
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            ws.send_json({'type': 'device_speech_end', 'turn_id': play['turn_id']})
            self.assertEqual(ws.receive_json()['type'], 'ignored')
            ws.send_json({'type': 'stop'})  # repeated stop is not an error
            self.assertEqual(ws.receive_json()['type'], 'stopped')

        final = self.session_state(session_id)
        self.assertEqual(final.status, 'stopped')
        self.assertEqual(len(final.turns), 1)
        self.assertEqual(final.current_phrase_index, 0)


class NoResponseTimeoutTests(ControlLoopTestCase):
    def test_timeout_pauses_and_is_not_recorded_as_a_response(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            ws.send_json({'type': 'playback_ended', 'turn_id': play['turn_id']})
            ws.send_json({'type': 'observation_timeout', 'turn_id': play['turn_id'],
                          'wait_ms': self.no_response_timeout_ms})
            stopped = ws.receive_json()
            self.assertEqual(stopped['type'], 'stopped')
            self.assertEqual(stopped['reason'], 'no_response_timeout')

        state = self.session_state(session_id)
        self.assertEqual(state.status, 'stopped')
        self.assertEqual(state.stop_reason, 'no_response_timeout')
        turn = state.turns[0]
        self.assertEqual(turn.observation, 'no_response')
        self.assertEqual(turn.closure_reason, 'no_response_timeout')
        self.assertEqual(turn.phase, 'no_response')
        self.assertEqual(state.current_phrase_index, 0)

        record = self.record(session_id)
        kinds = [e['kind'] for e in record['events']]
        self.assertIn('observation_timeout', kinds)
        self.assertNotIn('observation_speech_end', kinds)
        timeout_event = self.events_of(record, 'observation_timeout')[0]
        self.assertEqual(timeout_event['source'], 'browser')
        # a timeout carries no VAD basis: it is the absence of an observation
        self.assertNotIn('observation_basis', timeout_event['detail'])
        self.assertEqual(timeout_event['detail']['wait_ms'], self.no_response_timeout_ms)

    def test_continue_policy_advances_but_never_as_an_observed_response(self):
        session_id = self.create_fixed(on_no_response='continue')
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            ws.send_json({'type': 'observation_timeout', 'turn_id': play['turn_id'],
                          'wait_ms': self.no_response_timeout_ms})
            nxt = ws.receive_json()
            self.assertEqual((nxt['type'], nxt['phrase_index']), ('play', 1))
            state = self.session_state(session_id)
            self.assertEqual(state.status, 'running')
            self.assertEqual(state.turns[0].observation, 'no_response')
            self.assertEqual(state.turns[0].closure_reason, 'no_response_timeout')

    def test_invalid_control_policy_is_rejected(self):
        bad_policy = self.client.post('/api/voice-test/sessions', json={
            'mode': 'fixed', 'phrases': ['x'], 'on_no_response': 'ignore'})
        self.assertEqual(bad_policy.status_code, 400)
        bad_bound = self.client.post('/api/voice-test/sessions', json={
            'mode': 'fixed', 'phrases': ['x'], 'no_response_timeout_ms': 5})
        self.assertEqual(bad_bound.status_code, 400)
        good = self.client.post('/api/voice-test/sessions', json={
            'mode': 'fixed', 'phrases': ['x'], 'on_no_response': 'continue',
            'no_response_timeout_ms': 1200})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()['on_no_response'], 'continue')
        self.assertEqual(good.json()['no_response_timeout_ms'], 1200)


class ExecutionRecordAvailabilityTests(ControlLoopTestCase):
    def test_record_outlives_the_in_memory_session(self):
        session_id = self.create_fixed()
        with self.client.websocket_connect(self.ws_url(session_id)) as ws:
            play = self.start_run(ws)
            self.suspect_response(ws, play['turn_id'], expect_play=True)

        # simulate a backend restart: the in-memory session is gone
        self.manager.sessions.pop(session_id)

        still_there = self.client.get(f'/api/voice-test/sessions/{session_id}')
        self.assertEqual(still_there.status_code, 200)
        self.assertEqual(still_there.json()['session_id'], session_id)
        exported = self.record(session_id)
        # the trace survives even though the live session object does not
        self.assertEqual(exported['record_version'], '1.1.0')
        self.assertTrue(exported['events'])
        self.assertTrue(self.current_run(exported)['turns'])

    def test_missing_session_and_missing_record_are_404(self):
        self.assertEqual(self.client.get('/api/voice-test/sessions/VT-nope').status_code, 404)
        self.assertEqual(
            self.client.get('/api/voice-test/sessions/VT-nope/execution-record').status_code, 404)

    def test_websocket_for_unknown_session_closes_with_4004(self):
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect('/api/voice-test/sessions/VT-nope/ws'):
                pass
        self.assertEqual(caught.exception.code, 4004)


if __name__ == '__main__':
    unittest.main()
