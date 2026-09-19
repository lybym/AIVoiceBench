"""Shared optional-runtime gate for the Silero VAD tests (Issue #23).

Both Silero test modules need the same policy: an absent optional VAD runtime is
a skip by default, and a hard failure when ``AIVOICEBENCH_REQUIRE_SILERO_TESTS=1``.
CI sets that variable so "the model gate was skipped" can never be reported as a
pass.
"""

import os
import unittest

REQUIRE_SILERO = os.environ.get('AIVOICEBENCH_REQUIRE_SILERO_TESTS', '').strip() not in (
    '', '0', 'false', 'False')

#: Why the runtime is unusable, or ``None`` when it loads.
UNAVAILABLE = None
try:
    from aivoicebench.silero_vad import SileroVadSegmenter, inspect_model  # noqa: F401
    inspect_model()
except Exception as error:  # noqa: BLE001 - any load failure is a skip/raise reason
    UNAVAILABLE = f'{type(error).__name__}: {error}'


def require_available():
    """Raise ``SkipTest`` (or ``RuntimeError`` under the gate) when unavailable."""
    if not UNAVAILABLE:
        return
    message = f'Silero VAD runtime unavailable: {UNAVAILABLE}'
    if REQUIRE_SILERO:
        raise RuntimeError(f'Silero VAD gate required but unavailable: {UNAVAILABLE}')
    raise unittest.SkipTest(message)
