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
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .providers import ProviderFailure


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass
class VoiceTestTurn:
    """One turn in a conversation test."""
    turn_index: int
    role: str  # 'platform' (our test phrase) or 'device' (observed response)
    text: Optional[str] = None
    audio_path: Optional[str] = None
    audio_sha256: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    observation: Optional[str] = None  # 'speech_start', 'speech_end', 'timeout', 'none'
    status: str = 'pending'  # pending, playing, waiting_device, observed, complete, failed


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

    # WebSocket reference (set by the API layer)
    _websocket = None

    @property
    def is_running(self):
        return self.status == 'running'

    def to_dict(self):
        return {
            'session_id': self.session_id,
            'mode': self.mode,
            'status': self.status,
            'created_at': self.created_at,
            'device': self.device,
            'phrases': self.phrases,
            'current_phrase_index': self.current_phrase_index,
            'goal': self.goal,
            'constraints': self.constraints,
            'max_turns': self.max_turns,
            'turns': [
                {
                    'turn_index': t.turn_index,
                    'role': t.role,
                    'text': t.text,
                    'started_at': t.started_at,
                    'finished_at': t.finished_at,
                    'observation': t.observation,
                    'status': t.status,
                } for t in self.turns
            ],
            'stop_reason': self.stop_reason,
        }


class VoiceTestManager:
    """Manages voice test sessions in memory.

    Sessions are not persisted to disk (they are real-time control state, not
    measurement evidence). The user can record the conversation with a separate
    device and import it later for formal analysis.
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

    def create_session(self, mode, *, phrases=None, device=None, goal=None,
                       constraints=None, max_turns=10, llm_model=None):
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
            directory=self.output_root / 'voice-test' / session_id,
        )
        session.directory.mkdir(parents=True, exist_ok=True)
        self.sessions[session_id] = session
        return session

    def get_session(self, session_id):
        return self.sessions.get(session_id)

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

    def synthesize_text(self, session_id, text):
        """Generate TTS audio for arbitrary text (free mode).

        Returns the relative audio path.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError('Session not found')
        providers = self.get_providers()
        tts = providers.tts(session.directory) if providers and providers.tts else None
        if tts is None:
            raise ProviderFailure('No TTS provider configured')
        turn_index = len(session.turns)
        audio_path = session.directory / f'free-turn-{turn_index:04d}.wav'
        result = tts.synthesize(text, audio_path)
        rel_path = str(audio_path.relative_to(self.output_root))
        return {'path': rel_path, 'sha256': result.get('sha256'), 'text': text}

    def start(self, session_id):
        """Start a voice test session."""
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError('Session not found')
        if session.mode == 'fixed':
            unready = [i for i, p in enumerate(session.phrases) if p['status'] != 'ready']
            if unready:
                raise ValueError(f'Phrases not ready: {unready}. Generate audio first.')
        session.status = 'running'
        session.current_phrase_index = 0
        session.stop_reason = None
        session.turns = []
        return session

    def stop(self, session_id, reason='user_stop'):
        """Stop a running session."""
        session = self.sessions.get(session_id)
        if not session:
            return None
        session.status = 'stopped'
        session.stop_reason = reason
        return session

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
        else:
            # Free mode: look for free-turn files
            audio_path = session.directory / f'free-turn-{phrase_index:04d}.wav'
            return audio_path if audio_path.is_file() else None
