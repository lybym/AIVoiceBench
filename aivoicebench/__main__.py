"""Contract CLI; hardware execution is introduced in later issues."""

import argparse
from pathlib import Path
import sys
import wave

from .validation import case_errors, load_document, timeline_errors, metric_errors, finding_errors, transcript_errors, acoustic_errors, fused_errors, turns_errors


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
    acoustic = subparsers.add_parser('acoustic', help='Detect speech segments from a canonical WAV using energy VAD')
    acoustic.add_argument('source', type=Path)
    acoustic.add_argument('--output', type=Path, default=Path('artifacts/acoustic'))
    acoustic.add_argument('--frame-ms', type=float, default=30.0)
    acoustic.add_argument('--hop-ms', type=float, default=10.0)
    acoustic.add_argument('--threshold-factor', type=float, default=0.15)
    acoustic.add_argument('--min-speech-ms', type=float, default=100.0)
    acoustic.add_argument('--min-silence-ms', type=float, default=200.0)
    acoustic.add_argument('--merge-gap-ms', type=float, default=80.0)
    acoustic.add_argument('--pre-roll-ms', type=float, default=0.0)
    acoustic.add_argument('--post-roll-ms', type=float, default=0.0)
    fusion = subparsers.add_parser('fusion', help='Fuse acoustic segments, build turns, detect events, generate timeline')
    fusion.add_argument('acoustic', type=Path, help='Path to acoustic-segments JSON')
    fusion.add_argument('--transcript', type=Path, help='Optional ASR transcript JSON')
    fusion.add_argument('--output', type=Path, default=Path('artifacts/fusion'))
    fusion.add_argument('--timeout-ms', type=float, default=5000.0)
    fusion.add_argument('--false-endpoint-ms', type=float, default=300.0)
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
    validate = subparsers.add_parser('validate', help='Validate TestCase JSON/YAML without hardware or network')
    validate.add_argument('paths', nargs='+', type=Path)
    validate.add_argument('--kind', choices=['test-case', 'timeline', 'metric', 'finding', 'transcript', 'acoustic-segments', 'fused-segments', 'turns'], default='test-case')
    validate.add_argument('--timeline', type=Path, help='Required context for metric references')
    validate.add_argument('--metrics', nargs='*', type=Path, default=[], help='MetricResult files referenced by a finding')
    validate.add_argument('--regression-case', type=Path, help='Linked TestCase for a frozen regression candidate')
    args = parser.parse_args(argv)
    if args.command == 'import':
        import yaml
        from .audio_processing import FFmpegAudioProcessor
        from .import_pipeline import import_recording
        factory = None
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
                asr_provider_factory=factory, synthetic=args.synthetic)
            print(f'{manifest["status"].upper()} {directory} ({manifest["execution_kind"]}; retained import Run)')
            for name, stage in manifest['stages'].items():
                print(f'  {name}: {stage["status"]}')
            print(f'Report: {directory / "report.md"}')
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
        from .acoustic import EnergyVadSegmenter, segment_audio
        try:
            segmenter = EnergyVadSegmenter(
                frame_ms=args.frame_ms, hop_ms=args.hop_ms,
                threshold_factor=args.threshold_factor,
                min_speech_ms=args.min_speech_ms,
                min_silence_ms=args.min_silence_ms,
                merge_gap_ms=args.merge_gap_ms,
                pre_roll_ms=args.pre_roll_ms,
                post_roll_ms=args.post_roll_ms)
            document, out_path = segment_audio(args.source, args.output / ('ACOUSTIC-' + __import__('uuid').uuid4().hex + '.json'), segmenter)
            print(f'{document["status"].upper()} {out_path} ({len(document["segments"])} acoustic segment(s); signal timing, no speaker role)')
            return 0 if document['status'] == 'complete' else 2
        except (OSError, ValueError, wave.Error) as error:
            print(f'ACOUSTIC ERROR: {str(error) or type(error).__name__}', file=sys.stderr)
            return 1
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
