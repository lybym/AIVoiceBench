"""Recording Analysis runs the configured Judge and produces evidence-linked Findings.

This is the Issue #10 integration proof: the semantic stage runs inside the
Recording Analysis import chain, its output is validated before it becomes
metrics or findings input, and a Run without a configured Judge says so instead
of inventing a semantic value.

The fixture simulates the cloud ASR transport only. There is no real model call,
no real recording and no hardware: the semantic provider is the software fixture
`MockLLMProvider`, and the tests say so. Roles are confirmed through the real
product path — anonymous clusters first, then one saved human decision set that
triggers a role reanalysis.
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

from aivoicebench.cloud_transport import HTTPReply
from aivoicebench.diarization import ASRNativeDiarizationProvider
from aivoicebench.import_artifacts import recording_run_errors
from aivoicebench.import_pipeline import apply_role_mapping, import_recording
from aivoicebench.llm import MockLLMProvider
from aivoicebench.model_settings import RunProviders
from aivoicebench.semantic_evidence import judge_profile_id
from aivoicebench.validation import finding_errors, judge_document_errors
from aivoicebench.volcengine_asr import VolcengineASRProvider


# The device answers 2.5 s after the tester stops, so the fixture latency metric
# exceeds the fixture finding threshold and a candidate really appears.
REPLY = {'result': {'text': '今天天气怎么样 嗯好的北京今天晴', 'utterances': [
    {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
     'words': [], 'additions': {'speaker': '1'}},
    {'text': '嗯好的北京今天晴', 'start_time': 3500, 'end_time': 4000,
     'words': [], 'additions': {'speaker': '2'}},
]}}


class FixtureTransport:
    """Serves the signed-URL upload and one labelled recognition response."""

    def __init__(self):
        self.calls, self.audio = [], b''

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append(method)
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio)
        return HTTPReply(200, {'x-api-status-code': '20000000'},
                         json.dumps(copy.deepcopy(REPLY), ensure_ascii=False).encode())


def write_wav(path, rate=16000):
    """Tester burst, a long pause, then a device burst; VAD finds two segments."""
    samples = []
    for value in ([0] * int(0.5 * rate) + [18000] * int(0.5 * rate)
                  + [0] * int(2.5 * rate) + [18000] * int(0.5 * rate)
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
class JudgeImportStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'dialogue.wav'
        write_wav(self.source)
        env = patch.dict('os.environ', {
            'AIVOICEBENCH_AUDIO_PUT_URL': 'https://storage.example/audio?put=1',
            'AIVOICEBENCH_AUDIO_GET_URL': 'https://storage.example/audio?get=1',
            'AIVOICEBENCH_AUDIO_HOST': 'storage.example'})
        env.start()
        self.addCleanup(env.stop)

    def import_once(self, judge):
        """Import once with anonymous clusters, exactly as the product does first."""
        transport = FixtureTransport()
        providers = RunProviders(
            asr=lambda root: VolcengineASRProvider(
                root, 'synthetic-key',
                transport_config={'audio_transport': 'object_storage',
                                  'inline_max_bytes': 15728640}),
            diarization=lambda root: ASRNativeDiarizationProvider(),
            judge=judge)
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=transport.request):
            return import_recording(self.source, self.root / 'runs', synthetic=True,
                                    providers=providers)

    def analysis_dir(self, directory, manifest):
        return directory / 'analysis' / manifest['analysis_id']

    def analysis_files(self, directory, manifest):
        root = self.analysis_dir(directory, manifest)
        return {path.name: json.loads(path.read_text(encoding='utf-8'))
                for path in root.glob('*.json')}

    def confirm_roles(self, directory, decisions_by_native_label, providers):
        """Apply one saved human decision set through the real reanalysis path."""
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        diarization = self.analysis_files(directory, manifest)['speaker-assignments.json']['data']
        decisions = {}
        for segment in diarization['speaker_segments']:
            native = segment['native_speaker_id']
            if native in decisions_by_native_label:
                decisions[segment['speaker_id']] = decisions_by_native_label[native]
        return apply_role_mapping(directory, decisions, 'fixture-reviewer',
                                  reason='fixture: explicit human role decision',
                                  providers=providers)

    def test_role_confirmation_runs_the_judge_and_links_findings(self):
        judge = MockLLMProvider()
        providers = RunProviders(judge=judge)
        directory, manifest = self.import_once(judge)

        # Before the human decision, the semantic stage has not run at all — and the
        # Run says so instead of leaving the stage looking unimplemented.
        self.assertEqual(manifest['stages']['turns']['status'], 'insufficient_evidence')
        self.assertEqual(manifest['stages']['judge']['status'], 'insufficient_evidence')
        self.assertIn('Anonymous speaker clusters', manifest['stages']['judge']['reason'])
        self.assertEqual(judge.invocations if hasattr(judge, 'invocations') else [], [])

        directory, manifest, _review = self.confirm_roles(
            directory, {'1': 'tester', '2': 'device'}, providers)
        files = self.analysis_files(directory, manifest)
        stages = manifest['stages']

        # The semantic stages ran inside the Recording Analysis revision.
        self.assertEqual(stages['turns']['status'], 'complete')
        self.assertEqual(stages['timeline']['status'], 'complete')
        self.assertEqual(stages['judge']['status'], 'complete')
        for name in ('judge-results.json', 'judge-document.json', 'judge-raw.json',
                     'findings.json'):
            self.assertIn(name, files)

        judge_document = files['judge-document.json']
        timeline = files['timeline.json']['data']
        turns = files['turns.json']['data']
        self.assertEqual(judge_document_errors(judge_document, timeline, turns), [],
                         'the Run published a Judge artifact that breaks its own contract')
        # Raw provider output is preserved next to the validated results.
        self.assertTrue(judge_document['invocations'])
        successful = [item for item in judge_document['invocations']
                      if item['status'] == 'success']
        self.assertTrue(successful)
        for invocation in successful:
            self.assertIsNotNone(invocation['raw_response'])
            self.assertEqual(len(invocation['raw_response_sha256']), 64)

        # The semantic metric is a real decision backed by constrained evidence.
        metrics = files['metrics.json']['data']['metrics']
        semantic = next(metric for metric in metrics if metric['name'] == 'semantic_response')
        self.assertEqual(semantic['prd_ref'], 'PRD-M003')
        self.assertEqual(semantic['method'], 'llm_judge')
        self.assertEqual(semantic['confidence_source'], 'semantic_event')
        profile = judge_document['judge_profile']
        self.assertEqual(semantic['judge_profile'],
                         judge_profile_id(profile['provider'], profile['model'],
                                          profile['criteria_version']))
        self.assertEqual(semantic['status'], 'observed')
        self.assertIsInstance(semantic['value'], bool)
        self.assertTrue(semantic['evidence_ids'])

        # Findings exist and resolve against the same Timeline and metrics.
        findings = files['findings.json']['data']['findings']
        self.assertTrue(findings, 'the fixture latency candidate produced no Finding')
        for finding in findings:
            self.assertEqual(finding['schema_version'], '2.1.0')
            self.assertEqual(finding_errors(finding, timeline, metrics), [])
            self.assertTrue(finding['turn_ids'])
            self.assertTrue(finding['evidence_ids'])
            self.assertTrue(finding['metric_ids'])
            self.assertTrue(finding['requires_log_verification'])
        self.assertIn('high_latency', {finding['title'] for finding in findings})

    def test_unconfigured_judge_abstains_without_inventing_a_verdict(self):
        directory, manifest = self.import_once(None)
        directory, manifest, _review = self.confirm_roles(
            directory, {'1': 'tester', '2': 'device'}, RunProviders())
        files = self.analysis_files(directory, manifest)
        stages = manifest['stages']

        # A configured-but-absent Judge is an evidence state, not an unfinished
        # stage: the envelope is authoritative and carries the exact reason.
        self.assertEqual(files['judge-results.json']['status'], 'insufficient_evidence')
        self.assertIn('No semantic Judge provider is configured', files['judge-results.json']['reason'])
        self.assertEqual(files['findings.json']['status'], 'insufficient_evidence')
        self.assertIn('No accepted semantic judgment exists', files['findings.json']['reason'])
        self.assertIn(stages['judge']['status'], ('partial', 'insufficient_evidence'))
        self.assertNotIn('judge-document.json', files)

        # The semantic metric abstains rather than reporting a fabricated value,
        # while the deterministic metrics still report what they measured.
        metrics = files['metrics.json']['data']['metrics']
        semantic = next(metric for metric in metrics if metric['name'] == 'semantic_response')
        self.assertEqual(semantic['status'], 'insufficient_evidence')
        self.assertIsNone(semantic['value'])
        self.assertIn('Constrained semantic evidence is required', semantic['reason'])
        self.assertTrue(any(metric['status'] == 'observed' for metric in metrics))
        self.assertEqual(files['findings.json']['status'], 'insufficient_evidence')

    def test_an_invalid_judge_artifact_is_stopped_and_recorded(self):
        """The engine validates its own output instead of publishing it."""
        import aivoicebench.llm as llm_module
        original = llm_module.LLMJudge.evaluate

        def broken(self, fused_doc, turns_doc, metrics_result, timeline=None,
                   run_id=None, analysis_id=None):
            document = original(self, fused_doc, turns_doc, metrics_result, timeline,
                                run_id, analysis_id)
            document['results'][0]['evidence_refs'] = ['EVD-DOES-NOT-EXIST']
            return document

        judge = MockLLMProvider()
        directory, manifest = self.import_once(judge)
        llm_module.LLMJudge.evaluate = broken
        try:
            directory, manifest, _review = self.confirm_roles(
                directory, {'1': 'tester', '2': 'device'}, RunProviders(judge=judge))
        finally:
            llm_module.LLMJudge.evaluate = original

        stage = manifest['stages']['judge']
        self.assertEqual(stage['status'], 'failed')
        files = self.analysis_files(directory, manifest)
        # The findings *envelope* still publishes the abstention (correct); what
        # must not exist is a Finding payload derived from a rejected judgment.
        self.assertNotIn('findings-document.json', files)
        self.assertEqual(files['findings.json']['status'], 'insufficient_evidence')
        # `run.execute` reduces an unknown exception to its type, so the guard
        # retains the exact rejection reason as a diagnostic instead of losing it.
        retained = [item for item in manifest['artifacts']
                    if item['kind'] == 'retained_diagnostic'
                    and 'contract-violation' in item['path']]
        self.assertTrue(retained, 'the guard reason was not retained for diagnosis')
        violation = json.loads(
            (directory / retained[0]['path']).read_text(encoding='utf-8'))
        self.assertEqual(violation['status'], 'failed')
        self.assertTrue(any('unknown timeline evidence' in error
                            for error in violation['errors']))
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def test_run_without_confirmed_roles_never_reaches_the_judge(self):
        """Anonymous clusters still wait for a human decision, not a model."""
        provider = _CountingProvider()
        directory, manifest = self.import_once(provider)
        stages = manifest['stages']
        self.assertEqual(stages['turns']['status'], 'insufficient_evidence')
        self.assertEqual(provider.calls, 0)
        self.assertEqual(stages['judge']['status'], 'insufficient_evidence')
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def test_a_further_revision_leaves_the_earlier_judge_artifacts_untouched(self):
        """Repeat-run idempotency: a new revision never rewrites older evidence."""
        judge = MockLLMProvider()
        providers = RunProviders(judge=judge)
        directory, manifest = self.import_once(judge)
        directory, first_manifest, _review = self.confirm_roles(
            directory, {'1': 'tester', '2': 'device'}, providers)
        first_analysis = first_manifest['analysis_id']
        before = {item['path']: item['sha256'] for item in first_manifest['artifacts']
                  if first_analysis in item['path']
                  and (item['path'].endswith('judge-document.json')
                       or item['path'].endswith('judge-raw.json')
                       or item['path'].endswith('findings-document.json')
                       or item['path'].endswith('metrics.json'))}
        self.assertTrue(before, 'the first revision produced no judge/findings artifacts')

        # A second, different human decision set creates another revision.
        directory, second_manifest, _review = self.confirm_roles(
            directory, {'1': 'device', '2': 'tester'}, providers)
        self.assertNotEqual(second_manifest['analysis_id'], first_analysis)
        after = {item['path']: item['sha256'] for item in second_manifest['artifacts']}
        for path, digest in before.items():
            self.assertIn(path, after, 'an earlier revision artifact disappeared')
            self.assertEqual(after[path], digest,
                             f'{path} changed after a further revision')
        # The new revision has its own judge artifacts, and both judge the same Run.
        self.assertTrue(any(second_manifest['analysis_id'] in path
                            and path.endswith('judge-document.json') for path in after))
        self.assertEqual(recording_run_errors(second_manifest, directory), [])

    def test_a_failed_judge_stage_is_recovered_by_a_retry(self):
        """Checkpoint lifecycle: a failed Judge leaves no half-written envelope."""
        import aivoicebench.llm as llm_module
        original = llm_module.LLMJudge.evaluate
        judge = MockLLMProvider()
        providers = RunProviders(judge=judge)
        directory, manifest = self.import_once(judge)

        def broken(self, fused_doc, turns_doc, metrics_result, timeline=None,
                   run_id=None, analysis_id=None):
            document = original(self, fused_doc, turns_doc, metrics_result, timeline,
                                run_id, analysis_id)
            document['results'][0]['event_refs'] = ['EVT-MISSING']
            return document

        llm_module.LLMJudge.evaluate = broken
        try:
            directory, failed_manifest, _review = self.confirm_roles(
                directory, {'1': 'tester', '2': 'device'}, providers)
        finally:
            llm_module.LLMJudge.evaluate = original

        self.assertEqual(failed_manifest['stages']['judge']['status'], 'failed')
        failed_analysis = self.analysis_files(directory, failed_manifest)
        # No half-written evidence: the envelope is absent, not present-but-empty.
        self.assertNotIn('judge-results.json', failed_analysis)
        self.assertEqual(recording_run_errors(failed_manifest, directory), [])

        # Retrying with a working Judge completes the stage in a new revision.
        directory, recovered_manifest, _review = self.confirm_roles(
            directory, {'1': 'tester', '2': 'device'}, providers)
        self.assertEqual(recovered_manifest['stages']['judge']['status'], 'complete')
        recovered = self.analysis_files(directory, recovered_manifest)
        self.assertIn('judge-document.json', recovered)
        self.assertEqual(
            judge_document_errors(recovered['judge-document.json'],
                                  recovered['timeline.json']['data'],
                                  recovered['turns.json']['data']), [])
        self.assertEqual(recording_run_errors(recovered_manifest, directory), [])


class _CountingProvider(MockLLMProvider):
    """Fixture that records whether the Judge was invoked at all."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def complete(self, system_prompt, user_prompt, dimension, context):
        self.calls += 1
        return super().complete(system_prompt, user_prompt, dimension, context)


if __name__ == '__main__':
    unittest.main()