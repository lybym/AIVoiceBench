"""The asynchronous role-review save contract (Issue #113, PRD-F006/F012-F014/F017).

Issue #113 recorded what a synchronous save cost: a minutes-long request with no
trackable identifier and one generic ``409`` that could equally mean "a rebuild is
running", "the evidence is insufficient" or "the processor crashed".

These tests pin the replacement contract:

1. a save is accepted with ``202`` and a durable ``operation_id``;
2. the operation reports ordered phase progress and a terminal outcome;
3. a retry while a rebuild runs reports *that* operation instead of queueing a
   second one, and a stalled one is only retired by an explicit resubmit;
4. a held Run lock, missing evidence and an internal failure are told apart by
   ``detail.code``;
5. the rebuild writes structured ``run_id``/``phase``/``elapsed_ms``/``code`` lines
   that cannot carry a credential.

The Run here is built from **persisted documents only** (the same fixture the
Evidence Workbench contract uses), so the whole asynchronous path — API, worker,
operation record and reanalysis — is exercised without FFmpeg or any cloud call.
Real recognition quality and physical-device behaviour stay with #85.
"""

import json
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aivoicebench import api
from aivoicebench.import_artifacts import recording_run_errors
from aivoicebench.role_review import load_revisions
from aivoicebench.role_review_operations import REBUILD_PHASES
from aivoicebench.runner import digest
from tests.role_review_fixture import save_role_review
from tests.test_workbench import build_document_run

#: Repository-local scratch space, matching the other contract tests: the Run
#: fixture writes many small files and must not depend on the OS temp directory.
_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def build_rebuild_run(root):
    """A Run whose whole evidence chain is persisted, ready for one role rebuild.

    ``build_document_run(gate='awaiting_role_review')`` produces the anonymous
    clusters and the preserved recognition/clustering documents. Its ASR stage is
    marked complete exactly as ``import_recording`` leaves it after a real
    recognition, because ``apply_role_mapping`` restores that evidence instead of
    recomputing it.
    """
    directory, manifest = build_document_run(root, gate='awaiting_role_review')
    manifest_path = Path(directory) / 'manifest.json'
    manifest['stages']['asr'].update(
        status='complete', reason=None,
        output_artifact_ids=[artifact['artifact_id'] for artifact in manifest['artifacts']
                             if artifact['kind'] == 'transcript'])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return directory, manifest


