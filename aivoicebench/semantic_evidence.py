"""Project validated Judge results into the constrained semantic evidence contract.

PRD-F010/F011 + PRD-N003. The deterministic engine already defines what a
semantic measurement may be built from: PRD-M003 (`semantic_response`) and
PRD-M006 (`barge_in_semantic_compliance`) accept only *constrained semantic
evidence* — a boolean decision, a criterion id/version, a judge profile and
evidence/event references that resolve in the same Timeline. Anything less
abstains.

This module is that adapter, and it is deliberately the only place where a
JudgeResult becomes measurement input:

* a semantic claim is eligible only when the Timeline it cites really exists and
  the cited evidence/events belong to the turn being judged;
* an ineligible result never becomes a value. It is recorded as an explicit
  abstention with a reason, so a missing judgment is visible instead of looking
  like a `false` verdict;
* boundaries are *selected* from measured anchors, never authored by the model.
  `turn_anchors` exposes the existing event boundaries; `resolve_anchors` maps a
  provider's anchor selection back onto them and refuses anything else.

Nothing here computes a metric value, and nothing here invents timing.
"""

CRITERIA_VERSION = '1.0.0'

# One entry per semantic dimension that a canonical metric consumes. `kind` is
# the constrained-evidence kind `metrics._semantic_record` matches on; `metric`
# is the PRD-M requirement the resulting MetricResult is traceable to.
SEMANTIC_CRITERIA = {
    'semantic_response': {
        'kind': 'semantic_response',
        'criterion_id': 'CRIT-SEMANTIC-RESPONSE',
        'criterion_version': '1.0.0',
        'metric': 'PRD-M003',
        'meaning': 'The device response is semantically relevant/meaningful for the tester request',
    },
    'barge_in_compliance': {
        'kind': 'barge_in_compliance',
        'criterion_id': 'CRIT-BARGE-IN-COMPLIANCE',
        'criterion_version': '1.0.0',
        'metric': 'PRD-M006',
        'meaning': 'The response after an interruption honours the new tester intent',
    },
}

#: Boundary roles a judgment may select. Each resolves to a measured anchor.
ANCHOR_ROLES = ('meaningful_start', 'feedback_start', 'feedback_end', 'response_start')

#: Event-type prefixes that belong to one speaker role. A judgment about the
#: device response must not cite the tester's own speech as its evidence.
ROLE_EVENT_PREFIXES = {'tester': ('tester_',), 'device': ('device_', 'response_')}

#: Event types that are not usable as an anchor for semantic timing because they
#: are provider/derived estimates rather than measured boundaries.
NON_ANCHOR_EVENT_SOURCES = ('derived',)


def criterion_for(dimension):
    """Return the declared criterion for a semantic dimension, or None."""
    record = SEMANTIC_CRITERIA.get(dimension)
    return dict(record) if record else None


def _role_matches(event, role):
    if role is None:
        return True
    prefixes = ROLE_EVENT_PREFIXES.get(role)
    if prefixes is None:
        raise ValueError(f'Unknown evidence role {role!r}; expected one of {tuple(ROLE_EVENT_PREFIXES)}')
    return str(event.get('type') or '').startswith(prefixes)


def judge_profile_id(provider, model, criteria_version=CRITERIA_VERSION):
    """Build the stable identity a MetricResult records as its judge_profile.

    The id must survive a provider/model rename only by producing a *different*
    id, so it is derived from the exact provenance fields instead of a counter.
    """
    raw = f'{provider}|{model}|{criteria_version}'
    cleaned = ''.join(character if (character.isalnum() or character in '_.-') else '-'
                      for character in raw)
    return 'JUDGE-' + cleaned[:120]


def build_judge_profile(provider, model, prompt_version, criteria_version=CRITERIA_VERSION,
                        endpoint=None, config_version=None):
    """Full provenance block recorded on the Judge artifact and each result."""
    return {
        'profile_id': judge_profile_id(provider, model, criteria_version),
        'provider': provider,
        'model': model,
        'prompt_version': prompt_version,
        'criteria_version': criteria_version,
        'config_version': config_version,
    }


