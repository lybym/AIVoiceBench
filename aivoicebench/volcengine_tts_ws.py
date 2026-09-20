"""Volcengine (Doubao) TTS **V3 WebSocket** adapters (Active Voice Test).

Issue #98 replaces the V3 HTTP SSE one-shot adapter as the Active Voice Test TTS
transport with two explicitly separated capabilities (PRD-F020 / PRD-F021):

``tts``           V3 **unidirectional** WebSocket — a complete fixed text goes
                  in, MP3 audio frames come back, and the result is frozen as
                  an immutable MP3 Stimulus Artifact before any formal Run
                  plays it.
``streaming_tts`` V3 **bidirectional** WebSocket — the Free Test Agent appends
                  speakable text chunks as the LLM streams them, and MP3 audio
                  frames come back on the same session for streaming playback.

Both live on the ``openspeech.bytedance.com`` V3 WebSocket family that
``volcengine_streaming_asr.py`` already implements, and both are backend-only:
the credential is sent on the backend's handshake and never enters an audit
record, an API response or the browser.

Wire contract
-------------
The frame layout below is the documented V3 "event" envelope, re-verified
against the live official pages on 2026-09-18. The stable page ids are
``6561/2628951`` ("WebSocket 单向流式-V3") and ``6561/1329505``
("WebSocket 双向流式-V3"); the boundary, the capability matrix and the
unverified list live in ``docs/28-active-tts.md``::

    header          4 B : (version<<4|header_size), (msg_type<<4|flags),
                          (serialization<<4|compression), reserved
    client request      : [header][event i32][sid_len i32][sid][payload_len i32][payload]
    server response     : [header][event i32] then event-specific fields
    error from server   : [header][event i32][error_code i32][payload_len i32][payload]

``message_type`` is ``0b0001`` full client request, ``0b1001`` full server
response, ``0b1011`` audio-only response (TTS audio), ``0b1111`` error.
Bidirectional requests use the ``with event`` flag ``0b0100``.  The
unidirectional endpoint is deliberately different: it receives one full client
request without an event envelope.

Only the documented fields are ever sent; ``VERIFIED_REQUEST_FIELDS`` is
asserted by tests against the bytes actually put on the wire, so an unverified
field cannot be added quietly.

Deliberate limits
-----------------
* Only the new-console single-key authentication scheme is implemented.  The
  unidirectional endpoint requires ``X-Api-Key``, ``X-Api-Resource-Id`` and
  ``X-Api-Request-Id``; the bidirectional endpoint requires ``X-Api-Key`` and
  ``X-Api-Resource-Id`` and accepts ``X-Api-Connect-Id``.
  The legacy dual-credential console needs a second secret slot and is not
  guessed here.
* The Media format is fixed to **MP3** and is not configurable (Issue #98).
  ``format=wav``/``pcm`` is refused rather than silently coerced, because the
  product decided the frozen Stimulus Artifact is MP3.
* Nothing here claims real-cloud behaviour. Without credentials no call is made
  and the recorded status stays ``real_cloud_pending``.
"""

import asyncio
import io
import json
import re
import struct
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .providers import InvocationAudit, ProviderFailure, ProviderIdentity, immutable_json
from .streaming_tts import (failure_category_of, failure_code_for,
                              fallback_policy_record,
                              register_forbidden_fallback)

# The retired/alternate Active TTS transports this adapter must never be reached
# through implicitly. Declared here — where the transports actually live — so the
# shared boundary module stays vendor-neutral (Issue #98 §3, PRD-F020/F021).
FALLBACK_TRANSPORT_SSE = register_forbidden_fallback('volcengine_tts_sse')
FALLBACK_TRANSPORT_UNIDIRECTIONAL = register_forbidden_fallback('volcengine_tts_ws_unidirectional')

# ------------------------------------------------------------------ endpoints

# V3 unidirectional (complete text in, streamed audio out) — Fixed asset synthesis.
ENDPOINT_UNIDIRECTIONAL = 'wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream'
# V3 bidirectional (streamed text in, streamed audio out) — Free streaming session.
ENDPOINT_BIDIRECTIONAL = 'wss://openspeech.bytedance.com/api/v3/tts/bidirection'
# The shared provider-invocation audit contract records an HTTPS service endpoint.
AUDIT_ENDPOINT_UNIDIRECTIONAL = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional/stream'
AUDIT_ENDPOINT_BIDIRECTIONAL = 'https://openspeech.bytedance.com/api/v3/tts/bidirection'

TRANSPORT_UNIDIRECTIONAL = 'volcengine_tts_ws_unidirectional'
TRANSPORT_BIDIRECTIONAL = 'volcengine_tts_ws_bidirectional'

# Active TTS media format is fixed by product decision (Issue #98); it is not a
# configuration knob and there is no WAV/PCM encapsulation copy.
TTS_FORMAT = 'mp3'
TTS_AUDIO_FIELDS = ('format', 'sample_rate', 'speech_rate', 'loudness_rate')

VERIFIED_REQUEST_FIELDS = ('user.uid', 'event', 'namespace', 'req_params.text',
                           'req_params.speaker', 'req_params.audio_params.format',
                           'req_params.audio_params.sample_rate',
                           'req_params.audio_params.speech_rate',
                           'req_params.audio_params.loudness_rate',
                           'req_params.additions')
VERIFIED_RESPONSE_FIELDS = ('event', 'session_id', 'connect_id', 'payload')

# Documented protocol capability. Pitch adjustment is not implemented here because
# the unidirectional page still marks it unsupported (see docs/28-active-tts.md);
# declaring it would advertise a capability the protocol does not have.
CAPABILITY_MATRIX = {
    TRANSPORT_UNIDIRECTIONAL: {
        'complete_text': True,
        'streaming_text': False,
        'streaming_audio': True,
        'pitch': False,
        'loudness': True,
    },
    TRANSPORT_BIDIRECTIONAL: {
        'complete_text': True,
        'streaming_text': True,
        'streaming_audio': True,
        'pitch': True,
        'loudness': True,
    },
}

INTERFACE_CONTRACT = {
    'verified_at': '2026-09-20',
    'source': 'docs.volcengine.com live pages (see docs/28-active-tts.md)',
    'endpoints': {'unidirectional': ENDPOINT_UNIDIRECTIONAL,
                  'bidirectional': ENDPOINT_BIDIRECTIONAL},
    'auth_scheme': 'new_console_api_key',
    'wire_format': 'v3_event_envelope',
    'request_fields': list(VERIFIED_REQUEST_FIELDS),
    'response_fields': list(VERIFIED_RESPONSE_FIELDS),
    'media_format': TTS_FORMAT,
    'real_cloud_call': 'verified_2026-09-20',
}

# ------------------------------------------------------------ binary protocol

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001

MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_AUDIO_ONLY_RESPONSE = 0b1011
MSG_ERROR = 0b1111

FLAG_NONE = 0b0000
FLAG_WITH_EVENT = 0b0100

SERIALIZATION_NONE = 0b0000
SERIALIZATION_JSON = 0b0001
COMPRESSION_NONE = 0b0000

EVENT_NONE = 0
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_START_SESSION = 100
EVENT_CANCEL_SESSION = 101
EVENT_FINISH_SESSION = 102
EVENT_SESSION_STARTED = 150
EVENT_SESSION_CANCELED = 151
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_TASK_REQUEST = 200
EVENT_TTSSentence_START = 350
EVENT_TTSSentence_END = 351
EVENT_TTS_RESPONSE = 352

