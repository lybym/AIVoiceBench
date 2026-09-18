"""Import Run storage and append-only artifact catalog, reusing shared hash utilities."""

from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath, PurePosixPath
import time
import uuid

from .audio_processing import AudioProcessingError, FORMATS, MAX_INPUT_BYTES
from .runner import digest, write_json, contained_file
from .validation import schema_errors

STAGES = ('ingestion', 'normalization', 'audio_qa', 'asr', 'acoustic', 'diarization',
          'attribution', 'fusion', 'turns', 'timeline', 'metrics', 'judge', 'findings', 'report')
PROFILE_KEYS = ('device', 'hardware', 'firmware', 'model', 'prompt', 'supplier', 'environment', 'notes')

# Every artifact a Recording Analysis Run may legitimately register. The chain must
# stay isolated from Active Measurement evidence (live measurement audio, stimulus,
# control/execution evidence, etc.): a kind outside this set means an unrelated
# artifact entered the recording chain, so the Run is no longer a trustworthy
# recording import. Adding a kind is a deliberate change to what the chain may hold.
RECORDING_ARTIFACT_KINDS = frozenset({
    'original_recording', 'normalized_audio', 'audio_metadata', 'processor_audit',
    'audio-qa', 'audio_qa_conditions',
    'transcript', 'asr_native', 'provider_invocation',
    'acoustic-segments', 'speaker-assignments', 'speaker-alignment', 'attribution',
    'fused-segments',
    'turns', 'timeline', 'metrics', 'judge-results', 'findings',
    'retained_diagnostic', 'model_configuration', 'analysis_checkpoint',
    'report_json', 'report_markdown',
})


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_profile(profile):
    profile = profile or {}
    if not isinstance(profile, dict) or set(profile) - set(PROFILE_KEYS):
        raise ValueError('Import profile accepts only device/hardware/firmware/model/prompt/supplier/environment/notes')
    if any(value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 4000)
           for value in profile.values()):
        raise ValueError('Profile values must be nonempty text (up to 4000 characters) or null')
    return {key: profile.get(key) for key in PROFILE_KEYS}


class ImportRun:
    def __init__(self, output, profile=None, synthetic=False):
        profile = validate_profile(profile)
        run_id, analysis_id = 'RUN-' + uuid.uuid4().hex, 'ANALYSIS-' + uuid.uuid4().hex
        self.directory = Path(output).resolve() / run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.analysis = self.directory / 'analysis' / analysis_id
        self.analysis.mkdir(parents=True)
        self.manifest = {'schema_version': '1.0.0', 'workflow': 'recording_import', 'run_id': run_id,
            'analysis_id': analysis_id, 'created_at': utc_now(), 'status': 'partial',
            'execution_kind': 'synthetic' if synthetic else 'imported', 'profile': profile,
            'case_ref': None, 'original_sha256': None, 'artifacts': [],
            'stages': {name: {'status': 'pending', 'processor': {'name': name, 'version': '1.0.0'},
                'started_at': None, 'finished_at': None, 'latency_ms': None,
                'input_artifact_ids': [], 'output_artifact_ids': [], 'reason': 'Processor not run'} for name in STAGES}}
        self.checkpoint()

    def checkpoint(self):
        errors = schema_errors(self.manifest, 'recording-run')
        if errors:
            raise ValueError('Invalid import Run manifest: ' + '\n'.join(errors))
        path = self.directory / 'manifest.pending.json'
        write_json(path, self.manifest)
        path.replace(self.directory / 'manifest.json')  # Mutable checkpoint; artifact outputs are immutable.

    def register(self, path, kind, parents=(), processor=None):
        path = Path(path).resolve()
        if not path.is_file() or not path.is_relative_to(self.directory):
            raise ValueError('Artifact must resolve to a file within its Run')
        relative = path.relative_to(self.directory).as_posix()
        for item in self.manifest['artifacts']:
            if item['path'] == relative:
                if item['sha256'] != digest(path):
                    raise ValueError('An immutable registered artifact was modified')
                return item['artifact_id']
        known = {item['artifact_id'] for item in self.manifest['artifacts']}
        if set(parents) - known:
            raise ValueError('Unknown parent artifact')
        artifact = {'artifact_id': 'ART-' + uuid.uuid4().hex, 'path': relative, 'kind': kind,
            'sha256': digest(path), 'size_bytes': path.stat().st_size, 'parent_artifact_ids': list(parents),
            'processor': processor, 'created_at': utc_now()}
        self.manifest['artifacts'].append(artifact)
        return artifact['artifact_id']

    def envelope(self, kind, status, reason, data=None, refs=(), data_artifact_ref=None):
        value = {'schema_version': '1.0.0', 'run_id': self.manifest['run_id'],
            'analysis_id': self.manifest['analysis_id'], 'kind': kind, 'status': status,
            'reason': reason, 'data': data, 'artifact_refs': list(refs), 'data_artifact_ref': data_artifact_ref}
        if data_artifact_ref is not None and data_artifact_ref not in refs:
            raise ValueError('Canonical document must be one of the referenced artifacts')
        errors = schema_errors(value, 'analysis-output')
        if errors:
            raise ValueError('Invalid analysis stage envelope: ' + '\n'.join(errors))
        path = self.analysis / f'{kind}.json'
        if path.exists():
            raise ValueError('Analysis outputs are immutable; create a new revision')
        write_json(path, value)
        return self.register(path, kind, refs, kind + ':1.0.0')

    def execute(self, name, inputs, operation):
        stage = self.manifest['stages'][name]
        stage.update(status='running', started_at=utc_now(), input_artifact_ids=list(inputs), reason=None)
        self.checkpoint()
        start = time.monotonic()
        try:
            outputs, result, reason = operation()
            stage.update(status='complete' if not reason else 'partial', output_artifact_ids=list(outputs), reason=reason)
            return result
        except Exception as error:
            # Native providers may put request secrets in exception strings. Never reflect those here.
            reason = str(error) if isinstance(error, AudioProcessingError) else f'{type(error).__name__}: processor failed; retained local artifacts'
            stage.update(status='failed', reason=reason)
            return None
        finally:
            stage.update(finished_at=utc_now(), latency_ms=round((time.monotonic() - start) * 1000, 3))
            self.checkpoint()


