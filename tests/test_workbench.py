"""Evidence Workbench projection contract (PRD-F013/F014, PRD-N001/N002/N006).

The browser Evidence Workbench draws wavesurfer waveform, Regions and Timeline,
but it is a reviewer interface over persisted evidence: it must never become a
measurement producer. These tests pin the projection that makes that true — the
region geometry the browser is allowed to draw, the omission of role-dependent
tracks before the manual speaker-role gate is satisfied, and the fact that an
abstained value stays absent instead of becoming a fabricated zero.

The Run here is built **from persisted documents only** (no FFmpeg, no cloud), so
the projection contract is testable independently of the media pipeline. The
end-to-end Run shape is covered by ``tests/test_role_review.py`` and
``tests/test_alignment.py``; real recording acceptance stays with #85.
"""

import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aivoicebench import api
from aivoicebench.api import _load_run
from aivoicebench.import_artifacts import ImportRun
from aivoicebench.runner import write_json, digest
from aivoicebench.workbench import TRACKS, build_workbench

# Distinctive, non-round coordinates: a projection that "helpfully" rounded or
# re-derived a boundary could not pass by accident.
EVIDENCE_START_MS = 1100.5
EVIDENCE_END_MS = 2600.25
EVENT_START_MS = 1200.0
EVENT_END_MS = 1213.75
METRIC_VALUE = 432.75


def _write_audio(path, seconds=1):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\x10\x00' * (16000 * seconds))


