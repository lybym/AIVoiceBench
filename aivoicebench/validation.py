"""Offline contract checks. Schema validity does not certify assets or hardware."""

import hashlib
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

#: Methods that name a non-acoustic producer. An ASR/diarization timestamp is
#: valid *input* to alignment, but presenting it as an acoustic boundary would
#: silently replace signal evidence with provider timing.
_NON_ACOUSTIC_METHOD = re.compile(
    r'(?:^|[^a-z])(?:asr|diarization|diarisation|speaker|llm|transcript|provider_timestamp)',
    re.IGNORECASE)

#: Methods implemented with stdlib signal processing only; they must not claim a
#: model/runtime provenance block.
_STDLIB_ACOUSTIC_METHODS = ('energy_vad',)


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
        # A timeout is the observed no-response window, not an instant: PRD-F008
        # requires a complete observation window for it. Silence and custom spans
        # are intervals too; the remaining event types are boundary points.
        if event['type'] not in ('asr_segment', 'custom', 'silence', 'timeout') and event['end_ms'] != event['start_ms']:
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
    version = metric.get('schema_version')
    # Version-specific contract. The shared schema accepts both 2.0.0 and 3.0.0
    # structurally; each version then enforces its own identity, policy and
    # traceability rules here. Do not silently normalize one into the other.
    if version == '3.0.0':
        from .metrics import DEFINITION_VERSION, LEGACY_METRIC_NAMES, prd_ref_for
        name = metric.get('name')
        declared = metric.get('definition_version')
        legacy_continuity = declared == DEFINITION_VERSION and name in LEGACY_METRIC_NAMES
        if not metric.get('prd_ref') and not legacy_continuity:
            errors.append('/prd_ref: MetricResult 3.0.0 requires a PRD requirement reference; only '
                          'a declared legacy metric kept for continuity may carry none')
        if legacy_continuity and not metric.get('reason'):
            errors.append('/reason: a legacy metric without a PRD reference must state that the '
                          'current PRD decomposition no longer defines it')
        # A PRD id is only meaningful together with the definition version that
        # assigns it. Reusing an old id for a name the current definition maps
        # elsewhere would silently reinterpret a historical requirement.
        expected = prd_ref_for(name, declared)
        if declared == DEFINITION_VERSION:
            if expected and metric.get('prd_ref') != expected:
                errors.append(f'/prd_ref: definition_version {DEFINITION_VERSION} maps {name} to '
                              f'{expected}; a different reference silently reinterprets a PRD id')
            elif not expected and metric.get('prd_ref'):
                errors.append(f'/prd_ref: {name} has no PRD requirement under definition_version '
                              f'{DEFINITION_VERSION}')
        if name == 'false_endpoint':
            errors.append('/name: 3.0.0 deterministic output must use false_endpoint_candidate; '
                          'false_endpoint is the legacy confirmed form and requires explicit '
                          'semantic/human confirmation evidence')
        if metric.get('policy') is not None and metric.get('policy_version') is None:
            errors.append('/policy_version: a declared policy requires its version')
        if metric.get('confidence') is not None and metric.get('confidence_source') is None:
            errors.append('/confidence_source: a numeric confidence requires its dimension source')
    elif version == '2.0.0':
        # Legacy contract: keep the original required shape so old artifacts stay
        # interpretable and cannot be retrofitted with 3.0.0 policy fields.
        if metric.get('case_id') is None:
            errors.append('/case_id: MetricResult 2.0.0 requires a case identity')
        if metric.get('confidence') is None:
            errors.append('/confidence: MetricResult 2.0.0 requires a numeric confidence')
        if metric.get('prd_ref') is not None or metric.get('policy') is not None:
            errors.append('/: 2.0.0 documents must not carry 3.0.0 policy fields; '
                          're-emit under 3.0.0 instead of rewriting history')
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
    # 3.0.0 allows nullable case_id for unscripted imported runs; only compare
    # when both sides carry an identity, so imported metrics stay validatable.
    for key in ('run_id', 'case_id', 'execution_kind'):
        left, right = metric.get(key), timeline.get(key)
        if left is not None and right is not None and left != right:
            errors.append(f'/: metric and timeline {key} disagree')
    evidence = {item['evidence_id']: item for item in timeline['evidence']}
    events = {item['event_id']: item for item in timeline['events']}
    if any(key not in evidence for key in metric['evidence_ids']):
        errors.append('/evidence_ids: unknown timeline evidence')
    if any(key not in events for key in metric['event_ids']):
        errors.append('/event_ids: unknown timeline event')
    decided = metric['status'] not in ('insufficient_evidence', 'not_applicable', 'invalid')
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
    errors.extend(_finding_version_errors(finding))
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
    errors.extend(_finding_turn_errors(finding, events, metric_map))
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


