"""Volcengine big-model streaming ASR adapter (Active Voice Test / Control Evidence).

Wire contract verified against the live Volcengine documentation on 2026-09-11
(see ``docs/24-streaming-asr.md`` for the page IDs and the verbatim quotes). This
adapter implements **only** what those pages document, and the outgoing bytes are
asserted by tests against ``VERIFIED_REQUEST_FIELDS`` so an unverified field
cannot be added quietly.

Two deliberate limits:

* Only the **new-console** authentication scheme is implemented
  (``X-Api-Key`` + ``X-Api-Resource-Id`` + ``X-Api-Request-Id`` +
  ``X-Api-Sequence: -1``). The legacy console needs an APP ID *and* an access
  token; a second secret slot is not modelled here, and the combination is not
  guessed. ``Authorization: Bearer; …`` / HMAC256 belong to the legacy
  ``/api/v2/asr`` generation and are not used.
* Nothing here asserts real-cloud behaviour. Without credentials no call is made,
  and status stays ``real_cloud_pending``.

The credential stays server-side: it is read from the configured secret store and
sent only on the backend's WebSocket handshake. It never enters the audit record,
the execution trace, an API response or the browser.
"""

import asyncio
import gzip
import inspect
import json
import struct
import uuid
import wave
from pathlib import Path

from .providers import InvocationAudit, ProviderFailure, ProviderIdentity, immutable_json
from .streaming_asr import (BASIS_BROWSER_VAD_TIMEOUT, BASIS_CLIENT_CANCELLED,
                            BASIS_CLIENT_FINISHED, BASIS_PROVIDER_ENDPOINT,
                            BASIS_PROVIDER_LAST_PACKAGE, EVENT_ERROR, EVENT_FINAL,
                            EVENT_PARTIAL, EVENT_SESSION_CLOSED, EVENT_SESSION_STARTED,
                            EVENT_SPEECH_ENDED, SOURCE_PROVIDER, StreamingASREvent,
                            StreamingASRUnavailable, new_stream_id, utc_now)

ENDPOINT = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel'
ENDPOINT_ASYNC = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async'
# The audit records an HTTPS service endpoint (the shared provider contract
# rejects non-HTTPS URLs); the transport uses the wss scheme of the same resource.
AUDIT_ENDPOINT = 'https://openspeech.bytedance.com/api/v3/sauc/bigmodel'
AUDIT_ENDPOINT_ASYNC = 'https://openspeech.bytedance.com/api/v3/sauc/bigmodel_async'

SUPPORTED_ENDPOINTS = (ENDPOINT, ENDPOINT_ASYNC)
# Documented streaming resource IDs (1.0 generation; 2.0 uses volc.seedasr.*).
DOCUMENTED_RESOURCE_IDS = ('volc.bigasr.sauc.duration', 'volc.bigasr.sauc.concurrent',
                           'volc.seedasr.sauc.duration', 'volc.seedasr.sauc.concurrent')

# Documented request properties this adapter is allowed to send. Tests assert the
# outgoing JSON against this tuple.
VERIFIED_REQUEST_FIELDS = ('model_name', 'enable_itn', 'enable_punc', 'enable_ddc',
                           'show_utterances', 'end_window_size', 'force_to_speech_time')
VERIFIED_AUDIO_FIELDS = ('format', 'rate', 'bits', 'channel')

# Documented forced-endpointing bounds. The classic page says "minimum 200" while
# the current pages say [300, 5000] with [800, 1000] recommended; the newer and
# more widely repeated range is enforced here.
END_WINDOW_MIN_MS = 300
END_WINDOW_MAX_MS = 5000
FORCE_TO_SPEECH_MAX_MS = 10000

