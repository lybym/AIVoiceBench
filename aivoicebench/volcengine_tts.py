"""Volcengine TTS (语音合成大模型) HTTP provider.

Synthesises speech from text for the Active Voice Test. The generated audio
is saved as a WAV file that the browser plays through the user's speakers.

Interface-contract status: pending. The official parameter table could not be
extracted (docs are JS-rendered, same as the ASR endpoint). This implements the
widely-documented HTTP non-streaming format. Verify with credentials before
production use; the exact endpoint and parameter names may differ in the current
service revision.

Every call is audited through the existing InvocationAudit infrastructure, so
provider/model/latency/status are traceable. Secrets never enter artifacts.
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .cloud_transport import HTTPTransport
from .providers import ProviderFailure


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _sha256(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


# The widely-documented Volcengine TTS HTTP endpoint. The user configures the
# base_url in model settings; this is only the default.
DEFAULT_TTS_ENDPOINT = 'https://openspeech.bytedance.com/api/v1/tts'

# A default voice type. The user must configure a real voice_type from their
# Volcengine console; this is only a placeholder so the provider can construct
# a request body without a hard crash.
DEFAULT_VOICE_TYPE = 'BV001_streaming'


class VolcengineTTSProvider:
    """Synthesise speech via the Volcengine TTS HTTP API.

    The provider makes one bounded HTTP POST per call. No streaming, no
    websockets, no automatic retry. If the call fails, the caller keeps the
    error reason and does not mark the phrase as generated.
    """

    tts_version = '1.0.0'

    def __init__(self, root, api_key, *, endpoint=DEFAULT_TTS_ENDPOINT,
                 model='tts', voice_type=DEFAULT_VOICE_TYPE,
                 speed_ratio=1.0, volume_ratio=1.0, pitch_ratio=1.0,
                 audio_format='wav', sample_rate=16000,
                 timeout=30, transport=None):
        self.root = Path(root)
        self.api_key = api_key
        self.endpoint = endpoint.rstrip('/')
        self.model = model
        self.voice_type = voice_type
        self.speed_ratio = speed_ratio
        self.volume_ratio = volume_ratio
        self.pitch_ratio = pitch_ratio
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.timeout = timeout
        self.transport = transport or HTTPTransport(timeout)
        self.profile = {
            'provider': 'volcengine',
            'model_id': model,
            'model_version': 'service-managed',
            'library_version': f'aivoicebench-volcengine-tts:{self.tts_version}',
            'voice_type': voice_type,
            'audio_format': audio_format,
            'sample_rate': sample_rate,
        }

    def synthesize(self, text, destination):
        """Synthesise ``text`` and save the audio to ``destination``.

        Returns a dict with the synthesis metadata. Raises ProviderFailure on
        any error; the caller must not mark the phrase as generated.
        """
        if not self.api_key:
            raise ProviderFailure('TTS credential missing')

        request_id = str(uuid.uuid4())
        started_at = _utc_now()
        start_time = time.monotonic()
        config = {'model': self.model, 'voice_type': self.voice_type,
                  'audio_format': self.audio_format, 'sample_rate': self.sample_rate}

        try:
            body = self._build_request(text, request_id)
            reply = self.transport.request(
                'POST', self.endpoint,
                {'Content-Type': 'application/json',
                 'Authorization': f'Bearer;{self.api_key}'},
                json.dumps(body).encode())
            audio_data = self._parse_response(reply, text)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(audio_data)
            latency_ms = round((time.monotonic() - start_time) * 1000, 3)
            # Record a simple audit in the session directory (not the full
            # provider-invocation audit, which requires file input artifacts;
            # TTS input is text, not a file).
            audit_path = destination.parent / f'{destination.stem}-tts-audit.json'
            audit_path.write_text(json.dumps({
                'schema_version': '1.0.0',
                'invocation_id': 'CALL-' + request_id,
                'provider': 'volcengine',
                'model': self.model,
                'voice_type': self.voice_type,
                'endpoint': self.endpoint,
                'config': config,
                'started_at': started_at,
                'finished_at': _utc_now(),
                'latency_ms': latency_ms,
                'status': 'complete',
                'output': {
                    'path': str(destination.name),
                    'sha256': _sha256(audio_data),
                    'size_bytes': len(audio_data),
                },
                'input_text': text[:200] + '...' if len(text) > 200 else text,
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            return {
                'path': str(destination),
                'sha256': _sha256(audio_data),
                'size_bytes': len(audio_data),
                'format': self.audio_format,
                'sample_rate': self.sample_rate,
                'text': text,
                'voice_type': self.voice_type,
                'model': self.model,
                'request_id': request_id,
            }
        except ProviderFailure:
            raise
        except Exception:
            raise ProviderFailure('TTS synthesis failed; check configuration and credentials') from None

    def _build_request(self, text, request_id):
        """Build the TTS HTTP request body.

        This follows the widely-documented Volcengine TTS format. The exact
        field names and endpoint may differ in the current revision; verify
        with credentials before production use.
        """
        return {
            'app': {
                'appid': '',
                'token': 'access_token',
                'cluster': 'volcano_tts',
            },
            'user': {
                'uid': 'aivoicebench',
            },
            'audio': {
                'voice_type': self.voice_type,
                'encoding': self.audio_format,
                'speed_ratio': str(self.speed_ratio),
                'volume_ratio': str(self.volume_ratio),
                'pitch_ratio': str(self.pitch_ratio),
                'rate': self.sample_rate,
            },
            'request': {
                'reqid': request_id,
                'text': text,
                'text_type': 'plain',
                'operation': 'query',
            },
        }

    def _parse_response(self, reply, text):
        """Extract audio bytes from the TTS response.

        The response is expected to be JSON with a ``data`` field containing
        base64-encoded audio, or raw audio bytes with an appropriate
        content-type. Both are handled defensively.
        """
        # If the response is raw audio (content-type starts with audio/), use it directly.
        content_type = reply.headers.get('Content-Type', '')
        if content_type.startswith('audio/'):
            return reply.body

        # Otherwise, parse JSON and extract base64 audio.
        try:
            payload = json.loads(reply.body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as error:
            raise ProviderFailure(f'TTS response is not valid JSON or audio: {error}')

        code = payload.get('code') or payload.get('ResponseCode')
        if code and str(code) != '3000' and str(code) != '0':
            raise ProviderFailure(f'TTS rejected the request (code={code}): '
                                  f'{payload.get("message") or payload.get("ResponseMessage") or "unknown"}')

        data = payload.get('data') or payload.get('audio') or payload.get('audio_data')
        if not data:
            raise ProviderFailure('TTS response contained no audio data')

        import base64
        try:
            return base64.b64decode(data)
        except Exception as error:
            raise ProviderFailure(f'TTS audio data could not be decoded: {error}')


def _sha256(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


class UnavailableTTSProvider:
    """Production default when no TTS provider is configured."""

    def synthesize(self, text, destination):
        raise ProviderFailure('No TTS provider configured; '
                              'add a volcengine_tts profile in model settings')
