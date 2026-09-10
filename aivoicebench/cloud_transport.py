"""Explicit cloud file publication and resumable Volcano ASR jobs.

Each query is one bounded request. No automatic billable resubmission, hidden
upload, guessed role or acoustic ground truth. Credentials never enter job files.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qsl
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
import wave

from .providers import InvocationAudit, ProviderIdentity, ProviderFailure, immutable_json
from .runner import digest
from .validation import load_document


API = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'
MAX_AUDIO_BYTES = 60_000_000  # canonical 30-minute mono PCM16 is below this bound
MAX_REPLY_BYTES = 32_000_000


@dataclass
class HTTPReply:
    status: int
    headers: dict
    body: bytes


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def https_url(url, allowed_host):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != allowed_host or parsed.username
            or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
        raise ProviderFailure('Invalid configured HTTPS destination')
    return parsed


class HTTPTransport:
    def __init__(self, timeout=300):
        self.timeout = timeout

    def request(self, method, url, headers, body=None, *, limit=MAX_REPLY_BYTES):
        try:
            request = Request(url, data=body, headers=headers, method=method)
            try:
                response = build_opener(NoRedirect()).open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise ProviderFailure('Response exceeds configured size limit')
                return HTTPReply(response.code, {k.lower(): v for k, v in response.headers.items()}, payload)
        except Exception:
            raise ProviderFailure('Cloud transport failed; request outcome may be unknown') from None


def canonical_audio(path):
    path = Path(path)
    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise ProviderFailure('Canonical audio exceeds size limit')
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ProviderFailure('Cloud input requires canonical PCM16/16kHz/mono WAV')
        frames = audio.getnframes()
        if not 0 < frames <= 16000 * 1800:
            raise ProviderFailure('Cloud input must be nonempty and at most 30 minutes')
        count = 0
        while chunk := audio.readframes(65536):
            count += len(chunk)
        if count != frames * 2:
            raise ProviderFailure('Truncated canonical audio')
    return frames / 16


def closed_config(config):
    return {'type': 'object', 'additionalProperties': False, 'required': list(config),
            'properties': {key: {'const': value} for key, value in config.items()}}


class SignedURLPublication:
    """Caller supplies expiring PUT/GET URLs for the SAME configured object.

    Object content is read back and verified before ASR receives its URL. This
    does not create buckets, change ACLs or persist temporary authorization URLs.
    """

    def __init__(self, put_url, get_url, allowed_host, transport=None):
        put, get = https_url(put_url, allowed_host), https_url(get_url, allowed_host)
        if put.netloc != get.netloc or put.path != get.path:
            raise ProviderFailure('PUT and GET must address the same object')
        self.put_url, self.get_url = put_url, get_url
        self.host = allowed_host
        self.transport = transport or HTTPTransport()

    def publish(self, source, root):
        canonical_audio(source)
        config = {'method': 'signed_put_readback', 'host': self.host}
        audit = InvocationAudit(root, ProviderIdentity('configured-storage', 'object', 'signed-put-1.0.0'),
                                config, closed_config(config), [source])
        try:
            data = Path(source).read_bytes()
            result = self.transport.request('PUT', self.put_url, {'Content-Type': 'audio/wav'}, data)
            if not 200 <= result.status < 300:
                raise ProviderFailure('Configured audio publication failed')
            result = self.transport.request('GET', self.get_url, {}, limit=MAX_AUDIO_BYTES)
            if result.status != 200 or hashlib.sha256(result.body).hexdigest() != hashlib.sha256(data).hexdigest():
                raise ProviderFailure('Published audio readback does not match local evidence')
            proof = audit.directory / 'publication.json'
            immutable_json(proof, {'schema_version': '1.0.0', 'status': 'verified',
                                  'source_sha256': hashlib.sha256(data).hexdigest(),
                                  'host': self.host, 'verification': 'sha256_https_readback'})
            audit.finish('complete', [proof])
            return self.get_url, proof
        except Exception:
            audit.finish('failed', failure_code='provider_error')
            raise ProviderFailure('Audio publication failed; inspect invocation evidence') from None