def build_document_run(root, *, gate='complete_review', metric_value=METRIC_VALUE, profile=None):
    """A Run whose entire evidence chain exists as persisted documents."""
    run = ImportRun(root, profile={'device': 'probe-device'} if profile is None else profile)
    complete = gate == 'complete_review'
    decisions = {'speaker_0': 'tester', 'speaker_1': 'device'} if complete else {}
    audio = run.analysis / 'normalized.wav'
    _write_audio(audio)
    normalized_id = run.register(audio, 'normalized_audio', processor='ffmpeg_normalize:1.0.0')
    write_json(run.analysis / 'audio-metadata.json', {
        'schema_version': '1.0.0', 'run_id': run.manifest['run_id'],
        'analysis_id': run.manifest['analysis_id'], 'recording_sha256': 'a' * 64,
        'normalized': {'duration_ms': 3000.0, 'sample_rate': 16000}})
    run.register(run.analysis / 'audio-metadata.json', 'audio_metadata', [normalized_id],
                 'audio_normalize:1.0.0')

    run.envelope('acoustic-segments', 'complete', 'Deterministic acoustic boundaries', {
        'segments': [
            {'segment_id': 'SEG-1', 'start_ms': 0.0, 'end_ms': 1000.5},
            {'segment_id': 'SEG-2', 'start_ms': 1200.0, 'end_ms': 2500.25},
        ]})
    run.envelope('speaker-assignments', 'complete', 'Provider speaker clusters', {
        'document_id': 'SPEAKERS-1',
        'scope': {'recording_sha256': 'a' * 64, 'invocation_id': 'CALL-' + '0' * 32},
        'speaker_segments': [
            {'segment_id': 'SPK-1', 'speaker_id': 'speaker_0', 'native_speaker_id': '1',
             'start_ms': 0.0, 'end_ms': 1000.5, 'raw_utterance_index': 0},
            {'segment_id': 'SPK-2', 'speaker_id': 'speaker_1', 'native_speaker_id': '2',
             'start_ms': 1200.0, 'end_ms': 2500.25, 'raw_utterance_index': 1},
        ]})
    from aivoicebench.alignment import resolve_policy
    write_json(run.analysis / 'alignment.json', {
        'schema_version': '1.0.0', 'document_id': 'ALIGN-1',
        'run_id': run.manifest['run_id'], 'analysis_id': run.manifest['analysis_id'],
        'status': 'complete', 'reason': 'Deterministic acoustic↔speaker-span alignment',
        'policy': resolve_policy(), 'processor': {'name': 'speaker_alignment', 'version': '1.0.0'},
        'diagnostics': {'acoustic_segment_count': 2, 'speaker_span_count': 2,
                        'unmatched_acoustic_ms': 0.0, 'unmatched_speaker_ms': 0.0,
                        'conflicted_acoustic_segment_ids': [], 'per_cluster': []}})
    run.register(run.analysis / 'alignment.json', 'speaker-alignment',
                 processor='speaker_alignment:1.0.0')
    run.envelope('transcript', 'complete', 'Timestamped recognition', {
        'segments': [
            {'segment_id': 'UTT-1', 'start_ms': 0.0, 'end_ms': 1000.5, 'text': '今天天气怎么样',
             'speaker_id': 'speaker_0'},
            {'segment_id': 'UTT-2', 'start_ms': 1200.0, 'end_ms': 2500.25, 'text': '北京今天晴',
             'speaker_id': 'speaker_1'},
        ]})
    run.envelope('fused-segments', 'complete', 'Attribution and fusion', {
        'source': {'duration_ms': 3000.0, 'sample_rate': 16000, 'audio_sha256': 'a' * 64},
        'attribution': {'strategy': 'explicit_mapping' if complete else 'awaiting_role_review',
                        'confidence': 1.0 if complete else None},
        'segments': [
            # A closed gate leaves the anonymous cluster unnamed: the fused segment
            # carries `unknown`, never a role copied from the cluster order.
            {'segment_id': 'SEG-1', 'start_ms': 0.0, 'end_ms': 1000.5,
             'speaker_id': 'speaker_0',
             'speaker_role': 'tester' if complete else 'unknown', 'text': '今天天气怎么样'},
            {'segment_id': 'SEG-2', 'start_ms': 1200.0, 'end_ms': 2500.25,
             'speaker_id': 'speaker_1',
             'speaker_role': 'device' if complete else 'unknown', 'text': '北京今天晴'},
        ]})
    if not complete:
        # Exactly as the pipeline does: every role-dependent stage publishes an
        # explicit insufficient_evidence envelope instead of data, so a reviewer
        # sees an abstention rather than a missing document.
        for kind in ('turns', 'timeline', 'metrics', 'findings'):
            run.envelope(kind, 'insufficient_evidence',
                         'awaiting_role_review: anonymous clusters have no user decision',
                         None, [normalized_id])

    if complete:
        run.envelope('turns', 'complete', 'Turn association', {
            'turns': [{'turn_id': 'TURN-1', 'tester_speech_start_ms': 0.0,
                       'tester_speech_end_ms': 1000.5, 'device_speech_start_ms': 1200.0,
                       'device_speech_end_ms': 2500.25, 'has_interruption': False,
                       'has_overlap': False}]})
        run.envelope('timeline', 'complete', 'Canonical events', {
            'document_id': 'TIMELINE-1', 'execution_kind': 'imported', 'status': 'complete',
            'events': [{'event_id': 'EVT-1', 'type': 'response_start', 'start_ms': EVENT_START_MS,
                        'end_ms': EVENT_END_MS, 'source': 'deterministic_engine', 'confidence': 0.8,
                        'turn_id': 'TURN-1', 'evidence_ids': ['EVD-1']}],
            'evidence': [{'evidence_id': 'EVD-1', 'start_ms': EVIDENCE_START_MS,
                          'end_ms': EVIDENCE_END_MS, 'source': 'asr_speaker_span',
                          'confidence': 0.9}]})
        run.envelope('metrics', 'complete', 'Canonical Metric Engine', {
            'document_id': 'METRICS-1', 'status': 'complete',
            'metrics': [{'metric_id': 'MET-1', 'name': 'response_latency_ms', 'value': metric_value,
                         'unit': 'ms',
                         'status': 'observed' if metric_value is not None else 'insufficient_evidence',
                         'turn_id': 'TURN-1', 'evidence_ids': ['EVD-1'], 'confidence': 0.9,
                         'reason': None if metric_value is not None else 'No confirmed device role'}]})
        run.envelope('findings', 'complete', 'Evidence-linked Findings', {
            'document_id': 'FINDINGS-1',
            'findings': [{'finding_id': 'FND-1', 'title': '响应偏慢', 'severity': 'observation',
                          'status': 'needs_verification', 'confidence': 0.5,
                          'description': '设备响应起始晚于阈值', 'evidence_ids': ['EVD-1'],
                          'event_ids': ['EVT-1'], 'metric_ids': ['MET-1'], 'turn_ids': ['TURN-1'],
                          'human_review': {'status': 'pending'}}],
            'abstentions': [{'reason': 'candidate had no resolvable evidence'}],
            'rejected': []})

    write_json(run.analysis / 'role-review.json', {
        'schema_version': '1.0.0', 'run_id': run.manifest['run_id'],
        'analysis_id': run.manifest['analysis_id'],
        'processor': {'name': 'speaker_role_review', 'version': '1.0.0', 'llm_role_inference': False},
        'scope': {'audio_sha256': 'a' * 64, 'diarization_document_id': 'SPEAKERS-1',
                  'invocation_id': 'CALL-' + '0' * 32},
        'status': gate,
        'reason': 'All clusters have a user decision' if complete
                  else 'Anonymous speaker clusters are waiting for an explicit user role decision',
        'roles': ['tester', 'device', 'unknown'],
        'awaiting_decision_for': [] if complete else ['speaker_0', 'speaker_1'],
        'unknown_clusters': [],
        'clusters': [
            {'speaker_id': 'speaker_0', 'native_speaker_id': '1', 'segment_count': 1,
             'speech_ms': 1000.5, 'span_start_ms': 0.0, 'span_end_ms': 1000.5,
             'representative_intervals': [], 'playback': {'start_ms': 0.0, 'end_ms': 1000.5},
             'transcript_snippets': [], 'decision': decisions.get('speaker_0'),
             'decision_basis': 'user_review' if complete else None,
             'revision_ref': 'REV-abcdef123456' if complete else None},
            {'speaker_id': 'speaker_1', 'native_speaker_id': '2', 'segment_count': 1,
             'speech_ms': 1300.25, 'span_start_ms': 1200.0, 'span_end_ms': 2500.25,
             'representative_intervals': [], 'playback': {'start_ms': 1200.0, 'end_ms': 2500.25},
             'transcript_snippets': [], 'decision': decisions.get('speaker_1'),
             'decision_basis': 'user_review' if complete else None,
             'revision_ref': 'REV-abcdef123456' if complete else None},
        ],
        'revision': ({'revision_id': 'REV-abcdef123456', 'revision_index': 1, 'reviewer': 'zhang',
                      'reason': '听音确认', 'created_at': '2026-09-19T00:00:00+00:00',
                      'mapping_sha256': 'b' * 64, 'decisions': decisions,
                      'analysis_id': run.manifest['analysis_id']} if complete else None),
        'history': ([{'revision_id': 'REV-abcdef123456', 'revision_index': 1, 'reviewer': 'zhang',
                      'reason': '听音确认', 'created_at': '2026-09-19T00:00:00+00:00',
                      'mapping_sha256': 'b' * 64, 'decisions': decisions,
                      'analysis_id': run.manifest['analysis_id']}] if complete else []),
        'diff': {'changed': [], 'added': [], 'removed': [], 'unchanged': []},
        'gate': {'role_dependent_stages_blocked': not complete, 'note': 'probe'},
    })
    run.register(run.analysis / 'role-review.json', 'speaker-role-review',
                 processor='role_review:1.0.0')
    if complete:
        # The immutable human decision file itself, registered exactly where
        # `apply_role_mapping` puts it.
        revision_path = run.directory / 'role-review' / 'role-mapping-REV-0001.json'
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(revision_path, {
            'schema_version': '1.0.0', 'revision_id': 'REV-abcdef123456', 'revision_index': 1,
            'run_id': run.manifest['run_id'], 'analysis_id': run.manifest['analysis_id'],
            'recording_sha256': 'a' * 64, 'diarization_document_id': 'SPEAKERS-1',
            'cluster_ids': sorted(decisions), 'decisions': decisions, 'reviewer': 'zhang',
            'reason': '听音确认', 'created_at': '2026-09-19T00:00:00+00:00',
            'previous_revision_ref': None, 'evidence_refs': [], 'method': 'human_attribution',
            'confidence_basis': 'human_review', 'mapping_sha256': 'b' * 64})
        run.register(revision_path, 'speaker-role-mapping', processor='role_review:1')
    run.checkpoint()
    return run.directory, run.manifest


