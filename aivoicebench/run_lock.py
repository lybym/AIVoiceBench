"""Cross-process locks released by the OS after a crash; not evidence artifacts."""
from contextlib import contextmanager
import sqlite3
from pathlib import Path


class RunLocked(RuntimeError):
    """Another analysis is already writing this Run.

    Raised instead of a bare ``sqlite3.OperationalError`` so a caller can tell
    "someone else holds the Run" apart from any other storage failure. Both are
    real, but they need different user-facing answers: one is retryable and one
    is not (Issue #113).
    """


@contextmanager
def run_lock(directory):
    directory = Path(directory).resolve()
    locks = directory.parent / '.run-locks'
    locks.mkdir(exist_ok=True)
    db = sqlite3.connect(locks / (directory.name + '.sqlite3'), timeout=0)
    try:
        try:
            db.execute('BEGIN EXCLUSIVE')
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
            db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as error:
            raise RunLocked('Another role-review save is being recorded for this Run') from error
        yield
    finally:
        db.close()
