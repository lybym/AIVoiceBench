"""Offline contract checks. Schema validity does not certify assets or hardware."""

import json
import math
import re
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
FORMAT_CHECKER = FormatChecker()


@FORMAT_CHECKER.checks('date-time')
def _timestamp(value):
    # jsonschema's optional RFC3339 dependency is not required by this project.
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
        return False
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


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
    errors = sorted(Draft202012Validator(schema, registry=registry, format_checker=FORMAT_CHECKER).iter_errors(document), key=lambda e: str(list(e.absolute_path)))
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
             'interrupt_end': 'interrupt_start', 'planned_pause_end': 'planned_pause_start',
             'overlap_end': 'overlap_start', 'response_end': 'response_start'}
    for event in timeline['events']:
        location = f'/events/{event["event_id"]}'
        if event['run_id'] != timeline['run_id'] or event['case_id'] != timeline['case_id']:
            errors.append(f'{location}: run/case identity mismatch')
        if event['start_ms'] < previous_time:
            errors.append(f'{location}: events must be in nondecreasing start_ms order')
        previous_time = event['start_ms']
        if event['end_ms'] < event['start_ms']:
            errors.append(f'{location}: end_ms precedes start_ms')
        if event['type'] not in ('asr_segment', 'custom', 'silence') and event['end_ms'] != event['start_ms']:
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


def metric_errors(metric, timeline=None):
    errors = schema_errors(metric, 'metric')
    if errors:
        return errors
    aggregation = metric['aggregation']
    n = aggregation['sample_count']
    if n + aggregation['excluded_count'] != aggregation['total_count']:
        errors.append('/aggregation: sample_count + excluded_count must equal total_count')
    algorithms = {'single': 'single', 'percentile': 'R7', 'rate': 'eligible_ratio', 'micro': 'micro_cer'}
    if aggregation['algorithm'] != algorithms[aggregation['kind']]:
        errors.append('/aggregation/algorithm: incompatible with kind')
    if aggregation['kind'] == 'single' and (n > 1 or aggregation['total_count'] != 1):
        errors.append('/aggregation: single requires total_count=1 and at most one sample')
    if (aggregation['kind'] == 'percentile') != ('percentile' in aggregation):
        errors.append('/aggregation/percentile: required only for percentile kind')
    if aggregation['kind'] == 'percentile' and (metric['unit'] != 'ms' or len(aggregation['input_metric_ids']) != n):
        errors.append('/aggregation: latency percentile must link every eligible input metric')
    if aggregation['kind'] == 'micro' and metric['name'] != 'asr_cer':
        errors.append('/aggregation: micro is only defined for asr_cer')
    if aggregation['kind'] in ('rate', 'micro') and len(aggregation['input_metric_ids']) != n:
        errors.append('/aggregation: aggregate must link every eligible input metric')
    threshold = metric.get('threshold')
    if threshold and metric['status'] in ('pass', 'fail'):
        left, right, op = metric['value'], threshold['value'], threshold['operator']
        if type(left) is bool and (type(right) is not bool or op != 'eq'):
            errors.append('/threshold: boolean metric requires boolean eq threshold')
        elif type(left) is not bool and type(right) is bool:
            errors.append('/threshold: numeric metric requires numeric threshold')
        else:
            comparison = {'eq': lambda: left == right, 'lt': lambda: left < right,
                          'lte': lambda: left <= right, 'gt': lambda: left > right, 'gte': lambda: left >= right}[op]()
            if (metric['status'] == 'pass') != comparison:
                errors.append('/status: disagrees with threshold comparison')
    if timeline is None:
        return errors
    timeline_issues = timeline_errors(timeline)
    if timeline_issues:
        return errors + ['Referenced timeline is invalid: ' + message for message in timeline_issues]
    if any(metric[key] != timeline[key] for key in ('run_id', 'case_id', 'execution_kind')):
        errors.append('/: metric and timeline run/case/execution_kind disagree')
    evidence = {item['evidence_id']: item for item in timeline['evidence']}
    events = {item['event_id']: item for item in timeline['events']}
    if any(key not in evidence for key in metric['evidence_ids']):
        errors.append('/evidence_ids: unknown timeline evidence')
    if any(key not in events for key in metric['event_ids']):
        errors.append('/event_ids: unknown timeline event')
    decided = metric['status'] not in ('insufficient_evidence', 'not_applicable')
    refs = [evidence[key] for key in metric['evidence_ids'] if key in evidence]
    if decided and metric['measurement_scope'] in ('white_box', 'hybrid') and not any(item['source'] == 'device_log' for item in refs):
        errors.append('/measurement_scope: internal measurement requires device log evidence')
    if decided and metric['unit'] == 'ms':
        tracks = {track['track_id']: track for track in timeline['tracks']}
        clocks = {tracks[item['track_id']]['clock_id'] for item in refs if item['track_id'] in tracks}
        if len(clocks) > 1 and any(tracks[item['track_id']]['sync']['status'] == 'uncalibrated' for item in refs if item['track_id'] in tracks):
            errors.append('/evidence_ids: uncalibrated cross-clock timing is insufficient evidence')
    for key in metric['event_ids']:
        if key in events and not set(events[key]['evidence_ids']).intersection(metric['evidence_ids']):
            errors.append('/event_ids: event evidence must be included in metric evidence_ids')
    return errors


