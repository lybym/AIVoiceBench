"""Durable operation records for an asynchronous role-review rebuild.

Saving a manual speaker-role mapping reruns attribution → fusion → turns →
timeline → Judge → metrics → findings → report as a new AnalysisRevision. That is
a minutes-long request, and Issue #113 recorded what answering it synchronously
cost: no trackable identifier, no progress, and a single generic ``409`` that
could equally mean "a rebuild is running", "the evidence is insufficient" or "the
processor crashed".

This module owns the operation record only. It answers three questions without
re-deriving anything:

* **What was accepted** — the saved decisions, the reviewer and the source
  AnalysisRevision, written *before* any work starts. A retry therefore never
  submits a second rebuild behind the first one: the caller is given the
  operation that already exists.
* **Where it is now** — the phase, the ordered phase list and the elapsed time.
  Phases come from the reanalysis's own stage ledger, not from a second
  interpretation of the pipeline.
* **How it ended** — a terminal status plus one distinguishing ``error.code``.

Records live next to the Run's directory (``<root>/.role-review-operations/<run_id>/``,
the same convention the cross-process Run lock uses), so they survive a process
restart without becoming part of the Run's artifact chain. That separation is
deliberate: a Run's registered artifacts are immutable evidence, and an operation
record is mutated as it progresses — writing it inside the Run would make the
Run's own integrity check fail. Nothing here computes a role, a metric or an
event, and no record is ever registered as an artifact.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

OPERATION_PATTERN = 'OP-'
SCHEMA_VERSION = '1.0.0'

#: Statuses that mean "this Run already has a rebuild in flight".
ACTIVE_STATUSES = ('accepted', 'running')
TERMINAL_STATUSES = ('succeeded', 'failed')

#: The phases a rebuild passes through, in order. ``lock`` and
#: ``restore_evidence`` are reported by the worker; every later name is a stage in
#: the Run's own ledger, so the progress a caller reads is the ledger's own state.
REBUILD_PHASES = (
    'lock',
    'restore_evidence',
    'attribution',
    'fusion',
    'turns',
    'timeline',
    'judge',
    'metrics',
    'findings',
    'report',
)

#: Every failure this API reports, so a caller can branch on the code instead of
#: the message. ``retryable`` says whether submitting the same save again can be
#: expected to behave differently.
ERROR_CODES = {
    # Request refusals: answered synchronously, before any operation exists.
    'invalid_request': {'http_status': 400, 'retryable': False,
                        'message': 'The role-review request is not valid'},
    'forbidden_origin': {'http_status': 403, 'retryable': False,
                         'message': 'Submit the role decision from the served page'},
    'payload_too_large': {'http_status': 413, 'retryable': False,
                          'message': 'The role-review request is too large'},
    'legacy_analysis': {'http_status': 409, 'retryable': False,
                        'message': 'This Run predates the current pipeline; re-import it'},
    # Operation outcomes a caller must be able to tell apart (Issue #113).
    'run_locked': {'http_status': 409, 'retryable': True,
                   'message': 'Another analysis is currently writing this Run'},
    'operation_in_progress': {'http_status': 409, 'retryable': True,
                              'message': 'A role rebuild for this Run is already accepted or running'},
    'insufficient_evidence': {'http_status': 422, 'retryable': False,
                              'message': 'This Run cannot produce a role decision from its preserved evidence'},
    'rebuild_failed': {'http_status': 500, 'retryable': True,
                       'message': ('The rebuild could not finish its own new revision; the previous '
                                   'revision was kept unchanged and the save can be resubmitted')},
    'internal_error': {'http_status': 500, 'retryable': True,
                       'message': 'The role rebuild failed inside a processor'},
    'interrupted': {'http_status': 503, 'retryable': True,
                    'message': 'The rebuild stopped without a terminal result and can be resubmitted'},
    'operation_not_found': {'http_status': 404, 'retryable': False,
                            'message': 'Unknown role-review operation for this Run'},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def operations_dir(directory):
    """Where this Run's operation records live.

    Outside the Run's own directory on purpose: the Run tree is scanned for
    immutable retained diagnostics, and a record that is rewritten as it
    progresses must not be mistaken for one (see the module docstring).
    """
    run = Path(directory)
    return run.parent / '.role-review-operations' / run.name


def _parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _elapsed_ms(started_at, finished_at=None):
    start = _parse_time(started_at)
    end = _parse_time(finished_at) or datetime.now(timezone.utc)
    if start is None:
        return None
    return round((end - start).total_seconds() * 1000, 3)


def _write(path, payload):
    """Atomically replace one operation record; a reader never sees a partial file.

    The final ``replace`` is retried a bounded number of times: on Windows a
    replace can transiently collide with a concurrent open of the same path
    (an antivirus scan, a reader that started mid-rename). The retry only
    re-attempts the rename of an already complete pending file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending')
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    last_error = None
    for _ in range(5):
        try:
            pending.replace(path)
            return
        except PermissionError as error:  # transient; the pending file is complete
            last_error = error
            time.sleep(0.01)
    raise last_error


