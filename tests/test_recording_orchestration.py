"""Issue #27 integration proof: one staged Recording Analysis chain, one Run model.

This module is the integration acceptance for the M1 orchestration (PRD-F004,
F013–F017, N001–N006). It proves, at the level a reviewer can re-run, that:

* **AC1** one `import`/`POST /api/analyze` drives the *whole* available processor
  chain to an explicit state, with an automatically produced Timeline — no
  hand-authored Timeline and no stage left silently "not run".
* **AC2** a Run with anonymous clusters pauses at `awaiting_role_review`, and a saved
  human mapping resumes the downstream stages in a new AnalysisRevision without a
  second cloud recognition request.
* **AC3** a failure injected at each major stage leaves a persisted Run whose every
  stage has an explicit state, whose earlier revision artifacts are byte-identical,
  and whose report still exists.
* **AC4** reanalysis never repeats a billable submission and keeps the prior
  invocation and native-response evidence.
* **AC5** the Run view is reconstructible from the Run directory alone: a cold copy of
  the directory yields the same revision, stage ledger, evidence links and report.
* **AC6** the CLI and the HTTP API report the same Run/revision/status semantics for
  the same persisted data, because both render one projection
  (`aivoicebench.run_view`).
* **AC7** the browser workbench the Run view carries is byte-identical to the
  dedicated evidence endpoint, i.e. the integration reuses the #11 projection instead
  of adding a second one.
* **AC8** no long-lived secret reaches the CLI payload, the API payload or the report.

Everything here is synthetic software verification: a scripted transport stands in for
the cloud service, the semantic provider is a fixture, and there is no real recording,
no real provider call and no container. It therefore proves orchestration and recovery
behaviour only, and never `real_recording_verified` — that gate is #85.
"""

import copy
import io
import json
import math
import shutil
import sys
import tempfile
import unittest
import wave
from array import array
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aivoicebench import api
from aivoicebench.__main__ import main as cli_main
from aivoicebench.cloud_transport import HTTPReply
from aivoicebench.diarization import ASRNativeDiarizationProvider, SpeakerSegment
from aivoicebench.import_artifacts import STAGES, recording_run_errors
from aivoicebench.import_pipeline import apply_role_mapping, import_recording
from aivoicebench.llm import MockLLMProvider
from aivoicebench.model_settings import RunProviders
from aivoicebench.runner import digest
from aivoicebench.run_view import read_run_view
from aivoicebench.validation import timeline_errors
from aivoicebench.volcengine_asr import VolcengineASRProvider

SECRET_CANARY = 'synthetic-secret-canary'

# Two labelled utterances: a tester request followed by a device answer.
REPLY = {'result': {'text': '今天天气怎么样 嗯好的北京今天晴', 'utterances': [
    {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
     'words': [], 'additions': {'speaker': '1'}},
    {'text': '嗯好的北京今天晴', 'start_time': 3500, 'end_time': 4000,
     'words': [], 'additions': {'speaker': '2'}}]}}

#: Stages that must have executed once a human decision set is in place. `report` is
#: asserted separately because its envelope is the persisted report itself.
CHAIN_STAGES = ('ingestion', 'normalization', 'audio_qa', 'asr', 'acoustic', 'diarization',
                'attribution', 'fusion', 'turns', 'timeline', 'judge', 'metrics', 'findings')

#: Injecting a failure during `import` exercises the stages a role revision restores
#: instead of rerunning; injecting during the role revision exercises the rest.
IMPORT_INJECTED_STAGES = ('asr', 'acoustic', 'diarization')
REVISION_INJECTED_STAGES = ('attribution', 'fusion', 'turns', 'timeline', 'judge',
                            'metrics', 'findings')

#: The stage ledger name of each processor this module injects a failure into. The
#: processor function name is not always derivable from the stage name.
STAGE_PROCESSORS = {'asr': '_asr', 'acoustic': '_acoustic', 'diarization': '_diarize',
                    'attribution': '_attribute', 'fusion': '_fusion_with_speakers',
                    'turns': '_turns', 'timeline': '_timeline', 'judge': '_judge',
                    'metrics': '_metrics', 'findings': '_findings'}


class FixtureTransport:
    """Serves the signed-URL upload and one labelled recognition response."""

    def __init__(self):
        self.calls, self.audio = [], b''

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method, url))
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio)
        return HTTPReply(200, {'x-api-status-code': '20000000'},
                         json.dumps(copy.deepcopy(REPLY), ensure_ascii=False).encode())

    def recognition_submissions(self):
        return [call for call in self.calls if call[0] == 'POST']


