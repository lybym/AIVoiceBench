"""M1 acceptance-evidence checker tests (Issue #85).

Issue #85 is the authorized real-recording evidence gate. This environment has no
authorized 5–20 minute real recording, no human annotation and no real cloud
credential, so the checker must be able to say exactly that. These tests verify
the *claim rules*: the cases below are constructed record documents, and nothing
here is real-recording acceptance evidence.

What is asserted:

1. a blank/absent recording produces ``no_authorized_recording`` with
   ``real_recording_verified`` false — never a pass;
2. a gate claimed ``verified`` without the evidence that claim requires is an
   unauthorized claim and makes the record invalid;
3. a fully authorized record (authorized 5–20 minute sample, separate human
   review, reconciled denominators, immutable role correction, clean exposure
   scan) validates as ``complete`` — proving the pass path is reachable when the
   evidence really is present;
4. the failure modes that would otherwise read as success — a success-only
   denominator, machine output recorded as human annotation, an overwritten prior
   revision, a credential or committed private recording, a recompute that
   re-bills recognition — are each rejected.

Passing these tests is **not** M1 acceptance. The fixtures are synthetic by
construction, which is precisely why they cannot authorize the real-recording
gate.
"""

import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aivoicebench.acceptance_evidence import (
    CLAIM_NOT_AUTHORIZED, EVIDENCE_POLICY_VERSION, MAX_SAMPLE_MS, MIN_SAMPLE_MS,
    STATUS_COMPLETE, STATUS_INVALID, STATUS_NO_AUTHORIZED_RECORDING,
    STATUS_REAL_RECORDING_PENDING, AcceptanceError, blank_record, load_record,
    render_report, scan_secrets, sha256_file, validate,
)

ISSUE_URL = 'https://github.com/lybym/AIVoiceBench/issues/85'
COMMIT = '4' * 40
AUDIO_SHA = 'a' * 64
ANNOTATION_SHA = 'b' * 64
TRANSCRIPT_SHA = 'c' * 64
REPORT_SHA = 'd' * 64
CORRECTION_DIFF_SHA = 'e' * 64


def evidence(evidence_id, kind, location='outside-git://artifacts/evidence', **overrides):
    item = {
        'evidence_id': evidence_id,
        'kind': kind,
        'location': location,
        'sha256': None,
        'produced_at': None,
        'detail': None,
    }
    item.update(overrides)
    return item