def _finding_version_errors(finding):
    """Version-specific Finding contract.

    The shared schema accepts both 2.0.0 and 2.1.0 structurally; each version then
    enforces its own identity rules here. 2.1.0 adds explicit Turn linkage.

    Both versions require a Case identity: a Finding resolves against the persisted
    Timeline, and an EventTimeline always carries one (an unscripted Recording
    Analysis Run carries the placeholder `CASE-auto` established by #24). A
    nullable `case_id` would therefore be unreachable, so it is not offered. The
    measurement layer's nullable case identity is its own convention and is
    documented in `docs/07-contract-versions.md` rather than silently unified.
    """
    version = finding.get('schema_version')
    errors = []
    if version == '2.1.0':
        if 'turn_ids' not in finding:
            errors.append('/turn_ids: Finding 2.1.0 requires the Turns it is bound to '
                          '(an empty list when the evidence establishes none)')
    elif version == '2.0.0':
        if finding.get('case_id') is None:
            errors.append('/case_id: Finding 2.0.0 requires a case identity')
        for field in ('turn_ids', 'analysis_id'):
            if field in finding:
                errors.append(f'/{field}: 2.0.0 documents must not carry 2.1.0 fields; '
                              're-emit under 2.1.0 instead of rewriting history')
    return errors


def _finding_turn_errors(finding, events, metric_map):
    """A Finding's declared Turns must be reachable from its own citations."""
    turn_ids = finding.get('turn_ids')
    if turn_ids is None:
        return []
    errors = []
    reachable = {events[event_id].get('turn_id') for event_id in finding.get('event_ids') or []
                 if event_id in events and events[event_id].get('turn_id')}
    reachable |= {metric_map[metric_id].get('turn_id') for metric_id in finding.get('metric_ids') or []
                  if metric_id in metric_map and metric_map[metric_id].get('turn_id')}
    for turn_id in turn_ids:
        if turn_id not in reachable:
            errors.append(f'/turn_ids: {turn_id!r} is not reachable from this finding\'s events '
                          'or metrics; a turn link must be traceable to evidence')
    for turn_id in sorted(reachable):
        if turn_id not in turn_ids:
            errors.append(f'/turn_ids: {turn_id!r} is reachable from this finding\'s citations but '
                          'is not declared')
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
    """Validate acoustic segment candidates without manufacturing events.

    Beyond shape, this rejects two provenance mistakes that would silently turn
    non-acoustic timing into an acoustic boundary:

    * a boundary whose ``method`` names an ASR/diarization producer instead of a
      signal processor (an ASR timestamp may be *aligned* with acoustic evidence,
      never relabelled as one);
    * a model-method document that omits the model/runtime provenance block, which
      would make its boundaries unreplayable and unauditable.
    """
    errors = schema_errors(document, 'acoustic-segments')
    if errors:
        return errors
    method = document['processor']['method']
    if _NON_ACOUSTIC_METHOD.search(method):
        errors.append(f'/processor/method: {method!r} is not a signal-based acoustic '
                      'producer; ASR/provider timestamps must never be reported as '
                      'acoustic boundaries')
    model = document['processor'].get('model')
    if model is not None:
        if 'threshold' not in document['processor']['parameters']:
            errors.append('/processor/parameters: a model VAD must record the absolute '
                          'probability threshold it applied')
        if method in _STDLIB_ACOUSTIC_METHODS:
            errors.append(f'/processor/method: {method!r} is a stdlib signal method and must '
                          'not claim a model/runtime provenance block')
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
    errors.extend(_semantic_result_errors(document))
    errors.extend(_anchor_errors(document))
    return errors


