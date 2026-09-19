"""Import orchestrator. Signal, storage, ASR and reporting processors are independent."""

from pathlib import Path
import json

from .runner import digest, write_json
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
    metadata_id = None
    for path in result.artifacts:
        if path.resolve() != result.path.resolve():
            artifact_id = run.register(path, 'audio_metadata' if path.name == 'audio-metadata.json' else 'processor_audit',
                                       [parent, normalized_id], 'audio_normalization:1.0.0')
            output_ids.append(artifact_id)
            if path.name == 'audio-metadata.json':
                metadata_id = artifact_id
    run.manifest['stages']['normalization']['processor'] = result.processor
    # The canonical metadata document is the JSON artifact that carries the measured
    # facts, so stages that report on those facts need its id, not just the WAV id.
    return output_ids, (result, normalized_id, metadata_id), None


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


def _safe_diar_art_id(run, fallback):
    """Get diarization output artifact ID, or fallback."""
    if run.manifest['stages'].get('diarization', {}).get('output_artifact_ids'):
        return [run.manifest['stages']['diarization']['output_artifact_ids'][-1]]
    return [fallback]


def _diarize(run, norm_path, acoustic_doc, parent, diarization_provider=None,
             transcript_doc=None, transcript_invocation=None):
    """Run diarization on the normalized recording.

    Outputs speaker clustering (speaker_0, speaker_1, ...) — NOT tester/device.
    Without a configured provider, returns insufficient_evidence.
    MockDiarizationProvider is NEVER used in production — test injection only.

    When the provider derives clusters from an existing ASR native response,
    no second recognition request is submitted.
    """
    acoustic_segments = acoustic_doc.get('segments', []) if acoustic_doc else []

    if diarization_provider is None:
        # No provider configured — not a failure, just unavailable
        output = run.envelope('speaker-assignments', 'insufficient_evidence',
                              'Diarization provider not configured; speaker clustering unavailable',
                              None, [parent])
        run.manifest['stages']['diarization']['processor'] = {'name': 'none', 'version': '1.0.0'}
        run.manifest['stages']['diarization']['status'] = 'insufficient_evidence'
        return [output], None, None  # reason=None so run.execute doesn't override to 'partial'

    # Prefer deriving from the completed ASR native response: one cloud call
    # already produced the speaker labels, so do not resubmit recognition.
    if transcript_doc is not None and hasattr(diarization_provider, 'diarize_from_transcript'):
        audio_sha256 = (transcript_doc.get('source') or {}).get('sha256') or _audio_sha(run, parent)
        result = diarization_provider.diarize_from_transcript(
            transcript_doc,
            audio_sha256=audio_sha256,
            invocation_id=(transcript_invocation or {}).get('invocation_id'),
            native_response_sha256=(transcript_invocation or {}).get('native_response_sha256'),
            analysis_id=run.manifest.get('analysis_id'))
    else:
        result = diarization_provider.diarize(Path(norm_path).resolve(), acoustic_segments)

    doc = result.to_dict()
    status = doc['status']
    output = run.envelope('speaker-assignments', status,
                          doc.get('reason') or 'Speaker clustering only; role attribution is separate',
                          _envelope_data(doc, status), [parent])
    run.manifest['stages']['diarization']['processor'] = doc.get('processor', {})
    reason = None if doc.get('speaker_segments') else doc.get('reason') or 'No speaker segments produced'
    return [output], doc, reason


def _audio_sha(run, artifact_id):
    """Resolve a registered artifact's sha256."""
    for item in run.manifest.get('artifacts', []):
        if item['artifact_id'] == artifact_id:
            return item.get('sha256')
    return None