class RoleReviewOperationTests(unittest.TestCase):
    def setUp(self):
        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.runs = self.root / 'runs'
        self.directory, self.manifest = build_rebuild_run(self.runs)
        self.patch = patch.object(api, 'OUTPUT_ROOT', self.runs)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def clusters(self):
        return [cluster['speaker_id']
                for cluster in api._load_run(self.directory)['role_review']['clusters']]

    def unknown_mapping(self):
        return {cluster: 'unknown' for cluster in self.clusters()}

    # ------------------------------------------------------------ the contract

    def test_accepted_save_reports_a_trackable_operation_with_progress(self):
        accepted, operation, view = save_role_review(
            self.client, self.directory.name,
            {'mapping': {'speaker_0': 'tester', 'speaker_1': 'device'},
             'reviewer': 'zhang', 'reason': '听音比对后确认'})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        body = accepted.json()
        self.assertTrue(body['operation_id'].startswith('OP-'))
        self.assertEqual(body['run_id'], self.directory.name)
        self.assertEqual(body['status'], 'accepted')
        self.assertEqual(body['poll_url'],
                         f'/api/runs/{self.directory.name}/role-review/operations/'
                         f'{body["operation_id"]}')
        # The phase contract is published up front, and the operation ended.
        self.assertEqual(body['phases'], list(REBUILD_PHASES))
        self.assertEqual(operation['operation_id'], body['operation_id'])
        self.assertEqual(operation['status'], 'succeeded')
        self.assertEqual(operation['phase'], 'done')
        self.assertFalse(operation['stalled'])
        self.assertIsNotNone(operation['elapsed_ms'])
        self.assertIsNone(operation['error'])
        self.assertEqual(operation['result']['gate_status'], 'complete_review')
        self.assertEqual(operation['completed_phases'], list(REBUILD_PHASES))
        self.assertEqual(operation['phase_index'], len(REBUILD_PHASES))
        self.assertEqual(view['role_review']['status'], 'complete_review')
        self.assertTrue(operation['result']['analysis_id'])
        self.assertEqual(operation['result']['revision_index'], 1)

    def test_no_operation_endpoint_state_mutates_the_run_before_it_is_asked(self):
        listing = self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations').json()
        self.assertEqual(listing, {'run_id': self.directory.name, 'operations': [], 'active': None})
        self.assertEqual(load_revisions(self.directory), [])

    def test_a_second_save_while_one_runs_is_refused_with_its_operation_id(self):
        from aivoicebench.role_review_operations import start_operation

        mapping = self.unknown_mapping()
        active = start_operation(self.directory, mapping=mapping, reviewer='zhang',
                                 reason='', source_analysis_id='ANALYSIS-source',
                                 cluster_ids=self.clusters())
        response = self.client.post(f'/api/runs/{self.directory.name}/role-review',
                                    json={'mapping': mapping, 'reviewer': 'li'})
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()['detail']
        self.assertEqual(detail['code'], 'operation_in_progress')
        self.assertTrue(detail['retryable'])
        self.assertEqual(detail['operation_id'], active['operation_id'])
        self.assertEqual(detail['operation']['operation_id'], active['operation_id'])
        # The retry queued no second rebuild and wrote no revision.
        self.assertEqual(load_revisions(self.directory), [])
        self.assertEqual(len(self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations').json()['operations']), 1)

    def test_a_stalled_operation_is_retired_only_by_an_explicit_resubmit(self):
        from aivoicebench.role_review_operations import (load_operation, start_operation,
                                                         update_operation)

        mapping = self.unknown_mapping()
        stale = start_operation(self.directory, mapping=mapping, reviewer='zhang',
                                reason='', source_analysis_id='ANALYSIS-source',
                                cluster_ids=self.clusters())
        # A worker that stopped heartbeating is a pending state, not a terminal one.
        update_operation(self.directory, stale['operation_id'],
                         status='running', heartbeat_at='2000-01-01T00:00:00+00:00')
        pending = self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations/'
            f'{stale["operation_id"]}').json()
        self.assertTrue(pending['stalled'])
        self.assertEqual(pending['status'], 'running')

        accepted, operation, _view = save_role_review(
            self.client, self.directory.name, {'mapping': mapping, 'reviewer': 'li'})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertNotEqual(accepted.json()['operation_id'], stale['operation_id'])
        self.assertEqual(operation['status'], 'succeeded')
        retired = load_operation(self.directory, stale['operation_id'])
        self.assertEqual(retired['status'], 'failed')
        self.assertEqual(retired['error']['code'], 'interrupted')

    def test_a_held_run_lock_is_reported_as_its_own_code(self):
        from aivoicebench.run_lock import run_lock

        mapping = self.unknown_mapping()
        with run_lock(self.directory):
            response = self.client.post(f'/api/runs/{self.directory.name}/role-review',
                                        json={'mapping': mapping, 'reviewer': 'zhang'})
        self.assertEqual(response.status_code, 409, response.text)
        detail = response.json()['detail']
        self.assertEqual(detail['code'], 'run_locked')
        self.assertTrue(detail['retryable'])
        self.assertEqual(self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations').json()['operations'], [])

    def test_a_run_without_clusters_is_insufficient_evidence_not_a_lock(self):
        run_id = 'RUN-' + uuid.uuid4().hex
        analysis_id = 'ANALYSIS-' + uuid.uuid4().hex
        (self.runs / run_id / 'analysis' / analysis_id).mkdir(parents=True)
        (self.runs / run_id / 'manifest.json').write_text(json.dumps({
            'schema_version': '1.0.0', 'workflow': 'recording_import', 'run_id': run_id,
            'analysis_id': analysis_id, 'status': 'partial', 'artifacts': [],
            'stages': {}}), encoding='utf-8')
        response = self.client.post(f'/api/runs/{run_id}/role-review',
                                    json={'mapping': {'speaker_0': 'tester'}, 'reviewer': 'zhang'})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()['detail']['code'], 'insufficient_evidence')
        self.assertFalse(response.json()['detail']['retryable'])

    def test_an_unknown_operation_is_a_404_with_its_own_code(self):
        response = self.client.get(
            f'/api/runs/{self.directory.name}/role-review/operations/OP-does-not-exist')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail']['code'], 'operation_not_found')

    def test_an_internal_failure_is_reported_without_echoing_the_exception(self):
        mapping = self.unknown_mapping()
        # A raw exception string must never reach the caller or the log. The sentinel
        # is deliberately not credential-shaped, so the repository's own exposure
        # scan stays clean while the non-disclosure claim is still testable.
        with patch('aivoicebench.import_pipeline.apply_role_mapping',
                   side_effect=RuntimeError('raw-processor-detail-42')):
            accepted, operation, _view = save_role_review(
                self.client, self.directory.name, {'mapping': mapping, 'reviewer': 'zhang'})
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(operation['status'], 'failed', operation)
        self.assertEqual(operation['error']['code'], 'internal_error')
        self.assertTrue(operation['error']['retryable'])
        # The classified code is the contract; the raw exception text is not echoed.
        self.assertNotIn('raw-processor-detail-42', json.dumps(operation, ensure_ascii=False))

    def test_the_rebuild_logs_run_phase_and_outcome_without_secrets(self):
        mapping = self.unknown_mapping()
        with self.assertLogs('aivoicebench.role_review', level='INFO') as captured:
            _accepted, operation, _view = save_role_review(
                self.client, self.directory.name,
                {'mapping': mapping, 'reviewer': 'zhang', 'reason': '无法判断'})
        self.assertEqual(operation['status'], 'succeeded', operation)
        # The handler emits one JSON object per line; `assertLogs` prefixes it with
        # the level and logger name, so parse from the first brace.
        lines = [json.loads(message[message.index('{'):]) for message in captured.output]
        events = [line['event'] for line in lines]
        self.assertIn('role_review_rebuild_accepted', events)
        self.assertIn('role_review_rebuild_started', events)
        self.assertIn('role_review_rebuild_succeeded', events)
        phases = [line['phase'] for line in lines
                  if line['event'] == 'role_review_rebuild_phase']
        # An all-`unknown` mapping abstains downstream, so the phases reported are
        # exactly the stages this revision actually ran — never a phase the
        # pipeline skipped.
        self.assertIn('attribution', phases)
        self.assertIn('report', phases)
        for phase in phases:
            self.assertIn(phase, REBUILD_PHASES)
        for line in lines:
            self.assertEqual(line['run_id'], self.directory.name)
            self.assertTrue(line['operation_id'].startswith('OP-'))
        finished = [line for line in lines
                    if line['event'] == 'role_review_rebuild_succeeded'][0]
        self.assertEqual(finished['detail']['gate_status'], 'complete_review')
        self.assertIsNotNone(finished['elapsed_ms'])
        self.assertNotIn('synthetic-key', json.dumps(lines, ensure_ascii=False))

    def test_the_reanalysis_keeps_the_source_revision_and_creates_a_new_one(self):
        mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
        before_analysis = self.manifest['analysis_id']
        before_artifacts = {a['path']: a['sha256'] for a in self.manifest['artifacts']}
        _accepted, operation, view = save_role_review(
            self.client, self.directory.name, {'mapping': mapping, 'reviewer': 'zhang'})
        self.assertEqual(operation['status'], 'succeeded', operation)
        self.assertNotEqual(view['analysis_id'], before_analysis)
        manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['analysis_id'], view['analysis_id'])
        self.assertEqual(recording_run_errors(manifest, self.directory), [])
        for path, sha in before_artifacts.items():
            self.assertEqual(digest(self.directory / path), sha,
                             f'{path} must stay byte-identical in the earlier revision')
        roles = {segment['speaker_role'] for segment in view['fused_segments']}
        self.assertIn('tester', roles)
        self.assertIn('device', roles)
        # The human decision is complete; the metrics stage's own outcome is
        # published separately, so a completed review cannot read as a promise.
        self.assertEqual(view['role_review']['status'], 'complete_review')
        self.assertEqual(operation['result']['metrics_status'], view['stages']['metrics']['status'])


if __name__ == '__main__':
    unittest.main()