NAMESPACE_BIDIRECTIONAL_TTS = 'BidirectionalTTS'

# Documented V3 TTS application error codes (see docs/28-active-tts.md §7).
CODE_SUCCESS = 0
CODE_INVALID_REQUEST = 45000001
CODE_QUOTA_EXCEEDED = 45000002
CODE_RESOURCE_MISMATCH = 45000003
CODE_UNAUTHORIZED = 45000010
CODE_TTS_NO_AUDIO = 45000100
CODE_SERVER_BUSY = 55000031
CODE_SERVICE_INTERNAL = 55000000

CODE_ERROR_CATEGORY = {
    CODE_INVALID_REQUEST: 'invalid_request',
    CODE_QUOTA_EXCEEDED: 'provider_quota_exceeded',
    CODE_RESOURCE_MISMATCH: 'resource_mismatch',
    CODE_UNAUTHORIZED: 'provider_auth_failed',
    CODE_TTS_NO_AUDIO: 'provider_no_audio',
    CODE_SERVER_BUSY: 'provider_rate_limited',
    CODE_SERVICE_INTERNAL: 'provider_error',
}

# First-class TTS lifecycle failure categories. The vocabulary lives in the
# vendor-neutral boundary so the marker parser and the audit mapping cannot
# be read against a different list; this name is re-exported for importers.
from .streaming_tts import TTS_ERROR_CATEGORIES  # noqa: F401


class VolcengineTTSProtocolError(RuntimeError):
    """A frame cannot be parsed as the documented V3 TTS protocol."""




def provider_category_from_text(message):
    """The documented category for a provider failure message.

    The message is external input, so the documented code is read as a whole
    integer token and compared exactly. A substring test would read
    ``145000003`` as ``45000003`` and mis-report a quota problem as a resource
    mismatch.
    """
    for token in re.findall(r'\d+', str(message or '')):
        try:
            code = int(token)
        except ValueError:  # pragma: no cover - re guarantees digits
            continue
        if code in CODE_ERROR_CATEGORY:
            return CODE_ERROR_CATEGORY[code]
    return 'provider_error'


# ---------------------------------------------------------------------- codec

def build_header(message_type, flags, serialization=SERIALIZATION_JSON,
                 compression=COMPRESSION_NONE):
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def build_event_frame(event, *, session_id=None, payload=b'',
                      message_type=MSG_FULL_CLIENT_REQUEST, flags=FLAG_WITH_EVENT,
                      serialization=SERIALIZATION_JSON):
    """One documented client frame: header, event, optional session, payload."""
    frame = bytearray(build_header(message_type, flags, serialization, COMPRESSION_NONE))
    frame.extend(struct.pack('>i', event))
    if session_id is not None:
        encoded = session_id.encode('utf-8')
        frame.extend(struct.pack('>i', len(encoded)))
        frame.extend(encoded)
    frame.extend(struct.pack('>i', len(payload)))
    frame.extend(payload)
    return bytes(frame)


def build_full_client_request(payload):
    """Build the V3 unidirectional one-shot request frame.

    Unlike bidirectional streaming, this endpoint does not start an event
    session.  Its documented body is sent as one JSON ``FullClientRequest``.
    """
    return (build_header(MSG_FULL_CLIENT_REQUEST, FLAG_NONE,
                         SERIALIZATION_JSON, COMPRESSION_NONE)
            + struct.pack('>I', len(payload)) + payload)


