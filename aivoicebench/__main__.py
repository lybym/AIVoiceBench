"""Contract CLI; hardware execution is introduced in later issues."""

import argparse
import json
from pathlib import Path
import sys
import wave

from .validation import (case_errors, load_document, timeline_errors, metric_errors,
                         finding_errors, transcript_errors, acoustic_errors, fused_errors,
                         turns_errors, judge_result_errors, judge_document_errors)


def _load_json_document(path):
    """Read one JSON document for the CLI; native diagnostics stay in caller logs."""
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise ValueError(f'Cannot read JSON document {path}: {error}') from None


def _print_speaker_summary(directory, manifest):
    """Print the speaker-clustering slice without claiming a role was resolved.

    Clustering answers "which segments share a speaker". It never answers "who is
    the tester". The invocation reference shows the clusters came from the ASR
    response that was already paid for, not from a second recognition request.
    """
    import json
    envelope = directory / 'analysis' / manifest['analysis_id'] / 'speaker-assignments.json'
    if not envelope.exists():
        return
    try:
        data = json.loads(envelope.read_text(encoding='utf-8')).get('data') or {}
    except (OSError, ValueError):
        return
    segments = data.get('speaker_segments') or []
    if not segments:
        print('  speaker clusters: none (no speaker separation evidence in this Run)')
        return
    labels = sorted({str(s.get('native_speaker_id')) for s in segments})
    clusters = {s.get('speaker_id') for s in segments}
    print(f'  speaker clusters: {len(clusters)} cluster(s), {len(segments)} segment(s), '
          f'native labels {", ".join(labels)}')
    print('  speaker roles: unresolved — clustering is not attribution')
    scope = data.get('scope') or {}
    if scope.get('invocation_id'):
        print(f'  diarization evidence: {scope["invocation_id"]} '
              f'(derived from the existing ASR response; no second recognition request)')


