"""Configured cloud ASR, native evidence first; no tester/device inference.

Contract verified 2026-09-10: docs/6561/2608628 (updated 2026-09-09).
URL publication is explicit. Never retry a potentially billable call silently.
"""
import json
import math
import os
import uuid
from pathlib import Path

from .asr import ProviderOutput
from .cloud_transport import (API, HTTPTransport, SignedURLPublication,
                              canonical_audio, closed_config)
from .providers import InvocationAudit, ProviderIdentity, ProviderFailure, immutable_json


class VolcengineASRProvider:
    transcript_version = '1.1.0'

    def __init__(self, root, api_key, *, model='bigmodel', endpoint=API,
                 resource_id='volc.bigasr.auc_turbo', timeout=300,
                 publication=None, transport=None):
        # This adapter implements one verified contract, not arbitrary URLs/models.
        if endpoint != API or model != 'bigmodel' or resource_id != 'volc.bigasr.auc_turbo':
            raise ProviderFailure('Unsupported ASR contract; configure the documented flash endpoint/model/resource')
        self.root, self.key = Path(root), api_key
        self.transport = transport or HTTPTransport(timeout)
        self.publication = publication
        # `show_utterances` is the documented property that exposes utterance-level
        # `additions` (where the speaker label appears). No separate speaker flag is
        # sent: the exact parameter name for enabling speaker separation in the
        # current service revision is not verified, and sending an unverified field
        # could be rejected or silently change the request contract.
        self.config = {'model_name': model, 'resource_id': resource_id,
                       'enable_itn': False, 'enable_punc': True, 'enable_ddc': False,
                       'show_utterances': True, 'timeout_seconds': timeout}
        self.profile = dict(provider='volcengine', model_id=model,
                            model_version='service-managed', model_sha256=None,
                            library_version='aivoicebench-volcengine-flash:1.1.0', config=self.config)

    def transcribe(self, mono_wav):
        # Configured signed URLs may address one object. Serialize publication +
        # recognition until the synchronous service has consumed that object.
        from .run_lock import run_lock
        with run_lock(self.root.parent / 'cloud-publication'):
            return self._transcribe(mono_wav)

    def _transcribe(self, mono_wav):
        canonical_audio(mono_wav)
        audit = InvocationAudit(self.root, ProviderIdentity('volcengine', 'bigmodel',
            'volcengine-flash:1.0.0', API, 'v3'), self.config, closed_config(self.config), [mono_wav],
            operation_id='OP-' + self.root.name + '-ASR', attempt=1+sum(
                json.loads(p.read_text(encoding='utf-8')).get('provider')=='volcengine'
                for p in (self.root/'provider-calls').glob('*/start.json')))
        outputs = []
        try:
            if not self.key:
                raise ProviderFailure('ASR credential missing')
            publication = self.publication or SignedURLPublication(
                os.environ.get('AIVOICEBENCH_AUDIO_PUT_URL', ''),
                os.environ.get('AIVOICEBENCH_AUDIO_GET_URL', ''),
                os.environ.get('AIVOICEBENCH_AUDIO_HOST', ''))
            if callable(publication):
                publication = publication()
            url, proof = publication.publish(mono_wav, self.root)
            outputs.append(proof)
            request_id = str(uuid.uuid4())
            request_path = audit.directory / 'request.json'
            immutable_json(request_path, {'request_id': request_id,
                'publication_ref': proof.relative_to(self.root).as_posix(),
                'audio_format': 'wav', 'request': {k:v for k,v in self.config.items()
                    if k not in ('resource_id', 'timeout_seconds')}})
            outputs.append(request_path)
            body = {'audio': {'url': url, 'format': 'wav', 'rate': 16000, 'bits': 16, 'channel': 1},
                    'request': json.loads(request_path.read_text())['request']}
            reply = self.transport.request('POST', API, {'Content-Type': 'application/json',
                'X-Api-Key': self.key, 'X-Api-Resource-Id': self.config['resource_id'],
                'X-Api-Request-Id': request_id, 'X-Api-Sequence': '-1'},
                json.dumps(body).encode())
            # A provider may echo credentials/URL. Retain only its digest in that case.
            import hashlib
            decoded = reply.body.decode('utf-8', errors='replace')
            try:
                decoded = json.dumps(json.loads(decoded), ensure_ascii=False)
            except ValueError:
                pass
            if any(secret and secret in decoded for secret in (self.key, url)):
                path = audit.directory / 'withheld-response.json'
                immutable_json(path, {'sha256': hashlib.sha256(reply.body).hexdigest(),
                    'reason': 'Response contains request credentials; native body withheld'})
                outputs.append(path)
                raise ProviderFailure('Unsafe native response')
            raw = audit.directory / 'native-response.json'
            with raw.open('xb') as stream:
                stream.write(reply.body)
            outputs.append(raw)
            code = reply.headers.get('x-api-status-code', '')
            metadata = audit.directory / 'response-status.json'
            immutable_json(metadata, {'http_status': reply.status,
                'status_code': code if isinstance(code,str) and code.isdigit() and len(code)==8 else None})
            outputs.append(metadata)
            if reply.status != 200 or code != '20000000':
                raise ProviderFailure('Cloud ASR rejected the request or reported no usable speech')
            message = reply.body.decode('utf-8')
            audit.finish('complete', outputs)
            return ProviderOutput(self.profile, [message])
        except Exception:
            if not audit.finished:
                audit.finish('failed', outputs, failure_code='provider_error')
            raise ProviderFailure('Cloud ASR failed; retained invocation evidence, no automatic resubmission') from None

    def normalize(self, messages, duration_ms):
        segments, gaps = [], []
        for index, raw in enumerate(messages):
            document = json.loads(raw)
            result = document.get('result')
            if not isinstance(result, dict) or not isinstance(result.get('text'), str):
                raise ProviderFailure('Invalid ASR result structure')
            utterances = result.get('utterances', [])
            if not isinstance(utterances, list):
                raise ProviderFailure('Invalid ASR utterances')
            if not utterances and not result['text'].strip():
                raise ProviderFailure('Cloud ASR returned no usable speech transcript')
            if not utterances and result['text'].strip():
                gaps.append(dict(raw_message_index=index, text=result['text'], reason='No timestamped utterances'))
            for u in utterances:
                if not isinstance(u, dict) or not isinstance(u.get('text'), str) or not u['text'].strip():
                    raise ProviderFailure('Invalid ASR utterance text')
                start, end = u.get('start_time'), u.get('end_time')
                if start is None or end is None:
                    gaps.append(dict(raw_message_index=index, text=u['text'], reason='Utterance timestamp missing'))
                    continue
                self._bounds(start, end, 0, duration_ms)
                words = []
                for w in u.get('words', []):
                    self._bounds(w.get('start_time'), w.get('end_time'), start, end)
                    confidence = w.get('confidence')
                    if confidence is not None and (type(confidence) not in (int,float) or
                            not math.isfinite(confidence) or not 0 <= confidence <= 1):
                        raise ProviderFailure('Invalid recognition confidence')
                    words.append(dict(text=w['text'], start_ms=w['start_time'], end_ms=w['end_time'],
                                      recognition_confidence=confidence))
                speaker = u.get('additions', {}).get('speaker')
                segments.append(dict(segment_id=f'ASR-{len(segments)+1:04d}', text=u['text'],
                    start_ms=start, end_ms=end, timestamp_source='asr_provider', timestamp_confidence=None,
                    speaker_id=str(speaker) if speaker is not None else None,
                    raw_message_index=index, words=words))
        return segments, gaps

    @staticmethod
    def _bounds(start, end, low, high):
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or not low <= start <= end <= high:
            raise ProviderFailure('ASR timestamp outside audio bounds')