class DisjointSpeakerProvider(ASRNativeDiarizationProvider):
    """Speaker spans that deliberately do not overlap the acoustic segments.

    This reproduces the real #27 diagnostic shape (speaker clusters exist, acoustic
    segments exist, no attributable overlap) without fabricating evidence: the
    provider genuinely returns spans elsewhere on the timeline, so the published
    alignment honestly reports no match.
    """

    OFFSET_MS = 600000.0

    def diarize_from_transcript(self, transcript, **kwargs):
        result = super().diarize_from_transcript(transcript, **kwargs)
        result.speaker_segments = [
            SpeakerSegment(speaker_id=segment.speaker_id,
                           start_ms=segment.start_ms + self.OFFSET_MS,
                           end_ms=segment.end_ms + self.OFFSET_MS,
                           confidence=segment.confidence,
                           native_speaker_id=segment.native_speaker_id,
                           timestamp_source=segment.timestamp_source,
                           evidence_refs=segment.evidence_refs)
            for segment in result.speaker_segments]
        return result


def write_dialogue(path, rate=16000):
    """Tester burst, a long pause, then a device burst; the VAD finds two segments."""
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
class OrchestrationTests(unittest.TestCase):
    """The staged Recording Analysis chain, its gate, its recovery and its surfaces."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'dialogue.wav'
        write_dialogue(self.source)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        env = patch.dict('os.environ', {
            'AIVOICEBENCH_AUDIO_PUT_URL': 'https://storage.example/audio?put=' + SECRET_CANARY,
            'AIVOICEBENCH_AUDIO_GET_URL': 'https://storage.example/audio?get=' + SECRET_CANARY,
            'AIVOICEBENCH_AUDIO_HOST': 'storage.example'})
        env.start()
        self.addCleanup(env.stop)
        root_patch = patch.object(api, 'OUTPUT_ROOT', self.runs)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------ helpers

    def providers(self, *, judge=None, diarization=None):
        return RunProviders(
            asr=lambda root: VolcengineASRProvider(
                root, SECRET_CANARY,
                transport_config={'audio_transport': 'object_storage',
                                  'inline_max_bytes': 15728640}),
            diarization=diarization or (lambda root: ASRNativeDiarizationProvider()),
            judge=judge)

    def import_anonymous(self, *, judge=None, diarization=None):
        """Import once with anonymous clusters, exactly as the product does first."""
        transport = FixtureTransport()
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=transport.request):
            directory, manifest = import_recording(
                self.source, self.runs, synthetic=True,
                providers=self.providers(judge=judge, diarization=diarization))
        return directory, manifest, transport

    def analysis_dir(self, directory, manifest):
        return directory / 'analysis' / manifest['analysis_id']

    def analysis_files(self, directory, manifest):
        return {path.name: json.loads(path.read_text(encoding='utf-8'))
                for path in self.analysis_dir(directory, manifest).glob('*.json')}

    def cluster_decisions(self, directory, manifest, mapping=None):
        diarization = self.analysis_files(directory, manifest)['speaker-assignments.json']['data']
        mapping = mapping or {'1': 'tester', '2': 'device'}
        return {segment['speaker_id']: mapping[segment['native_speaker_id']]
                for segment in diarization['speaker_segments']}

    def confirm_roles(self, directory, mapping=None, *, providers=None):
        """Apply one saved human decision set through the real reanalysis path."""
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        decisions = self.cluster_decisions(directory, manifest, mapping)
        return apply_role_mapping(directory, decisions, 'fixture-reviewer',
                                  reason='fixture: explicit human role decision',
                                  providers=providers if providers is not None
                                  else self.providers(judge=MockLLMProvider()))

    def artifact_hashes(self, directory, manifest):
        return {item['path']: item['sha256'] for item in manifest['artifacts']}

    def assert_explicit_ledger(self, directory, manifest):
        """Every stage must publish a real state, never a silent absence."""
        stages = manifest['stages']
        self.assertEqual(sorted(stages), sorted(STAGES),
                         'every stage of the ledger must be present')
        for name, stage in stages.items():
            self.assertIn(stage['status'],
                          ('complete', 'partial', 'failed', 'insufficient_evidence', 'pending'),
                          f'{name} has an unknown state')
            if name in ('ingestion', 'normalization'):
                continue
            self.assertTrue(stage['reason'] or stage['output_artifact_ids'],
                            f'{name} published neither a reason nor an output')
        self.assertEqual(recording_run_errors(manifest, directory), [])

    def run_cli(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli_main(argv)
        return code, buffer.getvalue()

    # ------------------------------------------------------- AC1: the chain runs

    def test_one_import_drives_every_stage_to_an_explicit_state(self):
        directory, manifest, transport = self.import_anonymous(judge=MockLLMProvider())
        self.assertEqual(len(transport.recognition_submissions()), 1)
        # Anonymous clusters: the gate is explicit and downstream stages abstain with a
        # reason instead of being absent or invented.
        role_review = json.loads(
            (self.analysis_dir(directory, manifest) / 'role-review.json')
            .read_text(encoding='utf-8'))
        self.assertEqual(role_review['status'], 'awaiting_role_review')
        for name in ('turns', 'timeline', 'metrics', 'judge', 'findings'):
            self.assertEqual(manifest['stages'][name]['status'], 'insufficient_evidence')
            self.assertIn('awaiting_role_review', manifest['stages'][name]['reason'])
        self.assert_explicit_ledger(directory, manifest)

        directory, manifest, _ = self.confirm_roles(
            directory, providers=RunProviders(judge=MockLLMProvider()))
        stages = manifest['stages']
        for name in CHAIN_STAGES:
            self.assertIn(stages[name]['status'], ('complete', 'partial'),
                          f'{name} did not execute the full chain: {stages[name]}')
        self.assertEqual(stages['report']['status'], 'complete')
        self.assert_explicit_ledger(directory, manifest)

        # The Timeline is produced by the chain, never authored by hand, and its own
        # evidence graph must validate.
        timeline = self.analysis_files(directory, manifest)['timeline.json']['data']
        self.assertTrue(timeline['events'], 'the chain produced no canonical events')
        self.assertEqual(timeline_errors(timeline), [])

    # ------------------------------------- AC2: the gate and its resume semantics

    def test_the_gate_pauses_and_a_confirmed_mapping_resumes_without_a_second_call(self):
        directory, manifest, transport = self.import_anonymous(judge=MockLLMProvider())
        first_revision = manifest['analysis_id']
        self.assertEqual(manifest['stages']['turns']['status'], 'insufficient_evidence')

        directory, manifest, review = self.confirm_roles(
            directory, providers=RunProviders(judge=MockLLMProvider()))
        self.assertNotEqual(manifest['analysis_id'], first_revision)
        self.assertEqual(review['status'], 'complete_review')
        self.assertEqual(manifest['stages']['turns']['status'], 'complete')
        self.assertEqual(manifest['stages']['timeline']['status'], 'complete')
        # Recognition and clustering are restored, not recomputed: one upload, one
        # recognition submission and no third invocation.
        self.assertEqual(len(transport.recognition_submissions()), 1)
        self.assertEqual(len(list((directory / 'provider-calls').iterdir())), 2)
        self.assert_explicit_ledger(directory, manifest)

    def test_a_confirmed_mapping_that_matches_no_acoustic_span_reports_the_real_cause(self):
        """Confirmed roles + no attributable overlap must not be blamed on the gate."""
        directory, manifest, _ = self.import_anonymous(
            judge=MockLLMProvider(),
            diarization=lambda root: DisjointSpeakerProvider())
        directory, manifest, _ = self.confirm_roles(
            directory, providers=RunProviders(judge=MockLLMProvider()))
        self.assertEqual(manifest['stages']['attribution']['status'], 'complete')
        alignment = json.loads(
            (self.analysis_dir(directory, manifest) / 'alignment.json')
            .read_text(encoding='utf-8'))
        # The mapping confirmed tester/device, so the reason may not claim it did not.
        reason = manifest['stages']['turns']['reason']
        self.assertIn('confirmed', reason)
        self.assertIn('no fused segment carries a confirmed role', reason)
        self.assertNotIn('none is tester/device', reason)
        self.assertIn(alignment['status'], ('partial', 'insufficient_evidence'))
        self.assert_explicit_ledger(directory, manifest)

    def test_a_failed_attribution_names_the_processor_not_the_gate(self):
        """A processor failure and an unfinished human review are different states."""
        import aivoicebench.import_pipeline as pipeline
        directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
        original = pipeline._attribute

        def broken(*args, **kwargs):
            raise RuntimeError('injected attribution failure')

        pipeline._attribute = broken
        try:
            directory, manifest, _ = self.confirm_roles(
                directory, providers=RunProviders(judge=MockLLMProvider()))
        finally:
            pipeline._attribute = original
        self.assertEqual(manifest['stages']['attribution']['status'], 'failed')
        reason = manifest['stages']['turns']['reason']
        self.assertIn('Speaker attribution failed', reason)
        self.assertNotIn('none is tester/device', reason)
        self.assert_explicit_ledger(directory, manifest)

    # ------------------------------------------------- AC3/AC4: failure and retry

    def test_a_failure_during_import_keeps_the_run_its_original_and_its_report(self):
        for target in IMPORT_INJECTED_STAGES:
            with self.subTest(stage=target):
                self._assert_import_stage_failure(target)

    def test_a_failure_in_a_role_revision_keeps_prior_artifacts_and_the_report(self):
        import aivoicebench.import_pipeline as pipeline
        for target in REVISION_INJECTED_STAGES:
            with self.subTest(stage=target):
                directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
                directory, manifest, _ = self.confirm_roles(
                    directory, providers=RunProviders(judge=MockLLMProvider()))
                before = self.artifact_hashes(directory, manifest)

                original = getattr(pipeline, STAGE_PROCESSORS[target])

                def broken(*args, **kwargs):
                    raise RuntimeError('injected ' + target)

                setattr(pipeline, STAGE_PROCESSORS[target], broken)
                try:
                    directory, manifest, _ = self.confirm_roles(
                        directory, providers=RunProviders(judge=MockLLMProvider()))
                finally:
                    setattr(pipeline, STAGE_PROCESSORS[target], original)

                self.assertEqual(manifest['stages'][target]['status'], 'failed',
                                 f'{target} did not record its injected failure')
                self.assert_explicit_ledger(directory, manifest)
                # The report stage still runs: a failed processor never hides the Run.
                self.assertEqual(manifest['stages']['report']['status'], 'complete')
                self.assertTrue((self.analysis_dir(directory, manifest) / 'report.md').is_file())
                # No earlier artifact is destroyed or rewritten.
                after = self.artifact_hashes(directory, manifest)
                for path, sha in before.items():
                    self.assertIn(path, after, f'{path} disappeared after a {target} failure')
                    self.assertEqual(after[path], sha, f'{path} was rewritten')
                    self.assertEqual(digest(directory / path), sha)

    def _assert_import_stage_failure(self, target):
        import aivoicebench.import_pipeline as pipeline
        original = getattr(pipeline, STAGE_PROCESSORS[target])

        def broken(*args, **kwargs):
            raise RuntimeError('injected ' + target)

        setattr(pipeline, STAGE_PROCESSORS[target], broken)
        try:
            directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
        finally:
            setattr(pipeline, STAGE_PROCESSORS[target], original)
        self.assertEqual(manifest['stages'][target]['status'], 'failed')
        self.assert_explicit_ledger(directory, manifest)
        self.assertEqual(manifest['stages']['report']['status'], 'complete')
        # The immutable original recording survives the failed processor.
        original_artifact = next(item for item in manifest['artifacts']
                                 if item['kind'] == 'original_recording')
        self.assertEqual(digest(directory / original_artifact['path']),
                         original_artifact['sha256'])

    def test_reanalysis_preserves_invocation_evidence_and_never_repays(self):
        directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
        providers = RunProviders(judge=MockLLMProvider())
        directory, first, _ = self.confirm_roles(directory, providers=providers)
        invocation_names = sorted(path.name for path in (directory / 'provider-calls').iterdir())
        invocation_starts = {path.name: digest(path / 'start.json')
                             for path in (directory / 'provider-calls').iterdir()}
        native_paths = {item['path'] for item in first['artifacts']
                        if item['kind'] == 'asr_native'}
        self.assertTrue(native_paths)

        # A second, different decision set creates another revision.
        directory, second, _ = self.confirm_roles(directory, {'1': 'device', '2': 'tester'},
                                                 providers=providers)
        self.assertNotEqual(second['analysis_id'], first['analysis_id'])
        self.assertEqual(sorted(path.name for path in (directory / 'provider-calls').iterdir()),
                         invocation_names)
        for path in (directory / 'provider-calls').iterdir():
            self.assertEqual(digest(path / 'start.json'), invocation_starts[path.name],
                             'the recorded invocation changed')
        # The paid recognition response is the same registered evidence, restored
        # rather than derived again: no new native artifact was created.
        self.assertEqual({item['path'] for item in second['artifacts']
                          if item['kind'] == 'asr_native'}, native_paths)
        self.assert_explicit_ledger(directory, second)

    # ----------------------------------- AC5: reconstruction from the Run directory

    def test_a_cold_copy_reconstructs_the_same_revision_ledger_links_and_report(self):
        directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
        directory, manifest, _ = self.confirm_roles(
            directory, providers=RunProviders(judge=MockLLMProvider()))
        first = read_run_view(directory)

        # A separate output root holding only the persisted bytes: nothing may come
        # from process state, and the current revision must be the confirmed one.
        cold_root = self.root / 'cold'
        cold_root.mkdir()
        shutil.copytree(directory, cold_root / directory.name)
        cold = read_run_view(cold_root / directory.name)

        self.assertEqual(cold['analysis_id'], manifest['analysis_id'])
        self.assertEqual(cold['stages'], first['stages'])
        self.assertEqual(cold['status'], first['status'])
        self.assertEqual(cold['role_review']['status'], 'complete_review')
        self.assertEqual(cold['metrics_gap'], first['metrics_gap'])
        self.assertEqual(len(cold['invocation_refs']), len(first['invocation_refs']))
        self.assertEqual(cold['report_md'], first['report_md'])
        self.assertTrue(cold['report_md'])
        self.assertEqual(cold['workbench'], first['workbench'])
        self.assertEqual(read_run_view(directory), first,
                         'the projection must be deterministic across reads')

    def test_the_workbench_endpoint_serves_the_projection_the_run_view_carries(self):
        """AC7: #11's evidence projection is reused, not re-implemented for the Run view."""
        directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
        directory, manifest, _ = self.confirm_roles(
            directory, providers=RunProviders(judge=MockLLMProvider()))
        view = self.client.get('/api/runs/' + directory.name).json()
        endpoint = self.client.get(
            '/api/runs/' + directory.name + '/evidence-workbench').json()
        self.assertTrue(view['workbench'])
        self.assertEqual(view['workbench'], endpoint)

    # --------------------------------------------- AC6/AC8: surface consistency

    def test_the_cli_and_the_api_report_the_same_run_revision_and_status(self):
        directory, manifest, _ = self.import_anonymous(judge=MockLLMProvider())
        directory, manifest, _ = self.confirm_roles(
            directory, providers=RunProviders(judge=MockLLMProvider()))

        code, output = self.run_cli(['runs', directory.name, '--output', str(self.runs),
                                     '--json'])
        self.assertEqual(code, 0, output)
        detail = self.client.get('/api/runs/' + directory.name).json()
        self.assertEqual(json.loads(output), detail)
        self.assertEqual(json.loads(output)['analysis_id'], manifest['analysis_id'])

        code, listing = self.run_cli(['runs', '--output', str(self.runs), '--json'])
        self.assertEqual(code, 0, listing)
        self.assertEqual(json.loads(listing), self.client.get('/api/runs').json())
        row = json.loads(listing)['runs'][0]
        self.assertEqual(row['run_id'], directory.name)
        self.assertEqual(row['analysis_id'], manifest['analysis_id'])
        self.assertEqual(row['status'], detail['status'])

        # The human-readable surface reports the same ledger the JSON does.
        code, text = self.run_cli(['runs', directory.name, '--output', str(self.runs)])
        self.assertEqual(code, 0, text)
        self.assertIn(directory.name, text)
        self.assertIn(manifest['analysis_id'], text)
        self.assertIn('complete_review', text)

        # AC8: no long-lived secret or presigned URL may reach any reader.
        for payload in (output, listing, text, json.dumps(detail),
                        self.client.get('/api/runs').text, detail['report_md']):
            self.assertNotIn(SECRET_CANARY, payload)
            self.assertNotIn('?get=', payload)
            self.assertNotIn('?put=', payload)

    def test_an_absent_or_malformed_run_is_reported_instead_of_invented(self):
        """The CLI must not answer with a fabricated Run view for a missing Run."""
        code, output = self.run_cli(['runs', 'RUN-does-not-exist', '--output', str(self.runs)])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')
        self.assertEqual(self.client.get('/api/runs/RUN-does-not-exist').status_code, 404)
        code, output = self.run_cli(['runs', 'not-a-run-id', '--output', str(self.runs)])
        self.assertEqual(code, 1)
        self.assertEqual(output, '')


if __name__ == '__main__':
    unittest.main()