def main(argv=None):
    # Keep redirected Windows CLI JSON/text readable across shell code pages.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(prog='aivoicebench')
    subparsers = parser.add_subparsers(dest='command', required=True)
    importer = subparsers.add_parser('import', help='Import an existing WAV/MP3/M4A into a recoverable analysis Run')
    importer.add_argument('source', type=Path)
    importer.add_argument('--output', type=Path, default=Path('artifacts/imports'))
    importer.add_argument('--profile', type=Path, help='Device/hardware/firmware/model/prompt/supplier/environment/notes JSON')
    importer.add_argument('--ffmpeg', type=Path)
    importer.add_argument('--ffprobe', type=Path)
    importer.add_argument('--synthetic', action='store_true', help='Explicitly label generated/unit test audio')
    importer.add_argument('--asr-provider', choices=['vosk'], help='Optional offline fallback; no cloud upload by default')
    importer.add_argument('--model-settings', type=Path, help='Explicit configured cloud ASR route; may upload to the selected service')
    importer.add_argument('--model-dir', type=Path)
    importer.add_argument('--model-version')
    analyze = subparsers.add_parser('analyze', help='Evaluate canonical events with deterministic metrics')
    analyze.add_argument('case', type=Path)
    analyze.add_argument('--timeline', type=Path, required=True)
    analyze.add_argument('--artifact-root', type=Path)
    analyze.add_argument('--turn-id')
    analyze.add_argument('--output', type=Path, default=Path('artifacts/analysis'))
    asr = subparsers.add_parser('asr', help='Transcribe a local WAV with explicit provider/model selection')
    asr.add_argument('source', type=Path)
    asr.add_argument('--provider', choices=['vosk'], required=True)
    asr.add_argument('--model-dir', type=Path, required=True)
    asr.add_argument('--model-version', required=True)
    asr.add_argument('--source-role', choices=['stimulus', 'device_output', 'room_mix', 'unknown'], default='unknown')
    asr.add_argument('--channel', type=int, choices=[1, 2])
    asr.add_argument('--run-id')
    asr.add_argument('--case-id')
    asr.add_argument('--output', type=Path, default=Path('artifacts/asr'))
    acoustic = subparsers.add_parser('acoustic', help='Detect speech segments from a canonical WAV (energy or Silero VAD)')
    acoustic.add_argument('source', type=Path)
    acoustic.add_argument('--output', type=Path, default=Path('artifacts/acoustic'))
    acoustic.add_argument('--vad', choices=['energy', 'silero'],
                          help='Boundary provider family (default: AIVOICEBENCH_ACOUSTIC_PROVIDER, '
                               'else energy). silero requires the optional VAD runtime and never '
                               'falls back to energy silently.')
    acoustic.add_argument('--policy', help='Named policy version for the selected provider '
                                           '(for example 1.0.0 for silero)')
    acoustic.add_argument('--frame-ms', type=float, default=30.0)
    acoustic.add_argument('--hop-ms', type=float, default=10.0)
    acoustic.add_argument('--threshold-factor', type=float, default=0.15)
    acoustic.add_argument('--min-speech-ms', type=float, default=100.0)
    acoustic.add_argument('--min-silence-ms', type=float, default=200.0)
    acoustic.add_argument('--merge-gap-ms', type=float, default=80.0)
    acoustic.add_argument('--pre-roll-ms', type=float, default=0.0)
    acoustic.add_argument('--post-roll-ms', type=float, default=0.0)
    vad_eval = subparsers.add_parser(
        'vad-eval',
        help='Score produced acoustic boundaries against a manually annotated real recording (#23 AC3)')
    vad_eval.add_argument('--annotation', type=Path,
                          help='VadAnnotation 1.0.0 JSON; omit to report that no annotated sample set exists')
    vad_eval.add_argument('--acoustic', nargs='*', type=Path, default=[],
                          help='Produced AcousticSegments 1.0.0 documents to score')
    vad_eval.add_argument('--output', type=Path)
    fusion = subparsers.add_parser('fusion', help='Fuse acoustic segments, build turns, detect events, generate timeline')
    fusion.add_argument('acoustic', type=Path, help='Path to acoustic-segments JSON')
    fusion.add_argument('--transcript', type=Path, help='Optional ASR transcript JSON')
    fusion.add_argument('--output', type=Path, default=Path('artifacts/fusion'))
    fusion.add_argument('--timeout-ms', type=float, default=5000.0)
    fusion.add_argument('--false-endpoint-ms', type=float, default=300.0)
    metrics_cmd = subparsers.add_parser('metrics', help='Compute expanded latency metrics from a timeline')
    metrics_cmd.add_argument('timeline', type=Path, help='Path to EventTimeline JSON')
    metrics_cmd.add_argument('--output', type=Path)
    judge = subparsers.add_parser('judge', help='Run LLM semantic evaluation on fusion output')
    judge.add_argument('fused', type=Path, help='Path to fused-segments JSON')
    judge.add_argument('--turns', type=Path, required=True, help='Path to turns JSON')
    judge.add_argument('--metrics', type=Path, required=True, help='Path to metrics JSON')
    judge.add_argument('--timeline', type=Path, help='Path to EventTimeline JSON; required for an '
                                                    'evidence-linked semantic verdict')
    judge.add_argument('--output', type=Path, default=Path('artifacts/judge'))
    judge.add_argument('--provider', choices=['none', 'mock'], default='none', help='LLM provider (mock for testing)')
    audio = subparsers.add_parser('audio', help='Optional explicit audio station commands')
    audio_commands = audio.add_subparsers(dest='audio_command', required=True)
    audio_commands.add_parser('devices', help='List devices without opening a recording stream')
    capture = audio_commands.add_parser('capture', help='Play WAV and record the explicitly selected input')
    capture.add_argument('stimulus', type=Path)
    calibration = audio_commands.add_parser('calibrate', help='Play probe and record a physically connected loopback path')
    for command in (capture, calibration):
        command.add_argument('--input-device', type=int, required=True)
        command.add_argument('--output-device', type=int, required=True)
        command.add_argument('--output', type=Path, default=Path('artifacts/audio'))
    capture.add_argument('--input-channels', type=int, choices=[1, 2], default=1)
    capture.add_argument('--output-channels', type=int, choices=[1, 2], default=1)
    capture.add_argument('--pre-roll-ms', type=int, default=500)
    capture.add_argument('--tail-ms', type=int, default=5000)
    calibration.add_argument('--repetitions', type=int, default=3)
    run = subparsers.add_parser('run', help='Prepare one Case or suite; no hardware adapter yet')
    run.add_argument('path', type=Path)
    run.add_argument('--output', type=Path, default=Path('artifacts/runs'))
    run.add_argument('--asset-root', type=Path)
    run.add_argument('--device-profile', type=Path)
    run.add_argument('--dry-run', action='store_true', help='Only prepare assets/contracts, never capture hardware')
    findings_cmd = subparsers.add_parser('findings', help='Generate Finding 2.0.0 from judge results')
    findings_cmd.add_argument('--judge', type=Path, required=True, help='Judge results JSON')
    findings_cmd.add_argument('--timeline', type=Path, required=True, help='EventTimeline JSON')
    findings_cmd.add_argument('--metrics', type=Path, required=True, help='Metrics JSON')
    findings_cmd.add_argument('--output', type=Path, default=Path('artifacts/findings/findings.json'))
    report_cmd = subparsers.add_parser('report', help='Render Markdown + JSON report from pipeline outputs')
    report_cmd.add_argument('--output', type=Path, default=Path('artifacts/report'))
    report_cmd.add_argument('--profile', type=Path)
    report_cmd.add_argument('--fused', type=Path, required=True)
    report_cmd.add_argument('--turns', type=Path, required=True)
    report_cmd.add_argument('--timeline', type=Path, required=True)
    report_cmd.add_argument('--metrics', type=Path, required=True)
    report_cmd.add_argument('--judge', type=Path, required=True)
    report_cmd.add_argument('--findings', type=Path)
    pipeline_cmd = subparsers.add_parser('pipeline', help='Run full analysis: acoustic→fusion→metrics→judge→findings→report')
    pipeline_cmd.add_argument('source', type=Path, help='Path to canonical WAV (PCM16 16kHz mono)')
    pipeline_cmd.add_argument('--output', type=Path, default=Path('artifacts/pipeline'))
    pipeline_cmd.add_argument('--profile', type=Path, help='JSON profile (device/hardware/firmware/...)')
    pipeline_cmd.add_argument('--frame-ms', type=float, default=30.0)
    pipeline_cmd.add_argument('--hop-ms', type=float, default=10.0)
    pipeline_cmd.add_argument('--min-speech-ms', type=float, default=100.0)
    pipeline_cmd.add_argument('--min-silence-ms', type=float, default=200.0)
    pipeline_cmd.add_argument('--timeout-ms', type=float, default=5000.0)
    pipeline_cmd.add_argument('--false-endpoint-ms', type=float, default=300.0)
    pipeline_cmd.add_argument('--vad', choices=['energy', 'silero'],
                              help='Acoustic-boundary provider family (default: '
                                   'AIVOICEBENCH_ACOUSTIC_PROVIDER, else energy)')
    pipeline_cmd.add_argument('--policy', help='Named boundary policy version for the silero provider')
    revise_cmd = subparsers.add_parser('revise', help='Add a human revision to machine output')
    revise_cmd.add_argument('--output', type=Path, default=Path('artifacts/revisions'))
    revise_cmd.add_argument('--target-type', choices=['segment', 'event', 'turn', 'finding', 'transcript'], required=True)
    revise_cmd.add_argument('--target-id', required=True)
    revise_cmd.add_argument('--field', required=True)
    revise_cmd.add_argument('--revised-value', required=True)
    revise_cmd.add_argument('--reviewer', required=True)
    revise_cmd.add_argument('--reason', default='')
    validate = subparsers.add_parser('validate', help='Validate TestCase JSON/YAML without hardware or network')
    validate.add_argument('paths', nargs='+', type=Path)
    validate.add_argument('--kind', choices=['test-case', 'timeline', 'metric', 'finding', 'transcript', 'acoustic-segments', 'fused-segments', 'turns', 'judge-result', 'judge-results'], default='test-case')
    validate.add_argument('--timeline', type=Path, help='Required context for metric references')
    validate.add_argument('--metrics', nargs='*', type=Path, default=[], help='MetricResult files referenced by a finding')
    validate.add_argument('--turns', type=Path, help='Turns document; resolves turn references of a Judge artifact')
    validate.add_argument('--regression-case', type=Path, help='Linked TestCase for a frozen regression candidate')
    args = parser.parse_args(argv)
    if args.command == 'import':
        import yaml
        from .audio_processing import FFmpegAudioProcessor
        from .import_pipeline import import_recording
        factory = None
        snapshot = providers = None
        if args.model_settings and args.asr_provider:
            parser.error('Choose --model-settings or --asr-provider, not both')
        if args.model_settings:
            from .model_settings import ModelSettings
            snapshot, providers = ModelSettings(args.model_settings).capture()
        if args.asr_provider == 'vosk':
            def factory():
                from .asr import VoskProvider
                if args.model_dir is None or args.model_version is None:
                    raise ValueError('Vosk requires model directory and version')
                return VoskProvider(args.model_dir, args.model_version)
        try:
            directory, manifest = import_recording(args.source, args.output,
                profile=load_document(args.profile) if args.profile else None,
                audio_processor=FFmpegAudioProcessor(args.ffmpeg, args.ffprobe),
                asr_provider_factory=factory, synthetic=args.synthetic, model_snapshot=snapshot, providers=providers)
            print(f'{manifest["status"].upper()} {directory} ({manifest["execution_kind"]}; retained import Run)')
            for name, stage in manifest['stages'].items():
                print(f'  {name}: {stage["status"]}')
            _print_speaker_summary(directory, manifest)
            print(f'Report: {directory / "analysis" / manifest["analysis_id"] / "report.md"}')
            return 1 if any(stage['status'] == 'failed' for stage in manifest['stages'].values()) else 2
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f'IMPORT ERROR: {error}', file=sys.stderr)
            return 1
    if args.command == 'analyze':
        import uuid
        import yaml
        from .engine import evaluate
        from .runner import write_json, digest
        try:
            case, timeline = load_document(args.case), load_document(args.timeline)
            metrics = evaluate(case, timeline, args.artifact_root, args.turn_id)
            directory = args.output / ('ANALYSIS-' + uuid.uuid4().hex)
            directory.mkdir(parents=True, exist_ok=False)
            write_json(directory / 'case.json', case)
            write_json(directory / 'timeline.json', timeline)
            write_json(directory / 'metrics.json', metrics)
            write_json(directory / 'analysis.json', {'execution_kind': timeline['execution_kind'],
                'case_sha256': digest(directory / 'case.json'), 'timeline_sha256': digest(directory / 'timeline.json'),
                'metrics_sha256': digest(directory / 'metrics.json'),
                'artifact_root': str(args.artifact_root.resolve()) if args.artifact_root else None,
                'note': 'Canonical event evaluation; no new recording or internal-root-cause inference'})
            print(f'ANALYZED {directory} ({timeline["execution_kind"]})')
            return 0
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f'ANALYSIS ERROR: {error}', file=sys.stderr)
            return 1
    if args.command == 'asr':
        from .asr import VoskProvider, transcribe_file
        try:
            provider = VoskProvider(args.model_dir, args.model_version)
            directory, transcript = transcribe_file(args.source, provider, args.output, args.source_role,
                                                    args.channel, args.run_id, args.case_id)
            print(f'{transcript["status"].upper()} {directory} (external ASR; provider-estimated audio-relative timestamps)')
            return 0 if transcript['status'] == 'complete' else 2
        except (OSError, ValueError, EOFError, wave.Error) as error:
            print(f'ASR ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'acoustic':
        from .acoustic import AcousticError, EnergyVadSegmenter, segment_audio
        try:
            if args.vad == 'silero':
                # A model provider is selected explicitly; it never substitutes the
                # energy VAD, and any unavailable runtime/model raises below.
                segmenter = None
                provider_kwargs = {'vad': 'silero', 'policy': args.policy}
            elif args.vad == 'energy':
                segmenter = EnergyVadSegmenter(
                    frame_ms=args.frame_ms, hop_ms=args.hop_ms,
                    threshold_factor=args.threshold_factor,
                    min_speech_ms=args.min_speech_ms,
                    min_silence_ms=args.min_silence_ms,
                    merge_gap_ms=args.merge_gap_ms,
                    pre_roll_ms=args.pre_roll_ms,
                    post_roll_ms=args.post_roll_ms)
                provider_kwargs = {}
            else:
                # No --vad flag: AIVOICEBENCH_ACOUSTIC_PROVIDER decides, with the
                # energy VAD still the default so an unconfigured deployment keeps
                # its existing behaviour.
                from .acoustic import resolve_segmenter
                segmenter = resolve_segmenter(
                    None, args.policy,
                    frame_ms=args.frame_ms, hop_ms=args.hop_ms,
                    threshold_factor=args.threshold_factor,
                    min_speech_ms=args.min_speech_ms,
                    min_silence_ms=args.min_silence_ms,
                    merge_gap_ms=args.merge_gap_ms,
                    pre_roll_ms=args.pre_roll_ms,
                    post_roll_ms=args.post_roll_ms)
                provider_kwargs = {}
            destination = args.output
            if destination.suffix.lower() != '.json':
                destination = destination / ('ACOUSTIC-' + __import__('uuid').uuid4().hex + '.json')
            document, out_path = segment_audio(args.source, destination, segmenter,
                                               **provider_kwargs)
            method = document['processor']['method']
            print(f'{document["status"].upper()} {out_path} '
                  f'({len(document["segments"])} acoustic segment(s) from {method}; '
                  'signal timing, no speaker role)')
            return 0 if document['status'] == 'complete' else 2
        except (OSError, ValueError, wave.Error) as error:
            print(f'ACOUSTIC ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'vad-eval':
        from .runner import write_json
        from .vad_evaluation import evaluate, load_annotation_file
        try:
            if args.annotation is None:
                # No annotated sample set: report that the evaluation was not
                # performed instead of implying a zero-error result.
                result = evaluate(None, [])
            else:
                result = evaluate(load_annotation_file(args.annotation), [
                    _load_json_document(path) for path in args.acoustic])
        except (OSError, ValueError) as error:
            print(f'VAD EVAL ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
        if result['status'] != 'evaluated':
            print(f'{result["status"].upper()}: {result["reason"]}')
        else:
            print(f'EVALUATED {result["denominator"]["annotated_intervals"]} annotated interval(s) '
                  f'over {result["denominator"]["documents_evaluated"]} document(s); '
                  f'matched {result["denominator"]["intervals_with_a_match"]}/'
                  f'{result["denominator"]["expected_matches"]}, '
                  f'false alarms {result["false_alarm"]["false_alarm_segments"]}')
        if args.output:
            write_json(args.output, result)
        return 0 if result['status'] == 'evaluated' else 2
    if args.command == 'fusion':
        from .fusion import analyze_segments
        try:
            fused, turns, timeline = analyze_segments(
                args.acoustic, args.transcript, args.output,
                timeout_ms=args.timeout_ms, false_endpoint_ms=args.false_endpoint_ms)
            n_segs = len(fused['segments'])
            n_turns = len(turns['turns'])
            n_events = len(timeline['events'])
            print(f'{timeline["status"].upper()} {args.output} ({n_segs} fused, {n_turns} turns, {n_events} events)')
            return 0 if timeline['status'] == 'complete' else 2
        except (OSError, ValueError) as error:
            print(f'FUSION ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'metrics':
        from .metrics import compute_timeline_metrics
        try:
            import json
            timeline = json.loads(args.timeline.read_text(encoding='utf-8'))
            result = compute_timeline_metrics(timeline)
            if args.output:
                write_json_result = args.output
                import uuid as _uuid
                from .runner import write_json as _wj
                _wj(write_json_result, result)
            observed = [m for m in result['metrics'] if m['status'] == 'observed']
            insufficient = [m for m in result['metrics'] if m['status'] == 'insufficient_evidence']
            print(f'{result["status"].upper()} ({len(observed)} observed, {len(insufficient)} insufficient)')
            for m in result['metrics']:
                val = m['value'] if m['value'] is not None else 'N/A'
                print(f'  {m["name"]}: {val} {m["unit"]} [{m["status"]}]')
            return 0 if result['status'] == 'observed' else 2
        except (OSError, ValueError) as error:
            print(f'METRICS ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'judge':
        from .llm import judge_pipeline, MockLLMProvider, UnavailableLLMProvider
        try:
            provider = MockLLMProvider() if args.provider == 'mock' else UnavailableLLMProvider()
            document = judge_pipeline(
                args.fused, args.turns, args.metrics, args.output, provider,
                timeline_path=args.timeline)
            results = document['results']
            observed = [r for r in results if r['status'] == 'observed']
            insufficient = [r for r in results if r['status'] == 'insufficient_evidence']
            print(f'{len(results)} judge results ({len(observed)} observed, {len(insufficient)} insufficient)')
            for r in results:
                val = ''
                if r.get('meaningful_response_start_ms') is not None:
                    val = f' ms_start={r["meaningful_response_start_ms"]}'
                elif r.get('semantic_decision') is not None:
                    val = f' decision={r["semantic_decision"]}'
                elif r.get('score') is not None:
                    val = f' score={r["score"]}'
                elif r.get('intent_label'):
                    val = f' label={r["intent_label"]}'
                elif r.get('finding_severity'):
                    val = f' severity={r["finding_severity"]} layer={r.get("suspected_layer")}'
                print(f'  {r["dimension"]}: {r["decision"]} [{r["status"]}]{val} conf={r["confidence"]}')
            for abstention in document['abstentions']:
                print(f'  abstained: {abstention["dimension"]} ({abstention["state"]}) — '
                      f'{abstention["reason"]}')
            return 0 if observed else 2
        except (OSError, ValueError) as error:
            print(f'JUDGE ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'findings':
        from .findings import generate_findings_from_files
        try:
            findings, out_path = generate_findings_from_files(
                args.judge, args.timeline, args.metrics, args.output)
            defects = [f for f in findings if f['kind'] == 'defect']
            observations = [f for f in findings if f['kind'] == 'observation']
            print(f'{len(findings)} findings ({len(defects)} defects, {len(observations)} observations)')
            for f in findings:
                print(f'  [{f["severity"] or "—"}] {f["title"]} ({f["status"]}) '
                      f'turns={",".join(f["turn_ids"]) or "—"}')
            return 0 if findings else 2
        except (OSError, ValueError) as error:
            print(f'FINDINGS ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'pipeline':
        from .pipeline import run_full_pipeline
        try:
            import yaml
            profile = None
            if args.profile:
                profile = load_document(args.profile) if args.profile.suffix in ('.json', '.yaml', '.yml') else None
                if profile is None:
                    import json
                    profile = json.loads(args.profile.read_text(encoding='utf-8'))
            result = run_full_pipeline(
                args.source, args.output, profile,
                frame_ms=args.frame_ms, hop_ms=args.hop_ms,
                min_speech_ms=args.min_speech_ms, min_silence_ms=args.min_silence_ms,
                timeout_ms=args.timeout_ms, false_endpoint_ms=args.false_endpoint_ms,
                vad=args.vad, policy=args.policy)
            print(f'{result["status"].upper()} {result["output"]}')
            print(f'  Segments: {result["segment_count"]}, Turns: {result["turn_count"]}, '
                  f'Events: {result["event_count"]}, Metrics: {result["metric_count"]}')
            print(f'  Judge: {result["judge_result_count"]}, Findings: {result["finding_count"]}')
            print(f'  Report: {result["report_md"]}')
            return 0 if result['status'] == 'complete' else 2
        except (OSError, ValueError) as error:
            print(f'PIPELINE ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'revise':
        from .revision import RevisionStore
        try:
            store = RevisionStore(args.output)
            rev = store.add_revision(
                args.target_type, args.target_id, args.field,
                None,  # original_value will be looked up by the consumer
                args.revised_value, args.reviewer, args.reason)
            print(f'REVISED {rev["revision_id"]}: {args.target_type}/{args.target_id}/{args.field} = {args.revised_value}')
            return 0
        except (OSError, ValueError) as error:
            print(f'REVISE ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'report':
        from .report import render_report_from_files
        try:
            md_path, json_path = render_report_from_files(
                args.output, args.profile, args.fused, args.turns,
                args.timeline, args.metrics, args.judge, args.findings)
            print(f'REPORT {md_path}')
            return 0
        except (OSError, ValueError) as error:
            print(f'REPORT ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'audio':
        import json
        from .station import capture_fixed, calibrate_loopback, list_devices
        try:
            if args.audio_command == 'devices':
                print(json.dumps(list_devices(), ensure_ascii=False, indent=2))
                return 0
            if args.audio_command == 'capture':
                directory, metadata = capture_fixed(args.stimulus, args.output, args.input_device, args.output_device,
                    args.input_channels, args.output_channels, args.pre_roll_ms, args.tail_ms)
                print(f'{metadata["status"].upper()} {directory}')
                return 0 if metadata['status'] == 'captured' else 2
            directory, result = calibrate_loopback(args.output, args.input_device, args.output_device, args.repetitions)
            print(f'{result["status"].upper()} {directory} (no correction applied)')
            return 0 if result['status'] == 'observed' else 2
        except (OSError, ValueError, EOFError, wave.Error) as error:
            print(f'AUDIO ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
    if args.command == 'run':
        from .runner import run_input
        import yaml
        try:
            results = run_input(args.path, args.output, args.asset_root, args.dry_run,
                                load_document(args.device_profile) if args.device_profile else None)
        except (OSError, ValueError, yaml.YAMLError) as error:
            print(f'RUN ERROR: {error}', file=sys.stderr)
            return 1
        for directory, manifest in results:
            print(f'{manifest["status"].upper()} {directory} (dry_run; no hardware measurement)')
            for blocker in manifest['blockers']:
                print(f'  {blocker}')
        return 2 if any(manifest['status'] == 'blocked' for _, manifest in results) else 0
    if args.kind in ('metric', 'finding') and args.timeline is None:
        parser.error('--kind metric/finding requires --timeline')
    failures = 0
    for path in args.paths:
        try:
            if args.kind == 'transcript':
                errors = transcript_errors(load_document(path))
            elif args.kind == 'acoustic-segments':
                errors = acoustic_errors(load_document(path))
            elif args.kind == 'fused-segments':
                errors = fused_errors(load_document(path))
            elif args.kind == 'turns':
                errors = turns_errors(load_document(path))
            elif args.kind == 'judge-result':
                errors = judge_result_errors(load_document(path))
            elif args.kind == 'judge-results':
                errors = judge_document_errors(
                    load_document(path),
                    load_document(args.timeline) if args.timeline else None,
                    load_document(args.turns) if args.turns else None)
            elif args.kind == 'finding':
                errors = finding_errors(load_document(path), load_document(args.timeline),
                                        [load_document(item) for item in args.metrics],
                                        load_document(args.regression_case) if args.regression_case else None)
            elif args.kind == 'metric':
                errors = metric_errors(load_document(path), load_document(args.timeline))
            else:
                checker = case_errors if args.kind == 'test-case' else timeline_errors
                errors = checker(load_document(path))
        except (OSError, ValueError) as error:
            errors = [str(error)]
        except Exception as error:
            # YAML parse errors use a separate hierarchy; preserve the file/error in CLI output.
            import yaml
            if not isinstance(error, yaml.YAMLError):
                raise
            errors = [str(error)]
        if errors:
            failures += 1
            print(f'INVALID {path}', file=sys.stderr)
            print('\n'.join(errors), file=sys.stderr)
        else:
            print(f'VALID {path} (contract only; assets/hardware not verified)')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
