"""Active Voice Test session manager.

Manages real-time conversation sessions between the platform and a physical AI
device. Two modes:

- Fixed: a pre-defined list of test phrases, TTS-generated, played in sequence.
  The browser plays each phrase, detects the device's response via VAD, and
  advances to the next phrase when the device finishes.

- Free: an LLM test agent generates each phrase based on the test goal,
  conversation history, and the device's observed response (captured via mic
  and transcribed with ASR). The agent can ask follow-up questions, switch
  topics, and stop when the goal is met or the budget is exhausted.

Real-time control (playback, VAD, ASR) is NOT routed through ImportRun or the
offline analysis chain. It runs on its own session state. An external recording
can still be imported separately for formal measurement.

The browser handles audio playback and microphone capture; the backend handles
TTS synthesis, LLM calls, ASR (for free mode), and session state.

Control trace
-------------
Every session keeps a minimal *control* trace in ``execution-record.json``
inside its own session directory, so a finished run can answer: which
session/turn it was, which text and audio asset were used, which play
instruction the server issued, what the browser reported about playback, what
was observed (a suspected response, or no response at all), and why the run
advanced, stopped or failed.

Two boundaries are recorded explicitly and must not be blurred:

- "the server issued a play instruction" and "the browser reported playback"
  are separate events. Neither is a measured acoustic onset in the room.
- Browser VAD only reports a *suspected* response. It does not identify who
  spoke and it does not measure a formal response latency.

This trace is deliberately small. It is not an offline measurement pipeline,
not a Timeline, and not an input to metric computation.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .providers import ProviderFailure
from .streaming_asr import (EVENT_ERROR, EVENT_FINAL, EVENT_PARTIAL,
                            EVENT_SESSION_CLOSED, EVENT_SESSION_STARTED, EVENT_SPEECH_ENDED,
                            SOURCE_BROWSER_VAD, SOURCE_PROVIDER)

CONTROL_RECORD_VERSION = '1.1.0'

# How the device's answer becomes text during a free-mode turn (PRD-F021).
CAPTURE_MODE_STREAMING = 'streaming'
CAPTURE_MODE_TURN_FILE = 'turn_file'
CAPTURE_MODE_AUTO = 'auto'
CAPTURE_MODES = (CAPTURE_MODE_STREAMING, CAPTURE_MODE_TURN_FILE, CAPTURE_MODE_AUTO)

# Bounded wait for the provider's last package after the browser stops sending
# audio. A control guard, not a product latency target.
ASR_FINAL_TIMEOUT_MS = 15000
# Partials are kept for the trace, bounded so a long turn cannot grow forever.
MAX_STREAMING_PARTIAL_EVENTS = 200

STREAMING_EVIDENCE_SCOPE = 'control_evidence'

# Turn lifecycle phases. These describe platform control state, not acoustics.
PHASE_PENDING = 'pending'
PHASE_PLAY_ISSUED = 'play_issued'
PHASE_PLAYBACK_STARTED = 'playback_started'
PHASE_PLAYBACK_ENDED = 'playback_ended'
PHASE_OBSERVING = 'observing'
PHASE_OBSERVED = 'observed'
PHASE_NO_RESPONSE = 'no_response'
PHASE_CANCELLED = 'cancelled'
PHASE_FAILED = 'failed'

# Observation kinds recorded on a turn.
OBSERVATION_SPEECH_START = 'speech_start'
OBSERVATION_SPEECH_END = 'speech_end'
OBSERVATION_NO_RESPONSE = 'no_response'

# Every VAD-derived observation is suspected, not confirmed.
VAD_BASIS = 'browser_vad_rms'

# Explicit handling when no response is observed for the current turn.
ON_NO_RESPONSE_PAUSE = 'pause'
ON_NO_RESPONSE_CONTINUE = 'continue'

# Bounded control waits. These keep a stuck browser or a silent device from
# hanging a run forever. They are control guards, NOT product performance
# targets, and no measured latency is derived from them.
CONTROL_POLICY = {
    'playback_start_timeout_ms': 10000,
    'playback_max_duration_ms': 180000,
    'no_response_timeout_ms': 30000,
}

# Accepted range for a caller-supplied no-response wait bound (milliseconds).
NO_RESPONSE_TIMEOUT_MIN_MS = 1000
NO_RESPONSE_TIMEOUT_MAX_MS = 600000


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


RECORD_NOTES = [
    'Control trace only: playback and observation events are reported by the '
    'browser and are not measurements of the physical sound field.',
    'VAD observations are suspected responses (browser_vad_rms); speaker '
    'identity and formal response latency are not measured here.',
    'Streaming ASR transcripts are control observations as well: provider '
    'timestamps are not acoustic ground truth and never become Measurement '
    'Evidence by themselves.',
    'control_policy waits bound how long control waits before giving up; they '
    'are not product performance targets or SLAs.',
]


@dataclass
class VoiceTestCapture:
    """One streaming-ASR capture attached to a free-mode turn.

    Audio bytes are never stored here: they live in the session directory as a
    WAV artifact. This object keeps the control facts the execution record needs.
    """

    turn_id: str
    stream_id: str
    sample_rate: int
    channels: int
    bits: int
    mode: str = CAPTURE_MODE_STREAMING
    started_at: str = field(default_factory=utc_now)
    finished_at: Optional[str] = None
    status: str = 'active'  # active, finished, failed, cancelled
    asr: object = None
    partials: list = field(default_factory=list)
    final_text: str = ''
    final_basis: Optional[str] = None
    final_source: Optional[str] = None
    speech_started: bool = False
    endpoint_basis: Optional[str] = None
    failure: Optional[str] = None
    events_seen: int = 0
    audio_bytes: int = 0
    expected_sequence: int = 0
    last_sequence_seen: Optional[int] = None
    late_frames: int = 0
    duplicate_frames: int = 0
    gap_frames: int = 0
    fallback_reason: Optional[str] = None

    @property
    def is_streaming(self):
        return self.mode == CAPTURE_MODE_STREAMING

    def to_dict(self):
        return {
            'turn_id': self.turn_id,
            'stream_id': self.stream_id,
            'mode': self.mode,
            'is_streaming': self.is_streaming,
            'evidence_scope': STREAMING_EVIDENCE_SCOPE,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'status': self.status,
            'audio': {'format': 'pcm_s16le', 'sample_rate': self.sample_rate,
                      'bits': self.bits, 'channels': self.channels},
            'audio_bytes': self.audio_bytes,
            'partial_count': len(self.partials),
            'partials': list(self.partials),
            'final_text': self.final_text,
            'final_basis': self.final_basis,
            'final_source': self.final_source,
            'speech_started': self.speech_started,
            'endpoint_basis': self.endpoint_basis,
            'failure': self.failure,
            'events_seen': self.events_seen,
            'frame_ordering': {'expected_sequence': self.expected_sequence,
                               'late': self.late_frames,
                               'duplicate': self.duplicate_frames,
                               'gap': self.gap_frames},
            'fallback_reason': self.fallback_reason,
        }


@dataclass
class VoiceTestTurn:
    """One turn in a conversation test."""
    turn_index: int
    role: str  # 'platform' (our test phrase) or 'device' (observed response)
    turn_id: str = ''
    text: Optional[str] = None
    audio_path: Optional[str] = None
    audio_sha256: Optional[str] = None
    audio_url: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    # 'speech_start', 'speech_end', 'no_response'
    observation: Optional[str] = None
    observation_basis: Optional[str] = None  # 'browser_vad_rms' when VAD-derived
    observation_started_at: Optional[str] = None
    observation_ended_at: Optional[str] = None
    status: str = 'pending'
    phase: str = PHASE_PENDING
    play_issued_at: Optional[str] = None
    playback_started_at: Optional[str] = None
    playback_ended_at: Optional[str] = None
    closure_reason: Optional[str] = None
    closed: bool = False
    events: list = field(default_factory=list)

    def to_dict(self):
        return {
            'turn_index': self.turn_index,
            'turn_id': self.turn_id,
            'role': self.role,
            'text': self.text,
            'audio_path': self.audio_path,
            'audio_sha256': self.audio_sha256,
            'audio_url': self.audio_url,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'observation': self.observation,
            'observation_basis': self.observation_basis,
            'observation_started_at': self.observation_started_at,
            'observation_ended_at': self.observation_ended_at,
            'status': self.status,
            'phase': self.phase,
            'play_issued_at': self.play_issued_at,
            'playback_started_at': self.playback_started_at,
            'playback_ended_at': self.playback_ended_at,
            'closure_reason': self.closure_reason,
            'closed': self.closed,
        }


@dataclass
class VoiceTestSession:
    """A voice test session — fixed or free mode."""
    session_id: str
    mode: str  # 'fixed' or 'free'
    status: str = 'created'  # created, generating, ready, running, stopped, completed, failed
    created_at: str = field(default_factory=utc_now)
    device: Optional[str] = None

    # Fixed mode
    phrases: list = field(default_factory=list)  # [{text, audio_path, audio_sha256, status}]
    current_phrase_index: int = 0

    # Free mode
    goal: Optional[str] = None
    constraints: list = field(default_factory=list)
    max_turns: int = 10
    llm_model: Optional[str] = None

    # Common
    turns: list = field(default_factory=list)  # list of VoiceTestTurn
    stop_reason: Optional[str] = None
    config_snapshot: Optional[dict] = None
    directory: Optional[Path] = None

    # Control trace
    events: list = field(default_factory=list)
    awaiting_turn_id: Optional[str] = None
    on_no_response: str = ON_NO_RESPONSE_PAUSE
    no_response_timeout_ms: int = CONTROL_POLICY['no_response_timeout_ms']
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    # Free-mode device observation (PRD-F021). `capture_mode` is the operator's
    # request; `resolved_capture_mode` is what this run actually uses, so a
    # fallback can never be reported as streaming.
    capture_mode: str = CAPTURE_MODE_AUTO
    resolved_capture_mode: Optional[str] = None
    capture_fallback_reason: Optional[str] = None
    streaming_asr_error: Optional[str] = None
    capture: Optional['VoiceTestCapture'] = None
    # Authoritative copy of the last finished capture, written by the audio
    # socket and read by the control loop. The browser's echo is never trusted.
    last_capture_result: Optional[dict] = None
    capped_notes: list = field(default_factory=list)
    # Each start begins a new run. Turn ids are unique for the whole session so
    # a late event from an earlier run can never be mistaken for the current
    # one. Finished runs are archived instead of being overwritten.
    run_index: int = 0
    turn_seq: int = 0
    runs: list = field(default_factory=list)

    # WebSocket reference (set by the API layer)
    _websocket = None

    @property
    def is_running(self):
        return self.status == 'running'

    @property
    def record_path(self):
        return (self.directory / 'execution-record.json') if self.directory else None

    def to_dict(self):
        return {
            'session_id': self.session_id,
            'mode': self.mode,
            'status': self.status,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'finished_at': self.finished_at,
            'device': self.device,
            'phrases': self.phrases,
            'current_phrase_index': self.current_phrase_index,
            'awaiting_turn_id': self.awaiting_turn_id,
            'goal': self.goal,
            'constraints': self.constraints,
            'max_turns': self.max_turns,
            'on_no_response': self.on_no_response,
            'no_response_timeout_ms': self.no_response_timeout_ms,
            'capture_mode': self.capture_mode,
            'resolved_capture_mode': self.resolved_capture_mode,
            'capture_fallback_reason': self.capture_fallback_reason,
            'run_index': self.run_index,
            'turns': [t.to_dict() for t in self.turns],
            'stop_reason': self.stop_reason,
            'execution_record': {
                'record_version': CONTROL_RECORD_VERSION,
                'event_count': len(self.events),
                'path': 'execution-record.json',
                'url': f'/api/voice-test/sessions/{self.session_id}/execution-record',
            },
        }


class VoiceTestManager:
    """Manages voice test sessions in memory.

    Sessions are not persisted to disk (they are real-time control state, not
    measurement evidence), but the minimal control trace of each session is
    written to its own session directory so a finished run stays inspectable.
    """

    def __init__(self, output_root, providers=None):
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.sessions = {}  # session_id -> VoiceTestSession
        self._providers = providers

    def get_providers(self):
        """Resolve providers from model settings if not pre-injected."""
        if self._providers is not None:
            return self._providers
        from .model_settings import ModelSettings
        settings = ModelSettings(self.output_root / '.model-settings')
        _, providers = settings.capture()
        self._providers = providers
        return self._providers

    # -------------------------------------------------------------- sessions

    def create_session(self, mode, *, phrases=None, device=None, goal=None,
                       constraints=None, max_turns=10, llm_model=None,
                       on_no_response=ON_NO_RESPONSE_PAUSE,
                       no_response_timeout_ms=None, capture_mode=CAPTURE_MODE_AUTO):
        if on_no_response not in (ON_NO_RESPONSE_PAUSE, ON_NO_RESPONSE_CONTINUE):
            raise ValueError("on_no_response must be 'pause' or 'continue'")
        if capture_mode not in CAPTURE_MODES:
            raise ValueError(f"capture_mode must be one of {CAPTURE_MODES}")
        if no_response_timeout_ms is None:
            no_response_timeout_ms = CONTROL_POLICY['no_response_timeout_ms']
        try:
            no_response_timeout_ms = int(no_response_timeout_ms)
        except (TypeError, ValueError):
            raise ValueError('no_response_timeout_ms must be an integer') from None
        if not (NO_RESPONSE_TIMEOUT_MIN_MS <= no_response_timeout_ms <= NO_RESPONSE_TIMEOUT_MAX_MS):
            raise ValueError(
                f'no_response_timeout_ms must be between {NO_RESPONSE_TIMEOUT_MIN_MS} '
                f'and {NO_RESPONSE_TIMEOUT_MAX_MS}')
        session_id = 'VT-' + uuid.uuid4().hex[:12]
        session = VoiceTestSession(
            session_id=session_id,
            mode=mode,
            device=device,
            phrases=[{'text': p, 'audio_path': None, 'audio_sha256': None,
                       'status': 'pending'} for p in (phrases or [])],
            goal=goal,
            constraints=constraints or [],
            max_turns=max_turns,
            llm_model=llm_model,
            on_no_response=on_no_response,
            no_response_timeout_ms=no_response_timeout_ms,
            capture_mode=capture_mode,
            directory=self.output_root / 'voice-test' / session_id,
        )
        session.directory.mkdir(parents=True, exist_ok=True)
        self.sessions[session_id] = session
        self.record_event(session, 'session_created', detail={
            'mode': mode, 'phrase_count': len(session.phrases),
            'device': device, 'on_no_response': session.on_no_response,
            'no_response_timeout_ms': session.no_response_timeout_ms,
            'capture_mode': session.capture_mode,
        })
        return session

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def get_audio_path(self, session_id, phrase_index):
        """Get the absolute path to a generated audio file."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        if session.mode == 'fixed':
            if phrase_index >= len(session.phrases):
                return None
            phrase = session.phrases[phrase_index]
            if not phrase.get('audio_path'):
                return None
            return self.output_root / phrase['audio_path']
        # Free mode: look for free-turn files
        audio_path = session.directory / f'free-turn-{phrase_index:04d}.wav'
        return audio_path if audio_path.is_file() else None

    # ------------------------------------------------------------- synthesis

    def synthesize_phrase(self, session_id, phrase_index):
        """Generate TTS audio for a phrase (fixed mode)."""
        session = self.sessions.get(session_id)
        if not session or session.mode != 'fixed':
            raise ValueError('Session not found or not fixed mode')
        if phrase_index >= len(session.phrases):
            raise ValueError(f'Phrase index {phrase_index} out of range')
        phrase = session.phrases[phrase_index]
        if phrase['status'] == 'ready':
            return phrase  # Already generated, reuse

        providers = self.get_providers()
        tts = providers.tts(session.directory) if providers and providers.tts else None
        if tts is None:
            raise ProviderFailure('No TTS provider configured; add a volcengine_tts profile')

        audio_path = session.directory / f'phrase-{phrase_index:04d}.wav'
        result = tts.synthesize(phrase['text'], audio_path)
        phrase['audio_path'] = str(audio_path.relative_to(self.output_root))
        phrase['audio_sha256'] = result.get('sha256')
        phrase['status'] = 'ready'
        return phrase

    def synthesize_all(self, session_id):
        """Generate TTS audio for all phrases (fixed mode)."""
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError('Session not found')
        results = []
        for i in range(len(session.phrases)):
            results.append(self.synthesize_phrase(session_id, i))
        session.status = 'ready'
        return results

    def synthesize_text(self, session_id, text, turn_index=None):
        """Generate TTS audio for arbitrary text (free mode).

        ``turn_index`` lets the caller reserve the index of the platform turn
        this asset belongs to, so the asset name and the turn stay aligned even
        when the device's reply is recorded first. Returns the relative path.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError('Session not found')
        providers = self.get_providers()
        tts = providers.tts(session.directory) if providers and providers.tts else None
        if tts is None:
            raise ProviderFailure('No TTS provider configured')
        if turn_index is None:
            turn_index = len(session.turns)
        audio_path = session.directory / f'free-turn-{turn_index:04d}.wav'
        result = tts.synthesize(text, audio_path)
        rel_path = str(audio_path.relative_to(self.output_root))
        return {'path': rel_path, 'sha256': result.get('sha256'), 'text': text}

    # --------------------------------------------------------- control trace

    def record_event(self, session, kind, *, turn_id=None, detail=None, source='server'):
        """Append one control event and persist the session record."""
        event = {
            'seq': len(session.events) + 1,
            'at': utc_now(),
            'kind': kind,
            'source': source,  # 'server' or 'browser'
            'run_index': session.run_index,
            'turn_id': turn_id,
            'detail': detail or {},
        }
        session.events.append(event)
        if turn_id:
            turn = self.turn_by_id(session, turn_id)
            if turn is not None:
                turn.events.append({'seq': len(turn.events) + 1, 'at': event['at'],
                                    'kind': kind, 'source': source,
                                    'detail': event['detail']})
        self.persist_record(session)
        return event

    def build_record(self, session):
        return {
            'record_version': CONTROL_RECORD_VERSION,
            'session_id': session.session_id,
            'mode': session.mode,
            'device': session.device,
            'created_at': session.created_at,
            'run_index': session.run_index,
            'status': session.status,
            'stop_reason': session.stop_reason,
            'current_phrase_index': session.current_phrase_index,
            'awaiting_turn_id': session.awaiting_turn_id,
            'on_no_response': session.on_no_response,
            'control_policy': dict(CONTROL_POLICY,
                                   on_no_response=session.on_no_response,
                                   no_response_timeout_ms=session.no_response_timeout_ms),
            'capture_mode': session.capture_mode,
            'resolved_capture_mode': session.resolved_capture_mode,
            'capture_fallback_reason': session.capture_fallback_reason,
            'streaming_asr_error': session.streaming_asr_error,
            'streaming_capture': session.capture.to_dict() if session.capture else None,
            'notes': list(RECORD_NOTES) + list(session.capped_notes),
            'runs': session.runs + [self._current_run(session)],
            'events': session.events,
        }

    @staticmethod
    def _current_run(session):
        return {
            'run_index': session.run_index,
            'started_at': session.started_at,
            'finished_at': session.finished_at,
            'status': session.status,
            'stop_reason': session.stop_reason,
            'current': True,
            'turns': [t.to_dict() for t in session.turns],
        }

    def _archive_current_run(self, session):
        """Keep a finished run instead of letting the next start erase it."""
        if not session.turns:
            return
        archived = self._current_run(session)
        archived['current'] = False
        archived['finished_at'] = session.finished_at or utc_now()
        session.runs.append(archived)

    def persist_record(self, session):
        path = session.record_path
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.build_record(session), ensure_ascii=False, indent=2),
                        encoding='utf-8')
        return path

    def load_record(self, session_id):
        """Read a persisted control record (also works after a process restart)."""
        session = self.sessions.get(session_id)
        directory = session.directory if session else (self.output_root / 'voice-test' / session_id)
        path = directory / 'execution-record.json'
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return None

    # -------------------------------------------------------- turn lifecycle

    def turn_by_id(self, session, turn_id):
        if not turn_id:
            return None
        for turn in session.turns:
            if turn.turn_id == turn_id:
                return turn
        return None

    def awaiting_turn(self, session):
        return self.turn_by_id(session, session.awaiting_turn_id)

    def is_current_turn(self, session, turn):
        """A turn may only be advanced by events for the turn awaiting one."""
        return (turn is not None and not turn.closed
                and session.status == 'running'
                and session.awaiting_turn_id == turn.turn_id)

    def note_device_response(self, session, text, *, source='browser'):
        """Record the device's transcript so the next turn keeps full context.

        The text comes from ASR over browser-captured audio. It is control
        context for the next turn, not a measurement, and it never carries a
        confirmed speaker identity.
        """
        now = utc_now()
        turn = VoiceTestTurn(
            turn_index=len(session.turns),
            turn_id=f'D{session.turn_seq}',
            role='device',
            text=text,
            started_at=now,
            finished_at=now,
            status='observed',
            phase=PHASE_OBSERVED,
            observation=OBSERVATION_SPEECH_END,
            closure_reason='device_transcript',
            closed=True,
        )
        session.turn_seq += 1
        session.turns.append(turn)
        self.record_event(session, 'device_transcript', turn_id=turn.turn_id,
                          source=source, detail={
                              'transcript': text,
                              'note': 'ASR text from browser-captured audio; not a measurement, '
                                      'speaker not confirmed',
                          })
        return turn

    # -------------------------------------------------- streaming ASR capture

    def begin_capture(self, session, *, turn_id, stream_id, sample_rate, channels, bits,
                      mode=CAPTURE_MODE_STREAMING, asr=None, fallback_reason=None):
        """Attach one streaming capture to the current free-mode turn."""
        capture = VoiceTestCapture(turn_id=turn_id, stream_id=stream_id,
                                   sample_rate=sample_rate, channels=channels, bits=bits,
                                   mode=mode, asr=asr, fallback_reason=fallback_reason)
        session.capture = capture
        self.record_event(session, 'capture_started', turn_id=turn_id, detail={
            'mode': mode, 'stream_id': stream_id,
            'evidence_scope': STREAMING_EVIDENCE_SCOPE,
            'audio': capture.to_dict()['audio'],
            'fallback_reason': fallback_reason})
        return capture

    def note_streaming_events(self, session, events):
        """Fold unified streaming events into the control trace (never raw PCM)."""
        capture = session.capture
        if capture is None:
            return []
        applied = []
        for event in events:
            capture.events_seen += 1
            if event.kind == EVENT_PARTIAL:
                if len(capture.partials) < MAX_STREAMING_PARTIAL_EVENTS:
                    capture.partials.append({'text': event.text, 'sequence': event.sequence,
                                             'received_at': event.received_at,
                                             'provider_at': event.provider_at})
                elif 'streaming partials truncated' not in session.capped_notes:
                    session.capped_notes.append('streaming partials truncated')
            elif event.kind == EVENT_FINAL:
                capture.final_text = event.text or ''
                capture.final_basis = event.basis
                capture.final_source = event.source
            elif event.kind == EVENT_SPEECH_ENDED:
                capture.endpoint_basis = event.basis
            elif event.kind == EVENT_ERROR:
                capture.failure = event.error
                session.streaming_asr_error = event.error
            self.record_event(session, event.kind, turn_id=capture.turn_id,
                              source='provider', detail={
                                  'evidence_scope': STREAMING_EVIDENCE_SCOPE,
                                  'text': event.text, 'basis': event.basis,
                                  'error': event.error, 'sequence': event.sequence,
                                  'provider_at': event.provider_at,
                                  'received_at': event.received_at,
                                  'detail': event.detail})
            applied.append(event)
        return applied

    def finish_capture(self, session, *, status='finished', failure=None):
        """Close the capture and record what the control layer learned."""
        capture = session.capture
        if capture is None:
            return None
        capture.status = status
        capture.finished_at = utc_now()
        if failure:
            capture.failure = failure
        summary = capture.to_dict()
        if capture.asr is not None:
            try:
                summary['provider_summary'] = capture.asr.summary()
            except Exception:  # noqa: BLE001 - a summary must never break a run
                summary['provider_summary'] = None
        self.record_event(session, 'capture_finished', turn_id=capture.turn_id,
                          detail={'status': status, 'failure': capture.failure,
                                  'final_text': capture.final_text,
                                  'final_basis': capture.final_basis,
                                  'partial_count': len(capture.partials),
                                  'audio_bytes': capture.audio_bytes,
                                  'frame_ordering': summary['frame_ordering'],
                                  'evidence_scope': STREAMING_EVIDENCE_SCOPE})
        session.capture = None
        return capture

    def open_turn(self, session, *, text, audio_path=None, audio_url=None,
                  role='platform', audio_sha256=None):
        """Record that the server issued a play instruction for a new turn."""
        now = utc_now()
        turn = VoiceTestTurn(
            turn_index=len(session.turns),
            turn_id=f'T{session.turn_seq}',
            role=role,
            text=text,
            audio_path=audio_path,
            audio_sha256=audio_sha256,
            audio_url=audio_url,
            started_at=now,
            play_issued_at=now,
            status='play_issued',
            phase=PHASE_PLAY_ISSUED,
        )
        session.turn_seq += 1
        session.turns.append(turn)
        session.awaiting_turn_id = turn.turn_id
        self.record_event(session, 'play_issued', turn_id=turn.turn_id, detail={
            'text': text, 'audio_path': audio_path, 'audio_url': audio_url,
            'run_index': session.run_index,
            'note': 'server issued a play instruction; not a playback report',
        })
        return turn

    def note_playback(self, session, turn, kind, *, reason=None, detail=None):
        """Record a browser playback report: started / ended / cancelled / failed."""
        now = utc_now()
        payload = dict(detail or {})
        if reason:
            payload['reason'] = reason
        if kind == 'playback_started':
            turn.phase = PHASE_PLAYBACK_STARTED
            turn.playback_started_at = now
            turn.status = 'playing'
        elif kind == 'playback_ended':
            turn.phase = PHASE_PLAYBACK_ENDED
            turn.playback_ended_at = now
            turn.status = 'waiting_device'
        elif kind == 'playback_cancelled':
            turn.phase = PHASE_CANCELLED
            turn.status = 'cancelled'
        elif kind == 'playback_failed':
            turn.phase = PHASE_FAILED
            turn.status = 'failed'
        else:
            raise ValueError(f'Unknown playback report: {kind}')
        self.record_event(session, kind, turn_id=turn.turn_id, detail=payload,
                          source='browser')

    def note_observation(self, session, turn, kind, *, detail=None):
        """Record a browser VAD report: suspected speech start or end."""
        now = utc_now()
        payload = dict(detail or {})
        payload['observation_basis'] = VAD_BASIS
        payload['note'] = 'suspected response from browser VAD; not a confirmed speaker or latency'
        if kind == 'observation_speech_start':
            turn.phase = PHASE_OBSERVING
            turn.observation = OBSERVATION_SPEECH_START
            turn.observation_started_at = now
            turn.status = 'observing'
        elif kind == 'observation_speech_end':
            turn.observation = OBSERVATION_SPEECH_END
            turn.observation_ended_at = now
        else:
            raise ValueError(f'Unknown observation: {kind}')
        turn.observation_basis = VAD_BASIS
        self.record_event(session, kind, turn_id=turn.turn_id, detail=payload,
                          source='browser')

    def close_turn(self, session, turn, *, phase, status, closure_reason,
                   observation=None, observation_basis=None):
        turn.phase = phase
        turn.status = status
        turn.closure_reason = closure_reason
        turn.closed = True
        turn.finished_at = utc_now()
        if observation is not None:
            turn.observation = observation
        turn.observation_basis = observation_basis
        if session.awaiting_turn_id == turn.turn_id:
            session.awaiting_turn_id = None
        self.record_event(session, 'turn_closed', turn_id=turn.turn_id, detail={
            'phase': phase, 'status': status, 'closure_reason': closure_reason,
            'observation': turn.observation, 'observation_basis': turn.observation_basis,
        })
        return turn

    # ----------------------------------------------------------- run control

    def start(self, session_id):
        """Start a new run of a voice test session.

        The previous run is archived rather than overwritten, so a failed or
        stopped attempt keeps its execution record. Turn ids stay unique for
        the whole session, so a late event from an earlier run can never be
        mistaken for the current one.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError('Session not found')
        if session.mode == 'fixed':
            unready = [i for i, p in enumerate(session.phrases) if p['status'] != 'ready']
            if unready:
                raise ValueError(f'Phrases not ready: {unready}. Generate audio first.')
        self._archive_current_run(session)
        session.run_index += 1
        session.status = 'running'
        session.current_phrase_index = 0
        session.stop_reason = None
        session.turns = []
        session.awaiting_turn_id = None
        session.started_at = utc_now()
        session.finished_at = None
        self.record_event(session, 'session_started', detail={
            'mode': session.mode, 'phrase_count': len(session.phrases),
            'run_index': session.run_index,
        })
        return session

    def stop(self, session_id, reason='user_stop'):
        """Stop a running session and close any turn still awaiting observation."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        pending = self.awaiting_turn(session)
        if pending is not None and not pending.closed:
            self.close_turn(session, pending, phase=PHASE_CANCELLED, status='cancelled',
                            closure_reason=reason, observation=pending.observation,
                            observation_basis=pending.observation_basis)
        session.status = 'stopped'
        session.stop_reason = reason
        session.finished_at = utc_now()
        self.record_event(session, 'session_stopped', detail={'reason': reason})
        return session

    def complete(self, session_id, reason):
        """Mark a session completed and close the open turn if any."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        pending = self.awaiting_turn(session)
        if pending is not None and not pending.closed:
            self.close_turn(session, pending, phase=PHASE_OBSERVED, status='complete',
                            closure_reason=reason, observation=pending.observation,
                            observation_basis=pending.observation_basis)
        session.status = 'completed'
        session.stop_reason = reason
        session.finished_at = utc_now()
        self.record_event(session, 'session_completed', detail={'reason': reason})
        return session

    def fail(self, session_id, reason):
        """Mark a session failed and close the open turn with the reason."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        pending = self.awaiting_turn(session)
        if pending is not None and not pending.closed:
            self.close_turn(session, pending, phase=PHASE_FAILED, status='failed',
                            closure_reason=reason)
        session.status = 'failed'
        session.stop_reason = reason
        session.finished_at = utc_now()
        self.record_event(session, 'session_failed', detail={'reason': reason})
        return session
