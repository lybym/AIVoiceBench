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


def _envelope_data(doc, status):
    """Return doc as data for envelope, or None if status requires null data."""
    if status in ('pending', 'insufficient_evidence', 'failed'):
        return None
    return doc


def _acoustic(run, normalized_path, parent):
    """Run energy VAD acoustic segmentation on the normalized audio."""
    from .acoustic import EnergyVadSegmenter
    segmenter = EnergyVadSegmenter()
    result = segmenter.segment(Path(normalized_path).resolve())
    doc = result.to_dict()
    status = doc['status']
    output = run.envelope('acoustic-segments', status,
                          'Acoustic VAD; signal timing, no speaker role',
                          _envelope_data(doc, status), [parent])
    run.manifest['stages']['acoustic']['processor'] = {
        'name': 'energy_vad', 'version': '1.0.0',
        'method': 'energy_vad', 'parameters': doc.get('processor', {}).get('parameters', {})
    }
    return [output], doc, None if doc['segments'] else 'No speech segments detected'


def _fusion(run, acoustic_doc, transcript_doc, parent_ids):
    """Fuse acoustic segments with optional ASR transcript and attribute speakers."""
    from .fusion import fuse
    fused_doc = fuse(acoustic_doc, transcript_doc)
    status = fused_doc['status']
    output = run.envelope('fused-segments', status,
                          fused_doc.get('attribution', {}).get('note', 'Fused segments'),
                          _envelope_data(fused_doc, status), parent_ids)
    run.manifest['stages']['fusion']['processor'] = {
        'name': 'fusion', 'version': '1.0.0',
        'strategy': fused_doc.get('attribution', {}).get('strategy', 'unknown')
    }
    reason = None if fused_doc['segments'] else 'No fused segments produced'
    return [output], fused_doc, reason


def _turns(run, fused_doc, parent_ids):
    """Build conversational turns from fused segments."""
    from .fusion import build_turns
    turns_doc = build_turns(fused_doc)
    status = turns_doc['status']
    output = run.envelope('turns', status,
                          turns_doc.get('reason') or 'Turn builder',
                          _envelope_data(turns_doc, status), parent_ids)
    run.manifest['stages']['turns']['processor'] = {'name': 'turn_builder', 'version': '1.0.0'}
    reason = None if turns_doc['turns'] else 'No turns could be built'
    return [output], turns_doc, reason


def _timeline(run, fused_doc, turns_doc, parent_ids):
    """Detect events and generate EventTimeline from fused segments and turns."""
    from .fusion import detect_events, generate_timeline
    events, evidence, status, reason = detect_events(fused_doc, turns_doc)
    timeline_doc = generate_timeline(fused_doc, turns_doc, events, evidence, status, reason)
    tl_status = timeline_doc['status']
    output = run.envelope('timeline', tl_status,
                          reason or 'Automatic event detection',
                          _envelope_data(timeline_doc, tl_status), parent_ids)
    run.manifest['stages']['timeline']['processor'] = {'name': 'event_detector', 'version': '1.0.0'}
    return [output], timeline_doc, reason if not events else None


def _metrics(run, timeline_doc, parent_ids):
    """Compute deterministic metrics from the timeline."""
    from .metrics import compute_timeline_metrics
    metrics_result = compute_timeline_metrics(timeline_doc)
    status = metrics_result.get('status', 'partial')
    output = run.envelope('metrics', status,
                          'Deterministic metrics from timeline events',
                          _envelope_data(metrics_result, status), parent_ids)
    run.manifest['stages']['metrics']['processor'] = {'name': 'metrics', 'version': '1.0.0'}
    observed = [m for m in metrics_result.get('metrics', []) if m.get('status') == 'observed']
    reason = None if observed else 'No observed metrics'
    return [output], metrics_result, reason


def _pending_outputs(run, normalized_ok):
    """Only diarization, judge and findings remain pending in this slice."""
    stages = run.manifest['stages']
    # diarization, judge, findings are not wired in this slice
    stage_files = {'diarization': 'speaker-assignments', 'judge': 'judge-results', 'findings': 'findings'}
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
    else:
        _run_evidence_chain(run, normalized, asr_provider_factory is not None)
    _pending_outputs(run, normalized is not None)
    _retain_failures(run)
    run.checkpoint()
    run.execute('report', [item['artifact_id'] for item in run.manifest['artifacts']], lambda: write_import_report(run))
    errors = recording_run_errors(run.manifest, run.directory)
    if errors:
        raise ValueError('Import artifact validation failed: ' + '\n'.join(errors))
    run.checkpoint()
    return run.directory, run.manifest