def _asr_invocation(run):
    """Read the ASR provider invocation identity from registered evidence.

    Returns {'invocation_id', 'native_response_sha256'} when available, so
    derived speaker segments can reference the real call instead of pretending
    a second cloud request occurred.
    """
    info = {'invocation_id': None, 'native_response_sha256': None}
    directory = run.directory / 'provider-calls'
    if not directory.is_dir():
        return info
    for call_dir in sorted(directory.iterdir()):
        start = call_dir / 'start.json'
        result = call_dir / 'result.json'
        native = call_dir / 'native-response.json'
        if not start.exists():
            continue
        try:
            record = json.loads(start.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if record.get('provider') != 'volcengine':
            continue
        info['invocation_id'] = call_dir.name
        if native.exists():
            info['native_response_sha256'] = digest(native)
        elif result.exists():
            try:
                finished = json.loads(result.read_text(encoding='utf-8'))
                for ref in finished.get('output_artifact_refs', []) or []:
                    name = Path(ref).name
                    if name == 'native-response.json':
                        candidate = call_dir / name
                        if candidate.exists():
                            info['native_response_sha256'] = digest(candidate)
            except (OSError, ValueError):
                pass
    return info


def _attribute(run, diarization_doc, parent, explicit_mapping=None):
    """Map speaker_id to tester/device/unknown using evidence.

    Only explicit user/human evidence may assign tester/device. Without a saved
    mapping, all speakers remain unknown and role-dependent stages abstain.
    """
    from .diarization import attribute_speakers
    attr_doc = attribute_speakers(
        diarization_doc or {'speaker_segments': []}, explicit_mapping=explicit_mapping)
    status = attr_doc['status']
    reason = attr_doc.get('reason') or attr_doc.get('notes') or 'Source attribution'
    output = run.envelope('attribution', status, reason,
                          _envelope_data(attr_doc, status), [parent])
    return [output], attr_doc, None if status == 'complete' else reason


def _fusion_with_speakers(run, acoustic_doc, transcript_doc, diarization_doc,
                          attribution_doc, parent_ids):
    """Fuse acoustic segments with ASR transcript, diarization and role attribution.

    speaker_id comes from diarization.
    speaker_role comes from source attribution (explicit/human/none).
    They are separate fields — never conflated.

    The acoustic↔speaker-span alignment is computed once from the real acoustic
    document (so the low-energy diagnostics have frame statistics), validated
    against its own schema, and published as a registered artifact. The
    assignment or abstention of every fused segment is therefore traceable to
    recorded overlap/coverage facts instead of an implicit calculation.
    """
    from .alignment import align_speaker_spans
    from .fusion import fuse, apply_speakers
    from .validation import schema_errors

    alignment_doc = align_speaker_spans(acoustic_doc, diarization_doc,
                                        transcript_doc=transcript_doc)
    errors = schema_errors(alignment_doc, 'speaker-alignment')
    if errors:
        raise ValueError('Invalid speaker alignment document: ' + '\n'.join(errors))
    alignment_path = run.analysis / 'alignment.json'
    write_json(alignment_path, alignment_doc)
    alignment_id = run.register(
        alignment_path, 'speaker-alignment', parent_ids,
        'alignment:' + alignment_doc['processor']['version'])

    fused_doc = fuse(acoustic_doc, transcript_doc)
    apply_speakers(fused_doc, diarization_doc, attribution_doc, alignment_doc)

    status = fused_doc['status']
    output = run.envelope('fused-segments', status,
                          fused_doc.get('reason') or fused_doc.get('attribution', {}).get('note', 'Fused segments'),
                          _envelope_data(fused_doc, status), parent_ids)
    run.manifest['stages']['fusion']['processor'] = {
        'name': 'fusion', 'version': '1.0.0',
        'strategy': fused_doc.get('attribution', {}).get('strategy', 'unknown'),
        'alignment': {
            'document_id': alignment_doc['document_id'],
            'status': alignment_doc['status'],
            'policy_version': alignment_doc['policy']['policy_version'],
            'processor_version': alignment_doc['processor']['version'],
        },
    }
    reason = None if status == 'complete' else fused_doc.get('reason') or 'Speaker clusters unresolved'
    # The alignment document is published before the fusion envelope so a reader
    # enumerating stage outputs finds the evidence behind the decision first.
    return [alignment_id, output], fused_doc, reason


def _acoustic(run, normalized_path, parent):
    """Run the configured acoustic-boundary provider on the normalized audio.

    Two independent selections are recorded in the document and echoed into the
    stage manifest:

    * the **provider** (``AIVOICEBENCH_ACOUSTIC_PROVIDER``, default ``energy``) —
      the legacy energy/RMS VAD, or the Silero model provider. A model provider
      that cannot load raises; it is never silently replaced by the energy VAD.
    * the **sensitivity profile** (``AIVOICEBENCH_ACOUSTIC_PROFILE``) for the
      energy provider. The default is the canonical measurement policy; a
      non-canonical profile (for evaluating quiet device responses) is marked as
      such, so it can never be silently presented as the canonical measurement.
    """
    import os

    from .acoustic import EnergyVadSegmenter, resolve_segmenter
    profile = os.environ.get('AIVOICEBENCH_ACOUSTIC_PROFILE') or None
    provider = (os.environ.get('AIVOICEBENCH_ACOUSTIC_PROVIDER') or 'energy').strip().lower()
    if provider in ('', 'energy', 'energy_vad'):
        segmenter = EnergyVadSegmenter.from_profile(profile)
    elif profile:
        raise ValueError('AIVOICEBENCH_ACOUSTIC_PROFILE selects an energy-VAD sensitivity '
                         'profile and cannot be combined with a model acoustic provider')
    else:
        # resolve_segmenter raises for an unknown provider or an unavailable model
        # runtime/weights, so the stage fails explicitly rather than degrading.
        segmenter = resolve_segmenter(provider)
    result = segmenter.segment(Path(normalized_path).resolve())
    doc = result.to_dict()
    status = doc['status']
    output = run.envelope('acoustic-segments', status,
                          'Acoustic VAD; signal timing, no speaker role',
                          _envelope_data(doc, status), [parent])
    processor = doc.get('processor', {})
    run.manifest['stages']['acoustic']['processor'] = {
        'name': processor.get('method', 'energy_vad'),
        'version': processor.get('processor_version', '1.0.0'),
        'method': processor.get('method', 'energy_vad'),
        'parameters': processor.get('parameters', {}),
        'sensitivity': processor.get('sensitivity'),
        # Model identity/runtime and the boundary policy version are part of the
        # stage provenance: without them the Run's boundaries are not replayable.
        'model': processor.get('model'),
        'policy': processor.get('sensitivity', {}).get('profile') if processor.get('model') else None,
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
    """Detect events and generate EventTimeline from fused segments and turns.

    The timeline and its events carry this Run's real identity, so the persisted
    timeline validates on its own and the metrics derived from it agree.
    """
    from .fusion import detect_events, generate_timeline
    identity = {'run_id': run.manifest['run_id'],
                'case_id': run.manifest.get('case_ref') or 'CASE-auto',
                'execution_kind': run.manifest['execution_kind']}
    events, evidence, status, reason = detect_events(
        fused_doc, turns_doc, run_id=identity['run_id'], case_id=identity['case_id'])
    timeline_doc = generate_timeline(fused_doc, turns_doc, events, evidence, status, reason,
                                     **identity)
    tl_status = timeline_doc['status']
    output = run.envelope('timeline', tl_status,
                          reason or 'Automatic event detection',
                          _envelope_data(timeline_doc, tl_status), parent_ids)
    run.manifest['stages']['timeline']['processor'] = {'name': 'event_detector', 'version': '1.0.0'}
    return [output], timeline_doc, reason if not events else None


def _metrics(run, timeline_doc, parent_ids, semantic_evidence=None):
    """Compute canonical MetricResult 3.0.0 from the timeline.

    `semantic_evidence` are the constrained records a performed Judge produced.
    The deterministic engine decides what they may become; a missing or
    ineligible record abstains instead of yielding a semantic value.
    """
    from .metrics import compute_timeline_metrics
    # Bind the timeline to the Run identity so canonical metrics carry
    # run_id / analysis_id / execution_kind for provenance.
    scoped = dict(timeline_doc)
    scoped['run_id'] = run.manifest['run_id']
    scoped['analysis_id'] = run.manifest['analysis_id']
    scoped['execution_kind'] = run.manifest['execution_kind']
    scoped['case_id'] = run.manifest.get('case_ref') or None
    metrics_result = compute_timeline_metrics(scoped, semantic_evidence=semantic_evidence)
    raw_status = metrics_result.get('status', 'insufficient_evidence')
    has_metrics = bool(metrics_result.get('metrics'))
    if raw_status == 'observed':
        envelope_status = 'complete'
    elif has_metrics:
        envelope_status = 'partial'  # Has metric documents, just no observed values
    else:
        envelope_status = 'insufficient_evidence'
    output = run.envelope('metrics', envelope_status,
                          'Canonical MetricResult 3.0.0 from timeline events',
                          _envelope_data(metrics_result, envelope_status), parent_ids)
    run.manifest['stages']['metrics']['processor'] = {'name': 'metrics', 'version': '3.0.0'}
    counts = metrics_result.get('counts', {})
    reason = None if counts.get('observed') else 'No observed metrics (insufficient_evidence or not_applicable)'
    return [output], metrics_result, reason


def _retain_contract_violation(run, name, errors, document):
    """Keep the exact reason a self-produced document failed its own contract.

    `run.execute` deliberately reduces an unknown exception to its type name, so
    the failing detail would otherwise be lost. Writing the rejected document and
    its validation errors as a retained diagnostic keeps the failure diagnosable
    without publishing the invalid document as evidence.
    """
    path = run.analysis / f'{name}-contract-violation.json'
    write_json(path, {'schema_version': '1.0.0', 'run_id': run.manifest['run_id'],
                      'analysis_id': run.manifest['analysis_id'],
                      'status': 'failed', 'errors': list(errors),
                      'rejected_document': document})
    artifact_id = run.register(path, 'retained_diagnostic', (),
                               f'{name}_contract_guard:1.0.0')
    return artifact_id


def _judge(run, fused_doc, turns_doc, timeline_doc, parent_ids, provider):
    """Run the configured semantic Judge over eligible Turns/Events/Metrics (#10).

    The stage is honest about three states: a Run with no semantic provider
    configured is `insufficient_evidence` with that exact reason; a configured
    provider is invoked; and every judgment it returns is validated before it is
    published. A degraded judgment becomes an explicit abstention on the
    artifact instead of a silent absence.

    The Judge is never consulted about tester/device roles: this stage consumes
    Turns that user-confirmed role attribution already produced. The deterministic
    metrics are supplied as context, so a deterministic question keeps its
    deterministic answer.
    """
    from .llm import LLMJudge
    from .metrics import compute_timeline_metrics
    from .semantic_evidence import semantic_evidence_records
    from .validation import judge_document_errors, semantic_evidence_errors

    if provider is None:
        reason = ('No semantic Judge provider is configured; no semantic judgment was '
                  'performed')
        output = run.envelope('judge-results', 'insufficient_evidence', reason, None,
                              parent_ids)
        run.manifest['stages']['judge']['processor'] = {'name': 'none', 'version': '1.0.0'}
        return [output], None, reason

    scoped_timeline = dict(timeline_doc)
    scoped_timeline['run_id'] = run.manifest['run_id']
    scoped_timeline['analysis_id'] = run.manifest['analysis_id']
    scoped_timeline['execution_kind'] = run.manifest['execution_kind']
    scoped_timeline['case_id'] = run.manifest.get('case_ref') or None
    base_metrics = compute_timeline_metrics(scoped_timeline)

    # The Judge and its validators use the persisted Timeline as-is: identity
    # overrides that make a document schema-invalid must never be the reference a
    # citation is resolved against.
    judge = LLMJudge(provider)
    document = judge.evaluate(fused_doc, turns_doc, base_metrics, timeline_doc,
                              run_id=run.manifest['run_id'],
                              analysis_id=run.manifest['analysis_id'])
    errors = judge_document_errors(document, timeline_doc, turns_doc)
    if errors:
        _retain_contract_violation(run, 'judge-results', errors, document)
        raise ValueError('Judge artifact violates its own contract: ' + '; '.join(errors))

    semantic_records, semantic_abstentions = semantic_evidence_records(
        document, timeline_doc, turns_doc)
    errors = semantic_evidence_errors(semantic_records, timeline_doc, turns_doc)
    if errors:
        _retain_contract_violation(run, 'semantic-evidence', errors, semantic_records)
        raise ValueError('Semantic evidence violates the engine contract: ' + '; '.join(errors))
    document['abstentions'] = (document.get('abstentions') or []) + semantic_abstentions
    errors = judge_document_errors(document, timeline_doc, turns_doc)
    if errors:
        _retain_contract_violation(run, 'judge-results', errors, document)
        raise ValueError('Judge artifact violates its own contract after projection: '
                         + '; '.join(errors))

    # The envelope owns analysis/judge-results.json; the full payload and the raw
    # provider output are registered separately and referenced from it, so a reader
    # can always recover both the accepted and the rejected judgments.
    document_path = run.analysis / 'judge-document.json'
    write_json(document_path, document)
    document_id = run.register(document_path, 'judge-results', list(parent_ids), 'judge:1.0.0')
    raw_path = run.analysis / 'judge-raw.json'
    write_json(raw_path, {'schema_version': '1.0.0', 'run_id': run.manifest['run_id'],
                          'analysis_id': run.manifest['analysis_id'],
                          'invocations': document['invocations']})
    raw_id = run.register(raw_path, 'judge-raw', [document_id], 'judge:1.0.0')
    observed = [result for result in document['results'] if result['status'] == 'observed']
    status = 'complete' if observed else 'insufficient_evidence'
    reason = (None if observed else
              'No judge result was accepted; every judgment abstained or was rejected')
    output = run.envelope('judge-results', status,
                          reason or 'Schema-constrained Judge results with preserved raw output',
                          _envelope_data(document, status), list(parent_ids) + [document_id, raw_id],
                          data_artifact_ref=document_id)
    run.manifest['stages']['judge']['processor'] = {
        'name': document['judge_profile']['provider'],
        'version': document['criteria_version'],
        'model': document['judge_profile']['model'],
    }
    # `run.execute` unpacks (outputs, result, reason); the Judge's result is the
    # pair the caller needs — the artifact and the constrained records it yielded.
    return [output, document_id, raw_id], (document, semantic_records), reason


def _findings(run, judge_document, timeline_doc, metrics_result, parent_ids):
    """Turn judged finding candidates into validated Finding documents (#11).

    Candidates that cite no resolvable evidence never become Findings; they are
    recorded as abstentions. Each emitted Finding is validated against the same
    Timeline and metrics before publication.
    """
    from .findings import generate_findings_document

    if judge_document is None:
        reason = ('No semantic Judge result exists, so no Finding could be derived from a '
                  'judgment')
        output = run.envelope('findings', 'insufficient_evidence', reason, None, parent_ids)
        run.manifest['stages']['findings']['processor'] = {'name': 'none', 'version': '1.0.0'}
        return [output], [], reason

    document = generate_findings_document(judge_document, timeline_doc, metrics_result,
                                          run_id=run.manifest['run_id'],
                                          analysis_id=run.manifest['analysis_id'])
    if document['rejected']:
        raise ValueError('Generated findings violate the Finding contract: '
                         + '; '.join(item['reason'] for item in document['rejected']))
    path = run.analysis / 'findings-document.json'
    write_json(path, document)
    findings_id = run.register(path, 'findings', list(parent_ids), 'findings:2.1.0')
    status = 'complete' if document['findings'] else 'insufficient_evidence'
    reason = (None if document['findings'] else
              'No finding candidate cited resolvable evidence; see the recorded abstentions')
    output = run.envelope('findings', status,
                          reason or 'Evidence-linked Finding 2.1.0 documents',
                          _envelope_data(document, status), list(parent_ids) + [findings_id],
                          data_artifact_ref=findings_id)
    run.manifest['stages']['findings']['processor'] = {'name': 'findings', 'version': '2.1.0'}
    return [output, findings_id], document['findings'], reason


# Every analysis stage owns exactly one envelope kind. A stage that did not run
# still has to publish its evidence state; a stage left 'pending' with no output
# would let the Run imply work that never happened.
STAGE_KINDS = (
    ('asr', 'transcript'),
    ('acoustic', 'acoustic-segments'),
    ('diarization', 'speaker-assignments'),
    ('attribution', 'attribution'),
    ('fusion', 'fused-segments'),
    ('turns', 'turns'),
    ('timeline', 'timeline'),
    ('metrics', 'metrics'),
    ('judge', 'judge-results'),
    ('findings', 'findings'),
)
UNRUN_REASONS = {
    'asr': 'No ASR provider configured for this import; transcription was not performed',
    'acoustic': 'Acoustic segmentation did not run for this import',
    'diarization': 'Diarization provider or ASR speaker evidence unavailable; speaker clustering unavailable',
    'attribution': 'Source attribution did not run because upstream speaker evidence is unavailable',
    'fusion': 'Fusion did not run because acoustic or transcript evidence is unavailable',
    'turns': 'Turn building did not run because fused speaker segments are unavailable',
    'timeline': 'Event detection did not run because fused segments or turns are unavailable',
    'metrics': 'Metric computation did not run because the event timeline is unavailable',
    'judge': ('No semantic Judge provider is configured, or no eligible Turn exists; '
              'no semantic judgment was performed'),
    'findings': ('No accepted semantic judgment exists to derive a Finding from; '
                 'no Finding was created'),
}
# Stages gated on upstream speaker/acoustic evidence. By the end of an import they
# can no longer become available, so they report insufficient_evidence instead of
# pending. 'pending' is reserved for processors this milestone simply does not run
# (asr without a configured provider).
#
# `judge`/`findings` are implemented processors (#10): leaving them 'pending'
# would read as "not implemented" when the real state is "an evidence or
# configuration precondition was not met".
EVIDENCE_GATED_STAGES = ('acoustic', 'diarization', 'attribution', 'fusion', 'turns', 'timeline',
                         'metrics', 'judge', 'findings')


def _resolve_unrun_stages(run, normalized_ok):
    """Publish an evidence state for every stage that did not produce an output.

    A stage without canonical audio is reported as insufficient_evidence. A stage
    whose processor is simply not configured for this milestone keeps its own
    status but still publishes an envelope, so no stage is silently absent.
    Never fabricates data: unrun stages carry data=null.
    """
    stages = run.manifest['stages']
    for stage_name, kind in STAGE_KINDS:
        stage = stages[stage_name]
        if stage['status'] not in ('pending', 'insufficient_evidence'):
            continue
        if (run.analysis / f'{kind}.json').exists():
            continue
        if stage['status'] == 'pending':
            stage['reason'] = UNRUN_REASONS[stage_name] if normalized_ok \
                else 'Canonical audio unavailable; dependent analysis was not run'
        if not normalized_ok or stage_name in EVIDENCE_GATED_STAGES:
            stage['status'] = 'insufficient_evidence'
        output = run.envelope(kind, stage['status'], stage['reason'] or UNRUN_REASONS[stage_name])
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
        result, artifact_id, metadata_id = normalized

        def emit_qa():
            qa = result.metadata['normalized']
            insufficient = [item['condition_id'] for item in qa.get('conditions', [])
                            if item['status'] == 'insufficient']
            if insufficient:
                # The envelope must abstain (an abstaining evidence state cannot carry
                # measurement data), so the real measurements are published as their own
                # registered document. Abstention never deletes evidence, and the Run
                # still points at every related document through the envelope's refs.
                reason = ('Canonical audio measured, but Audio QA conditions are not satisfied: '
                          + ', '.join(insufficient))
                conditions = run.analysis / 'audio-qa-conditions.json'
                write_json(conditions, {'schema_version': '1.0.0', 'run_id': run.manifest['run_id'],
                    'analysis_id': run.manifest['analysis_id'], 'recording_sha256': run.manifest['original_sha256'],
                    'status': 'insufficient_evidence', 'reason': reason, 'measurements': qa})
                conditions_id = run.register(conditions, 'audio_qa_conditions', [artifact_id, metadata_id],
                                             'audio_qa:1.0.0')
                refs = [artifact_id, metadata_id, conditions_id]
                item = run.envelope('audio-qa', 'insufficient_evidence', reason, None, refs)
                # Every artifact this stage produced belongs in its own ledger entry, or a
                # machine consumer enumerating stage outputs misses the published measurements.
                return [item, conditions_id], qa, reason
            reason = ('Measurements only; no acceptance threshold configured and no '
                      'recognition or measurement accuracy is claimed')
            # `data` is the measurements member of the canonical metadata document, so the
            # "Canonical document" reference must be that JSON artifact - never the WAV,
            # which a consumer following the reference cannot parse for the envelope's data.
            item = run.envelope('audio-qa', 'complete', reason, qa, [artifact_id, metadata_id],
                                data_artifact_ref=metadata_id)
            # The envelope carries the precise evidence state; the stage reason is what
            # makes that state visible in the Run ledger next to the other stages.
            return [item], qa, None

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
        _run_evidence_chain(run, normalized, asr_provider_factory is not None, providers)
    _resolve_unrun_stages(run, normalized is not None)
    _retain_failures(run)
    run.checkpoint()
    run.execute('report', [item['artifact_id'] for item in run.manifest['artifacts']], lambda: write_import_report(run))
    errors = recording_run_errors(run.manifest, run.directory)
    if errors:
        raise ValueError('Import artifact validation failed: ' + '\n'.join(errors))
    run.checkpoint()
    return run.directory, run.manifest


def _run_evidence_chain(run, normalized, asr_available, providers=None):
    """Run acoustic → diarization → attribution → fusion → turns → timeline → metrics.

    Each stage is independent: failure in one does not prevent the report stage.
    Uses lazy imports so missing modules don't crash the import pipeline.
    """
    result, norm_art_id, _metadata_id = normalized
    norm_path = result.path

    # Acoustic segmentation — parent: normalized_audio
    acoustic_result = run.execute('acoustic', [norm_art_id],
        lambda: _acoustic(run, norm_path, norm_art_id))

    acoustic_art_id = None
    if run.manifest['stages']['acoustic']['output_artifact_ids']:
        acoustic_art_id = run.manifest['stages']['acoustic']['output_artifact_ids'][-1]

    # Load the completed transcript and its invocation evidence BEFORE diarization
    # so speaker clusters can be derived from the existing ASR call.
    transcript_doc = None
    asr_art_id = None
    asr_invocation = _asr_invocation(run)
    asr_stage = run.manifest['stages']['asr']
    # A partial ASR result can still carry usable utterance and speaker-label
    # evidence (for example, one word-level timestamp may be unavailable).
    # Do not discard that valid sentence-level evidence before diarization.
    if asr_stage['status'] in ('complete', 'partial'):
        transcript_path = run.analysis / 'transcript.json'
        if transcript_path.exists():
            envelope_data = json.loads(transcript_path.read_text(encoding='utf-8'))
            transcript_doc = envelope_data.get('data')
            if asr_stage['output_artifact_ids']:
                asr_art_id = asr_stage['output_artifact_ids'][-1]

    # Diarization — parents: transcript (preferred) or acoustic_segments
    # Only runs if a real diarization provider is configured.
    diar_provider = None
    if providers is not None and hasattr(providers, 'diarization') and providers.diarization is not None:
        diar_provider = providers.diarization(run.directory)

    diar_parents = [p for p in [asr_art_id, acoustic_art_id, norm_art_id] if p]
    diarization_result = run.execute('diarization', diar_parents,
        lambda: _diarize(run, norm_path, acoustic_result, acoustic_art_id or norm_art_id,
                         diar_provider, transcript_doc, asr_invocation))

    diar_art_id = None
    if run.manifest['stages']['diarization']['output_artifact_ids']:
        diar_art_id = run.manifest['stages']['diarization']['output_artifact_ids'][-1]

    # Source attribution — parent: diarization output
    # Role assignment is user-owned. The current explicit mapping seam is used by
    # fixtures and by the persisted manual revisions; no LLM is consulted here.
    explicit_mapping = None
    if hasattr(run, '_explicit_speaker_mapping'):
        explicit_mapping = run._explicit_speaker_mapping

    _run_role_dependent_chain(run, acoustic_result, transcript_doc, diarization_result,
                              acoustic_art_id=acoustic_art_id, diar_art_id=diar_art_id,
                              asr_art_id=asr_art_id, norm_art_id=norm_art_id,
                              explicit_mapping=explicit_mapping, providers=providers)


def _run_role_dependent_chain(run, acoustic_result, transcript_doc, diarization_result, *,
                              acoustic_art_id=None, diar_art_id=None, asr_art_id=None,
                              norm_art_id=None, explicit_mapping=None, providers=None):
    """Run attribution → fusion → turns → timeline → Judge → metrics → findings.

    Split out of `_run_evidence_chain` so a saved human role mapping can rerun
    exactly this suffix in a new AnalysisRevision without recomputing recognition or
    clustering. Role assignment is user-owned: an unknown cluster stays unknown and
    the role-dependent stages abstain rather than guessing.

    `providers.judge` is the configured semantic Judge. It is passed through so a
    role revision re-runs the same semantic stage, and so a revision cannot
    silently inherit a different semantic configuration.
    """
    attr_parents = [diar_art_id] if diar_art_id else [acoustic_art_id or norm_art_id]
    attribution_result = run.execute('attribution', attr_parents,
        lambda: _attribute(run, diarization_result, diar_art_id or (acoustic_art_id or norm_art_id),
                           explicit_mapping))

    attr_art_id = None
    if run.manifest['stages']['attribution']['output_artifact_ids']:
        attr_art_id = run.manifest['stages']['attribution']['output_artifact_ids'][-1]

    # Fusion — parents: acoustic + diarization + attribution (+ transcript)
    if acoustic_result and acoustic_result.get('segments'):
        acoustic_doc = acoustic_result
        fusion_parents = [p for p in [acoustic_art_id, diar_art_id, attr_art_id, asr_art_id] if p]
        if not fusion_parents:
            fusion_parents = [norm_art_id]
        fusion_result = run.execute('fusion', fusion_parents,
            lambda: _fusion_with_speakers(run, acoustic_doc, transcript_doc,
                                         diarization_result, attribution_result, fusion_parents))

        fusion_art_id = None
        if run.manifest['stages']['fusion']['output_artifact_ids']:
            fusion_art_id = run.manifest['stages']['fusion']['output_artifact_ids'][-1]

        # Turns — parent: fusion
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

                if turns_result and turns_result.get('turns'):
                    turns_doc = turns_result
                    tl_parents = [p for p in [fusion_art_id, turns_art_id] if p]
                    timeline_result = run.execute('timeline', tl_parents,
                        lambda: _timeline(run, fused_doc, turns_doc, tl_parents))

                    timeline_art_id = None
                    if run.manifest['stages']['timeline']['output_artifact_ids']:
                        timeline_art_id = run.manifest['stages']['timeline']['output_artifact_ids'][-1]

                    if timeline_result and timeline_result.get('events') is not None and timeline_art_id:
                        timeline_doc = timeline_result
                        # Judge → constrained semantic evidence → metrics → findings.
                        # The Judge stage runs first because a published metric
                        # document is immutable: PRD-M003/M006 must be computed once,
                        # with the semantic records the Judge produced. The Judge
                        # receives the deterministic metrics in memory as context, so
                        # deterministic values still take precedence.
                        judge_provider = None
                        if providers is not None and getattr(providers, 'judge', None) is not None:
                            judge_provider = providers.judge
                        judge_stage = run.execute('judge', [timeline_art_id],
                            lambda: _judge(run, fused_doc, turns_doc, timeline_doc,
                                           [timeline_art_id], judge_provider))
                        judge_document, semantic_records = None, None
                        if judge_stage is not None:
                            judge_document, semantic_records = judge_stage
                        metrics_result = run.execute('metrics', [timeline_art_id],
                            lambda: _metrics(run, timeline_doc, [timeline_art_id],
                                             semantic_records))
                        if judge_document is not None:
                            findings_parents = [p for p in
                                                [run.manifest['stages']['judge']['output_artifact_ids'][0]
                                                 if run.manifest['stages']['judge']['output_artifact_ids']
                                                 else None,
                                                 timeline_art_id] if p]
                            run.execute('findings', findings_parents,
                                lambda: _findings(run, judge_document, timeline_doc,
                                                  metrics_result, findings_parents))
            else:
                _abstain_role_dependent_stages(run, fusion_art_id or norm_art_id,
                                               _role_gate_reason(run, diarization_result))
    # Every revision publishes its own gate state and review surface, so a reader can
    # see which decisions that exact revision was produced from.
    _publish_role_review(run, diarization_result, transcript_doc)
    return attribution_result


def _publish_role_review(run, diarization_doc, transcript_doc):
    """Register the role-review surface and gate state as chain evidence."""
    from .role_review import REVIEW_KIND, build_role_review
    from .validation import schema_errors
    document = build_role_review(run.directory, diarization_doc, transcript_doc)
    errors = schema_errors(document, 'role-review')
    if errors:
        raise ValueError('Invalid role review document: ' + '\n'.join(errors))
    path = run.analysis / 'role-review.json'
    write_json(path, document)
    artifact_id = run.register(path, REVIEW_KIND, processor='role_review:1.0.0')
    run.manifest['stages']['attribution']['output_artifact_ids'].append(artifact_id)
    return document


def _role_gate_reason(run, diarization_doc):
    """Name the exact gate that blocks role-dependent stages.

    An anonymous cluster waiting for a human decision is a different state from a
    Run that produced no clusters at all, and from a complete mapping in which the
    user deliberately chose `unknown` for everything.
    """
    from .role_review import load_revisions, review_status
    cluster_ids = list(dict.fromkeys(segment['speaker_id']
                                     for segment in (diarization_doc or {}).get('speaker_segments') or []))
    revisions = load_revisions(run.directory)
    decisions = revisions[-1]['decisions'] if revisions else {}
    status, detail = review_status(cluster_ids, decisions)
    if not cluster_ids:
        return 'No speaker roles are known: ' + detail
    if status == 'complete_review':
        return ('All speaker clusters have a user decision, but none is tester/device; '
                'role-dependent stages cannot run from an all-unknown mapping')
    return f'{status}: {detail}'


def _abstain_role_dependent_stages(run, parent, reason):
    """Publish an explicit insufficient_evidence envelope for each blocked stage.

    Semantic stages are included: with anonymous clusters there is no eligible
    Turn for the Judge to judge, which is an evidence state and must be visible
    as one instead of leaving the stage looking merely unimplemented.
    """
    kinds = {'turns': 'turns', 'timeline': 'timeline', 'metrics': 'metrics',
             'judge': 'judge-results', 'findings': 'findings'}
    for stage_name in ('turns', 'timeline', 'metrics', 'judge', 'findings'):
        stage = run.manifest['stages'][stage_name]
        stage['status'] = 'insufficient_evidence'
        stage['reason'] = reason
        stage['input_artifact_ids'] = [parent]
        output = run.envelope(kinds[stage_name], 'insufficient_evidence', stage['reason'],
                              refs=[parent])
        stage['output_artifact_ids'].append(output)


def _configuration_refs(run):
    return [a['artifact_id'] for a in run.manifest['artifacts']
            if a['kind'] == 'model_configuration' and a['path'].startswith(run.analysis.relative_to(run.directory).as_posix())]


def _run_evidence_chain_resume(run, directory, normalized_artifact, norm_art_id, providers=None):
    """Run evidence chain during resume (same logic as _run_evidence_chain but for resumed runs)."""
    norm_path = directory / normalized_artifact['path']
    class _Result:
        def __init__(self, path):
            self.path = path
    normalized = (_Result(norm_path), norm_art_id, None)
    _run_evidence_chain(run, normalized, run.manifest['stages']['asr']['status'] == 'complete', providers)


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
    # Run evidence chain after ASR restore/retry. Speaker clusters are rebuilt
    # from the preserved native response: no re-upload, no second recognition.
    _run_evidence_chain_resume(run, directory, normalized, parent, providers)
    _resolve_unrun_stages(run, True)
    _retain_failures(run)
    run.execute('report', [a['artifact_id'] for a in run.manifest['artifacts']], lambda: write_import_report(run))
    run.checkpoint()
    if recording_run_errors(run.manifest, directory):
        raise ValueError('Resumed Run evidence failed validation')
    return directory, run.manifest


def apply_role_mapping(directory, decisions, reviewer, *, reason='', model_snapshot=None,
                       evidence_refs=(), providers=None):
    """Create a new AnalysisRevision from one saved human role decision set.

    Caller must hold the Run lock. Recognition and speaker clustering are original
    machine evidence, so both are **restored** from the current revision instead of
    being recomputed: no second recognition request, no changed clustering, and no
    provider configuration is required to reanalyze. Only attribution and the stages
    downstream of it are recomputed, from the human mapping.

    `providers.judge` is the configured semantic Judge. A revision re-runs the
    semantic stage with the same configuration, so confirming roles cannot
    silently drop the Judge or change which model judged the Run.

    The previous revision's artifacts are never modified. Returns
    ``(directory, manifest, review_document)``.
    """
    import copy
    import uuid

    from .role_review import (REVISION_KIND, RoleReviewError, build_role_review,
                              save_revision, validate_decisions)

    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if recording_run_errors(manifest, directory):
        raise ValueError('Run evidence failed integrity validation')
    current = directory / 'analysis' / manifest['analysis_id']

    diarization_envelope = json.loads((current / 'speaker-assignments.json').read_text(encoding='utf-8'))
    diarization_doc = diarization_envelope.get('data') or {}
    cluster_ids = list(dict.fromkeys(segment['speaker_id']
                                     for segment in diarization_doc.get('speaker_segments') or []))
    if not cluster_ids:
        raise RoleReviewError('This Run produced no speaker clusters; a role decision cannot be applied')
    resolved = validate_decisions(decisions, cluster_ids)

    asr_stage = manifest['stages']['asr']
    if asr_stage['status'] not in ('complete', 'partial') or not (current / 'transcript.json').is_file():
        raise RoleReviewError('A preserved timestamped transcript is required before roles can be applied')
    transcript_envelope = json.loads((current / 'transcript.json').read_text(encoding='utf-8'))
    acoustic_envelope = json.loads((current / 'acoustic-segments.json').read_text(encoding='utf-8'))
    acoustic_doc = acoustic_envelope.get('data') or {}

    # One immutable revision file per save; the index is derived from what exists.
    revision_path, revision = save_revision(
        directory, resolved, reviewer, reason=reason,
        analysis_id=manifest['analysis_id'],
        recording_sha256=manifest.get('original_sha256'),
        diarization_document_id=diarization_doc.get('document_id'),
        evidence_refs=evidence_refs)

    run = ImportRun.__new__(ImportRun)
    run.directory, run.manifest = directory, copy.deepcopy(manifest)
    checkpoint = current / 'manifest-snapshot.json'
    if checkpoint.exists():
        if json.loads(checkpoint.read_text(encoding='utf-8')) != manifest:
            raise ValueError('Conflicting analysis checkpoint')
    else:
        write_json(checkpoint, manifest)
    run.register(checkpoint, 'analysis_checkpoint', processor='role_review:1.0.0')
    revision_ref = run.register(revision_path, REVISION_KIND, processor=f'role_review:{revision["revision_index"]}')
    run.manifest['analysis_id'] = 'ANALYSIS-' + uuid.uuid4().hex
    run.analysis = directory / 'analysis' / run.manifest['analysis_id']
    run.analysis.mkdir()
    for name, stage in run.manifest['stages'].items():
        if name not in ('ingestion', 'normalization', 'audio_qa'):
            stage.update(status='pending', started_at=None, finished_at=None, latency_ms=None,
                         input_artifact_ids=[], output_artifact_ids=[], reason='Processor not run in this revision')
    run.manifest['status'] = 'partial'
    _save_configuration(run, model_snapshot)
    run.checkpoint()

    def restore(kind, envelope, parents):
        """Re-publish preserved evidence into this revision without recomputing it.

        The envelope's own artifact refs are carried over because they are already
        registered in this Run; `data_artifact_ref` must stay one of them or the
        canonical document reference would dangle.
        """
        refs = [ref for ref in (list(envelope.get('artifact_refs') or []) + list(parents))]
        item = run.envelope(kind, envelope['status'], envelope['reason'], envelope['data'],
                            refs, envelope.get('data_artifact_ref'))
        return [item], envelope.get('data'), None

    preserved_parents = [revision_ref] + _configuration_refs(run)
    run.execute('asr', preserved_parents, lambda: restore('transcript', transcript_envelope, preserved_parents))
    run.manifest['stages']['asr']['processor'] = asr_stage['processor']
    asr_art_id = run.manifest['stages']['asr']['output_artifact_ids'][-1]

    run.execute('acoustic', [asr_art_id], lambda: restore('acoustic-segments', acoustic_envelope, [asr_art_id]))
    run.manifest['stages']['acoustic']['processor'] = manifest['stages']['acoustic']['processor']
    acoustic_art_id = run.manifest['stages']['acoustic']['output_artifact_ids'][-1]

    run.execute('diarization', [acoustic_art_id], lambda: restore('speaker-assignments', diarization_envelope, [acoustic_art_id]))
    run.manifest['stages']['diarization']['processor'] = manifest['stages']['diarization']['processor']
    diar_art_id = run.manifest['stages']['diarization']['output_artifact_ids'][-1]

    run._explicit_speaker_mapping = resolved
    _run_role_dependent_chain(run, acoustic_doc, transcript_envelope.get('data'), diarization_doc,
                              acoustic_art_id=acoustic_art_id, diar_art_id=diar_art_id,
                              asr_art_id=asr_art_id, norm_art_id=revision_ref,
                              explicit_mapping=resolved, providers=providers)
    _resolve_unrun_stages(run, True)
    _retain_failures(run)
    run.execute('report', [a['artifact_id'] for a in run.manifest['artifacts']],
                lambda: write_import_report(run))
    run.checkpoint()
    if recording_run_errors(run.manifest, directory):
        raise ValueError('Role reanalysis produced invalid Run evidence')
    return directory, run.manifest, build_role_review(directory)
