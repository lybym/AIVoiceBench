"""Manual speaker-role review gate.

The contract under test:

1. Anonymous ASR clusters wait at ``awaiting_role_review``; no role-dependent stage
   or final report is presented before a human decision.
2. No Recording Analysis path calls an LLM to assign tester/device.
3. Every cluster needs an explicit decision; ``unknown`` is a valid deliberate one,
   and "not reviewed" is a different state from "reviewed as unknown".
4. Saving creates a new immutable AnalysisRevision; the previous revision's outputs
   and earlier decisions are preserved, and the diff is shown.
5. Reanalysis uses only the saved human mapping and stays usable after a restart.

Saving answers ``202`` with a trackable operation (Issue #113); that operation
contract, including its distinguishable refusals, is pinned by
``tests/test_role_review_operations.py``, which runs without FFmpeg.
"""

import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aivoicebench import api
from aivoicebench.import_artifacts import RECORDING_ARTIFACT_KINDS, recording_run_errors
from aivoicebench.role_review import (
    REVIEW_KIND, REVISION_KIND, RoleReviewError, build_role_review, load_revisions,
    mapping_diff, review_status, save_revision, validate_decisions)
from aivoicebench.runner import digest
from aivoicebench.validation import schema_errors
from tests.role_review_fixture import save_role_review, wait_for_operation

_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def diarization(spans, scope=None):
    return {
        'document_id': 'DIAR-1',
        'scope': scope or {'recording_sha256': 'a' * 64, 'invocation_id': 'CALL-1'},
        'processor': {'provider': 'test', 'model': 'test'},
        'status': 'complete',
        'speaker_segments': [
            {'segment_id': f'SPK-{i:04d}', 'speaker_id': sid, 'start_ms': start, 'end_ms': end,
             'confidence': conf, 'native_speaker_id': sid.rsplit('_', 1)[-1],
             'raw_utterance_index': i, 'timestamp_source': 'provider_utterance_estimate'}
            for i, (sid, start, end, conf) in enumerate(spans)
        ],
    }


def transcript(segments):
    return {'transcript_id': 'TSR-1', 'segments': segments}


UTTERANCES = [
    {'segment_id': 'ASR-0001', 'text': '今天天气怎么样', 'start_ms': 500, 'end_ms': 1000},
    {'segment_id': 'ASR-0002', 'text': '北京今天晴', 'start_ms': 1500, 'end_ms': 2000},
    {'segment_id': 'ASR-0003', 'text': '那明天呢', 'start_ms': 2500, 'end_ms': 3000},
]


class ReviewSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.diar = diarization([('scope:speaker_0', 500, 1000, 0.8),
                                 ('scope:speaker_1', 1500, 2000, 0.7),
                                 ('scope:speaker_1', 2500, 3000, 0.7)])

    def test_surface_lists_every_cluster_with_reviewable_evidence(self):
        review = build_role_review(_TMP_ROOT / uuid.uuid4().hex, self.diar, transcript(UTTERANCES))
        self.assertEqual([c['speaker_id'] for c in review['clusters']],
                         ['scope:speaker_0', 'scope:speaker_1'])
        first, second = review['clusters']
        self.assertEqual((first['native_speaker_id'], first['segment_count']), ('0', 1))
        self.assertEqual((second['segment_count'], second['speech_ms']), (2, 1000.0))
        # Playback range and representative intervals come from provider evidence.
        self.assertEqual(second['playback'], {'start_ms': 1500.0, 'end_ms': 3000.0})
        self.assertEqual([(i['start_ms'], i['end_ms']) for i in second['representative_intervals']],
                         [(1500.0, 2000.0), (2500.0, 3000.0)])
        # Transcript snippets are tied to the utterance that produced them.
        self.assertEqual([s['text'] for s in second['transcript_snippets']],
                         ['北京今天晴', '那明天呢'])
        self.assertEqual(second['transcript_snippets'][0]['asr_segment_id'], 'ASR-0002')
        # Nothing is decided yet, and no role was guessed.
        self.assertIsNone(first['decision'])
        self.assertEqual(review['status'], 'awaiting_role_review')
        self.assertTrue(review['gate']['role_dependent_stages_blocked'])
        self.assertEqual(review['processor']['llm_role_inference'], False)

    def test_surface_is_schema_valid(self):
        review = build_role_review(_TMP_ROOT / uuid.uuid4().hex, self.diar, transcript(UTTERANCES))
        self.assertEqual(schema_errors(review, 'role-review'), [])

    def test_no_clusters_cannot_run_role_dependent_stages(self):
        review = build_role_review(_TMP_ROOT / uuid.uuid4().hex, {'speaker_segments': []}, {})
        self.assertEqual(review['status'], 'awaiting_role_review')
        self.assertEqual(review['clusters'], [])
        self.assertTrue(review['gate']['role_dependent_stages_blocked'])