# Documented guidance: 100-200 ms per packet, 200 ms optimal for bidirectional.
CHUNK_MS = 200
SAMPLE_RATE = 16000
BITS = 16
CHANNELS = 1
BYTES_PER_MS = SAMPLE_RATE * (BITS // 8) * CHANNELS // 1000
CHUNK_BYTES = CHUNK_MS * BYTES_PER_MS

MAX_SESSION_AUDIO_BYTES = SAMPLE_RATE * (BITS // 8) * 300  # 5 minutes of control audio

# Binary protocol constants (documented; integers big-endian).
PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001
MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_AUDIO_ONLY_REQUEST = 0b0010
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_ERROR = 0b1111
FLAG_NONE = 0b0000
FLAG_POSITIVE_SEQUENCE = 0b0001
FLAG_LAST_NO_SEQUENCE = 0b0010
FLAG_NEGATIVE_SEQUENCE = 0b0011
SERIALIZATION_NONE = 0b0000
SERIALIZATION_JSON = 0b0001
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

# Documented application error codes.
CODE_SUCCESS = 20000000
CODE_INVALID_REQUEST = 45000001
CODE_EMPTY_AUDIO = 45000002
CODE_PACKET_TIMEOUT = 45000081
CODE_INVALID_AUDIO_FORMAT = 45000151
CODE_SERVER_BUSY = 55000031

# Vendor code -> the unified first-class failure category.
CODE_ERROR_CATEGORY = {
    CODE_INVALID_REQUEST: 'provider_error',
    CODE_EMPTY_AUDIO: 'provider_error',
    CODE_PACKET_TIMEOUT: 'stream_timeout',
    CODE_INVALID_AUDIO_FORMAT: 'invalid_audio',
    CODE_SERVER_BUSY: 'provider_rate_limited',
}


class VolcengineStreamingProtocolError(RuntimeError):
    """Raised when a server frame cannot be parsed as the documented protocol."""


# --------------------------------------------------------------------- codec

def build_header(message_type, flags, serialization, compression):
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def compress_payload(payload: bytes, compression):
    if compression == COMPRESSION_GZIP:
        return gzip.compress(payload)
    if compression == COMPRESSION_NONE:
        return payload
    raise VolcengineStreamingProtocolError('Unsupported compression flag')


def decompress_payload(payload: bytes, compression):
    if compression == COMPRESSION_GZIP:
        try:
            return gzip.decompress(payload)
        except OSError as error:
            raise VolcengineStreamingProtocolError('Invalid gzip payload') from error
    if compression == COMPRESSION_NONE:
        return payload
    raise VolcengineStreamingProtocolError('Unsupported compression flag')


def build_full_client_request(payload: dict, *, compression=COMPRESSION_GZIP):
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    body = compress_payload(body, compression)
    header = build_header(MSG_FULL_CLIENT_REQUEST, FLAG_NONE, SERIALIZATION_JSON, compression)
    return header + struct.pack('>I', len(body)) + body


def build_audio_frame(pcm: bytes, *, compression=COMPRESSION_NONE, last=False):
    body = compress_payload(pcm, compression)
    flags = FLAG_LAST_NO_SEQUENCE if last else FLAG_NONE
    header = build_header(MSG_AUDIO_ONLY_REQUEST, flags, SERIALIZATION_NONE, compression)
    return header + struct.pack('>I', len(body)) + body


def parse_frame(data: bytes) -> dict:
    """Parse one documented frame, in either direction.

    Accepts the documented layouts:
      full client request  : [header][payload size][payload]
      audio only request   : [header][payload size][payload]
      full server response : [header][sequence][payload size][payload]
      response without seq : [header][payload size][payload]
      error                : [header][code][size][message]

    Parsing both directions with one implementation keeps the tests honest: they
    decode the bytes the client actually put on the wire rather than a private
    reimplementation.
    """
    if len(data) < 4:
        raise VolcengineStreamingProtocolError('Frame shorter than the documented header')
    header_size = (data[0] & 0x0F) * 4
    if header_size != 4 or len(data) < header_size + 4:
        raise VolcengineStreamingProtocolError('Unsupported header size')
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    if message_type == MSG_ERROR:
        if len(data) < header_size + 8:
            raise VolcengineStreamingProtocolError('Truncated error frame')
        code, size = struct.unpack('>II', data[header_size:header_size + 8])
        message = data[header_size + 8:header_size + 8 + size]
        return {'message_type': message_type, 'flags': flags, 'serialization': serialization,
                'compression': compression, 'sequence': None, 'payload': None,
                'error_code': code, 'error_message': message.decode('utf-8', 'replace')}
    if message_type not in (MSG_FULL_CLIENT_REQUEST, MSG_AUDIO_ONLY_REQUEST,
                            MSG_FULL_SERVER_RESPONSE):
        raise VolcengineStreamingProtocolError(f'Unexpected message type {message_type:#06b}')
    offset = header_size
    sequence = None
    if message_type == MSG_FULL_SERVER_RESPONSE and flags in (FLAG_POSITIVE_SEQUENCE,
                                                              FLAG_NEGATIVE_SEQUENCE):
        if len(data) < offset + 4:
            raise VolcengineStreamingProtocolError('Truncated sequence field')
        sequence = struct.unpack('>i', data[offset:offset + 4])[0]
        offset += 4
    if len(data) < offset + 4:
        raise VolcengineStreamingProtocolError('Truncated payload size field')
    size = struct.unpack('>I', data[offset:offset + 4])[0]
    offset += 4
    if len(data) < offset + size:
        raise VolcengineStreamingProtocolError('Truncated payload')
    return {'message_type': message_type, 'flags': flags, 'serialization': serialization,
            'compression': compression, 'sequence': sequence,
            'payload': data[offset:offset + size], 'error_code': None, 'error_message': None}


def extract_result(document: dict):
    """Return (result_dict, shape) across the two documented nestings."""
    message = document.get('payload_msg')
    if isinstance(message, dict) and isinstance(message.get('result'), dict):
        return message['result'], 'payload_msg'
    if isinstance(document.get('result'), dict):
        return document['result'], 'top_level'
    return None, None


# ------------------------------------------------------------------- transport

class _RealConnection:
    def __init__(self, socket):
        self._socket = socket

    async def send(self, data: bytes):
        await self._socket.send(data)

    async def recv(self):
        return await self._socket.recv()

    async def close(self):
        await self._socket.close()


class WebSocketTransport:
    """Default transport: a real ``websockets`` client connection."""

    def __init__(self, *, open_timeout=15, close_timeout=5, max_size=8 * 1024 * 1024):
        self.open_timeout = open_timeout
        self.close_timeout = close_timeout
        self.max_size = max_size

    async def connect(self, url: str, headers: dict):
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:  # pragma: no cover - dependency guard
            raise StreamingASRUnavailable(
                'The websockets client is required for streaming ASR') from error
        parameters = set(inspect.signature(connect).parameters)
        header_kw = ('additional_headers' if 'additional_headers' in parameters
                     else 'extra_headers')
        socket = await connect(url, **{header_kw: headers}, open_timeout=self.open_timeout,
                               close_timeout=self.close_timeout, max_size=self.max_size)
        return _RealConnection(socket)


# --------------------------------------------------------------------- session

class VolcengineStreamingSession:
    """One streaming recognition session against the documented SAUC protocol."""

    def __init__(self, *, stream_id, root, key, resource_id, request_endpoint,
                 audit_endpoint, model='bigmodel', transport, compression=COMPRESSION_GZIP,
                 timeout=300, retain_audio=True, end_window_size=800,
                 force_to_speech_time=1000, enable_itn=False, enable_punc=True,
                 enable_ddc=False, audit_root=None, context=None):
        self.stream_id = stream_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.key = key
        self.resource_id = resource_id
        self.request_endpoint = request_endpoint
        self.audit_endpoint = audit_endpoint
        self.model = model
        self.transport = transport
        self.compression = compression
        self.timeout = timeout
        self.retain_audio = retain_audio
        self.context = dict(context or {})
        self.audio_path = self.root / f'{stream_id}.wav'
        self.events_path = self.root / f'{stream_id}-events.json'
        self.request_path = self.root / f'{stream_id}-request.json'
        self.config = {
            'model_name': model,
            'resource_id': resource_id,
            'transport': 'websocket',
            'websocket_path': request_endpoint.split('openspeech.bytedance.com')[-1],
            'audio_format': 'pcm',
            'audio_rate': SAMPLE_RATE,
            'audio_bits': BITS,
            'audio_channel': CHANNELS,
            'chunk_bytes': CHUNK_BYTES,
            'compression': 'gzip' if compression == COMPRESSION_GZIP else 'none',
            'enable_itn': enable_itn,
            'enable_punc': enable_punc,
            'enable_ddc': enable_ddc,
            'show_utterances': True,
            'end_window_size': end_window_size,
            'force_to_speech_time': force_to_speech_time,
            'timeout_seconds': timeout,
            'retain_audio': bool(retain_audio),
            'auth_scheme': 'new_console_api_key',
        }
        self.profile = {
            'provider': 'volcengine',
            'model_id': model,
            'model_version': 'service-managed',
            'model_sha256': None,
            'library_version': 'aivoicebench-volcengine-streaming:1.0.0',
            'interface_contract_verified': {
                'verified_at': '2026-09-11',
                'source': 'docs.volcengine.com live pages (see docs/24-streaming-asr.md)',
                'request_fields': list(VERIFIED_REQUEST_FIELDS),
                'audio_fields': list(VERIFIED_AUDIO_FIELDS),
                'termination': 'binary flag 0b0010 (last packet) + is_last_package',
                'real_cloud_call': 'not_attempted',
            },
            'config': dict(self.config),
        }
        self._events = []
        # Append-only history: poll_events() drains the queue, but the audit and
        # the execution trace need the complete set, so keep both.
        self._history = []
        self._event_signal = asyncio.Event()
        self._buffer = bytearray()
        self._audio_bytes = 0
        self._wave = None
        self._reader = None
        self._state = 'created'
        self._failure = None
        self._last_text = ''
        self._last_definite = ''
        self._saw_last_package = False
        self._audit = None
        self._audit_outputs = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- lifecycle

    async def open(self):
        immutable_json(self.request_path, {
            'schema_version': '1.0.0', 'stream_id': self.stream_id,
            'endpoint': self.audit_endpoint, 'transport': 'websocket',
            'config': self.config, 'context': self.context,
            'note': 'Secret-free request descriptor; credentials are never recorded'})
        self._audit = InvocationAudit(
            self.root, ProviderIdentity('volcengine', self.model,
                                        'volcengine-streaming:1.0.0',
                                        self.audit_endpoint, 'v3'),
            self.config, self._closed_config(), [self.request_path],
            operation_id=f'OP-{self.stream_id}', attempt=1)
        headers = {
            'X-Api-Key': self.key,
            'X-Api-Resource-Id': self.resource_id,
            'X-Api-Request-Id': str(uuid.uuid4()),
            'X-Api-Sequence': '-1',
            'X-Api-Connect-Id': str(uuid.uuid4()),
        }
        if not self.key:
            self._failure = 'provider_auth_failed'
            raise StreamingASRUnavailable('Streaming ASR credential missing')
        try:
            connection = await self.transport.connect(self.request_endpoint, headers)
        except Exception as error:  # noqa: BLE001 - any handshake failure is one category
            category = self._classify_open_failure(error)
            self._failure = category
            self._record(StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER,
                                           error=category,
                                           detail={'phase': 'connect',
                                                   'error_type': type(error).__name__}))
            self._finish_audit('failed', failure_code='unavailable')
            raise StreamingASRUnavailable(
                f'Streaming ASR session could not be opened ({category})') from None
        self._connection = connection
        self._state = 'open'
        self._record(StreamingASREvent(kind=EVENT_SESSION_STARTED, source=SOURCE_PROVIDER,
                                       detail={'endpoint': self.audit_endpoint,
                                               'resource_id': self.resource_id,
                                               'auth_scheme': 'new_console_api_key'}))
        payload = {
            'audio': {'format': 'pcm', 'rate': SAMPLE_RATE, 'bits': BITS, 'channel': CHANNELS},
            'request': {'model_name': self.model, 'enable_itn': self.config['enable_itn'],
                        'enable_punc': self.config['enable_punc'],
                        'enable_ddc': self.config['enable_ddc'], 'show_utterances': True,
                        'end_window_size': self.config['end_window_size'],
                        'force_to_speech_time': self.config['force_to_speech_time']},
        }
        request_sent = False
        try:
            await connection.send(build_full_client_request(
                payload, compression=self.compression))
            request_sent = True
            self._reader = asyncio.create_task(self._read_loop())
            if self.retain_audio:
                self._wave = wave.open(str(self.audio_path), 'wb')
                self._wave.setparams(
                    (CHANNELS, BITS // 8, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
        except Exception as error:  # noqa: BLE001 - close partial startup deterministically
            category = 'audio_capture_failed' if request_sent else 'stream_open_failed'
            self._failure = self._failure or category
            self._record(StreamingASREvent(
                kind=EVENT_ERROR, source=SOURCE_PROVIDER, error=category,
                detail={'phase': 'initialise', 'error_type': type(error).__name__}))
            await self.cancel(reason='stream_initialise_failed')
            raise StreamingASRUnavailable(
                f'Streaming ASR session could not be initialised ({category})') from None
        return self

    @staticmethod
    def _classify_open_failure(error):
        text = f'{type(error).__name__} {error}'.lower()
        if any(term in text for term in ('401', '403', 'unauthor', 'forbidden', 'invalidstatus')):
            return 'provider_auth_failed'
        if any(term in text for term in ('429', 'rate', 'busy')):
            return 'provider_rate_limited'
        if 'timeout' in text:
            return 'stream_timeout'
        return 'stream_open_failed'

    # ----------------------------------------------------------------- audio

    async def push_audio(self, pcm: bytes):
        if self._state != 'open':
            raise StreamingASRUnavailable('Streaming ASR session is not open')
        if not isinstance(pcm, (bytes, bytearray)) or not pcm:
            self._failure = 'invalid_audio'
            self._record(StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER,
                                           error='invalid_audio',
                                           detail={'reason': 'empty or non-bytes audio chunk'}))
            return
        if len(pcm) % 2:
            self._failure = 'invalid_audio'
            self._record(StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER,
                                           error='invalid_audio',
                                           detail={'reason': 'PCM16 chunk has an odd byte length'}))
            return
        if self._audio_bytes + len(pcm) > MAX_SESSION_AUDIO_BYTES:
            self._failure = 'audio_capture_failed'
            self._record(StreamingASREvent(
                kind=EVENT_ERROR, source=SOURCE_PROVIDER, error='audio_capture_failed',
                detail={'reason': 'session audio exceeded the configured control bound'}))
            return
        self._audio_bytes += len(pcm)
        if self._wave is not None:
            self._wave.writeframes(bytes(pcm))
        self._buffer.extend(pcm)
        while len(self._buffer) >= CHUNK_BYTES:
            chunk = bytes(self._buffer[:CHUNK_BYTES])
            del self._buffer[:CHUNK_BYTES]
            await self._connection.send(build_audio_frame(chunk, compression=COMPRESSION_NONE))

    async def _send_last_packet(self):
        if self._buffer:
            await self._connection.send(build_audio_frame(bytes(self._buffer),
                                                          compression=COMPRESSION_NONE))
            self._buffer.clear()
        # Documented "last packet" framing: flag 0b0010, no trailing sequence.
        await self._connection.send(build_audio_frame(b'', compression=COMPRESSION_NONE,
                                                      last=True))
        self._state = 'finishing'

    async def finish_input(self):
        if self._state == 'open':
            try:
                await self._send_last_packet()
            except Exception:  # noqa: BLE001
                self._failure = self._failure or 'stream_disconnected'
                self._record(StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER,
                                               error='stream_disconnected',
                                               detail={'phase': 'finish_input'}))

    # ---------------------------------------------------------------- events

    def _record(self, event: StreamingASREvent):
        self._history.append(event)
        self._events.append(event)
        self._event_signal.set()

    def history(self):
        return list(self._history)

    def poll_events(self):
        events, self._events = self._events, []
        if self._events:
            self._event_signal.set()
        else:
            self._event_signal.clear()
        return events

    async def wait_events(self, timeout: float):
        """Wait up to ``timeout`` for at least one event; [] on timeout."""
        if self._events:
            return self.poll_events()
        self._event_signal.clear()
        try:
            await asyncio.wait_for(self._event_signal.wait(), timeout)
        except asyncio.TimeoutError:
            return []
        return self.poll_events()

    async def _read_loop(self):
        try:
            while True:
                raw = await self._connection.recv()
                if isinstance(raw, str):
                    raw = raw.encode('utf-8')
                self._handle_frame(raw)
                if self._saw_last_package:
                    break
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise
        except Exception as error:  # noqa: BLE001
            if self._state in ('closed', 'cancelled'):
                return
            text = f'{type(error).__name__} {error}'.lower()
            category = 'stream_timeout' if 'timeout' in text else 'stream_disconnected'
            self._failure = self._failure or category
            self._record(StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER,
                                           error=category,
                                           detail={'phase': 'read',
                                                   'error_type': type(error).__name__}))
        finally:
            if self._saw_last_package and self._state == 'finishing':
                self._state = 'finished'

    def _handle_frame(self, raw: bytes):
        frame = parse_frame(raw)
        if frame['message_type'] == MSG_ERROR:
            category = CODE_ERROR_CATEGORY.get(frame['error_code'], 'provider_error')
            self._failure = self._failure or category
            self._record(StreamingASREvent(
                kind=EVENT_ERROR, source=SOURCE_PROVIDER, error=category,
                detail={'phase': 'protocol', 'provider_code': frame['error_code'],
                        'provider_message': frame['error_message'][:200]}))
            return
        body = decompress_payload(frame['payload'], frame['compression'])
        try:
            document = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            self._failure = self._failure or 'provider_error'
            self._record(StreamingASREvent(kind=EVENT_ERROR, source=SOURCE_PROVIDER,
                                           error='provider_error',
                                           detail={'phase': 'decode'}))
            return
        if not isinstance(document, dict):
            return
        code = document.get('code')
        if isinstance(code, int) and code != 0:
            category = CODE_ERROR_CATEGORY.get(code, 'provider_error')
            self._failure = self._failure or category
            self._record(StreamingASREvent(
                kind=EVENT_ERROR, source=SOURCE_PROVIDER, error=category,
                sequence=frame['sequence'],
                detail={'phase': 'result', 'provider_code': code}))
            return
        result, shape = extract_result(document)
        sequence = document.get('payload_sequence') or frame['sequence']
        if isinstance(result, dict):
            self._emit_transcripts(result, shape, sequence)
        if document.get('is_last_package') is True:
            self._saw_last_package = True
            if not self._last_text.strip():
                self._failure = self._failure or 'asr_no_final'
                self._record(StreamingASREvent(
                    kind=EVENT_SPEECH_ENDED, source=SOURCE_PROVIDER,
                    basis=BASIS_PROVIDER_LAST_PACKAGE, sequence=sequence,
                    detail={'note': 'provider reported the last package with no usable text'}))
            else:
                self._emit_final(sequence, BASIS_PROVIDER_LAST_PACKAGE)

    def _emit_transcripts(self, result, shape, sequence):
        text = result.get('text')
        utterances = result.get('utterances') if isinstance(result.get('utterances'), list) else []
        definite_parts = [u.get('text', '') for u in utterances
                          if isinstance(u, dict) and u.get('definite') is True and u.get('text')]
        definite = ''.join(definite_parts).strip()
        if isinstance(text, str) and text.strip() and text.strip() != self._last_text:
            self._last_text = text.strip()
            self._record(StreamingASREvent(
                kind=EVENT_PARTIAL, source=SOURCE_PROVIDER, text=self._last_text,
                sequence=sequence if isinstance(sequence, int) else None,
                detail={'response_shape': shape, 'definite': bool(definite)}))
        if definite and definite != self._last_definite:
            self._last_definite = definite
            self._record(StreamingASREvent(
                kind=EVENT_FINAL, source=SOURCE_PROVIDER, text=definite,
                sequence=sequence if isinstance(sequence, int) else None,
                basis=BASIS_PROVIDER_ENDPOINT,
                detail={'response_shape': shape,
                        'definite_utterances': len(definite_parts),
                        'note': 'provider marked this segment definite (endpoint)'}))
            self._record(StreamingASREvent(
                kind=EVENT_SPEECH_ENDED, source=SOURCE_PROVIDER,
                basis=BASIS_PROVIDER_ENDPOINT, sequence=sequence if isinstance(sequence, int) else None,
                detail={'note': 'endpoint inferred from definite utterances'}))

    def _emit_final(self, sequence, basis):
        if self._last_definite:
            return
        self._last_definite = self._last_text
        self._record(StreamingASREvent(
            kind=EVENT_FINAL, source=SOURCE_PROVIDER, text=self._last_text,
            sequence=sequence if isinstance(sequence, int) else None, basis=basis,
            detail={'note': 'final text taken from the last package'}))
        self._record(StreamingASREvent(
            kind=EVENT_SPEECH_ENDED, source=SOURCE_PROVIDER, basis=basis,
            sequence=sequence if isinstance(sequence, int) else None,
            detail={'note': 'segment ended with the last package'}))

    # ----------------------------------------------------------------- state

    @property
    def state(self):
        return self._state

    @property
    def failure(self):
        return self._failure

    @property
    def last_text(self):
        return self._last_definite or self._last_text

    @property
    def saw_last_package(self):
        return self._saw_last_package

    def summary(self):
        return {
            'stream_id': self.stream_id,
            'endpoint': self.audit_endpoint,
            'resource_id': self.resource_id,
            'auth_scheme': 'new_console_api_key',
            'audio_format': {'format': 'pcm', 'rate': SAMPLE_RATE, 'bits': BITS,
                             'channel': CHANNELS},
            'audio_bytes': self._audio_bytes,
            'chunk_bytes': CHUNK_BYTES,
            'state': self._state,
            'failure': self._failure,
            'saw_last_package': self._saw_last_package,
            'final_text': self.last_text,
            'evidence_scope': 'control_evidence',
        }

    async def close(self, reason: str = BASIS_CLIENT_FINISHED):
        await self._shutdown(reason, cancelled=False)

    async def cancel(self, reason: str = BASIS_CLIENT_CANCELLED):
        await self._shutdown(reason, cancelled=True)

    async def _shutdown(self, reason, *, cancelled):
        if self._state in ('closed', 'cancelled'):
            return
        async with self._lock:
            self._state = 'cancelled' if cancelled else 'closed'
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            connection = getattr(self, '_connection', None)
            if connection is not None:
                try:
                    await connection.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._wave is not None:
                self._wave.close()
                self._wave = None
            self._record(StreamingASREvent(
                kind=EVENT_SESSION_CLOSED, source=SOURCE_PROVIDER, basis=reason,
                detail={'cancelled': bool(cancelled), 'audio_bytes': self._audio_bytes,
                        'final_text': self.last_text, 'failure': self._failure}))
            status, failure_code = self._terminal_status(cancelled)
            self._finish_audit(status, failure_code=failure_code)

    def _terminal_status(self, cancelled):
        if cancelled and self._failure is None:
            return 'partial', None
        if self._failure in ('provider_auth_failed', 'provider_rate_limited',
                             'stream_open_failed', 'stream_disconnected'):
            return 'failed', 'unavailable'
        if self._failure == 'stream_timeout':
            return 'failed', 'timeout'
        if self._failure in ('invalid_audio', 'audio_capture_failed'):
            return 'failed', 'invalid_output'
        if self._failure == 'provider_error':
            return 'failed', 'provider_error'
        if self._last_text.strip():
            return 'complete', None
        return 'insufficient_evidence', None

    def _closed_config(self):
        return {'type': 'object', 'additionalProperties': False, 'required': list(self.config),
                'properties': {key: {'const': value} for key, value in self.config.items()}}

    def _finish_audit(self, status, failure_code=None):
        if self._audit is None or self._audit.finished:
            return
        immutable_json(self.events_path, {
            'schema_version': '1.0.0', 'stream_id': self.stream_id,
            'summary': self.summary(),
            'events': [event.to_dict() for event in self._history]})
        outputs = [self.events_path]
        if self.audio_path.is_file() and self.audio_path.stat().st_size > 44:
            outputs.append(self.audio_path)
        outputs.extend(self._audit_outputs)
        try:
            self._audit.finish(status, outputs, failure_code=failure_code)
        except Exception:  # noqa: BLE001 - audit failure must not mask the run outcome
            pass