def _read_record(path):
    """Read one complete operation record, tolerating a transient rename race.

    ``_write`` replaces the record atomically, so a file that opens is always a
    full record — but on Windows an open that lands in the rename window can be
    denied outright (``PermissionError``). A bounded retry keeps every reader
    (the API, the worker, the heartbeat) on its feet without ever weakening the
    "an unreadable record is reported" contract: a genuinely corrupt or locked
    file still raises after the retries.
    """
    last_error = None
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError as error:  # transient open/rename collision
            last_error = error
            time.sleep(0.01)
    raise last_error


def load_operations(directory):
    """Every operation record for this Run, newest first.

    An unreadable record is reported by raising: it is this Run's own bookkeeping,
    so silently skipping it would hide a rebuild that may still be running.
    """
    folder = operations_dir(directory)
    if not folder.is_dir():
        return []
    records = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or not path.name.startswith(OPERATION_PATTERN):
            continue
        if path.name.endswith('.pending'):
            continue
        records.append(_read_record(path))
    records.sort(key=lambda item: item.get('created_at') or '', reverse=True)
    return records


def load_operation(directory, operation_id):
    for record in load_operations(directory):
        if record.get('operation_id') == operation_id:
            return record
    return None


def active_operation(directory):
    """The rebuild already in flight for this Run, or ``None``."""
    for record in load_operations(directory):
        if record.get('status') in ACTIVE_STATUSES:
            return record
    return None


def operation_is_stalled(record, stale_after_ms):
    """Whether an active operation has stopped heartbeating.

    A stalled rebuild is a *pending* state, not a failure: it stays queryable and
    the record is only retired when an operator explicitly resubmits the save.
    """
    if record is None or record.get('status') not in ACTIVE_STATUSES:
        return False
    heartbeat = _parse_time(record.get('heartbeat_at') or record.get('created_at'))
    if heartbeat is None:
        return True
    age_ms = (datetime.now(timezone.utc) - heartbeat).total_seconds() * 1000
    return age_ms > stale_after_ms


#: The API layer reads the same predicate under its own name.
active_operation_is_stalled = operation_is_stalled


