"""Streaming ASR boundary + Volcengine SAUC protocol tests (PRD-F016/F021/F023).

These are software tests. Nothing here contacts Volcengine: the transport is
injected, and the fake server speaks the documented binary protocol so the test
decodes exactly the bytes the client puts on the wire. Real-cloud behaviour stays
``real_cloud_pending`` (see docs/24-streaming-asr.md).

Covered: framing in both directions, chunk aggregation and ordering, last-packet
framing, partial/final/endpoint mapping, empty-audio and no-final handling, the
documented error codes, handshake failure classification, cancellation, session
isolation, duplicate/late results, and the secret-free invocation audit.
"""

import asyncio
import gzip
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

from aivoicebench.streaming_asr import (BASIS_PROVIDER_ENDPOINT, ERROR_CATEGORIES,
                                        EVENT_ERROR, EVENT_FINAL,
                                        EVENT_PARTIAL, EVENT_SESSION_CLOSED,
                                        EVENT_SESSION_STARTED, EVENT_SPEECH_ENDED,
                                        SOURCE_BROWSER_VAD, SOURCE_COMBINED,
                                        SOURCE_PROVIDER, StreamingASREvent,
                                        StreamingASRUnavailable,
                                        UnavailableStreamingASRProvider,
                                        combine_sources)
from aivoicebench.volcengine_streaming_asr import (CHUNK_BYTES, CODE_EMPTY_AUDIO,
                                                   CODE_INVALID_AUDIO_FORMAT,
                                                   CODE_PACKET_TIMEOUT, CODE_SERVER_BUSY,
                                                   COMPRESSION_GZIP,
                                                   COMPRESSION_NONE, ENDPOINT,
                                                   ENDPOINT_ASYNC, FLAG_LAST_NO_SEQUENCE,
                                                   FLAG_NONE, MSG_AUDIO_ONLY_REQUEST,
                                                   MSG_FULL_CLIENT_REQUEST,
                                                   MSG_FULL_SERVER_RESPONSE,
                                                   SERIALIZATION_JSON, VERIFIED_AUDIO_FIELDS,
                                                   VERIFIED_REQUEST_FIELDS,
                                                   VolcengineStreamingASRProvider,
                                                   VolcengineStreamingProtocolError,
                                                   WebSocketTransport,
                                                   build_audio_frame,
                                                   build_full_client_request, build_header,
                                                   compress_payload, decompress_payload,
                                                   extract_result, parse_frame)
from aivoicebench.providers import ProviderFailure


def server_frame(document, *, sequence=1, flags=0b0001, compression=COMPRESSION_NONE,
                 message_type=MSG_FULL_SERVER_RESPONSE):
    body = json.dumps(document, ensure_ascii=False).encode('utf-8')
    body = compress_payload(body, compression)
    header = build_header(message_type, flags, SERIALIZATION_JSON, compression)
    return header + struct.pack('>i', sequence) + struct.pack('>I', len(body)) + body


def error_frame(code, message='provider detail'):
    body = message.encode('utf-8')
    header = build_header(0b1111, FLAG_NONE, 0, 0)
    return header + struct.pack('>II', code, len(body)) + body


class FakeServer:
    """Minimal documented server side; records what the client sent."""

    def __init__(self, responses=(), *, fail_on_connect=False, fail_on_send=False):
        self.frames = []
        self.closed = False
        # Each entry is either bytes (a raw frame) or a callable taking the
        # decoded client frame and returning frames to send.
        self._script = list(responses)
        self._outbox = asyncio.Queue()
        self.fail_on_connect = fail_on_connect
        self.fail_on_send = fail_on_send

    async def connect(self, url, headers):
        self.url = url
        self.headers = dict(headers)
        if self.fail_on_connect:
            raise OSError('handshake refused')
        return _FakeConnection(self)

    async def on_client(self, data):
        if self.fail_on_send:
            raise OSError('initial request send failed')
        frame = parse_frame(data)
        self.frames.append(frame)
        for entry in list(self._script):
            produced = entry(frame) if callable(entry) else []
            for item in produced or []:
                await self._outbox.put(item)
        if frame['message_type'] == MSG_AUDIO_ONLY_REQUEST and \
                frame['flags'] == FLAG_LAST_NO_SEQUENCE:
            for item in self.final_frames():
                await self._outbox.put(item)

    def final_frames(self):
        return []

    async def next_server_frame(self):
        return await self._outbox.get()


class _FakeConnection:
    def __init__(self, server):
        self.server = server

    async def send(self, data):
        await self.server.on_client(data)

    async def recv(self):
        return await self.server.next_server_frame()

    async def close(self):
        self.server.closed = True


class NormalClose(Exception):
    """Stand-in for ``websockets.exceptions.ConnectionClosedOK`` (1000/1001).

    The documented async SAUC endpoint closes the socket itself once the segment
    has been endpointed, and the client observes ConnectionClosedOK.
    """

    def __init__(self, code=1000, reason='ok'):
        super().__init__(f'normal closure (code {code})')
        self.code = code
        self.rcvd = SimpleNamespace(code=code, reason=reason)


