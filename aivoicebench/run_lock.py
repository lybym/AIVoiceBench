"""Cross-process locks released by the OS after a crash; not evidence artifacts."""
from contextlib import contextmanager
import sqlite3
from pathlib import Path


@contextmanager
def run_lock(directory):
    directory = Path(directory).resolve()
    locks = directory.parent / '.run-locks'
    locks.mkdir(exist_ok=True)
    db = sqlite3.connect(locks / (directory.name + '.sqlite3'), timeout=0)
    try:
        db.execute('BEGIN EXCLUSIVE')
        yield
    finally:
        db.close()