def start_operation(directory, *, mapping, reviewer, reason, source_analysis_id,
                    cluster_ids=None):
    """Persist a new operation *before* any work starts and return it.

    ``mapping`` is stored as its own decisions plus a sha256 of the canonical form,
    never as free text, so the record states exactly which decision set this
    operation will apply.
    """
    created_at = utc_now()
    operation_id = 'OP-' + uuid.uuid4().hex
    decisions = dict(sorted(mapping.items()))
    payload = {
        'schema_version': SCHEMA_VERSION,
        'operation_id': operation_id,
        'run_id': Path(directory).name,
        'kind': 'role_review_rebuild',
        'status': 'accepted',
        'phase': 'accepted',
        'phases': list(REBUILD_PHASES),
        'created_at': created_at,
        'started_at': None,
        'finished_at': None,
        'elapsed_ms': None,
        'heartbeat_at': created_at,
        'source_analysis_id': source_analysis_id,
        'cluster_ids': sorted(cluster_ids or decisions),
        'decisions': decisions,
        'decisions_sha256': hashlib.sha256(
            json.dumps(decisions, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'reviewer': reviewer,
        'reason': reason,
        'error': None,
        'result': None,
        'note': ('A role rebuild is long-running; this record is written before any work '
                 'starts so a retry reports this operation instead of submitting a second '
                 'rebuild. Control bookkeeping, not analysis evidence.'),
    }
    _write(operations_dir(directory) / (operation_id + '.json'), payload)
    return payload


#: One writer at a time per operation record. The rebuild worker updates phases and
#: the terminal outcome while its heartbeat thread refreshes ``heartbeat_at``, and
#: both do read-merge-write on the same file, so without this mutex a heartbeat
#: could resurrect a stale phase over a fresher one (Issue #113 review).
_operation_locks = {}
_operation_locks_guard = threading.Lock()


def _operation_lock(directory, operation_id):
    key = (str(directory), operation_id)
    with _operation_locks_guard:
        lock = _operation_locks.get(key)
        if lock is None:
            lock = _operation_locks[key] = threading.Lock()
        return lock


def update_operation(directory, operation_id, **fields):
    """Merge and persist one operation's progress. Unknown ids are refused."""
    with _operation_lock(directory, operation_id):
        path = operations_dir(directory) / (operation_id + '.json')
        if not path.is_file():
            raise ValueError(f'Unknown role-review operation {operation_id}')
        record = _read_record(path)
        record.update(fields)
        if 'heartbeat_at' not in fields:
            # A caller that states the heartbeat explicitly (a recovery tool, a test
            # reproducing a dead worker) must be able to keep it.
            record['heartbeat_at'] = utc_now()
        if record.get('status') in TERMINAL_STATUSES:
            record['finished_at'] = record.get('finished_at') or utc_now()
        record['elapsed_ms'] = _elapsed_ms(record.get('started_at') or record.get('created_at'),
                                           record.get('finished_at'))
        _write(path, record)
        return record


def touch_operation(directory, operation_id):
    """Refresh only ``heartbeat_at`` of an active operation, changing nothing else.

    The rebuild's heartbeat thread calls this while a stage runs so a *long* stage
    (a Judge call can take minutes) is never indistinguishable from a dead worker.
    A terminal record is never touched: a heartbeat that races the worker's final
    write must not resurrect or alter a recorded outcome.
    """
    with _operation_lock(directory, operation_id):
        path = operations_dir(directory) / (operation_id + '.json')
        if not path.is_file():
            return None
        record = _read_record(path)
        if record.get('status') in TERMINAL_STATUSES:
            return record
        record['heartbeat_at'] = utc_now()
        _write(path, record)
        return record


def heartbeat_interval_s(stale_after_ms):
    """How often the rebuild's heartbeat ticks, derived from the stale bound.

    A tick must land well inside the window that decides ``stalled``: a quarter of
    the bound, clamped to a sane floor so tiny bounds cannot spin and to a ceiling
    that keeps real deployments quiet.
    """
    return min(30.0, max(0.2, stale_after_ms / 4000.0))


def heartbeat_loop(directory, operation_id, interval_s, stop_event):
    """Tick :func:`touch_operation` until ``stop_event`` is set.

    Run on a daemon thread owned by the rebuild worker, so the heartbeat lives and
    dies with the worker itself: a record that stops heartbeating means the worker
    (or its process) is gone, which is exactly what ``stalled`` must mean.
    """
    while not stop_event.wait(interval_s):
        try:
            touch_operation(directory, operation_id)
        except Exception:  # noqa: BLE001 - a heartbeat must never kill a rebuild
            continue


def error_payload(code, message=None, detail=None):
    """One machine-readable failure, always with a code the caller can branch on.

    An unknown code is reported as ``internal_error`` rather than being echoed:
    the vocabulary is closed, so a caller can branch on it exhaustively.
    """
    resolved = code if code in ERROR_CODES else 'internal_error'
    known = ERROR_CODES[resolved]
    payload = {'code': resolved, 'message': message or known['message'],
               'retryable': known['retryable'], 'http_status': known['http_status']}
    if detail:
        payload['detail'] = detail
    return payload


def view(record, *, stale_after_ms=None):
    """The public, progress-only view of one operation record.

    Derived facts are computed here so every reader (the API, the CLI and the
    tests) sees the same progress semantics. ``stalled`` is a *pending* state, not
    a failure: a rebuild whose process stopped heartbeating is still the operation
    a retry must report until it is explicitly resubmitted.
    """
    if record is None:
        return None
    status = record.get('status')
    current = record.get('phase')
    phases = list(record.get('phases') or [])
    if current == 'done':
        # A terminal marker, not a pipeline phase: every phase completed.
        index = len(phases)
    else:
        index = phases.index(current) if current in phases else -1
    stale = operation_is_stalled(record, stale_after_ms) if stale_after_ms else False
    return {
        'schema_version': record.get('schema_version'),
        'operation_id': record.get('operation_id'),
        'run_id': record.get('run_id'),
        'kind': record.get('kind'),
        'status': status,
        'phase': current,
        'phase_index': index,
        'phase_count': len(phases),
        'phases': phases,
        'completed_phases': phases[:index] if index >= 0 else [],
        'stalled': stale,
        'elapsed_ms': record.get('elapsed_ms'),
        'created_at': record.get('created_at'),
        'started_at': record.get('started_at'),
        'finished_at': record.get('finished_at'),
        'heartbeat_at': record.get('heartbeat_at'),
        'source_analysis_id': record.get('source_analysis_id'),
        'decisions_sha256': record.get('decisions_sha256'),
        'reviewer': record.get('reviewer'),
        'error': record.get('error'),
        'result': record.get('result'),
        'note': record.get('note'),
    }


def mark_interrupted(directory, operation_id, message=None):
    """Close an operation whose worker is gone, without claiming its outcome.

    Used only when the record is still active but its heartbeat is older than the
    stale bound: the rebuild stopped for a reason this process cannot observe, so
    it is reported as ``interrupted`` (retryable) rather than as a success or as a
    processor failure it did not record.
    """
    return update_operation(
        directory, operation_id, status='failed',
        error=error_payload('interrupted', message),
        note='no terminal result was recorded for this rebuild')