class WorkbenchFixture(unittest.TestCase):
    gate = 'complete_review'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.manifest = build_document_run(Path(self._tmp.name) / 'runs',
                                                           gate=self.gate)
        self.document = build_workbench(self.directory)

    def regions(self, track_id):
        return [region for region in self.document['regions'] if region['track_id'] == track_id]

    def region(self, region_id):
        return next((region for region in self.document['regions']
                     if region['region_id'] == region_id), None)


class ProjectionTests(WorkbenchFixture):
    """Region geometry is the persisted evidence, verbatim."""

    def test_regions_carry_the_persisted_evidence_intervals(self):
        acoustic = {region['region_id']: region for region in self.regions('acoustic')}
        self.assertEqual(set(acoustic), {'acoustic:SEG-1', 'acoustic:SEG-2'})
        self.assertEqual((acoustic['acoustic:SEG-2']['start_ms'],
                          acoustic['acoustic:SEG-2']['end_ms']), (1200.0, 2500.25))
        # Seconds are a single unit conversion of the persisted milliseconds.
        self.assertEqual(acoustic['acoustic:SEG-2']['start_sec'], 1.2)
        self.assertEqual(acoustic['acoustic:SEG-2']['end_sec'], 2.50025)
        event = self.region('event:EVT-1')
        self.assertEqual((event['start_ms'], event['end_ms']), (EVENT_START_MS, EVENT_END_MS))
        self.assertEqual(event['evidence_class'], 'deterministic')
        self.assertEqual(event['source']['evidence_ids'], ['EVD-1'])
        self.assertEqual(event['source']['document'], 'timeline.json')

    def test_metric_region_is_the_envelope_of_its_own_referenced_evidence(self):
        metric = self.region('metric:MET-1')
        self.assertIsNotNone(metric)
        self.assertEqual((metric['start_ms'], metric['end_ms']),
                         (EVIDENCE_START_MS, EVIDENCE_END_MS))
        self.assertEqual(metric['envelope_of'], 1)
        self.assertEqual(metric['source']['evidence_ids'], ['EVD-1'])
        self.assertEqual(metric['source']['metric_ids'], ['MET-1'])
        # An interval that resolves from evidence is never claimed as a new one.
        self.assertEqual(metric['kind'], 'metric')

    def test_finding_region_resolves_from_its_own_evidence_references(self):
        finding = self.region('finding:FND-1')
        self.assertIsNotNone(finding)
        self.assertEqual((finding['start_ms'], finding['end_ms']),
                         (EVIDENCE_START_MS, EVIDENCE_END_MS))
        self.assertEqual(finding['source']['event_ids'], ['EVT-1'])
        self.assertEqual(finding['source']['metric_ids'], ['MET-1'])
        self.assertEqual(finding['source']['turn_ids'], ['TURN-1'])
        self.assertEqual(finding['evidence_class'], 'semantic')

    def test_metric_values_are_projected_verbatim_and_never_recomputed(self):
        self.assertEqual(self.document['metrics'][0]['value'], METRIC_VALUE)
        self.assertEqual(json.dumps(self.document['metrics'][0]['value']), '432.75')
        self.assertEqual(self.document['metrics'][0]['unit'], 'ms')
        self.assertEqual(self.document['metrics'][0]['region_id'], 'metric:MET-1')

    def test_speaker_regions_never_invent_a_role(self):
        speaker = self.region('speaker:speaker_1:1')
        self.assertEqual(speaker['role'], 'device')
        self.assertEqual(speaker['role_basis'], 'user_review')
        self.assertTrue(speaker['source']['document'].endswith('speaker-assignments.json'))

    def test_transcript_and_provenance_travel_with_the_document(self):
        self.assertEqual([segment['text'] for segment in self.document['transcript']],
                         ['今天天气怎么样', '北京今天晴'])
        provenance = self.document['provenance']
        kinds = {artifact['kind'] for artifact in provenance['artifacts']}
        self.assertIn('normalized_audio', kinds)
        self.assertIn('timeline', kinds)
        self.assertTrue(all(artifact['sha256'] for artifact in provenance['artifacts']))
        self.assertIn('ffmpeg_normalize:1.0.0', provenance['processors'])
        self.assertEqual(self.document['audio']['url'],
                         f'/api/runs/{self.manifest["run_id"]}/audio')

    def test_document_is_deterministic_for_one_revision(self):
        self.assertEqual(build_workbench(self.directory), build_workbench(self.directory))

    def test_every_region_belongs_to_a_declared_track(self):
        declared = {track_id for track_id, _, _ in TRACKS}
        for region in self.document['regions']:
            self.assertIn(region['track_id'], declared)
            self.assertGreaterEqual(region['end_ms'], region['start_ms'])
            self.assertAlmostEqual(region['start_sec'], region['start_ms'] / 1000.0)


