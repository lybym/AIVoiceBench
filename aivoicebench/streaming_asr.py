"""Vendor-neutral Streaming ASR boundary for Active Voice Test (PRD-F016/F021/F023).

Recording Analysis keeps the File ASR contract in ``asr.py``: one finished
recording, one recognition, a full transcript, Measurement Evidence, and — where
the service requires it — signed-URL publication.

Active Voice Test needs the other lifecycle: a session that accepts continuous
audio and emits observations while the conversation is still happening. Merging
the two into one file-only interface would hide that difference, so this module
defines the streaming family separately:

    start_session -> push_audio -> poll/wait events -> finish_input -> close/cancel

Two boundaries are enforced here and must not be blurred:

* **Credentials never leave the backend.** The browser ships audio to
  AIVoiceBench; AIVoiceBench authenticates to the provider. No AppID, access
  token, API key or secret is ever handed to a page.
* **Observation is not measurement.** Everything produced here is Control
  Evidence: a suspected or provider-reported observation with its own source,
  timestamp basis and confidence. Provider timing is never acoustic ground truth
  and never becomes Measurement Evidence by itself.

See ``docs/24-streaming-asr.md`` for the boundary, the event model and the
verified vendor contract.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

# Where an observation came from. Never collapse these into one value: the
# product requires conflicts between them to stay visible.
SOURCE_BROWSER_VAD = 'browser_vad'
SOURCE_PROVIDER = 'volcengine_streaming_asr'
SOURCE_COMBINED = 'combined'
OBSERVATION_SOURCES = (SOURCE_BROWSER_VAD, SOURCE_PROVIDER, SOURCE_COMBINED)

# Unified event kinds.
EVENT_SESSION_STARTED = 'asr_session_started'
EVENT_SPEECH_STARTED = 'speech_started'
EVENT_PARTIAL = 'partial_transcript'
EVENT_FINAL = 'final_transcript'
EVENT_SPEECH_ENDED = 'speech_ended'
EVENT_ERROR = 'asr_error'
EVENT_SESSION_CLOSED = 'asr_session_closed'
EVENT_KINDS = (EVENT_SESSION_STARTED, EVENT_SPEECH_STARTED, EVENT_PARTIAL, EVENT_FINAL,
               EVENT_SPEECH_ENDED, EVENT_ERROR, EVENT_SESSION_CLOSED)

# Why a segment or a session ended. Kept explicit so a silent device, a timeout
# and a real response can never be confused.
BASIS_PROVIDER_ENDPOINT = 'provider_endpoint'
BASIS_PROVIDER_LAST_PACKAGE = 'provider_last_package'
BASIS_BROWSER_VAD_SILENCE = 'browser_vad_silence'
BASIS_BROWSER_VAD_TIMEOUT = 'browser_vad_timeout'
BASIS_CLIENT_FINISHED = 'client_finished'
BASIS_CLIENT_CANCELLED = 'client_cancelled'

# First-class failure categories (PRD-F021/F023). A streaming run must end in one
# of these rather than hanging, failing silently, or treating empty text as an
# answer.
ERROR_CATEGORIES = (
    'mic_permission_denied',
    'mic_disconnected',
    'audio_capture_failed',
    'stream_open_failed',
    'stream_disconnected',
    'stream_timeout',
    'provider_auth_failed',
    'provider_rate_limited',
    'provider_error',
    'invalid_audio',
    'asr_no_final',
    'vad_timeout',
    'user_stop',
    'backend_restart',
)


class StreamingASRUnavailable(RuntimeError):
    """The configured streaming capability cannot be used (PRD-F016)."""


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass
class StreamingASREvent:
    """One vendor-neutral streaming observation.

    ``provider_at`` is the provider's own timestamp (when it supplied one) and
    ``received_at`` is when this process received it. They are kept apart on
    purpose: neither is a measured acoustic onset.
    """

    kind: str
    source: str
    text: Optional[str] = None
    sequence: Optional[int] = None
    provider_at: Optional[str] = None
    received_at: str = field(default_factory=utc_now)
    confidence: Optional[float] = None
    basis: Optional[str] = None
    error: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in EVENT_KINDS:
            raise ValueError(f'Unknown streaming ASR event kind: {self.kind!r}')
        if self.source not in OBSERVATION_SOURCES:
            raise ValueError(f'Unknown observation source: {self.source!r}')
        if self.error is not None and self.error not in ERROR_CATEGORIES:
            raise ValueError(f'Unknown streaming ASR error category: {self.error!r}')
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError('Streaming ASR confidence must be within 0..1')

    def to_dict(self):
        return {
            'kind': self.kind,
            'source': self.source,
            'text': self.text,
            'sequence': self.sequence,
            'provider_at': self.provider_at,
            'received_at': self.received_at,
            'confidence': self.confidence,
            'basis': self.basis,
            'error': self.error,
            'detail': self.detail,
            'evidence_scope': 'control_evidence',
        }


class StreamingASRSession(Protocol):
    """A live streaming recognition session.

    ``poll_events`` never blocks; ``wait_events`` waits at most ``timeout``
    seconds and returns as soon as at least one event is available (returning an
    empty list on timeout). ``finish_input`` marks the end of audio; the session
    must still be drained until it reports the last package or times out.
    """

    stream_id: str
    profile: dict

    async def push_audio(self, pcm: bytes) -> None: ...

    async def finish_input(self) -> None: ...

    def poll_events(self) -> list: ...

    async def wait_events(self, timeout: float) -> list: ...

    async def close(self, reason: str = BASIS_CLIENT_FINISHED) -> None: ...

    async def cancel(self, reason: str = BASIS_CLIENT_CANCELLED) -> None: ...


class StreamingASRProvider(Protocol):
    async def start_session(self, *, session_id: str, turn_id: str, run_index: int,
                            directory) -> StreamingASRSession: ...


class UnavailableStreamingASRProvider:
    """Used when no streaming ASR route is configured. Fails loudly, never fakes."""

    name = 'unavailable'

    async def start_session(self, *, session_id, turn_id, run_index, directory):
        raise StreamingASRUnavailable(
            'No streaming ASR capability is configured; configure a streaming ASR model '
            'or run free mode with the explicit turn-file fallback')


def new_stream_id():
    return 'STR-' + uuid.uuid4().hex[:16]


def combine_sources(vad_seen: bool, provider_seen: bool) -> str:
    """Label a merged observation without hiding which side produced it."""
    if vad_seen and provider_seen:
        return SOURCE_COMBINED
    if provider_seen:
        return SOURCE_PROVIDER
    return SOURCE_BROWSER_VAD