def _semantic_result_errors(document):
    """A semantic verdict needs its criterion, provenance and cited evidence.

    `schema_errors` already requires the members for the two canonical semantic
    dimensions. The rules here are the ones a schema cannot state: an abstention
    must say why, and an observed verdict must not be a fabricated default.
    """
    from .semantic_evidence import SEMANTIC_CRITERIA
    dimension = document.get('dimension')
    if dimension not in SEMANTIC_CRITERIA:
        return []
    criterion = SEMANTIC_CRITERIA[dimension]
    errors = []
    if document['status'] == 'observed':
        if not document.get('evidence_refs'):
            errors.append('/evidence_refs: an observed semantic verdict must cite evidence')
        if document.get('semantic_decision') is None:
            errors.append('/semantic_decision: an observed semantic verdict must be a boolean')
        if document.get('criterion_id') != criterion['criterion_id']:
            errors.append('/criterion_id: must name the declared criterion for this dimension')
        if document.get('criterion_version') != criterion['criterion_version']:
            errors.append('/criterion_version: must be the declared criterion version')
        profile = document.get('judge_profile')
        if isinstance(profile, dict) and profile.get('provider') == 'unavailable':
            errors.append('/judge_profile: an unavailable provider cannot produce an observed verdict')
    else:
        if document.get('semantic_decision') is not None:
            errors.append('/semantic_decision: an abstaining semantic result must not carry a decision')
        if not document.get('abstention_reason'):
            errors.append('/abstention_reason: an abstaining semantic result must state why')
    return errors


def _anchor_errors(document):
    """Reject model-authored timing and duplicated anchor roles.

    A boundary role may be selected once. Two different anchors for the same role
    would make the resolved interval depend on list order instead of evidence.
    """
    anchors = document.get('anchor_refs')
    if not anchors:
        return []
    errors = []
    roles = [anchor.get('role') for anchor in anchors]
    if len(roles) != len(set(roles)):
        errors.append('/anchor_refs: each boundary role may be selected at most once')
    for anchor in anchors:
        if anchor.get('end_ms', 0) < anchor.get('start_ms', 0):
            errors.append('/anchor_refs: an anchor end_ms precedes its start_ms')
    meaningful = [a for a in anchors if a.get('role') == 'meaningful_start']
    if meaningful and document.get('meaningful_response_start_ms') is not None:
        if abs(document['meaningful_response_start_ms'] - meaningful[0]['start_ms']) > 1e-6:
            errors.append('/meaningful_response_start_ms: must equal the selected anchor, not a '
                          'separately authored estimate')
    feedback_start = [a for a in anchors if a.get('role') == 'feedback_start']
    feedback_end = [a for a in anchors if a.get('role') == 'feedback_end']
    if feedback_start and document.get('feedback_start_ms') is not None:
        if abs(document['feedback_start_ms'] - feedback_start[0]['start_ms']) > 1e-6:
            errors.append('/feedback_start_ms: must equal the selected anchor')
    if feedback_end and document.get('feedback_end_ms') is not None:
        if abs(document['feedback_end_ms'] - feedback_end[0]['end_ms']) > 1e-6:
            errors.append('/feedback_end_ms: must equal the selected anchor')
    return errors


#: Credential-shaped material that must never reach a Judge artifact, a report or
#: a browser payload (PRD-N004). Keys are matched case-insensitively; values are
#: matched against provider key shapes.
_CREDENTIAL_KEYS = re.compile(r'(?:api[_-]?key|apikey|secret|password|passwd|token|bearer|authorization|credential)',
                              re.IGNORECASE)
