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


API = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/'
RESOURCE = 'volc.seedasr.auc'
CONFIG = {'model_name': 'bigmodel', 'enable_itn': False, 'enable_punc': True,
          'enable_ddc': False, 'show_utterances': True, 'enable_speaker_info': True,
          'ssd_version': '200', 'ssd_mode': 1}
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
    def request(self, method, url, headers, body=None, *, limit=MAX_REPLY_BYTES):
        try:
            request = Request(url, data=body, headers=headers, method=method)
            try:
                response = build_opener(NoRedirect()).open(request, timeout=30)
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


class VolcengineASRJobs:
    """Async provider operations; raw response artifacts precede interpretation."""

    def __init__(self, root, api_key, transport=None):
        self.root = Path(root).resolve()
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderFailure('Volcano API key is not configured')
        self._key = api_key
        self.transport = transport or HTTPTransport()

    def _call(self, action, request_id, body, inputs, sensitive=()):
        config = {'resource_id': RESOURCE, 'action': action, **CONFIG}
        audit = InvocationAudit(self.root,
            ProviderIdentity('volcengine', 'bigmodel', 'volc-asr-jobs-1.0.0', API + action, 'v3'),
            config, closed_config(config), inputs, operation_id=request_id)
        try:
            result = self.transport.request('POST', API + action, {
                'Content-Type': 'application/json', 'X-Api-Key': self._key,
                'X-Api-Resource-Id': RESOURCE, 'X-Api-Request-Id': request_id,
                'X-Api-Sequence': '-1'}, json.dumps(body).encode())
            # Do not persist an SDK/server response that echoes authentication.
            secret_values = [self._key, *sensitive]
            for url in sensitive:
                secret_values.extend(value for key, value in parse_qsl(urlsplit(url).query)
                    if any(term in key.lower() for term in ('signature', 'token', 'credential', 'key'))
                    or key.lower() in ('sig', 'sign', 'auth'))
            if any(value.encode() in result.body for value in secret_values if value):
                raise ProviderFailure('Provider response contains credential material')
            raw = audit.directory / 'native-response.bin'
            with raw.open('xb') as stream:
                stream.write(result.body)
            code = result.headers.get('x-api-status-code')
            # Current documentation also shows status fields in JSON submit bodies.
            if code is None:
                try:
                    parsed = json.loads(result.body)
                    code = parsed.get('X-Api-Status-Code') if isinstance(parsed, dict) else None
                except (ValueError, UnicodeError):
                    pass
            status = ('complete' if code == '20000000' else 'partial'
                      if code in ('20000001', '20000002') else 'insufficient_evidence'
                      if code == '20000003' else 'failed')
            if result.status != 200:
                status = 'failed'
            metadata = audit.directory / 'response-status.json'
            immutable_json(metadata, {'http_status': result.status,
                                      'service_status': code if isinstance(code, str) and len(code) == 8 and code.isdigit() else None,
                                      'analysis_status': status})
            audit.finish(status, [raw, metadata], 'provider_error' if status == 'failed' else None)
            return status, raw
        except Exception:
            if not audit.finished:
                audit.finish('failed', failure_code='provider_error')
            raise ProviderFailure('Cloud request failed; resume by querying the saved job, do not blindly resubmit') from None

    def submit(self, source, publication):
        source = Path(source).resolve()
        if not source.is_relative_to(self.root):
            raise ProviderFailure('Source must be inside the evidence root')
        duration = canonical_audio(source)
        url, proof = publication.publish(source, self.root)
        job_dir = self.root / 'cloud-jobs' / ('JOB-' + uuid.uuid4().hex)
        if not job_dir.resolve().is_relative_to(self.root):
            raise ProviderFailure('Job directory must remain inside evidence root')
        job_dir.mkdir(parents=True, exist_ok=False)
        request_id = str(uuid.uuid4())
        job = job_dir / 'job.json'
        immutable_json(job, {'schema_version': '1.0.0', 'request_id': request_id,
            'resource_id': RESOURCE, 'config': CONFIG, 'duration_ms': duration,
            'source': source.relative_to(self.root).as_posix(), 'source_sha256': digest(source),
            'publication': proof.relative_to(self.root).as_posix(), 'publication_sha256': digest(proof)})
        # The durable UUID exists before submitting, including uncertain network outcomes.
        status, raw = self._call('submit', request_id, {'audio': {'url': url, 'format': 'wav',
            'rate': 16000, 'bits': 16, 'channel': 1, 'language': 'zh-CN'}, 'request': CONFIG},
            [source, proof, job], sensitive=(url,))
        task_id = request_id
        if status == 'complete':
            try:
                response = json.loads(raw.read_bytes())
                if isinstance(response, dict) and response.get('task_id') is not None:
                    task_id = str(uuid.UUID(response['task_id']))
            except Exception:
                raise ProviderFailure('Submit response task identity is invalid; retain job for review') from None
        immutable_json(job_dir / 'submission.json', {'status': status,
                       'task_id': task_id,
                       'native_sha256': digest(raw),
                       'native_response': raw.relative_to(self.root).as_posix()})
        return job, status

    def query(self, job_path):
        job_path = Path(job_path).resolve()
        if not job_path.is_relative_to(self.root):
            raise ProviderFailure('Job must be inside evidence root')
        job = load_document(job_path)
        if (job.get('schema_version') != '1.0.0' or job.get('resource_id') != RESOURCE
                or job.get('config') != CONFIG):
            raise ProviderFailure('Unsupported cloud job contract')
        try:
            uuid.UUID(job['request_id'])
            source = (self.root / job['source']).resolve()
            if not source.is_relative_to(self.root) or digest(source) != job['source_sha256']:
                raise ValueError()
            proof = (self.root / job['publication']).resolve()
            if not proof.is_relative_to(self.root) or digest(proof) != job['publication_sha256']:
                raise ValueError()
            if load_document(proof)['source_sha256'] != job['source_sha256']:
                raise ValueError()
        except Exception:
            raise ProviderFailure('Job source or request identity is invalid') from None
        inputs = [job_path, source, proof]
        request_id = job['request_id']
        receipt = job_path.parent / 'submission.json'
        if receipt.exists():
            submitted = load_document(receipt)
            if submitted.get('status') == 'failed':
                raise ProviderFailure('Submission was rejected; review before creating another job')
            native = (self.root / submitted['native_response']).resolve()
            if not native.is_relative_to(self.root) or digest(native) != submitted['native_sha256']:
                raise ProviderFailure('Submission evidence changed')
            response = json.loads(native.read_bytes())
            original_task = response.get('task_id', job['request_id']) if isinstance(response, dict) else job['request_id']
            if submitted['task_id'] != original_task:
                raise ProviderFailure('Query task identity differs from native submission evidence')
            request_id = str(uuid.UUID(submitted['task_id']))
            inputs.extend([receipt, native])
        return self._call('query', request_id, {}, inputs)


def main(argv=None):
    import argparse
    import os
    parser = argparse.ArgumentParser(description='Explicit cloud ASR jobs; raw provider output only')
    parser.add_argument('action', choices=['submit', 'query'])
    parser.add_argument('input', type=Path, help='Canonical WAV for submit, saved job.json for query')
    parser.add_argument('--root', type=Path, required=True, help='Local evidence root containing the input')
    args = parser.parse_args(argv)
    try:
        provider = VolcengineASRJobs(args.root, os.environ.get('VOLCENGINE_API_KEY'))
        if args.action == 'submit':
            publication = SignedURLPublication(os.environ.get('AIVOICEBENCH_AUDIO_PUT_URL', ''),
                os.environ.get('AIVOICEBENCH_AUDIO_GET_URL', ''), os.environ.get('AIVOICEBENCH_AUDIO_HOST', ''))
            path, status = provider.submit(args.input, publication)
        else:
            status, path = provider.query(args.input)
        print(json.dumps({'provider_status': status, 'artifact': str(path),
                          'note': 'Provider job status only; no acoustic events or device findings claimed'}))
        return 0 if status == 'complete' else 1 if status == 'failed' else 2
    except Exception:
        print('Cloud operation could not complete. Review retained local job/audit; credentials are not printed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
