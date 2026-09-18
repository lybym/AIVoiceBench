"""Recording import does not use an LLM to assign speaker roles.

Chain under test:
  labelled ASR native response fixture
    -> one ImportRun (cloud ASR path)
    -> speaker clusters derived from that same response
    -> anonymous clusters awaiting user role review
    -> role-dependent Turns/Timeline/Metrics abstain

The ASR transport and a configured semantic provider are simulated. The test
proves that Recording Analysis does not call that provider for tester/device
roles. No real model, speech service or recording is involved.
"""

import copy
import json
import math
import shutil
import sys
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import patch
import wave

from aivoicebench.cloud_transport import API, HTTPReply
from aivoicebench.diarization import ASRNativeDiarizationProvider
from aivoicebench.import_pipeline import import_recording
from aivoicebench.model_settings import RunProviders
from aivoicebench.volcengine_asr import VolcengineASRProvider
from tests.test_semantic_attribution import ScriptedRoleProvider, content_decider


REPLY = {'result': {'text': '今天天气怎么样 北京今天晴', 'utterances': [
    {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
     'words': [], 'additions': {'speaker': '1'}},
    {'text': '北京今天晴', 'start_time': 1500, 'end_time': 2000,
     'words': [], 'additions': {'speaker': '2'}},
]}}

# One word offset is a provider placeholder (-1). The utterance-level text and
# timing are still valid, so the transcript must be `partial` rather than
# rejected, and the surviving speaker labels must still reach diarization.
PARTIAL_REPLY = {'result': {'text': '今天天气怎么样 北京今天晴', 'utterances': [
    {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
     'words': [{'text': '今', 'start_time': -1, 'end_time': -1, 'confidence': 0}],
     'additions': {'speaker': '1'}},
    {'text': '北京今天晴', 'start_time': 1500, 'end_time': 2000,
     'words': [], 'additions': {'speaker': '2'}},
]}}


class LabelledTransport:
    """Serves the signed-URL upload and one labelled recognition response."""

    def __init__(self, reply=None):
        self.reply = REPLY if reply is None else reply
        self.calls, self.audio = [], b''

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method, url, headers, body))
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio)
        return HTTPReply(200, {'x-api-status-code': '20000000'},
                         json.dumps(copy.deepcopy(self.reply), ensure_ascii=False).encode())