class ProvisionalGateTests(WorkbenchFixture):
    """Before the manual role decision, no role-dependent evidence is published."""

    gate = 'awaiting_role_review'

    def test_role_dependent_tracks_are_omitted_not_guessed(self):
        self.assertEqual(self.document['gate']['view_kind'], 'provisional')
        self.assertFalse(self.document['gate']['role_dependent_available'])
        self.assertEqual(self.regions('turn'), [])
        self.assertEqual(self.regions('metric'), [])
        self.assertEqual(self.regions('finding'), [])
        published = {track['track_id'] for track in self.document['tracks']}
        self.assertEqual(published, {'acoustic', 'speaker', 'event'})
        self.assertIsNone(self.document['revision'])

    def test_omitted_tracks_are_explained_instead_of_silently_missing(self):
        blocked = {item['stage'] for item in self.document['unavailable']}
        self.assertTrue({'turn', 'metric', 'finding'} <= blocked)
        reasons = {item['stage']: item['reason'] for item in self.document['unavailable']}
        self.assertIn('awaiting_role_review', reasons['metric'])

    def test_speaker_region_roles_are_unknown_until_decided(self):
        speaker = self.region('speaker:speaker_0:0')
        self.assertIsNone(speaker['role'])
        self.assertTrue(speaker['provisional'])
        self.assertTrue(speaker['uncertain'])
        self.assertIn('尚未由用户确认', speaker['uncertainty'])

    def test_abstained_metric_value_stays_absent(self):
        # With the gate closed the metrics document itself abstains; the projection
        # must not turn that absence into 0 or into a synthesised region.
        self.assertEqual(self.document['metrics'], [])
        self.assertEqual(self.document['findings'], [])

    def test_metrics_gap_explains_the_empty_projection(self):
        gap = self.document['abstentions']['metrics_gap']
        self.assertIn('reasons', gap)
        self.assertTrue(gap['reasons'])


