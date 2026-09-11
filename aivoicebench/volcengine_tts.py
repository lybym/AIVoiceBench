"""Volcengine (Doubao) TTS V3 single-direction SSE provider.

The provider uses the current API-Key based V3 SSE contract. A profile keeps
the resource id and voice selection, while the API key remains write-only in
``ModelSettings`` or an environment variable. Calls are bounded and each
attempt writes an audit record without credentials or raw provider errors.
"""

import base64
import io
import json
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from .cloud_transport import HTTPTransport
from .providers import ProviderFailure


DEFAULT_TTS_ENDPOINT = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse'
TTS_API_VERSION = 'v3_sse_unidirectional'


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _sha256(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


class VolcengineTTSProvider:
    """Synthesize browser-playable WAV through Volcengine's V3 SSE API."""

    tts_version = '2.0.0'

    def __init__(self, root, api_key, *, endpoint=DEFAULT_TTS_ENDPOINT,
                 model='tts', resource_id='', voice_type='', speech_rate=0,
                 loudness_rate=0, pitch_rate=0, audio_format='wav',
                 sample_rate=16000, timeout=30, transport=None):
        self.root = Path(root)
        self.api_key = api_key
        self.endpoint = endpoint.rstrip('/')
        self.model = model
        self.resource_id = resource_id
        self.voice_type = voice_type
        self.speech_rate = speech_rate
        self.loudness_rate = loudness_rate
        self.pitch_rate = pitch_rate
        self.audio_format = audio_format.lower()
        self.sample_rate = sample_rate
        self.timeout = timeout
        self.transport = transport or HTTPTransport(timeout)
        self.profile = {
            'provider': 'volcengine',
            'model_id': model,
            'model_version': 'service-managed',
            'library_version': f'aivoicebench-volcengine-tts:{self.tts_version}',
            'api_version': TTS_API_VERSION,
            'resource_id': resource_id,
            'voice_type': voice_type,
            'audio_format': self.audio_format,
            'sample_rate': sample_rate,
        }

    def synthesize(self, text, destination):
        """Synthesize ``text`` and save a verified WAV to ``destination``."""
        destination = Path(destination)
        request_id = str(uuid.uuid4())
        started_at = _utc_now()
        start_time = time.monotonic()
        config = self._public_config()
        failure_code = None

        try:
            self._validate_configuration(text)
            reply = self.transport.request(
                'POST', self.endpoint,
                {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                    'X-Api-Key': self.api_key,
                    'X-Api-Resource-Id': self.resource_id,
                    'X-Api-Request-Id': request_id,
                },
                json.dumps(self._build_request(text), ensure_ascii=False,
                           separators=(',', ':')).encode('utf-8'))
            if not 200 <= reply.status < 300:
                failure_code = f'http_{reply.status}'
                raise ProviderFailure(f'TTS request rejected (HTTP {reply.status})')
            audio_data = self._parse_sse(reply)
            audio_metadata = self._validate_wav(audio_data)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(audio_data)
            self._write_audit(destination, request_id, started_at, start_time,
                              config, text, 'complete', audio_data=audio_data,
                              audio_metadata=audio_metadata)
            return {
                'path': str(destination),
                'sha256': _sha256(audio_data),
                'size_bytes': len(audio_data),
                'format': self.audio_format,
                'sample_rate': audio_metadata['sample_rate'],
                'text': text,
                'voice_type': self.voice_type,
                'model': self.model,
                'resource_id': self.resource_id,
                'request_id': request_id,
            }
        except ProviderFailure as error:
            self._write_audit(destination, request_id, started_at, start_time,
                              config, text, 'failed',
                              failure_code=failure_code or self._failure_code(error))
            raise
        except Exception:
            self._write_audit(destination, request_id, started_at, start_time,
                              config, text, 'failed', failure_code='unexpected_error')
            raise ProviderFailure('TTS synthesis failed; check configuration and credentials') from None

    def _validate_configuration(self, text):
        if not self.api_key:
            raise ProviderFailure('TTS credential missing')
        if not isinstance(text, str) or not text.strip():
            raise ProviderFailure('TTS text is empty')
        if not self.resource_id:
            raise ProviderFailure('TTS resource_id missing')
        if not self.voice_type:
            raise ProviderFailure('TTS voice missing')
        if self.audio_format != 'wav':
            raise ProviderFailure('Active Voice Test requires TTS format=wav')

    def _public_config(self):
        return {
            'api_version': TTS_API_VERSION,
            'model': self.model,
            'resource_id': self.resource_id,
            'voice_type': self.voice_type,
            'audio_format': self.audio_format,
            'sample_rate': self.sample_rate,
            'speech_rate': self.speech_rate,
            'loudness_rate': self.loudness_rate,
            'pitch_rate': self.pitch_rate,
        }

    def _build_request(self, text):
        additions = {
            'post_process': {'pitch': self.pitch_rate},
            'disable_markdown_filter': True,
            'enable_latex_tn': False,
        }
        return {
            'user': {'uid': 'aivoicebench'},
            'req_params': {
                'text': text,
                'speaker': self.voice_type,
                'audio_params': {
                    'format': self.audio_format,
                    'sample_rate': self.sample_rate,
                    'speech_rate': self.speech_rate,
                    'loudness_rate': self.loudness_rate,
                },
                'additions': json.dumps(additions, ensure_ascii=False,
                                        separators=(',', ':')),
            },
        }

    def _parse_sse(self, reply):
        chunks = []
        try:
            lines = reply.body.decode('utf-8', errors='strict').splitlines()
        except UnicodeDecodeError as error:
            raise ProviderFailure('TTS response contains invalid SSE data') from error
        for line in lines:
            if not line.startswith('data:'):
                continue
            raw = line[5:].strip()
            if not raw or raw == '[DONE]':
                continue
            try:
                event = json.loads(raw)
            except ValueError as error:
                raise ProviderFailure('TTS response contains invalid SSE data') from error
            code = event.get('code', 0)
            if str(code) not in ('0', '20000000'):
                raise ProviderFailure(f'TTS request rejected (code={code})')
            if event.get('data'):
                try:
                    chunks.append(base64.b64decode(event['data'], validate=True))
                except (ValueError, TypeError) as error:
                    raise ProviderFailure('TTS response contains invalid audio data') from error
        if not chunks:
            raise ProviderFailure('TTS response contained no audio data')
        return b''.join(chunks)

    @staticmethod
    def _validate_wav(audio_data):
        try:
            with wave.open(io.BytesIO(audio_data), 'rb') as audio:
                if audio.getnframes() <= 0:
                    raise ProviderFailure('TTS returned an empty WAV')
                return {
                    'channels': audio.getnchannels(),
                    'sample_width_bytes': audio.getsampwidth(),
                    'sample_rate': audio.getframerate(),
                    'frames': audio.getnframes(),
                }
        except (wave.Error, EOFError) as error:
            raise ProviderFailure('TTS response is not a valid WAV asset') from error

    @staticmethod
    def _failure_code(error):
        message = str(error)
        if 'credential' in message:
            return 'credential_missing'
        if 'resource_id' in message or 'voice missing' in message or 'format=' in message:
            return 'configuration_invalid'
        if 'empty' in message:
            return 'input_invalid'
        if 'SSE' in message or 'audio data' in message or 'WAV' in message:
            return 'response_invalid'
        if 'code=' in message:
            return 'provider_rejected'
        return 'provider_error'

    def _write_audit(self, destination, request_id, started_at, start_time,
                     config, text, status, *, audio_data=None,
                     audio_metadata=None, failure_code=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        audit = {
            'schema_version': '1.1.0',
            'invocation_id': 'CALL-' + request_id,
            'provider': 'volcengine',
            'model': self.model,
            'endpoint': self.endpoint,
            'config': config,
            'started_at': started_at,
            'finished_at': _utc_now(),
            'latency_ms': round((time.monotonic() - start_time) * 1000, 3),
            'status': status,
            'input_text': text[:200] + '...' if len(text) > 200 else text,
        }
        if status == 'complete':
            audit['output'] = {
                'path': str(destination.name),
                'sha256': _sha256(audio_data),
                'size_bytes': len(audio_data),
                'audio_metadata': audio_metadata,
            }
        else:
            audit['failure_code'] = failure_code or 'provider_error'
        (destination.parent / f'{destination.stem}-tts-audit.json').write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')


class UnavailableTTSProvider:
    """Production default when no TTS provider is configured."""

    def synthesize(self, text, destination):
        raise ProviderFailure('No TTS provider configured; add a volcengine_tts profile in model settings')