def build_unidirectional_request_payload(*, text, speaker, audio_params,
                                         model=None, additions=None):
    """Documented one-shot body for the V3 unidirectional endpoint."""
    req_params = {
        'text': text,
        'speaker': speaker,
        'audio_params': dict(audio_params),
    }
    # The current documentation requires this only for cloned voices.  Sending
    # it for ordinary Seed TTS voices would turn a documented default into an
    # account-specific compatibility assumption.
    if model:
        req_params['model'] = model
    if additions:
        req_params['additions'] = json.dumps(additions, ensure_ascii=False,
                                              separators=(',', ':'))
    return json.dumps({'req_params': req_params}, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def read_length_prefixed(data, offset):
    """Read one documented ``[i32 length][bytes]`` field."""
    if len(data) < offset + 4:
        raise VolcengineTTSProtocolError('Truncated length field')
    size = struct.unpack('>i', data[offset:offset + 4])[0]
    if size < 0:
        # A negative size is the documented "no value" marker.
        return b'', offset + 4
    if len(data) < offset + 4 + size:
        raise VolcengineTTSProtocolError('Truncated length-delimited field')
    return data[offset + 4:offset + 4 + size], offset + 4 + size


def parse_response(data):
    """Parse one documented V3 TTS frame, in either direction.

    Parsing the client's own bytes with the same implementation the server
    frames use keeps the tests honest: they decode what was really put on the
    wire instead of a private reimplementation.
    """
    if len(data) < 4:
        raise VolcengineTTSProtocolError('Frame shorter than the documented header')
    header_size = (data[0] & 0x0F) * 4
    if header_size < 4 or len(data) < header_size:
        raise VolcengineTTSProtocolError('Unsupported header size')
    message_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F
    compression = data[2] & 0x0F

    frame = {
        'protocol_version': (data[0] >> 4) & 0x0F,
        'header_size': header_size,
        'message_type': message_type,
        'flags': flags,
        'serialization': serialization,
        'compression': compression,
        'event': EVENT_NONE,
        'session_id': None,
        'connect_id': None,
        'payload': b'',
        'error_code': None,
        'error_message': None,
    }

    if message_type == MSG_ERROR:
        offset = header_size
        if len(data) < offset + 4:
            raise VolcengineTTSProtocolError('Truncated error frame')
        frame['error_code'] = struct.unpack('>i', data[offset:offset + 4])[0]
        offset += 4
        body, _ = read_length_prefixed(data, offset)
        frame['error_message'] = body.decode('utf-8', 'replace')
        return frame

    if message_type not in (MSG_FULL_CLIENT_REQUEST, MSG_FULL_SERVER_RESPONSE,
                            MSG_AUDIO_ONLY_RESPONSE):
        raise VolcengineTTSProtocolError(f'Unexpected message type {message_type:#06b}')

    offset = header_size
    if flags & FLAG_WITH_EVENT:
        if len(data) < offset + 4:
            raise VolcengineTTSProtocolError('Truncated event field')
        frame['event'] = struct.unpack('>i', data[offset:offset + 4])[0]
        offset += 4
    if message_type == MSG_FULL_CLIENT_REQUEST:
        if frame['event'] in (EVENT_START_SESSION, EVENT_TASK_REQUEST,
                              EVENT_FINISH_SESSION, EVENT_CANCEL_SESSION):
            body, offset = read_length_prefixed(data, offset)
            frame['session_id'] = body.decode('utf-8') if body else None
        body, offset = read_length_prefixed(data, offset)
        frame['payload'] = body
    elif message_type == MSG_AUDIO_ONLY_RESPONSE:
        # Documented audio-only layout: [sid_len][sid][payload_len][audio].
        # The session field is always present for an evented frame, so it is
        # always consumed — an empty session id must not shift the payload.
        if flags & FLAG_WITH_EVENT:
            body, offset = read_length_prefixed(data, offset)
            frame['session_id'] = body.decode('utf-8') if body else None
        body, offset = read_length_prefixed(data, offset)
        frame['payload'] = body
    elif message_type == MSG_FULL_SERVER_RESPONSE:
        # Documented full-server-response layouts depend on the event class; the
        # optional session field is present exactly when the event class has it.
        if not flags & FLAG_WITH_EVENT:
            body, _ = read_length_prefixed(data, offset)
            frame['payload'] = body
            return frame
        if frame['event'] in (EVENT_CONNECTION_STARTED, EVENT_CONNECTION_FAILED):
            # Connection events carry a connection id, not a session id:
            # [connect_id_len][connect_id][payload_len][payload].
            body, offset = read_length_prefixed(data, offset)
            frame['connect_id'] = body.decode('utf-8') if body else None
            body, offset = read_length_prefixed(data, offset)
            if body:
                frame['error_message'] = body.decode('utf-8', 'replace')
            return frame
        if frame['event'] in (EVENT_SESSION_STARTED, EVENT_SESSION_FINISHED,
                              EVENT_SESSION_FAILED, EVENT_SESSION_CANCELED):
            # [sid_len][sid] and, when the provider reports a reason, a trailing
            # [error_len][error]. SessionStarted/Finished usually stop here, so a
            # missing trailing field is normal rather than a truncated frame.
            body, offset = read_length_prefixed(data, offset)
            frame['session_id'] = body.decode('utf-8') if body else None
            if len(data) > offset:
                body, offset = read_length_prefixed(data, offset)
                if body:
                    frame['error_message'] = body.decode('utf-8', 'replace')
            return frame
        # [sid_len][sid][payload_len][payload] — TTS sentence and response events.
        if frame['event']:
            body, offset = read_length_prefixed(data, offset)
            frame['session_id'] = body.decode('utf-8') if body else None
        body, offset = read_length_prefixed(data, offset)
        frame['payload'] = body
    return frame


def build_request_payload(*, event, uid, text='', speaker, audio_params, additions=None):
    """The documented V3 TTS request body (only verified fields)."""
    return json.dumps({
        'user': {'uid': uid},
        'event': event,
        'namespace': NAMESPACE_BIDIRECTIONAL_TTS,
        'req_params': {
            'text': text,
            'speaker': speaker,
            'audio_params': dict(audio_params),
            'additions': json.dumps(additions or {}, ensure_ascii=False,
                                    separators=(',', ':')),
        },
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


# ------------------------------------------------------------- MP3 validation

_MPEG_VERSIONS = {0b00: 2, 0b10: 2, 0b11: 1}
_MPEG_LAYERS = {0b01: 3, 0b10: 2, 0b11: 1}
_BITRATE_TABLES = {
    # (version_group, layer) -> kbps, index 0..15
    ('1', 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    ('1', 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    ('1', 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    ('2', 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    ('2', 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    ('2', 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_SAMPLE_RATES = {
    '1': (44100, 48000, 32000),  # MPEG 1
    '2': (22050, 24000, 16000),  # MPEG 2
    '25': (11025, 12000, 8000),  # MPEG 2.5
}
_SAMPLES_PER_FRAME = {('1', 1): 384, ('1', 2): 1152, ('1', 3): 1152,
                      ('2', 1): 384, ('2', 2): 1152, ('2', 3): 576}


class MP3ValidationError(ProviderFailure):
    """The provider bytes are not a decodable MP3 stream."""


def parse_mp3_frame_header(data, offset=0):
    """Parse one MPEG audio frame header; ``None`` when it is not one."""
    if len(data) < offset + 4:
        return None
    b0, b1, b2, b3 = data[offset], data[offset + 1], data[offset + 2], data[offset + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_bits = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    if version_bits == 0b01 or layer_bits == 0b00:
        return None
    layer = _MPEG_LAYERS[layer_bits]
    version = _MPEG_VERSIONS[version_bits]
    group = '1' if version == 1 else '2'
    sample_table = '1' if version == 1 else ('2' if version == 2 else '25')
    bitrate_index = (b2 >> 4) & 0x0F
    sample_rate_index = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if bitrate_index in (0, 15) or sample_rate_index == 3:
        return None
    bitrate = _BITRATE_TABLES[(group, layer)][bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[sample_table][sample_rate_index]
    channels = 1 if ((b3 >> 6) & 0x03) == 0b11 else 2
    if layer == 1:
        frame_length = (12 * bitrate // sample_rate + padding) * 4
    else:
        frame_length = (_SAMPLES_PER_FRAME[(group, layer)] // 8 * bitrate // sample_rate
                        + padding)
    return {
        'mpeg_version': version,
        'layer': layer,
        'bitrate_bps': bitrate,
        'sample_rate': sample_rate,
        'channels': channels,
        'frame_length': frame_length,
        'samples_per_frame': _SAMPLES_PER_FRAME[(group, layer)],
    }


def parse_mp3_metadata(data):
    """Scan an MP3 stream and report frame count / sample metadata.

    Scanning rather than trusting one header matters: a truncated or interlaced
    stream that only looks like MP3 at the first frame must be refused instead of
    frozen as a Stimulus Artifact. A leading ID3v2 tag is skipped.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise MP3ValidationError('TTS returned no audio bytes')
    offset = 0
    if data[:3] == b'ID3' and len(data) >= 10:
        size = ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14
                | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
        offset = 10 + size
    first = None
    frames = 0
    samples = 0
    while offset < len(data):
        header = parse_mp3_frame_header(data, offset)
        if header is None:
            if frames == 0:
                raise MP3ValidationError('TTS response is not a valid MP3 asset')
            # Trailing non-frame bytes after at least one good frame: stop counting
            # rather than claim the remainder is audio.
            break
        if header['frame_length'] <= 0:
            raise MP3ValidationError('TTS response contains an empty MP3 frame')
        if frames == 0:
            first = header
        elif header['sample_rate'] != first['sample_rate'] or \
                header['channels'] != first['channels']:
            raise MP3ValidationError('TTS response changes MP3 format mid-stream')
        frames += 1
        samples += header['samples_per_frame']
        offset += header['frame_length']
    if frames == 0 or first is None:
        raise MP3ValidationError('TTS response is not a valid MP3 asset')
    duration_ms = round(samples / first['sample_rate'] * 1000, 3)
    return {
        'format': TTS_FORMAT,
        'sample_rate': first['sample_rate'],
        'channels': first['channels'],
        'bitrate_bps': first['bitrate_bps'],
        'mpeg_version': first['mpeg_version'],
        'layer': first['layer'],
        'frames': frames,
        'samples': samples,
        'duration_ms': duration_ms,
        'size_bytes': len(data),
    }


# -------------------------------------------------------------------- helpers

def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _sha256(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _classify_open_failure(error):
    text = f'{type(error).__name__} {error}'.lower()
    if any(term in text for term in ('401', '403', 'unauthor', 'forbidden', 'invalidstatus')):
        return 'provider_auth_failed'
    if any(term in text for term in ('429', 'rate', 'busy')):
        return 'provider_rate_limited'
    if 'timeout' in text:
        return 'stream_timeout'
    return 'stream_open_failed'


def _close_code_of(error):
    for candidate in (getattr(error, 'code', None),
                      getattr(getattr(error, 'rcvd', None), 'code', None),
                      getattr(getattr(error, 'sent', None), 'code', None)):
        if isinstance(candidate, int):
            return candidate
    return None


class _RealConnection:
    def __init__(self, socket):
        self._socket = socket

    async def send(self, data):
        await self._socket.send(data)

    async def recv(self):
        return await self._socket.recv()

    async def close(self):
        await self._socket.close()


class TTSWebSocketTransport:
    """Default transport: a real ``websockets`` client connection."""

    def __init__(self, *, open_timeout=15, close_timeout=5, max_size=32 * 1024 * 1024):
        self.open_timeout = open_timeout
        self.close_timeout = close_timeout
        self.max_size = max_size
        # Production transports read frames on the session's background task.
        self.passive = False

    async def connect(self, url, headers):
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:  # pragma: no cover - dependency guard
            raise ProviderFailure(
                'The websockets client is required for Volcengine V3 TTS') from error
        import inspect
        parameters = set(inspect.signature(connect).parameters)
        header_kw = ('additional_headers' if 'additional_headers' in parameters
                     else 'extra_headers')
        socket = await connect(url, **{header_kw: headers}, open_timeout=self.open_timeout,
                               close_timeout=self.close_timeout, max_size=self.max_size)
        return _RealConnection(socket)


# ---------------------------------------------------- bidirectional lifecycle

SESSION_CREATED = 'created'
SESSION_OPEN = 'open'
SESSION_STREAMING = 'streaming'
SESSION_FINISHING = 'finishing'
SESSION_FINISHED = 'finished'
SESSION_CANCELLED = 'cancelled'
SESSION_FAILED = 'failed'

# A late audio frame is kept as an explicitly labelled diagnostic, bounded so a
# long conversation cannot grow without limit. It is never written to the turn's
# audio file and never becomes playable output.
MAX_STALE_AUDIO_CHUNKS = 20


class VolcengineBidirectionalTTSSession:
    """One V3 bidirectional TTS session bound to one Run/Turn (PRD-F021).

    Lifecycle::

        open() -> append_text(chunk)* -> finish() -> close()
                                      \\-> cancel(reason)

    The session owns its own audio staging file. Once it is cancelled — user
    Stop, a stale turn, a run stop or a provider failure — every later audio
    frame is dropped and counted as a diagnostic. That is the mechanism that
    stops late audio from a previous turn leaking into the next one; it is
    enforced here (transport level) rather than by the caller remembering to.
    """

    def __init__(self, *, stream_id, root, key, resource_id, request_endpoint,
                 audit_endpoint, speaker, model='tts', transport=None,
                 sample_rate=24000, speech_rate=0, loudness_rate=0,
                 timeout=60, context=None, transport_name=TRANSPORT_BIDIRECTIONAL,
                 audit_root=None):
        self.stream_id = stream_id
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_root = Path(audit_root) if audit_root is not None else self.root
        self.key = key
        self.resource_id = resource_id
        self.request_endpoint = request_endpoint
        self.audit_endpoint = audit_endpoint
        self.speaker = speaker
        self.model = model
        self.transport = transport or TTSWebSocketTransport()
        self.timeout = timeout
        self.context = dict(context or {})
        self.transport_name = transport_name
        self.session_id = uuid.uuid4().hex
        self.audio_path = self.root / f'{stream_id}.mp3'
        self.events_path = self.root / f'{stream_id}-tts-events.json'
        self.request_path = self.root / f'{stream_id}-tts-request.json'
        self.config = {
            'provider': 'volcengine',
            'transport': transport_name,
            'endpoint': audit_endpoint,
            'resource_id': resource_id,
            'speaker': speaker,
            'model': model,
            'format': TTS_FORMAT,
            'sample_rate': sample_rate,
            'speech_rate': speech_rate,
            'loudness_rate': loudness_rate,
            'timeout_seconds': timeout,
            'auth_scheme': 'new_console_api_key',
            'capabilities': dict(CAPABILITY_MATRIX[transport_name]),
        }
        self._history = []
        self._audio_chunks = []
        self._audio_bytes = 0
        self._connection = None
        self._reader = None
        self._state = SESSION_CREATED
        self._failure = None
        self._terminated = False
        self._saw_session_finished = False
        self._sentence_started = 0
        self._sentences_finished = 0
        self._text_chunks = []
        self._stale_chunks = 0
        self._stale_bytes = 0
        self._cancel_reason = None
        self._close_code = None
        self._finalized = False
        self._audit = None
        self._lock = asyncio.Lock()
        self._connection_started = False

    # ------------------------------------------------------------- lifecycle

    async def open(self):
        immutable_json(self.request_path, {
            'schema_version': '1.0.0', 'stream_id': self.stream_id,
            'session_id': self.session_id, 'endpoint': self.audit_endpoint,
            'transport': self.transport_name,
            'interface_contract': dict(INTERFACE_CONTRACT),
            'config': self.config, 'context': self.context,
            'note': 'Secret-free request descriptor; credentials are never recorded'})
        self._audit = InvocationAudit(
            self.audit_root, ProviderIdentity('volcengine', self.model,
                                              f'{self.transport_name}:1.0.0',
                                              self.audit_endpoint, 'v3'),
            self.config, self._closed_config(), [self.request_path],
            operation_id=f'OP-{self.stream_id}', attempt=1)
        if not self.key:
            self._failure = 'credential_missing'
            self._record('tts_error', error='credential_missing')
            self._finish_audit('failed', failure_code='credential_missing')
            raise ProviderFailure('TTS credential missing')
        headers = {
            'X-Api-Key': self.key,
            'X-Api-Resource-Id': self.resource_id,
            'X-Api-Connect-Id': str(uuid.uuid4()),
        }
        try:
            self._connection = await self.transport.connect(self.request_endpoint, headers)
        except Exception as error:  # noqa: BLE001 - one explicit category
            category = _classify_open_failure(error)
            self._failure = category
            self._record('tts_error', error=category,
                         detail={'phase': 'connect', 'error_type': type(error).__name__})
            self._finish_audit('failed', failure_code=category)
            raise ProviderFailure(f'TTS session could not be opened ({category})') from None
        self._state = SESSION_OPEN
        self._record('tts_session_started', detail={'endpoint': self.audit_endpoint,
                                                    'resource_id': self.resource_id,
                                                    'transport': self.transport_name})
        request_sent = False
        try:
            # The V3 bidirectional endpoint is connection-scoped.  A Session is
            # invalid until the provider acknowledges StartConnection.
            await self._connection.send(build_event_frame(
                EVENT_START_CONNECTION, payload=b'{}'))
            raw = await asyncio.wait_for(self._connection.recv(), timeout=self.timeout)
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
            frame = parse_response(bytes(raw))
            if frame['message_type'] == MSG_ERROR:
                category = CODE_ERROR_CATEGORY.get(frame['error_code'], 'provider_error')
                raise ProviderFailure(f'TTS connection was rejected ({category})')
            if frame['event'] == EVENT_CONNECTION_FAILED:
                raise ProviderFailure('TTS connection was rejected (stream_open_failed)')
            if frame['event'] != EVENT_CONNECTION_STARTED:
                raise ProviderFailure('TTS connection did not acknowledge StartConnection')
            self._connection_started = True
            self._record('tts_connection_started', detail={
                'event': frame['event'], 'connect_id': frame.get('connect_id')})
            await self._connection.send(build_event_frame(
                EVENT_START_SESSION, session_id=self.session_id,
                payload=build_request_payload(
                    event=EVENT_START_SESSION, uid='aivoicebench', text='',
                    speaker=self.speaker, audio_params=self._audio_params(),
                    additions=self._additions())))
            request_sent = True
            if not getattr(self.transport, 'passive', False):
                # A passive transport is used by tests that hand frames to
                # ``_handle_frame`` themselves; production transports always
                # read on a background task.
                self._reader = asyncio.create_task(self._read_loop())
        except Exception as error:  # noqa: BLE001
            # A handshake or initialise failure is classified the same way any
            # other open failure is; collapsing auth/rate-limit/timeout into one
            # category would hide why the session never started.
            category = _classify_open_failure(error)
            self._failure = category
            self._record('tts_error', error=category,
                         detail={'phase': 'initialise', 'error_type': type(error).__name__})
            await self.cancel(reason='session_initialise_failed')
            raise ProviderFailure(
                f'TTS session could not be initialised ({category})') from None
        return self

    def _audio_params(self):
        return {
            'format': TTS_FORMAT,
            'sample_rate': self.config['sample_rate'],
            'speech_rate': self.config['speech_rate'],
            'loudness_rate': self.config['loudness_rate'],
        }

    def _additions(self):
        # Pitch is not advertised for the unidirectional transport; only send it
        # where the capability matrix says the protocol supports it.
        additions = {}
        if CAPABILITY_MATRIX[self.transport_name].get('pitch'):
            additions['post_process'] = {'pitch': 0}
        return additions

    # ------------------------------------------------------------- text input

    async def append_text(self, text):
        """Append one ordered speakable chunk. Caller keeps chunk ordering.

        An empty/whitespace chunk is refused rather than forwarded: the provider
        would synthesise nothing for it and the turn's text evidence would then
        claim a chunk that never existed.
        """
        if self._state in (SESSION_CANCELLED, SESSION_FINISHED, SESSION_FAILED):
            raise ProviderFailure('TTS session is no longer accepting text')
        if not isinstance(text, str) or not text.strip():
            raise ProviderFailure('TTS text chunk is empty')
        if self._state == SESSION_CREATED:
            raise ProviderFailure('TTS session is not open')
        self._text_chunks.append(text)
        self._state = SESSION_STREAMING
        try:
            await self._connection.send(build_event_frame(
                EVENT_TASK_REQUEST, session_id=self.session_id,
                payload=build_request_payload(
                    event=EVENT_TASK_REQUEST, uid='aivoicebench', text=text,
                    speaker=self.speaker, audio_params=self._audio_params(),
                    additions=self._additions())))
        except Exception as error:  # noqa: BLE001
            self._failure = self._failure or 'stream_disconnected'
            self._record('tts_error', error='stream_disconnected',
                         detail={'phase': 'append_text', 'error_type': type(error).__name__})
            raise ProviderFailure('TTS text chunk could not be sent') from None
        self._record('tts_text_chunk', text=text)

    async def finish_input(self):
        """No more text for this session; ask the provider to flush."""
        if self._state in (SESSION_CANCELLED, SESSION_FINISHED, SESSION_FAILED):
            return
        if self._terminated or self._saw_session_finished:
            self._state = SESSION_FINISHED
            return
        if self._state not in (SESSION_OPEN, SESSION_STREAMING, SESSION_FINISHING):
            return
        try:
            await self._connection.send(build_event_frame(
                EVENT_FINISH_SESSION, session_id=self.session_id, payload=b'{}'))
            self._state = SESSION_FINISHING
        except Exception as error:  # noqa: BLE001
            self._failure = self._failure or 'stream_disconnected'
            self._record('tts_error', error='stream_disconnected',
                         detail={'phase': 'finish_input',
                                 'error_type': type(error).__name__})

    # ------------------------------------------------------------ audio drain

    async def drain(self, *, timeout=None):
        """Wait until the provider ends the session or the timeout expires.

        Returns ``True`` when the provider reached a terminal event. The caller
        can then read :meth:`audio_bytes`; the staging file is complete.
        """
        deadline = self.timeout if timeout is None else timeout
        started = time.monotonic()
        while not self._terminated and self._state not in (SESSION_CANCELLED, SESSION_FAILED):
            remaining = deadline - (time.monotonic() - started)
            if remaining <= 0:
                self._failure = self._failure or 'stream_timeout'
                self._record('tts_error', error='stream_timeout',
                             detail={'phase': 'drain'})
                return False
            await asyncio.sleep(min(0.05, max(remaining, 0.001)))
        return self._terminated

    # ------------------------------------------------------------------ state

    @property
    def state(self):
        return self._state

    @property
    def failure(self):
        return self._failure

    @property
    def turn_id(self):
        return self.context.get('turn_id')

    @property
    def cancelled(self):
        return self._state == SESSION_CANCELLED

    @property
    def stale_audio_chunks(self):
        """How many late audio frames were dropped because the session ended."""
        return self._stale_chunks

    @property
    def stale_audio_bytes(self):
        return self._stale_bytes

    @property
    def audio_bytes(self):
        return self._audio_bytes

    @property
    def text(self):
        return ''.join(self._text_chunks)

    @property
    def terminated(self):
        return self._terminated

    def history(self):
        return list(self._history)

    def summary(self):
        return {
            'stream_id': self.stream_id,
            'session_id': self.session_id,
            'transport': self.transport_name,
            'endpoint': self.audit_endpoint,
            'resource_id': self.resource_id,
            'speaker': self.speaker,
            'format': TTS_FORMAT,
            'sample_rate': self.config['sample_rate'],
            'state': self._state,
            'failure': self._failure,
            'cancelled': self.cancelled,
            'cancel_reason': self._cancel_reason,
            'text_chunks': len(self._text_chunks),
            'sentence_starts': self._sentence_started,
            'sentences_finished': self._sentences_finished,
            'audio_chunks': len(self._audio_chunks),
            'audio_bytes': self._audio_bytes,
            'stale_audio_chunks': self._stale_chunks,
            'stale_audio_bytes': self._stale_bytes,
            'saw_session_finished': self._saw_session_finished,
            'terminated': self._terminated,
            'close_code': self._close_code,
            'evidence_scope': 'control_evidence',
            # Persisted, not merely implied: a streaming failure must be reported
            # as a failure of this transport (Issue #98 §3).
            'fallback_policy': fallback_policy_record(),
            'audio_file': self.audio_artifact_name(),
        }

    # --------------------------------------------------------------- protocol

    def _record(self, kind, **fields):
        entry = {'seq': len(self._history) + 1, 'at': _utc_now(), 'kind': kind}
        entry.update(fields)
        self._history.append(entry)
        return entry

    async def _read_loop(self):
        try:
            while True:
                raw = await self._connection.recv()
                if isinstance(raw, str):
                    raw = raw.encode('utf-8')
                frame = parse_response(bytes(raw))
                self._handle_frame(frame)
                if self._terminated:
                    return
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            raise
        except Exception as error:  # noqa: BLE001
            if self._state in (SESSION_CANCELLED, SESSION_FINISHED):
                return
            code = _close_code_of(error)
            if self._close_code is None:
                self._close_code = code
            text = f'{type(error).__name__} {error}'.lower()
            category = 'stream_timeout' if 'timeout' in text else 'stream_disconnected'
            self._failure = self._failure or category
            self._record('tts_error', error=category,
                         detail={'phase': 'read', 'close_code': code,
                                 'error_type': type(error).__name__})

    def _handle_frame(self, frame):
        if frame['message_type'] == MSG_ERROR:
            category = CODE_ERROR_CATEGORY.get(frame['error_code'], 'provider_error')
            self._failure = self._failure or category
            self._record('tts_error', error=category,
                         detail={'phase': 'protocol', 'provider_code': frame['error_code'],
                                 'provider_message': (frame['error_message'] or '')[:200]})
            self._terminated = True
            self._state = SESSION_FAILED
            return
        event = frame['event']
        if event == EVENT_CONNECTION_FAILED:
            self._failure = self._failure or 'stream_open_failed'
            self._record('tts_error', error='stream_open_failed',
                         detail={'phase': 'connection', 'event': event})
            self._terminated = True
            self._state = SESSION_FAILED
            return
        if event == EVENT_SESSION_FAILED:
            # The provider reports the failure text in the frame's own field.
            detail_text = str(frame['error_message'] or '')
            category = provider_category_from_text(detail_text)
            self._failure = self._failure or category
            self._record('tts_error', error=category,
                         detail={'phase': 'session',
                                 'provider_message': detail_text[:200]})
            self._terminated = True
            self._state = SESSION_FAILED
            return
        if event == EVENT_TTSSentence_START:
            self._sentence_started += 1
            self._record('tts_sentence_started',
                         detail={'sentence_index': self._sentence_started})
            return
        if event == EVENT_TTSSentence_END:
            self._sentences_finished += 1
            self._record('tts_sentence_ended',
                         detail={'sentence_index': self._sentences_finished})
            return
        if event == EVENT_TTS_RESPONSE:
            self._handle_audio(frame)
            return
        if event == EVENT_SESSION_FINISHED:
            self._saw_session_finished = True
            self._terminated = True
            self._state = SESSION_FINISHED
            self._record('tts_session_finished', detail={'audio_bytes': self._audio_bytes})
            return
        if event == EVENT_SESSION_CANCELED:
            self._terminated = True
            self._state = SESSION_CANCELLED
            self._record('tts_session_canceled', detail={})
            return
        # Connection/session bookkeeping events carry no audio; keep them visible.
        self._record('tts_event', detail={'event': event})

    def _handle_audio(self, frame):
        payload = frame['payload'] or b''
        if self._state in (SESSION_CANCELLED, SESSION_FAILED, SESSION_FINISHED) \
                or self._terminated:
            # Late audio after Stop/cancel/turn change: diagnostics only, and it
            # never reaches the turn's audio bytes.
            if payload:
                self._stale_chunks += 1
                self._stale_bytes += len(payload)
                self._record('tts_stale_audio', detail={'bytes': len(payload),
                                                        'reason': self._cancel_reason or
                                                                  self._state})
            return
        if not payload:
            return
        self._audio_chunks.append(payload)
        self._audio_bytes += len(payload)
        self._record('tts_audio_chunk', detail={'bytes': len(payload),
                                                'index': len(self._audio_chunks)})

    # ------------------------------------------------------------------ close

    async def cancel(self, reason='client_cancelled'):
        """Stop this session; every later audio frame is dropped."""
        await self._shutdown(reason, cancelled=True)

    async def close(self, reason='client_finished'):
        await self._shutdown(reason, cancelled=False)

    async def _shutdown(self, reason, *, cancelled):
        if self._finalized:
            # The session already reached a terminal state. Neither a repeated
            # cancel nor a later close may relabel the evidence or resurrect the
            # audio: the first terminal outcome is the recorded one.
            return
        async with self._lock:
            self._finalized = True
            if cancelled:
                self._state = SESSION_CANCELLED
                self._cancel_reason = reason
                self._terminated = True
            elif self._state not in (SESSION_FINISHED, SESSION_FAILED):
                self._state = SESSION_FINISHED
                self._terminated = True
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self._reader = None
            if self._connection is not None:
                try:
                    if self._connection_started:
                        await self._connection.send(build_event_frame(
                            EVENT_FINISH_CONNECTION, payload=b'{}'))
                    await self._connection.close()
                except Exception:  # noqa: BLE001
                    pass
            self._record('tts_session_closed', detail={
                'cancelled': bool(cancelled), 'reason': reason,
                'audio_bytes': self._audio_bytes, 'failure': self._failure,
                'stale_audio_chunks': self._stale_chunks})
            status, failure_code = self._terminal_status(cancelled)
            if self._write_audio_file() is False:
                # The bytes exist only in memory. Reporting a completed synthesis
                # whose audio never reached disk would fabricate an artifact, so
                # the outcome is demoted and the reason is recorded.
                self._failure = self._failure or 'response_invalid'
                self._record('tts_error', error='response_invalid',
                             detail={'phase': 'audio_write',
                                     'note': 'session audio could not be written to disk'})
                status, failure_code = 'failed', 'response_invalid'
            self._finish_audit(status, failure_code=failure_code)

    def audio_artifact_name(self):
        """The file name the session audio was (or would be) written to."""
        return self.audio_path.name

    def _write_audio_file(self):
        """Materialise the audio this session really owns (never stale frames).

        Returns ``True`` when audio was written, ``False`` when audio existed but
        could not be written, and ``None`` when there was nothing to write. A
        cancelled or failed session writes to a ``.partial`` name so a truncated
        capture can never be mistaken for a completed turn asset.
        """
        if not self._audio_chunks:
            return None
        complete = self._state == SESSION_FINISHED and self._failure is None
        if not complete:
            self.audio_path = self.audio_path.with_suffix('.partial.mp3')
        try:
            self.audio_path.write_bytes(b''.join(self._audio_chunks))
        except OSError:
            return False
        return True

    def _terminal_status(self, cancelled):
        if cancelled and self._failure is None:
            return 'partial', None
        if self._failure in ('provider_auth_failed', 'provider_rate_limited',
                             'stream_open_failed', 'stream_disconnected'):
            return 'failed', 'unavailable'
        if self._failure == 'stream_timeout':
            return 'failed', 'timeout'
        if self._failure in ('credential_missing', 'configuration_invalid',
                             'invalid_request', 'unsupported_parameter',
                             'resource_mismatch'):
            return 'failed', 'configuration_invalid'
        if self._failure in ('provider_no_audio', 'response_invalid'):
            return 'insufficient_evidence', None
        if self._failure:
            return 'failed', 'provider_error'
        if self._saw_session_finished and self._audio_bytes:
            return 'complete', None
        return 'insufficient_evidence', None

    def _closed_config(self):
        return {'type': 'object', 'additionalProperties': False,
                'required': list(self.config),
                'properties': {key: {'const': value} for key, value in self.config.items()}}

    def _finish_audit(self, status, failure_code=None):
        if self._audit is None or self._audit.finished:
            return
        immutable_json(self.events_path, {
            'schema_version': '1.0.0', 'stream_id': self.stream_id,
            'summary': self.summary(), 'events': list(self._history)})
        outputs = [self.events_path]
        # A partial/failed capture is recorded as an output artifact under its own
        # distinguishable name (``.partial.mp3``), never as the turn's final asset.
        if self.audio_path.is_file() and self.audio_path.stat().st_size:
            outputs.append(self.audio_path)
        try:
            self._audit.finish(status, outputs, failure_code=failure_code)
        except Exception:  # noqa: BLE001 - audit failure must not mask the outcome
            pass


# ------------------------------------------- unidirectional (Fixed synthesis)

class VolcengineUnidirectionalTTSProvider:
    """V3 unidirectional WebSocket TTS: complete text in, MP3 asset out.

    ``synthesize()`` keeps the existing synchronous ``TTSProvider`` shape used by
    the Fixed Case Runner, so the runner can freeze a stimulus without knowing
    that the transport is a WebSocket session. The asset it returns is validated
    MP3: there is no PCM/WAV encapsulation step (Issue #98).
    """

    tts_version = '3.0.0'
    transport_name = TRANSPORT_UNIDIRECTIONAL

    def __init__(self, root, api_key, *, endpoint=ENDPOINT_UNIDIRECTIONAL,
                 audit_endpoint=AUDIT_ENDPOINT_UNIDIRECTIONAL,
                 model='tts', resource_id='', voice_type='', speech_rate=0,
                 loudness_rate=0, sample_rate=24000, timeout=60, transport=None):
        if resource_id and (not isinstance(resource_id, str) or len(resource_id) > 200):
            raise ProviderFailure('TTS resource_id is invalid')
        self.root = Path(root)
        self.api_key = api_key
        self.endpoint = endpoint
        self.audit_endpoint = audit_endpoint
        self.model = model
        self.resource_id = resource_id
        self.voice_type = voice_type
        self.speech_rate = speech_rate
        self.loudness_rate = loudness_rate
        self.sample_rate = sample_rate
        self.timeout = timeout
        self.transport = transport or TTSWebSocketTransport()
        self.audio_format = TTS_FORMAT
        self.profile = {
            'provider': 'volcengine',
            'model_id': model,
            'model_version': 'service-managed',
            'library_version': f'aivoicebench-volcengine-tts-ws:{self.tts_version}',
            'api_version': 'v3_ws_unidirectional',
            'transport': self.transport_name,
            'resource_id': resource_id,
            'voice_type': voice_type,
            'audio_format': self.audio_format,
            'sample_rate': sample_rate,
            'capabilities': dict(CAPABILITY_MATRIX[self.transport_name]),
        }

    # ------------------------------------------------------------ public API

    def validate_text(self, text):
        """Configuration/input validation shared by the CLI, API and tests.

        Raises :class:`ProviderFailure` with a first-class category; it never
        makes a network call, so a rejected request costs nothing.
        """
        if not self.api_key:
            raise ProviderFailure('TTS credential missing')
        if not isinstance(text, str) or not text.strip():
            raise ProviderFailure('TTS text is empty')
        if not self.resource_id:
            raise ProviderFailure('TTS resource_id missing')
        if not self.voice_type:
            raise ProviderFailure('TTS voice missing')
        if self.audio_format != TTS_FORMAT:
            raise ProviderFailure('Active Voice Test requires TTS format=mp3')

    def synthesize(self, text, destination):
        """Synthesize ``text`` into a verified MP3 at ``destination``.

        Synchronous on purpose: the Fixed Case Runner freezes a stimulus in a
        request-scoped flow. The WebSocket lifecycle itself lives in
        :meth:`synthesize_async`.
        """
        destination = Path(destination)
        try:
            return asyncio.run(self.synthesize_async(text, destination))
        except ProviderFailure:
            raise
        except RuntimeError as error:
            raise ProviderFailure(
                'TTS synthesis failed; check configuration and credentials') from error

    async def synthesize_async(self, text, destination):
        destination = Path(destination)
        request_id = str(uuid.uuid4())
        started_at = _utc_now()
        start_time = time.monotonic()
        config = self._public_config()
        failure_code = None
        chunks = []
        try:
            self.validate_text(text)
            session = _UnidirectionalSession(
                provider=self, request_id=request_id, text=text, transport=self.transport)
            audio_data = await session.run()
            metadata = parse_mp3_metadata(audio_data)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(audio_data)
            self._write_audit(destination, request_id, started_at, start_time, config,
                              text, 'complete', audio_data=audio_data,
                              audio_metadata=metadata)
            return {
                'path': str(destination),
                'sha256': _sha256(audio_data),
                'size_bytes': len(audio_data),
                'format': TTS_FORMAT,
                'sample_rate': metadata['sample_rate'],
                'channels': metadata['channels'],
                'frames': metadata['frames'],
                'duration_ms': metadata['duration_ms'],
                'text': text,
                'voice_type': self.voice_type,
                'model': self.model,
                'resource_id': self.resource_id,
                'request_id': request_id,
                'transport': self.transport_name,
                'audio_metadata': metadata,
            }
        except ProviderFailure as error:
            failure_code = failure_code or self._failure_code(error)
            self._write_audit(destination, request_id, started_at, start_time, config,
                              text, 'failed', failure_code=failure_code)
            raise
        except Exception:  # noqa: BLE001 - one safe public error
            self._write_audit(destination, request_id, started_at, start_time, config,
                              text, 'failed', failure_code='provider_error')
            raise ProviderFailure(
                'TTS synthesis failed; check configuration and credentials') from None

    def _public_config(self):
        return {
            'provider': 'volcengine',
            'transport': self.transport_name,
            'endpoint': self.audit_endpoint,
            'api_version': 'v3_ws_unidirectional',
            'model': self.model,
            'resource_id': self.resource_id,
            'voice_type': self.voice_type,
            'audio_format': self.audio_format,
            'sample_rate': self.sample_rate,
            'speech_rate': self.speech_rate,
            'loudness_rate': self.loudness_rate,
            'timeout_seconds': self.timeout,
            'auth_scheme': 'new_console_api_key',
            'capabilities': dict(CAPABILITY_MATRIX[self.transport_name]),
        }

    def _audio_params(self):
        return {
            'format': TTS_FORMAT,
            'sample_rate': self.sample_rate,
            'speech_rate': self.speech_rate,
            'loudness_rate': self.loudness_rate,
        }

    @staticmethod
    def _failure_code(error):
        """Map a raised error to the recorded first-class failure code.

        The synthesis session already decided the category and carries it in a
        trailing ``(category)`` marker, so the recorded code is the one the
        transport really produced rather than a second guess from prose. The
        marker format and the category-to-code mapping live in
        :mod:`aivoicebench.streaming_tts` so no second copy can drift from it.
        """
        message = str(error)
        decided = failure_category_of(error)
        if decided is not None:
            return failure_code_for(decided)
        if 'credential' in message:
            return 'credential_missing'
        if 'resource_id' in message or 'voice missing' in message or 'format=' in message:
            return 'configuration_invalid'
        if 'empty' in message:
            return 'input_invalid'
        if 'MP3' in message or 'audio data' in message or 'no audio' in message:
            return 'response_invalid'
        if 'timeout' in message:
            return failure_code_for('stream_timeout')
        return 'provider_error'

    def _write_audit(self, destination, request_id, started_at, start_time, config, text,
                     status, *, audio_data=None, audio_metadata=None, failure_code=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        audit = {
            'schema_version': '1.2.0',
            'invocation_id': 'CALL-' + request_id,
            'provider': 'volcengine',
            'model': self.model,
            'endpoint': self.audit_endpoint,
            'transport': self.transport_name,
            'interface_contract': dict(INTERFACE_CONTRACT),
            'config': config,
            'started_at': started_at,
            'finished_at': _utc_now(),
            'latency_ms': round((time.monotonic() - start_time) * 1000, 3),
            'status': status,
            'input_text': text[:200] + '...' if len(text) > 200 else text,
        }
        if status == 'complete':
            audit['output'] = {
                'path': destination.name,
                'sha256': _sha256(audio_data),
                'size_bytes': len(audio_data),
                'audio_metadata': audio_metadata,
            }
        else:
            audit['failure_code'] = failure_code or 'provider_error'
        (destination.parent / f'{destination.stem}-tts-audit.json').write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')


class _UnidirectionalSession:
    """One completed V3 unidirectional, complete-text synthesis."""

    def __init__(self, *, provider, request_id, text, transport):
        self.provider = provider
        self.request_id = request_id
        self.text = text
        self.transport = transport
        self.session_id = uuid.uuid4().hex
        self.chunks = []
        self.failure = None
        self.connection = None
        self.finished = False

    async def run(self):
        if not self.provider.api_key:
            raise ProviderFailure('TTS credential missing')
        headers = {
            'X-Api-Key': self.provider.api_key,
            'X-Api-Resource-Id': self.provider.resource_id,
            'X-Api-Request-Id': self.request_id,
        }
        try:
            self.connection = await asyncio.wait_for(
                self.transport.connect(self.provider.endpoint, headers),
                timeout=self.provider.timeout)
        except asyncio.TimeoutError:
            raise ProviderFailure('TTS synthesis timed out while opening the session') from None
        except Exception as error:  # noqa: BLE001
            category = _classify_open_failure(error)
            raise ProviderFailure(f'TTS session could not be opened ({category})') from None
        try:
            cloned_voice = self.provider.resource_id == 'seed-icl-2.0'
            payload = build_unidirectional_request_payload(
                text=self.text, speaker=self.provider.voice_type,
                audio_params=self.provider._audio_params(),
                model=self.provider.model if cloned_voice else None)
            await self.connection.send(build_full_client_request(payload))
        except Exception:  # noqa: BLE001
            raise ProviderFailure('TTS request could not be sent') from None
        await self._read_until_finished()
        if self.failure:
            raise ProviderFailure({
                'provider_error': 'TTS request was rejected by the provider',
                'provider_auth_failed': 'TTS credential was rejected',
                'provider_rate_limited': 'TTS provider is rate limiting',
                'provider_quota_exceeded': 'TTS quota exhausted',
                'resource_mismatch': 'TTS resource id does not match the voice',
                'invalid_request': 'TTS request was rejected as invalid',
                'provider_no_audio': 'TTS response contained no audio data',
                'response_invalid': 'TTS response is not a valid MP3 asset',
                'stream_timeout': 'TTS synthesis timed out',
                'stream_disconnected': 'TTS session was disconnected',
            }.get(self.failure, 'TTS synthesis failed')
                + f' ({self.failure})')
        data = b''.join(self.chunks)
        if not data:
            raise ProviderFailure('TTS response contained no audio data (provider_no_audio)')
        return data

    async def _read_until_finished(self):
        deadline = time.monotonic() + self.provider.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failure = self.failure or 'stream_timeout'
                    return
                try:
                    raw = await asyncio.wait_for(self.connection.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    self.failure = self.failure or 'stream_timeout'
                    return
                if isinstance(raw, str):
                    raw = raw.encode('utf-8')
                if self._handle(parse_response(bytes(raw))):
                    return
        except ProviderFailure:
            raise
        except Exception:  # noqa: BLE001
            # A normal close after a finished session is not a failure.
            if not self.finished:
                self.failure = self.failure or 'stream_disconnected'
        finally:
            try:
                await self.connection.close()
            except Exception:  # noqa: BLE001
                pass

    def _handle(self, frame):
        if frame['message_type'] == MSG_ERROR:
            self.failure = self.failure or CODE_ERROR_CATEGORY.get(
                frame['error_code'], 'provider_error')
            return True
        event = frame['event']
        if event == EVENT_TTS_RESPONSE:
            payload = frame['payload'] or b''
            if payload:
                self.chunks.append(payload)
            return False
        if event in (EVENT_SESSION_FINISHED, EVENT_CONNECTION_FINISHED):
            self.finished = True
            return True
        if event in (EVENT_SESSION_FAILED, EVENT_CONNECTION_FAILED):
            category = provider_category_from_text(frame['error_message'])
            self.failure = self.failure or category
            return True
        return False


class UnavailableTTSProvider:
    """Production default when no TTS provider is configured."""

    def synthesize(self, text, destination):
        raise ProviderFailure(
            'No TTS provider configured; add a volcengine_tts_ws profile in providers.yaml')


# --------------------------------------------------------------------- factory

def build_unidirectional_provider(root, keys, doc, transport=None):
    """Build the Fixed-asset provider factory from a resolved configuration."""
    by_id = {p['id']: p for p in doc['profiles']}
    selected = by_id.get(doc['routes'].get('tts'))
    if not selected or not selected.get('enabled'):
        return None
    if selected['protocol'] != 'volcengine_tts_ws':
        return None
    params = selected['parameters']
    endpoint = selected['base_url'] or ENDPOINT_UNIDIRECTIONAL
    audit_endpoint = _audit_endpoint_for(endpoint)

    def factory(evidence_root, _profile=selected, _params=params, _endpoint=endpoint,
                _audit=audit_endpoint, _transport=transport, _keys=keys):
        return VolcengineUnidirectionalTTSProvider(
            evidence_root, _keys.get(_profile['id'], ''),
            endpoint=_endpoint, audit_endpoint=_audit,
            model=_profile['model'] or 'tts',
            resource_id=_params.get('resource_id', ''),
            voice_type=_params.get('voice', ''),
            speech_rate=_params.get('speed', 0),
            loudness_rate=_params.get('volume', 0),
            sample_rate=_params.get('sample_rate', 24000),
            timeout=_params.get('timeout_seconds', 60),
            transport=_transport)
    return factory


def build_bidirectional_factory(root, keys, doc, transport=None):
    """Build the Free streaming-session factory from a resolved configuration."""
    by_id = {p['id']: p for p in doc['profiles']}
    selected = by_id.get(doc['routes'].get('streaming_tts'))
    if not selected or not selected.get('enabled'):
        return None
    if selected['protocol'] != 'volcengine_tts_ws_bidirectional':
        return None
    params = selected['parameters']
    endpoint = selected['base_url'] or ENDPOINT_BIDIRECTIONAL
    audit_endpoint = _audit_endpoint_for(endpoint)

    def factory(evidence_root, *, session_id, turn_id, run_index, directory=None,
                _profile=selected, _params=params, _endpoint=endpoint,
                _audit=audit_endpoint, _transport=transport, _keys=keys):
        from .streaming_tts import new_tts_stream_id
        return VolcengineBidirectionalTTSSession(
            stream_id=new_tts_stream_id(), root=Path(directory) if directory else Path(evidence_root),
            key=_keys.get(_profile['id'], ''), resource_id=_params.get('resource_id', ''),
            request_endpoint=_endpoint, audit_endpoint=_audit,
            speaker=_params.get('voice', ''), model=_profile['model'] or 'tts',
            transport=_transport, sample_rate=_params.get('sample_rate', 24000),
            speech_rate=_params.get('speed', 0), loudness_rate=_params.get('volume', 0),
            timeout=_params.get('timeout_seconds', 60),
            context={'session_id': session_id, 'turn_id': turn_id, 'run_index': run_index},
            audit_root=Path(evidence_root))
    return factory


def _audit_endpoint_for(endpoint):
    """The HTTPS audit form of the configured WSS endpoint."""
    if not isinstance(endpoint, str) or not endpoint.startswith('wss://'):
        raise ProviderFailure('Volcengine V3 TTS endpoint must be a wss:// address')
    path = endpoint[len('wss://'):]
    if path.startswith('openspeech.bytedance.com/api/v3/tts/bidirection'):
        return AUDIT_ENDPOINT_BIDIRECTIONAL
    if path.startswith('openspeech.bytedance.com/api/v3/tts/unidirectional/stream'):
        return AUDIT_ENDPOINT_UNIDIRECTIONAL
    raise ProviderFailure('Unsupported Volcengine V3 TTS endpoint')