def _run_evidence_chain(run, normalized, asr_available):
    """Run acoustic → fusion → turns → timeline → metrics after ASR (or without ASR).

    Each stage is independent: failure in one does not prevent the report stage.
    Uses lazy imports so missing modules don't crash the import pipeline.
    """
    result, norm_art_id = normalized
    norm_path = result.path

    # Acoustic segmentation — parent: normalized_audio
    acoustic_result = run.execute('acoustic', [norm_art_id],
        lambda: _acoustic(run, norm_path, norm_art_id))

    acoustic_art_id = None
    if run.manifest['stages']['acoustic']['output_artifact_ids']:
        acoustic_art_id = run.manifest['stages']['acoustic']['output_artifact_ids'][-1]

    # Load transcript if ASR ran
    transcript_doc = None
    asr_art_id = None
    asr_stage = run.manifest['stages']['asr']
    if asr_stage['status'] == 'complete':
        transcript_path = run.analysis / 'transcript.json'
        if transcript_path.exists():
            envelope_data = json.loads(transcript_path.read_text(encoding='utf-8'))
            transcript_doc = envelope_data.get('data')
            if asr_stage['output_artifact_ids']:
                asr_art_id = asr_stage['output_artifact_ids'][-1]

    # Fusion — parents: acoustic_segments (+ transcript if available)
    if acoustic_result and acoustic_result.get('segments'):
        acoustic_doc = acoustic_result
        fusion_parents = [p for p in [acoustic_art_id, asr_art_id] if p]
        if not fusion_parents:
            fusion_parents = [norm_art_id]
        fusion_result = run.execute('fusion', fusion_parents,
            lambda: _fusion(run, acoustic_doc, transcript_doc, fusion_parents))

        fusion_art_id = None
        if run.manifest['stages']['fusion']['output_artifact_ids']:
            fusion_art_id = run.manifest['stages']['fusion']['output_artifact_ids'][-1]

        # Turns — parent: fused_segments (not normalized_audio)
        if fusion_result and fusion_result.get('segments'):
            fused_doc = fusion_result
            has_roles = any(s.get('speaker_role') in ('tester', 'device')
                           for s in fused_doc.get('segments', []))
            if has_roles and fusion_art_id:
                turns_result = run.execute('turns', [fusion_art_id],
                    lambda: _turns(run, fused_doc, [fusion_art_id]))

                turns_art_id = None
                if run.manifest['stages']['turns']['output_artifact_ids']:
                    turns_art_id = run.manifest['stages']['turns']['output_artifact_ids'][-1]

                # Timeline — parents: fused_segments + turns
                if turns_result and turns_result.get('turns'):
                    turns_doc = turns_result
                    tl_parents = [p for p in [fusion_art_id, turns_art_id] if p]
                    timeline_result = run.execute('timeline', tl_parents,
                        lambda: _timeline(run, fused_doc, turns_doc, tl_parents))

                    timeline_art_id = None
                    if run.manifest['stages']['timeline']['output_artifact_ids']:
                        timeline_art_id = run.manifest['stages']['timeline']['output_artifact_ids'][-1]

                    # Metrics — parent: timeline
                    if timeline_result and timeline_result.get('events') is not None and timeline_art_id:
                        timeline_doc = timeline_result
                        run.execute('metrics', [timeline_art_id],
                            lambda: _metrics(run, timeline_doc, [timeline_art_id]))
            else:
                insuf_parent = fusion_art_id or norm_art_id
                for stage_name in ('turns', 'timeline', 'metrics'):
                    stage = run.manifest['stages'][stage_name]
                    stage['status'] = 'insufficient_evidence'
                    stage['reason'] = 'No known speaker roles; diarization not configured'
                    stage['input_artifact_ids'] = [insuf_parent]
                    output = run.envelope(
                        {'turns': 'turns', 'timeline': 'timeline', 'metrics': 'metrics'}[stage_name],
                        'insufficient_evidence', stage['reason'], refs=[insuf_parent])
                    stage['output_artifact_ids'].append(output)


def _configuration_refs(run):
    return [a['artifact_id'] for a in run.manifest['artifacts']
            if a['kind'] == 'model_configuration' and a['path'].startswith(run.analysis.relative_to(run.directory).as_posix())]


def _run_evidence_chain_resume(run, directory, normalized_artifact, norm_art_id):
    """Run evidence chain during resume (same logic as _run_evidence_chain but for resumed runs)."""
    norm_path = directory / normalized_artifact['path']
    # Wrap in a compatible structure for _run_evidence_chain
    class _Result:
        def __init__(self, path):
            self.path = path
    normalized = (_Result(norm_path), norm_art_id)
    _run_evidence_chain(run, normalized, run.manifest['stages']['asr']['status'] == 'complete')


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
    # Run evidence chain after ASR restore/retry
    _run_evidence_chain_resume(run, directory, normalized, parent)
    _pending_outputs(run, True)
    _retain_failures(run)
    run.execute('report', [a['artifact_id'] for a in run.manifest['artifacts']], lambda: write_import_report(run))
    run.checkpoint()
    if recording_run_errors(run.manifest, directory):
        raise ValueError('Resumed Run evidence failed validation')
    return directory, run.manifest