class AbnormalClose(Exception):
    """Stand-in for ``websockets.exceptions.ConnectionClosedError``."""

    def __init__(self, code=1006, reason='abnormal'):
        super().__init__(f'abnormal closure (code {code})')
        self.code = code
        self.rcvd = SimpleNamespace(code=code, reason=reason)


class ClosingServer(FakeServer):
    """Delivers the scripted frames, then closes the socket.

    ``close_on_last_packet`` waits for the documented last packet (the client
    finished its input); ``close_after_frames`` closes once that many frames have
    been delivered, which is how the documented async endpoint behaves when it
    endpointed a segment and closed on its own.
    """

    def __init__(self, responses=(), *, close=None, close_after_frames=None,
                 close_on_last_packet=False, **kwargs):
        super().__init__(responses, **kwargs)
        self.close = close
        self.close_after_frames = close_after_frames
        self.close_on_last_packet = close_on_last_packet
        self._close_pending = False
        self._delivered = 0

    async def on_client(self, data):
        await super().on_client(data)
        frame = parse_frame(data)
        if (self.close_on_last_packet
                and frame['message_type'] == MSG_AUDIO_ONLY_REQUEST
                and frame['flags'] == FLAG_LAST_NO_SEQUENCE):
            self._close_pending = True

    async def next_server_frame(self):
        if self._outbox.empty():
            reached = (self.close_after_frames is not None
                       and self._delivered >= self.close_after_frames)
            if reached or self._close_pending:
                raise self.close or NormalClose()
        frame = await super().next_server_frame()
        self._delivered += 1
        return frame


class FrameCodecTests(unittest.TestCase):
    def test_header_bytes_match_the_documented_layout(self):
        header = build_header(MSG_FULL_CLIENT_REQUEST, FLAG_NONE, SERIALIZATION_JSON,
                              COMPRESSION_GZIP)
        self.assertEqual(header[:4], bytes([0x11, 0x10, 0x11, 0x00]))
        self.assertEqual((header[0] >> 4), 0b0001)  # protocol version 1
        self.assertEqual((header[0] & 0x0F), 0b0001)  # header size 4 bytes
        self.assertEqual((header[1] >> 4), MSG_FULL_CLIENT_REQUEST)
        self.assertEqual((header[2] >> 4), SERIALIZATION_JSON)
        self.assertEqual((header[2] & 0x0F), COMPRESSION_GZIP)

    def test_full_client_request_round_trip(self):
        payload = {'audio': {'format': 'pcm', 'rate': 16000, 'bits': 16, 'channel': 1},
                   'request': {'model_name': 'bigmodel', 'show_utterances': True}}
        frame = build_full_client_request(payload, compression=COMPRESSION_GZIP)
        parsed = parse_frame(frame)
        self.assertEqual(parsed['message_type'], MSG_FULL_CLIENT_REQUEST)
        self.assertEqual(parsed['compression'], COMPRESSION_GZIP)
        self.assertEqual(json.loads(decompress_payload(parsed['payload'],
                                                       parsed['compression'])), payload)
        self.assertEqual(parsed['sequence'], None)

    def test_audio_frame_flags_and_payload(self):
        pcm = bytes(range(0, 200, 2)) * 4
        normal = parse_frame(build_audio_frame(pcm, compression=COMPRESSION_NONE))
        self.assertEqual(normal['message_type'], MSG_AUDIO_ONLY_REQUEST)
        self.assertEqual(normal['flags'], FLAG_NONE)
        self.assertEqual(normal['payload'], pcm)
        last = parse_frame(build_audio_frame(pcm, compression=COMPRESSION_NONE, last=True))
        self.assertEqual(last['flags'], FLAG_LAST_NO_SEQUENCE)

    def test_server_response_carries_a_leading_sequence(self):
        frame = server_frame({'code': 0, 'payload_msg': {'result': {'text': 'hi'}}},
                             sequence=7)
        parsed = parse_frame(frame)
        self.assertEqual(parsed['sequence'], 7)
        self.assertEqual(json.loads(parsed['payload'])['code'], 0)

    def test_error_frame_layout(self):
        parsed = parse_frame(error_frame(CODE_SERVER_BUSY, 'busy'))
        self.assertEqual(parsed['message_type'], 0b1111)
        self.assertEqual(parsed['error_code'], CODE_SERVER_BUSY)
        self.assertEqual(parsed['error_message'], 'busy')

    def test_malformed_frames_are_rejected(self):
        for bad in (b'', b'\x11', b'\x11\x10\x11\x00\x00'):
            with self.assertRaises(VolcengineStreamingProtocolError):
                parse_frame(bad)
        with self.assertRaises(VolcengineStreamingProtocolError):
            parse_frame(bytes([0x12, 0x10, 0x11, 0x00]) + struct.pack('>I', 0))

    def test_result_nesting_is_accepted_both_ways(self):
        self.assertEqual(extract_result({'payload_msg': {'result': {'text': 'a'}}}),
                         ({'text': 'a'}, 'payload_msg'))
        self.assertEqual(extract_result({'result': {'text': 'b'}}), ({'text': 'b'}, 'top_level'))
        self.assertEqual(extract_result({'code': 0}), (None, None))