# -------------------------------------------------------------------- provider

class VolcengineStreamingASRProvider:
    """Factory for :class:`VolcengineStreamingSession` instances."""

    name = 'volcengine_streaming_asr'

    def __init__(self, root, api_key, *, endpoint=ENDPOINT, resource_id='volc.bigasr.sauc.duration',
                 model='bigmodel', transport=None, timeout=300, retain_audio=True,
                 end_window_size=800, force_to_speech_time=1000, enable_itn=False,
                 enable_punc=True, enable_ddc=False):
        if endpoint not in SUPPORTED_ENDPOINTS:
            raise ProviderFailure('Unsupported streaming endpoint; use a documented sauc endpoint')
        if resource_id not in DOCUMENTED_RESOURCE_IDS:
            raise ProviderFailure('Unsupported streaming resource id')
        if model != 'bigmodel':
            raise ProviderFailure('The documented streaming model_name is bigmodel')
        if not isinstance(end_window_size, int) or not END_WINDOW_MIN_MS <= end_window_size <= END_WINDOW_MAX_MS:
            raise ProviderFailure(f'end_window_size must be within '
                                  f'{END_WINDOW_MIN_MS}-{END_WINDOW_MAX_MS} ms')
        if not isinstance(force_to_speech_time, int) or not 0 <= force_to_speech_time <= FORCE_TO_SPEECH_MAX_MS:
            raise ProviderFailure('force_to_speech_time is outside the accepted range')
        self.root = Path(root)
        self.key = api_key
        self.endpoint = endpoint
        self.audit_endpoint = (AUDIT_ENDPOINT_ASYNC if endpoint == ENDPOINT_ASYNC
                               else AUDIT_ENDPOINT)
        self.resource_id = resource_id
        self.model = model
        self.transport = transport or WebSocketTransport()
        self.timeout = timeout
        self.retain_audio = retain_audio
        self.end_window_size = end_window_size
        self.force_to_speech_time = force_to_speech_time
        self.enable_itn = enable_itn
        self.enable_punc = enable_punc
        self.enable_ddc = enable_ddc

    async def start_session(self, *, session_id, turn_id, run_index, directory=None):
        if not self.key:
            raise StreamingASRUnavailable('Streaming ASR credential missing')
        root = Path(directory) if directory is not None else self.root
        session = VolcengineStreamingSession(
            stream_id=new_stream_id(), root=root, key=self.key, resource_id=self.resource_id,
            request_endpoint=self.endpoint, audit_endpoint=self.audit_endpoint,
            model=self.model, transport=self.transport, timeout=self.timeout,
            retain_audio=self.retain_audio, end_window_size=self.end_window_size,
            force_to_speech_time=self.force_to_speech_time, enable_itn=self.enable_itn,
            enable_punc=self.enable_punc, enable_ddc=self.enable_ddc,
            context={'session_id': session_id, 'turn_id': turn_id, 'run_index': run_index})
        return await session.open()