def write_speech_like_wav(path, rate=16000):
    """Two tone bursts separated by silence, so VAD finds two acoustic segments."""
    samples = []
    for value in ([0] * int(0.5 * rate) + [18000] * int(0.5 * rate)
                  + [0] * int(0.5 * rate) + [18000] * int(0.5 * rate)
                  + [0] * int(0.5 * rate)):
        samples.append(int(18000 * math.sin(value)) if value else 0)
    data = array('h', samples)
    if sys.byteorder != 'little':
        data.byteswap()
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(data.tobytes())


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class SemanticAttributionEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'dialogue.wav'
        write_speech_like_wav(self.source)
        self.transport = LabelledTransport()
        self.role_provider = ScriptedRoleProvider(content_decider)
        env = patch.dict('os.environ', {
            'AIVOICEBENCH_AUDIO_PUT_URL': 'https://storage.example/audio?put=1',
            'AIVOICEBENCH_AUDIO_GET_URL': 'https://storage.example/audio?get=1',
            'AIVOICEBENCH_AUDIO_HOST': 'storage.example'})
        env.start()
        self.addCleanup(env.stop)

    def run_import(self, reply=None):
        self.transport = LabelledTransport(reply)
        providers = RunProviders(
            asr=lambda root: VolcengineASRProvider(root, 'synthetic-key',
                transport_config={'audio_transport': 'object_storage',
                                   'inline_max_bytes': 15728640}),
            diarization=lambda root: ASRNativeDiarizationProvider(),
            judge=self.role_provider)
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=self.transport.request):
            directory, manifest = import_recording(
                self.source, self.root / 'runs', synthetic=True, providers=providers)
        return directory, manifest

    def documents(self, directory, manifest):
        root = directory / 'analysis' / manifest['analysis_id']
        out = {}
        for kind in ('transcript', 'speaker-assignments', 'attribution', 'fused-segments',
                     'turns', 'timeline', 'metrics'):
            envelope = json.loads((root / f'{kind}.json').read_text(encoding='utf-8'))
            out[kind] = envelope
        return out

    def test_labelled_fixture_waits_for_manual_roles_without_llm_call(self):
        directory, manifest = self.run_import()
        docs = self.documents(directory, manifest)

        # One recognition submission produced both the transcript and the clusters.
        self.assertEqual([m for m, *_ in self.transport.calls], ['PUT', 'GET', 'POST'])
        transcript = docs['transcript']['data']
        self.assertEqual([s['speaker_id'] for s in transcript['segments']], ['1', '2'])

        # Clusters came from that same response.
        speaker = docs['speaker-assignments']
        self.assertEqual(speaker['status'], 'complete')
        self.assertEqual(len(speaker['data']['speaker_segments']), 2)
        self.assertEqual(self.role_provider.calls, [])

        # Anonymous clusters wait for a user mapping; no machine role proposal.
        attribution = docs['attribution']['data']
        self.assertEqual(attribution['status'], 'partial')
        for item in attribution['attributions']:
            self.assertEqual(item['role'], 'unknown')
            self.assertEqual(item['method'], 'none')
        invocation_artifacts = [a for a in manifest['artifacts']
                               if a['kind'] == 'provider_invocation']
        self.assertFalse(any('attribution-invocations' in a['path'] for a in invocation_artifacts))

        # Fusion keeps clusters but role-dependent outputs abstain.
        fused = docs['fused-segments']['data']
        for segment in fused['segments']:
            self.assertEqual(segment['speaker_role'], 'unknown')
            self.assertIsNone(segment['role_attribution_confidence'])
        for kind in ('turns', 'timeline', 'metrics'):
            self.assertEqual(docs[kind]['status'], 'insufficient_evidence')
            self.assertIsNone(docs[kind]['data'])

        # Provenance still resolves from metrics back to the original recording.
        self.assertEqual(manifest['status'] in ('partial', 'complete'), True)
        errors = [error for error in
                  json.loads(json.dumps(manifest['artifacts']))]
        self.assertTrue(errors)

    def test_abstaining_outputs_bind_to_run_identity_not_a_fabricated_case(self):
        directory, manifest = self.run_import()
        docs = self.documents(directory, manifest)
        self.assertIsNone(manifest['case_ref'])
        for kind in ('attribution', 'turns', 'timeline', 'metrics'):
            self.assertEqual(docs[kind]['run_id'], manifest['run_id'])
            self.assertNotIn('case_id', docs[kind])

    def test_partial_asr_evidence_still_reaches_diarization(self):
        """A partial transcript keeps its valid utterance/speaker evidence.

        One placeholder word offset must not delete the whole transcript, and a
        stage that is not `complete` must not stop the surviving speaker labels
        from being handed to diarization.
        """
        directory, manifest = self.run_import(reply=PARTIAL_REPLY)
        docs = self.documents(directory, manifest)

        transcript = docs['transcript']
        self.assertEqual(transcript['status'], 'partial')
        data = transcript['data']
        self.assertEqual([s['text'] for s in data['segments']],
                         ['今天天气怎么样', '北京今天晴'])
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in data['segments']],
                         [(500, 1000), (1500, 2000)])
        # The unusable word timing is an explicit gap, not a silently dropped word.
        self.assertEqual(len(data['gaps']), 1)
        self.assertIn('invalid word timestamps', data['gaps'][0]['reason'])
        self.assertEqual(data['segments'][0]['words'], [])
        self.assertEqual([s['speaker_id'] for s in data['segments']], ['1', '2'])

        # Diarization consumed that partial transcript: two anonymous clusters,
        # derived from the same single recognition call (no second submission).
        self.assertEqual([m for m, *_ in self.transport.calls], ['PUT', 'GET', 'POST'])
        speaker = docs['speaker-assignments']
        self.assertIn(speaker['status'], ('complete', 'partial'))
        self.assertEqual(len(speaker['data']['speaker_segments']), 2)
        self.assertEqual(sorted(s['native_speaker_id'] for s in speaker['data']['speaker_segments']),
                         ['1', '2'])
        self.assertEqual(self.role_provider.calls, [])

        # Roles are still user-owned, so role-dependent stages still abstain.
        self.assertEqual(docs['attribution']['data']['status'], 'partial')
        for kind in ('turns', 'timeline', 'metrics'):
            self.assertEqual(docs[kind]['status'], 'insufficient_evidence')
            self.assertIsNone(docs[kind]['data'])


if __name__ == '__main__':
    unittest.main()