def turn_anchors(turn, timeline, role=None):
    """Measured boundaries the Judge may select for one turn.

    Anchors are the Timeline events bound to that turn. A turn without any event
    has no measured boundary and therefore no eligible semantic timing. A `role`
    restricts the scope to that speaker's own events.
    """
    if not isinstance(turn, dict) or not isinstance(timeline, dict):
        return []
    turn_id = turn.get('turn_id')
    anchors = []
    for event in timeline.get('events') or ():
        if not isinstance(event, dict) or event.get('turn_id') != turn_id:
            continue
        event_id = event.get('event_id')
        if not event_id or not _role_matches(event, role):
            continue
        anchors.append({
            'anchor_id': event_id,
            'event_type': event.get('type'),
            'start_ms': event.get('start_ms'),
            'end_ms': event.get('end_ms'),
            'source': event.get('confidence_source') or event.get('source') or 'event',
            'usable': bool(event.get('evidence_ids'))
                    and event.get('source') not in NON_ANCHOR_EVENT_SOURCES,
        })
    return sorted(anchors, key=lambda item: (item['start_ms'], item['anchor_id']))


def turn_evidence(turn, timeline, role=None):
    """Evidence and event ids that belong to one turn.

    A semantic claim about turn T may only cite T's own evidence. Letting it cite
    an arbitrary Timeline object would make an unreviewable cross-turn claim look
    like a supported one.
    """
    evidence_ids, event_ids = [], []
    for anchor in turn_anchors(turn, timeline, role):
        event_ids.append(anchor['anchor_id'])
        for evidence_id in (timeline_event(anchor['anchor_id'], timeline) or {}).get('evidence_ids') or ():
            evidence_ids.append(evidence_id)
    return list(dict.fromkeys(evidence_ids)), list(dict.fromkeys(event_ids))


def timeline_event(event_id, timeline):
    for event in (timeline or {}).get('events') or ():
        if isinstance(event, dict) and event.get('event_id') == event_id:
            return event
    return None


def turn_event_types(turn, timeline):
    """Event types bound to one turn.

    Used so the Judge and the deterministic engine answer "was this turn
    interrupted?" from the same evidence instead of from two different rules.
    """
    if not isinstance(turn, dict) or not isinstance(timeline, dict):
        return set()
    turn_id = turn.get('turn_id')
    return {event.get('type') for event in timeline.get('events') or ()
            if isinstance(event, dict) and event.get('turn_id') == turn_id}


def has_interrupt_evidence(turn, timeline):
    """Whether the Turn carries an `interrupt_start` event.

    This is the same predicate PRD-M006 uses to decide applicability, so a
    semantic judgment is requested exactly when a metric can consume it.
    """
    return 'interrupt_start' in turn_event_types(turn, timeline)


def resolve_anchors(selection, anchors):
    """Resolve a provider's anchor selection against measured anchors.

    Returns (resolved, problems). A selection naming an unknown or unusable
    anchor, or carrying its own milliseconds, is a problem: the Judge picks an
    existing boundary, it never authors timing.
    """
    resolved, problems = [], []
    available = {anchor['anchor_id']: anchor for anchor in anchors}
    for item in selection or ():
        if not isinstance(item, dict):
            problems.append('anchor selection must be an object')
            continue
        role = item.get('role')
        anchor_id = item.get('anchor_id')
        if role not in ANCHOR_ROLES:
            problems.append(f'anchor role {role!r} is not one of {ANCHOR_ROLES}')
            continue
        if any(key in item for key in ('start_ms', 'end_ms')):
            problems.append(f'anchor {role} carries model-authored milliseconds')
            continue
        anchor = available.get(anchor_id)
        if anchor is None:
            problems.append(f'anchor {anchor_id!r} does not exist in the judged turn')
            continue
        if not anchor['usable']:
            problems.append(f'anchor {anchor_id!r} has no measured evidence')
            continue
        resolved.append({'role': role, 'anchor_id': anchor_id,
                         'start_ms': anchor['start_ms'], 'end_ms': anchor['end_ms'],
                         'source': anchor['source']})
    return resolved, problems


def anchor_for(resolved, role):
    for item in resolved or ():
        if item['role'] == role:
            return item
    return None


