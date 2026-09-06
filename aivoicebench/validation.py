"""Offline contract checks. Schema validity does not certify assets or hardware."""

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml
from jsonschema import Draft202012Validator

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
    schema = json.loads((ROOT / 'schemas' / f'{schema_name}.schema.json').read_text(encoding='utf-8-sig'))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda e: str(list(e.absolute_path)))
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