def finding_errors(finding, timeline, metrics=(), regression_case=None):
    errors = schema_errors(finding, 'finding')
    if errors:
        return errors
    timeline_issues = timeline_errors(timeline)
    if timeline_issues:
        return ['Referenced timeline is invalid: ' + message for message in timeline_issues]
    if any(finding[key] != timeline[key] for key in ('run_id', 'case_id', 'execution_kind')):
        errors.append('/: finding and timeline run/case/execution_kind disagree')
    evidence = {item['evidence_id']: item for item in timeline['evidence']}
    events = {item['event_id']: item for item in timeline['events']}
    if any(key not in evidence for key in finding['evidence_ids']):
        errors.append('/evidence_ids: unknown timeline evidence')
    for key in finding['event_ids']:
        if key not in events:
            errors.append('/event_ids: unknown timeline event')
        elif not set(events[key]['evidence_ids']).intersection(finding['evidence_ids']):
            errors.append('/event_ids: event evidence must be included in finding evidence_ids')
    refs = [evidence[key] for key in finding['evidence_ids'] if key in evidence]
    if finding['kind'] == 'defect' and finding['status'] in ('confirmed', 'fixed'):
        if not any(item['end_ms'] > item['start_ms'] for item in refs):
            errors.append('/evidence_ids: confirmed defect requires a nonempty evidence snippet')
    if finding['attribution_status'] == 'verified':
        if not any(item['source'] == 'device_log' for item in refs):
            errors.append('/attribution_status: verified cause requires device log evidence')
        if finding['human_review']['status'] != 'approved':
            errors.append('/attribution_status: verified cause requires human review')
    metric_map = {metric['metric_id']: metric for metric in metrics if isinstance(metric, dict) and 'metric_id' in metric}
    if len(metric_map) != len(metrics):
        errors.append('/metric_ids: supplied metrics require unique metric_id')
    for key in finding['metric_ids']:
        if key not in metric_map:
            errors.append('/metric_ids: unknown supplied metric')
        else:
            errors.extend('Referenced metric is invalid: ' + message for message in metric_errors(metric_map[key], timeline))
    if finding.get('regression', {}).get('state') == 'frozen':
        if regression_case is None:
            errors.append('/regression: frozen candidate requires the linked TestCase')
        else:
            errors.extend('Regression case is invalid: ' + message for message in case_errors(regression_case))
            regression = finding['regression']
            if any(regression.get(key) != regression_case.get(key) for key in ('case_id', 'case_version')):
                errors.append('/regression: Case identity/version mismatch')
            golden = regression_case.get('golden_set', {})
            if golden.get('set_id') != regression['golden_set_id'] or golden.get('version') != regression['golden_set_version']:
                errors.append('/regression: Golden Set identity/version mismatch')
            if regression_case.get('mode') == 'exploratory_agent':
                errors.append('/regression: frozen case must use deterministic assets')
    return errors


def transcript_errors(transcript):
    errors = schema_errors(transcript, 'transcript')
    if errors:
        return errors
    ids = [segment['segment_id'] for segment in transcript['segments']]
    if len(ids) != len(set(ids)):
        errors.append('/segments: segment_id must be unique')
    previous_end = 0
    duration = transcript['source']['duration_ms']
    for index, segment in enumerate(transcript['segments']):
        if segment['start_ms'] < previous_end - 0.001 or segment['end_ms'] < segment['start_ms'] or segment['end_ms'] > duration + 0.001:
            errors.append(f'/segments/{index}: timing out of source bounds or order')
        word_end = segment['start_ms']
        for word in segment['words']:
            if word['start_ms'] < word_end - 0.001 or word['end_ms'] < word['start_ms'] or word['end_ms'] > segment['end_ms'] + 0.001:
                errors.append(f'/segments/{index}/words: timing out of segment bounds or order')
            word_end = word['end_ms']
        previous_end = segment['start_ms'] if transcript['schema_version'] == '1.1.0' else segment['end_ms']
    return errors


