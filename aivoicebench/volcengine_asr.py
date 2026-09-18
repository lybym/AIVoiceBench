"""Configured cloud ASR, native evidence first; no tester/device inference.

Flash and Seed-standard contracts are closed adapter modes rather than arbitrary
URL pass-through. URL publication is explicit. Never retry a potentially
billable call silently. Interface-contract verification and real-call
verification are recorded separately; the Seed-standard speaker switch is not
sent to the Flash endpoint.
"""
import base64
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

from .asr import ProviderOutput
from .cloud_transport import (API, HTTPTransport, canonical_audio, closed_config)
from .model_settings import DEFAULT_INLINE_MAX_BYTES
from .providers import InvocationAudit, ProviderIdentity, ProviderFailure, immutable_json


FLASH_API = API
STANDARD_SUBMIT_API = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit'
STANDARD_QUERY_API = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/query'
STANDARD_POLL_SECONDS = 2


class PendingStandardJob(ProviderFailure):
    """The submitted Seed job is still queryable and must not be resubmitted."""

# The exact request properties this adapter is allowed to send, because each one
# is backed by an official documented property or example we could verify.
# Tests assert the outgoing body against this list, so an unverified field cannot
# be introduced without deliberately changing this declaration.
VERIFIED_REQUEST_FIELDS = ('model_name', 'enable_itn', 'enable_punc', 'enable_ddc', 'show_utterances')
SEED_STANDARD_REQUEST_FIELDS = VERIFIED_REQUEST_FIELDS + ('enable_speaker_info',)

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

SEED_STANDARD_CAPABILITIES = {
    'speaker_separation': {
        'interface_contract': 'verified',
        'read_path': 'utterances[].additions.speaker',
        'detail': ('Recording-file ASR supports automatic speaker separation; '
                   'enable_speaker_info is sent for the Seed ASR 2.0 standard contract.'),
        'real_call': 'verified',
    },
}


