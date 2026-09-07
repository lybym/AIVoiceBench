"""Deterministic evaluation of evidence-backed canonical events; no root-cause guesses."""

from pathlib import Path

from .formulas import character_error_rate, latency_ms, overlap_ms, percentiles
from .runner import _missing_metric, contained_file, digest
from .validation import case_errors, metric_errors, timeline_errors


def _intervals(events, start_type, end_type):
    active, intervals = {}, []
    for event in events:
        key = (event['turn_id'], event['response_id'])
        if event['type'] == start_type:
            active[key] = event
        elif event['type'] == end_type and key in active:
            intervals.append((active.pop(key), event))
    return intervals


def evaluate(case, timeline, artifact_root=None, turn_id=None):
    issues = case_errors(case) + timeline_errors(timeline)
    if issues:
        raise ValueError('\n'.join(issues))
    if case['case_id'] != timeline['case_id'] or case['case_version'] != timeline['case_version']:
        raise ValueError('Case identity/version does not match Timeline snapshot')
    events = timeline['events']
    turns = {event['turn_id'] for event in events if event['type'] == 'tester_speech_start'}
    if turn_id is None:
        if len(turns) == 1:
            turn_id = next(iter(turns))
        elif case['mode'] == 'interactive':
            interrupts = [event for event in events if event['type'] == 'interrupt_start']
            if len(interrupts) == 1:
                turn_id = interrupts[0]['turn_id']
    evidence = {item['evidence_id']: item for item in timeline['evidence']}
    artifacts = {item['artifact_id']: item for item in timeline['artifacts']}
    tracks = {item['track_id']: item for item in timeline['tracks']}
    file_problem = None
    if timeline['execution_kind'] not in ('synthetic', 'dry_run'):
        if artifact_root is None:
            file_problem = 'Artifact root required to verify real/imported evidence files'
        else:
            for item in artifacts.values():
                try:
                    if digest(contained_file(artifact_root, item['path'])) != item['sha256']:
                        raise ValueError('Artifact SHA-256 mismatch')
                except (OSError, ValueError) as error:
                    file_problem = str(error)
                    break
    timing_ready = bool(tracks) and all(track['sync']['status'] != 'uncalibrated' for track in tracks.values())
    trusted_boundaries = all(event['source'] in ('audio_signal', 'manual_annotation') or
        (event['source'] == 'derived' and all(evidence[key]['source'] in ('audio_signal', 'manual_annotation')
                                             for key in event['evidence_ids']))
        for event in events if event['type'].endswith(('_start', '_end')))
    tester_intervals = _intervals(events, 'tester_speech_start', 'tester_speech_end')
    device_intervals = _intervals(events, 'device_speech_start', 'device_speech_end')
    pauses = _intervals(events, 'planned_pause_start', 'planned_pause_end')
    results = []

    def observed(metric, value, selected, extra_evidence=()):
        metric.update(value=value, status='observed', confidence=min((event['confidence'] for event in selected), default=0))
        metric.pop('reason', None)
        metric['event_ids'] = list(dict.fromkeys(event['event_id'] for event in selected))
        metric['evidence_ids'] = list(dict.fromkeys([key for event in selected for key in event['evidence_ids']] + list(extra_evidence)))
        metric['aggregation'].update(sample_count=1, total_count=1, excluded_count=0)
        for assertion in case['expected']['assertions']:
            if assertion['metric_id'] != metric['name'] or assertion['evaluator'] != 'deterministic':
                continue
            if 'threshold' in metric:
                raise ValueError('Multiple thresholds for one metric require a separate evaluation policy')
            op, limit = assertion['operator'], assertion['value']
            if type(value) is bool and (op != 'eq' or type(limit) is not bool):
                raise ValueError('Boolean metric requires a boolean eq assertion')
            if type(value) is not bool and type(limit) not in (int, float):
                raise ValueError('Numeric metric requires a numeric assertion')
            uncertainty = sum(track['sync']['uncertainty_ms'] + track['sync']['max_drift_ppm'] *
                max((event['start_ms'] for event in selected), default=0) / 1000000 for track in tracks.values()) if metric['unit'] == 'ms' else 0
            if uncertainty and abs(value - limit) <= uncertainty:
                metric.update(value=None, status='insufficient_evidence', reason='Timing uncertainty overlaps the configured threshold')
                metric['aggregation'].update(sample_count=0, excluded_count=1)
                return
            compare = {'eq': lambda: value == limit, 'lt': lambda: value < limit, 'lte': lambda: value <= limit,
                       'gt': lambda: value > limit, 'gte': lambda: value >= limit}[op]
            metric['threshold'] = {'operator': op, 'value': limit, 'policy_id': case['case_id'] + '.' + assertion['assertion_id'],
                                   'policy_version': case['case_version'], 'source': 'case'}
            metric['status'] = 'pass' if compare() else 'fail'

    def not_applicable(metric, reason):
        metric.update(status='not_applicable', reason=reason)

    for name in case['metrics']:
        metric = _missing_metric(case, timeline, name)
        metric['execution_kind'] = timeline['execution_kind']
        metric['reason'] = 'Required observations or evaluator are unavailable'
        if file_problem:
            metric['reason'] = file_problem
        elif timeline['status'] != 'complete':
            metric['reason'] = 'Timeline is partial/blocked; complete observation coverage is required'
        elif name == 'timeout_rate':
            timeouts = [event for event in events if event['type'] == 'timeout' and event['source'] == 'controller'
                        and event['start_ms'] >= case['case_timeout_ms']]
            healthy_window = [key for key, item in evidence.items() if item['track_id'] in tracks
                              and tracks[item['track_id']]['role'] == 'device_output'
                              and item['start_ms'] == 0 and item['end_ms'] >= case['case_timeout_ms']]
            if len(timeouts) == 1 and healthy_window:
                observed(metric, 1.0, timeouts, healthy_window)
            elif not timeouts and tester_intervals:
                final_tester_end = tester_intervals[-1][1]
                completed = [(start, end) for start, end in device_intervals if start['turn_id'] == final_tester_end['turn_id']
                             and start['start_ms'] >= final_tester_end['start_ms'] and end['start_ms'] < case['case_timeout_ms']]
                if completed:
                    observed(metric, 0.0, [final_tester_end, *completed[-1]])
        elif name == 'asr_cer':
            reference = case['expected'].get('reference_text')
            transcript_events = [event for event in events if event['type'] == 'asr_segment' and event['source'] == 'device_log' and event['turn_id'] == turn_id]
            if not reference:
                not_applicable(metric, 'Nonempty reference text is required for device ASR CER')
            elif transcript_events:
                hypothesis = ''.join(event['payload']['text'] for event in transcript_events)
                observed(metric, character_error_rate(reference, hypothesis)['value'], transcript_events)
            else:
                metric['reason'] = 'Device-internal ASR log transcript is required; external ASR is not device truth'
        elif name in ('e2e_first_audio_latency_ms', 'false_endpoint', 'barge_in_stop_latency_ms', 'overlap_duration_ms'):
            if not timing_ready or not trusted_boundaries:
                metric['reason'] = 'Synchronized tracks and reviewed/audio-derived boundaries required; ASR/scheduling estimates are insufficient'
            elif name == 'e2e_first_audio_latency_ms':
                tester_ends = [end for _, end in tester_intervals if end['turn_id'] == turn_id]
                starts = [event for event in events if event['type'] == 'device_speech_start' and event['turn_id'] == turn_id]
                if tester_ends and starts:
                    end, start = tester_ends[-1], starts[0]
                    if start['start_ms'] < end['start_ms']:
                        not_applicable(metric, 'Device responded before final utterance end; analyze overlap/false endpoint')
                    else:
                        observed(metric, latency_ms(end['start_ms'], start['start_ms']), [end, start])
            elif name == 'false_endpoint':
                if not pauses:
                    not_applicable(metric, 'No planned intra-utterance pause')
                else:
                    selected, coverage = [], []
                    false_endpoint = False
                    for start, end in pauses:
                        # A complete channel snippet must cover the entire planned pause.
                        snippets = [key for key, item in evidence.items() if item['track_id'] in tracks
                                    and tracks[item['track_id']]['role'] == 'device_output'
                                    and item['start_ms'] <= start['start_ms'] and item['end_ms'] >= end['start_ms']]
                        resumed = [event for event in events if event['type'] == 'tester_speech_start'
                                   and event['turn_id'] == start['turn_id'] and event['start_ms'] == end['start_ms']]
                        if not snippets or not resumed:
                            break
                        during = [event for event in events if event['type'] == 'device_speech_start'
                                  and start['start_ms'] < event['start_ms'] < end['start_ms']]
                        false_endpoint |= bool(during)
                        selected.extend([start, end, *resumed, *during])
                        coverage.extend(snippets)
                    else:
                        observed(metric, false_endpoint, selected, coverage)
            elif name == 'barge_in_stop_latency_ms':
                interrupts = [event for event in events if event['type'] == 'interrupt_start' and event['turn_id'] == turn_id]
                if len(interrupts) == 1:
                    interrupt = interrupts[0]
                    old = [(start, end) for start, end in device_intervals if start['start_ms'] < interrupt['start_ms'] < end['start_ms']]
                    if len(old) == 1:
                        start, end = old[0]
                        observed(metric, end['start_ms'] - interrupt['start_ms'], [start, interrupt, end])
                    elif not old:
                        not_applicable(metric, 'No identified old response active at interruption')
            else:
                if tester_intervals and device_intervals:
                    observed(metric, overlap_ms([(start['start_ms'], end['start_ms']) for start, end in tester_intervals],
                        [(start['start_ms'], end['start_ms']) for start, end in device_intervals]),
                        [event for pair in tester_intervals + device_intervals for event in pair])
        errors = metric_errors(metric, timeline)
        if errors:
            raise ValueError('Invalid metric output: ' + '\n'.join(errors))
        results.append(metric)
    return results


def aggregate_latency(results):
    if not results:
        return {**percentiles([]), 'input_metric_ids': [], 'evidence_refs': []}
    keys = {(item['name'], item['definition_version'], item['measurement_scope'], item['execution_kind']) for item in results}
    if len({item['metric_id'] for item in results}) != len(results):
        raise ValueError('Duplicate metric samples cannot be counted twice')
    if len(keys) != 1 or any(item['unit'] != 'ms' or item['aggregation']['kind'] != 'single' for item in results):
        raise ValueError('Only compatible single-sample latency results may be aggregated')
    eligible = [item for item in results if item['status'] in ('observed', 'pass', 'fail')]
    values = [item['value'] if item in eligible else None for item in results]
    return {**percentiles(values), 'name': results[0]['name'], 'execution_kind': results[0]['execution_kind'],
            'input_metric_ids': [item['metric_id'] for item in eligible],
            'evidence_refs': [{'run_id': item['run_id'], 'evidence_id': key} for item in eligible for key in item['evidence_ids']]}
