"""Shared fixture: a real Run whose ASR returned two anonymous speaker clusters.

Used by the API/persistence tests and the browser test so both exercise the same
Run shape. Recognition and diarization are scripted and synthetic: this proves the
gate and the surfaces, never real recognition quality.
"""

import json
import math
import sys
import time
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

REPLY = {'result': {'text': '今天天气怎么样 北京今天晴', 'utterances': [
    {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
     'words': [], 'additions': {'speaker': '1'}},
    {'text': '北京今天晴', 'start_time': 1500, 'end_time': 2000,
     'words': [], 'additions': {'speaker': '2'}},
]}}

#: Contract constant: the API accepts a save with 202 and an operation id.
ROLE_REVIEW_ACCEPTED = 202


def wait_for_operation(client, poll_url, *, timeout=60.0, interval=0.05):
    """Follow one accepted role-review operation until it reaches a terminal state.

    Mirrors what the page does, so the tests exercise the real asynchronous
    contract instead of a synchronous shortcut that no longer exists (Issue #113).
    """
    deadline = time.monotonic() + timeout
    operation = None
    while True:
        response = client.get(poll_url)
        assert response.status_code == 200, response.text
        operation = response.json()
        if operation['status'] in ('succeeded', 'failed'):
            return operation
        if time.monotonic() >= deadline:
            raise AssertionError(f'role rebuild did not finish in {timeout}s: {operation}')
        time.sleep(interval)


def save_role_review(client, run_id, payload, *, timeout=60.0, interval=0.05):
    """Save one role decision and follow the accepted rebuild to its outcome.

    Returns ``(post_response, operation, run_view)``. A request the API refuses
    outright returns its own response with ``operation`` and ``run_view`` left as
    ``None``, so a caller can assert on the refusal code.
    """
    post = client.post(f'/api/runs/{run_id}/role-review', json=payload)
    if post.status_code != ROLE_REVIEW_ACCEPTED:
        return post, None, None
    accepted = post.json()
    operation = wait_for_operation(client, accepted['poll_url'], timeout=timeout,
                                   interval=interval)
    view = client.get(f'/api/runs/{run_id}').json()
    return post, operation, view


class ScriptedTransport:
    """Inline File ASR transport: one POST, no object storage, no cloud."""

    def __init__(self, reply=None):
        self.reply = REPLY if reply is None else reply
        self.calls = []
        self.audio = b''

    def request(self, method, url, headers, body=None, **kwargs):
        from aivoicebench.cloud_transport import HTTPReply
        self.calls.append((method, url, headers, body))
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio)
        return HTTPReply(200, {'x-api-status-code': '20000000'},
                         json.dumps(self.reply, ensure_ascii=False).encode())


def write_dialogue_wav(path):
    """Three tone bursts separated by silence, so VAD finds three speech segments."""
    samples = []
    for value in ([0] * 4000 + [18000] * 6000 + [0] * 4000
                  + [18000] * 6000 + [0] * 4000 + [18000] * 6000 + [0] * 4000):
        samples.append(int(18000 * math.sin(value)) if value else 0)
    data = array('h', samples)
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(data.tobytes())


def build_anonymous_run(root):
    """Import one synthetic recording that yields two anonymous clusters.

    Returns ``(runs_dir, run_id, transport)``. No provider credential is used and
    the transport is scripted, so no network request leaves the process.
    """
    from aivoicebench.cloud_transport import API
    from aivoicebench.diarization import ASRNativeDiarizationProvider
    from aivoicebench.import_pipeline import import_recording
    from aivoicebench.model_settings import RunProviders
    from aivoicebench.volcengine_asr import VolcengineASRProvider

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    runs = root / 'runs'
    source = root / 'dialogue.wav'
    write_dialogue_wav(source)
    transport = ScriptedTransport()
    providers = RunProviders(
        asr=lambda directory: VolcengineASRProvider(
            directory, 'synthetic-key', endpoint=API,
            transport_config={'audio_transport': 'inline', 'inline_max_bytes': 1_000_000}),
        diarization=lambda directory: ASRNativeDiarizationProvider())
    with patch('aivoicebench.cloud_transport.HTTPTransport.request',
               side_effect=transport.request):
        directory, manifest = import_recording(source, runs, synthetic=True, providers=providers)
    return runs, directory.name, transport
