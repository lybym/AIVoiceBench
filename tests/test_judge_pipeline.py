"""The classic pipeline path runs the Judge and validates its own output.

`pipeline.run_full_pipeline` consumes one canonical WAV directly (energy VAD, no
ASR provider), so it can run offline in the test suite. It covers the path the
ImportRun chain does not: it also feeds the semantic records back into the
canonical metrics and refuses to write an artifact that fails its own contract.

Because this path has no ASR or diarization, `build_turns` refuses to invent
role-dependent turns, so it exercises the run-level Judge and the abstention path
rather than per-turn semantic metrics (those are covered through the import chain
in `tests/test_judge_import_stage.py` and the contract tests).

The WAV is synthetic tone bursts; no recording, provider or hardware is involved.
"""

import json
import math
import shutil
import sys
import tempfile
import unittest
from array import array
from pathlib import Path
import wave

from aivoicebench.llm import MockLLMProvider, UnavailableLLMProvider
from aivoicebench.pipeline import run_full_pipeline
from aivoicebench.validation import judge_document_errors


def write_wav(path, rate=16000):
    """Tester burst, a pause, then a device burst: the energy VAD finds two segments."""
    samples = []
    for value in ([0] * int(0.5 * rate) + [18000] * int(0.5 * rate)
                  + [0] * int(0.7 * rate) + [18000] * int(0.5 * rate)
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


class FullPipelineJudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'dialogue.wav'
        write_wav(self.source)

    def read(self, out, name):
        return json.loads((Path(out) / name).read_text(encoding='utf-8'))

    def test_pipeline_writes_a_valid_judge_artifact_and_findings_document(self):
        out = self.root / 'run-fixture'
        summary = run_full_pipeline(self.source, out, provider=MockLLMProvider())
        self.assertTrue(summary['judge_result_count'] > 0)

        judge_data = self.read(out, 'judge-results.json')
        timeline = self.read(out, 'timeline.json')
        turns = self.read(out, 'turns.json')
        self.assertEqual(judge_document_errors(judge_data, timeline, turns), [],
                         'the pipeline published a Judge artifact that breaks its contract')
        self.assertTrue(judge_data['invocations'])
        self.assertTrue(all(invocation['raw_response'] is not None
                            for invocation in judge_data['invocations']
                            if invocation['status'] == 'success'))
        # The raw responses are preserved as their own artifact too.
        self.assertEqual(self.read(out, 'judge-raw.json')['invocations'],
                         judge_data['invocations'])

        findings_document = self.read(out, 'findings.json')
        self.assertEqual(findings_document['schema_version'], '2.1.0')
        self.assertEqual(findings_document['rejected'], [],
                         'the pipeline published a Finding that breaks its contract')

    def test_unconfigured_provider_keeps_the_semantic_metric_abstained(self):
        out = self.root / 'run-unavailable'
        summary = run_full_pipeline(self.source, out, provider=UnavailableLLMProvider())
        self.assertEqual(summary['finding_count'], 0)
        judge_data = self.read(out, 'judge-results.json')
        self.assertTrue(all(result['status'] != 'observed'
                            for result in judge_data['results']))
        self.assertTrue(judge_data['abstentions'])
        self.assertEqual(
            judge_document_errors(judge_data, self.read(out, 'timeline.json'),
                                  self.read(out, 'turns.json')), [])
        # No Finding is derived from an abstained judgment.
        findings_document = self.read(out, 'findings.json')
        self.assertEqual(findings_document['findings'], [])
        self.assertEqual(findings_document['rejected'], [])

    def test_the_run_level_semantic_metric_abstains_without_evidence(self):
        """PRD-M003/M006 abstain here because this path builds no turns.

        The classic pipeline consumes a WAV with no ASR or diarization, so
        `build_turns` correctly refuses to invent role-dependent turns. The
        per-turn semantic metrics are therefore absent rather than fabricated,
        and every emitted metric still passes the engine's own validator.
        """
        out = self.root / 'run-no-turns'
        run_full_pipeline(self.source, out, provider=MockLLMProvider())
        self.assertEqual(self.read(out, 'turns.json')['turns'], [])
        metrics_result = self.read(out, 'metrics.json')
        self.assertEqual(metrics_result['metrics'], [])
        self.assertEqual(metrics_result['status'], 'insufficient_evidence')
        self.assertEqual(self.read(out, 'findings.json')['findings'], [])

    def test_an_invalid_self_produced_artifact_stops_the_pipeline(self):
        """The pipeline validates its own output instead of writing it."""
        import aivoicebench.llm as llm_module
        original = llm_module.LLMJudge.evaluate

        def broken(self, fused_doc, turns_doc, metrics_result, timeline=None,
                   run_id=None, analysis_id=None):
            document = original(self, fused_doc, turns_doc, metrics_result, timeline,
                                run_id, analysis_id)
            document['results'][0]['evidence_refs'] = ['EVD-DOES-NOT-EXIST']
            return document

        out = self.root / 'run-broken'
        llm_module.LLMJudge.evaluate = broken
        try:
            with self.assertRaises(ValueError) as caught:
                run_full_pipeline(self.source, out, provider=MockLLMProvider())
        finally:
            llm_module.LLMJudge.evaluate = original
        self.assertIn('Judge artifact violates its own contract', str(caught.exception))
        # Nothing that failed its contract was published as evidence.
        self.assertFalse((out / 'judge-results.json').exists())

    def test_a_run_with_no_acoustic_segments_still_publishes_valid_documents(self):
        """The abstention branch must write artifacts a consumer can validate."""
        silent = self.root / 'silence.wav'
        with wave.open(str(silent), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(array('h', [0] * 16000).tobytes())
        out = self.root / 'run-silent'
        summary = run_full_pipeline(silent, out, provider=MockLLMProvider())
        self.assertEqual(summary['segment_count'], 0)

        judge_data = self.read(out, 'judge-results.json')
        self.assertEqual(judge_data['results'], [])
        self.assertEqual(judge_document_errors(judge_data), [],
                         'the abstention branch published an unvalidatable Judge artifact')
        self.assertEqual(self.read(out, 'judge-raw.json')['invocations'], [])
        findings_document = self.read(out, 'findings.json')
        self.assertEqual(findings_document['schema_version'], '2.1.0')
        self.assertEqual(findings_document['findings'], [])
        self.assertTrue((out / 'report.md').exists())


def test_an_invalid_input_timeline_abstains_instead_of_blaming_the_judge(self):
        """An invalid *input* Timeline is not a Judge-contract violation."""
        import aivoicebench.pipeline as pipeline_module
        original = pipeline_module.generate_timeline

        def invalid_timeline(*args, **kwargs):
            timeline = original(*args, **kwargs)
            # A dangling evidence reference makes the Timeline invalid regardless of
            # its status, without touching the Judge's own output.
            timeline['events'].append({
                'schema_version': '2.0.0', 'event_id': 'EVT-INJECTED',
                'run_id': timeline['run_id'], 'case_id': timeline['case_id'],
                'turn_id': None, 'response_id': None, 'type': 'interrupt_start',
                'start_ms': 0, 'end_ms': 0, 'source': 'derived',
                'observation_scope': 'black_box', 'confidence': None,
                'confidence_source': None, 'uncertainty_ms': None,
                'evidence_ids': ['EVD-DOES-NOT-EXIST'],
            })
            return timeline

        provider = _CountingProvider()
        pipeline_module.generate_timeline = invalid_timeline
        try:
            out = self.root / 'run-invalid-timeline'
            run_full_pipeline(self.source, out, provider=provider)
        finally:
            pipeline_module.generate_timeline = original

        # No provider call was made for an unusable Timeline.
        self.assertEqual(provider.calls, 0)
        judge_data = self.read(out, 'judge-results.json')
        self.assertEqual(judge_data['results'], [])
        abstention = next(item for item in judge_data['abstentions']
                          if item['dimension'] == 'timeline')
        self.assertEqual(abstention['state'], 'not_eligible')
        self.assertIn('EVD-DOES-NOT-EXIST', abstention['reason'])
        self.assertEqual(judge_document_errors(judge_data), [])
        self.assertTrue(self.read(out, 'metrics.json')['metrics'] == [])
        self.assertEqual(self.read(out, 'findings.json')['findings'], [])


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