def acoustic_errors(document):
    """Validate acoustic segment candidates without manufacturing events."""
    errors = schema_errors(document, 'acoustic-segments')
    if errors:
        return errors
    ids = [seg['segment_id'] for seg in document['segments']]
    if len(ids) != len(set(ids)):
        errors.append('/segments: segment_id must be unique')
    duration = document['source']['duration_ms']
    previous_end = 0.0
    for index, segment in enumerate(document['segments']):
        location = f'/segments/{index}'
        if segment['start_ms'] < 0:
            errors.append(f'{location}: start_ms must be non-negative')
        if segment['end_ms'] < segment['start_ms']:
            errors.append(f'{location}: end_ms precedes start_ms')
        if segment['end_ms'] > duration + 0.001:
            errors.append(f'{location}: end_ms exceeds source duration')
        if segment['start_ms'] < previous_end - 0.001:
            errors.append(f'{location}: segments must be in nondecreasing start_ms order')
        if segment['start_ms'] < previous_end - 0.001 and segment['start_ms'] < previous_end:
            errors.append(f'{location}: segments must not overlap')
        previous_end = max(previous_end, segment['end_ms'])
        if segment['source'] != 'acoustic':
            errors.append(f'{location}: acoustic segments must have source=acoustic')
        if segment['method'] != document['processor']['method']:
            errors.append(f'{location}: method must match processor method')
    if document['status'] == 'insufficient_evidence' and document['segments']:
        errors.append('/segments: insufficient_evidence status requires no segments')
    return errors


def fused_errors(document):
    """Validate fused segments with speaker attribution."""
    errors = schema_errors(document, 'fused-segments')
    if errors:
        return errors
    ids = [seg['segment_id'] for seg in document['segments']]
    if len(ids) != len(set(ids)):
        errors.append('/segments: segment_id must be unique')
    duration = document['source']['duration_ms']
    previous_end = 0.0
    for index, segment in enumerate(document['segments']):
        location = f'/segments/{index}'
        if segment['start_ms'] < 0:
            errors.append(f'{location}: start_ms must be non-negative')
        if segment['end_ms'] < segment['start_ms']:
            errors.append(f'{location}: end_ms precedes start_ms')
        if segment['end_ms'] > duration + 0.001:
            errors.append(f'{location}: end_ms exceeds source duration')
        if segment['start_ms'] < previous_end - 0.001:
            errors.append(f'{location}: segments must be in nondecreasing start_ms order')
        previous_end = max(previous_end, segment['end_ms'])
    if document['status'] == 'insufficient_evidence' and document['segments']:
        errors.append('/segments: insufficient_evidence status requires no segments')
    return errors


def turns_errors(document):
    """Validate conversational turns built from fused segments."""
    errors = schema_errors(document, 'turns')
    if errors:
        return errors
    ids = [t['turn_id'] for t in document['turns']]
    if len(ids) != len(set(ids)):
        errors.append('/turns: turn_id must be unique')
    for index, turn in enumerate(document['turns']):
        location = f'/turns/{index}'
        if turn['end_ms'] < turn['start_ms']:
            errors.append(f'{location}: end_ms precedes start_ms')
        if not turn['tester_segment_ids'] and not turn['device_segment_ids']:
            errors.append(f'{location}: turn must have at least one segment')
    if document['status'] == 'insufficient_evidence' and document['turns']:
        errors.append('/turns: insufficient_evidence status requires no turns')
    return errors


def judge_result_errors(document):
    """Validate a JudgeResult 1.0.0 structured LLM output."""
    errors = schema_errors(document, 'judge-result')
    if errors:
        return errors
    dim = document['dimension']
    if dim == 'meaningful_response' and document.get('meaningful_response_start_ms') is None:
        if document['status'] == 'observed':
            errors.append('/meaningful_response_start_ms: required for observed meaningful_response')
    if dim == 'feedback_detection' and document['status'] == 'observed':
        if document.get('feedback_type') is None:
            errors.append('/feedback_type: required for observed feedback_detection')
        if document.get('feedback_start_ms') is None:
            errors.append('/feedback_start_ms: required for observed feedback_detection')
        if document.get('feedback_end_ms') is None:
            errors.append('/feedback_end_ms: required for observed feedback_detection')
    if dim == 'intent' and document['status'] == 'observed':
        if document.get('intent_label') is None:
            errors.append('/intent_label: required for observed intent')
    if dim == 'finding_candidate' and document['status'] == 'observed':
        if document.get('finding_severity') is None:
            errors.append('/finding_severity: required for observed finding_candidate')
        if document.get('suspected_layer') is None:
            errors.append('/suspected_layer: required for observed finding_candidate')
    if document.get('suspected_layer') is not None:
        if not document.get('requires_log_verification'):
            errors.append('/requires_log_verification: suspected_layer requires log verification')
        if document.get('attribution_confidence') is None:
            errors.append('/attribution_confidence: suspected_layer requires attribution_confidence')
    return errors
