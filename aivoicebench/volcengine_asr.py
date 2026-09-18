"""Configured cloud ASR, native evidence first; no tester/device inference.

Contract verified 2026-09-10: docs/6561/2608628 (updated 2026-09-09).
URL publication is explicit. Never retry a potentially billable call silently.

Interface-contract verification and real-call verification are recorded
separately. `show_utterances` is a verified request property; whether speaker
separation needs its own request flag is NOT verified, so no such flag is sent
(see UNVERIFIED_CAPABILITIES). Sending an unknown field could be rejected or
silently change the request contract.
"""
import base64
import hashlib
import json
import math
import os
import uuid
from pathlib import Path

from .asr import ProviderOutput
from .cloud_transport import (API, HTTPTransport, canonical_audio, closed_config)
from .model_settings import DEFAULT_INLINE_MAX_BYTES
from .providers import InvocationAudit, ProviderIdentity, ProviderFailure, immutable_json

# The exact request properties this adapter is allowed to send, because each one
# is backed by an official documented property or example we could verify.
# Tests assert the outgoing body against this list, so an unverified field cannot
# be introduced without deliberately changing this declaration.
VERIFIED_REQUEST_FIELDS = ('model_name', 'enable_itn', 'enable_punc', 'enable_ddc', 'show_utterances')

# Capabilities whose request contract is NOT verified. Absence of a verified flag
# is not evidence that the capability is unsupported; it is evidence that we have
# not confirmed how to ask for it.
UNVERIFIED_CAPABILITIES = {
    'speaker_separation': {
        'interface_contract': 'pending',
        'read_path': 'utterances[].additions.speaker',
        'detail': (
            'Not verified: whether the current service revision requires an explicit '
            'request property to enable speaker separation, the exact property name, the '
            'label field type and its unknown value, the time unit and boundary meaning of '
            'the label interval, and whether this model/resource type supports the '
            'capability. The official parameter tables are JS-rendered and did not render '
            'as text when checked on 2026-09-11, so reading the label path remains an '
            'unverified code fact and no unverified request property is sent.'),
        'real_call': 'not_attempted',
    },
}