_CREDENTIAL_VALUES = re.compile(r'(?:^|[^A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[a-z]_[A-Za-z0-9]{16,}|'
                                r'Bearer\s+[A-Za-z0-9._-]{8,})')


def credential_errors(document, location='/'):
    """Find long-lived credential material in a document tree.

    Only the *location* is reported. Echoing the offending text would move the
    secret into the validation message, which is itself persisted.
    """
    errors = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if _CREDENTIAL_KEYS.search(str(key)) and value not in (None, '', False):
                    errors.append(f'{path}{key}: credential-shaped field must not be persisted')
                walk(value, f'{path}{key}/')
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f'{path}{index}/')
        elif isinstance(node, str) and _CREDENTIAL_VALUES.search(node):
            errors.append(f'{path[:-1]}: credential-shaped value must not be persisted')

    walk(document, location)
    return errors


def judge_document_errors(document, timeline=None, turns_document=None):
    """Validate a JudgeResults 1.0.0 artifact and its references.

    Beyond shape this rejects the failure modes that would otherwise look like a
    successful semantic evaluation: an unrecorded invocation, a result citing
    evidence that does not exist, a semantic verdict without its criterion, and
    any credential material in the preserved raw provider output.
    """
    errors = schema_errors(document, 'judge-results')
    if errors:
        return errors
    from .semantic_evidence import CRITERIA_VERSION, SEMANTIC_CRITERIA
    if document.get('criteria_version') != CRITERIA_VERSION:
        errors.append(f'/criteria_version: the adapter contract is {CRITERIA_VERSION}')
    profile = document.get('judge_profile') or {}
    if profile.get('criteria_version') != document.get('criteria_version'):
        errors.append('/judge_profile/criteria_version: must agree with the artifact criteria_version')
    invocations = document.get('invocations') or []
    invocation_ids = [item.get('invocation_id') for item in invocations]
    if len(invocation_ids) != len(set(invocation_ids)):
        errors.append('/invocations: invocation_id must be unique')
    known = set(invocation_ids)
    results = document.get('results') or []
    judge_ids = [item.get('judge_id') for item in results]
    if len(judge_ids) != len(set(judge_ids)):
        errors.append('/results: judge_id must be unique')
    for index, result in enumerate(results):
        prefix = f'/results/{index}'
        for message in judge_result_errors(result):
            errors.append(f'{prefix}{message}')
        invocation_id = result.get('invocation_id')
        if invocation_id not in known:
            errors.append(f'{prefix}/invocation_id: must resolve to a recorded invocation')
        if result.get('run_id') != document.get('run_id'):
            errors.append(f'{prefix}/run_id: must agree with the artifact run_id')
    for index, item in enumerate(invocations):
        prefix = f'/invocations/{index}'
        if item.get('status') == 'success' and item.get('raw_response') is None:
            errors.append(f'{prefix}/raw_response: a successful call must preserve its raw output')
        digest_value = item.get('raw_response_sha256')
        response = item.get('raw_response')
        if (response is None) != (digest_value is None):
            errors.append(f'{prefix}/raw_response_sha256: must accompany exactly the raw output it hashes')
        elif response is not None and hashlib.sha256(response.encode('utf-8')).hexdigest() != digest_value:
            errors.append(f'{prefix}/raw_response_sha256: does not match the preserved raw output')
        for message in credential_errors(item, prefix + '/'):
            errors.append(message)
    for message in credential_errors(profile, '/judge_profile/'):
        errors.append(message)
    if timeline is not None:
        timeline_issues = timeline_errors(timeline)
        if timeline_issues:
            errors.append('Referenced timeline is invalid: ' + timeline_issues[0])
        else:
            errors.extend(_result_reference_errors(results, timeline, turns_document))
    return errors


