"""Import orchestrator. Signal, storage, ASR and reporting processors are independent."""

from pathlib import Path
import json

from .runner import write_json
from .run_lock import run_lock

from .asr import transcribe_file
from .audio_processing import FFmpegAudioProcessor, MAX_RECORDING_MS
from .import_artifacts import ImportRun, ingest_file, recording_run_errors
from .import_report import write_import_report


def _normalize(run, source, parent, processor):
    result = processor.normalize(source, run.analysis / 'normalization')
    output_ids = []
    normalized_id = run.register(result.path, 'normalized_audio', [parent], 'audio_normalization:1.0.0')
    output_ids.append(normalized_id)
    for path in result.artifacts:
        if path.resolve() != result.path.resolve():
            output_ids.append(run.register(path, 'audio_metadata' if path.name == 'audio-metadata.json' else 'processor_audit',
                                           [parent, normalized_id], 'audio_normalization:1.0.0'))
    run.manifest['stages']['normalization']['processor'] = result.processor
    return output_ids, (result, normalized_id), None


def _asr(run, normalized, parent, provider_factory):
    # Model initialization belongs inside its stage so missing models cannot lose the imported Run.
    provider = provider_factory()
    known = {a['path'] for a in run.manifest['artifacts']}
    try:
        directory, transcript = transcribe_file(normalized, provider, run.analysis / 'asr-native',
                                                source_role='room_mix', max_duration_ms=MAX_RECORDING_MS)
    finally:
        for path in sorted((run.directory / 'provider-calls').rglob('*')):
            if path.is_file() and path.relative_to(run.directory).as_posix() not in known:
                ref = run.register(path, 'provider_invocation', [parent], 'invocation_audit:1.0.0')
                if ref not in run.manifest['stages']['asr']['output_artifact_ids']:
                    run.manifest['stages']['asr']['output_artifact_ids'].append(ref)
        run.checkpoint()
    mapping = {path.name: run.register(path, 'asr_native', [parent], 'timestamped_asr:1.0.0') for path in sorted(directory.iterdir())}
    ids = list(run.manifest['stages']['asr']['output_artifact_ids']) + list(mapping.values())
    run.manifest['stages']['asr']['processor'] = transcript['provider_profile']
    # Transcript 1.0 pairs run_id with case_id; the envelope binds unscripted imports without a fake Case.
    output = run.envelope('transcript', transcript['status'], 'External ASR; role unknown and acoustic timing unverified',
                          transcript, ids, data_artifact_ref=mapping['transcript.json'])
    return ids + [output], transcript, 'Provider returned transcript gaps' if transcript['gaps'] else None


def _pending_outputs(run, normalized_ok):
    stages = run.manifest['stages']
    stage_files = {'asr': 'transcript', 'acoustic': 'acoustic-segments', 'diarization': 'speaker-assignments',
                   'fusion': 'fused-segments', 'turns': 'turns', 'timeline': 'timeline',
                   'metrics': 'metrics', 'judge': 'judge-results', 'findings': 'findings'}
    for stage_name, kind in stage_files.items():
        if (run.analysis / f'{kind}.json').exists():
            continue
        stage = stages[stage_name]
        if stage['status'] == 'pending':
            stage['reason'] = ('Provider or processor is not configured/implemented for this import milestone' if normalized_ok
                               else 'Canonical audio unavailable; dependent analysis was not run')
            if not normalized_ok:
                stage['status'] = 'insufficient_evidence'
        output = run.envelope(kind, stage['status'], stage['reason'])
        stage['output_artifact_ids'].append(output)


def _retain_failures(run):
    """Catalog leftover local diagnostics/partial data without promoting them to successful outputs."""
    known = {item['path'] for item in run.manifest['artifacts']}
    for path in sorted(run.directory.rglob('*')):
        if path.is_file() and path.name not in ('manifest.json', 'manifest.pending.json'):
            relative = path.relative_to(run.directory).as_posix()
            if relative not in known:
                artifact_id = run.register(path, 'retained_diagnostic', processor='failure_retention:1.0.0')
                owner = ('normalization' if 'normalization' in path.parts else
                         'asr' if 'asr-native' in path.parts else 'ingestion' if 'original' in path.parts else None)
                if owner:
                    run.manifest['stages'][owner]['output_artifact_ids'].append(artifact_id)


def import_recording(source, output_root='artifacts/imports', *, profile=None, audio_processor=None,
                     asr_provider_factory=None, synthetic=False, model_snapshot=None, providers=None):
    """Return a durable Run even when a processor fails. Does not perform cloud or hardware I/O by default."""
    run = ImportRun(output_root, profile, synthetic)
    with run_lock(run.directory):
        return _continue_import(run, source, audio_processor, asr_provider_factory, model_snapshot, providers)


