"""Offline contract checks. Schema validity does not certify assets or hardware."""

import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate keys in YAML and its JSON subset."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError('Object keys must be strings')
        if key in result:
            raise ValueError(f'Duplicate key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_document(path):
    with Path(path).open(encoding='utf-8-sig') as stream:
        return yaml.load(stream, Loader=UniqueKeyLoader)


def schema_errors(document, schema_name='test-case'):
    try:
        json.dumps(document, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return ['/: contract must be finite, acyclic JSON data']
    schema = json.loads((ROOT / 'schemas' / f'{schema_name}.schema.json').read_text(encoding='utf-8-sig'))
    Draft202012Validator.check_schema(schema)
    resources = []
    for path in (ROOT / 'schemas').glob('*.schema.json'):
        contract = json.loads(path.read_text(encoding='utf-8-sig'))
        resources.append((contract['$id'], Resource.from_contents(contract)))
    registry = Registry().with_resources(resources)
    errors = sorted(Draft202012Validator(schema, registry=registry).iter_errors(document), key=lambda e: str(list(e.absolute_path)))
    messages = []
    for error in errors:
        path = '/' + '/'.join(str(p) for p in error.absolute_path)
        messages.append(f'{path}: {error.message}')
        # oneOf context makes malformed segment/trigger failures actionable.
        for child in error.context:
            child_path = '/' + '/'.join(str(p) for p in child.absolute_path)
            messages.append(f'  {child_path}: {child.message}')
    return messages


def case_errors(case):
    errors = schema_errors(case)
    if errors:
        return errors
    stimulus = case['stimulus']
    assets = stimulus.get('assets', [])
    ids = [asset['asset_id'] for asset in assets]
    if len(set(ids)) != len(ids):
        errors.append('/stimulus/assets: asset_id must be unique')
    for index, asset in enumerate(assets):
        path = asset['path']
        # Both Windows and POSIX containment, even when validated on Linux CI.
        if (PureWindowsPath(path).drive or PureWindowsPath(path).root or
                PurePosixPath(path).is_absolute() or '..' in path.replace('\\', '/').split('/') or ':' in path):
            errors.append(f'/stimulus/assets/{index}/path: use a relative path within the asset root')
    groups = []
    if case['mode'] == 'fixed_audio':
        groups.append(('segments', stimulus['segments']))
    elif case['mode'] == 'interactive':
        groups.extend((name, stimulus[name]) for name in ('prompt', 'interruption'))
        trigger = stimulus['trigger']
        if trigger['offset_ms'] >= trigger['timeout_ms']:
            errors.append('/stimulus/trigger/offset_ms: must be less than timeout_ms')
        if trigger['timeout_ms'] > case['case_timeout_ms']:
            errors.append('/stimulus/trigger/timeout_ms: exceeds case_timeout_ms')
    elif case['mode'] == 'multi_turn':
        turns = stimulus['turns']
        turn_ids = [turn['turn_id'] for turn in turns]
        if len(set(turn_ids)) != len(turn_ids):
            errors.append('/stimulus/turns: turn_id must be unique')
        for index, turn in enumerate(turns):
            groups.append((f'turns/{index}/segments', turn['segments']))
            if turn['wait_for']['timeout_ms'] > case['case_timeout_ms']:
                errors.append(f'/stimulus/turns/{index}/wait_for/timeout_ms: exceeds case_timeout_ms')
    elif stimulus['agent']['max_duration_ms'] > case['case_timeout_ms']:
        errors.append('/stimulus/agent/max_duration_ms: exceeds case_timeout_ms')
    for name, segments in groups:
        if not any(segment['type'] != 'silence' for segment in segments):
            errors.append(f'/stimulus/{name}: at least one audio segment is required')
        for index, segment in enumerate(segments):
            if segment.get('asset_id') is not None and segment['asset_id'] not in ids:
                errors.append(f'/stimulus/{name}/{index}/asset_id: undeclared asset {segment["asset_id"]}')
    assertions = case['expected']['assertions']
    assertion_ids = [item['assertion_id'] for item in assertions]
    if len(set(assertion_ids)) != len(assertion_ids):
        errors.append('/expected/assertions: assertion_id must be unique')
    for index, assertion in enumerate(assertions):
        if assertion['metric_id'] not in case['metrics']:
            errors.append(f'/expected/assertions/{index}/metric_id: must appear in metrics')
        if assertion.get('operator') in ('lt', 'lte', 'gt', 'gte') and type(assertion['value']) not in (int, float):
            errors.append(f'/expected/assertions/{index}/value: ordered comparison requires a number')
    return errors


def timeline_errors(timeline):
    """Validate references and ordering without manufacturing missing observations."""
    errors = schema_errors(timeline, 'event-timeline')
    if errors:
        return errors
    indexes = {}
    for field, key in [('tracks', 'track_id'), ('artifacts', 'artifact_id'), ('evidence', 'evidence_id'), ('events', 'event_id')]:
        items = timeline[field]
        indexes[field] = {item[key]: item for item in items}
        if len(indexes[field]) != len(items):
            errors.append(f'/{field}: {key} must be unique')
    tracks, artifacts, evidence = (indexes[key] for key in ('tracks', 'artifacts', 'evidence'))
    for artifact in artifacts.values():
        track_id = artifact['track_id']
        if (artifact['kind'] == 'audio' or track_id is not None) and track_id not in tracks:
            errors.append(f'/artifacts/{artifact["artifact_id"]}: unknown or missing track_id')
        if timeline['execution_kind'] == 'hardware' and artifact['origin'] == 'synthetic':
            errors.append('/artifacts: synthetic artifact cannot be hardware evidence')
    if timeline['execution_kind'] == 'hardware':
        for track in tracks.values():
            if track['sync']['status'] == 'synthetic':
                errors.append('/tracks: synthetic calibration cannot label a hardware run')
    for item in evidence.values():
        location = f'/evidence/{item["evidence_id"]}'
        if item['end_ms'] < item['start_ms']:
            errors.append(f'{location}: end_ms precedes start_ms')
        artifact = artifacts.get(item['artifact_id'])
        if artifact is None:
            errors.append(f'{location}: unknown artifact_id')
            continue
        if item['track_id'] != artifact['track_id']:
            errors.append(f'{location}: track_id disagrees with artifact')
        if item['track_id'] in tracks:
            sync = tracks[item['track_id']]['sync']
            if sync['offset_ms'] is not None:
                lower = sync['offset_ms'] - sync['uncertainty_ms']
                upper = sync['offset_ms'] + artifact['duration_ms'] + sync['uncertainty_ms']
                if item['start_ms'] < lower or item['end_ms'] > upper:
                    errors.append(f'{location}: evidence range outside artifact')
        if item['source'] == 'device_log' and artifact['kind'] != 'device_log':
            errors.append(f'{location}: device_log source requires device_log artifact')
    previous_time = -math.inf
    active = {}
    pairs = {'tester_speech_end': 'tester_speech_start', 'device_speech_end': 'device_speech_start',
             'interrupt_end': 'interrupt_start', 'planned_pause_end': 'planned_pause_start', 'overlap_end': 'overlap_start'}
    for event in timeline['events']:
        location = f'/events/{event["event_id"]}'
        if event['run_id'] != timeline['run_id'] or event['case_id'] != timeline['case_id']:
            errors.append(f'{location}: run/case identity mismatch')
        if event['start_ms'] < previous_time:
            errors.append(f'{location}: events must be in nondecreasing start_ms order')
        previous_time = event['start_ms']
        if event['end_ms'] < event['start_ms']:
            errors.append(f'{location}: end_ms precedes start_ms')
        if event['type'] not in ('asr_segment', 'custom') and event['end_ms'] != event['start_ms']:
            errors.append(f'{location}: boundary event must be a point')
        refs = [evidence.get(key) for key in event['evidence_ids']]
        if any(item is None for item in refs):
            errors.append(f'{location}: unknown evidence_id')
        else:
            if not any(item['start_ms'] <= event['start_ms'] and item['end_ms'] >= event['end_ms'] for item in refs):
                errors.append(f'{location}: evidence must cover the event interval')
            if event['source'] == 'device_log' and not any(item['source'] == 'device_log' for item in refs):
                errors.append(f'{location}: white-box event needs device log evidence')
        event_type = event['type']
        pair_type = pairs.get(event_type, event_type)
        key = (pair_type, event['turn_id'], event['response_id'])
        if event_type in pairs.values():
            if key in active:
                errors.append(f'{location}: duplicate active start for same turn/response')
            active[key] = event
        elif event_type in pairs:
            if key not in active:
                if timeline['status'] == 'complete':
                    errors.append(f'{location}: end has no associated start')
            else:
                del active[key]
    if active and timeline['status'] == 'complete':
        errors.append('/events: complete timeline has unclosed intervals; use partial plus gaps')
    for gap in timeline['gaps']:
        if gap['end_ms'] < gap['start_ms']:
            errors.append('/gaps: end_ms precedes start_ms')
    return errors
