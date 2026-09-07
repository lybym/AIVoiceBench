"""Provider-neutral, append-only invocation evidence; no network or retry policy."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Protocol
from urllib.parse import urlsplit
import uuid

from jsonschema import Draft202012Validator

from .runner import digest
from .validation import load_document, schema_errors


class ProviderFailure(RuntimeError):
    """Safe public error. Never embeds provider exceptions or credentials."""


class LLMProvider(Protocol):
    def decide(self, context_refs: list[str], output_schema: dict): ...


class TTSProvider(Protocol):
    def synthesize(self, text: str, destination: Path): ...


class DiarizationProvider(Protocol):
    def diarize(self, audio: Path): ...


# ASRProvider remains in asr.py; AudioProcessingProvider remains in the import
# implementation (PR #29). Do not introduce competing copies of those contracts.


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def immutable_json(path, value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded + '\n')


def _credential_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            compact = re.sub(r'[^a-z]', '', str(key).lower())
            if any(term in compact for term in ('secret', 'password', 'credential', 'authorization', 'apikey', 'accesstoken', 'signedurl')):
                raise ValueError('Credential-bearing configuration is not an audit input')
            _credential_keys(child)
    elif isinstance(value, list):
        for child in value:
            _credential_keys(child)


@dataclass(frozen=True)
class ProviderIdentity:
    provider: str
    model: str
    processor_version: str
    endpoint: str | None = None
    api_version: str | None = None
    prompt_version: str | None = None


class InvocationAudit:
    """One directory per attempt; start survives crashes, result never replaces it.

    The orchestrator owns operation IDs/retry decisions. Config schema is trusted
    adapter code, never model output. It must reject unspecified properties.
    Input/output refs address files relative to evidence_root, not remote URLs.
    """

    def __init__(self, evidence_root, identity, config, config_schema, input_paths,
                 *, operation_id=None, attempt=1):
        self.root = Path(evidence_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate_config_schema(config_schema)
        _credential_keys(config)
        if list(Draft202012Validator(config_schema).iter_errors(config)):
            raise ValueError('Provider config does not match its explicit schema')
        # JSON serialization rejects nonfinite values and detaches mutable inputs.
        config = json.loads(json.dumps(config, allow_nan=False))
        if identity.endpoint is not None:
            endpoint = urlsplit(identity.endpoint)
            if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError('Audit endpoint must be HTTPS without credentials, query or fragment')
        self.started = time.monotonic_ns()
        self.record = {
            'schema_version': '1.0.0', 'invocation_id': 'CALL-' + uuid.uuid4().hex,
            'operation_id': operation_id or 'OP-' + uuid.uuid4().hex, 'attempt': attempt,
            'provider': identity.provider, 'model': identity.model,
            'processor_version': identity.processor_version, 'endpoint': identity.endpoint,
            'api_version': identity.api_version, 'prompt_version': identity.prompt_version,
            'config': config, 'config_schema': json.loads(json.dumps(config_schema, allow_nan=False)),
            'started_at': utc_now(), 'finished_at': None,
            'latency_ms': None, 'status': 'pending', 'failure_code': None,
            'input_artifacts': [self.artifact(path) for path in input_paths],
            'output_artifacts': [],
        }
        self._validate(self.record)
        self.directory = self.root / 'provider-calls' / self.record['invocation_id']
        if not self.directory.resolve().is_relative_to(self.root):
            raise ValueError('Provider audit directory must remain inside evidence root')
        self.directory.mkdir(parents=True, exist_ok=False)
        immutable_json(self.directory / 'start.json', self.record)
        self.finished = False

    @staticmethod
    def _validate_config_schema(schema):
        Draft202012Validator.check_schema(schema)
        if schema.get('type') != 'object' or schema.get('additionalProperties') is not False:
            raise ValueError('Provider config schema must be a closed object')
        # Nested structures need equally explicit contracts; no open payload bags.
        def inspect(node):
            if isinstance(node, dict):
                if node.get('type') == 'object' and node.get('additionalProperties') is not False:
                    raise ValueError('Nested config objects must reject unknown fields')
                for child in node.values():
                    inspect(child)
            elif isinstance(node, list):
                for child in node:
                    inspect(child)
        inspect(schema)

    @staticmethod
    def _validate(record):
        if schema_errors(record, 'provider-invocation'):
            raise ValueError('Invalid provider invocation contract')

    def artifact(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError('Provider artifact must be an existing contained file')
        return {'path': resolved.relative_to(self.root).as_posix(), 'sha256': digest(resolved),
                'size_bytes': resolved.stat().st_size}

    def finish(self, status, output_paths=(), failure_code=None):
        if self.finished:
            raise ValueError('Provider result is immutable')
        if status == 'pending':
            raise ValueError('Pending calls have no terminal record')
        for artifact in self.record['input_artifacts']:
            if self.artifact(artifact['path']) != artifact:
                raise ValueError('Provider input changed during invocation')
        result = dict(self.record, status=status, failure_code=failure_code,
                      finished_at=utc_now(), latency_ms=(time.monotonic_ns() - self.started) / 1_000_000,
                      output_artifacts=[self.artifact(path) for path in output_paths])
        self._validate(result)
        immutable_json(self.directory / 'result.json', result)
        self.finished = True
        return result


def invocation_errors(record, evidence_root):
    """Validate structure and current file integrity when consuming audit evidence."""
    errors = schema_errors(record, 'provider-invocation')
    if errors:
        return errors
    try:
        InvocationAudit._validate_config_schema(record['config_schema'])
        _credential_keys(record['config'])
        if list(Draft202012Validator(record['config_schema']).iter_errors(record['config'])):
            errors.append('Provider config violates recorded schema')
    except Exception:
        errors.append('Invalid provider configuration contract')
    root = Path(evidence_root).resolve()
    for artifact in record['input_artifacts'] + record['output_artifacts']:
        path = (root / artifact['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append('Missing or uncontained provider evidence')
        elif path.stat().st_size != artifact['size_bytes'] or digest(path) != artifact['sha256']:
            errors.append('Provider evidence hash/size mismatch')
    return errors


def read_invocation(directory, evidence_root):
    """Read one attempt, validating immutable start/result linkage and artifacts."""
    root, directory = Path(evidence_root).resolve(), Path(directory).resolve()
    if not directory.is_relative_to(root):
        raise ValueError('Invocation directory is outside evidence root')
    for filename in ('start.json', 'result.json'):
        if not (directory / filename).resolve().is_relative_to(root):
            raise ValueError('Invocation document is outside evidence root')
    start = load_document(directory / 'start.json')
    if invocation_errors(start, root) or start['status'] != 'pending' or directory.name != start['invocation_id']:
        raise ValueError('Invalid invocation start evidence')
    result_path = directory / 'result.json'
    if not result_path.exists():
        return start
    result = load_document(result_path)
    if invocation_errors(result, root) or result['status'] == 'pending':
        raise ValueError('Invalid invocation result evidence')
    terminal_fields = {'status', 'finished_at', 'latency_ms', 'failure_code', 'output_artifacts'}
    if any(result[key] != value for key, value in start.items() if key not in terminal_fields):
        raise ValueError('Invocation result does not match immutable start')
    return result