class VolcengineASRProvider:
    transcript_version = '1.1.0'

    def __init__(self, root, api_key, *, model='bigmodel', endpoint=API,
                 resource_id='volc.bigasr.auc_turbo', timeout=300,
                 publication=None, transport=None,
                 transport_config=None, storage_adapter_factory=None):
        # These are deliberately closed, documented contracts rather than an
        # arbitrary URL/resource pass-through.  Seed ASR 2.0 standard mode is
        # asynchronous (submit then query); flash is synchronous.
        contracts = {
            (FLASH_API, 'volc.bigasr.auc_turbo'): 'flash',
            (STANDARD_SUBMIT_API, 'volc.seedasr.auc'): 'seed_standard',
        }
        self.mode = contracts.get((endpoint, resource_id))
        if model != 'bigmodel' or self.mode is None:
            raise ProviderFailure('Unsupported ASR contract; configure a documented flash or Seed standard endpoint/model/resource')
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
        self.endpoint, self.query_endpoint = endpoint, (STANDARD_QUERY_API if self.mode == 'seed_standard' else None)
        self.config = {'model_name': model, 'resource_id': resource_id,
                       'enable_itn': False, 'enable_punc': True, 'enable_ddc': False,
                       'show_utterances': True, 'timeout_seconds': timeout}
        if self.mode == 'seed_standard':
            # The recording-file API documents automatic speaker separation and
            # exposes this request switch in its official example.  This is the
            # evidence needed by the downstream role-attribution stage; it still
            # never guesses tester/device roles from a speaker number.
            self.config['enable_speaker_info'] = True
        self.profile = dict(provider='volcengine', model_id=model,
                            model_version='service-managed', model_sha256=None,
                            library_version='aivoicebench-volcengine-file-asr:1.2.0', config=self.config,
                            interface_contract_verified=list(SEED_STANDARD_REQUEST_FIELDS if self.mode == 'seed_standard' else VERIFIED_REQUEST_FIELDS),
                            capability_contract_pending=json.loads(json.dumps(
                                SEED_STANDARD_CAPABILITIES if self.mode == 'seed_standard' else UNVERIFIED_CAPABILITIES)))

    def transcribe(self, mono_wav):
        # Configured signed URLs may address one object. Serialize publication +
        # recognition until the synchronous service has consumed that object.
        from .run_lock import run_lock
        with run_lock(self.root.parent / 'cloud-publication'):
            pending = self._pending_standard_audit()
            if pending is not None:
                return self._recover_standard_job(pending)
            return self._transcribe(mono_wav)

    def _pending_standard_audit(self):
        """Find one locally recorded Seed submission without a final result.

        Standard ASR is submit-then-poll.  If a worker is interrupted after the
        submit succeeds, recovering that request ID is safer than submitting the
        same recording again.
        """
        if self.mode != 'seed_standard':
            return None
        for directory in (self.root / 'provider-calls').glob('CALL-*'):
            request = directory / 'request.json'
            if (directory / 'result.json').exists() or not request.exists():
                continue
            try:
                snapshot = json.loads(request.read_text(encoding='utf-8'))
                start = json.loads((directory / 'start.json').read_text(encoding='utf-8'))
                if snapshot.get('request_id') and start.get('config', {}).get('resource_id') == self.config['resource_id']:
                    return directory, snapshot['request_id'], start
            except (OSError, ValueError, KeyError):
                continue
        return None

    def _recover_standard_job(self, pending):
        directory, request_id, record = pending
        audit = object.__new__(InvocationAudit)
        audit.root, audit.directory, audit.record = self.root, directory, record
        audit.started, audit.finished = time.monotonic_ns(), False
        outputs = [p for p in directory.iterdir() if p.is_file() and p.name not in ('start.json', 'result.json')]
        try:
            headers = {'Content-Type': 'application/json', 'X-Api-Key': self.key,
                       'X-Api-Resource-Id': self.config['resource_id'],
                       'X-Api-Request-Id': request_id, 'X-Api-Sequence': '-1'}
            prior = len(list(directory.glob('native-query-*.json')))
            reply = self._poll_standard(headers, request_id, audit, outputs, prior)
            raw = directory / 'native-response.json'
            with raw.open('xb') as stream:
                stream.write(reply.body)
            outputs.append(raw)
            status = directory / 'response-status.json'
            immutable_json(status, {'http_status': reply.status, 'status_code': reply.headers.get('x-api-status-code')})
            outputs.append(status)
            audit.finish('complete', outputs)
            return ProviderOutput(self.profile, [reply.body.decode('utf-8')])
        except PendingStandardJob:
            # Keep the invocation pending (no immutable result.json) so a later
            # resume queries the same request ID instead of submitting audio again.
            raise
        except Exception:
            if not audit.finished:
                audit.finish('failed', outputs, failure_code='provider_error')
            raise ProviderFailure('Cloud ASR recovery failed; retained existing invocation evidence') from None

    def _transcribe(self, mono_wav):
        canonical_audio(mono_wav)
        audit = InvocationAudit(self.root, ProviderIdentity('volcengine', 'bigmodel',
            'volcengine-file-asr:1.2.0', self.endpoint, 'v3'), self.config, closed_config(self.config), [mono_wav],
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
            headers = {'Content-Type': 'application/json',
                'X-Api-Key': self.key, 'X-Api-Resource-Id': self.config['resource_id'],
                'X-Api-Request-Id': request_id, 'X-Api-Sequence': '-1'}
            reply = self.transport.request('POST', self.endpoint, headers, json.dumps(body).encode())
            if self.mode == 'seed_standard':
                self._require_success(reply, 'submit')
                submit_path = audit.directory / 'native-submit-response.json'
                with submit_path.open('xb') as stream:
                    stream.write(reply.body)
                outputs.append(submit_path)
                reply = self._poll_standard(headers, request_id, audit, outputs)
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
            self._require_success(reply, 'recognition')
            message = reply.body.decode('utf-8')
            audit.finish('complete', outputs)
            return ProviderOutput(self.profile, [message])
        except PendingStandardJob:
            # Poll budget exhaustion is not a terminal provider failure. Leaving
            # the audit pending is the durable recovery signal for the next run.
            raise
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

    @staticmethod
    def _require_success(reply, operation):
        code = reply.headers.get('x-api-status-code', '')
        if reply.status != 200 or code != '20000000':
            raise ProviderFailure(f'Cloud ASR {operation} rejected the request or reported no usable speech')

    def _poll_standard(self, headers, request_id, audit, outputs, prior_polls=0):
        """Poll one explicitly submitted Seed ASR job; never resubmit audio."""
        max_polls = max(1, int(math.ceil(self.config['timeout_seconds'] / STANDARD_POLL_SECONDS)))
        query_headers = dict(headers)
        query_headers['X-Api-Request-Id'] = request_id
        for attempt in range(prior_polls + 1, prior_polls + max_polls + 1):
            if attempt > 1:
                time.sleep(STANDARD_POLL_SECONDS)
            reply = self.transport.request('POST', self.query_endpoint, query_headers, b'{}')
            path = audit.directory / f'native-query-{attempt:03d}.json'
            with path.open('xb') as stream:
                stream.write(reply.body)
            outputs.append(path)
            code = reply.headers.get('x-api-status-code', '')
            if reply.status == 200 and code == '20000000':
                return reply
            if reply.status == 200 and code in ('20000001', '20000002'):
                continue
            raise ProviderFailure('Cloud ASR query rejected the submitted job')
        raise PendingStandardJob(
            'Cloud ASR standard job is still pending after the configured poll window; '
            'resume will query the same request ID')

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
                word_timing_valid = True
                previous_word_end = start
                for w in u.get('words', []):
                    if not isinstance(w, dict) or not isinstance(w.get('text'), str) or not w['text'].strip():
                        word_timing_valid = False
                        break
                    word_start, word_end = w.get('start_time'), w.get('end_time')
                    try:
                        self._bounds(word_start, word_end, start, end)
                    except ProviderFailure:
                        word_timing_valid = False
                        break
                    confidence = w.get('confidence')
                    if confidence is not None and (type(confidence) not in (int,float) or
                            not math.isfinite(confidence) or not 0 <= confidence <= 1):
                        word_timing_valid = False
                        break
                    if word_start < previous_word_end:
                        word_timing_valid = False
                        break
                    words.append(dict(text=w['text'], start_ms=word_start, end_ms=word_end,
                                      recognition_confidence=confidence))
                    previous_word_end = word_end
                if not word_timing_valid:
                    # A successful provider response can contain a valid utterance span but
                    # placeholder word offsets such as -1.  Preserve the provider's
                    # utterance-level text/timing and make the unavailable word timing
                    # explicit, rather than rejecting the whole transcription.
                    words = []
                    gaps.append(dict(
                        raw_message_index=index,
                        text=u['text'],
                        reason='Provider returned invalid word timestamps; retained utterance text without word timing',
                    ))
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