def eligibility_problems(result, timeline, turn, judge_artifact=None):
    """Structural problems that stop a JudgeResult from becoming semantic evidence.

    An empty list means eligible. Every problem is a reason to abstain, never a
    reason to reinterpret the result.
    """
    problems = []
    dimension = result.get('dimension')
    criterion = criterion_for(dimension)
    if criterion is None:
        return [f'dimension {dimension!r} is not a canonical semantic dimension']
    if result.get('status') != 'observed':
        problems.append(f'status is {result.get("status")!r}, not observed')
    if type(result.get('semantic_decision')) is not bool:
        problems.append('a semantic decision must be a boolean')
    if result.get('criterion_id') != criterion['criterion_id']:
        problems.append('criterion_id does not match the declared criterion')
    if result.get('criterion_version') != criterion['criterion_version']:
        problems.append('criterion_version does not match the declared criterion')
    profile = result.get('judge_profile')
    if not isinstance(profile, dict) or not profile.get('profile_id'):
        problems.append('a judgment requires its judge profile provenance')
    elif profile.get('criteria_version') != CRITERIA_VERSION:
        problems.append('judge profile criteria_version disagrees with the adapter contract')
    invocation_id = result.get('invocation_id')
    if not invocation_id:
        problems.append('a judgment requires the invocation that produced it')
    elif judge_artifact is not None:
        known = {item.get('invocation_id') for item in judge_artifact.get('invocations') or ()}
        if invocation_id not in known:
            problems.append('invocation_id does not resolve to a recorded invocation')
        else:
            record = next((item for item in judge_artifact['invocations']
                           if item.get('invocation_id') == invocation_id), {})
            if record.get('status') != 'success':
                problems.append(f'invocation status is {record.get("status")!r}, not success')
    if not isinstance(timeline, dict):
        problems.append('a semantic judgment requires the Timeline it cites')
        return problems
    if turn is None:
        problems.append('the judged turn does not exist in the Turns document')
        return problems
    if result.get('turn_id') != turn.get('turn_id'):
        problems.append('the result turn_id does not match the judged turn')
    turn_evidence_ids, turn_event_ids = turn_evidence(turn, timeline)
    timeline_evidence = {item.get('evidence_id') for item in timeline.get('evidence') or ()}
    timeline_events = {item.get('event_id') for item in timeline.get('events') or ()}
    evidence_refs = result.get('evidence_refs') or []
    event_refs = result.get('event_refs') or []
    if not evidence_refs and not event_refs:
        problems.append('a semantic judgment requires evidence or event references')
    for reference in evidence_refs:
        if reference not in timeline_evidence:
            problems.append(f'evidence reference {reference!r} does not exist in the Timeline')
        elif reference not in turn_evidence_ids:
            problems.append(f'evidence reference {reference!r} belongs to another turn')
    for reference in event_refs:
        if reference not in timeline_events:
            problems.append(f'event reference {reference!r} does not exist in the Timeline')
        elif reference not in turn_event_ids:
            problems.append(f'event reference {reference!r} belongs to another turn')
    return problems


def semantic_evidence_records(judge_artifact, timeline, turns_document=None):
    """Return (records, abstentions) for every semantic dimension judged.

    `records` are eligible constrained-semantic-evidence records for
    `compute_timeline_metrics(semantic_evidence=...)`. `abstentions` are the
    explicit, reasoned rejections. A dimension judged for a turn produces exactly
    one of the two, so absence is never ambiguous.
    """
    turns = {turn.get('turn_id'): turn for turn in (turns_document or {}).get('turns') or ()}
    records, abstentions = [], []
    seen = set()
    for result in (judge_artifact or {}).get('results') or ():
        dimension = result.get('dimension')
        criterion = criterion_for(dimension)
        if criterion is None:
            continue
        turn_id = result.get('turn_id')
        key = (dimension, turn_id)
        if key in seen:
            abstentions.append(_abstention(result, 'invalid', 'duplicate judgment for this turn'))
            continue
        seen.add(key)
        turn = turns.get(turn_id)
        problems = eligibility_problems(result, timeline, turn, judge_artifact)
        if problems:
            abstentions.append(_abstention(result, 'insufficient_evidence', '; '.join(problems)))
            continue
        profile = result['judge_profile']
        evidence_refs = list(result.get('evidence_refs') or [])
        event_refs = list(result.get('event_refs') or [])
        records.append({
            'kind': criterion['kind'],
            'turn_id': turn_id,
            'response_id': result.get('response_id'),
            'decision': result['semantic_decision'],
            'criterion_id': criterion['criterion_id'],
            'criterion_version': criterion['criterion_version'],
            'metric': criterion['metric'],
            'judge_profile': profile['profile_id'],
            'judge_id': result.get('judge_id'),
            'invocation_id': result.get('invocation_id'),
            'confidence': result.get('confidence'),
            'evidence_ids': evidence_refs,
            'event_ids': event_refs,
            'rationale': result.get('reason'),
        })
    return records, abstentions


def _abstention(result, state, reason):
    return {
        'dimension': result.get('dimension'),
        'turn_id': result.get('turn_id'),
        'response_id': result.get('response_id'),
        'state': state,
        'reason': reason,
        'invocation_id': result.get('invocation_id'),
    }