def record(**overrides):
    """A fully authorized record.

    Every number here is a fixture value chosen to satisfy the claim rules, not a
    measurement of any recording. The test suite asserts the *rules*, so the values
    never need to be true — and this record may never be published as acceptance
    evidence.
    """
    document = {
        'schema_version': '1.0.0',
        'record_id': 'ACC-M1-0001',
        'issue_url': ISSUE_URL,
        'recorded_at': '2026-09-19T12:00:00+00:00',
        'recorded_by': 'lybym',
        'prd_refs': ['PRD-F001', 'PRD-N001', 'PRD-N002', 'PRD-N007'],
        'product_version': '0.5.0',
        'code_commit': COMMIT,
        'environment': {
            'deployment': 'docker-compose on Windows 11',
            'browser': 'Chrome 140',
            'browser_remote': True,
            'os': 'Windows 11 24H2',
            'container_image': 'aivoicebench:0.5.0',
            'container_digest': 'sha256:' + 'f' * 64,
            'notes': None,
        },
        'samples': [{
            'sample_id': 'SAMPLE-0001',
            'source': 'authorized_real_recording',
            'device': 'AI toy under test (serial redacted)',
            'duration_ms': 8 * 60 * 1000,
            'sample_rate_hz': 16000,
            'channels': 1,
            'encoding': 'PCM_S16LE',
            'language': 'zh-CN',
            'authorization': {
                'authorized_by': 'sample owner',
                'authorized_at': '2026-09-19T09:00:00+00:00',
                'scope': 'internal M1 acceptance evaluation only',
                'handling_constraints': 'stay outside Git; no redistribution; delete on request',
            },
            'unresolved_constraints': [],
            'selection_reason': 'representative of the target AI-toy workflow',
            'artifacts': [
                {'kind': 'audio', 'location': 'D:/secure/acc/SAMPLE-0001.wav', 'in_git': False,
                 'sha256': AUDIO_SHA, 'byte_length': 15360000, 'produced_by': 'device capture',
                 'notes': None},
                {'kind': 'transcript', 'location': 'D:/secure/acc/SAMPLE-0001.transcript.json',
                 'in_git': False, 'sha256': TRANSCRIPT_SHA, 'byte_length': 4096,
                 'produced_by': 'Seed ASR 2.0', 'notes': None},
            ],
        }],
        'human_reviews': [{
            'review_id': 'REVIEW-0001',
            'kind': 'boundary_annotation',
            'sample_id': 'SAMPLE-0001',
            'annotator': 'human-reviewer-A',
            'annotated_at': '2026-09-19T10:00:00+00:00',
            'artifact': {
                'location': 'D:/secure/acc/SAMPLE-0001.annotation.json',
                'in_git': False,
                'sha256': ANNOTATION_SHA,
                'derived_from_machine_outputs': False,
            },
            'preserved_separately_from_machine_originals': True,
            'notes': None,
        }],
        'evaluations': [{
            'evaluation_id': 'EVAL-0001',
            'kind': 'acoustic_boundary',
            'status': 'measured',
            'sample_ids': ['SAMPLE-0001'],
            'evidence': [evidence('EV-0001', 'real_recording_artifact')],
            'measures': {'evaluated_denominator': 84},
            'unresolved': [],
            'reason': None,
        }],
        'denominators': [{
            'stage': 'file_asr',
            'counters': {'complete': 1, 'partial': 0, 'failed': 0, 'unknown': 0,
                         'abstained': 0, 'not_applicable': 0},
            'expected_total': 1,
            'evidence': [evidence('EV-0002', 'real_cloud_invocation')],
            'notes': 'one authorized sample',
        }],
        'traceability': [{
            'chain_id': 'CHAIN-0001',
            'conclusion': 'the opening turn is tester speech',
            'run_id': 'RUN-0001',
            'analysis_id': 'ANALYSIS-0002',
            'turn_id': 'TURN-0001',
            'event_id': 'EVENT-0001',
            'audio_interval': {'start_ms': 1200.0, 'end_ms': 4200.0},
            'evidence_ids': ['EV-0001'],
            'processor': {'name': 'speaker-alignment', 'version': '1.0.0'},
            'model': 'volc.seedasr.auc',
            'policy': 'SpeakerAlignment 1.0.0',
        }],
        'manual_role_corrections': [{
            'correction_id': 'CORR-0001',
            'run_id': 'RUN-0001',
            'reviewer': 'human-reviewer-A',
            'prior_analysis_id': 'ANALYSIS-0001',
            'new_analysis_id': 'ANALYSIS-0002',
            'prior_result_preserved': True,
            'recompute_evidence': [evidence('EV-0003', 'human_review')],
            'diff_evidence': [evidence('EV-0004', 'human_review',
                                       sha256=CORRECTION_DIFF_SHA)],
            'recompute_scope': 'downstream_only',
            'notes': None,
        }],
        'gates': [
            {'gate': 'software_verified', 'state': 'verified', 'verified_at': '2026-09-19T12:00:00+00:00',
             'evidence': [evidence('EV-0010', 'ci_run')], 'unresolved': [], 'reason': None},
            {'gate': 'container_verified', 'state': 'verified', 'verified_at': '2026-09-19T12:00:00+00:00',
             'evidence': [evidence('EV-0011', 'container_run')], 'unresolved': [], 'reason': None},
            {'gate': 'browser_verified', 'state': 'verified', 'verified_at': '2026-09-19T12:00:00+00:00',
             'evidence': [evidence('EV-0012', 'browser_run')], 'unresolved': [], 'reason': None},
            {'gate': 'real_cloud_verified', 'state': 'verified', 'verified_at': '2026-09-19T12:00:00+00:00',
             'evidence': [evidence('EV-0013', 'real_cloud_invocation',
                                   location='outside-git://invocations/SAMPLE-0001')],
             'unresolved': [], 'reason': None},
            {'gate': 'real_recording_verified', 'state': 'verified',
             'verified_at': '2026-09-19T12:00:00+00:00',
             'evidence': [evidence('EV-0014', 'human_review'),
                          evidence('EV-0015', 'real_recording_artifact')],
             'unresolved': [], 'reason': None},
        ],
        'exposure_scan': {'scanned_at': '2026-09-19T12:00:00+00:00', 'scan_roots': [],
                          'clean': True, 'findings': []},
        'notes': None,
    }
    document.update(overrides)
    return document