def _continue_import(run, source, audio_processor, asr_provider_factory, model_snapshot, providers):
    _save_configuration(run, model_snapshot)
    if providers is not None and providers.asr is not None:
        asr_provider_factory = lambda: providers.asr(run.directory)
    original = run.execute('ingestion', [], lambda: ingest_file(run, source))
    normalized = None
    if original:
        path, artifact_id = original
        normalized = run.execute('normalization', [artifact_id],
            lambda: _normalize(run, path, artifact_id, audio_processor or FFmpegAudioProcessor()))
    else:
        run.manifest['status'] = 'failed'
        run.manifest['stages']['normalization'].update(status='insufficient_evidence', reason='Original source unavailable')
    if normalized:
        result, artifact_id = normalized
        def emit_qa():
            item = run.envelope('audio-qa', 'complete', 'Measurements only; no acceptance threshold configured',
                                result.metadata['normalized'], [artifact_id])
            return [item], result.metadata['normalized'], None
        run.execute('audio_qa', [artifact_id], emit_qa)
        if asr_provider_factory is not None:
            run.execute('asr', [artifact_id] + _configuration_refs(run), lambda: _asr(run, result.path, artifact_id, asr_provider_factory))
    else:
        stage = run.manifest['stages']['audio_qa']
        stage.update(status='insufficient_evidence', reason='No verified canonical audio')
        stage['output_artifact_ids'] = [run.envelope('audio-qa', stage['status'], stage['reason'])]
    if normalized is None:
        run.manifest['status'] = 'failed'
    _pending_outputs(run, normalized is not None)
    _retain_failures(run)
    run.checkpoint()
    run.execute('report', [item['artifact_id'] for item in run.manifest['artifacts']], lambda: write_import_report(run))
    errors = recording_run_errors(run.manifest, run.directory)
    if errors:
        raise ValueError('Import artifact validation failed: ' + '\n'.join(errors))
    run.checkpoint()
    return run.directory, run.manifest


def _configuration_refs(run):
    return [a['artifact_id'] for a in run.manifest['artifacts']
            if a['kind'] == 'model_configuration' and a['path'].startswith(run.analysis.relative_to(run.directory).as_posix())]


def _save_configuration(run, snapshot):
    if snapshot is not None:
        path = run.analysis / 'model-config.json'
        write_json(path, snapshot)
        run.register(path, 'model_configuration', processor='model_settings:1.1.0')
        run.checkpoint()


def resume_recording(directory, *, providers=None, model_snapshot=None):
    """Explicit ASR retry in the same Run; preserve every earlier analysis artifact.

    Caller must hold the Run lock. No automatic retry of a cloud request whose
    outcome may be unknown. Completed timestamped ASR is idempotently returned.
    """
    import copy
    import uuid
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if recording_run_errors(manifest, directory):
        raise ValueError('Run evidence failed integrity validation')
    asr_complete = manifest['stages']['asr']['status'] == 'complete'
    if asr_complete and manifest['stages']['report']['status'] == 'complete':
        return directory, manifest
    normalized = next((a for a in manifest['artifacts'] if a['kind']=='normalized_audio'), None)
    if normalized is None:
        raise ValueError('Verified canonical audio is required to resume ASR')
    if not asr_complete and (providers is None or providers.asr is None):
        raise ValueError('Configure an ASR route before retrying')
    run = ImportRun.__new__(ImportRun)
    run.directory, run.manifest = directory, copy.deepcopy(manifest)
    old = directory / 'analysis' / manifest['analysis_id'] / 'manifest-snapshot.json'
    if old.exists():
        if json.loads(old.read_text(encoding='utf-8')) != manifest:
            raise ValueError('Conflicting analysis checkpoint')
    else:
        write_json(old, manifest)
    run.register(old, 'analysis_checkpoint', processor='resume:1.0.0')
    run.manifest['analysis_id'] = 'ANALYSIS-' + uuid.uuid4().hex
    run.analysis = directory / 'analysis' / run.manifest['analysis_id']
    run.analysis.mkdir()
    for name,stage in run.manifest['stages'].items():
        if name not in ('ingestion','normalization','audio_qa'):
            stage.update(status='pending', started_at=None, finished_at=None, latency_ms=None,
                         input_artifact_ids=[], output_artifact_ids=[], reason='Processor not run in this revision')
    run.manifest['status'] = 'partial'
    _save_configuration(run, model_snapshot)
    run.checkpoint()
    parent = normalized['artifact_id']
    if asr_complete:
        old_envelope = directory / 'analysis' / manifest['analysis_id'] / 'transcript.json'
        data = json.loads(old_envelope.read_text(encoding='utf-8'))
        def restore_asr():
            ref=run.envelope('transcript', data['status'], data['reason'], data['data'],
                             data['artifact_refs'], data['data_artifact_ref'])
            return data['artifact_refs']+[ref], data['data'], None
        run.execute('asr', manifest['stages']['asr']['input_artifact_ids'], restore_asr)
        run.manifest['stages']['asr']['processor']=manifest['stages']['asr']['processor']
    else:
        run.execute('asr', [parent]+_configuration_refs(run), lambda: _asr(run, directory / normalized['path'],
            parent, lambda: providers.asr(directory)))
    _pending_outputs(run, True)
    _retain_failures(run)
    run.execute('report', [a['artifact_id'] for a in run.manifest['artifacts']], lambda: write_import_report(run))
    run.checkpoint()
    if recording_run_errors(run.manifest, directory):
        raise ValueError('Resumed Run evidence failed validation')
    return directory, run.manifest
