"""Cross-process locks released by the OS after a crash; not evidence artifacts."""
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path


class RunLocked(RuntimeError):
    """Another analysis is already writing this Run.

    Raised instead of a bare ``sqlite3.OperationalError`` so a caller can tell
    "someone else holds the Run" apart from any other storage failure. Both are
    real, but they need different user-facing answers: one is retryable and one
    is not (Issue #113).
    """


def _begin_exclusive(db, attempts=10, pause_s=0.02):
    """Take the exclusive lock, retrying a transient loss of the race.

    ``timeout=0`` makes a genuinely held lock fail immediately, which is the
    designed fast answer — but on Windows the *first* contact with a freshly
    created lock database can also be denied outright while an external scanner
    holds the new file, and then a two-thread race loses on both sides with nobody
    holding anything. A short bounded retry (about 0.2 s) re-attempts only that
    window; a lock that is genuinely held for its whole analysis still answers
    ``RunLocked`` immediately after the retries.
    """
    last_error = None
    for _ in range(attempts):
        try:
            db.execute('BEGIN EXCLUSIVE')
            return
        except sqlite3.OperationalError as error:
            last_error = error
            time.sleep(pause_s)
    raise last_error


@contextmanager
def run_lock(directory):
    directory = Path(directory).resolve()
    locks = directory.parent / '.run-locks'
    locks.mkdir(exist_ok=True)
    db = sqlite3.connect(locks / (directory.name + '.sqlite3'), timeout=0)
    try:
        try:
            _begin_exclusive(db)
        except sqlite3.OperationalError as error:
            raise RunLocked('Another analysis is already writing this Run') from error
        yield
    finally:
        db.close()


@contextmanager
def control_lock(directory):
    """Serialize one Run's *control-plane* bookkeeping across processes.

    Separate from :func:`run_lock` on purpose. The Run lock is held for the whole
    duration of a long analysis, and the operation record that says "a rebuild is
    already accepted" is written by the accepting request, long before that lock is
    taken. A check and its write therefore need their own mutex, or two concurrent
    saves can both find no active operation and both record one (Issue #113).

    Held only for the bookkeeping itself — never across the rebuild — so it can
    never be confused with the Run lock by a caller or a test.
    """
    directory = Path(directory).resolve()
    locks = directory.parent / '.run-locks'
    locks.mkdir(exist_ok=True)
    db = sqlite3.connect(locks / (directory.name + '.control.sqlite3'), timeout=0)
    try:
        try:
            _begin_exclusive(db)
        except sqlite3.OperationalError as error:
            raise RunLocked('Another role-review save is being recorded for this Run') from error
        yield
    finally:
        db.close()