def ingest_file(run, source):
    """Copy with a byte limit and change detection before declaring OriginalArtifact."""
    source = Path(source)
    suffix = source.suffix.lower()
    if suffix not in FORMATS:
        raise AudioProcessingError('Supported recording formats are WAV, MP3 and M4A')
    if not source.is_file():
        raise AudioProcessingError('Recording file is unavailable')
    before = source.stat()
    if not 0 < before.st_size <= MAX_INPUT_BYTES:
        raise AudioProcessingError('Recording must be nonempty and no larger than 1 GiB')
    folder = run.directory / 'original'
    folder.mkdir()
    pending = folder / 'copy.incomplete'
    count = 0
    with source.open('rb') as incoming, pending.open('xb') as outgoing:
        while block := incoming.read(1024 * 1024):
            count += len(block)
            if count > MAX_INPUT_BYTES:
                raise AudioProcessingError('Recording grew beyond the import limit')
            outgoing.write(block)
    after = source.stat()
    if count != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise AudioProcessingError('Recording changed while being imported; retained copy is not approved as source')
    # Verify current source and copied content, not just length or timestamps.
    copied_hash = digest(pending)
    if copied_hash != digest(source):
        raise AudioProcessingError('Recording changed during snapshot verification')
    destination = folder / ('source' + suffix)
    pending.rename(destination)
    artifact_id = run.register(destination, 'original_recording', processor='file_copy_sha256:1.0.0')
    run.manifest['original_sha256'] = copied_hash
    return [artifact_id], (destination, artifact_id), None


def recording_run_errors(manifest, root=None):
    errors = schema_errors(manifest, 'recording-run')
    if errors:
        return errors
    artifacts = manifest['artifacts']
    indexed = {item['artifact_id']: item for item in artifacts}
    if len(indexed) != len(artifacts) or len({item['path'] for item in artifacts}) != len(artifacts):
        errors.append('Duplicate artifact ID/path')
    seen = set()
    for item in artifacts:
        relative = item['path']
        if (PureWindowsPath(relative).drive or PureWindowsPath(relative).root or PurePosixPath(relative).is_absolute()
                or '..' in relative.replace('\\', '/').split('/') or ':' in relative):
            errors.append('Artifact path must stay inside its Run')
        if item['kind'] not in RECORDING_ARTIFACT_KINDS:
            errors.append('Artifact kind is not part of the recording pipeline chain: ' + item['kind'])
        if set(item['parent_artifact_ids']) - seen:
            errors.append('Artifact parents must reference earlier artifacts; cycles/dangling refs forbidden')
        seen.add(item['artifact_id'])
        if root is not None:
            try:
                path = contained_file(root, item['path'])
                if path.stat().st_size != item['size_bytes'] or digest(path) != item['sha256']:
                    errors.append('Artifact integrity mismatch: ' + item['artifact_id'])
            except (OSError, ValueError):
                errors.append('Artifact unavailable: ' + item['artifact_id'])
    for stage in manifest['stages'].values():
        if set(stage['input_artifact_ids'] + stage['output_artifact_ids']) - set(indexed):
            errors.append('Stage contains dangling artifact references')
    originals = [item for item in artifacts if item['kind'] == 'original_recording']
    if manifest['original_sha256'] is not None and (len(originals) != 1 or originals[0]['sha256'] != manifest['original_sha256']):
        errors.append('Original source identity is inconsistent')
    if manifest['stages']['ingestion']['status'] == 'complete' and not originals:
        errors.append('Completed ingestion requires an original artifact')
    if manifest['status'] == 'complete' and any(stage['status'] != 'complete' for stage in manifest['stages'].values()):
        errors.append('Incomplete analysis stages cannot produce a complete Run')
    return errors
