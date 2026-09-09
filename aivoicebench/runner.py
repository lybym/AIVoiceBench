"""Local preparation runner. It never synthesizes observations for absent hardware."""

from array import array
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import uuid
import wave

from .validation import ROOT, case_errors, load_document, metric_errors, schema_errors, timeline_errors

MAX_PREPARE_MS = 600000  # Bounded in-memory MVP; long-run streaming is a later adapter.


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def contained_file(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'Path escapes configured root: {relative}')
    if not path.is_file():
        raise ValueError(f'Missing file: {relative}')
    return path


def inspect_audio(path, max_duration_ms):
    """Read only bounded PCM data; report QA without inventing approved levels."""
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError('Expected WAV / PCM_S16LE / 16000 Hz / mono')
        count = audio.getnframes()
        if count == 0 or count > 16 * max_duration_ms:
            raise ValueError('Audio duration is empty or exceeds case timeout')
        raw = audio.readframes(count)
        if len(raw) != count * 2:
            raise ValueError('Truncated WAV sample data')
    samples = array('h', raw)
    if sys.byteorder != 'little':
        samples.byteswap()
    return {'sample_count': count, 'duration_ms': count / 16,
            'peak': max(abs(value) for value in samples) / 32768,
            'rms': math.sqrt(sum(value * value for value in samples) / count) / 32768,
            'dc_offset': sum(samples) / count / 32768,
            'clipped_samples': sum(value in (-32768, 32767) for value in samples)}, raw


