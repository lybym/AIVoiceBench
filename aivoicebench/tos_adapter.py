"""Volcengine TOS object-storage adapter for File ASR URL transport.

Object storage is an **implementation transport**, not an Evidence source.
The backend uploads a private object directly through the storage SDK/adapter
and only hands a time-bounded Presigned GET URL to the File ASR provider when
URL transport is required.  Signed URLs and storage credentials never enter
Run snapshots, reports, logs, or the repository.

The real ``tos`` SDK is imported lazily so that CI and local development
without Volcengine credentials can still exercise the adapter contract through
an injected client.  Real TOS calls and authorized recordings are recorded
separately under #22/#85 acceptance; software tests prove the contract only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .providers import InvocationAudit, ProviderIdentity, ProviderFailure, immutable_json
from .runner import digest


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _safe_key(prefix: str, run_id: str | None) -> str:
    """Build a collision-resistant private object key inside the configured prefix."""
    token = uuid.uuid4().hex
    run_segment = re.sub(r'[^A-Za-z0-9_.-]', '', run_id or '')[:64] or 'standalone'
    base = prefix.rstrip('/') + '/' if prefix else ''
    return f'{base}{run_segment}/{token}.wav'


class StorageClient(Protocol):
    """Minimal storage client contract the adapter depends on.

    The real ``tos.TosClient`` satisfies this.  Tests inject a double that
    implements the same methods without network access.
    """

    def put_object(self, bucket: str, key: str, content: bytes) -> dict: ...
    def pre_signed_url(self, method: str, bucket: str, key: str, expires: int) -> str: ...
    def delete_object(self, bucket: str, key: str) -> dict: ...


class StorageAdapter(Protocol):
    """Object-storage transport adapter used by File ASR URL transport."""

    store_id: str

    def upload(self, source: Path, root: Path) -> tuple[str, Path]:
        """Upload a private object; return (object_key, audit_proof_path)."""
        ...

    def presigned_get(self, object_key: str) -> str:
        """Return a short-lived Presigned GET URL for the private object."""
        ...

    def cleanup(self, object_key: str, root: Path) -> dict:
        """Delete the private object; return an auditable cleanup status."""
        ...


@dataclass(frozen=True)
class TOSStorageConfig:
    """Validated, secret-free description of one TOS storage store."""

    id: str
    endpoint: str
    region: str
    bucket: str
    prefix: str
    access_key_env: str
    secret_key_env: str
    session_token_env: str
    presigned_get_ttl_seconds: int
    delete_after_use: bool
    lifecycle_max_age_hours: int


def validate_storage_store(raw: dict) -> dict:
    """Validate and normalize one storage store entry.

    Credential values are never accepted here — only environment-variable
    references.  This keeps the file safe for Git and audit.
    """
    if not isinstance(raw, dict):
        raise ValueError('storage store must be a mapping')
    allowed = {'id', 'provider', 'endpoint', 'region', 'bucket', 'prefix',
               'credentials', 'publication'}
    extra = set(raw) - allowed
    if extra:
        raise ValueError(f'storage store has unsupported fields: {sorted(extra)}')
    store = dict(raw)
    for key in ('id', 'provider', 'endpoint', 'region', 'bucket'):
        value = store.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f'storage {key} must be a non-empty string')
    if store['provider'] != 'tos':
        raise ValueError('only the tos object-storage provider is supported')
    parsed_endpoint = re.match(r'^https?://[A-Za-z0-9.-]+$', store['endpoint'])
    if not parsed_endpoint:
        raise ValueError('storage endpoint must be an http(s) URL without credentials or path')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', store['bucket']):
        raise ValueError('invalid storage bucket name')
    prefix = store.get('prefix', '')
    if not isinstance(prefix, str) or len(prefix) > 512:
        raise ValueError('invalid storage prefix')
    # Reject anything that looks like a credential value in the file.
    for key, value in store.items():
        _reject_secret_value(key, value)
    credentials = store.get('credentials')
    if not isinstance(credentials, dict):
        raise ValueError('storage credentials must be a mapping of env references')
    cred_fields = {'access_key_env', 'secret_key_env', 'session_token_env'}
    extra_cred = set(credentials) - cred_fields
    if extra_cred:
        raise ValueError(f'storage credentials has unsupported fields: {sorted(extra_cred)}')
    access_key_env = credentials.get('access_key_env', '')
    secret_key_env = credentials.get('secret_key_env', '')
    session_token_env = credentials.get('session_token_env', '')
    for name, env in (('access_key_env', access_key_env), ('secret_key_env', secret_key_env)):
        if not isinstance(env, str) or not env or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', env):
            raise ValueError(f'storage {name} must be a valid environment-variable name')
    if session_token_env and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', session_token_env):
        raise ValueError('storage session_token_env must be a valid environment-variable name')
    publication = store.get('publication', {})
    if not isinstance(publication, dict):
        raise ValueError('storage publication must be a mapping')
    pub_fields = {'presigned_get_ttl_seconds', 'delete_after_use', 'lifecycle_max_age_hours'}
    extra_pub = set(publication) - pub_fields
    if extra_pub:
        raise ValueError(f'storage publication has unsupported fields: {sorted(extra_pub)}')
    ttl = publication.get('presigned_get_ttl_seconds', 3600)
    if type(ttl) is not int or not 60 <= ttl <= 86400:
        raise ValueError('presigned_get_ttl_seconds must be an integer between 60 and 86400')
    delete_after_use = publication.get('delete_after_use', True)
    if type(delete_after_use) is not bool:
        raise ValueError('delete_after_use must be a boolean')
    lifecycle = publication.get('lifecycle_max_age_hours', 24)
    if type(lifecycle) is not int or not 1 <= lifecycle <= 720:
        raise ValueError('lifecycle_max_age_hours must be an integer between 1 and 720')
    store['prefix'] = prefix
    store['publication'] = {'presigned_get_ttl_seconds': ttl,
                            'delete_after_use': delete_after_use,
                            'lifecycle_max_age_hours': lifecycle}
    store['credentials'] = {'access_key_env': access_key_env,
                            'secret_key_env': secret_key_env,
                            'session_token_env': session_token_env}
    return store


def _reject_secret_value(key, value):
    """Refuse anything that looks like an inline secret in the storage file."""
    compact = re.sub(r'[^a-z]', '', str(key).lower())
    if any(term in compact for term in ('secret', 'password', 'credential', 'token', 'apikey', 'accesskey')):
        if isinstance(value, str) and value and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}', value):
            raise ValueError(f'credential value must not appear in storage config; use an *_env reference for {key}')


class TOSStorageAdapter:
    """Private upload + Presigned GET + cleanup for File ASR URL transport.

    The real ``tos`` SDK is imported lazily.  A ``client_factory`` may be
    injected for testing; when omitted the adapter constructs a ``TosClient``
    from the resolved credentials at first use.

    Credentials are resolved from the environment at call time and never
    persisted.  The Presigned GET URL is returned only to the ASR provider
    in memory; it is never written to Run snapshots or logs.
    """

    def __init__(self, config: TOSStorageConfig, *, client_factory=None):
        self.config = config
        self._client_factory = client_factory
        self._client = None

    @property
    def store_id(self) -> str:
        return self.config.id

    def _resolve_credentials(self):
        access_key = os.environ.get(self.config.access_key_env, '')
        secret_key = os.environ.get(self.config.secret_key_env, '')
        session_token = os.environ.get(self.config.session_token_env, '') or None
        if not access_key or not secret_key:
            raise ProviderFailure(
                f'TOS credentials missing for store {self.config.id}; '
                f'set {self.config.access_key_env} and {self.config.secret_key_env}')
        return access_key, secret_key, session_token

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        # Lazy import: the tos SDK is an optional deployment dependency.
        try:
            import tos  # type: ignore
        except ImportError:
            raise ProviderFailure(
                'The Volcengine TOS SDK (tos) is not installed; install it or '
                'use inline transport for eligible recordings') from None
        access_key, secret_key, session_token = self._resolve_credentials()
        self._client = tos.TosClient(access_key, secret_key, self.config.endpoint,
                                     self.config.region, session_token=session_token)
        return self._client

    def upload(self, source: Path, root: Path) -> tuple[str, Path]:
        """Upload a private TOS object and return (object_key, audit_proof_path).

        The proof records transport metadata (object key, source hash, store id)
        without persisting the Presigned URL or any credential.  It is created
        before the GET URL is generated so a failure after upload is still
        auditable.
        """
        from .cloud_transport import canonical_audio
        canonical_audio(source)
        data = Path(source).read_bytes()
        object_key = _safe_key(self.config.prefix, root.name)
        config = {'method': 'tos_put', 'store_id': self.config.id,
                  'endpoint': self.config.endpoint, 'region': self.config.region,
                  'bucket': self.config.bucket, 'object_key': object_key}
        audit = InvocationAudit(
            root, ProviderIdentity('tos', 'object', 'tos-transport-1.0.0',
                                   self.config.endpoint), config,
            _closed_config(config), [source])
        proof = audit.directory / 'storage-upload.json'
        try:
            client = self._get_client()
            client.put_object(self.config.bucket, object_key, data)
            immutable_json(proof, {
                'schema_version': '1.0.0', 'status': 'uploaded',
                'store_id': self.config.id, 'object_key': object_key,
                'source_sha256': hashlib.sha256(data).hexdigest(),
                'size_bytes': len(data),
                'lifecycle_max_age_hours': self.config.lifecycle_max_age_hours})
            audit.finish('complete', [proof])
            return object_key, proof
        except Exception:
            if not audit.finished:
                audit.finish('failed', failure_code='storage_error')
            raise ProviderFailure('TOS upload failed; inspect invocation evidence') from None

    def presigned_get(self, object_key: str) -> str:
        """Generate a short-lived Presigned GET URL for the private object.

        The URL is returned only to the caller (the ASR provider) and is never
        persisted.  The TTL comes from the validated storage config.
        """
        client = self._get_client()
        url = client.pre_signed_url('GET', self.config.bucket, object_key,
                                    self.config.presigned_get_ttl_seconds)
        if not isinstance(url, str) or not url.startswith('https://'):
            raise ProviderFailure('TOS presigned URL generation did not return a valid HTTPS URL')
        return url

    def cleanup(self, object_key: str, root: Path) -> dict:
        """Delete the private object and return an auditable cleanup status.

        Cleanup is best-effort: a failure is recorded but does not suppress the
        ASR result.  The bucket lifecycle policy is the backstop.  Unlike
        upload, cleanup has no input artifact to audit through the full
        provider-invocation contract, so it returns a status dict that the
        caller writes alongside the ASR invocation evidence.
        """
        try:
            client = self._get_client()
            client.delete_object(self.config.bucket, object_key)
            return {'schema_version': '1.0.0', 'status': 'deleted',
                    'object_key': object_key, 'store_id': self.config.id,
                    'backstop': 'lifecycle' if not self.config.delete_after_use else 'explicit'}
        except Exception:
            return {'schema_version': '1.0.0', 'status': 'cleanup_failed',
                    'object_key': object_key, 'store_id': self.config.id,
                    'backstop': f'lifecycle_max_age_hours={self.config.lifecycle_max_age_hours}'}


def _closed_config(config):
    return {'type': 'object', 'additionalProperties': False, 'required': list(config),
            'properties': {key: {'const': value} for key, value in config.items()}}
