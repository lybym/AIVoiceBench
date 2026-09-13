"""Backend integration for the free-mode Streaming ASR loop (PRD-F016/F021/F023).

Drives the real HTTP + WebSocket surface of ``aivoicebench.api``:

    browser audio -> audio socket -> StreamingASRProvider -> unified events
    -> session control trace -> observation -> LLM agent -> next TTS/play

The ASR provider and the LLM agent are deterministic stand-ins, so this is
software verification only: no cloud call, no audio device, no physical AI
device. The transport contract, the fallback labelling and the "an empty
transcript is not an answer" rule are all exercised here.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from aivoicebench import api  # noqa: E402
from aivoicebench.model_settings import RunProviders  # noqa: E402
from aivoicebench.streaming_asr import (EVENT_FINAL, EVENT_PARTIAL, EVENT_SESSION_CLOSED,
                                        EVENT_SESSION_STARTED, EVENT_SPEECH_ENDED,
                                        SOURCE_PROVIDER, StreamingASREvent)  # noqa: E402
from aivoicebench.voice_test import VoiceTestManager  # noqa: E402
from browser_server import ScriptedFileASR, ScriptedJudge  # noqa: E402

FFMPEG = shutil.which('ffmpeg')


def canonical_wav_bytes(duration_ms=500, value=1200):
    """A real 16 kHz mono PCM16 WAV, as bytes, for the upload endpoint."""
    import io
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(value.to_bytes(2, 'little', signed=True) * (16 * duration_ms))
    return buffer.getvalue()


class MockTTSProvider:
    def __init__(self, root, *args, **kwargs):
        self.root = Path(root)

    def synthesize(self, text, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b'\x00\x00' * 160)
        import hashlib
        data = destination.read_bytes()
        return {'path': str(destination), 'sha256': hashlib.sha256(data).hexdigest(),
                'size_bytes': len(data), 'format': 'wav', 'sample_rate': 16000, 'text': text}


class ScriptedStreamingSession:
    """Deterministic streaming session: no network, no vendor dependency."""

    def __init__(self, *, stream_id='STR-test', script=None, failure=None,
                 normal_close=False):
        self.stream_id = stream_id
        self.script = list(script or [])
        self.failure = failure
        # ``normal_close`` mirrors the documented async endpoint: it returns a
        # definite final and then closes the socket itself, without ever setting
        # the documented ``is_last_package`` field.
        self.normal_close = normal_close
        self.audio_bytes = 0
        self.received = []
        self.finished = False
        self.saw_last_package = False
        self.terminated = False
        self.termination_basis = None
        self.close_code = None
        self._events = []
        self.state = 'open'
        self.profile = {'provider': 'scripted', 'model_id': 'scripted-streaming'}

    async def push_audio(self, pcm):
        self.received.append(pcm)
        self.audio_bytes += len(pcm)
        if self.script:
            text = self.script.pop(0)
            self._events.append(StreamingASREvent(kind=EVENT_PARTIAL, source=SOURCE_PROVIDER,
                                                  text=text, sequence=len(self.received)))

    async def finish_input(self):
        self.finished = True
        if self.received:
            parts = []
            text = '这是设备的回答'
            self._events.append(StreamingASREvent(kind=EVENT_FINAL, source=SOURCE_PROVIDER,
                                                  text=text, basis='provider_endpoint'))
            self._events.append(StreamingASREvent(kind=EVENT_SPEECH_ENDED,
                                                  source=SOURCE_PROVIDER,
                                                  basis='provider_endpoint'))
        if self.normal_close:
            self.terminated = True
            self.termination_basis = 'provider_normal_close'
            self.close_code = 1000
            self.state = 'finished'
            return
        # Mirrors the real adapter: the documented last package is also a
        # termination, recorded with its own basis.
        self.saw_last_package = True
        self.terminated = True
        self.termination_basis = 'provider_last_package'
        self.state = 'finished'

    def poll_events(self):
        events, self._events = self._events, []
        return events

    async def wait_events(self, timeout):  # pragma: no cover - nothing left to wait for
        return self.poll_events()

    async def close(self, reason='client_finished'):
        self.state = 'closed'
        self._events.append(StreamingASREvent(kind=EVENT_SESSION_CLOSED,
                                              source=SOURCE_PROVIDER, basis=reason))

    async def cancel(self, reason='client_cancelled'):
        self.state = 'cancelled'

    def summary(self):
        return {'stream_id': self.stream_id, 'audio_bytes': self.audio_bytes,
                'state': self.state, 'failure': self.failure,
                'evidence_scope': 'control_evidence'}


class ScriptedStreamingProvider:
    def __init__(self, *, script=None, failure=None, empty=False, normal_close=False):
        self.script = script
        self.failure = failure
        self.empty = empty
        self.normal_close = normal_close
        self.sessions = []

    async def start_session(self, *, session_id, turn_id, run_index, directory):
        session = ScriptedStreamingSession(
            script=[] if self.empty else (self.script or ['南京']), failure=self.failure,
            normal_close=self.normal_close)
        if self.empty:
            # The empty stream still terminates like the real adapter: the
            # documented last package arrives, it just carries no transcript.
            # The flags belong to the *session* being finalised, not to this
            # provider object.
            async def _empty_finish(_session=session):
                _session.finished = True
                _session.saw_last_package = True
                _session.terminated = True
                _session.termination_basis = 'provider_last_package'
                _session.state = 'finished'

            session.finish_input = _empty_finish
        self.sessions.append(session)
        return session


class FreeModeHarness:
    """Shared fixture for the free-mode backend integration tests.

    The ASR provider, the LLM agent and TTS are deterministic stand-ins; the
    HTTP and WebSocket surface, the session state and the execution record are
    the real ones.
    """

    phrases = ['第一个问题', '第二个问题']

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output_root = self.root / 'out'
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.manager = VoiceTestManager(self.output_root)
        self.file_asr = ScriptedFileASR()
        self.provider = ScriptedStreamingProvider()
        # The run's capability set is injected, so "which capability is present"
        # is explicit in the test rather than an accident of the environment.
        self.manager._providers = RunProviders(
            tts=lambda r: MockTTSProvider(r),
            judge=ScriptedJudge(),
            streaming_asr=lambda root: self.provider,
            asr=lambda root: self.file_asr,
        )
        self.agent_calls = []

        patches = [
            patch.object(api, '_voice_test_manager', self.manager),
            patch.object(api, 'OUTPUT_ROOT', self.output_root),
            patch('aivoicebench.voice_agent.generate_next_phrase', self._fake_agent),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        self.control = TestClient(api.app)
        self.addCleanup(self.control.close)
        self.audio = TestClient(api.app)
        self.addCleanup(self.audio.close)

    def _fake_agent(self, session, history, device_text, output_root, manager, turn_index=None):
        self.agent_calls.append({'device_text': device_text, 'history': list(history),
                                 'turn_index': turn_index})
        # The real agent records its provider call; the double does the same so
        # the session's own call audit is meaningful in these tests.
        manager.note_provider_call(session, 'llm')
        if len(self.agent_calls) > len(self.phrases):
            return None
        text = self.phrases[len(self.agent_calls) - 1]
        audio = manager.synthesize_text(session.session_id, text, turn_index)
        return text, audio['path']

    # ------------------------------------------------------------- helpers

    def create_free_session(self, **extra):
        body = {'mode': 'free', 'goal': '测试天气', 'max_turns': 4}
        body.update(extra)
        response = self.control.post('/api/voice-test/sessions', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['session_id']

    def start_control(self, session_id):
        """Open the control socket and consume capture_mode + the first play."""
        return self.control.websocket_connect(f'/api/voice-test/sessions/{session_id}/ws')

    def send_capture(self, session_id, turn_id, *, sample_rate=16000, channels=1, bits=16,
                     frames=4, sequences=None, stop=True):
        """Stream PCM over the audio socket and return the terminal message.

        Display updates (partial/final transcript) may arrive first; they are
        collected on the returned message under ``seen``.
        """
        seen = []
        with self.audio.websocket_connect(
                f'/api/voice-test/sessions/{session_id}/audio') as ws:
            ws.send_json({'type': 'capture_started', 'turn_id': turn_id,
                          'sample_rate': sample_rate, 'channels': channels, 'bits': bits})
            ready = ws.receive_json()
            if ready.get('type') != 'capture_ready':
                ready['seen'] = seen
                return ready
            for index in range(frames):
                sequence = sequences[index] if sequences else index
                pcm = b'\x01\x02' * 1600
                ws.send_bytes(sequence.to_bytes(4, 'big') + pcm)
            if stop:
                ws.send_json({'type': 'capture_stopped', 'reason': 'speech_end'})
                for _ in range(20):
                    message = ws.receive_json()
                    if message.get('type') in ('capture_result', 'capture_error'):
                        message['seen'] = seen
                        return message
                    seen.append(message)
        return None

    def record(self, session_id):
        response = self.control.get(f'/api/voice-test/sessions/{session_id}/execution-record')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

class FreeModeStreamingTests(FreeModeHarness, unittest.TestCase):
    """Free-mode Streaming ASR loop, transport and fallback labelling."""

    # --------------------------------------------------------------- tests

    def test_streaming_loop_reaches_the_agent_and_the_trace(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            mode = control.receive_json()
            self.assertEqual(mode['type'], 'capture_mode')
            self.assertEqual(mode['mode'], 'streaming')
            self.assertIsNone(mode['fallback_reason'])

            play = control.receive_json()
            self.assertEqual(play['type'], 'play')
            self.assertEqual(play['capture']['mode'], 'streaming')
            first_turn = play['turn_id']

            # The browser streams the captured answer, then reports the result.
            result = self.send_capture(session_id, first_turn)
            self.assertEqual(result['type'], 'capture_result')
            self.assertEqual(result['final_text'], '这是设备的回答')
            self.assertFalse(result['empty_transcript'])
            self.assertEqual(self.provider.sessions[0].state, 'closed',
                             'a completed capture must close its provider session')
            # Partial text is pushed to the page while the turn is still running.
            self.assertTrue([m for m in result['seen'] if m['type'] == 'partial_transcript'],
                            result['seen'])
            self.assertTrue(all(m.get('evidence_scope') == 'control_evidence'
                                for m in result['seen']))

            control.send_json({'type': 'capture_result', 'turn_id': first_turn,
                               'stream_id': result['stream_id']})
            nxt = control.receive_json()
            self.assertEqual(nxt['type'], 'play')
            self.assertEqual(nxt['device_text'], '这是设备的回答')
            self.assertEqual(nxt['observation_scope'], 'control_evidence')
            self.assertNotEqual(nxt['turn_id'], first_turn)

            # A second streaming round: the device's reply from round one must
            # now be part of the conversation the agent reads.
            second = self.send_capture(session_id, nxt['turn_id'])
            self.assertEqual(second['type'], 'capture_result')
            control.send_json({'type': 'capture_result', 'turn_id': nxt['turn_id']})
            third = control.receive_json()
            self.assertIn(third['type'], ('play', 'complete'))

        # The agent's first decision had no device answer; the second received the
        # observation separately; the third also saw it in the history.
        self.assertEqual(self.agent_calls[0]['device_text'], '')
        self.assertEqual(self.agent_calls[1]['device_text'], '这是设备的回答')
        self.assertTrue(any(turn.get('role') == 'device' and turn.get('text') == '这是设备的回答'
                            for turn in self.agent_calls[2]['history']),
                        'the device reply must reach the next decision through the history')

        # Conversation order is question -> answer -> question, not the reverse.
        session = self.manager.get_session(session_id)
        roles = [turn.role for turn in session.turns]
        self.assertEqual(roles[:3], ['platform', 'device', 'platform'], roles)

        record = self.record(session_id)
        self.assertEqual(record['resolved_capture_mode'], 'streaming')
        self.assertIsNone(record['streaming_asr_error'])
        kinds = [e['kind'] for e in record['events']]
        self.assertIn('capture_started', kinds)
        self.assertIn('capture_finished', kinds)
        self.assertIn('device_observation', kinds)
        self.assertIn('partial_transcript', kinds)
        self.assertIn('final_transcript', kinds)
        text_events = [e for e in record['events'] if e['kind'] == 'final_transcript']
        self.assertEqual(text_events[0]['detail']['evidence_scope'], 'control_evidence')
        self.assertIn('asr_session_closed', kinds)

    def test_streaming_capture_is_summarised_in_the_record(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            self.send_capture(session_id, play['turn_id'])
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            control.receive_json()

        record = self.record(session_id)
        started = [e for e in record['events'] if e['kind'] == 'capture_started'][0]
        self.assertEqual(started['detail']['evidence_scope'], 'control_evidence')
        self.assertEqual(started['detail']['audio'],
                         {'format': 'pcm_s16le', 'sample_rate': 16000, 'bits': 16, 'channels': 1})
        finished = [e for e in record['events'] if e['kind'] == 'capture_finished'][0]
        self.assertEqual(finished['detail']['final_text'], '这是设备的回答')
        self.assertEqual(finished['detail']['final_basis'], 'provider_endpoint')
        self.assertGreater(finished['detail']['audio_bytes'], 0)
        summary = [e for e in record['events'] if e['kind'] == 'device_observation'][0]
        self.assertEqual(summary['detail']['capture_mode'], 'streaming')
        self.assertNotIn('payload', json.dumps(record))

    def test_normal_provider_close_finishes_the_capture_without_waiting_for_the_bound(self):
        """A normal provider close after a definite final ends the capture at once.

        Real-cloud evidence (session VT-e9adbbe08a1d): the provider returned a
        definite final and then closed the socket normally (ConnectionClosedOK)
        without ever sending the documented last package. The capture must finish
        with the final text instead of staying active until the control bound.
        """
        self.provider.normal_close = True
        session_id = self.create_free_session(no_response_timeout_ms=8000)
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            started = time.monotonic()
            result = self.send_capture(session_id, play['turn_id'])
            elapsed = time.monotonic() - started

            self.assertEqual(result['type'], 'capture_result')
            self.assertEqual(result['status'], 'finished')
            self.assertIsNone(result['failure'])
            self.assertEqual(result['final_text'], '这是设备的回答')
            self.assertEqual(result['final_basis'], 'provider_endpoint')
            self.assertFalse(result['empty_transcript'])
            self.assertLess(elapsed, 4.0,
                            'a normal close must not wait for the no-response bound')

            stream = self.provider.sessions[0]
            self.assertTrue(stream.terminated)
            self.assertEqual(stream.termination_basis, 'provider_normal_close')
            self.assertEqual(stream.close_code, 1000)
            self.assertFalse(stream.saw_last_package,
                             'the documented is_last_package field never arrived')

            # The turn still closes and the conversation advances.
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            nxt = control.receive_json()
            self.assertEqual(nxt['type'], 'play')
            self.assertEqual(nxt['device_text'], '这是设备的回答')
            self.assertNotEqual(nxt['turn_id'], play['turn_id'])

        record = self.record(session_id)
        finished = [e for e in record['events'] if e['kind'] == 'capture_finished'][0]
        self.assertEqual(finished['detail']['final_text'], '这是设备的回答')
        self.assertEqual(finished['detail']['final_basis'], 'provider_endpoint')
        self.assertEqual(finished['detail']['status'], 'finished')
        observation = [e for e in record['events'] if e['kind'] == 'device_observation'][0]
        self.assertEqual(observation['detail']['capture_mode'], 'streaming')
        self.assertEqual(observation['detail']['final_basis'], 'provider_endpoint')
        self.assertNotIn('stream_disconnected', json.dumps(record))

    def test_observed_transcript_closes_the_turn_and_advances(self):
        """The platform turn is closed by the verified transcript, not left open."""
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            self.send_capture(session_id, play['turn_id'])
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            nxt = control.receive_json()
            self.assertEqual(nxt['type'], 'play')
            self.assertNotEqual(nxt['turn_id'], play['turn_id'])

        record = self.record(session_id)
        run = [item for item in record['runs'] if item['current']][0]
        observed = [turn for turn in run['turns'] if turn['turn_id'] == play['turn_id']][0]
        self.assertTrue(observed['closed'])
        self.assertEqual(observed['status'], 'complete')
        self.assertEqual(observed['closure_reason'], 'device_transcript')
        closed = [event for event in record['events'] if event['kind'] == 'turn_closed'
                  and event['turn_id'] == play['turn_id']]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]['detail']['closure_reason'], 'device_transcript')
        self.assertNotEqual(record['awaiting_turn_id'], play['turn_id'])

    def test_requested_streaming_without_a_provider_refuses_to_start(self):
        """A missing Streaming ASR is a refusal, not a silent downgrade.

        Neither the control socket nor the HTTP start endpoint may fall back on
        its own, and the refusal must happen before any model or speech call.
        """
        original = self.manager._providers
        self.manager._providers = RunProviders(
            tts=original.tts, judge=original.judge, asr=original.asr)
        try:
            session_id = self.create_free_session(capture_mode='streaming')
            # The HTTP start endpoint enforces the same check.
            blocked = self.control.post(f'/api/voice-test/sessions/{session_id}/start')
            self.assertEqual(blocked.status_code, 409, blocked.text)
            detail = blocked.json()['detail']
            self.assertEqual(detail['reason'], 'capability_precheck_failed')
            self.assertIn('streaming_asr', detail['missing'])
            self.assertEqual(detail['connectivity'], 'not_probed')

            with self.start_control(session_id) as control:
                control.send_json({'type': 'start'})
                message = control.receive_json()
                self.assertEqual(message['type'], 'blocked')
                self.assertEqual(message['reason'], 'capability_precheck_failed')
                self.assertIn('streaming_asr', message['missing'])
        finally:
            self.manager._providers = original

        session = self.manager.get_session(session_id)
        # A refused start never opened a run: no turn, no provider call.
        self.assertEqual(session.run_index, 0)
        self.assertEqual(session.turns, [])
        self.assertEqual(session.provider_calls, {'tts': 0, 'llm': 0, 'asr': 0})
        self.assertEqual(self.agent_calls, [])
        record = self.record(session_id)
        self.assertIn('session_start_refused', [e['kind'] for e in record['events']])
        self.assertNotIn('play_issued', [e['kind'] for e in record['events']])

    def test_explicit_turn_file_downgrade_uses_the_server_side_upload(self):
        """The fallback runs only when chosen explicitly, and only its own upload.

        The transcript must come from the server's own capture record: a browser
        payload cannot advance the conversation, and the recording is decoded and
        converted before File ASR sees it.
        """
        original = self.manager._providers
        self.manager._providers = RunProviders(
            tts=original.tts, judge=original.judge, asr=original.asr)
        try:
            session_id = self.create_free_session(capture_mode='turn_file')
            with self.start_control(session_id) as control:
                control.send_json({'type': 'start'})
                mode = control.receive_json()
                self.assertEqual(mode['mode'], 'turn_file')
                self.assertEqual(mode['fallback_reason'], 'configured_turn_file')
                play = control.receive_json()
                self.assertEqual(play['capture']['mode'], 'turn_file')
                self.assertTrue(play['capture_id'])

                # A browser-supplied transcript without a recognised upload is
                # ignored, never treated as an observation.
                control.send_json({'type': 'device_audio_ready', 'turn_id': play['turn_id'],
                                   'transcript': '浏览器自报的转写'})
                ignored = control.receive_json()
                self.assertEqual(ignored['type'], 'ignored')

                if FFMPEG is None:  # pragma: no cover - environment dependent
                    self.skipTest('ffmpeg is required for the fallback decode path')
                uploaded = self.control.post(
                    f'/api/voice-test/sessions/{session_id}/device-audio',
                    content=canonical_wav_bytes(),
                    headers={'Content-Type': 'audio/wav', 'X-Voice-Mime-Type': 'audio/wav',
                             'X-Voice-Capture-Id': play['capture_id'],
                             'X-Voice-Turn-Id': play['turn_id']})
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                body = uploaded.json()
                self.assertEqual(body['status'], 'recognized')
                self.assertEqual(body['capture_id'], play['capture_id'])

                # The recogniser received canonical 16 kHz mono PCM16, not the
                # browser's container.
                self.assertTrue(self.file_asr.calls, 'File ASR was never called')
                self.assertEqual(self.file_asr.calls[0]['sample_rate'], 16000)
                self.assertEqual(self.file_asr.calls[0]['channels'], 1)
                self.assertEqual(self.file_asr.calls[0]['sample_width'], 2)

                control.send_json({'type': 'device_audio_ready', 'turn_id': play['turn_id'],
                                   'capture_id': play['capture_id']})
                nxt = control.receive_json()
                self.assertEqual(nxt['type'], 'play')
                self.assertEqual(nxt['device_text'], '降级路径转写：南京明天晴')

                # The same capture cannot be consumed twice.
                control.send_json({'type': 'device_audio_ready', 'turn_id': play['turn_id'],
                                   'capture_id': play['capture_id']})
                self.assertEqual(control.receive_json()['type'], 'ignored')
        finally:
            self.manager._providers = original

        record = self.record(session_id)
        observation = [e for e in record['events'] if e['kind'] == 'device_observation'][0]
        self.assertEqual(observation['detail']['capture_mode'], 'turn_file')
        self.assertEqual(record['resolved_capture_mode'], 'turn_file')

    def test_invalid_media_and_asr_failure_are_not_empty_successes(self):
        """Invalid media, a recogniser failure and an empty result are failures."""
        if FFMPEG is None:  # pragma: no cover - environment dependent
            self.skipTest('ffmpeg is required for the fallback decode path')
        original = self.manager._providers
        self.manager._providers = RunProviders(
            tts=original.tts, judge=original.judge, asr=original.asr)
        try:
            session_id = self.create_free_session(capture_mode='turn_file')
            with self.start_control(session_id) as control:
                control.send_json({'type': 'start'})
                control.receive_json()
                play = control.receive_json()
                headers = {'X-Voice-Turn-Id': play['turn_id']}
                # 1. Content that is not the declared container at all.
                wrong = self.control.post(
                    f'/api/voice-test/sessions/{session_id}/device-audio',
                    content=b'\x00' * 2048,
                    headers={**headers, 'Content-Type': 'audio/webm',
                             'X-Voice-Mime-Type': 'audio/webm',
                             'X-Voice-Capture-Id': 'cap-' + 'a' * 16})
                self.assertEqual(wrong.status_code, 422, wrong.text)
                record = self.record(session_id)
                states = [e['detail']['status'] for e in record['events']
                          if e['kind'] == 'fallback_upload_finished']
                self.assertEqual(states[-1], 'invalid_audio')

                # 2. A recogniser failure.
                self.file_asr.fail = True
                headers2 = {**headers, 'Content-Type': 'audio/wav',
                            'X-Voice-Mime-Type': 'audio/wav',
                            'X-Voice-Capture-Id': 'cap-' + 'b' * 16}
                failed = self.control.post(
                    f'/api/voice-test/sessions/{session_id}/device-audio',
                    content=canonical_wav_bytes(), headers=headers2)
                self.assertEqual(failed.status_code, 502, failed.text)
                record = self.record(session_id)
                states = [e['detail']['status'] for e in record['events']
                          if e['kind'] == 'fallback_upload_finished']
                self.assertEqual(states[-1], 'asr_failed')

                # 3. Recognised, but no usable text: still not a success.
                self.file_asr.fail = False
                self.file_asr.text = ''
                headers3 = {**headers, 'Content-Type': 'audio/wav',
                            'X-Voice-Mime-Type': 'audio/wav',
                            'X-Voice-Capture-Id': 'cap-' + 'c' * 16}
                empty = self.control.post(
                    f'/api/voice-test/sessions/{session_id}/device-audio',
                    content=canonical_wav_bytes(), headers=headers3)
                self.assertEqual(empty.status_code, 502, empty.text)

                # None of the failures advanced the conversation: only the
                # opening turn was ever generated, and no device text reached it.
                self.assertEqual(len(self.agent_calls), 1, self.agent_calls)
                self.assertEqual(self.agent_calls[0]['device_text'], '')
        finally:
            self.manager._providers = original
        record = self.record(session_id)
        self.assertEqual(record['resolved_capture_mode'], 'turn_file')
        kinds = [e['kind'] for e in record['events']]
        self.assertNotIn('device_observation', kinds)
        self.assertEqual(len([t for t in record['runs'] if t['current']][0]['turns']), 1)

    def test_empty_transcript_is_not_treated_as_an_answer(self):
        self.provider.empty = True
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            result = self.send_capture(session_id, play['turn_id'])
            self.assertTrue(result['empty_transcript'])
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            stopped = control.receive_json()
            self.assertEqual(stopped['type'], 'stopped')
        self.assertIn('no_device_transcript', stopped['reason'])
        # The agent was never asked to continue on an empty answer.
        self.assertEqual(len(self.agent_calls), 1)
        record = self.record(session_id)
        self.assertEqual(record['status'], 'stopped')
        self.assertEqual(record['streaming_capture'], None)

    def test_unsupported_audio_format_is_refused(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            result = self.send_capture(session_id, play['turn_id'], sample_rate=8000)
        self.assertEqual(result['type'], 'capture_error')
        self.assertEqual(result['error'], 'invalid_audio')
        self.assertEqual(self.provider.sessions, [])

    def test_stale_turn_capture_is_refused(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            control.receive_json()
            result = self.send_capture(session_id, 'T-does-not-exist')
        self.assertEqual(result['type'], 'capture_error')
        self.assertEqual(result['note'], 'no turn is awaiting an observation')

    def test_frame_ordering_is_counted(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            # 0, 1, 3 (gap), 3 (duplicate), 2 (late)
            self.send_capture(session_id, play['turn_id'],
                              sequences=[0, 1, 3, 3, 2], frames=5)
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            control.receive_json()
        session = self.manager.get_session(session_id)
        ordering = session.last_capture_result['frame_ordering']
        self.assertEqual(ordering['gap'], 1)
        self.assertEqual(ordering['duplicate'], 1)
        self.assertEqual(ordering['late'], 1)
        self.assertEqual(len(self.provider.sessions[0].received), 3,
                         'duplicate and late PCM must not be replayed into ASR')

    def test_audio_disconnect_cancels_provider_and_releases_capture(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            with self.audio.websocket_connect(
                    f'/api/voice-test/sessions/{session_id}/audio') as ws:
                ws.send_json({'type': 'capture_started', 'turn_id': play['turn_id'],
                              'sample_rate': 16000, 'channels': 1, 'bits': 16})
                self.assertEqual(ws.receive_json()['type'], 'capture_ready')
                ws.send_bytes((0).to_bytes(4, 'big') + b'\x01\x02' * 1600)
                # Leaving the context simulates the browser/audio socket going away
                # without a capture_stopped message.

        self.assertEqual(self.provider.sessions[0].state, 'cancelled')
        session = self.manager.get_session(session_id)
        self.assertIsNone(session.capture)
        self.assertEqual(session.last_capture_result['status'], 'cancelled')
        self.assertEqual(session.last_capture_result['failure'], 'stream_disconnected')

    def test_second_audio_socket_cannot_replace_an_active_capture(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            endpoint = f'/api/voice-test/sessions/{session_id}/audio'
            hello = {'type': 'capture_started', 'turn_id': play['turn_id'],
                     'sample_rate': 16000, 'channels': 1, 'bits': 16}
            with self.audio.websocket_connect(endpoint) as first:
                first.send_json(hello)
                self.assertEqual(first.receive_json()['type'], 'capture_ready')
                with self.audio.websocket_connect(endpoint) as second:
                    second.send_json(hello)
                    refused = second.receive_json()
                    self.assertEqual(refused['type'], 'capture_error')
                    self.assertIn('already active', refused['note'])
                self.assertEqual(len(self.provider.sessions), 1)

        self.assertEqual(self.provider.sessions[0].state, 'cancelled')

    def test_capture_result_without_a_capture_is_ignored(self):
        session_id = self.create_free_session()
        with self.start_control(session_id) as control:
            control.send_json({'type': 'start'})
            control.receive_json()
            play = control.receive_json()
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            ignored = control.receive_json()
        self.assertEqual(ignored['type'], 'ignored')
        self.assertIn('capture', ignored['reason'])


class MaxTurnsRoundSemanticsTests(FreeModeHarness, unittest.TestCase):
    """``max_turns=N`` means N *complete* platform rounds (PRD-F016/F021).

    A round is question -> playback -> device observation -> capture
    finalisation -> turn closure. The budget therefore ends the run *after* the
    Nth answer has been observed and closed — never right after the Nth question
    was issued, which left the last answer unobservable and forced operators to
    configure ``max_turns=N+1``.
    """

    # The scripted agent is willing to keep talking past the budget: it owns five
    # phrases, so only the server's turn budget can stop the run at three.
    phrases = ['问题一', '问题二', '问题三', '问题四', '问题五']

    # ------------------------------------------------------------- helpers

    def start_free_run(self, control):
        """Send ``start`` and return the first play instruction."""
        control.send_json({'type': 'start'})
        mode = control.receive_json()
        self.assertEqual(mode['type'], 'capture_mode', mode)
        play = control.receive_json()
        self.assertEqual(play['type'], 'play', play)
        return play

    def answer_over_streaming(self, control, session_id, play):
        """Stream one answer, let control consume it, and return the reply."""
        result = self.send_capture(session_id, play['turn_id'])
        self.assertEqual(result['type'], 'capture_result', result)
        self.assertEqual(result['final_text'], '这是设备的回答')
        control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
        return control.receive_json()

    def upload_answer(self, session_id, play):
        """Upload one whole-turn recording for the explicitly chosen fallback."""
        response = self.control.post(
            f'/api/voice-test/sessions/{session_id}/device-audio',
            content=canonical_wav_bytes(),
            headers={'Content-Type': 'audio/wav', 'X-Voice-Mime-Type': 'audio/wav',
                     'X-Voice-Capture-Id': play['capture_id'],
                     'X-Voice-Turn-Id': play['turn_id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'recognized', response.text)

    # --------------------------------------------------------------- tests

    def test_max_turns_one_waits_for_and_saves_the_first_answer(self):
        session_id = self.create_free_session(max_turns=1)
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            # Exactly one question was generated before any answer existed.
            self.assertEqual(len(self.agent_calls), 1)
            done = self.answer_over_streaming(control, session_id, play)
            self.assertEqual(done['type'], 'complete', done)
            self.assertEqual(done['reason'], 'max_turns')

        session = self.manager.get_session(session_id)
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.stop_reason, 'max_turns')
        self.assertIsNone(session.awaiting_turn_id)
        self.assertEqual([turn.role for turn in session.turns], ['platform', 'device'])
        self.assertTrue(all(turn.closed for turn in session.turns))
        self.assertEqual(session.turns[0].closure_reason, 'device_transcript')
        # The closing transcript is the evidence; no VAD observation is invented
        # for a turn whose answer was recognised rather than merely suspected.
        self.assertIsNone(session.turns[0].observation)
        self.assertEqual(session.turns[1].text, '这是设备的回答')
        # One question, one answer, one synthesis and one model decision.
        self.assertEqual(session.provider_calls, {'tts': 1, 'llm': 1, 'asr': 0})

        record = self.record(session_id)
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['stop_reason'], 'max_turns')
        self.assertIsNone(record['awaiting_turn_id'])
        kinds = [event['kind'] for event in record['events']]
        self.assertEqual(kinds.count('play_issued'), 1)
        self.assertEqual(kinds.count('device_observation'), 1)
        self.assertEqual(kinds.count('device_transcript'), 1)
        # The platform turn is closed; the device turn is created already closed.
        self.assertEqual(kinds.count('turn_closed'), 1)
        self.assertIn('session_completed', kinds)
        # No queue of questions was generated ahead of the observed answer.
        self.assertEqual([e['detail']['text'] for e in record['events']
                          if e['kind'] == 'play_issued'], ['问题一'])

    def test_max_turns_three_answers_three_and_never_asks_a_fourth(self):
        session_id = self.create_free_session(max_turns=3)
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            for index in range(3):
                self.assertEqual(play['type'], 'play', play)
                self.assertEqual(play['text'], self.phrases[index])
                self.assertEqual(len(self.agent_calls), index + 1)
                play = self.answer_over_streaming(control, session_id, play)
            # The third answer was observed and closed before the run ended.
            self.assertEqual(play['type'], 'complete', play)
            self.assertEqual(play['reason'], 'max_turns')

        session = self.manager.get_session(session_id)
        self.assertEqual(len(self.agent_calls), 3, self.agent_calls)
        self.assertEqual(session.provider_calls, {'tts': 3, 'llm': 3, 'asr': 0})
        self.assertEqual(session.status, 'completed')
        self.assertIsNone(session.awaiting_turn_id)
        self.assertEqual([turn.role for turn in session.turns],
                         ['platform', 'device'] * 3)
        self.assertTrue(all(turn.closed for turn in session.turns))
        self.assertEqual([turn.text for turn in session.turns if turn.role == 'device'],
                         ['这是设备的回答'] * 3)
        # The last platform turn is closed by its verified answer, not by the
        # budget itself.
        self.assertEqual(session.turns[-2].closure_reason, 'device_transcript')

        record = self.record(session_id)
        self.assertEqual(record['status'], 'completed')
        self.assertEqual([event['detail']['text'] for event in record['events']
                          if event['kind'] == 'play_issued'], self.phrases[:3])
        self.assertEqual(len([event for event in record['events']
                              if event['kind'] == 'device_observation']), 3)
        self.assertEqual(len([event for event in record['events']
                              if event['kind'] == 'capture_finished']), 3)

    def test_turn_file_fallback_has_the_same_round_semantics(self):
        original = self.manager._providers
        self.manager._providers = RunProviders(
            tts=original.tts, judge=original.judge, asr=original.asr)
        try:
            session_id = self.create_free_session(max_turns=2, capture_mode='turn_file')
            with self.start_control(session_id) as control:
                play = self.start_free_run(control)
                self.assertEqual(play['capture']['mode'], 'turn_file')
                for index in range(2):
                    self.assertEqual(play['text'], self.phrases[index])
                    self.upload_answer(session_id, play)
                    control.send_json({'type': 'device_audio_ready',
                                       'turn_id': play['turn_id'],
                                       'capture_id': play['capture_id']})
                    play = control.receive_json()
                self.assertEqual(play['type'], 'complete', play)
                self.assertEqual(play['reason'], 'max_turns')
        finally:
            self.manager._providers = original

        session = self.manager.get_session(session_id)
        self.assertEqual(len(self.agent_calls), 2)
        self.assertEqual(session.provider_calls['llm'], 2)
        self.assertIsNone(session.awaiting_turn_id)
        self.assertEqual([turn.role for turn in session.turns],
                         ['platform', 'device'] * 2)
        self.assertTrue(all(turn.closed for turn in session.turns))
        record = self.record(session_id)
        self.assertEqual(record['resolved_capture_mode'], 'turn_file')
        self.assertEqual(len([event for event in record['events']
                              if event['kind'] == 'play_issued']), 2)
        self.assertEqual(len([event for event in record['events']
                              if event['kind'] == 'device_observation']), 2)

    def test_last_round_provider_failure_ends_the_run_explicitly(self):
        # A recogniser that fails without producing a usable final: a real
        # provider failure, never an empty success.
        self.provider.empty = True
        self.provider.failure = 'provider_rate_limited'
        session_id = self.create_free_session(max_turns=1)
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            result = self.send_capture(session_id, play['turn_id'])
            self.assertTrue(result['empty_transcript'])
            self.assertEqual(result['failure'], 'provider_rate_limited')
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            stopped = control.receive_json()
        self.assertEqual(stopped['type'], 'stopped', stopped)
        self.assertIn('provider_rate_limited', stopped['reason'])
        # The failure never became an answer and never produced another question.
        self.assertEqual(len(self.agent_calls), 1)
        session = self.manager.get_session(session_id)
        self.assertEqual(session.status, 'stopped')
        self.assertIsNone(session.awaiting_turn_id)
        self.assertTrue(all(turn.closed for turn in session.turns))
        self.assertEqual([turn.role for turn in session.turns], ['platform'])
        record = self.record(session_id)
        observation = [event for event in record['events']
                       if event['kind'] == 'device_observation'][0]
        self.assertEqual(observation['detail']['text'], '')
        self.assertEqual(observation['detail']['failure'], 'provider_rate_limited')

    def test_last_round_no_response_timeout_completes_instead_of_hanging(self):
        session_id = self.create_free_session(max_turns=1, on_no_response='continue')
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            control.send_json({'type': 'observation_timeout', 'turn_id': play['turn_id'],
                               'wait_ms': 1000, 'reason': 'no_response_observed'})
            self.assertEqual(control.receive_json()['type'], 'no_response')
            done = control.receive_json()
        self.assertEqual(done['type'], 'complete', done)
        self.assertEqual(done['reason'], 'max_turns')
        # A silence is never turned into an answer or into another question.
        self.assertEqual(len(self.agent_calls), 1)
        session = self.manager.get_session(session_id)
        self.assertEqual(session.status, 'completed')
        self.assertIsNone(session.awaiting_turn_id)
        self.assertEqual(session.turns[0].observation, 'no_response')
        self.assertEqual(session.turns[0].closure_reason, 'no_response_timeout')
        self.assertEqual([turn.role for turn in session.turns], ['platform'])

    def test_mid_run_no_response_timeout_still_asks_the_next_question(self):
        session_id = self.create_free_session(max_turns=2, on_no_response='continue')
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            control.send_json({'type': 'observation_timeout', 'turn_id': play['turn_id'],
                               'wait_ms': 1000, 'reason': 'no_response_observed'})
            self.assertEqual(control.receive_json()['type'], 'no_response')
            second = control.receive_json()
            self.assertEqual(second['type'], 'play', second)
            self.assertEqual(second['text'], self.phrases[1])
            done = self.answer_over_streaming(control, session_id, second)
            self.assertEqual(done['type'], 'complete', done)

        session = self.manager.get_session(session_id)
        self.assertEqual(len(self.agent_calls), 2)
        self.assertEqual(session.status, 'completed')
        self.assertIsNone(session.awaiting_turn_id)
        self.assertEqual([turn.role for turn in session.turns],
                         ['platform', 'platform', 'device'])
        self.assertTrue(all(turn.closed for turn in session.turns))
        self.assertEqual(session.turns[0].closure_reason, 'no_response_timeout')

    def test_a_duplicate_capture_result_never_creates_a_second_turn(self):
        session_id = self.create_free_session(max_turns=2)
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            self.send_capture(session_id, play['turn_id'])
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            second = control.receive_json()
            self.assertEqual(second['type'], 'play')
            # The same answer reported twice must not advance the run twice.
            control.send_json({'type': 'capture_result', 'turn_id': play['turn_id']})
            ignored = control.receive_json()
            self.assertEqual(ignored['type'], 'ignored', ignored)
            # Still waiting on the second question, with no extra turn created.
            session = self.manager.get_session(session_id)
            self.assertEqual([turn.role for turn in session.turns],
                             ['platform', 'device', 'platform'])
            self.assertEqual(session.awaiting_turn_id, session.turns[2].turn_id)

    def test_a_late_stop_cannot_rewrite_a_completed_run(self):
        session_id = self.create_free_session(max_turns=1)
        with self.start_control(session_id) as control:
            play = self.start_free_run(control)
            self.answer_over_streaming(control, session_id, play)

        # A socket disconnect, a reconnect or a duplicate stop after the run
        # already ended must not re-close a turn or rewrite the outcome.
        self.manager.stop(session_id, reason='disconnected')
        session = self.manager.get_session(session_id)
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.stop_reason, 'max_turns')
        self.assertEqual(len(session.turns), 2)
        self.assertTrue(all(turn.closed for turn in session.turns))
        record = self.record(session_id)
        kinds = [event['kind'] for event in record['events']]
        self.assertEqual(kinds.count('session_completed'), 1)
        self.assertEqual(kinds.count('session_stopped'), 0)
        self.assertEqual(kinds.count('turn_closed'), 1)
        self.assertEqual(kinds.count('device_transcript'), 1)

    def test_max_turns_accepts_only_the_documented_integer_range(self):
        """1 and 50 are the bounds the page offers; both must be accepted as-is."""
        for value in (1, 50):
            response = self.control.post('/api/voice-test/sessions',
                                         json={'mode': 'free', 'goal': '边界',
                                               'max_turns': value})
            self.assertEqual(response.status_code, 200, (value, response.text))
            self.assertEqual(response.json()['max_turns'], value)
            self.assertIsInstance(response.json()['max_turns'], int)

    def test_max_turns_rejects_out_of_range_integers(self):
        for value in (0, -1, 51, 1000):
            response = self.control.post('/api/voice-test/sessions',
                                         json={'mode': 'free', 'goal': '边界',
                                               'max_turns': value})
            self.assertEqual(response.status_code, 400, (value, response.text))
            self.assertIn('max_turns', response.json()['detail'])
            self.assertIn('between 1 and 50', response.json()['detail'])

    def test_max_turns_rejects_json_values_that_only_look_like_a_count(self):
        """A turn budget is a count: 1.0, 1.5, true, false, "3" and null are not.

        ``bool`` is an ``int`` subclass in Python and ``int(1.5)`` truncates, so a
        conversion-based check would silently accept these as 1/0 rounds.
        """
        for value in (1.0, 1.5, True, False, '3', None, [], {}):
            response = self.control.post('/api/voice-test/sessions',
                                         json={'mode': 'free', 'goal': '边界',
                                               'max_turns': value})
            self.assertEqual(response.status_code, 400, (value, response.text))
            detail = response.json()['detail']
            self.assertIn('max_turns', detail, (value, detail))
            self.assertIn('integer', detail, (value, detail))
        # The manager enforces the same contract without going through HTTP.
        for value in (1.0, True, '3', None):
            with self.assertRaises(ValueError):
                self.manager.create_session('free', goal='边界', max_turns=value)
        session = self.manager.create_session('free', goal='边界', max_turns=1)
        self.assertEqual(session.max_turns, 1)


if __name__ == '__main__':
    unittest.main()