def _result_reference_errors(results, timeline, turns_document):
    """Every cited evidence/event/turn reference must resolve."""
    errors = []
    evidence = {item.get('evidence_id') for item in timeline.get('evidence') or ()}
    events = {item.get('event_id') for item in timeline.get('events') or ()}
    turns = {item.get('turn_id') for item in (turns_document or {}).get('turns') or ()}
    for index, result in enumerate(results):
        prefix = f'/results/{index}'
        for reference in result.get('evidence_refs') or ():
            if reference not in evidence:
                errors.append(f'{prefix}/evidence_refs: unknown timeline evidence {reference!r}')
        for reference in result.get('event_refs') or ():
            if reference not in events:
                errors.append(f'{prefix}/event_refs: unknown timeline event {reference!r}')
        turn_id = result.get('turn_id')
        if turn_id is not None and turns and turn_id not in turns:
            errors.append(f'{prefix}/turn_id: unknown turn in the supplied Turns document')
    return errors


def semantic_evidence_errors(records, timeline, turns_document=None):
    """Validate the constrained semantic records that feed PRD-M003/M006.

    The engine silently drops an ineligible record and abstains, which is correct
    but invisible. This validator makes the drop explicit at the producer, so a
    mis-shaped Judge can never quietly become "no semantic evidence".
    """
    from .semantic_evidence import CRITERIA_VERSION, SEMANTIC_CRITERIA, turn_evidence
    errors = []
    evidence = {item.get('evidence_id') for item in timeline.get('evidence') or ()}
    events = {item.get('event_id') for item in timeline.get('events') or ()}
    turns = {item.get('turn_id'): item for item in (turns_document or {}).get('turns') or ()}
    kinds = {'semantic_response': 'semantic_response', 'barge_in_compliance': 'barge_in_compliance'}
    declared = {criterion['kind']: criterion for criterion in SEMANTIC_CRITERIA.values()}
    seen = set()
    for index, record in enumerate(records or ()):
        prefix = f'/records/{index}'
        if not isinstance(record, dict):
            errors.append(f'{prefix}: a semantic record must be an object')
            continue
        kind = record.get('kind')
        if kind not in kinds:
            errors.append(f'{prefix}/kind: {kind!r} is not a constrained semantic kind')
            continue
        criterion = declared[kind]
        key = (kind, record.get('turn_id'))
        if key in seen:
            errors.append(f'{prefix}: duplicate semantic record for this turn')
        seen.add(key)
        if type(record.get('decision')) is not bool:
            errors.append(f'{prefix}/decision: must be a boolean')
        if record.get('criterion_id') != criterion['criterion_id']:
            errors.append(f'{prefix}/criterion_id: must name the declared criterion')
        if record.get('criterion_version') != criterion['criterion_version']:
            errors.append(f'{prefix}/criterion_version: must be the declared criterion version')
        if not record.get('judge_profile'):
            errors.append(f'{prefix}/judge_profile: required for a performed judgment')
        if not (record.get('evidence_ids') or record.get('event_ids')):
            errors.append(f'{prefix}: requires evidence or event references')
        turn_id = record.get('turn_id')
        if turn_id is not None and turns and turn_id not in turns:
            errors.append(f'{prefix}/turn_id: unknown turn in the supplied Turns document')
        turn = turns.get(turn_id)
        if turn is not None:
            allowed_evidence, allowed_events = turn_evidence(turn, timeline)
            for reference in record.get('evidence_ids') or ():
                if reference not in evidence:
                    errors.append(f'{prefix}/evidence_ids: unknown timeline evidence {reference!r}')
                elif reference not in allowed_evidence:
                    errors.append(f'{prefix}/evidence_ids: {reference!r} belongs to another turn')
            for reference in record.get('event_ids') or ():
                if reference not in events:
                    errors.append(f'{prefix}/event_ids: unknown timeline event {reference!r}')
                elif reference not in allowed_events:
                    errors.append(f'{prefix}/event_ids: {reference!r} belongs to another turn')
    return errors