class VolcengineASRProvider:
    transcript_version = '1.1.0'

    def __init__(self, root, api_key, *, model='bigmodel', endpoint=API,
                 resource_id='volc.bigasr.auc_turbo', timeout=300,
                 publication=None, transport=None,
                 transport_config=None, storage_adapter_factory=None):
        # This adapter implements one verified contract, not arbitrary URLs/models.
        if endpoint != API or model != 'bigmodel' or resource_id != 'volc.bigasr.auc_turbo':
            raise ProviderFailure('Unsupported ASR contract; configure the documented flash endpoint/model/resource')
        self.root, self.key = Path(root), api_key
        self.transport = transport or HTTPTransport(timeout)
        # transport_config selects inline Base64 vs object-storage URL transport
        # (PRD-F005/F016, Issue #87).  When absent, defaults to auto with the
        # engineering threshold; the legacy SignedURLPublication path is used
        # only as a migration fallback when no storage adapter is configured.
        self.transport_config = transport_config or {
            'audio_transport': 'auto', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES}
        self.storage_adapter_factory = storage_adapter_factory
        self.publication = publication
        self.config = {'model_name': model, 'resource_id': resource_id,
                       'enable_itn': False, 'enable_punc': True, 'enable_ddc': False,
                       'show_utterances': True, 'timeout_seconds': timeout}
        self.profile = dict(provider='volcengine', model_id=model,
                            model_version='service-managed', model_sha256=None,
                            library_version='aivoicebench-volcengine-flash:1.1.0', config=self.config,
                            interface_contract_verified=list(VERIFIED_REQUEST_FIELDS),
                            capability_contract_pending=json.loads(json.dumps(UNVERIFIED_CAPABILITIES)))

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
        cleanup = None
        try:
            if not self.key:
                raise ProviderFailure('ASR credential missing')
            audio_field, transport_meta = self._prepare_audio(mono_wav, audit, outputs)
            cleanup = transport_meta.get('cleanup')
            request_id = str(uuid.uuid4())
            request_path = audit.directory / 'request.json'
            immutable_json(request_path, {'request_id': request_id,
                'transport': transport_meta['mode'],
                'audio_transport_config': dict(self.transport_config),
                'audio_format': 'wav', 'request': {k:v for k,v in self.config.items()
                    if k not in ('resource_id', 'timeout_seconds')}})
            outputs.append(request_path)
            body = {'audio': dict(audio_field, format='wav', rate=16000, bits=16, channel=1),
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
            secret_echoes = [self.key] + ([audio_field.get('url')] if 'url' in audio_field else [])
            if any(secret and secret in decoded for secret in secret_echoes):
                path = audit.directory / 'withheld-response.json'
                immutable_json(path, {'sha256': hashlib.sha256(reply.body).hexdigest(),
                    'reason': 'Response contains request credentials or transport URL; native body withheld'})
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
        finally:
            if cleanup is not None:
                adapter, object_key = cleanup
                try:
                    cleanup_status = adapter.cleanup(object_key, self.root)
                    transport_proof = audit.directory / 'transport-cleanup.json'
                    immutable_json(transport_proof, cleanup_status)
                except Exception:
                    pass  # lifecycle policy is the backstop; do not mask the ASR result

    def _prepare_audio(self, mono_wav, audit, outputs):
        """Select inline Base64 or object-storage URL transport.

        Returns ``(audio_field, transport_meta)`` where ``audio_field`` is
        ``{'data': base64}`` or ``{'url': presigned_get}``, and
        ``transport_meta`` carries the auditable transport choice.  The
        presigned URL stays in memory only; it never enters outputs or logs.
        """
        mode = self.transport_config.get('audio_transport', 'auto')
        inline_max = self.transport_config.get('inline_max_bytes', DEFAULT_INLINE_MAX_BYTES)
        size = Path(mono_wav).stat().st_size
        use_inline = mode == 'inline' or (mode == 'auto' and size <= inline_max)
        if use_inline:
            return self._inline_audio(mono_wav, audit, outputs, mode)
        return self._object_storage_audio(mono_wav, audit, outputs, mode)

    def _inline_audio(self, mono_wav, audit, outputs, requested_mode):
        """Encode canonical WAV as Base64 audio.data (no object storage needed)."""
        data = Path(mono_wav).read_bytes()
        encoded = base64.b64encode(data).decode('ascii')
        proof = audit.directory / 'transport.json'
        immutable_json(proof, {'schema_version': '1.0.0',
            'transport': 'inline', 'requested_mode': requested_mode,
            'size_bytes': len(data),
            'source_sha256': hashlib.sha256(data).hexdigest()})
        outputs.append(proof)
        return {'data': encoded}, {'mode': 'inline', 'cleanup': None}

    def _object_storage_audio(self, mono_wav, audit, outputs, requested_mode):
        """Upload a private TOS object and return a short-lived Presigned GET URL.

        The presigned URL is returned only to the caller and never persisted.
        Transport preference: TOS adapter (external config) → legacy
        ``publication`` → legacy env-var signed URLs.  When none is configured
        the request fails so inline transport can remain available for eligible
        recordings (PRD-F005, Issue #87).
        """
        if self.storage_adapter_factory is not None:
            adapter = self.storage_adapter_factory(self.root)
            object_key, upload_proof = adapter.upload(mono_wav, self.root)
            outputs.append(upload_proof)
            url = adapter.presigned_get(object_key)
            proof = audit.directory / 'transport.json'
            immutable_json(proof, {'schema_version': '1.0.0',
                'transport': 'object_storage', 'requested_mode': requested_mode,
                'storage_adapter': adapter.store_id, 'object_key': object_key,
                'source_sha256': hashlib.sha256(Path(mono_wav).read_bytes()).hexdigest()})
            outputs.append(proof)
            return {'url': url}, {'mode': 'object_storage', 'cleanup': (adapter, object_key)}
        # Legacy publication: caller-supplied or env-var signed PUT/GET URLs.
        from .cloud_transport import SignedURLPublication
        publication = self.publication
        if publication is None:
            legacy = tuple(os.environ.get(k, '') for k in
                           ('AIVOICEBENCH_AUDIO_PUT_URL', 'AIVOICEBENCH_AUDIO_GET_URL', 'AIVOICEBENCH_AUDIO_HOST'))
            if all(legacy):
                publication = SignedURLPublication(*legacy)
        if publication is not None:
            publication = publication() if callable(publication) else publication
            url, proof = publication.publish(mono_wav, self.root)
            outputs.append(proof)
            transport_proof = audit.directory / 'transport.json'
            immutable_json(transport_proof, {'schema_version': '1.0.0',
                'transport': 'object_storage_legacy', 'requested_mode': requested_mode,
                'source': 'signed_url_publication'})
            outputs.append(transport_proof)
            return {'url': url}, {'mode': 'object_storage_legacy', 'cleanup': None}
        raise ProviderFailure(
            'Object-storage transport requires a storage adapter or legacy publication, '
            'but none is configured; use inline transport for eligible recordings')

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
