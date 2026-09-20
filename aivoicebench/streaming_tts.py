"""Vendor-neutral streaming TTS boundary for the Free Voice Test Agent (PRD-F021).

Active Voice Test needs two different TTS lifecycles, and collapsing them into
one interface is what let the previous implementation hide an SSE one-shot call
behind a "streaming" name:

``tts``
    **Complete-text asset synthesis.** Used by the Fixed Case Runner to freeze an
    immutable Stimulus Artifact *before* the formal Run. The Run then plays the
    frozen asset and never re-synthesises it.

``streaming_tts``
    **Streaming-text / streaming-audio session.** Used by the Free Test Agent.
    Speakable text chunks are appended as the LLM emits them and audio comes back
    on the same session, so playback can start before the LLM has finished.

Two boundaries are enforced here and must not be blurred:

* **Credentials never leave the backend.** The browser never connects to the TTS
  provider and never holds its key.
* **TTS is Control/Provider evidence, not measurement.** Text-chunk order, audio
  arrival and playback callbacks are diagnostics. They never become a formal
  acoustic boundary or a Measurement Evidence artifact (see
  ``docs/25-active-measurement.md``).

Stale-ownership is a first-class rule: a TTS session belongs to exactly one
Run/Turn. After Stop, cancel, a run stop or a turn change, late audio must be
discarded as a diagnostic instead of played into the next turn, and a streaming
chain failure must never silently fall back to the old SSE path.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Protocol

from .providers import ProviderFailure

# Session event kinds. Kept separate from the ASR event family: a TTS session
# describes synthesis, not recognition, and mixing them would make a turn's
# trace ambiguous.
TTS_EVENT_SESSION_STARTED = 'tts_session_started'
TTS_EVENT_TEXT_CHUNK = 'tts_text_chunk'
TTS_EVENT_SENTENCE_STARTED = 'tts_sentence_started'
TTS_EVENT_SENTENCE_ENDED = 'tts_sentence_ended'
TTS_EVENT_AUDIO_CHUNK = 'tts_audio_chunk'
TTS_EVENT_SESSION_FINISHED = 'tts_session_finished'
TTS_EVENT_SESSION_CANCELED = 'tts_session_canceled'
TTS_EVENT_SESSION_CLOSED = 'tts_session_closed'
TTS_EVENT_STALE_AUDIO = 'tts_stale_audio'
TTS_EVENT_ERROR = 'tts_error'
TTS_EVENT_KINDS = (TTS_EVENT_SESSION_STARTED, TTS_EVENT_TEXT_CHUNK,
                   TTS_EVENT_SENTENCE_STARTED, TTS_EVENT_SENTENCE_ENDED,
                   TTS_EVENT_AUDIO_CHUNK, TTS_EVENT_SESSION_FINISHED,
                   TTS_EVENT_SESSION_CANCELED, TTS_EVENT_SESSION_CLOSED,
                   TTS_EVENT_STALE_AUDIO, TTS_EVENT_ERROR)

# Why a session ended. A silent provider, a user Stop and a real completion must
# never collapse into one value.
BASIS_CLIENT_FINISHED = 'client_finished'
BASIS_CLIENT_CANCELLED = 'client_cancelled'
BASIS_PROVIDER_FINISHED = 'provider_finished'
BASIS_USER_STOP = 'user_stop'
BASIS_STALE_TURN = 'stale_turn'
BASIS_RUN_STOPPED = 'run_stopped'

# First-class failure categories. A streaming TTS run must end in one of these
# rather than hanging, silently degrading to another transport, or playing an
# empty buffer as if it were speech.
TTS_ERROR_CATEGORIES = (
    'credential_missing',
    'configuration_invalid',
    'input_invalid',
    'invalid_request',
    'unsupported_parameter',
    'provider_auth_failed',
    'provider_rate_limited',
    'provider_quota_exceeded',
    'provider_no_audio',
    'resource_mismatch',
    'provider_error',
    'stream_open_failed',
    'stream_timeout',
    'stream_disconnected',
    'response_invalid',
    'session_cancelled',
    'turn_not_current',
)

# The explicit statement that a streaming failure is not allowed to fall back to
# another TTS transport. Issue #98 §3 requires that if a fallback is ever wanted,
# the product must define it explicitly, label it in the UI and **record it on the
# Run** — so the decision itself is persisted (``TTS_FALLBACK_POLICY``) rather than
# being implied by the absence of a legacy profile.
#
# The forbidden transports are named by the *adapter* that owns them, so this
# module stays vendor-neutral: a second TTS provider declares its own retired
# transports through ``register_forbidden_fallback`` instead of editing a vendor
# list here.
TTS_FALLBACK_POLICY = 'explicit_no_silent_fallback'
TTS_FALLBACK_POLICY_NOTE = (
    'A streaming TTS failure must be reported as a failure of this transport; '
    'silently switching to another TTS transport is forbidden. A future fallback '
    'must be defined by the product, labelled in the UI and recorded on the Run.')

# Retired/alternate transports that must never be reached implicitly.
_FORBIDDEN_FALLBACKS = set()


def register_forbidden_fallback(transport_name):
    """Declare a transport this process must never fall back to implicitly."""
    _FORBIDDEN_FALLBACKS.add(transport_name)
    return transport_name


def forbidden_fallbacks():
    """The transports currently declared as forbidden fallbacks."""
    return tuple(sorted(_FORBIDDEN_FALLBACKS))


def fallback_policy_record():
    """The persisted form of the no-silent-fallback decision (Issue #98 §3)."""
    return {
        'policy': TTS_FALLBACK_POLICY,
        'forbidden_transports': list(forbidden_fallbacks()),
        'silent_fallback': False,
        'note': TTS_FALLBACK_POLICY_NOTE,
    }


STREAMING_TTS_EVIDENCE_SCOPE = 'control_evidence'


class StreamingTTSUnavailable(RuntimeError):
    """The configured streaming-TTS capability cannot be used (PRD-F021)."""


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def new_tts_stream_id():
    return 'TTS-' + uuid.uuid4().hex[:12]


@dataclass
class StreamingTTSEvent:
    """One provider/control-observable event in a TTS session.

    ``source`` is supplied by the adapter that produced the event, so this
    vendor-neutral module never names a provider itself. ``SOURCE_UNKNOWN`` is
    the explicit "no adapter claimed this event" value rather than a provider
    name, so a missing source stays visible instead of masquerading as one.
    """

    kind: str
    source: str
    at: str = field(default_factory=utc_now)
    text: Optional[str] = None
    sequence: Optional[int] = None
    basis: Optional[str] = None
    error: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self):
        payload = {'kind': self.kind, 'at': self.at, 'source': self.source}
        for key in ('text', 'sequence', 'basis', 'error'):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.detail:
            payload['detail'] = dict(self.detail)
        return payload


