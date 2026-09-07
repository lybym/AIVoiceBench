"""Import orchestrator. Signal, storage, ASR and reporting processors are independent."""

from pathlib import Path

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
    directory, transcript = transcribe_file(normalized, provider, run.analysis / 'asr-native',
                                            source_role='room_mix', max_duration_ms=MAX_RECORDING_MS)
    mapping = {path.name: run.register(path, 'asr_native', [parent], 'timestamped_asr:1.0.0') for path in sorted(directory.iterdir())}
    ids = list(mapping.values())
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
                     asr_provider_factory=None, synthetic=False):
    """Return a durable Run even when a processor fails. Does not perform cloud or hardware I/O by default."""
    run = ImportRun(output_root, profile, synthetic)
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
            run.execute('asr', [artifact_id], lambda: _asr(run, result.path, artifact_id, asr_provider_factory))
    else:
        stage = run.manifest['stages']['audio_qa']
        stage.update(status='insufficient_evidence', reason='No verified canonical audio')
        stage['output_artifact_ids'] = [run.envelope('audio-qa', stage['status'], stage['reason'])]
    _pending_outputs(run, normalized is not None)
    _retain_failures(run)
    run.checkpoint()
    run.execute('report', [item['artifact_id'] for item in run.manifest['artifacts']], lambda: write_import_report(run))
    errors = recording_run_errors(run.manifest, run.directory)
    if errors:
        raise ValueError('Import artifact validation failed: ' + '\n'.join(errors))
    run.checkpoint()
    return run.directory, run.manifest