def gate(document, name):
    for item in document['gates']:
        if item['gate'] == name:
            return item
    raise AssertionError(f'no gate {name}')


class BlankRecordTest(unittest.TestCase):
    def test_blank_record_claims_no_gate(self):
        template = blank_record('ACC-M1-TEMPLATE', issue_url=ISSUE_URL, recorded_by='lybym',
                                product_version='0.5.0', code_commit=COMMIT)
        self.assertEqual(template['samples'], [])
        self.assertEqual(template['gates'][0]['state'], 'not_reached')
        result = validate(template)
        self.assertEqual(result['status'], STATUS_NO_AUTHORIZED_RECORDING)
        self.assertFalse(result['real_recording_verified'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(sorted(result['pending_gates']),
                         ['browser_verified', 'container_verified', 'real_cloud_verified',
                          'real_recording_verified', 'software_verified'])

    def test_blank_record_is_contract_valid(self):
        template = blank_record('ACC-M1-TEMPLATE', issue_url=ISSUE_URL, recorded_by='lybym',
                                product_version='0.5.0', code_commit=COMMIT)
        self.assertIs(load_record(template), template)


class NoAuthorizedRecordingTest(unittest.TestCase):
    def test_no_samples_reports_pending_not_passed(self):
        document = record(samples=[], human_reviews=[], evaluations=[], denominators=[],
                          traceability=[], manual_role_corrections=[])
        gate(document, 'real_recording_verified')['state'] = 'not_reached'
        gate(document, 'real_recording_verified')['evidence'] = []
        gate(document, 'real_recording_verified')['unresolved'] = ['no authorized recording yet']
        result = validate(document)
        self.assertEqual(result['status'], STATUS_NO_AUTHORIZED_RECORDING)
        self.assertFalse(result['real_recording_verified'])

    def test_a_synthetic_sample_never_authorizes_the_gate(self):
        document = record()
        document['samples'][0]['source'] = 'synthetic'
        result = validate(document)
        self.assertFalse(result['real_recording_verified'])
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('no authorized real recording is recorded in this acceptance record',
                      ' '.join(result['errors']))
        self.assertTrue(any('synthetic' in item for item in result['unresolved']))

    def test_an_out_of_band_recording_never_authorizes_the_gate(self):
        for duration in (3 * 60 * 1000, MIN_SAMPLE_MS - 1, MAX_SAMPLE_MS + 1, 25 * 60 * 1000):
            with self.subTest(duration=duration):
                document = record()
                document['samples'][0]['duration_ms'] = duration
                result = validate(document)
                self.assertFalse(result['real_recording_verified'])
                self.assertEqual(result['status'], STATUS_INVALID)
                self.assertIn('outside the authorized 5–20 minute band', ' '.join(result['errors']))

    def test_the_band_edges_are_inclusive(self):
        for duration in (MIN_SAMPLE_MS, MAX_SAMPLE_MS):
            with self.subTest(duration=duration):
                document = record()
                document['samples'][0]['duration_ms'] = duration
                self.assertEqual(validate(document)['status'], STATUS_COMPLETE)


class ClaimAuthorizationTest(unittest.TestCase):
    def test_a_verified_gate_without_evidence_is_unauthorized(self):
        document = record()
        gate(document, 'software_verified')['evidence'] = []
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertEqual(result['unauthorized_claims'],
                         [{'gate': 'software_verified', 'authorized': False,
                           'reason': 'verified without evidence'}])

    def test_a_gate_claimed_not_reached_must_still_name_its_gap(self):
        document = record()
        gate(document, 'browser_verified').update({'state': 'pending', 'evidence': [],
                                                   'unresolved': [], 'reason': None})
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must record what is missing', ' '.join(result['errors']))

    def test_an_omitted_gate_is_not_a_pending_gate(self):
        document = record()
        document['gates'] = [item for item in document['gates']
                             if item['gate'] != 'real_recording_verified']
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('the real_recording_verified gate must be stated explicitly',
                      ' '.join(result['errors']))

    def test_a_fixture_backed_real_recording_claim_is_rejected(self):
        document = record()
        gate(document, 'real_recording_verified')['evidence'] = [
            evidence('EV-0020', 'synthetic_fixture'),
            evidence('EV-0021', 'ci_run'),
        ]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        unauthorized = result['unauthorized_claims'][0]
        self.assertEqual(unauthorized['gate'], 'real_recording_verified')
        self.assertIn('human_review', unauthorized['reason'])

    def test_a_real_cloud_claim_needs_a_real_invocation(self):
        document = record()
        gate(document, 'real_cloud_verified')['evidence'] = [evidence('EV-0030', 'synthetic_fixture')]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('real_cloud_invocation', ' '.join(result['errors']))

    def test_cloud_evidence_must_be_bound_to_a_recorded_sample(self):
        document = record()
        gate(document, 'real_cloud_verified')['evidence'] = [
            evidence('EV-0031', 'real_cloud_invocation',
                     location='outside-git://invocations/UNRELATED-9999')]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must be bound to an authorized sample', ' '.join(result['errors']))

    def test_evidence_kind_must_be_a_known_kind(self):
        document = record()
        gate(document, 'real_recording_verified')['evidence'][0]['kind'] = 'looks_real'
        with self.assertRaises(AcceptanceError):
            validate(document)


class CompleteRecordTest(unittest.TestCase):
    def test_a_fully_authorized_record_validates_as_complete(self):
        result = validate(record())
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertTrue(result['real_recording_verified'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['unauthorized_claims'], [])
        self.assertEqual(result['authorized_samples'], ['SAMPLE-0001'])
        self.assertEqual(result['evidence_policy_version'], EVIDENCE_POLICY_VERSION)

    def test_markdown_names_every_gate_and_never_claims_a_pending_one(self):
        document = record()
        gate(document, 'browser_verified').update({'state': 'pending', 'evidence': [],
                                                  'unresolved': ['remote Chrome run not performed']})
        result = validate(document)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertFalse(result['real_recording_verified'])
        text = render_report(result)
        for name in ('software_verified', 'container_verified', 'browser_verified',
                     'real_cloud_verified', 'real_recording_verified'):
            self.assertIn(f'`{name}`', text)
        self.assertIn('**no**', text)
        self.assertIn('remote Chrome run not performed', text)

    def test_partial_evaluation_must_state_its_gap(self):
        document = record()
        document['evaluations'][0].update({'status': 'abstained', 'reason': None,
                                           'unresolved': []})
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must state its reason or its unresolved items', ' '.join(result['errors']))


class DenominatorTest(unittest.TestCase):
    def test_a_success_only_denominator_is_rejected(self):
        document = record()
        document['denominators'][0]['counters']['failed'] = 1
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('an outcome excluded from the counters hides records from the denominator',
                      ' '.join(result['errors']))

    def test_expected_total_must_reconcile_with_the_counters(self):
        document = record()
        document['denominators'][0]['expected_total'] = 3
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('counters sum to 1 but expected_total is 3', ' '.join(result['errors']))

    def test_every_counter_is_required_so_an_unknown_cannot_be_omitted(self):
        document = record()
        del document['denominators'][0]['counters']['unknown']
        with self.assertRaises(AcceptanceError):
            validate(document)

    def test_a_reported_stage_must_cite_its_evidence(self):
        document = record()
        document['denominators'][0]['evidence'] = []
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must cite the evidence it was evaluated from', ' '.join(result['errors']))

    def test_a_stage_reported_twice_is_rejected(self):
        document = record()
        document['denominators'].append(copy.deepcopy(document['denominators'][0]))
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('is reported twice', ' '.join(result['errors']))


class HumanLayerTest(unittest.TestCase):
    def test_machine_output_recorded_as_human_annotation_is_rejected(self):
        document = record()
        document['human_reviews'][0]['artifact']['derived_from_machine_outputs'] = True
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('is not human annotation', ' '.join(result['errors']))

    def test_a_review_that_is_not_separate_from_machine_originals_is_rejected(self):
        document = record()
        document['human_reviews'][0]['preserved_separately_from_machine_originals'] = False
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must be preserved separately from machine originals', ' '.join(result['errors']))

    def test_a_review_reusing_a_machine_artifact_digest_is_rejected(self):
        document = record()
        document['human_reviews'][0]['artifact']['sha256'] = AUDIO_SHA
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('byte-identical to a machine sample artifact', ' '.join(result['errors']))

    def test_a_real_sample_without_human_review_is_reported_open(self):
        document = record()
        document['human_reviews'] = []
        result = validate(document)
        self.assertTrue(any('has no human review bound to it' in item
                            for item in result['unresolved']))

    def test_a_review_artifact_committed_to_git_is_rejected(self):
        document = record()
        document['human_reviews'][0]['artifact']['in_git'] = True
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must not be committed to Git', ' '.join(result['errors']))

class RevisionImmutabilityTest(unittest.TestCase):
    def test_a_correction_must_create_a_new_revision(self):
        document = record()
        document['manual_role_corrections'][0]['new_analysis_id'] = 'ANALYSIS-0001'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must create a new AnalysisRevision', ' '.join(result['errors']))

    def test_a_correction_that_overwrites_the_prior_result_is_rejected(self):
        document = record()
        document['manual_role_corrections'][0]['prior_result_preserved'] = False
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('the prior result must be preserved', ' '.join(result['errors']))

    def test_a_correction_without_diff_evidence_is_rejected(self):
        document = record()
        document['manual_role_corrections'][0]['diff_evidence'] = []
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must cite its downstream diff evidence', ' '.join(result['errors']))

    def test_a_recompute_that_rebills_recognition_is_rejected(self):
        document = record()
        document['manual_role_corrections'][0]['recompute_scope'] = 'recomputed_recognition'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must not recompute recognition/clustering evidence',
                      ' '.join(result['errors']))

    def test_an_unknown_recompute_scope_is_reported_open(self):
        document = record()
        document['manual_role_corrections'][0]['recompute_scope'] = 'unknown'
        result = validate(document)
        self.assertTrue(any('does not state which stages were recomputed' in item
                            for item in result['unresolved']))


class TraceabilityTest(unittest.TestCase):
    def test_a_conclusion_without_evidence_is_rejected(self):
        document = record()
        document['traceability'][0]['evidence_ids'] = []
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('is not traceable', ' '.join(result['errors']))

    def test_a_backwards_interval_is_rejected(self):
        document = record()
        document['traceability'][0]['audio_interval'] = {'start_ms': 5000.0, 'end_ms': 5000.0}
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('end_ms must exceed start_ms', ' '.join(result['errors']))

    def test_a_chain_without_a_conversational_unit_is_reported_open(self):
        document = record()
        document['traceability'][0]['turn_id'] = None
        document['traceability'][0]['event_id'] = None
        result = validate(document)
        self.assertTrue(any('cites neither a Turn nor an Event' in item
                            for item in result['unresolved']))

    def test_a_chain_without_a_producing_version_is_reported_open(self):
        document = record()
        document['traceability'][0]['model'] = None
        document['traceability'][0]['policy'] = None
        result = validate(document)
        self.assertTrue(any('names no model or policy version' in item
                            for item in result['unresolved']))


class ExposureScanTest(unittest.TestCase):
    def test_a_credential_shaped_value_in_the_record_is_reported(self):
        document = record()
        document['notes'] = 'provider returned Bearer abcdefgh12345678 during setup'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('credential_shaped_value', ' '.join(result['errors']))

    def test_a_credential_field_in_the_record_is_reported(self):
        document = record()
        document['environment']['api_key'] = 'placeholder-value'
        with self.assertRaises(AcceptanceError):
            # The contract has no such member; the field must be rejected outright.
            validate(document)

    def test_a_signed_url_is_treated_as_credential_material(self):
        document = record()
        document['samples'][0]['artifacts'][0]['location'] = (
            'https://tos.example/bucket/SAMPLE-0001.wav?X-Amz-Signature=deadbeef')
        findings = scan_secrets(document)
        self.assertTrue(any(item['category'] == 'signed_url' for item in findings))

    def test_a_private_key_block_is_reported(self):
        document = record()
        document['notes'] = '-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('private_key', ' '.join(result['errors']))

    def test_the_scan_never_echoes_the_offending_text(self):
        secret = 'Bearer abcdefgh12345678'
        document = record()
        document['notes'] = secret
        findings = scan_secrets(document)
        self.assertTrue(findings)
        self.assertNotIn(secret, json.dumps(findings))

    def test_a_digest_is_not_mistaken_for_a_credential(self):
        document = record()
        findings = scan_secrets(document)
        self.assertEqual(findings, [])

    def test_declaring_a_clean_scan_while_material_is_present_is_rejected(self):
        document = record()
        document['notes'] = 'sk-abcdefghijklmnop'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('the record declares a clean scan but the scan found', ' '.join(result['errors']))

    def test_a_committed_recording_is_rejected(self):
        document = record()
        document['samples'][0]['artifacts'][0]['in_git'] = True
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('a private recording must never be committed', ' '.join(result['errors']))

    def test_a_committed_transcript_is_rejected(self):
        document = record()
        document['samples'][0]['artifacts'][1]['in_git'] = True
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('generated personal reports and provider output must stay outside Git',
                      ' '.join(result['errors']))

    def test_scanning_a_scan_root_reports_audio_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'private.wav'
            audio.write_bytes(b'RIFF-not-really-audio')
            document = record()
            document['exposure_scan']['scan_roots'] = [directory]
            findings = scan_secrets(document)
            self.assertTrue(any(item['category'] == 'real_recording_in_git' for item in findings))

    def test_scanning_a_repository_root_reports_committed_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / 'data'
            nested.mkdir()
            (nested / 'sample.mp3').write_bytes(b'ID3')
            findings = scan_secrets(record(), repository_root=directory)
            self.assertTrue(any(item['category'] == 'real_recording_in_git' for item in findings))


class ArtifactVerificationTest(unittest.TestCase):
    def test_a_matching_artifact_hash_is_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'SAMPLE-0001.wav'
            audio.write_bytes(b'RIFF-pretend-audio')
            document = record()
            document['samples'][0]['artifacts'][0]['location'] = str(audio)
            document['samples'][0]['artifacts'][0]['sha256'] = sha256_file(audio)
            result = validate(document, verify_artifacts=True)
            self.assertEqual(result['artifacts_hashed'], 1)
            self.assertEqual(result['status'], STATUS_COMPLETE)

    def test_a_mismatched_artifact_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / 'SAMPLE-0001.wav'
            audio.write_bytes(b'RIFF-pretend-audio')
            document = record()
            document['samples'][0]['artifacts'][0]['location'] = str(audio)
            result = validate(document, verify_artifacts=True)
            self.assertEqual(result['status'], STATUS_INVALID)
            self.assertIn('does not match the preserved artifact', ' '.join(result['errors']))

    def test_an_absent_artifact_is_reported_instead_of_assumed_to_match(self):
        document = record()
        document['samples'][0]['artifacts'][0]['location'] = 'D:/definitely/not/here.wav'
        result = validate(document, verify_artifacts=True)
        self.assertTrue(any('is not locally present' in item for item in result['unresolved']))


class ContractTest(unittest.TestCase):
    def test_an_unknown_member_is_rejected(self):
        document = record()
        document['real_recording_verified'] = True
        with self.assertRaises(AcceptanceError):
            validate(document)

    def test_an_absent_required_member_is_rejected(self):
        document = record()
        del document['exposure_scan']
        with self.assertRaises(AcceptanceError):
            validate(document)

    def test_the_evaluation_policy_version_is_reported(self):
        result = validate(record())
        self.assertEqual(result['evidence_policy_version'], EVIDENCE_POLICY_VERSION)


class AcceptanceCliTest(unittest.TestCase):
    def _run(self, argv):
        from aivoicebench.__main__ import main
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_init_writes_a_record_that_claims_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            out_path = Path(directory) / 'acceptance.json'
            code, output = self._run([
                'acceptance', 'init', '--record-id', 'ACC-M1-TEMPLATE',
                '--issue-url', ISSUE_URL, '--recorded-by', 'lybym',
                '--product-version', '0.5.0', '--code-commit', COMMIT,
                '--output', str(out_path)])
            self.assertEqual(code, 2)
            self.assertIn('claims no verification', output)
            written = json.loads(out_path.read_text(encoding='utf-8'))
            self.assertEqual(load_record(written)['record_id'], 'ACC-M1-TEMPLATE')
            self.assertEqual([item['state'] for item in written['gates']],
                             ['not_reached'] * 5)

    def test_check_exits_2_for_a_sound_but_unfinished_record(self):
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / 'acceptance.json'
            markdown_path = Path(directory) / 'acceptance.md'
            record_path.write_text(json.dumps(blank_record(
                'ACC-M1-TEMPLATE', issue_url=ISSUE_URL, recorded_by='lybym',
                product_version='0.5.0', code_commit=COMMIT)), encoding='utf-8')
            code, output = self._run(['acceptance', 'check', str(record_path),
                                      '--markdown', str(markdown_path)])
            self.assertEqual(code, 2)
            self.assertIn('NO_AUTHORIZED_RECORDING', output)
            self.assertIn('real_recording_verified=False', output)
            self.assertIn('M1 acceptance evidence', markdown_path.read_text(encoding='utf-8'))

    def test_check_exits_1_for_an_unauthorized_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            document = record()
            gate(document, 'real_recording_verified')['evidence'] = [
                evidence('EV-0040', 'synthetic_fixture')]
            record_path = Path(directory) / 'acceptance.json'
            record_path.write_text(json.dumps(document), encoding='utf-8')
            code, output = self._run(['acceptance', 'check', str(record_path)])
            self.assertEqual(code, 1)
            self.assertIn('UNAUTHORIZED CLAIM real_recording_verified', output)

    def test_check_exits_0_only_for_a_complete_record(self):
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / 'acceptance.json'
            record_path.write_text(json.dumps(record()), encoding='utf-8')
            code, output = self._run(['acceptance', 'check', str(record_path)])
            self.assertEqual(code, 0)
            self.assertIn('real_recording_verified=True', output)

    def test_check_reports_an_unreadable_record(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _ = self._run(['acceptance', 'check', str(Path(directory) / 'missing.json')])
            self.assertEqual(code, 1)

    def test_the_cli_help_names_the_issue_scope(self):
        result = subprocess.run(
            [sys.executable, '-m', 'aivoicebench', '--help'],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        # argparse wraps the subcommand help across lines, so assert on the tokens
        # rather than on one physical line.
        self.assertIn('acceptance', result.stdout)
        self.assertIn('real-recording', result.stdout)
        self.assertIn('#85', result.stdout)


if __name__ == '__main__':
    unittest.main()