class EventModelTests(unittest.TestCase):
    def test_event_validation(self):
        event = StreamingASREvent(kind=EVENT_PARTIAL, source=SOURCE_PROVIDER, text='hi')
        self.assertEqual(event.to_dict()['evidence_scope'], 'control_evidence')
        with self.assertRaises(ValueError):
            StreamingASREvent(kind='not_a_kind', source=SOURCE_PROVIDER)
        with self.assertRaises(ValueError):
            StreamingASREvent(kind=EVENT_PARTIAL, source='somewhere')
        with self.assertRaises(ValueError):
            StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER, error='mystery')
        with self.assertRaises(ValueError):
            StreamingASREvent(kind=EVENT_PARTIAL, source=SOURCE_PROVIDER, confidence=2.0)

    def test_sources_stay_distinguishable(self):
        self.assertEqual(combine_sources(True, False), SOURCE_BROWSER_VAD)
        self.assertEqual(combine_sources(False, True), SOURCE_PROVIDER)
        self.assertEqual(combine_sources(True, True), SOURCE_COMBINED)

    def test_error_taxonomy_covers_the_required_categories(self):
        required = {'mic_permission_denied', 'mic_disconnected', 'audio_capture_failed',
                    'stream_open_failed', 'stream_disconnected', 'stream_timeout',
                    'provider_auth_failed', 'provider_rate_limited', 'provider_error',
                    'invalid_audio', 'asr_no_final', 'vad_timeout', 'user_stop',
                    'backend_restart'}
        self.assertTrue(required.issubset(set(ERROR_CATEGORIES)))


class ProviderValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def provider(self, **kwargs):
        defaults = dict(root=Path(self.tmp.name), api_key='k')
        defaults.update(kwargs)
        return VolcengineStreamingASRProvider(**defaults)

    def test_documented_configuration_is_accepted(self):
        for endpoint in (ENDPOINT, ENDPOINT_ASYNC):
            self.provider(endpoint=endpoint)
        self.provider(resource_id='volc.seedasr.sauc.duration')

    def test_unverified_configuration_is_refused(self):
        with self.assertRaises(ProviderFailure):
            self.provider(endpoint='wss://example.invalid/api/v3/sauc/bigmodel')
        with self.assertRaises(ProviderFailure):
            self.provider(resource_id='volc.bigasr.sauc.guess')
        with self.assertRaises(ProviderFailure):
            self.provider(model='bigmodel-2')
        with self.assertRaises(ProviderFailure):
            self.provider(end_window_size=100)
        with self.assertRaises(ProviderFailure):
            self.provider(end_window_size=9000)
        with self.assertRaises(ProviderFailure):
            self.provider(force_to_speech_time=99999)

    def test_missing_credential_fails_loudly(self):
        provider = self.provider(api_key='')

        async def run():
            await provider.start_session(session_id='s', turn_id='T0', run_index=1)

        with self.assertRaises(Exception) as caught:
            asyncio.run(run())
        self.assertIn('credential', str(caught.exception).lower())

    def test_unavailable_provider_never_fakes_a_session(self):
        provider = UnavailableStreamingASRProvider()

        async def run():
            await provider.start_session(session_id='s', turn_id='T0', run_index=1)

        with self.assertRaises(Exception):
            asyncio.run(run())