class StreamingTTSProvider(Protocol):
    """The Free-mode streaming-TTS contract (a session factory)."""

    async def start_session(self, *, session_id, turn_id, run_index, directory=None): ...


class UnavailableStreamingTTSProvider:
    """Production default when no ``streaming_tts`` route is configured."""

    name = 'unavailable_streaming_tts'

    async def start_session(self, *, session_id, turn_id, run_index, directory=None):
        raise StreamingTTSUnavailable(
            'No streaming TTS provider configured; add a streaming_tts route in providers.yaml')


def split_speakable_chunks(text, *, max_chars=None):
    """Split streamed LLM text into ordered, speakable chunks.

    Ordering is preserved exactly: this never reorders, deduplicates or drops a
    chunk, because the turn's text evidence and the provider's audio would then
    disagree. Splitting happens only at safe boundaries — sentence-ending
    punctuation, or a hard limit — and never inside a multi-byte character, since
    the input is already a ``str``.
    """
    if not isinstance(text, str):
        raise ValueError('chunking input must be text')
    if not text:
        return []
    if max_chars is None:
        max_chars = 120
    if not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError('max_chars must be a positive integer')
    boundaries = '。！？；\n!?;'
    chunks = []
    buffer = ''
    for character in text:
        buffer += character
        if character in boundaries or len(buffer) >= max_chars:
            stripped = buffer.strip()
            if stripped:
                chunks.append(stripped)
            buffer = ''
    tail = buffer.strip()
    if tail:
        chunks.append(tail)
    return chunks


def stale_turn_reason(session, turn):
    """Why ``turn`` may no longer own a TTS session on ``session``.

    ``None`` means the turn is still current. Kept as one function so the
    backend, the API and the tests agree on what "stale" means instead of each
    re-deriving it.
    """
    if session is None or turn is None:
        return BASIS_STALE_TURN
    if getattr(session, 'status', None) in ('stopped', 'failed'):
        return BASIS_RUN_STOPPED
    if getattr(turn, 'phase', None) in ('cancelled', 'failed'):
        return BASIS_STALE_TURN
    current = getattr(session, 'turns', None) or []
    if not current or current[-1] is not turn:
        return BASIS_STALE_TURN
    return None


def tts_failure_category(error):
    """Map a raised error to one first-class category (never a raw message).

    The adapter's session already decided the category and carries it in a
    trailing ``(category)`` marker, so this delegates to the adapter's mapper
    instead of re-deriving the answer from message text. Two parallel
    classifications of the same failure would drift, and the drift would be
    invisible until the two reported different reasons for one incident.
    """
    if isinstance(error, StreamingTTSUnavailable):
        return 'stream_open_failed'
    from .volcengine_tts_ws import VolcengineUnidirectionalTTSProvider
    if isinstance(error, ProviderFailure):
        return VolcengineUnidirectionalTTSProvider._failure_code(error)
    return 'provider_error'
