"""Test-only server entrypoint for the browser control tests.

Runs the real FastAPI app (real routes, real WebSocket protocol, real static
assets) with a deterministic synthetic TTS provider, so the browser tests can
drive the real page without cloud credentials, a Docker image, or an audio
device.

This module is **test infrastructure only**. Nothing in ``aivoicebench`` imports
it, and it is never part of a release artifact. It also exposes a
``__test__/forget`` route used to simulate a backend restart that loses the
in-memory session.

Usage:
    python tests/browser_server.py --port 8099 --output <dir> [options]
"""

import argparse
import hashlib
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aivoicebench import api  # noqa: E402
from aivoicebench.model_settings import RunProviders  # noqa: E402
from aivoicebench.voice_test import VoiceTestManager  # noqa: E402


class SyntheticTTS:
    """Writes a valid silent WAV whose length depends on the phrase.

    Phrase 0 uses ``first_phrase_ms`` so a test has time to press Stop while
    audio is still playing; later phrases use ``phrase_ms`` to keep the suite
    fast. A phrase containing ``[slow]`` always uses ``slow_phrase_ms``, which
    gives a stop-during-playback test a wide, race-free window.
    """

    SLOW_MARKER = '[slow]'

    def __init__(self, root, *, first_phrase_ms=2500, phrase_ms=250,
                 slow_phrase_ms=8000, **kwargs):
        self.root = Path(root)
        self.first_phrase_ms = first_phrase_ms
        self.phrase_ms = phrase_ms
        self.slow_phrase_ms = slow_phrase_ms
        self.calls = []

    def _duration_ms(self, text, destination):
        if self.SLOW_MARKER in text:
            return self.slow_phrase_ms
        index = 0
        stem = Path(destination).stem
        if '-' in stem:
            tail = stem.rsplit('-', 1)[-1]
            if tail.isdigit():
                index = int(tail)
        return self.first_phrase_ms if index == 0 else self.phrase_ms

    def synthesize(self, text, destination):
        self.calls.append(text)
        destination.parent.mkdir(parents=True, exist_ok=True)
        frames = max(1, int(16000 * self._duration_ms(text, destination) / 1000))
        with wave.open(str(destination), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b'\x00\x00' * frames)
        data = destination.read_bytes()
        return {
            'path': str(destination),
            'sha256': hashlib.sha256(data).hexdigest(),
            'size_bytes': len(data),
            'format': 'wav',
            'sample_rate': 16000,
            'text': text,
        }


def build_app(args):
    output_root = Path(args.output).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    tts = SyntheticTTS(output_root, first_phrase_ms=args.first_phrase_ms,
                       phrase_ms=args.phrase_ms, slow_phrase_ms=args.slow_phrase_ms)
    manager = VoiceTestManager(output_root)
    manager._providers = RunProviders(tts=lambda root: tts)

    api._voice_test_manager = manager
    api.OUTPUT_ROOT = output_root

    @api.app.post('/__test__/forget/{session_id}')
    def _forget_session(session_id: str):
        """Simulate a backend restart that loses in-memory sessions."""
        manager.sessions.pop(session_id, None)
        return {'forgotten': session_id}

    @api.app.get('/__test__/state/{session_id}')
    def _state(session_id: str):
        session = manager.get_session(session_id)
        return session.to_dict() if session else {'missing': True}

    @api.app.get('/__test__/record/{session_id}')
    def _record(session_id: str):
        record = manager.load_record(session_id)
        return record if record else {'missing': True}

    return manager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8099)
    parser.add_argument('--output', required=True)
    parser.add_argument('--first-phrase-ms', type=int, default=2500)
    parser.add_argument('--phrase-ms', type=int, default=250)
    parser.add_argument('--slow-phrase-ms', type=int, default=8000)
    parser.add_argument('--no-response-timeout-ms', type=int, default=2500)
    args = parser.parse_args()

    manager = build_app(args)
    # Give every session created through the API the short control bound so the
    # timeout path can be exercised in a browser test.
    original_create = manager.create_session

    def create_session(*call_args, **call_kwargs):
        # The UI does not send this field, so an explicit None must not win.
        if not call_kwargs.get('no_response_timeout_ms'):
            call_kwargs['no_response_timeout_ms'] = args.no_response_timeout_ms
        return original_create(*call_args, **call_kwargs)

    manager.create_session = create_session

    print(json.dumps({'ready': True, 'port': args.port, 'output': args.output}), flush=True)

    import uvicorn
    uvicorn.run(api.app, host=args.host, port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