class AbstainingMetricTests(WorkbenchFixture):
    """A Run whose Metric Engine abstained keeps the abstention visible."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.manifest = build_document_run(Path(self._tmp.name) / 'runs',
                                                           gate='complete_review',
                                                           metric_value=None)
        self.document = build_workbench(self.directory)

    def test_null_metric_value_is_not_replaced_by_zero(self):
        self.assertEqual(len(self.document['metrics']), 1)
        metric = self.document['metrics'][0]
        self.assertIsNone(metric['value'])
        self.assertEqual(metric['status'], 'insufficient_evidence')
        self.assertIn('No confirmed device role', metric['reason'])
        region = self.region('metric:MET-1')
        self.assertIsNotNone(region, 'an abstaining metric still carries its evidence interval')
        self.assertTrue(region['uncertain'])
        self.assertEqual(region['status'], 'insufficient_evidence')


class BrowserFacingSafetyTests(WorkbenchFixture):
    """The browser document must not carry provider internals or credentials."""

    def setUp(self):
        super().setUp()
        # An invocation document is registered exactly as the provider writes it,
        # including request parameters that must never reach the browser.
        analysis = self.directory / 'analysis' / self.manifest['analysis_id']
        write_json(analysis / 'call.json', {
            'schema_version': '1.0.0', 'invocation_id': 'CALL-' + '1' * 32,
            'operation_id': 'file_asr_recognize', 'attempt': 1, 'provider': 'volcengine',
            'model': 'volc.seedasr.auc', 'processor_version': 'volcengine_asr:1.0.0',
            'endpoint': 'https://example.invalid/api', 'api_version': 'v3',
            'prompt_version': None, 'config': {'api_key': 'sk-SUPERSECRET', 'headers': {'X': 'y'}},
            'config_schema': {}, 'started_at': '2026-09-19T00:00:00+00:00',
            'finished_at': '2026-09-19T00:00:01+00:00', 'latency_ms': 1000.0, 'status': 'complete',
            'failure_code': None, 'input_artifacts': [{'path': 'normalized.wav', 'sha256': 'c' * 64,
                                                       'size_bytes': 1}], 'output_artifacts': []})
        run = ImportRun.__new__(ImportRun)
        run.directory = self.directory
        run.manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        run.register(analysis / 'call.json', 'provider_invocation', processor='volcengine_asr:1.0.0')
        run.checkpoint()
        self.document = build_workbench(self.directory)

    def test_invocation_provenance_excludes_config_and_endpoint(self):
        invocations = self.document['provenance']['provider_invocations']
        self.assertEqual(len(invocations), 1)
        invocation = invocations[0]
        self.assertEqual(invocation['provider'], 'volcengine')
        self.assertEqual(invocation['model'], 'volc.seedasr.auc')
        self.assertNotIn('config', invocation)
        self.assertNotIn('endpoint', invocation)
        serialized = json.dumps(self.document, ensure_ascii=False)
        self.assertNotIn('sk-SUPERSECRET', serialized)
        self.assertNotIn('example.invalid', serialized)


class WorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patch = patch.object(api, 'OUTPUT_ROOT', Path(self._tmp.name) / 'runs')
        self._patch.start()
        self.addCleanup(self._patch.stop)
        Path(self._tmp.name, 'runs').mkdir(parents=True, exist_ok=True)
        self.directory, self.manifest = build_document_run(Path(self._tmp.name) / 'runs')
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def test_run_document_carries_the_workbench_projection(self):
        view = _load_run(self.directory)
        self.assertEqual(view['workbench']['run_id'], self.manifest['run_id'])
        self.assertEqual(view['workbench']['gate']['status'], 'complete_review')

    def test_workbench_endpoint_serves_the_same_document_as_the_run_view(self):
        response = self.client.get(f'/api/runs/{self.directory.name}/evidence-workbench')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), _load_run(self.directory)['workbench'])

    def test_unknown_run_is_not_answered_with_a_fabricated_workbench(self):
        response = self.client.get('/api/runs/RUN-does-not-exist/evidence-workbench')
        self.assertEqual(response.status_code, 404)

    def test_run_listing_does_not_pay_for_the_projection(self):
        listing = self.client.get('/api/runs').json()['runs']
        self.assertEqual([item['run_id'] for item in listing], [self.directory.name])
        self.assertNotIn('workbench', listing[0])


if __name__ == '__main__':
    unittest.main()