def prepare_audio(case, asset_root, run_dir):
    assets = {}
    qa = []
    blockers = []
    if case['case_timeout_ms'] > MAX_PREPARE_MS:
        return [], [], ['Preparation MVP supports case_timeout_ms up to 600000; use a future streaming adapter for endurance cases']
    loaded_bytes = 0
    for manifest in case['stimulus'].get('assets', []):
        try:
            path = contained_file(asset_root, manifest['path'])
            observed_hash = digest(path)
            if observed_hash != manifest['sha256']:
                raise ValueError('SHA-256 differs from frozen manifest')
            checks, raw = inspect_audio(path, case['case_timeout_ms'])
            loaded_bytes += len(raw)
            if loaded_bytes > 32 * MAX_PREPARE_MS:
                raise ValueError('Combined source audio exceeds bounded preparation memory budget')
            assets[manifest['asset_id']] = raw
            qa.append({'asset_id': manifest['asset_id'], 'sha256': observed_hash, 'status': 'verified', **checks})
        except (OSError, ValueError, wave.Error, EOFError) as error:
            blockers.append(f'{manifest["asset_id"]}: {error}')
            qa.append({'asset_id': manifest['asset_id'], 'status': 'blocked', 'reason': str(error)})
    plan = []
    if case['mode'] == 'fixed_audio' and not blockers:
        total = sum(segment.get('sample_count', len(assets.get(segment.get('asset_id'), b'')) // 2)
                    for segment in case['stimulus']['segments'])
        if total > 16 * case['case_timeout_ms']:
            blockers.append('Composed stimulus exceeds case_timeout_ms')
        else:
            cursor = 0
            with wave.open(str(run_dir / 'stimulus.wav'), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                for segment in case['stimulus']['segments']:
                    data = b'\0\0' * segment['sample_count'] if segment['type'] == 'silence' else assets[segment['asset_id']]
                    count = len(data) // 2
                    plan.append({'type': segment['type'], 'asset_id': segment.get('asset_id'),
                                 'start_sample': cursor, 'end_sample': cursor + count})
                    audio.writeframes(data)
                    cursor += count
    elif case['mode'] != 'fixed_audio':
        blockers.append(f'{case["mode"]}: execution adapter is not implemented; scaffold only')
    return qa, plan, blockers


def _missing_metric(case, timeline, name):
    boolean_names = {'false_endpoint', 'barge_in_success', 'barge_in_stop_success',
                     'barge_in_new_response_success', 'context_success', 'instruction_success'}
    unit = 'boolean' if name in boolean_names else ('ms' if name.endswith('_ms') else ('dB' if name == 'aec_erle_db' else 'ratio'))
    scope = 'white_box' if name == 'asr_cer' or name.startswith('internal_') or name == 'aec_erle_db' else 'black_box'
    method = 'ground_truth_comparison' if name == 'asr_cer' else ('composite' if name == 'barge_in_success' else 'deterministic')
    is_rate = name in {'false_endpoint_rate', 'barge_in_success_rate', 'context_success_rate', 'instruction_success_rate'}
    return {'schema_version': '2.0.0', 'metric_id': f'{timeline["run_id"]}.{name}', 'run_id': timeline['run_id'],
            'case_id': case['case_id'], 'name': name, 'definition_version': '1.0.0', 'execution_kind': 'dry_run',
            'value': None, 'unit': unit, 'status': 'insufficient_evidence',
            'reason': 'Preparation only: no captured device audio, ASR, semantic Judge or measured event boundaries',
            'measurement_scope': scope, 'method': method, 'confidence': 0, 'evidence_ids': [], 'event_ids': [],
            'aggregation': {'kind': 'rate' if is_rate else 'single', 'sample_count': 0, 'total_count': 1,
                            'excluded_count': 1, 'algorithm': 'eligible_ratio' if is_rate else 'single', 'input_metric_ids': []}}


def run_case(case_path, output_root, asset_root=None, dry_run=False, attempt=1, device_profile=None):
    case_path = Path(case_path)
    case = load_document(case_path)
    errors = case_errors(case)
    if errors:
        raise ValueError('\n'.join(errors))
    known_metrics = json.loads((ROOT / 'schemas/metric.schema.json').read_text(encoding='utf-8-sig'))['properties']['name']['enum']
    unknown = set(case['metrics']) - set(known_metrics)
    if unknown:
        raise ValueError('Unsupported metric IDs: ' + ', '.join(sorted(unknown)))
    profile = device_profile or {key: None for key in ('device', 'hardware', 'firmware', 'model', 'prompt', 'environment')}
    if not isinstance(profile, dict) or set(profile) != {'device', 'hardware', 'firmware', 'model', 'prompt', 'environment'} or any(value is not None and (not isinstance(value, str) or not value.strip()) for value in profile.values()):
        raise ValueError('Device profile accepts only device/hardware/firmware/model/prompt/environment version labels or null')
    run_id = 'RUN-' + uuid.uuid4().hex
    run_dir = Path(output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / 'case.json', case)
    qa, plan, blockers = prepare_audio(case, Path(asset_root) if asset_root else case_path.parent, run_dir)
    if not dry_run:
        blockers.append('Hardware station adapter is not configured/implemented (#6); no playback or recording performed')
    timeline = {'schema_version': '2.0.0', 'run_id': run_id, 'case_id': case['case_id'], 'case_version': case['case_version'],
                'attempt': attempt, 'execution_kind': 'dry_run', 'time_base': {'kind': 'run_monotonic_ms', 'origin': 'Reserved origin; capture has not started'},
                'run_snapshot': {**profile, 'test_assets': {'golden_set_id': case.get('golden_set', {}).get('set_id'),
                    'golden_set_version': case.get('golden_set', {}).get('version'), 'case_sha256': digest(run_dir / 'case.json'),
                    'asset_sha256': {asset['asset_id']: asset['sha256'] for asset in case['stimulus'].get('assets', [])}}},
                'status': 'blocked', 'gaps': [{'reason': 'No hardware capture in preparation runner',
                'required_evidence': 'Synchronized stimulus/device recordings and observed event boundaries', 'start_ms': 0, 'end_ms': 0}],
                'tracks': [], 'artifacts': [], 'evidence': [], 'events': []}
    metrics = [_missing_metric(case, timeline, name) for name in case['metrics']]
    issues = timeline_errors(timeline)
    for metric in metrics:
        issues.extend(metric_errors(metric, timeline))
    if issues:
        raise ValueError('Runner generated invalid contracts: ' + '\n'.join(issues))
    write_json(run_dir / 'timeline.json', timeline)
    write_json(run_dir / 'metrics.json', metrics)
    write_json(run_dir / 'findings.json', [])
    write_json(run_dir / 'audio-qa.json', qa)
    write_json(run_dir / 'stimulus-plan.json', {'time_basis': 'planned_asset_samples_not_observed_events', 'sample_rate_hz': 16000, 'segments': plan})
    write_json(run_dir / 'BUILD_INFO.json', {'runner_version': '0.1.0', 'python': sys.version.split()[0],
               'case_sha256': digest(run_dir / 'case.json'), 'asset_qa': qa, 'playback_performed': False, 'recording_performed': False})
    manifest = {'schema_version': '1.0.0', 'run_id': run_id, 'case_id': case['case_id'], 'case_version': case['case_version'],
                'attempt': attempt, 'created_at': datetime.now(timezone.utc).isoformat(),
                'requested_execution': 'dry_run' if dry_run else 'hardware', 'execution_kind': 'dry_run',
                'status': 'blocked' if blockers else 'prepared', 'blockers': blockers, 'device_profile': profile,
                'artifacts': [{'path': path.name, 'sha256': digest(path)} for path in sorted(run_dir.iterdir()) if path.is_file()]}
    errors = schema_errors(manifest, 'run-manifest')
    if errors:
        raise ValueError('\n'.join(errors))
    write_json(run_dir / 'manifest.json', manifest)
    return run_dir, manifest


def run_input(path, output_root, asset_root=None, dry_run=False, device_profile=None):
    """Validate the whole suite before side effects; each attempt gets its own directory."""
    path = Path(path)
    document = load_document(path)
    if isinstance(document, dict) and 'suite_id' in document:
        errors = schema_errors(document, 'test-suite')
        if errors:
            raise ValueError('\n'.join(errors))
        case_paths = [contained_file(path.parent, relative) for relative in document['cases']]
    else:
        case_paths = [path]
    cases = [(case_path, load_document(case_path)) for case_path in case_paths]
    for case_path, case in cases:
        errors = case_errors(case)
        if errors:
            raise ValueError(f'{case_path}: ' + '\n'.join(errors))
    results = []
    for case_path, case in cases:
        for attempt in range(1, case.get('repeat', 1) + 1):
            results.append(run_case(case_path, output_root, asset_root, dry_run, attempt, device_profile))
    return results