class SessionCase(unittest.TestCase):
    """Each scenario runs inside ONE event loop.

    The session owns a background reader task, so it must be opened, fed and
    closed in the same loop; driving it with several ``asyncio.run`` calls would
    tear the task down between calls.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_session(self, server, body, **provider_kwargs):
        """Open a session against ``server``, run ``body``, then close it.

        ``body`` receives the live session and returns the value to hand back.
        The session is always cancelled, so the audit and closed event exist.
        """
        async def scenario():
            provider = VolcengineStreamingASRProvider(self.root, 'test-key',
                                                      transport=server, **provider_kwargs)
            session = await provider.start_session(session_id='VT-test', turn_id='T0',
                                                   run_index=1, directory=self.root)
            try:
                return await body(session)
            finally:
                await session.cancel()

        return asyncio.run(scenario())


class SessionTests(SessionCase):

    def test_handshake_headers_and_start_request(self):
        server = FakeServer()

        async def body(session):
            return session

        session = self.run_session(server, body)
        self.assertEqual(server.url, ENDPOINT)
        self.assertEqual(server.headers['X-Api-Key'], 'test-key')
        self.assertEqual(server.headers['X-Api-Resource-Id'], 'volc.bigasr.sauc.duration')
        self.assertEqual(server.headers['X-Api-Sequence'], '-1')
        self.assertTrue(server.headers['X-Api-Request-Id'])
        self.assertTrue(server.headers['X-Api-Connect-Id'])
        self.assertNotIn('Authorization', server.headers)
        start = server.frames[0]
        self.assertEqual(start['message_type'], MSG_FULL_CLIENT_REQUEST)
        payload = json.loads(decompress_payload(start['payload'], start['compression']))
        self.assertEqual(set(payload), {'audio', 'request'})
        self.assertEqual(set(payload['audio']), set(VERIFIED_AUDIO_FIELDS))
        self.assertEqual(set(payload['request']), set(VERIFIED_REQUEST_FIELDS))
        self.assertEqual(payload['audio']['format'], 'pcm')
        self.assertEqual(payload['audio']['rate'], 16000)
        self.assertEqual(payload['audio']['bits'], 16)
        self.assertEqual(payload['audio']['channel'], 1)
        self.assertEqual(payload['request']['model_name'], 'bigmodel')
        self.assertTrue(payload['request']['show_utterances'])
        self.assertEqual(session.profile['provider'], 'volcengine')

    def test_initial_request_failure_closes_connection_and_finishes_audit(self):
        server = FakeServer(fail_on_send=True)

        async def scenario():
            provider = VolcengineStreamingASRProvider(
                self.root, 'test-key', transport=server)
            with self.assertRaises(StreamingASRUnavailable):
                await provider.start_session(
                    session_id='VT-test', turn_id='T0', run_index=1,
                    directory=self.root)

        asyncio.run(scenario())
        self.assertTrue(server.closed)
        calls = list((self.root / 'provider-calls').glob('*/result.json'))
        self.assertEqual(len(calls), 1, 'partial startup must finish its invocation audit')
        result = json.loads(calls[0].read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('test-key', json.dumps(result))

    def test_audio_is_aggregated_into_packets_and_sent_in_order(self):
        server = FakeServer()

        async def body(session):
            payload = bytes([(i % 251) for i in range(640)])
            for _ in range(10):
                await session.push_audio(payload)
            await session.finish_input()
            await asyncio.sleep(0.05)

        self.run_session(server, body)
        audio = [f for f in server.frames if f['message_type'] == MSG_AUDIO_ONLY_REQUEST]
        data = [f for f in audio if f['payload']]
        # 10 x 640 bytes buffered into exactly one documented-size packet, then
        # the explicit last packet (flag 0b0010) closes the audio stream.
        self.assertEqual(len(data), 1)
        self.assertEqual(len(data[0]['payload']), CHUNK_BYTES)
        self.assertEqual(data[0]['payload'][:640], bytes([(i % 251) for i in range(640)]))
        self.assertEqual(audio[-1]['flags'], FLAG_LAST_NO_SEQUENCE)
        self.assertEqual(audio[-1]['payload'], b'')

    def test_partial_then_definite_final_and_endpoint(self):
        partial = server_frame({'code': 0, 'payload_msg': {'result': {
            'text': '你好', 'utterances': [{'text': '你好', 'definite': False}]}}}, sequence=1)
        definite = server_frame({'code': 0, 'payload_sequence': 2, 'payload_msg': {'result': {
            'text': '你好，我在', 'utterances': [
                {'text': '你好，', 'definite': True, 'start_time': 0, 'end_time': 500},
                {'text': '我在', 'definite': False}]}}}, sequence=2)

        def hook(frame):
            return [partial, definite] if frame['message_type'] == MSG_AUDIO_ONLY_REQUEST else []

        server = FakeServer([hook])

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            events = await session.wait_events(1.0)
            await asyncio.sleep(0.05)
            return events + session.poll_events()

        events = self.run_session(server, body)
        kinds = [e.kind for e in events]
        self.assertIn(EVENT_PARTIAL, kinds)
        self.assertIn(EVENT_FINAL, kinds)
        self.assertIn(EVENT_SPEECH_ENDED, kinds)
        finals = [e for e in events if e.kind == EVENT_FINAL]
        self.assertEqual(finals[0].text, '你好，')
        self.assertEqual(finals[0].source, SOURCE_PROVIDER)
        self.assertEqual(finals[0].detail['response_shape'], 'payload_msg')
        partials = [e for e in events if e.kind == EVENT_PARTIAL]
        self.assertEqual(partials[-1].text, '你好，我在')

    def test_duplicate_and_late_results_do_not_repeat_the_final(self):
        response = server_frame({'code': 0, 'payload_sequence': 3, 'payload_msg': {'result': {
            'text': '北京呢', 'utterances': [{'text': '北京呢', 'definite': True}]}}}, sequence=3)
        server = FakeServer([lambda frame: [response, response]
                             if frame['message_type'] == MSG_AUDIO_ONLY_REQUEST else []])

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            await asyncio.sleep(0.05)
            return session.poll_events()

        events = self.run_session(server, body)
        self.assertEqual(len([e for e in events if e.kind == EVENT_FINAL]), 1,
                         'a repeated identical definite result must not emit a second final')

    def test_last_package_with_text_emits_final(self):
        response = server_frame({'code': 0, 'is_last_package': True, 'payload_sequence': 5,
                                 'payload_msg': {'audio_info': {'duration': 900},
                                                 'result': {'text': '北京呢'}}}, sequence=5,
                                flags=0b0011)
        server = FakeServer()
        server.final_frames = lambda: [response]
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 2000)
            await session.finish_input()
            await asyncio.sleep(0.1)
            captured['events'] = session.poll_events()
            captured['session'] = session

        self.run_session(server, body)
        events = captured['events']
        finals = [e for e in events if e.kind == EVENT_FINAL]
        self.assertTrue(finals)
        self.assertEqual(finals[0].text, '北京呢')
        self.assertEqual(finals[0].basis, 'provider_last_package')
        self.assertEqual(captured['session'].last_text, '北京呢')
        self.assertEqual(captured['session'].failure, None)

    def test_empty_final_is_not_an_answer(self):
        response = server_frame({'code': 0, 'is_last_package': True,
                                 'payload_msg': {'result': {'text': ''}}}, sequence=9,
                                flags=0b0011)
        server = FakeServer()
        server.final_frames = lambda: [response]
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 2000)
            await session.finish_input()
            await asyncio.sleep(0.1)
            captured['events'] = session.poll_events()
            captured['session'] = session

        self.run_session(server, body)
        events = captured['events']
        self.assertEqual([e.kind for e in events if e.kind == EVENT_FINAL], [])
        self.assertEqual(captured['session'].failure, 'asr_no_final')
        self.assertEqual(captured['session'].last_text, '')
        ended = [e for e in events if e.kind == EVENT_SPEECH_ENDED]
        self.assertEqual(ended[0].basis, 'provider_last_package')

    def test_documented_error_codes_map_to_categories(self):
        cases = {CODE_SERVER_BUSY: 'provider_rate_limited',
                 CODE_PACKET_TIMEOUT: 'stream_timeout',
                 CODE_INVALID_AUDIO_FORMAT: 'invalid_audio',
                 CODE_EMPTY_AUDIO: 'provider_error'}
        for code, category in cases.items():
            with self.subTest(code=code):
                server = FakeServer()
                server.final_frames = lambda code=code: [error_frame(code)]
                captured = {}

                async def body(session):
                    await session.push_audio(b'\x00\x00' * 2000)
                    await session.finish_input()
                    await asyncio.sleep(0.1)
                    captured['events'] = session.poll_events()
                    captured['session'] = session

                self.run_session(server, body)
                mapped = [e for e in captured['events'] if e.kind == EVENT_ERROR]
                self.assertTrue(mapped, f'no error event for code {code}')
                self.assertEqual(mapped[0].error, category)
                self.assertEqual(captured['session'].failure, category)

    def test_protocol_error_frame_is_reported(self):
        server = FakeServer()
        server.final_frames = lambda: [error_frame(CODE_EMPTY_AUDIO, 'no audio')]
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 2000)
            await session.finish_input()
            await asyncio.sleep(0.1)
            captured['events'] = session.poll_events()

        self.run_session(server, body)
        self.assertTrue([e for e in captured['events'] if e.kind == EVENT_ERROR])

    def test_handshake_failure_is_classified(self):
        server = FakeServer(fail_on_connect=True)

        async def scenario():
            provider = VolcengineStreamingASRProvider(self.root, 'test-key', transport=server)
            await provider.start_session(session_id='s', turn_id='T0', run_index=1,
                                         directory=self.root)

        with self.assertRaises(Exception) as caught:
            asyncio.run(scenario())
        self.assertIn('stream_open_failed', str(caught.exception))

    def test_invalid_audio_is_rejected_not_silently_sent(self):
        server = FakeServer()
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00')  # odd byte count cannot be PCM16
            await session.push_audio(b'')
            captured['events'] = session.poll_events()
            captured['session'] = session

        self.run_session(server, body)
        errors = [e for e in captured['events'] if e.kind == EVENT_ERROR]
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e.error == 'invalid_audio' for e in errors))
        self.assertEqual(captured['session'].failure, 'invalid_audio')
        audio = [f for f in server.frames if f['message_type'] == MSG_AUDIO_ONLY_REQUEST]
        self.assertEqual(audio, [])

    def test_cancel_records_closed_event_and_writes_secret_free_audit(self):
        server = FakeServer()
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 100)
            captured['session'] = session

        self.run_session(server, body)
        session = captured['session']
        events = session.history()
        self.assertEqual(events[0].kind, EVENT_SESSION_STARTED)
        self.assertEqual(events[-1].kind, EVENT_SESSION_CLOSED)
        self.assertTrue(events[-1].detail['cancelled'])
        calls = list((self.root / 'provider-calls').glob('*/result.json'))
        self.assertTrue(calls, 'the invocation audit must be finished')
        result = json.loads(calls[0].read_text(encoding='utf-8'))
        self.assertNotIn('test-key', json.dumps(result))
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['output_artifacts'])
        events_doc = json.loads((self.root / f'{session.stream_id}-events.json')
                                .read_text(encoding='utf-8'))
        self.assertTrue(events_doc['events'])
        self.assertEqual(events_doc['summary']['evidence_scope'], 'control_evidence')

    def test_cancel_is_idempotent_and_late_audio_is_refused(self):
        server = FakeServer()
        captured = {}

        async def body(session):
            await session.cancel()
            await session.cancel()
            captured['session'] = session
            with self.assertRaises(Exception):
                await session.push_audio(b'\x00\x00')

        self.run_session(server, body)
        closed = [e for e in captured['session'].history() if e.kind == EVENT_SESSION_CLOSED]
        self.assertEqual(len(closed), 1)

    def test_audio_artifact_is_a_valid_wav_and_bounded(self):
        server = FakeServer()
        captured = {}

        async def body(session):
            await session.push_audio(b'\x01\x02' * 800)
            captured['session'] = session

        self.run_session(server, body)
        session = captured['session']
        path = self.root / f'{session.stream_id}.wav'
        self.assertTrue(path.is_file())
        with wave.open(str(path), 'rb') as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.getnframes(), 800)

    def test_sessions_are_isolated(self):
        server_a, server_b = FakeServer(), FakeServer()

        async def scenario():
            provider = VolcengineStreamingASRProvider(self.root, 'test-key', transport=server_a)
            provider_b = VolcengineStreamingASRProvider(self.root, 'test-key', transport=server_b)
            a = await provider.start_session(session_id='A', turn_id='T0', run_index=1,
                                             directory=self.root / 'a')
            b = await provider_b.start_session(session_id='B', turn_id='T0', run_index=1,
                                               directory=self.root / 'b')
            await a.push_audio(b'\x00\x00' * 100)
            await b.push_audio(b'\x00\x00' * 200)
            await a.cancel()
            await b.cancel()
            return a, b

        a, b = asyncio.run(scenario())
        self.assertNotEqual(a.stream_id, b.stream_id)
        self.assertEqual(a.summary()['audio_bytes'], 200)
        self.assertEqual(b.summary()['audio_bytes'], 400)
        self.assertTrue(a.summary()['stream_id'].startswith('STR-'))


class NormalCloseTests(SessionCase):
    """A normal provider close after a definite final is a real termination.

    Real-cloud evidence: the documented async endpoint returned 11 partials and a
    definite final (`basis=provider_endpoint`), then closed the socket itself, and
    the client saw ConnectionClosedOK. The old code recorded that as
    ``stream_disconnected`` (phase=read) and left the capture active forever.
    """

    FINAL_TEXT = '请稍等一会哈。当然可以呀，我会一直陪着你的。'

    def definite_frame(self, text=None, sequence=2):
        text = text or self.FINAL_TEXT
        return server_frame({'code': 0, 'payload_sequence': sequence, 'payload_msg': {'result': {
            'text': text, 'utterances': [{'text': text, 'definite': True,
                                          'start_time': 0, 'end_time': 2600}]}}}, sequence=sequence)

    def partial_frame(self, text='请稍等一会哈', sequence=1):
        return server_frame({'code': 0, 'payload_sequence': sequence, 'payload_msg': {'result': {
            'text': text, 'utterances': [{'text': text, 'definite': False}]}}}, sequence=sequence)

    @staticmethod
    def on_audio(frames):
        return lambda frame: list(frames) if frame['message_type'] == MSG_AUDIO_ONLY_REQUEST else []

    def test_normal_close_after_client_finish_with_definite_final_succeeds(self):
        server = ClosingServer([self.on_audio([self.partial_frame(), self.definite_frame()])],
                               close=NormalClose(1000), close_on_last_packet=True)
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            await session.finish_input()
            for _ in range(60):
                if session.terminated:
                    break
                await asyncio.sleep(0.05)
            captured.update(events=session.poll_events(), session=session,
                            state=session.state, failure=session.failure,
                            terminated=session.terminated,
                            saw_last_package=session.saw_last_package,
                            basis=session.termination_basis, close_code=session.close_code,
                            summary=session.summary())

        self.run_session(server, body)
        session = captured['session']
        # The normal close is accepted, with the definite final as its evidence.
        self.assertTrue(captured['terminated'])
        self.assertIsNone(captured['failure'])
        self.assertEqual(captured['state'], 'finished')
        self.assertEqual(captured['basis'], 'provider_normal_close')
        self.assertEqual(captured['close_code'], 1000)
        # The documented field keeps its exact meaning: the provider never sent it.
        self.assertFalse(captured['saw_last_package'])
        self.assertFalse(captured['summary']['saw_last_package'])
        self.assertTrue(captured['summary']['terminated'])
        self.assertEqual(captured['summary']['termination_basis'], 'provider_normal_close')
        self.assertEqual(captured['summary']['close_code'], 1000)
        self.assertEqual(session.last_text, self.FINAL_TEXT)
        # No transport failure may be recorded for a normal termination.
        self.assertEqual([e for e in captured['events'] if e.kind == EVENT_ERROR], [])
        finals = [e for e in captured['events'] if e.kind == EVENT_FINAL]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0].text, self.FINAL_TEXT)
        self.assertEqual(finals[0].basis, BASIS_PROVIDER_ENDPOINT)
        self.assertEqual(session._terminal_status(False), ('complete', None))

    def test_normal_close_before_client_finish_with_definite_final_succeeds(self):
        """The async endpoint may close before our own last packet arrives."""
        server = ClosingServer([self.on_audio([self.partial_frame(), self.definite_frame()])],
                               close=NormalClose(1000), close_after_frames=2)
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            for _ in range(60):
                if session.terminated:
                    break
                await asyncio.sleep(0.05)
            captured.update(terminated=session.terminated, state=session.state,
                            failure=session.failure)
            # The turn ends later, when the browser stops the capture: sending the
            # last packet on the already-closed socket must not invent a failure.
            await session.finish_input()
            captured.update(after_finish_state=session.state, after_finish_failure=session.failure,
                            events=session.poll_events())

        self.run_session(server, body)
        self.assertTrue(captured['terminated'])
        self.assertEqual(captured['state'], 'finished')
        self.assertIsNone(captured['failure'])
        self.assertEqual(captured['after_finish_state'], 'finished')
        self.assertIsNone(captured['after_finish_failure'])
        self.assertEqual([e for e in captured['events'] if e.kind == EVENT_ERROR], [])

    def test_normal_close_without_a_definite_final_is_not_an_answer(self):
        server = ClosingServer([self.on_audio([self.partial_frame()])],
                               close=NormalClose(1000), close_on_last_packet=True)
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            await session.finish_input()
            for _ in range(60):
                if session.terminated:
                    break
                await asyncio.sleep(0.05)
            captured.update(events=session.poll_events(), session=session,
                            failure=session.failure, basis=session.termination_basis)

        self.run_session(server, body)
        self.assertEqual(captured['failure'], 'asr_no_final')
        self.assertTrue(captured['session'].terminated)
        self.assertFalse(captured['session'].saw_last_package)
        self.assertEqual([e for e in captured['events'] if e.kind == EVENT_FINAL], [])
        errors = [e for e in captured['events'] if e.kind == EVENT_ERROR]
        self.assertEqual([e.error for e in errors], ['asr_no_final'])
        # A clean stream with no usable final is not a completed recognition.
        self.assertEqual(captured['session']._terminal_status(False),
                         ('insufficient_evidence', None))

    def test_normal_close_before_the_client_finishes_the_input_is_a_disconnect(self):
        server = ClosingServer([self.on_audio([self.partial_frame()])],
                               close=NormalClose(1000), close_after_frames=1)
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            for _ in range(60):
                if session.failure:
                    break
                await asyncio.sleep(0.05)
            captured.update(events=session.poll_events(), failure=session.failure,
                            terminated=session.terminated)

        self.run_session(server, body)
        self.assertEqual(captured['failure'], 'stream_disconnected')
        self.assertFalse(captured['terminated'])
        errors = [e for e in captured['events'] if e.kind == EVENT_ERROR]
        self.assertEqual([e.error for e in errors], ['stream_disconnected'])
        self.assertEqual(errors[0].detail['phase'], 'read')

    def test_abnormal_close_is_still_a_disconnect(self):
        server = ClosingServer([self.on_audio([self.definite_frame()])],
                               close=AbnormalClose(1006), close_on_last_packet=True)
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            await session.finish_input()
            for _ in range(60):
                if session.failure:
                    break
                await asyncio.sleep(0.05)
            captured.update(events=session.poll_events(), failure=session.failure,
                            terminated=session.terminated, close_code=session.close_code)

        self.run_session(server, body)
        self.assertEqual(captured['failure'], 'stream_disconnected')
        self.assertFalse(captured['terminated'])
        self.assertEqual(captured['close_code'], 1006)
        errors = [e for e in captured['events'] if e.kind == EVENT_ERROR]
        self.assertEqual([e.error for e in errors], ['stream_disconnected'])
        self.assertEqual(errors[0].detail['error_type'], 'AbnormalClose')

    def test_read_failure_without_a_close_code_is_still_a_disconnect(self):
        server = ClosingServer([self.on_audio([self.definite_frame()])],
                               close=OSError('connection reset by peer'),
                               close_on_last_packet=True)
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 4000)
            await session.finish_input()
            for _ in range(60):
                if session.failure:
                    break
                await asyncio.sleep(0.05)
            captured.update(failure=session.failure, terminated=session.terminated,
                            close_code=session.close_code)

        self.run_session(server, body)
        self.assertEqual(captured['failure'], 'stream_disconnected')
        self.assertFalse(captured['terminated'])
        self.assertIsNone(captured['close_code'])

    def test_explicit_last_package_records_its_own_termination_basis(self):
        response = server_frame({'code': 0, 'is_last_package': True, 'payload_sequence': 5,
                                 'payload_msg': {'result': {'text': '北京呢'}}}, sequence=5,
                                flags=0b0011)
        server = FakeServer()
        server.final_frames = lambda: [response]
        captured = {}

        async def body(session):
            await session.push_audio(b'\x00\x00' * 2000)
            await session.finish_input()
            await asyncio.sleep(0.1)
            # Read the live state before ``run_session`` shuts the session down.
            captured.update(state=session.state, summary=session.summary(),
                            saw_last_package=session.saw_last_package,
                            terminated=session.terminated,
                            basis=session.termination_basis)

        self.run_session(server, body)
        self.assertTrue(captured['saw_last_package'])
        self.assertTrue(captured['terminated'])
        self.assertEqual(captured['basis'], 'provider_last_package')
        self.assertEqual(captured['summary']['termination_basis'], 'provider_last_package')
        self.assertEqual(captured['state'], 'finished')


class RealWebSocketTransportTests(unittest.TestCase):
    """Exercise the real websockets client and the real frame bytes on a socket.

    The injected-transport tests above prove the protocol logic; this one proves
    the default transport actually opens a WebSocket, carries the documented
    handshake headers and exchanges documented frames over a real connection.
    Only the URL is redirected to a local server — the documented endpoint stays
    what the provider is configured with.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        try:
            import websockets  # noqa: F401
            from websockets.asyncio.server import serve  # noqa: F401
        except ImportError as error:  # pragma: no cover - dependency guard
            self.skipTest(f'websockets server unavailable: {error}')

    def test_documented_frames_over_a_real_socket(self):
        import websockets
        from websockets.asyncio.server import serve

        seen = {'headers': None, 'frames': []}

        async def handler(connection):
            seen['headers'] = dict(connection.request.headers)
            async for message in connection:
                frame = parse_frame(message)
                seen['frames'].append(frame)
                if frame['message_type'] == MSG_FULL_CLIENT_REQUEST:
                    await connection.send(server_frame(
                        {'code': 0, 'payload_sequence': 1, 'payload_msg': {'result': {
                            'text': '你好', 'utterances': [{'text': '你好', 'definite': False}]}}},
                        sequence=1))
                elif (frame['message_type'] == MSG_AUDIO_ONLY_REQUEST
                      and frame['flags'] == FLAG_LAST_NO_SEQUENCE):
                    await connection.send(server_frame(
                        {'code': 0, 'is_last_package': True, 'payload_sequence': 2,
                         'payload_msg': {'result': {
                             'text': '你好，我在',
                             'utterances': [{'text': '你好，我在', 'definite': True}]}}},
                        sequence=2, flags=0b0011))

        class LocalTransport(WebSocketTransport):
            def __init__(self, url):
                super().__init__(open_timeout=10, close_timeout=5)
                self.url = url

            async def connect(self, url, headers):  # noqa: ARG002 - documented URL kept
                return await super().connect(self.url, headers)

        async def scenario():
            async with serve(handler, '127.0.0.1', 0) as server:
                port = server.sockets[0].getsockname()[1]
                provider = VolcengineStreamingASRProvider(
                    self.root, 'test-key',
                    transport=LocalTransport(f'ws://127.0.0.1:{port}/api/v3/sauc/bigmodel'))
                session = await provider.start_session(session_id='VT-real', turn_id='T0',
                                                       run_index=1, directory=self.root)
                await session.push_audio(b'\x00\x00' * 4000)
                await session.finish_input()
                collected = []
                for _ in range(20):
                    collected.extend(await session.wait_events(0.25))
                    if session.saw_last_package:
                        break
                await session.close()
                return session, collected

        session, events = asyncio.run(scenario())

        def header(name):
            for key, value in (seen['headers'] or {}).items():
                if key.lower() == name.lower():
                    return value
            return None

        # The documented handshake headers really crossed the wire.
        self.assertEqual(header('X-Api-Key'), 'test-key')
        self.assertEqual(header('X-Api-Resource-Id'), 'volc.bigasr.sauc.duration')
        self.assertEqual(header('X-Api-Sequence'), '-1')
        self.assertTrue(header('X-Api-Request-Id'))
        self.assertIsNone(header('Authorization'), 'credentials must not use the legacy scheme')
        types = [f['message_type'] for f in seen['frames']]
        self.assertEqual(types[0], MSG_FULL_CLIENT_REQUEST)
        self.assertIn(MSG_AUDIO_ONLY_REQUEST, types)
        self.assertEqual(seen['frames'][-1]['flags'], FLAG_LAST_NO_SEQUENCE)
        kinds = [e.kind for e in events]
        self.assertIn(EVENT_PARTIAL, kinds)
        self.assertIn(EVENT_FINAL, kinds)
        self.assertEqual(session.last_text, '你好，我在')


if __name__ == '__main__':
    unittest.main()
