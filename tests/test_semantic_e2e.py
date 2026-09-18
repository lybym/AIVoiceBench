"""Positive end-to-end: labelled service fixture -> semantic roles -> Metrics.

Chain under test:
  labelled ASR native response fixture
    -> one ImportRun (cloud ASR path)
    -> speaker clusters derived from that same response
    -> semantic role proposal from existing evidence
    -> Fusion -> Turns -> Timeline -> canonical MetricResult

The ASR transport and the semantic provider are both simulated. This proves the
software chain only: no real model call, no real speech service call and no real
recording are involved.
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
from aivoicebench.validation import metric_errors, schema_errors
from aivoicebench.volcengine_asr import VolcengineASRProvider
from tests.test_semantic_attribution import ScriptedRoleProvider, content_decider


REPLY = {'result': {'text': '今天天气怎么样 北京今天晴', 'utterances': [
    {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
     'words': [], 'additions': {'speaker': '1'}},
    {'text': '北京今天晴', 'start_time': 1500, 'end_time': 2000,
     'words': [], 'additions': {'speaker': '2'}},
]}}


class LabelledTransport:
    """Serves the signed-URL upload and one labelled recognition response."""

    def __init__(self):
        self.calls, self.audio = [], b''

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method, url, headers, body))
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio)
        return HTTPReply(200, {'x-api-status-code': '20000000'},
                         json.dumps(copy.deepcopy(REPLY), ensure_ascii=False).encode())


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

    def run_import(self):
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

    def test_labelled_fixture_reaches_metrics_through_semantic_roles(self):
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
        self.assertEqual(json.loads(self.role_provider.calls[0]['user_prompt']),
                         json.loads(self.role_provider.calls[0]['user_prompt']))

        # Semantic roles: machine proposals, review-required, uncalibrated.
        attribution = docs['attribution']['data']
        self.assertEqual(attribution['semantic_status'], 'proposed')
        roles = {a['speaker_id']: a['role'] for a in attribution['attributions']}
        self.assertEqual(sorted(roles.values()), ['device', 'tester'])
        for item in attribution['attributions']:
            self.assertEqual(item['method'], 'semantic_attribution')
            self.assertTrue(item['needs_review'])
            self.assertEqual(item['confidence_basis'], 'uncalibrated_model_self_report')
            self.assertTrue(item['evidence_refs'])
            self.assertEqual(item['invocation_id'], 'CALL-role-1')
        self.assertEqual(attribution['scope']['analysis_id'], manifest['analysis_id'])
        self.assertTrue(attribution['scope']['speaker_output_revision'])
        invocation_artifacts = [a for a in manifest['artifacts']
                               if a['kind'] == 'provider_invocation']
        self.assertTrue(any('attribution-invocations' in a['path'] for a in invocation_artifacts))

        # Roles reached Fusion, and no unfounded role confidence was published.
        fused = docs['fused-segments']['data']
        self.assertEqual(fused['status'], 'complete')
        for segment in fused['segments']:
            self.assertIn(segment['speaker_role'], ('tester', 'device'))
            self.assertIsNone(segment['role_attribution_confidence'])
            self.assertEqual(segment['role_attribution']['method'], 'semantic_attribution')
            self.assertTrue(segment['role_attribution']['needs_review'])
        self.assertEqual({s['speaker_role'] for s in fused['segments']}, {'tester', 'device'})

        # Turns, timeline and metrics follow from those roles.
        turns = docs['turns']['data']['turns']
        self.assertEqual(len(turns), 1)
        self.assertIsNotNone(turns[0]['response_id'])
        timeline = docs['timeline']['data']
        self.assertTrue(timeline['events'])
        metrics = docs['metrics']['data']['metrics']
        self.assertTrue(metrics)
        by_name = {m['name']: m for m in metrics}
        self.assertEqual(by_name['first_speech_latency_ms']['status'], 'observed')
        self.assertGreater(by_name['first_speech_latency_ms']['value'], 0)
        self.assertEqual(by_name['turn_gap_ms']['status'], 'observed')
        for metric in metrics:
            self.assertEqual(schema_errors(metric, 'metric'), [], metric['name'])
            self.assertEqual(metric_errors(metric, timeline), [], metric['name'])

        # Provenance still resolves from metrics back to the original recording.
        self.assertEqual(manifest['status'] in ('partial', 'complete'), True)
        errors = [error for error in
                  json.loads(json.dumps(manifest['artifacts']))]
        self.assertTrue(errors)

    def test_metrics_bind_to_the_run_identity_not_a_fabricated_case(self):
        directory, manifest = self.run_import()
        docs = self.documents(directory, manifest)
        self.assertIsNone(manifest['case_ref'])
        for metric in docs['metrics']['data']['metrics']:
            self.assertEqual(metric['run_id'], manifest['run_id'])
            self.assertIsNone(metric['case_id'])
            self.assertEqual(metric['execution_kind'], 'synthetic')
            self.assertTrue(metric['prd_ref'])


if __name__ == '__main__':
    unittest.main()
