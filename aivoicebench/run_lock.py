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