class DecisionValidationTests(unittest.TestCase):
    CLUSTERS = ['a', 'b']

    def test_every_cluster_needs_an_explicit_decision(self):
        with self.assertRaises(RoleReviewError) as caught:
            validate_decisions({'a': 'tester'}, self.CLUSTERS)
        self.assertIn('missing: b', str(caught.exception))
        # `unknown` is a decision; it satisfies the requirement.
        self.assertEqual(validate_decisions({'a': 'tester', 'b': 'unknown'}, self.CLUSTERS),
                         {'a': 'tester', 'b': 'unknown'})

    def test_invented_cluster_and_bad_role_are_rejected(self):
        with self.assertRaises(RoleReviewError):
            validate_decisions({'a': 'tester', 'b': 'device', 'c': 'tester'}, self.CLUSTERS)
        with self.assertRaises(RoleReviewError):
            validate_decisions({'a': 'tester', 'b': 'speaker'}, self.CLUSTERS)
        with self.assertRaises(RoleReviewError):
            validate_decisions({'a': 'tester', 'b': 'AI'}, self.CLUSTERS)

    def test_status_distinguishes_unreviewed_from_reviewed_unknown(self):
        self.assertEqual(review_status(self.CLUSTERS, None)[0], 'awaiting_role_review')
        self.assertEqual(review_status(self.CLUSTERS, {'a': 'tester'})[0], 'incomplete_review')
        self.assertEqual(review_status(self.CLUSTERS, {'a': 'unknown', 'b': 'tester'})[0], 'complete_review')
        self.assertEqual(review_status([], None)[0], 'awaiting_role_review')

    def test_diff_reports_changes_additions_and_removals(self):
        diff = mapping_diff({'a': 'tester', 'b': 'unknown'}, {'a': 'device', 'c': 'tester'})
        self.assertEqual(diff['changed'], [{'speaker_id': 'a', 'from': 'tester', 'to': 'device'}])
        self.assertEqual(diff['added'], [{'speaker_id': 'c', 'to': 'tester'}])
        self.assertEqual(diff['removed'], [{'speaker_id': 'b', 'from': 'unknown'}])
        self.assertEqual(diff['unchanged'], [])


class RevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_revisions_are_append_only_and_indexed(self):
        first_path, first = save_revision(self.root, {'a': 'tester', 'b': 'unknown'},
                                          'zhang', reason='initial')
        second_path, second = save_revision(self.root, {'a': 'device', 'b': 'unknown'},
                                            'li', reason='corrected after re-listening')
        self.assertNotEqual(first_path, second_path)
        self.assertEqual((first['revision_index'], second['revision_index']), (1, 2))
        self.assertEqual(second['previous_revision_ref'], first['revision_id'])
        # The earlier decision file is byte-identical: nothing was overwritten.
        self.assertTrue(first_path.is_file())
        self.assertEqual(json.loads(first_path.read_text(encoding='utf-8'))['decisions'],
                         {'a': 'tester', 'b': 'unknown'})
        self.assertEqual([r['revision_index'] for r in load_revisions(self.root)], [1, 2])
        review = build_role_review(
            self.root,
            diarization([('a', 500, 1000, 0.8), ('b', 1500, 2000, 0.7)]),
            transcript(UTTERANCES))
        self.assertEqual(review['status'], 'complete_review')
        self.assertEqual([d['to'] for d in review['diff']['changed']], ['device'])
        self.assertEqual(review['revision']['reviewer'], 'li')
        self.assertEqual(len(review['history']), 2)

    def test_reviewer_is_required_and_roles_are_closed(self):
        with self.assertRaises(RoleReviewError):
            save_revision(self.root, {'a': 'tester'}, '')
        with self.assertRaises(RoleReviewError):
            save_revision(self.root, {'a': 'tester'}, 'zhang', reason='x' * 2001)
        self.assertEqual(load_revisions(self.root), [])

    def test_saved_revision_survives_a_reload(self):
        save_revision(self.root, {'a': 'device'}, 'zhang', reason='listened')
        with TestClient(api.app):
            pass  # a fresh client must not be needed to read the decision from disk
        self.assertEqual(load_revisions(self.root)[0]['decisions'], {'a': 'device'})

    def test_review_and_revision_kinds_are_part_of_the_chain(self):
        self.assertIn(REVIEW_KIND, RECORDING_ARTIFACT_KINDS)
        self.assertIn(REVISION_KIND, RECORDING_ARTIFACT_KINDS)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RoleGateEndToEndTests(unittest.TestCase):
    """Import → awaiting_role_review → save → new revision with metrics."""

    def setUp(self):
        from tests.role_review_fixture import build_anonymous_run

        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # A scripted inline File ASR response with two anonymous speaker clusters.
        self.runs, run_id, self.transport = build_anonymous_run(self.root)
        self.directory = self.runs / run_id
        self.manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        self.patch = patch.object(api, 'OUTPUT_ROOT', self.runs)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def analysis(self, manifest=None):
        manifest = manifest or json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        return self.directory / 'analysis' / manifest['analysis_id']

    def cluster_ids(self):
        expected = api._load_run(self.directory)
        return [cluster['speaker_id'] for cluster in expected['role_review']['clusters']]

    def test_run_pauses_at_awaiting_role_review_without_metrics(self):
        view = api._load_run(self.directory)
        review = view['role_review']
        self.assertEqual(review['status'], 'awaiting_role_review')
        self.assertEqual(len(review['clusters']), 2)
        self.assertEqual(len(review['awaiting_decision_for']), 2)
        # No role-dependent output is presented, and the gate is named in the ledger.
        for kind in ('turns', 'timeline', 'metrics'):
            self.assertEqual(view['stages'][kind]['status'], 'insufficient_evidence')
            self.assertIn('awaiting_role_review', view['stages'][kind]['reason'])
        self.assertEqual(view['turns'], [])
        self.assertEqual(view['metrics'], [])
        codes = {reason['code'] for reason in view['metrics_gap']['reasons']}
        self.assertIn('roles_not_confirmed', codes)
        # Anonymous clusters are never mapped to a role, and no LLM was called.
        for cluster in review['clusters']:
            self.assertIsNone(cluster['decision'])
        for segment in view['fused_segments']:
            self.assertEqual(segment['speaker_role'], 'unknown')
        # The revision publishes its own review surface as registered evidence.
        kinds = [a['kind'] for a in self.manifest['artifacts']]
        self.assertIn(REVIEW_KIND, kinds)

    def test_report_is_provisional_and_states_the_gate(self):
        report = (self.analysis() / 'report.md').read_text(encoding='utf-8')
        self.assertIn('awaiting_role_review', report)
        self.assertIn('## 指标可用性', report)

    def test_api_exposes_the_review_and_rejects_an_incomplete_mapping(self):
        response = self.client.get(f'/api/runs/{self.directory.name}/role-review')
        self.assertEqual(response.status_code, 200, response.text)
        review = response.json()
        self.assertEqual(review['status'], 'awaiting_role_review')
        self.assertEqual(len(review['clusters']), 2)

        clusters = self.cluster_ids()
        partial = self.client.post(f'/api/runs/{self.directory.name}/role-review',
            json={'mapping': {clusters[0]: 'tester'}, 'reviewer': 'zhang'})
        # An incomplete mapping is refused synchronously with its own code: it is a
        # client error to fix, not a failed operation to poll.
        self.assertEqual(partial.status_code, 422, partial.text)
        self.assertEqual(partial.json()['detail']['code'], 'insufficient_evidence')
        self.assertIn('explicit decision', partial.json()['detail']['message'])
        empty = self.client.post(f'/api/runs/{self.directory.name}/role-review',
            json={'mapping': {}, 'reviewer': 'zhang'})
        self.assertEqual(empty.status_code, 400)
        anonymous = self.client.post(f'/api/runs/{self.directory.name}/role-review',
            json={'mapping': {cluster: 'tester' for cluster in clusters}})
        self.assertEqual(anonymous.status_code, 400)
        self.assertIn('复核人', anonymous.json()['detail']['message'])
        # A rejected request leaves no revision and no operation behind.
        self.assertEqual(load_revisions(self.directory), [])
        self.assertEqual(self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations').json()['operations'], [])

    def test_applying_a_mapping_creates_a_new_revision_with_role_outputs(self):
        clusters = self.cluster_ids()
        before = {a['path']: a['sha256'] for a in self.manifest['artifacts']}
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=AssertionError('Role reanalysis must not call a cloud provider')):
            accepted, operation, latest = save_role_review(
                self.client, self.directory.name,
                {'mapping': {clusters[0]: 'tester', clusters[1]: 'device'},
                 'reviewer': 'zhang', 'reason': '听音比对后确认'})
        # The save is accepted asynchronously and reports a trackable operation.
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertTrue(accepted.json()['operation_id'].startswith('OP-'))
        self.assertEqual(operation['status'], 'succeeded', operation)
        self.assertEqual(operation['result']['gate_status'], 'complete_review')

        # A new AnalysisRevision; earlier revision artifacts are byte-identical.
        self.assertNotEqual(latest['analysis_id'], self.manifest['analysis_id'])
        for path, sha in before.items():
            self.assertEqual(digest(self.directory / path), sha)
        # The user mapping is the only role evidence used.
        self.assertEqual(latest['role_review']['status'], 'complete_review')
        self.assertEqual(latest['role_review']['revision']['reviewer'], 'zhang')
        self.assertEqual(latest['role_review']['revision']['revision_index'], 1)
        for attribution in latest['attribution']['attributions']:
            self.assertEqual(attribution['method'], 'explicit_evidence')
            self.assertEqual(attribution['confidence_basis'], 'explicit_user_evidence')
        # Role-dependent stages now ran from the human mapping.
        self.assertEqual(latest['stages']['attribution']['status'], 'complete')
        roles = {segment['speaker_role'] for segment in latest['fused_segments']}
        self.assertTrue(roles <= {'tester', 'device', 'unknown'})
        self.assertIn('tester', roles)
        self.assertIn('device', roles)
        self.assertNotEqual(latest['stages']['turns']['status'], 'pending')
        self.assertEqual(recording_run_errors(
            json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8')),
            self.directory), [])

        # The operation record is durable and lists every phase it passed through.
        operations = self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations').json()
        self.assertEqual(len(operations['operations']), 1)
        self.assertIsNone(operations['active'])
        phases = operations['operations'][0]['phases']
        self.assertEqual(operations['operations'][0]['completed_phases'], phases)
        self.assertEqual(operations['operations'][0]['phase_index'], len(phases))

        # No cloud provider was contacted during reanalysis: inline transport means the
        # import's single recognition POST is still the only call ever made.
        self.assertEqual([m for m, *_ in self.transport.calls], ['POST'])

    def test_changing_a_mapping_adds_a_revision_and_a_diff(self):
        clusters = self.cluster_ids()
        mapping = {clusters[0]: 'tester', clusters[1]: 'device'}
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=AssertionError('no cloud call')):
            _post, _operation, first = save_role_review(
                self.client, self.directory.name, {'mapping': mapping, 'reviewer': 'zhang'})
        swapped = {clusters[0]: 'device', clusters[1]: 'tester'}
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=AssertionError('no cloud call')):
            _post, _operation, second = save_role_review(
                self.client, self.directory.name,
                {'mapping': swapped, 'reviewer': 'li', 'reason': '角色标反了'})

        self.assertNotEqual(first['analysis_id'], second['analysis_id'])
        review = second['role_review']
        self.assertEqual(review['revision']['revision_index'], 2)
        self.assertEqual(len(review['history']), 2)
        self.assertEqual({item['speaker_id'] for item in review['diff']['changed']}, set(clusters))
        # Both revisions and both decisions remain readable after a restart.
        with TestClient(api.app) as restarted:
            reloaded = restarted.get(f'/api/runs/{self.directory.name}/role-review').json()
        self.assertEqual(len(reloaded['history']), 2)
        self.assertEqual(reloaded['revision']['reviewer'], 'li')
        revisions = load_revisions(self.directory)
        self.assertEqual([r['decisions'] for r in revisions], [mapping, swapped])

    def test_unknown_decision_keeps_outputs_explicitly_insufficient(self):
        clusters = self.cluster_ids()
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=AssertionError('no cloud call')):
            _post, operation, latest = save_role_review(
                self.client, self.directory.name,
                {'mapping': {cluster: 'unknown' for cluster in clusters},
                 'reviewer': 'zhang', 'reason': '无法判断'})
        # Reviewed and recorded, but no role claim is invented from it. The
        # operation still succeeds: the human decision completed, and the
        # downstream abstention is its own, separately-reported state.
        self.assertEqual(operation['status'], 'succeeded', operation)
        self.assertEqual(operation['result']['gate_status'], 'complete_review')
        self.assertIn(operation['result']['metrics_status'],
                      ('partial', 'insufficient_evidence', 'failed'))
        self.assertEqual(latest['role_review']['status'], 'complete_review')
        self.assertEqual(sorted(latest['role_review']['unknown_clusters']), sorted(clusters))
        self.assertEqual(latest['turns'], [])
        self.assertEqual(latest['metrics'], [])
        self.assertEqual(latest['stages']['turns']['status'], 'insufficient_evidence')
        for segment in latest['fused_segments']:
            self.assertEqual(segment['speaker_role'], 'unknown')

    def test_persistence_across_restart_keeps_the_gate_state(self):
        clusters = self.cluster_ids()
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=AssertionError('no cloud call')):
            _post, operation, _view = save_role_review(
                self.client, self.directory.name,
                {'mapping': {clusters[0]: 'tester', clusters[1]: 'device'},
                 'reviewer': 'zhang'})
        self.assertEqual(operation['status'], 'succeeded', operation)
        with TestClient(api.app) as restarted:
            view = restarted.get(f'/api/runs/{self.directory.name}').json()
            self.assertEqual(view['role_review']['status'], 'complete_review')
            self.assertEqual(view['role_review']['revision']['reviewer'], 'zhang')
            self.assertIn('tester', {s['speaker_role'] for s in view['fused_segments']})
            # The accepted operation, its phases and its outcome survive the restart.
            restored = restarted.get(
                f'/api/runs/{self.directory.name}/role-review/operations').json()
            self.assertEqual(restored['operations'][0]['operation_id'],
                             operation['operation_id'])
            self.assertEqual(restored['operations'][0]['status'], 'succeeded')
            self.assertEqual(restored['operations'][0]['result']['analysis_id'],
                             view['analysis_id'])


if __name__ == '__main__':
    unittest.main()
