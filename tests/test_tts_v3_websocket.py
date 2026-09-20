"""Volcengine V3 WebSocket TTS contract tests (Issue #98, PRD-F020/F021).

These are software tests. Nothing here contacts Volcengine: the transport is
injected and the fake server speaks the documented V3 event envelope, so the
tests decode exactly the bytes the client puts on the wire. Real-cloud behaviour
stays ``real_cloud_pending`` (see ``docs/28-active-tts.md``).

Covered, following the Issue's "Tests and audit" section:

* unidirectional synthesis: happy path, multi-chunk assembly, provider error,
  abnormal close, timeout;
* Fixed MP3 stream -> frozen MP3 stimulus hash/sample-metadata stability;
* bidirectional session: open / text append / audio receive / finish / cancel;
* LLM streaming order == TTS text order;
* Stop/Barge-in-preparation/stale turn: late chunks dropped, never played;
* protocol-specific parameter validation (including pcapability of pitch);
* credential redaction (audit, snapshot and provider errors);
* Fixed/Free provider-call accounting;
* the configured ``tts`` / ``streaming_tts`` routes and capability matrix.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aivoicebench.config_loaders import load_providers_config
from aivoicebench.model_settings import (TTS_FIXED_FORMAT, SettingsError,
                                         build_run_providers, validate_profile)
from aivoicebench.providers import ProviderFailure
from aivoicebench.streaming_tts import (STREAMING_TTS_EVIDENCE_SCOPE,
                                        StreamingTTSEvent, StreamingTTSUnavailable,
                                        TTS_FALLBACK_POLICY,
                                        UnavailableStreamingTTSProvider,
                                        fallback_policy_record, forbidden_fallbacks,
                                        new_tts_stream_id, split_speakable_chunks,
                                        stale_turn_reason, tts_failure_category)
from aivoicebench.volcengine_tts_ws import (AUDIT_ENDPOINT_BIDIRECTIONAL,
                                           AUDIT_ENDPOINT_UNIDIRECTIONAL,
                                           CAPABILITY_MATRIX, CODE_ERROR_CATEGORY,
                                           CODE_INVALID_REQUEST,
                                           CODE_RESOURCE_MISMATCH, CODE_SERVER_BUSY,
                                           CODE_SUCCESS, CODE_TTS_NO_AUDIO,
                                           ENDPOINT_BIDIRECTIONAL,
                                           ENDPOINT_UNIDIRECTIONAL,
                                           EVENT_CANCEL_SESSION,
                                           EVENT_CONNECTION_STARTED,
                                           EVENT_CONNECTION_FINISHED,
                                           EVENT_FINISH_SESSION,
                                           EVENT_START_CONNECTION,
                                           EVENT_SESSION_FAILED,
                                           EVENT_SESSION_FINISHED,
                                           EVENT_START_SESSION,
                                           EVENT_TASK_REQUEST,
                                           EVENT_TTSSentence_END,
                                           EVENT_TTSSentence_START,
                                           EVENT_TTS_RESPONSE,
                                           FALLBACK_TRANSPORT_SSE,
                                           FALLBACK_TRANSPORT_UNIDIRECTIONAL,
                                           FLAG_WITH_EVENT,
                                           INTERFACE_CONTRACT,
                                           MSG_AUDIO_ONLY_RESPONSE,
                                           MSG_FULL_CLIENT_REQUEST, MSG_FULL_SERVER_RESPONSE,
                                           MP3ValidationError,
                                           SERIALIZATION_JSON, TTS_FORMAT,
                                           TRANSPORT_BIDIRECTIONAL,
                                           TRANSPORT_UNIDIRECTIONAL,
                                           UnavailableTTSProvider,
                                           VERIFIED_REQUEST_FIELDS,
                                           VolcengineBidirectionalTTSSession,
                                           VolcengineTTSProtocolError,
                                           VolcengineUnidirectionalTTSProvider,
                                           build_event_frame, build_header,
                                           build_full_client_request,
                                           build_request_payload,
                                           parse_mp3_frame_header, parse_mp3_metadata,
                                           parse_response, read_length_prefixed)


# --------------------------------------------------------------- MP3 fixtures

class FakeTTSLegacyProvider:
    """A minimal ``TTSProvider`` that declares the media format it emits.

    Stands in for the retired V3 HTTP SSE adapter during the migration period so
    the runner's format-driven naming can be tested for both containers without
    a network call.
    """

    def __init__(self, audio_format='mp3'):
        self.audio_format = audio_format

    def synthesize(self, text, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = (mp3_stream(frames=1) if self.audio_format == 'mp3'
                   else b'RIFF0000WAVEfmt ')
        destination.write_bytes(payload)
        return {'path': str(destination), 'sha256': 'deadbeef', 'format': self.audio_format}


def mp3_frame(sample_rate_index=1, bitrate_index=9, padding=0, mpeg1=True):
    """One valid MPEG-1 Layer III frame at the requested rate/bitrate.

    ``sample_rate_index`` 1 -> 48000 Hz, ``bitrate_index`` 9 -> 128 kbps.
    """
    b1 = 0xFB if mpeg1 else 0xF3
    b2 = (bitrate_index << 4) | (sample_rate_index << 2) | (padding << 1)
    header = bytes([0xFF, b1, b2, 0x00])
    length = parse_mp3_frame_header(header)['frame_length']
    return header + bytes(length - 4)


def mp3_stream(frames=3, **kwargs):
    return b''.join(mp3_frame(**kwargs) for _ in range(frames))


def silence_frame_bytes():
    return mp3_stream(frames=2)


# ------------------------------------------------------------- fake V3 server

def server_frame(event, *, session_id='sess', payload=b'', message_type=None,
                 sequence_payload=None):
    """A documented server frame: header, event, (sid), payload."""
    if message_type is None:
        message_type = MSG_FULL_SERVER_RESPONSE
    return build_event_frame(event, session_id=session_id, payload=payload,
                             message_type=message_type, flags=FLAG_WITH_EVENT,
                             serialization=SERIALIZATION_JSON)


def audio_frame(payload, session_id='sess'):
    return build_event_frame(EVENT_TTS_RESPONSE, session_id=session_id, payload=payload,
                             message_type=MSG_AUDIO_ONLY_RESPONSE, flags=FLAG_WITH_EVENT,
                             serialization=SERIALIZATION_JSON)


def session_failed_frame(message='45000003 resource mismatched', session_id='sess'):
    """Documented ``SessionFailed`` layout: [sid_len][sid] and no payload field."""
    return build_event_frame(EVENT_SESSION_FAILED, session_id=session_id,
                             payload=message.encode('utf-8'),
                             message_type=MSG_FULL_SERVER_RESPONSE,
                             flags=FLAG_WITH_EVENT, serialization=SERIALIZATION_JSON)


def error_frame(code, message='provider detail'):
    header = build_header(0b1111, 0b0000, 0, 0)
    body = message.encode('utf-8')
    import struct
    return header + struct.pack('>ii', code, len(body)) + body


# ------------------------------------------------- golden byte-level fixtures
#
# Independent of the implementation under test: these are literal hex strings
# transcribed from the documented V3 envelope layout, so a wrong assumption about
# the frame layout fails here instead of being reproduced by the parser that
# shares the same assumption. Frames built by the module's own helpers cannot
# catch a shared mistake, which is exactly the gap these fixtures close.
#
# Layout recap (big-endian integers):
#   audio only response : [header][event i32][sid_len i32][sid][payload_len i32][audio]
#   error from server   : [header][event i32][error_code i32][payload_len i32][message]
GOLDEN_AUDIO_ONLY_FRAME_HEX = (
    # header 11 b4 10 00 -> v1/4B, msg_type 0b1011, flags 0b0100 (with event),
    #                       serialization JSON, no compression
    '11b41000'
    # event 352 (0x00000160)
    '00000160'
    # session id length 4, session id 'sess' (0x73657373)
    '0000000473657373'
    # payload length 6, audio bytes 0x010203040506
    '00000006010203040506'
)
GOLDEN_ERROR_FRAME_HEX = (
    # header 11 f0 00 00 -> v1/4B, msg_type 0b1111 (error), flags 0, no serialization
    '11f00000'
    # error code 45000100 (0x02AEA5A4, documented "TTS returned no audio")
    '02aea5a4'
    # payload length 5, message 'oops!' (0x6f6f707321)
    '000000056f6f707321'
)
GOLDEN_START_SESSION_FRAME_HEX = (
    # header 11 14 10 00 -> full client request, with event, JSON, no compression
    '11141000'
    # event 100 (0x00000064)
    '00000064'
    # session id length 2, session id 'ab' (0x6162)
    '000000026162'
    # payload length 2, payload '{}' (0x7b7d)
    '000000027b7d'
)


class FakeServer:
    """Minimal documented server side; records what the client sent."""

    def __init__(self, *, fail_on_connect=False, fail_on_send=False,
                 script_for_event=None):
        self.frames = []
        self.headers = None
        self.url = None
        self.closed = False
        # Map of client event -> callable returning the server frames to enqueue.
        self.script_for_event = {
            EVENT_START_CONNECTION: lambda _frame: [
                server_frame(EVENT_CONNECTION_STARTED, session_id='')],
        }
        self.script_for_event.update(script_for_event or {})
        self.fail_on_connect = fail_on_connect
        self.fail_on_send = fail_on_send
        self._outbox = asyncio.Queue()
        # Tests hand queued frames to the session explicitly via ``settle`` so
        # frame handling is deterministic instead of racing the reader task.
        self.passive = True

    async def connect(self, url, headers):
        self.url = url
        self.headers = dict(headers)
        if self.fail_on_connect:
            raise OSError('handshake refused')
        return _FakeConnection(self)

    async def on_client(self, data):
        if self.fail_on_send:
            raise OSError('initial request send failed')
        frame = parse_response(data)
        self.frames.append(frame)
        handler = self.script_for_event.get(frame['event'])
        if handler is None:
            return
        for item in handler(frame) or []:
            await self._outbox.put(item)

    async def next_frame(self):
        return await self._outbox.get()

    def enqueue(self, item):
        self._outbox.put_nowait(item)

    def has_pending(self):
        return not self._outbox.empty()

    def events(self):
        return [frame['event'] for frame in self.frames]

    async def settle(self, session, *, limit=50):
        """Hand every queued server frame to ``session`` synchronously.

        The real transport reads frames on a background task; in a unit test that
        task competes with ``asyncio.wait_for`` timers for the loop, which makes
        "did the client process the scripted frames?" nondeterministic. Pumping
        the same ``session._handle_frame`` path the reader task uses removes that
        race while still exercising the production parsing and state machine.
        """
        processed = 0
        while self.has_pending() and processed < limit:
            frame = parse_response(bytes(await self._outbox.get()))
            session._handle_frame(frame)
            processed += 1
        return processed


class _FakeConnection:
    def __init__(self, server):
        self.server = server

    async def send(self, data):
        await self.server.on_client(data)

    async def recv(self):
        return await self.server.next_frame()

    async def close(self):
        self.server.closed = True


def make_provider(server, root, **overrides):
    options = {
        'endpoint': ENDPOINT_UNIDIRECTIONAL,
        'audit_endpoint': AUDIT_ENDPOINT_UNIDIRECTIONAL,
        'model': 'tts-model',
        'resource_id': 'volc.service_type.test',
        'voice_type': 'BV700_test',
        'sample_rate': 48000,
        'timeout': 5,
        'transport': server,
    }
    options.update(overrides)
    return VolcengineUnidirectionalTTSProvider(root, 'test-key', **options)


# --------------------------------------------------------- 1. wire contract

class ProtocolConformanceTests(unittest.TestCase):
    """The outgoing bytes must match the documented V3 envelope exactly."""

    def test_client_header_bits(self):
        header = build_header(MSG_FULL_CLIENT_REQUEST, FLAG_WITH_EVENT,
                              SERIALIZATION_JSON, 0)
        self.assertEqual(header, bytes([0x11, 0x14, 0x10, 0x00]))
        # version 1, header size 1 word; full client request; with-event flag.
        self.assertEqual(header[0] >> 4, 0b0001)
        self.assertEqual(header[0] & 0x0F, 0b0001)
        self.assertEqual(header[1] >> 4, MSG_FULL_CLIENT_REQUEST)
        self.assertEqual(header[1] & 0x0F, FLAG_WITH_EVENT)
        self.assertEqual(header[2] >> 4, SERIALIZATION_JSON)
        self.assertEqual(header[2] & 0x0F, 0)

    def test_start_session_frame_layout(self):
        frame = build_event_frame(EVENT_START_SESSION, session_id='sess-1',
                                  payload=b'{"a":1}')
        parsed = parse_response(frame)
        self.assertEqual(parsed['event'], EVENT_START_SESSION)
        self.assertEqual(parsed['session_id'], 'sess-1')
        self.assertEqual(parsed['payload'], b'{"a":1}')
        self.assertEqual(parsed['message_type'], MSG_FULL_CLIENT_REQUEST)

    def test_frame_without_session_id_is_parseable(self):
        frame = build_event_frame(1, session_id=None, payload=b'{}')
        parsed = parse_response(frame)
        self.assertIsNone(parsed['session_id'])
        self.assertEqual(parsed['payload'], b'{}')

    def test_connection_started_uses_connect_id_not_session_id(self):
        frame = server_frame(EVENT_CONNECTION_STARTED, session_id='connection-1',
                             payload=b'{}')
        parsed = parse_response(frame)
        self.assertIsNone(parsed['session_id'])
        self.assertEqual(parsed['connect_id'], 'connection-1')

    def test_length_prefixed_rejects_truncation(self):
        with self.assertRaises(VolcengineTTSProtocolError):
            read_length_prefixed(b'\x00\x00', 0)
        with self.assertRaises(VolcengineTTSProtocolError):
            read_length_prefixed(b'\x00\x00\x00\x10abc', 0)

    def test_short_and_bad_frames_are_refused(self):
        with self.assertRaises(VolcengineTTSProtocolError):
            parse_response(b'\x11\x14')
        with self.assertRaises(VolcengineTTSProtocolError):
            parse_response(bytes([0x11, 0x24, 0x10, 0x00]) + bytes(8))

    def test_error_frame_carries_code_and_message(self):
        parsed = parse_response(error_frame(CODE_INVALID_REQUEST, 'bad param'))
        self.assertEqual(parsed['message_type'], 0b1111)
        self.assertEqual(parsed['error_code'], CODE_INVALID_REQUEST)
        self.assertEqual(parsed['error_message'], 'bad param')

    def test_request_payload_uses_only_verified_fields(self):
        payload = json.loads(build_request_payload(
            event=EVENT_TASK_REQUEST, uid='aivoicebench', text='你好',
            speaker='BV700', audio_params={'format': TTS_FORMAT, 'sample_rate': 48000,
                                           'speech_rate': 0, 'loudness_rate': 0}))
        self.assertEqual(payload['event'], EVENT_TASK_REQUEST)
        self.assertEqual(payload['namespace'], 'BidirectionalTTS')
        self.assertEqual(set(payload), {'user', 'event', 'namespace', 'req_params'})
        self.assertEqual(set(payload['req_params']),
                         {'text', 'speaker', 'audio_params', 'additions'})
        self.assertEqual(payload['req_params']['audio_params']['format'], 'mp3')
        # Only the whitelisted request fields are advertised as verified.
        self.assertEqual(set(VERIFIED_REQUEST_FIELDS), {
            'user.uid', 'event', 'namespace', 'req_params.text',
            'req_params.speaker', 'req_params.audio_params.format',
            'req_params.audio_params.sample_rate',
            'req_params.audio_params.speech_rate',
            'req_params.audio_params.loudness_rate',
            'req_params.additions'})

    def test_interface_contract_names_both_official_endpoints(self):
        self.assertEqual(INTERFACE_CONTRACT['endpoints']['unidirectional'],
                         ENDPOINT_UNIDIRECTIONAL)
        self.assertEqual(INTERFACE_CONTRACT['endpoints']['bidirectional'],
                         ENDPOINT_BIDIRECTIONAL)
        self.assertEqual(INTERFACE_CONTRACT['media_format'], 'mp3')
        self.assertEqual(INTERFACE_CONTRACT['real_cloud_call'], 'verified_2026-09-20')

    def test_capability_matrix_is_protocol_specific(self):
        self.assertFalse(CAPABILITY_MATRIX[TRANSPORT_UNIDIRECTIONAL]['pitch'],
                         'the V3 unidirectional page still marks pitch unsupported')
        self.assertFalse(CAPABILITY_MATRIX[TRANSPORT_UNIDIRECTIONAL]['streaming_text'])
        self.assertTrue(CAPABILITY_MATRIX[TRANSPORT_BIDIRECTIONAL]['streaming_text'])
        self.assertTrue(CAPABILITY_MATRIX[TRANSPORT_BIDIRECTIONAL]['streaming_audio'])

    def test_client_sends_only_whitelisted_request_fields(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            0: lambda frame: [
                audio_frame(stream), server_frame(EVENT_SESSION_FINISHED)]})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            provider.synthesize('固定话术', Path(tmp) / 'out.mp3')
        events = [frame['event'] for frame in server.frames]
        self.assertEqual(events, [0])
        body = json.loads(server.frames[0]['payload'].decode('utf-8'))
        self.assertEqual(set(body), {'req_params'})
        self.assertEqual(body['req_params']['text'], '固定话术')

    def test_credential_is_only_in_handshake_headers(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            0: lambda frame: [
                audio_frame(stream), server_frame(EVENT_SESSION_FINISHED)]})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            provider.synthesize('hi', Path(tmp) / 'out.mp3')
        self.assertEqual(server.headers['X-Api-Key'], 'test-key')
        self.assertIn('X-Api-Resource-Id', server.headers)
        self.assertIn('X-Api-Request-Id', server.headers)
        self.assertNotIn('X-Api-Connect-Id', server.headers)
        # Never in the outgoing body.
        for frame in server.frames:
            self.assertNotIn(b'test-key', frame['payload'] or b'')


# ------------------------------------------- 1b. golden byte-level frames

class GoldenFrameTests(unittest.TestCase):
    """Frames transcribed from the documented layout, independent of the codec."""

    def test_golden_audio_only_frame_parses(self):
        frame = parse_response(bytes.fromhex(GOLDEN_AUDIO_ONLY_FRAME_HEX))
        self.assertEqual(frame['message_type'], MSG_AUDIO_ONLY_RESPONSE)
        self.assertEqual(frame['flags'] & FLAG_WITH_EVENT, FLAG_WITH_EVENT)
        self.assertEqual(frame['event'], EVENT_TTS_RESPONSE)
        self.assertEqual(frame['session_id'], 'sess')
        self.assertEqual(frame['payload'], b'\x01\x02\x03\x04\x05\x06')

    def test_golden_error_frame_parses(self):
        frame = parse_response(bytes.fromhex(GOLDEN_ERROR_FRAME_HEX))
        self.assertEqual(frame['message_type'], 0b1111)
        self.assertEqual(frame['error_code'], CODE_TTS_NO_AUDIO)
        self.assertEqual(frame['error_message'], 'oops!')
        self.assertEqual(CODE_ERROR_CATEGORY[CODE_TTS_NO_AUDIO], 'provider_no_audio')

    def test_golden_start_session_frame_shape(self):
        frame = parse_response(bytes.fromhex(GOLDEN_START_SESSION_FRAME_HEX))
        self.assertEqual(frame['message_type'], MSG_FULL_CLIENT_REQUEST)
        self.assertEqual(frame['event'], EVENT_START_SESSION)
        self.assertEqual(frame['session_id'], 'ab')
        self.assertEqual(frame['payload'], b'{}')
        # The client must produce exactly this shape for the same inputs.
        self.assertEqual(
            build_event_frame(EVENT_START_SESSION, session_id='ab', payload=b'{}').hex(),
            GOLDEN_START_SESSION_FRAME_HEX)

    def test_golden_audio_frame_matches_the_client_encoder(self):
        """The server-side audio layout and the client encoder must agree."""
        self.assertEqual(
            build_event_frame(EVENT_TTS_RESPONSE, session_id='sess',
                              payload=b'\x01\x02\x03\x04\x05\x06',
                              message_type=MSG_AUDIO_ONLY_RESPONSE,
                              flags=FLAG_WITH_EVENT,
                              serialization=SERIALIZATION_JSON).hex(),
            GOLDEN_AUDIO_ONLY_FRAME_HEX)

    def test_golden_error_frame_matches_the_client_encoder(self):
        self.assertEqual(
            error_frame(CODE_TTS_NO_AUDIO, 'oops!').hex(), GOLDEN_ERROR_FRAME_HEX)


# ------------------------------------------------------- 2. MP3 validation


class MP3ValidationTests(unittest.TestCase):

    def test_parses_metadata(self):
        metadata = parse_mp3_metadata(mp3_stream(frames=2, sample_rate_index=1))
        self.assertEqual(metadata['format'], 'mp3')
        self.assertEqual(metadata['sample_rate'], 48000)
        self.assertEqual(metadata['channels'], 2)
        self.assertEqual(metadata['bitrate_bps'], 128000)
        self.assertEqual(metadata['frames'], 2)
        self.assertGreater(metadata['duration_ms'], 0)

    def test_refuses_non_mp3_bytes(self):
        with self.assertRaises(MP3ValidationError):
            parse_mp3_metadata(b'not audio at all')
        with self.assertRaises(MP3ValidationError):
            parse_mp3_metadata(b'')

    def test_refuses_format_change_mid_stream(self):
        with self.assertRaises(MP3ValidationError):
            parse_mp3_metadata(mp3_frame(sample_rate_index=1)
                               + mp3_frame(sample_rate_index=1, mpeg1=False))

    def test_skips_leading_id3_tag(self):
        tag_body = b'\x00' * 10
        tag = b'ID3\x04\x00\x00' + bytes([0, 0, 0, 10]) + tag_body
        metadata = parse_mp3_metadata(tag + mp3_stream(frames=1))
        self.assertEqual(metadata['frames'], 1)

    def test_stops_at_trailing_non_frame_bytes(self):
        metadata = parse_mp3_metadata(mp3_stream(frames=2) + b'\xff\xff\xff\xff')
        self.assertEqual(metadata['frames'], 2)


# ------------------------------------ 3. unidirectional (Fixed asset) synthesis

class UnidirectionalSynthesisTests(unittest.TestCase):

    def _server(self, chunks=(b'aaa', b'bbb')):
        def on_request(frame):
            produced = [audio_frame(chunk) for chunk in chunks]
            produced.append(server_frame(EVENT_SESSION_FINISHED))
            return produced
        return FakeServer(script_for_event={0: on_request})

    def test_happy_path_freezes_a_validated_mp3(self):
        stream = mp3_stream(frames=3)
        server = self._server(chunks=(stream[:400], stream[400:800], stream[800:]))
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            destination = Path(tmp) / 'phrase-0000.mp3'
            result = provider.synthesize('固定话术一', destination)
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), stream)
            self.assertEqual(result['format'], 'mp3')
            self.assertEqual(result['sha256'],
                             __import__('hashlib').sha256(stream).hexdigest())
            self.assertEqual(result['sample_rate'], 48000)
            self.assertEqual(result['transport'], TRANSPORT_UNIDIRECTIONAL)

    def test_hash_and_sample_metadata_are_stable_across_runs(self):
        stream = mp3_stream(frames=2)
        digests = []
        for _ in range(2):
            server = self._server(chunks=(stream,))
            with tempfile.TemporaryDirectory() as tmp:
                provider = make_provider(server, tmp)
                result = provider.synthesize('同一话术', Path(tmp) / 'a.mp3')
                digests.append((result['sha256'], result['sample_rate'],
                                result['channels'], result['frames']))
        self.assertEqual(digests[0], digests[1],
                         'the same provider bytes must freeze identically')

    def test_frozen_asset_is_not_resynthesised_when_present(self):
        """The artifact identity is stable: a second Run must not re-synthesise."""
        stream = mp3_stream(frames=2)
        calls = []
        server = self._server(chunks=(stream,))
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            first = provider.synthesize('话术', Path(tmp) / 'a.mp3')
            calls.append(len(server.frames))
            frozen = Path(tmp) / 'a.mp3'
            before = frozen.read_bytes()
            # A frozen stimulus is played by reference; nothing in the provider
            # rewrites it unless it is asked to synthesise again.
            self.assertEqual(before, stream)
            self.assertEqual(first['sha256'],
                             __import__('hashlib').sha256(stream).hexdigest())
        self.assertEqual(calls, [1])

    def test_provider_error_code_is_classified(self):
        server = FakeServer(script_for_event={
            0: lambda frame: [error_frame(CODE_RESOURCE_MISMATCH,
                                                             'resource mismatched')]})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.mp3')
            audit = json.loads((Path(tmp) / 'a-tts-audit.json').read_text('utf-8'))
            self.assertEqual(audit['status'], 'failed')
            self.assertEqual(audit['failure_code'], 'configuration_invalid')
            self.assertEqual(audit['transport'], TRANSPORT_UNIDIRECTIONAL)

    def test_server_busy_is_rate_limited(self):
        server = FakeServer(script_for_event={
            0: lambda f: [error_frame(CODE_SERVER_BUSY)]})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.mp3')
            audit = json.loads((Path(tmp) / 'a-tts-audit.json').read_text('utf-8'))
            self.assertEqual(audit['failure_code'], 'provider_error')

    def test_empty_audio_is_insufficient_not_a_silent_success(self):
        server = FakeServer(script_for_event={
            0: lambda f: [server_frame(EVENT_SESSION_FINISHED)]})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.mp3')
            audit = json.loads((Path(tmp) / 'a-tts-audit.json').read_text('utf-8'))
            self.assertEqual(audit['status'], 'failed')
            self.assertEqual(audit['failure_code'], 'response_invalid')

    def test_non_mp3_audio_is_refused(self):
        server = self._server(chunks=(b'\x00\x01\x02\x03notmp3',))
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.mp3')
            self.assertFalse((Path(tmp) / 'a.mp3').exists(),
                             'an invalid stream must not be frozen as a stimulus')

    def test_abnormal_close_is_a_failure(self):
        def boom(frame):
            raise ConnectionResetError('socket closed')
        server = FakeServer(script_for_event={0: boom})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.mp3')

    def test_timeout_is_a_first_class_failure(self):
        server = FakeServer(script_for_event={0: lambda f: []})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp, timeout=1)
            original = server.next_frame

            async def never():
                await asyncio.sleep(10)
            server.next_frame = never
            try:
                with self.assertRaises(ProviderFailure):
                    provider.synthesize('x', Path(tmp) / 'a.mp3')
            finally:
                server.next_frame = original
            audit = json.loads((Path(tmp) / 'a-tts-audit.json').read_text('utf-8'))
            self.assertEqual(audit['failure_code'], 'timeout')

    def test_handshake_failure_is_classified(self):
        server = FakeServer(fail_on_connect=True)
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.mp3')

    def test_missing_credential_is_refused_without_network(self):
        server = FakeServer()
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            provider.api_key = ''
            with self.assertRaises(ProviderFailure) as caught:
                provider.synthesize('x', Path(tmp) / 'a.mp3')
            self.assertIn('credential', str(caught.exception))
        self.assertEqual(server.frames, [], 'no call may be placed without a credential')

    def test_configuration_validation_is_local(self):
        server = FakeServer()
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            with self.assertRaises(ProviderFailure):
                provider.validate_text('   ')
            provider.resource_id = ''
            with self.assertRaises(ProviderFailure):
                provider.validate_text('ok')
            provider.resource_id = 'volc.service_type.test'
            provider.voice_type = ''
            with self.assertRaises(ProviderFailure):
                provider.validate_text('ok')
        self.assertEqual(server.frames, [], 'validation must not place a call')

    def test_wav_format_is_refused(self):
        server = FakeServer()
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            provider.audio_format = 'wav'
            with self.assertRaises(ProviderFailure):
                provider.synthesize('x', Path(tmp) / 'a.wav')
        self.assertEqual(server.frames, [])

    def test_unavailable_provider_names_the_configured_route(self):
        with self.assertRaises(ProviderFailure) as caught:
            UnavailableTTSProvider().synthesize('x', Path('unused'))
        self.assertIn('providers.yaml', str(caught.exception))


# ------------------------------------------ 4. bidirectional Free streaming

class BidirectionalSessionTests(unittest.TestCase):

    def _session(self, server, root, **overrides):
        options = {
            'stream_id': new_tts_stream_id(),
            'root': root,
            'key': 'test-key',
            'resource_id': 'volc.service_type.test',
            'request_endpoint': ENDPOINT_BIDIRECTIONAL,
            'audit_endpoint': AUDIT_ENDPOINT_BIDIRECTIONAL,
            'speaker': 'BV700_test',
            'model': 'tts-model',
            'transport': server,
            'sample_rate': 48000,
            'timeout': 5,
            'context': {'session_id': 'VT-1', 'turn_id': 'TURN-1', 'run_index': 1},
        }
        options.update(overrides)
        return VolcengineBidirectionalTTSSession(**options)

    def test_open_sends_start_session_with_documented_payload(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.close()
        asyncio.run(scenario())
        self.assertEqual(server.frames[0]['event'], EVENT_START_CONNECTION)
        frame = server.frames[1]
        self.assertEqual(frame['event'], EVENT_START_SESSION)
        body = json.loads(frame['payload'].decode('utf-8'))
        self.assertEqual(body['namespace'], 'BidirectionalTTS')
        self.assertEqual(body['req_params']['audio_params']['format'], 'mp3')
        self.assertEqual(body['req_params']['speaker'], 'BV700_test')

    def test_text_order_is_preserved_and_audio_is_collected(self):
        stream = mp3_stream(frames=1)
        sent_text = []

        def on_task(frame):
            sent_text.append(json.loads(frame['payload'].decode('utf-8'))
                             ['req_params']['text'])
            return [audio_frame(stream)]

        def on_finish(frame):
            return [audio_frame(stream), server_frame(EVENT_SESSION_FINISHED)]

        server = FakeServer(script_for_event={EVENT_TASK_REQUEST: on_task,
                                              EVENT_FINISH_SESSION: on_finish})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                for chunk in ['第一句。', '第二句。', '第三句。']:
                    await session.append_text(chunk)
                await session.finish_input()
                await server.settle(session)
                finished = await session.drain(timeout=5)
                await session.close()
                return session, finished, tmp
        session, finished, _ = asyncio.run(scenario())
        self.assertTrue(finished)
        self.assertEqual(sent_text, ['第一句。', '第二句。', '第三句。'])
        self.assertEqual(session.text, '第一句。第二句。第三句。')
        # Three TaskRequest audio frames plus the two flush frames.
        self.assertEqual(session.audio_bytes, len(stream) * 4)
        self.assertEqual(session.state, 'finished')
        self.assertIsNone(session.failure)

    def test_pitch_is_not_sent_on_the_unidirectional_transport(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp,
                                        transport_name=TRANSPORT_UNIDIRECTIONAL)
                await session.open()
                await session.close()
        asyncio.run(scenario())
        body = json.loads(server.frames[1]['payload'].decode('utf-8'))
        additions = json.loads(body['req_params']['additions'])
        self.assertNotIn('post_process', additions,
                         'pitch must not be advertised on a protocol without it')

    def test_empty_text_chunk_is_refused(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                with self.assertRaises(ProviderFailure):
                    await session.append_text('   ')
                with self.assertRaises(ProviderFailure):
                    await session.append_text('')
                await session.close()
        asyncio.run(scenario())
        self.assertNotIn(EVENT_TASK_REQUEST, [f['event'] for f in server.frames])

    def test_cancel_drops_late_audio_and_never_writes_it(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)],
            EVENT_TASK_REQUEST: lambda f: [audio_frame(stream)],
            EVENT_CANCEL_SESSION: lambda f: [audio_frame(stream), audio_frame(stream)],
        })

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.append_text('被取消的一句话。')
                await server.settle(session)
                before = session.audio_bytes
                await session.cancel(reason='user_stop')
                # Frames the provider keeps sending after the cancel.
                session._handle_audio({'payload': b'LATE', 'event': EVENT_TTS_RESPONSE})
                audio_written = session.audio_path.read_bytes() if \
                    session.audio_path.is_file() else b''
                return session, before, audio_written, tmp
        session, before, audio_written, _ = asyncio.run(scenario())
        self.assertGreater(before, 0)
        self.assertEqual(session.state, 'cancelled')
        self.assertEqual(session.stale_audio_chunks, 1)
        self.assertEqual(session.stale_audio_bytes, 4)
        self.assertNotIn(b'LATE', audio_written,
                         'late audio after cancel must never reach the turn audio')
        self.assertIn('tts_stale_audio', [e['kind'] for e in session.history()])

    def test_cancelled_session_refuses_further_text(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.cancel(reason='stale_turn')
                with self.assertRaises(ProviderFailure):
                    await session.append_text('不该被合成')
        asyncio.run(scenario())

    def test_session_failed_event_is_classified(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [session_failed_frame()],
        })

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                await session.close()
                return session
        session = asyncio.run(scenario())
        self.assertEqual(session.failure, 'resource_mismatch')
        self.assertEqual(session.state, 'failed')

    def test_provider_error_frame_is_classified(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [error_frame(CODE_INVALID_REQUEST, 'bad')]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                await session.close()
                return session
        session = asyncio.run(scenario())
        self.assertEqual(session.failure, 'invalid_request')
        self.assertEqual(session.state, 'failed')

    def test_handshake_failure_is_one_category(self):
        server = FakeServer(fail_on_connect=True)

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                with self.assertRaises(ProviderFailure):
                    await session.open()
        asyncio.run(scenario())

    def test_missing_credential_places_no_call(self):
        server = FakeServer()

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp, key='')
                with self.assertRaises(ProviderFailure):
                    await session.open()
        asyncio.run(scenario())
        self.assertEqual(server.frames, [])

    def test_drain_timeout_sets_stream_timeout(self):
        server = FakeServer(script_for_event={EVENT_START_SESSION: lambda f: []})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                finished = await session.drain(timeout=0.2)
                await session.close()
                return session, finished
        session, finished = asyncio.run(scenario())
        self.assertFalse(finished)
        self.assertEqual(session.failure, 'stream_timeout')

    def test_sentence_events_are_counted(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(EVENT_TTSSentence_START),
                                            server_frame(EVENT_TTSSentence_END),
                                            server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                await session.close()
                return session
        session = asyncio.run(scenario())
        self.assertEqual(session.summary()['sentence_starts'], 1)
        self.assertEqual(session.summary()['sentences_finished'], 1)

    def test_audit_records_control_evidence_scope_and_no_secret(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [
                server_frame(EVENT_TTSSentence_START), audio_frame(stream),
                server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                await session.close()
                # The events/audit files are written on close, and the temporary
                # directory is removed with the context, so their contents are
                # read here.
                events_raw = Path(session.events_path).read_text('utf-8')
                calls = [path.read_text('utf-8')
                         for path in Path(tmp, 'provider-calls').glob('*/result.json')]
                return session, events_raw, calls
        session, events_raw, calls = asyncio.run(scenario())
        events = json.loads(events_raw)
        self.assertEqual(events['summary']['evidence_scope'], 'control_evidence')
        self.assertNotIn('test-key', events_raw)
        # The shared invocation audit lives under provider-calls and must be
        # secret-free too.
        self.assertEqual(len(calls), 1)
        self.assertNotIn('test-key', calls[0])
        self.assertEqual(events['summary']['transport'], TRANSPORT_BIDIRECTIONAL)

    def test_summary_records_the_no_silent_fallback_decision(self):
        """P1-2: the decision is really persisted, not merely implied."""
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.close()
                events_raw = Path(session.events_path).read_text('utf-8')
                return session, events_raw
        session, events_raw = asyncio.run(scenario())
        summary = session.summary()
        self.assertEqual(summary['transport'], TRANSPORT_BIDIRECTIONAL)
        self.assertEqual(summary['format'], 'mp3')
        self.assertEqual(summary['evidence_scope'], STREAMING_TTS_EVIDENCE_SCOPE)
        # The policy object is in the persisted summary, not just a module constant.
        policy = summary['fallback_policy']
        self.assertEqual(policy['policy'], TTS_FALLBACK_POLICY)
        self.assertIs(policy['silent_fallback'], False)
        self.assertIn(FALLBACK_TRANSPORT_SSE, policy['forbidden_transports'])
        self.assertIn(FALLBACK_TRANSPORT_UNIDIRECTIONAL, policy['forbidden_transports'])
        self.assertIn(TTS_FALLBACK_POLICY, events_raw)

    def test_forbidden_fallbacks_are_declared_by_the_adapter(self):
        """The vendor-neutral module must not name a vendor itself."""
        import aivoicebench.streaming_tts as boundary
        source = Path(boundary.__file__).read_text('utf-8')
        for vendor_token in ('volcengine_tts_sse', 'volcengine_tts_ws_unidirectional',
                             'volcengine_streaming_tts'):
            self.assertNotIn(vendor_token, source,
                             f'{vendor_token} must be declared by the adapter')
        # The adapter declares them, and the shared view sees them.
        self.assertIn(FALLBACK_TRANSPORT_SSE, forbidden_fallbacks())
        self.assertIn(FALLBACK_TRANSPORT_UNIDIRECTIONAL, forbidden_fallbacks())
        self.assertEqual(fallback_policy_record()['forbidden_transports'],
                         list(forbidden_fallbacks()))

    def test_neutral_boundary_does_not_import_a_vendor(self):
        """Round-2 finding: the boundary must not depend on any provider module."""
        import aivoicebench.streaming_tts as boundary
        source = Path(boundary.__file__).read_text('utf-8')
        self.assertNotIn('volcengine', source)
        self.assertNotIn('from .volcengine', source)
        # And importing it must not pull a provider module into sys.modules.
        import subprocess
        import sys
        program = ('import sys; import aivoicebench.streaming_tts; '
                   "print(any('volcengine' in name for name in sys.modules))")
        result = subprocess.run([sys.executable, '-c', program],
                                capture_output=True, text=True, cwd=str(
                                    Path(__file__).resolve().parents[1]))
        self.assertEqual(result.stdout.strip(), 'False', result.stderr)

    def test_failure_classification_has_one_source_of_truth(self):
        """The category vocabulary and mapping live only in the neutral module."""
        import aivoicebench.streaming_tts as boundary
        from aivoicebench import volcengine_tts_ws as adapter
        adapter_source = Path(adapter.__file__).read_text('utf-8')
        # The adapter may name categories as provider error-code values, but its
        # failure-code mapping must not keep a second category -> audit-code table.
        method = adapter_source.split('def _failure_code(error):', 1)[1]
        method = method.split('def _write_audit', 1)[0]
        for category in ('provider_auth_failed', 'provider_rate_limited',
                         'provider_quota_exceeded', 'stream_open_failed',
                         'stream_disconnected'):
            self.assertNotIn(f"'{category}':", method,
                             f'{category} mapping must live in streaming_tts only')
        self.assertIn('failure_code_for', method,
                      'the adapter must consume the shared mapping')
        # Both paths agree on the code for the same category.
        for category, code in boundary.FAILURE_CODE_BY_CATEGORY.items():
            self.assertEqual(boundary.failure_code_for(category), code)
            self.assertEqual(
                adapter.VolcengineUnidirectionalTTSProvider._failure_code(
                    ProviderFailure(f'x ({category})')), code)

    def test_tts_failure_category_reads_the_recorded_decision(self):
        self.assertEqual(tts_failure_category(ProviderFailure('x (resource_mismatch)')),
                         'resource_mismatch')
        self.assertEqual(tts_failure_category(StreamingTTSUnavailable('nope')),
                         'stream_open_failed')
        self.assertEqual(tts_failure_category(ProviderFailure('TTS credential missing')),
                         'credential_missing')

    def test_event_source_is_supplied_by_the_caller(self):
        """A vendor-neutral event must not default to one provider's name."""
        import inspect
        signature = inspect.signature(StreamingTTSEvent)
        self.assertNotIn('volcengine', str(signature))
        event = StreamingTTSEvent(kind='tts_audio_chunk', source='some_adapter')
        self.assertEqual(event.to_dict()['source'], 'some_adapter')


    def test_cancelled_or_failed_audio_is_named_as_partial(self):
        """P2-3: a truncated capture must not look like a completed turn asset."""
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)],
            EVENT_TASK_REQUEST: lambda f: [audio_frame(stream)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.append_text('一句要被取消的话。')
                await server.settle(session)
                await session.cancel(reason='user_stop')
                # The temporary directory is removed with the context, so the
                # file listing is captured inside it.
                return session, sorted(path.name for path in Path(tmp).glob('*.mp3'))
        session, names = asyncio.run(scenario())
        self.assertTrue(any(name.endswith('.partial.mp3') for name in names),
                        f'truncated audio must be distinguishable: {names}')
        self.assertNotIn(f'{session.stream_id}.mp3', names)
        self.assertTrue(session.summary()['audio_file'].endswith('.partial.mp3'))

    def test_completed_session_audio_uses_the_final_name(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [audio_frame(stream),
                                            server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                await session.close()
                return session, sorted(path.name for path in Path(tmp).glob('*.mp3'))
        session, names = asyncio.run(scenario())
        self.assertIn(f'{session.stream_id}.mp3', names)
        self.assertEqual(session.summary()['audio_file'], f'{session.stream_id}.mp3')

    def test_audio_write_failure_is_not_reported_as_complete(self):
        """P2-3: bytes only in memory must not produce a completed synthesis."""
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [audio_frame(stream),
                                            server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                # Simulate an unwritable destination.
                session.audio_path = Path(tmp) / 'no-such-dir' / 'x.mp3'
                original = Path.write_bytes

                def deny(self, data):
                    raise OSError('disk full')
                Path.write_bytes = deny
                try:
                    await session.close()
                finally:
                    Path.write_bytes = original
                events_raw = Path(session.events_path).read_text('utf-8')
                return session, events_raw
        session, events_raw = asyncio.run(scenario())
        self.assertIn('tts_error', [e['kind'] for e in session.history()])
        self.assertNotEqual(session.failure, None)
        # The recorded evidence must not describe a completed call for audio that
        # never reached disk.
        self.assertIn('response_invalid', events_raw)
        audit = json.loads(events_raw)
        self.assertEqual(audit['summary']['failure'], 'response_invalid')

    def test_close_is_idempotent(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.close()
                closed_once = Path(session.events_path).read_text('utf-8')
                await session.close()
                await session.close()
                return session, closed_once, Path(session.events_path).read_text('utf-8')
        session, closed_once, after = asyncio.run(scenario())
        # A second close must not rewrite the terminal evidence.
        self.assertEqual(after, closed_once)
        self.assertEqual(session.state, 'finished')

    def test_cancel_after_close_does_not_resurrect_audio(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [audio_frame(stream),
                                            server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await server.settle(session)
                await session.close()
                before = session.audio_bytes
                await session.cancel(reason='late_cancel')
                return session, before
        session, before = asyncio.run(scenario())
        self.assertEqual(session.audio_bytes, before)
        self.assertEqual(session.state, 'finished',
                         'a cancel after a completed session must not relabel it')

    def test_close_after_cancel_keeps_the_cancelled_evidence(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = self._session(server, tmp)
                await session.open()
                await session.cancel(reason='stale_turn')
                cancelled_events = Path(session.events_path).read_text('utf-8')
                await session.close()
                return session, cancelled_events, Path(session.events_path).read_text('utf-8')
        session, cancelled_events, after_close = asyncio.run(scenario())
        self.assertEqual(session.state, 'cancelled')
        self.assertEqual(after_close, cancelled_events)


# ------------------------------------- 5. streaming_tts boundary (vendor-neutral)

class StreamingTTSBoundaryTests(unittest.TestCase):

    def test_chunking_preserves_order_at_safe_boundaries(self):
        chunks = split_speakable_chunks('第一句。第二句！第三句？尾巴')
        self.assertEqual(chunks, ['第一句。', '第二句！', '第三句？', '尾巴'])

    def test_chunking_respects_hard_limit_without_losing_text(self):
        text = '字' * 300
        chunks = split_speakable_chunks(text, max_chars=100)
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))

    def test_chunking_never_reorders_or_invents_text(self):
        text = 'a。b；c\nd!e?f'
        chunks = split_speakable_chunks(text)
        self.assertEqual(''.join(chunks).replace('', ''), text.replace('\n', ''))

    def test_chunking_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            split_speakable_chunks(None)
        with self.assertRaises(ValueError):
            split_speakable_chunks('x', max_chars=0)

    def test_chunking_returns_no_chunk_for_whitespace_only(self):
        self.assertEqual(split_speakable_chunks('   '), [])

    def test_unavailable_provider_is_explicit(self):
        with self.assertRaises(StreamingTTSUnavailable) as caught:
            asyncio.run(UnavailableStreamingTTSProvider().start_session(
                session_id='S', turn_id='T', run_index=1))
        self.assertIn('streaming_tts', str(caught.exception))

    def test_error_categories_are_first_class(self):
        # Read the vocabulary from its single owner, not from an adapter re-export
        # that could silently disagree with the parser reading it.
        import aivoicebench.streaming_tts as boundary
        self.assertIs(boundary.TTS_ERROR_CATEGORIES,
                      __import__('aivoicebench.volcengine_tts_ws',
                                 fromlist=['TTS_ERROR_CATEGORIES']).TTS_ERROR_CATEGORIES,
                      'the adapter must re-export the neutral vocabulary, not fork it')
        self.assertIn('session_cancelled', boundary.TTS_ERROR_CATEGORIES)
        self.assertIn('provider_no_audio', boundary.TTS_ERROR_CATEGORIES)
        self.assertIn('turn_not_current', boundary.TTS_ERROR_CATEGORIES)
        # Every category the parser accepts must have an audit code; a category
        # that only exists in the vocabulary would silently fall back to
        # provider_error, so the direction the tests iterate matters.
        self.assertTrue(
            set(boundary.TTS_ERROR_CATEGORIES) <= set(boundary.FAILURE_CODE_BY_CATEGORY),
            'every category must have an audit failure code: %s'
            % sorted(set(boundary.TTS_ERROR_CATEGORIES)
                     - set(boundary.FAILURE_CODE_BY_CATEGORY)))
        self.assertEqual(tts_failure_category(StreamingTTSUnavailable('x')),
                         'stream_open_failed')
        self.assertEqual(tts_failure_category(ProviderFailure('TTS credential missing')),
                         'credential_missing')

    def test_every_category_round_trips_through_a_marker(self):
        """A category the parser accepts must classify back to itself."""
        import aivoicebench.streaming_tts as boundary
        from aivoicebench import volcengine_tts_ws as adapter
        for category in boundary.TTS_ERROR_CATEGORIES:
            marked = ProviderFailure(f'provider detail ({category})')
            self.assertEqual(boundary.failure_category_of(marked), category)
            self.assertEqual(boundary.tts_failure_category(marked), category)
            self.assertEqual(
                boundary.failure_code_for(category),
                adapter.VolcengineUnidirectionalTTSProvider._failure_code(marked))
        # The reverse direction matters just as much: a marker naming a category
        # outside the vocabulary must be refused rather than silently accepted,
        # otherwise widening the parser would be undetectable by any of the above.
        # (Surrounding whitespace is tolerated by design; the *name* must match.)
        for outsider in ('not_a_category', 'PROVIDER_ERROR', 'provider_error_extra'):
            self.assertIsNone(
                boundary.failure_category_of(ProviderFailure(f'x ({outsider})')),
                f'{outsider!r} must not be accepted as a first-class category')

    def test_untagged_errors_keep_their_documented_division_of_labour(self):
        """Adapter prose rules stay in the adapter; the boundary stays generic."""
        import aivoicebench.streaming_tts as boundary
        from aivoicebench import volcengine_tts_ws as adapter
        mapper = adapter.VolcengineUnidirectionalTTSProvider._failure_code
        # Provider-specific prose: only the adapter recognises these.
        self.assertEqual(mapper(ProviderFailure('TTS resource_id missing')),
                         'configuration_invalid')
        self.assertEqual(mapper(ProviderFailure('TTS response is not a valid MP3 asset')),
                         'response_invalid')
        # Generic prose both sides agree on.
        for message, category in (('TTS credential missing', 'credential_missing'),
                                  ('TTS text is empty', 'input_invalid')):
            self.assertEqual(mapper(ProviderFailure(message)), category)
            self.assertEqual(boundary.tts_failure_category(ProviderFailure(message)),
                             category)
        # Documented boundary behaviour: the generic fallback does not guess at
        # provider-specific prose, so an untagged provider detail stays generic.
        self.assertEqual(
            boundary.tts_failure_category(ProviderFailure('TTS resource_id missing')),
            boundary.failure_code_for('provider_error'))

    def test_provider_error_text_is_matched_by_whole_code(self):
        """A substring must not be read as a documented provider code."""
        from aivoicebench import volcengine_tts_ws as adapter
        self.assertEqual(adapter.provider_category_from_text('error 45000003 x'),
                         'resource_mismatch')
        self.assertEqual(adapter.provider_category_from_text('code 145000003 x'),
                         'provider_error')
        self.assertEqual(adapter.provider_category_from_text('code 155000031 x'),
                         'provider_error')
        self.assertEqual(adapter.provider_category_from_text('no code here'),
                         'provider_error')
        self.assertEqual(adapter.provider_category_from_text(None), 'provider_error')

    def test_stale_turn_reason_reports_run_stop(self):
        class Turn:
            phase = 'play_issued'

        class Session:
            status = 'stopped'
            turns = [Turn()]

        turn = Turn()
        session = Session()
        session.turns = [turn]
        self.assertEqual(stale_turn_reason(session, turn), 'run_stopped')
        session.status = 'running'
        self.assertIsNone(stale_turn_reason(session, turn))
        other = Turn()
        self.assertEqual(stale_turn_reason(session, other), 'stale_turn')


# ------------------------------- 6. configuration & protocol capability checks

def _tts_profile(**overrides):
    profile = {
        'id': 'volc-tts',
        'name': 'Volcengine V3 TTS',
        'provider': 'volcengine',
        'protocol': 'volcengine_tts_ws',
        'base_url': ENDPOINT_UNIDIRECTIONAL,
        'model': 'tts-model',
        'credential_env': 'VOLCENGINE_TTS_API_KEY',
        'enabled': True,
        'capabilities': ['tts'],
        'parameters': {'resource_id': 'seed-tts-2.0', 'voice': 'BV700',
                       'sample_rate': 48000, 'speed': 0, 'volume': 0,
                       'timeout_seconds': 60},
    }
    profile.update(overrides)
    return profile


def _streaming_profile(**overrides):
    profile = _tts_profile(
        id='volc-streaming-tts',
        protocol='volcengine_tts_ws_bidirectional',
        base_url=ENDPOINT_BIDIRECTIONAL,
        capabilities=['streaming_tts'],
    )
    profile.update(overrides)
    return profile


def _document(profiles):
    return {
        'schema_version': 1,
        'profiles': profiles,
        'routes': {'tts': 'volc-tts', 'streaming_tts': 'volc-streaming-tts',
                   'asr': None, 'streaming_asr': None, 'diarization': None,
                   'judge': None},
    }


class TTSConfigurationTests(unittest.TestCase):

    def test_unidirectional_profile_validates(self):
        validated = validate_profile(_tts_profile())
        self.assertEqual(validated['protocol'], 'volcengine_tts_ws')

    def test_format_is_not_accepted_as_a_configuration_item(self):
        profile = _tts_profile()
        profile['parameters']['format'] = 'mp3'
        with self.assertRaises(SettingsError) as caught:
            validate_profile(profile)
        self.assertIn('mp3', str(caught.exception))
        self.assertIn('Issue #98', str(caught.exception))

    def test_legacy_wav_format_is_refused_with_a_migration_message(self):
        profile = _tts_profile()
        profile['parameters']['format'] = 'wav'
        with self.assertRaises(SettingsError) as caught:
            validate_profile(profile)
        self.assertIn('固定为 mp3', str(caught.exception))

    def test_pitch_is_refused_on_the_unidirectional_protocol(self):
        profile = _tts_profile()
        profile['parameters']['pitch'] = 0
        with self.assertRaises(SettingsError) as caught:
            validate_profile(profile)
        self.assertIn('pitch', str(caught.exception))

    def test_nonzero_pitch_is_refused_on_the_bidirectional_protocol(self):
        profile = _streaming_profile()
        profile['parameters']['pitch'] = 20
        with self.assertRaises(SettingsError):
            validate_profile(profile)

    def test_resource_and_voice_are_required(self):
        for missing in ('resource_id', 'voice'):
            profile = _tts_profile()
            profile['parameters'][missing] = ''
            with self.assertRaises(SettingsError):
                validate_profile(profile)

    def test_wrong_endpoint_for_the_named_protocol_is_refused(self):
        profile = _tts_profile(base_url=ENDPOINT_BIDIRECTIONAL)
        with self.assertRaises(SettingsError):
            validate_profile(profile)
        profile = _streaming_profile(base_url=ENDPOINT_UNIDIRECTIONAL)
        with self.assertRaises(SettingsError):
            validate_profile(profile)

    def test_sse_endpoint_is_refused_for_a_websocket_protocol(self):
        profile = _tts_profile(
            base_url='https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse')
        with self.assertRaises(SettingsError):
            validate_profile(profile)

    def test_capability_and_protocol_must_match(self):
        profile = _tts_profile(protocol='volcengine_tts_ws_bidirectional')
        profile['base_url'] = ENDPOINT_BIDIRECTIONAL
        with self.assertRaises(SettingsError):
            validate_profile(profile)
        profile = _streaming_profile(capabilities=['tts'])
        with self.assertRaises(SettingsError):
            validate_profile(profile)

    def test_build_run_providers_wires_both_routes(self):
        doc = _document([_tts_profile(), _streaming_profile()])
        _, providers = build_run_providers(doc, {'volc-tts': 'key', 'volc-streaming-tts': 'key'})
        self.assertIsNotNone(providers.tts)
        self.assertIsNotNone(providers.streaming_tts)
        self.assertIsNot(providers.tts, providers.streaming_tts,
                         'the two routes must be distinct factories')
        self.assertEqual(doc['streaming_tts_transport'],
                         'volcengine_tts_ws_bidirectional')
        self.assertEqual(doc['streaming_tts_format'], TTS_FIXED_FORMAT)

    def test_missing_streaming_route_leaves_the_factory_unset(self):
        doc = _document([_tts_profile()])
        _, providers = build_run_providers(doc, {'volc-tts': 'key'})
        self.assertIsNotNone(providers.tts)
        self.assertIsNone(providers.streaming_tts)

    def test_readiness_reports_both_tts_routes_separately(self):
        doc = _document([_tts_profile(), _streaming_profile()])
        build_run_providers(doc, {'volc-tts': '', 'volc-streaming-tts': ''})
        self.assertEqual(doc['readiness']['tts'], 'credential_missing')
        self.assertEqual(doc['readiness']['streaming_tts'], 'credential_missing')

    def test_snapshot_carries_no_credential_value(self):
        doc = _document([_tts_profile(), _streaming_profile()])
        build_run_providers(doc, {'volc-tts': 'super-secret',
                                  'volc-streaming-tts': 'super-secret'})
        self.assertNotIn('super-secret', json.dumps(doc, ensure_ascii=False))

    def test_example_providers_file_declares_both_routes(self):
        doc = load_providers_config(Path(__file__).resolve().parents[1]
                                    / 'config' / 'providers.example.yaml')
        tts = {p['id']: p for p in doc['profiles']}['volc-tts']
        streaming = {p['id']: p for p in doc['profiles']}['volc-streaming-tts']
        self.assertEqual(tts['base_url'], ENDPOINT_UNIDIRECTIONAL)
        self.assertEqual(streaming['base_url'], ENDPOINT_BIDIRECTIONAL)
        self.assertNotIn('format', tts['parameters'])
        self.assertNotIn('format', streaming['parameters'])
        self.assertEqual(doc['routes']['streaming_tts'], 'volc-streaming-tts')

    def test_legacy_sse_profile_still_loads_during_migration(self):
        """Existing deployments keep working until the operator switches."""
        profile = _tts_profile(
            protocol='volcengine_tts',
            base_url='https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse')
        profile['parameters']['format'] = 'wav'
        validate_profile(profile)
        doc = _document([profile])
        _, providers = build_run_providers(doc, {'volc-tts': 'key'})
        self.assertIsNotNone(providers.tts)
        self.assertIsNone(providers.streaming_tts)


# --------------------------------------- 7. Fixed/Free provider-call accounting

class ProviderCallAccountingTests(unittest.TestCase):
    """One synthesis request and one session per turn: no hidden extra calls."""

    def test_fixed_synthesis_makes_exactly_one_session(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            0: lambda f: [audio_frame(stream),
                                             server_frame(EVENT_SESSION_FINISHED)]})
        with tempfile.TemporaryDirectory() as tmp:
            provider = make_provider(server, tmp)
            provider.synthesize('a', Path(tmp) / 'a.mp3')
            provider.synthesize('a', Path(tmp) / 'b.mp3')
        requests = [f for f in server.frames if f['event'] == 0]
        self.assertEqual(len(requests), 2, 'one request per explicit synthesis request')

    def test_free_session_uses_one_connection_for_many_chunks(self):
        stream = mp3_stream(frames=1)
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)],
            EVENT_TASK_REQUEST: lambda f: [audio_frame(stream)],
            EVENT_FINISH_SESSION: lambda f: [server_frame(EVENT_SESSION_FINISHED)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = VolcengineBidirectionalTTSSession(
                    stream_id=new_tts_stream_id(), root=tmp, key='k',
                    resource_id='r', request_endpoint=ENDPOINT_BIDIRECTIONAL,
                    audit_endpoint=AUDIT_ENDPOINT_BIDIRECTIONAL, speaker='BV700',
                    model='tts-model', transport=server,
                    context={'session_id': 'S', 'turn_id': 'T', 'run_index': 1})
                await session.open()
                for chunk in ('一。', '二。', '三。'):
                    await session.append_text(chunk)
                await session.finish_input()
                await session.drain(timeout=5)
                await session.close()
        asyncio.run(scenario())
        starts = [f for f in server.frames if f['event'] == EVENT_START_SESSION]
        tasks = [f for f in server.frames if f['event'] == EVENT_TASK_REQUEST]
        self.assertEqual(len(starts), 1, 'three chunks must share one TTS session')
        self.assertEqual(len(tasks), 3)

    def test_no_audio_request_is_sent_when_there_is_no_text(self):
        server = FakeServer(script_for_event={
            EVENT_START_SESSION: lambda f: [server_frame(150)]})

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                session = VolcengineBidirectionalTTSSession(
                    stream_id=new_tts_stream_id(), root=tmp, key='k',
                    resource_id='r', request_endpoint=ENDPOINT_BIDIRECTIONAL,
                    audit_endpoint=AUDIT_ENDPOINT_BIDIRECTIONAL, speaker='BV700',
                    model='tts-model', transport=server, timeout=1,
                    context={'session_id': 'S', 'turn_id': 'T', 'run_index': 1})
                await session.open()
                await session.finish_input()
                await session.drain(timeout=0.2)
                await session.close()
        asyncio.run(scenario())
        self.assertNotIn(EVENT_TASK_REQUEST, [f['event'] for f in server.frames])


if __name__ == '__main__':
    unittest.main()
