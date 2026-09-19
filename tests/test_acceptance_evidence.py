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
    CLAIM_NOT_AUTHORIZED, EVIDENCE_POLICY_VERSION, M1_STAGES, MAX_SAMPLE_MS, MIN_SAMPLE_MS,
    PCM_ENCODING_BYTES_PER_SAMPLE, STATUS_COMPLETE, STATUS_INVALID,
    STATUS_NO_AUTHORIZED_RECORDING, STATUS_REAL_RECORDING_PENDING, AcceptanceError,
    blank_record, load_record, render_report, scan_secrets, sha256_file, validate,
)


def implied_pcm_bytes(sample):
    """The artifact size a raw-PCM sample's own parameters imply (test-side mirror)."""
    bytes_per_sample = PCM_ENCODING_BYTES_PER_SAMPLE.get(str(sample['encoding']).upper())
    if bytes_per_sample is None:
        return None
    return (sample['sample_rate_hz'] * sample['channels'] * bytes_per_sample
            * (sample['duration_ms'] / 1000.0))

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
        'sample_id': overrides.pop('sample_id', 'SAMPLE-0001'),
        'sha256': None,
        'produced_at': None,
        'detail': None,
    }
    item.update(overrides)
    return item


def unbound(evidence_id, kind, **overrides):
    """Evidence that names no sample; used to prove a real gate refuses it."""
    return evidence(evidence_id, kind, sample_id=None, **overrides)


def denominators(**stage_totals):
    """Report every M1 stage, as a complete acceptance record must.

    A record that claims the real-recording gate has to account for every stage,
    including the stages where nothing failed or that did not apply; absent stages
    are indistinguishable from "nothing failed". ``stage_totals`` overrides the
    default of one complete record per stage, and a value of ``None`` marks the
    stage as not applicable.
    """
    known = {
        'file_asr': [evidence('EV-0002', 'real_cloud_invocation')],
        'acoustic_boundary': [evidence('EV-0005', 'real_recording_artifact')],
    }
    entries = []
    for index, stage in enumerate(M1_STAGES):
        total = stage_totals.get(stage, 1)
        if total is None:
            counters = {'complete': 0, 'partial': 0, 'failed': 0, 'unknown': 0,
                        'abstained': 0, 'not_applicable': 1}
            total = 1
        else:
            counters = {'complete': total, 'partial': 0, 'failed': 0, 'unknown': 0,
                        'abstained': 0, 'not_applicable': 0}
        entries.append({
            'stage': stage,
            'counters': counters,
            'expected_total': total,
            'evidence': known.get(stage) or [evidence(f'EV-S{index:02d}', 'human_review')],
            'notes': None,
        })
    return entries


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
        'denominators': denominators(),
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
             # The gate's human-review evidence is the record's own review artifact: the
             # checker reconciles the entry with `human_reviews[]` by digest, so an entry
             # pointing at unrelated bytes cannot authorize the gate.
             'evidence': [evidence('EV-0014', 'human_review',
                                   location='D:/secure/acc/SAMPLE-0001.annotation.json',
                                   sha256=ANNOTATION_SHA),
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


def materialize(document, directory):
    """Point every declared artifact and evidence reference at a real file.

    A verified gate now requires the declared digests to be checked for the sample
    artifacts, the human-review artifact and the evidence that authorizes a real
    gate, so the complete-record fixtures must all actually exist and hash to what
    the record says. ``byte_length`` is filled in from the file that was just written
    for the same reason: a declared size that contradicts the preserved artifact is
    now a reconciliation failure, and a fixture that keeps a decorative size would
    assert the wrong thing about the control. This does **not** make the fixture a
    real recording: it is generated bytes whose only purpose is to satisfy the digest
    and size controls under test.
    """
    directory = Path(directory)
    # An evidence entry already declared as one of the human review artifacts must
    # resolve to *that* file: the checker reconciles the gate's `human_review` evidence
    # with `human_reviews[]` by digest, so generating a separate file here would
    # silently break the binding the fixture means to express. Captured before the
    # review locations are rewritten below.
    review_locations = {review['artifact']['location']: review['artifact']
                        for review in document['human_reviews']}
    for index, sample in enumerate(document['samples']):
        for position, artifact in enumerate(sample['artifacts']):
            path = directory / f'{sample["sample_id"]}-{position}{Path(artifact["location"]).suffix or ".bin"}'
            payload = f'fixture {sample["sample_id"]} {position}'.encode('utf-8')
            if artifact['kind'] == 'audio':
                # A raw-PCM sample states its own duration, rate and channel count, so
                # the fixture writes the byte count those parameters imply and then
                # declares the size it actually wrote. Otherwise the fixture would be
                # asserting a recording whose own three statements contradict each other.
                implied = implied_pcm_bytes(sample)
                if implied is not None:
                    payload = payload.ljust(max(int(implied), len(payload)), b'\x00')
            path.write_bytes(payload)
            artifact['location'] = str(path)
            artifact['sha256'] = sha256_file(path)
            artifact['byte_length'] = path.stat().st_size
            if artifact['kind'] == 'audio' and implied_pcm_bytes(sample) is not None:
                # Express the sample's duration as the one its real artifact has.
                sample['duration_ms'] = path.stat().st_size / (
                    sample['sample_rate_hz'] * sample['channels']
                    * PCM_ENCODING_BYTES_PER_SAMPLE[str(sample['encoding']).upper()]) * 1000.0
    for index, review in enumerate(document['human_reviews']):
        path = directory / f'{review["review_id"]}-annotation.json'
        path.write_text(json.dumps({'review': review['review_id']}), encoding='utf-8')
        review['artifact']['location'] = str(path)
        review['artifact']['sha256'] = sha256_file(path)
    for label, entry in _evidence_entries(document):
        declared = review_locations.get(entry['location'])
        if declared is not None:
            entry['location'] = declared['location']
            entry['sha256'] = declared['sha256']
            continue
        slug = f'{label}-{entry["evidence_id"]}'.replace('/', '_')
        path = directory / f'{slug}.json'
        path.write_text(json.dumps({'evidence': entry['evidence_id']}), encoding='utf-8')
        entry['location'] = str(path)
        entry['sha256'] = sha256_file(path)
    return document


def resize_audio_fixture(document, directory, duration_ms):
    """Re-express the sample's duration with a matching, real artifact.

    The duration, the artifact's real size and its declared `byte_length` are three
    statements about one recording, so a test that moves the duration must move the
    others with it.
    """
    sample = document['samples'][0]
    artifact = sample['artifacts'][0]
    path = Path(artifact['location'])
    implied = implied_pcm_bytes(dict(sample, duration_ms=duration_ms))
    payload = path.read_bytes()[:64]
    path.write_bytes(payload.ljust(int(implied), b'\x00'))
    artifact['sha256'] = sha256_file(path)
    artifact['byte_length'] = path.stat().st_size
    sample['duration_ms'] = duration_ms
    return document


def _evidence_entries(document):
    """Every evidence reference in the record, with a stable label."""
    for index, evaluation in enumerate(document.get('evaluations') or []):
        for position, entry in enumerate(evaluation.get('evidence') or []):
            yield f'evaluations-{index}-{position}', entry
    for index, entry in enumerate(document.get('denominators') or []):
        for position, item in enumerate(entry.get('evidence') or []):
            yield f'denominators-{index}-{position}', item
    for index, correction in enumerate(document.get('manual_role_corrections') or []):
        for key in ('recompute_evidence', 'diff_evidence'):
            for position, item in enumerate(correction.get(key) or []):
                yield f'corrections-{index}-{key}-{position}', item
    for index, gate in enumerate(document.get('gates') or []):
        for position, entry in enumerate(gate.get('evidence') or []):
            yield f'gates-{index}-{position}', entry


def validate_complete(document):
    """Validate a record as a real acceptance run would: from a scanned repository.

    `real_recording_verified` is only authorized when artifact verification was
    requested and the repository was scanned, so the pass path is exercised that way.
    """
    return validate(document, verify_artifacts=True, repository_root=Path(__file__).resolve().parent)


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
        # The real-recording gate is the one under test, and a record with no sample
        # cannot also carry a real-cloud call or a conclusion chain.
        document = record(samples=[], human_reviews=[], evaluations=[], traceability=[])
        for name in ('real_cloud_verified', 'real_recording_verified'):
            gate(document, name).update({
                'state': 'not_reached', 'evidence': [],
                'unresolved': ['no authorized recording yet']})
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
        self.assertTrue(any('synthetic' in item for item in result['observations']))

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
                with tempfile.TemporaryDirectory() as directory:
                    document = materialize(record(), directory)
                    resize_audio_fixture(document, directory, duration)
                    self.assertEqual(validate_complete(document)['status'], STATUS_COMPLETE)


class ClaimAuthorizationTest(unittest.TestCase):
    def test_a_verified_gate_without_evidence_is_unauthorized(self):
        document = record()
        gate(document, 'software_verified')['evidence'] = []
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn({'gate': 'software_verified', 'authorized': False,
                       'reason': 'verified without evidence'}, result['unauthorized_claims'])

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

    def test_cloud_evidence_must_name_an_authorized_sample(self):
        document = record()
        gate(document, 'real_cloud_verified')['evidence'] = [
            unbound('EV-0031', 'real_cloud_invocation',
                    location='outside-git://invocations/UNRELATED-9999')]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must name the authorized sample', ' '.join(result['errors']))

    def test_a_path_containing_a_sample_name_does_not_bind_cloud_evidence(self):
        # Substring binding used to authorize this; a path that merely mentions the
        # sample is not the sample the call was run for.
        document = record()
        gate(document, 'real_cloud_verified')['evidence'] = [
            unbound('EV-0032', 'real_cloud_invocation',
                    location='outside-git://invocations/SAMPLE-0001/notes.txt')]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])

    def test_a_synthetic_sample_cannot_authorize_the_real_cloud_gate(self):
        document = record()
        document['samples'].append(copy.deepcopy(document['samples'][0]))
        document['samples'][1]['sample_id'] = 'SYN-0001'
        document['samples'][1]['source'] = 'synthetic'
        gate(document, 'real_cloud_verified')['evidence'] = [
            evidence('EV-0033', 'real_cloud_invocation',
                     location='outside-git://invocations/SYN-0001', sample_id='SYN-0001')]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn({'gate': 'real_cloud_verified', 'authorized': False,
                       'reason': 'the cloud evidence is bound to a non-authorized '
                                 '(synthetic/fixture) sample'}, result['unauthorized_claims'])

    def test_a_human_review_not_bound_to_the_sample_cannot_authorize_the_gate(self):
        document = record()
        gate(document, 'real_recording_verified')['evidence'] = [
            unbound('EV-0034', 'human_review'),
            evidence('EV-0035', 'real_recording_artifact'),
        ]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('no human review evidence is bound to an authorized real sample',
                      ' '.join(result['errors']))

    def test_a_human_review_bound_to_a_synthetic_sample_cannot_authorize_the_gate(self):
        document = record()
        document['samples'].append(copy.deepcopy(document['samples'][0]))
        document['samples'][1]['sample_id'] = 'SYN-0002'
        document['samples'][1]['source'] = 'synthetic'
        gate(document, 'real_recording_verified')['evidence'] = [
            evidence('EV-0036', 'human_review', sample_id='SYN-0002'),
            evidence('EV-0037', 'real_recording_artifact'),
        ]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('bound to a non-authorized (synthetic/fixture) sample',
                      ' '.join(result['errors']))

    def test_duplicate_evidence_ids_are_rejected(self):
        document = record()
        # EV-0001 is already declared by the acoustic_boundary evaluation.
        document['denominators'][1]['evidence'] = [evidence('EV-0001', 'real_recording_artifact')]
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must be unique', ' '.join(result['errors']))

    def test_a_chain_citing_undeclared_evidence_is_rejected(self):
        document = record()
        document['traceability'][0]['evidence_ids'] = ['EV-NOT-DECLARED-ANYWHERE']
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('is not declared as evidence anywhere in this record',
                      ' '.join(result['errors']))

    def test_evidence_kind_must_be_a_known_kind(self):
        document = record()
        gate(document, 'real_recording_verified')['evidence'][0]['kind'] = 'looks_real'
        with self.assertRaises(AcceptanceError):
            validate(document)

    def test_a_verified_gate_without_a_timestamp_is_rejected(self):
        # P2 regression: `verified_at` was optional on a `verified` gate, so a
        # verification that cannot be placed in time was reported as complete.
        for name in ('software_verified', 'real_recording_verified'):
            with self.subTest(gate=name):
                document = record()
                gate(document, name)['verified_at'] = None
                result = validate(document)
                self.assertEqual(result['status'], STATUS_INVALID)
                self.assertFalse(result['real_recording_verified'])
                self.assertIn('without a verified_at timestamp', ' '.join(result['errors']))

    def test_a_verified_gate_with_a_timestamp_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_COMPLETE)

    def test_a_verification_before_the_recording_was_authorized_is_rejected(self):
        # P2 hardening: the timestamp was only required to exist, so a gate verified
        # before the sample was authorized still read as a reconstructible acceptance.
        document = record()
        gate(document, 'real_recording_verified')['verified_at'] = '2026-09-19T08:00:00+00:00'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('before sample SAMPLE-0001 was authorized', ' '.join(result['errors']))

    def test_a_verification_after_the_record_was_written_is_rejected(self):
        document = record()
        gate(document, 'browser_verified')['verified_at'] = '2026-09-19T13:00:00+00:00'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('after the record was written', ' '.join(result['errors']))

    def test_an_unparseable_timestamp_is_not_treated_as_ordered(self):
        # An unparseable timestamp is not silently accepted as in-order; it simply
        # cannot be compared, and the presence requirement still applies.
        document = record()
        gate(document, 'software_verified')['verified_at'] = 'not-a-timestamp'
        result = validate(document)
        self.assertNotIn('after the record was written', ' '.join(result['errors']))
        self.assertNotIn('was authorized', ' '.join(result['errors']))


class VerificationControlTest(unittest.TestCase):
    """A gate may not be authorized while its own controls were not exercised."""

    def test_the_real_gate_is_not_authorized_when_artifacts_were_not_verified(self):
        document = record()
        result = validate(document)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertFalse(result['real_recording_verified'])
        self.assertFalse(result['artifacts_verification_requested'])
        self.assertIn('artifact verification was not requested',
                      ' '.join(item['reason'] or '' for item in result['unauthorized_claims']))

    def test_the_real_gate_is_not_authorized_when_the_repository_was_not_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate(document, verify_artifacts=True)
            self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
            self.assertFalse(result['real_recording_verified'])
            self.assertFalse(result['repository_scanned'])
            self.assertTrue(any('repository was not scanned' in item for item in result['gaps']))

    def test_a_complete_record_reports_that_both_controls_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate_complete(document)
            self.assertTrue(result['artifacts_verification_requested'])
            self.assertTrue(result['repository_scanned'])
            self.assertGreater(result['artifacts_hashed'], 0)

    def test_an_absent_artifact_blocks_complete_and_the_exit_code(self):
        # P0 regression: an artifact that could not be hashed used to still produce
        # `complete` with exit 0. It must now be a blocking gap.
        document = record()
        document['samples'][0]['artifacts'][0]['location'] = 'D:/definitely/not/here.wav'
        with tempfile.TemporaryDirectory() as directory:
            annotation = Path(directory) / 'annotation.json'
            annotation.write_text('{}', encoding='utf-8')
            review = document['human_reviews'][0]['artifact']
            review['location'] = str(annotation)
            review['sha256'] = sha256_file(annotation)
            # The gate cites the same review artifact, so the record stays internally
            # consistent and the only open item is the absent sample artifact.
            for entry in gate(document, 'real_recording_verified')['evidence']:
                if entry['kind'] == 'human_review':
                    entry['location'] = review['location']
                    entry['sha256'] = review['sha256']
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertFalse(result['real_recording_verified'])
        self.assertTrue(any('is not locally present' in item for item in result['gaps']))
        self.assertNotIn('gaps', result['errors'])

    def test_an_absent_human_review_artifact_blocks_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['human_reviews'][0]['artifact']['location'] = 'Z:/nonexistent/annotation.json'
            document['human_reviews'][0]['artifact']['sha256'] = '9' * 64
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        # The gate's human-review evidence no longer resolves to a declared review
        # artifact (its digest moved), so the claim is unauthorized and the record is
        # invalid — a stronger refusal than "the artifact could not be hashed".
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('is not any declared human_reviews artifact', ' '.join(result['errors']))
        self.assertTrue(any('human review REVIEW-0001 artifact' in item for item in result['gaps']))

    def test_an_unhashable_sample_artifact_is_a_blocking_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            # Present but unreadable as a file (a directory cannot be hashed).
            Path(document['samples'][0]['artifacts'][0]['location']).unlink()
            Path(document['samples'][0]['artifacts'][0]['location']).mkdir()
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertTrue(any('is not locally present' in item for item in result['gaps']))

    def test_a_sample_artifact_must_declare_a_digest(self):
        # The contract itself requires the digest, so an omitted one is a shape error
        # rather than something the checker has to discover at verification time.
        document = record()
        del document['samples'][0]['artifacts'][0]['sha256']
        with self.assertRaises(AcceptanceError):
            validate(document, verify_artifacts=True)


class CompleteRecordTest(unittest.TestCase):
    def test_a_fully_authorized_record_validates_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertTrue(result['real_recording_verified'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['gaps'], [])
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
        self.assertIn('Artifact verification requested: **no**', text)
        self.assertIn('Repository scanned for committed recordings: **no**', text)

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
                            for item in result['gaps']))

    def test_a_review_artifact_committed_to_git_is_rejected(self):
        document = record()
        document['human_reviews'][0]['artifact']['in_git'] = True
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must not be committed to Git', ' '.join(result['errors']))


class ArtifactIdentityTest(unittest.TestCase):
    """Two authorized samples must not be one preserved file counted twice."""

    def _second_sample(self, document, digest, location):
        """A second authorized sample that points at the first sample's artifact."""
        first = document['samples'][0]
        duplicate = json.loads(json.dumps(first))
        duplicate['sample_id'] = 'SAMPLE-0002'
        duplicate['duration_ms'] = 6 * 60 * 1000
        duplicate['artifacts'] = [dict(first['artifacts'][0], sha256=digest,
                                       location=location)]
        return duplicate

    def test_two_samples_sharing_one_artifact_digest_is_rejected(self):
        # P1 regression: the human layer was already guarded against reusing a machine
        # artifact digest, but nothing stopped two *samples* from resolving to the same
        # file, which inflates the reported authorized-recording count.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            artifact = document['samples'][0]['artifacts'][0]
            document['samples'].append(self._second_sample(
                document, artifact['sha256'], artifact['location']))
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('is declared by more than one sample', ' '.join(result['errors']))
        self.assertEqual(sorted(result['authorized_samples']),
                         ['SAMPLE-0001', 'SAMPLE-0002'])

    def test_two_samples_with_distinct_artifacts_are_accepted(self):
        # The negative half: the new rule must not reject genuinely distinct samples.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['samples'].append(self._second_sample(
                document, '0' * 64, str(Path(directory) / 'SAMPLE-0002.wav')))
            Path(document['samples'][1]['artifacts'][0]['location']).write_bytes(
                b'fixture SAMPLE-0002 0')
            second = document['samples'][1]['artifacts'][0]
            second['sha256'] = sha256_file(second['location'])
            second['byte_length'] = Path(second['location']).stat().st_size
            document['human_reviews'].append(dict(
                document['human_reviews'][0], review_id='REVIEW-0002', sample_id='SAMPLE-0002'))
            annotation = Path(directory) / 'REVIEW-0002-annotation.json'
            annotation.write_text(json.dumps({'review': 'REVIEW-0002'}), encoding='utf-8')
            document['human_reviews'][1]['artifact'] = dict(
                document['human_reviews'][1]['artifact'],
                location=str(annotation), sha256=sha256_file(annotation))
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertNotIn('more than one sample', ' '.join(result['errors']))

    def test_one_sample_repeating_an_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            audio = document['samples'][0]['artifacts'][0]
            document['samples'][0]['artifacts'].append(dict(audio, kind='log'))
            result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('repeats one preserved file', ' '.join(result['errors']))

    def test_one_annotation_reused_as_two_reviews_is_rejected(self):
        # P2 regression, the human-layer twin of the cross-sample rule: #85 asks for
        # review covering several distinct things, so one preserved annotation reused
        # verbatim as two reviews overstates the human layer.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            second = json.loads(json.dumps(document['human_reviews'][0]))
            second['review_id'] = 'REVIEW-0002'
            second['kind'] = 'speaker_role_review'
            second['annotator'] = 'human-reviewer-B'
            document['human_reviews'].append(second)
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('is declared by more than one review', ' '.join(result['errors']))

    def test_distinct_review_artifacts_are_accepted_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            second = json.loads(json.dumps(document['human_reviews'][0]))
            second['review_id'] = 'REVIEW-0002'
            second['kind'] = 'speaker_role_review'
            annotation = Path(directory) / 'REVIEW-0002-annotation.json'
            annotation.write_text(json.dumps({'review': 'REVIEW-0002'}), encoding='utf-8')
            second['artifact'] = dict(second['artifact'], location=str(annotation),
                                      sha256=sha256_file(annotation))
            document['human_reviews'].append(second)
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertNotIn('more than one review', ' '.join(result['errors']))
        self.assertEqual(result['human_review_count'], 2)


class GateHumanReviewBindingTest(unittest.TestCase):
    """The gate's human-review evidence must be the record's human layer, not any file."""

    def _record_with_stray_gate_evidence(self, directory):
        """Point the gate's `human_review` entry at an unrelated local file."""
        document = materialize(record(), directory)
        stray = Path(directory) / 'not-a-review.txt'
        stray.write_text('unrelated material', encoding='utf-8')
        entry = [item for item in gate(document, 'real_recording_verified')['evidence']
                 if item['kind'] == 'human_review'][0]
        entry['location'] = str(stray)
        entry['sha256'] = sha256_file(stray)
        return document

    def test_gate_human_review_evidence_must_be_a_declared_review_artifact(self):
        # P1 regression: the human_review entry was checked for kind and sample binding
        # but never reconciled with human_reviews[], so arbitrary bytes (correctly
        # digested) could carry the entire real-recording claim while the declared human
        # layer said something else.
        with tempfile.TemporaryDirectory() as directory:
            document = self._record_with_stray_gate_evidence(directory)
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('is not any declared human_reviews artifact', ' '.join(result['errors']))
        self.assertIn('**no**', render_report(result))

    def test_gate_human_review_evidence_without_a_digest_is_unauthorized(self):
        with tempfile.TemporaryDirectory() as directory:
            document = self._record_with_stray_gate_evidence(directory)
            entry = [item for item in gate(document, 'real_recording_verified')['evidence']
                     if item['kind'] == 'human_review'][0]
            entry['sha256'] = None
            result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('declares no sha256', ' '.join(result['errors']))

    def test_a_declared_review_artifact_still_authorizes_the_gate(self):
        # The refusal path must not have closed the legitimate one.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            review = document['human_reviews'][0]
            entry = [item for item in gate(document, 'real_recording_verified')['evidence']
                     if item['kind'] == 'human_review'][0]
            entry['location'] = review['artifact']['location']
            entry['sha256'] = review['artifact']['sha256']
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertTrue(result['real_recording_verified'])

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
                            for item in result['gaps']))


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
                            for item in result['gaps']))

    def test_a_chain_without_a_producing_version_is_reported_open(self):
        document = record()
        document['traceability'][0]['model'] = None
        document['traceability'][0]['policy'] = None
        result = validate(document)
        self.assertTrue(any('names no model or policy version' in item
                            for item in result['gaps']))


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
        findings, _, _, _, _ = scan_secrets(document)
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
        findings, _, _, _, _ = scan_secrets(document)
        self.assertTrue(findings)
        self.assertNotIn(secret, json.dumps(findings))

    def test_a_digest_is_not_mistaken_for_a_credential(self):
        document = record()
        findings, _, _, _, _ = scan_secrets(document)
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
            findings, _, _, _, _ = scan_secrets(document)
            self.assertTrue(any(item['category'] == 'real_recording_in_git' for item in findings))

    def test_scanning_a_repository_root_reports_committed_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / 'data'
            nested.mkdir()
            (nested / 'sample.mp3').write_bytes(b'ID3')
            findings, _, _, _, _ = scan_secrets(record(), repository_root=directory)
            self.assertTrue(any(item['category'] == 'real_recording_in_git' for item in findings))

    def test_a_credential_outside_the_old_extension_allowlist_is_reported(self):
        # P2 regression: the scan only read eight extensions, so a committed credential
        # in a .py/.toml/.ini/.cfg/Dockerfile was silently reported as a clean scan.
        secret = 'Bearer abcdefgh12345678'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('creds.py', 'notes.toml', 'app.ini', 'deploy.cfg', 'Dockerfile',
                         'data.csv', 'plain'):
                (root / name).write_text(f'token = "{secret}"\n', encoding='utf-8')
            document = record()
            document['exposure_scan']['scan_roots'] = [directory]
            result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        reported = ' '.join(item['location'] for item in result['secret_findings'])
        for name in ('creds.py', 'notes.toml', 'app.ini', 'deploy.cfg', 'Dockerfile', 'data.csv',
                     'plain'):
            self.assertIn(name, reported)

    def test_a_binary_file_is_reported_as_unread_not_as_clean(self):
        # P2 regression: an undecodable file was counted as inspected and contributed
        # nothing, so it was indistinguishable from a clean file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'blob.bin').write_bytes(b'\xff\xfe\x00\x01not-text')
            document = record()
            document['exposure_scan']['scan_roots'] = [directory]
            result = validate(document)
        self.assertTrue(any('blob.bin' in entry for entry in result['repository_files_unread']))
        self.assertNotIn('blob.bin', ' '.join(result['errors']))

    def test_an_unreadable_oversized_file_is_an_error_not_a_clean_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            big = root / 'huge.log'
            big.write_text('')
            with open(big, 'wb') as stream:
                stream.seek(9 * 1024 * 1024)
                stream.write(b'\n')
            document = record()
            document['exposure_scan']['scan_roots'] = [directory]
            result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('unverified rather than clean', ' '.join(result['errors']))

    def test_the_detection_policy_sources_are_disclosed_not_silently_dropped(self):
        # Scanning the repository itself skips the files that define or exercise the
        # detection patterns, and says so.
        document = record()
        result = validate(document, repository_root=Path(__file__).resolve().parent.parent)
        self.assertTrue(result['detection_policy_sources_skipped'])
        self.assertTrue(any(entry.endswith('acceptance_evidence.py')
                            for entry in result['detection_policy_sources_skipped']))
        rendered = render_report(result)
        self.assertIn('Detection-policy sources excluded from the scan', rendered)

    def test_a_same_named_file_elsewhere_is_still_scanned(self):
        # The exclusion is matched on the repository-relative path, not the basename, so
        # a copy of a policy-source name in an unrelated tree is scanned like any file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'elsewhere'
            root.mkdir()
            (root / 'test_acceptance_evidence.py').write_text(
                "secret = 'Bearer abcdefgh12345678'\n", encoding='utf-8')
            document = record()
            document['exposure_scan']['scan_roots'] = [directory]
            result = validate(document)
        self.assertTrue(any('test_acceptance_evidence.py' in item['location']
                            for item in result['secret_findings']))
        self.assertEqual(result['detection_policy_sources_skipped'], [])

    def test_directories_the_walk_did_not_enter_are_disclosed(self):
        # P2 regression: `dist`/`build`/`node_modules`/`.git` and friends were excluded
        # from the scan with nothing in the result or the docs saying so, while the
        # record and changelog claimed the policy-source files were "the only exclusion".
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'node_modules').mkdir()
            (root / 'node_modules' / 'leak.py').write_text(
                "secret = 'Bearer abcdefgh12345678'\n", encoding='utf-8')
            (root / 'clean.txt').write_text('clean\n', encoding='utf-8')
            document = record()
            document['exposure_scan']['scan_roots'] = [directory]
            result = validate(document)
        disclosed = result['repository_directories_skipped']
        self.assertTrue(any(Path(entry).name == 'node_modules' for entry in disclosed))
        # Only the one non-skipped file was inspected.
        self.assertEqual(result['repository_files_inspected'], 1)
        self.assertTrue(any(Path(entry).name == 'node_modules'
                            for entry in disclosed))
        text = render_report(result)
        self.assertIn('Directories the scan did not descend into', text)
        self.assertIn('`node_modules`', text)

    def test_the_policy_source_constants_carry_no_dead_entry_point(self):
        # P2 regression: a basename-keyed constant was left behind by the round-4
        # path-matching refactor and matched nothing.
        from aivoicebench import acceptance_evidence as module
        self.assertFalse(hasattr(module, 'DETECTION_POLICY_SOURCE_NAMES'))
        self.assertTrue(module.DETECTION_POLICY_SOURCE_PATHS)


class RepositoryControlTest(unittest.TestCase):
    """A control that was not exercised must never be reported as exercised."""

    @staticmethod
    def _run_cli(argv):
        from aivoicebench.__main__ import main
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_a_nonexistent_repository_root_is_an_error_not_a_skipped_control(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            missing = Path(directory) / 'no-such-repo'
            result = validate(document, verify_artifacts=True, repository_root=missing)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertFalse(result['repository_scanned'])
        self.assertIn('does not exist or is not a directory', ' '.join(result['errors']))

    def test_a_repository_root_that_is_a_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            a_file = Path(directory) / 'not-a-directory.txt'
            a_file.write_text('x', encoding='utf-8')
            result = validate(document, verify_artifacts=True, repository_root=a_file)
        self.assertFalse(result['repository_scanned'])
        self.assertIn('does not exist or is not a directory', ' '.join(result['errors']))

    def test_a_walked_repository_root_reports_the_root_and_file_count(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate_complete(document)
        self.assertTrue(result['repository_scanned'])
        self.assertEqual(result['repository_root'], str(Path(__file__).resolve().parent))
        self.assertGreater(result['repository_files_inspected'], 0)

    def test_a_root_named_twice_is_walked_and_counted_once(self):
        # P2 regression: declaring the same directory in exposure_scan.scan_roots and
        # passing it as --repository-root walked it twice, inflating the inspected
        # count that documents how much evidence the control actually covered.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'scan-me'
            root.mkdir()
            for index in range(5):
                (root / f'file-{index}.txt').write_text('clean\n', encoding='utf-8')
            document = materialize(record(), directory)
            document['exposure_scan']['scan_roots'] = [str(root)]
            declared_only = validate(document, repository_root=None)
            declared_and_passed = validate(document, repository_root=root)
        self.assertEqual(declared_only['repository_files_inspected'], 5)
        self.assertEqual(declared_and_passed['repository_files_inspected'], 5)

    def test_a_declared_scan_root_that_does_not_exist_is_a_blocking_gap(self):
        # A declared clean scan whose scan_root was never walked covers less than it
        # claims, so it cannot be the evidence for the real-recording gate.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['exposure_scan']['scan_roots'] = [str(Path(directory) / 'never-scanned')]
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertFalse(result['real_recording_verified'])
        self.assertTrue(any('do not resolve to directories' in item for item in result['gaps']))

    def test_a_declared_scan_root_that_exists_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            # The declared root is a separate, clean directory: scanning the directory
            # that holds the sample audio would (correctly) report that audio.
            clean_root = Path(directory) / 'release-artifacts'
            clean_root.mkdir()
            document['exposure_scan']['scan_roots'] = [str(clean_root)]
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_COMPLETE)

    def test_audio_inside_a_declared_scan_root_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['exposure_scan']['scan_roots'] = [directory]
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('real_recording_in_git', ' '.join(result['errors']))

    def test_the_cli_refuses_a_nonexistent_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            record_path = Path(directory) / 'acceptance.json'
            record_path.write_text(json.dumps(document), encoding='utf-8')
            code, output = self._run_cli([
                'acceptance', 'check', str(record_path), '--verify-artifacts',
                '--repository-root', str(Path(directory) / 'no-such-repo')])
            self.assertEqual(code, 1)
            self.assertNotIn('real_recording_verified=True', output)


class EvidenceReferenceTest(unittest.TestCase):
    """A real gate is only as real as the artifact behind its evidence."""

    def test_evidence_that_resolves_to_nothing_is_a_blocking_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            gate(document, 'real_cloud_verified')['evidence'][0]['location'] = (
                'outside-git://totally-made-up/never-ran.json')
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertFalse(result['real_recording_verified'])
        self.assertTrue(any('does not resolve to a locally present artifact' in item
                            for item in result['gaps']))

    def test_a_denominator_evidence_that_resolves_to_nothing_is_a_blocking_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['denominators'][1]['evidence'][0]['location'] = 'outside-git://invented/stage.json'
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertTrue(any('does not resolve to a locally present artifact' in item
                            for item in result['gaps']))

    def test_an_evidence_digest_mismatch_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            gate(document, 'real_cloud_verified')['evidence'][0]['sha256'] = '0' * 64
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('does not match the preserved artifact', ' '.join(result['errors']))

    def test_evidence_without_a_declared_digest_is_a_blocking_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            gate(document, 'real_cloud_verified')['evidence'][0]['sha256'] = None
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertTrue(any('declares no sha256' in item for item in result['gaps']))


class StageAccountabilityTest(unittest.TestCase):
    """Every declared evaluation needs its denominator, whatever its status."""

    def _add_evaluation(self, document, status):
        document['evaluations'].append({
            'evaluation_id': 'EVAL-0002',
            'kind': 'semantic_judge',
            'status': status,
            'sample_ids': ['SAMPLE-0001'],
            'evidence': [evidence('EV-0099', 'human_review')],
            'measures': None,
            'unresolved': [],
            'reason': f'{status} during the authorized run',
        })
        return document

    def test_an_evaluation_needs_its_stage_denominator_whatever_its_status(self):
        for status in ('failed', 'abstained', 'not_attempted', 'measured', 'partial'):
            with self.subTest(status=status):
                document = self._add_evaluation(record(), status)
                document['denominators'] = [entry for entry in document['denominators']
                                            if entry['stage'] != 'semantic_judge']
                result = validate(document)
                self.assertEqual(result['status'], STATUS_INVALID)
                self.assertIn('semantic_judge stage has no reported denominator',
                              ' '.join(result['errors']))

    def test_a_declared_stage_with_its_denominator_is_accepted(self):
        document = self._add_evaluation(record(), 'failed')
        for entry in document['denominators']:
            if entry['stage'] == 'semantic_judge':
                entry['counters'] = {'complete': 0, 'partial': 0, 'failed': 1, 'unknown': 0,
                                     'abstained': 0, 'not_applicable': 0}
        with tempfile.TemporaryDirectory() as directory:
            result = validate_complete(materialize(document, directory))
        self.assertEqual(result['status'], STATUS_COMPLETE)

    def test_an_m1_record_must_account_for_every_stage(self):
        for stage in M1_STAGES:
            with self.subTest(stage=stage):
                document = record()
                document['denominators'] = [entry for entry in document['denominators']
                                            if entry['stage'] != stage]
                result = validate(document)
                self.assertEqual(result['status'], STATUS_INVALID)
                self.assertIn('must account for every stage', ' '.join(result['errors']))
                self.assertIn(stage, ' '.join(result['errors']))

    def test_a_stage_may_be_reported_as_not_applicable(self):
        document = record()
        for entry in document['denominators']:
            if entry['stage'] == 'browser_navigation':
                entry['counters'] = {'complete': 0, 'partial': 0, 'failed': 0, 'unknown': 0,
                                     'abstained': 0, 'not_applicable': 1}
        with tempfile.TemporaryDirectory() as directory:
            result = validate_complete(materialize(document, directory))
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertIn('browser_navigation', result['stages_reported'])


class ArtifactVerificationTest(unittest.TestCase):
    def test_matching_artifact_hashes_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
            # both sample artifacts plus the human-review artifact
            self.assertEqual(result['artifacts_hashed'], 3)
            self.assertEqual(result['status'], STATUS_COMPLETE)

    def test_a_mismatched_artifact_hash_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            audio = Path(document['samples'][0]['artifacts'][0]['location'])
            audio.write_bytes(b'RIFF-a-different-recording')
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
            self.assertEqual(result['status'], STATUS_INVALID)
            self.assertIn('does not match the preserved artifact', ' '.join(result['errors']))

    def test_an_absent_artifact_is_reported_instead_of_assumed_to_match(self):
        document = record()
        document['samples'][0]['artifacts'][0]['location'] = 'D:/definitely/not/here.wav'
        result = validate(document, verify_artifacts=True)
        self.assertTrue(any('is not locally present' in item for item in result['gaps']))

    def test_a_declared_byte_length_that_contradicts_the_artifact_is_rejected(self):
        # P1 regression: `byte_length` was declared by the contract and populated by
        # the fixtures, but never compared with the preserved file, so a record could
        # declare any size while still pointing at the right bytes and pass.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            audio = document['samples'][0]['artifacts'][0]
            actual = Path(audio['location']).stat().st_size
            audio['byte_length'] = actual + 15_000_000
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('declared byte_length', ' '.join(result['errors']))
        self.assertIn('does not match the preserved artifact', ' '.join(result['errors']))

    def test_a_matching_byte_length_is_accepted(self):
        # The positive half of the same control: reconciling the size must not be the
        # same as rejecting it.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            audio = document['samples'][0]['artifacts'][0]
            audio['byte_length'] = Path(audio['location']).stat().st_size
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertNotIn('byte_length', ' '.join(result['errors']))

    def test_an_authorized_audio_artifact_must_declare_its_size(self):
        # The one quantity of an authorized sample this checker can measure must not be
        # exempt from the contract.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['samples'][0]['artifacts'][0]['byte_length'] = None
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_REAL_RECORDING_PENDING)
        self.assertFalse(result['real_recording_verified'])
        self.assertTrue(any('declares no byte_length' in item for item in result['gaps']))

    def test_a_byte_length_that_contradicts_a_non_audio_sample_artifact_is_rejected(self):
        # The size control is not audio-only: any declared sample artifact size is
        # reconciled, so the transcript cannot declare a size the file does not have.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            transcript = document['samples'][0]['artifacts'][1]
            transcript['byte_length'] = Path(transcript['location']).stat().st_size + 12345
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('declared byte_length', ' '.join(result['errors']))


class DenominatorCoverageTest(unittest.TestCase):
    def test_an_omitted_stage_for_a_measured_evaluation_is_rejected(self):
        # P1 regression: an omitted stage used to look identical to "nothing failed".
        document = record()
        document['denominators'] = [entry for entry in document['denominators']
                                    if entry['stage'] != 'acoustic_boundary']
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('acoustic_boundary stage has no reported denominator',
                      ' '.join(result['errors']))

    def test_no_denominator_at_all_is_a_blocking_gap(self):
        document = record()
        document['denominators'] = []
        result = validate(document)
        self.assertTrue(any('no stage denominator was reported at all' in item
                            for item in result['gaps']))

    def test_a_record_claiming_the_real_gate_must_report_an_evaluation(self):
        document = record()
        document['evaluations'] = []
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('must report at least one evaluation', ' '.join(result['errors']))

    def test_a_record_claiming_nothing_may_report_nothing(self):
        template = blank_record('ACC-M1-TEMPLATE', issue_url=ISSUE_URL, recorded_by='lybym',
                                product_version='0.5.0', code_commit=COMMIT)
        result = validate(template)
        self.assertEqual(result['status'], STATUS_NO_AUTHORIZED_RECORDING)
        self.assertEqual(result['errors'], [])


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

    def test_the_result_discloses_which_conditions_are_declared_only(self):
        # P1 regression: the 5-20 minute band is evaluated against the author's own
        # `duration_ms`, and that limit existed only in prose. The result now states it
        # in machine-readable form, and the rendering repeats it.
        result = validate(record())
        disclosure = ' '.join(result['declared_only_controls'])
        self.assertIn('duration_ms', disclosure)
        self.assertIn('5–20 minute band', disclosure)
        self.assertIn('declared value only', disclosure)
        self.assertIn('Conditions checked against declared values', render_report(result))

    def test_the_clean_flag_disclosure_matches_what_the_code_does(self):
        # P2 regression: the disclosure claimed `exposure_scan.clean` was "re-derived",
        # but validate() never rewrites the record's flag — it only contradicts it. The
        # wording is asserted here so it cannot drift from the behaviour again.
        result = validate(record())
        disclosure = ' '.join(result['declared_only_controls'])
        self.assertIn('exposure_scan.clean', disclosure)
        self.assertNotIn('re-derived when', disclosure)
        self.assertIn('never rewrites it', disclosure)

    def test_a_clean_flag_contradicted_by_the_scan_is_an_error_not_a_rewrite(self):
        document = record()
        document['notes'] = 'sk-abcdefghijklmnop'
        result = validate(document)
        self.assertEqual(result['status'], STATUS_INVALID)
        # The record's own flag is reported as contradicted, and the record is not
        # silently corrected into a non-clean scan.
        self.assertTrue(document['exposure_scan']['clean'])
        self.assertIn('the record declares a clean scan but the scan found',
                      ' '.join(result['errors']))

    def test_a_declared_duration_is_not_reported_as_a_measured_one(self):
        # The artifact's size now agrees with the sample's own stated parameters, and
        # the band is still a declared-value check: the result must say so rather than
        # imply the checker measured the recording.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            artifact = document['samples'][0]['artifacts'][0]
            self.assertEqual(artifact['byte_length'], Path(artifact['location']).stat().st_size)
            result = validate_complete(document)
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertTrue(any('not a measured one' in item
                            for item in result['declared_only_controls']))

    def test_a_declared_size_that_contradicts_the_sample_parameters_is_rejected(self):
        # P1 regression: duration, rate, channels and encoding imply a size for raw PCM,
        # so a tiny artifact cannot be declared as eight minutes of 16 kHz mono audio.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['samples'][0]['artifacts'][0]['byte_length'] = 48
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn("contradicts the sample's own parameters", ' '.join(result['errors']))

    def test_a_declared_size_matching_the_sample_parameters_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            sample = document['samples'][0]
            artifact = sample['artifacts'][0]
            artifact['byte_length'] = int(implied_pcm_bytes(sample))
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertNotIn("contradicts the sample's own parameters", ' '.join(result['errors']))

    def test_a_compressed_encoding_is_not_judged_by_pcm_arithmetic(self):
        # No size arithmetic exists for compressed encodings; the checker must not invent
        # one. The record is made internally consistent (declared size == file size) so the
        # assertion is about the *absent* PCM message and nothing else — asserting it on a
        # record that is invalid for an unrelated reason would pass vacuously.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            sample = document['samples'][0]
            artifact = sample['artifacts'][0]
            sample['encoding'] = 'MP3'
            path = Path(artifact['location'])
            path.write_bytes(path.read_bytes()[:4096])
            artifact['byte_length'] = 4096
            artifact['sha256'] = sha256_file(path)
            sample['duration_ms'] = 8 * 60 * 1000
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertEqual(result['errors'], [])
        self.assertNotIn("contradicts the sample's own parameters", ' '.join(result['errors']))
        # ... and the non-application is named rather than silent.
        self.assertEqual([item['sample_id']
                          for item in result['audio_size_arithmetic_applied']], [])
        self.assertIn({'sample_id': 'SAMPLE-0001', 'encoding': 'MP3'},
                      result['audio_size_arithmetic_not_applicable'])

    def test_an_unrecognised_encoding_spelling_is_disclosed_as_not_applied(self):
        # P1 regression: `encoding` is free text, so a sample could sidestep the size
        # arithmetic *and* report "every control ran" merely by spelling its encoding
        # differently ('wav' instead of 'PCM_S16LE'). It could not be checked before; now
        # it cannot be checked *silently*.
        for encoding in ('wav', 'PCM_S16LE '):
            with self.subTest(encoding=encoding):
                with tempfile.TemporaryDirectory() as directory:
                    document = materialize(record(), directory)
                    sample = document['samples'][0]
                    artifact = sample['artifacts'][0]
                    sample['encoding'] = encoding
                    sample['duration_ms'] = 8 * 60 * 1000
                    path = Path(artifact['location'])
                    path.write_bytes(b'not really eight minutes')
                    artifact['byte_length'] = path.stat().st_size
                    artifact['sha256'] = sha256_file(path)
                    result = validate(document, verify_artifacts=True,
                                      repository_root=Path(__file__).resolve().parent)
                self.assertEqual(result['audio_size_arithmetic_applied'], [])
                self.assertEqual(
                    [item['sample_id'] for item in result['audio_size_arithmetic_not_applicable']],
                    ['SAMPLE-0001'])
                disclosure = ' '.join(result['declared_only_controls'])
                self.assertIn('audio_size_arithmetic_not_applicable', disclosure)
                text = render_report(result)
                self.assertIn('Audio size arithmetic NOT applied to', text)
                self.assertIn('`SAMPLE-0001`', text)

    def test_a_record_declaring_its_own_exposure_finding_is_not_clean(self):
        # P1 regression: `exposure_scan.findings` was never read, so a record could
        # record a committed private recording AND declare `clean: true` and still be
        # reported complete — authorized by exactly the condition PRD-N004 forbids.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['exposure_scan']['findings'] = [{
                'location': 'recordings/private-session.wav',
                'category': 'real_recording_in_git',
                'detail': 'committed private recording',
                'resolution': None,
            }]
            document['exposure_scan']['clean'] = True
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('the record itself declares 1 exposure finding(s)',
                      ' '.join(result['errors']))
        self.assertIn('declares `clean: true` at the same time', ' '.join(result['errors']))

    def test_an_exposure_finding_without_a_clean_claim_is_still_not_evidence(self):
        # The record cannot be acceptance evidence even when it does not also claim
        # clean: a recorded finding means the scan it rests on found prohibited material.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['exposure_scan']['clean'] = False
            document['exposure_scan']['findings'] = [{
                'location': 'recordings/private-session.wav',
                'category': 'real_recording_in_git',
            }]
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertIn('real_recording_in_git', ' '.join(result['errors']))

    def test_an_empty_findings_list_still_reports_clean(self):
        # The refusal must not reject the normal clean record.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            self.assertEqual(document['exposure_scan']['findings'], [])
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_COMPLETE)
        self.assertNotIn('exposure finding', ' '.join(result['errors']))

    def test_a_measured_evaluation_on_a_zero_total_stage_is_rejected(self):
        # P1 regression: every M1 stage reported with expected_total = 0 accounted for
        # every stage while measuring none, and a `measured` evaluation still passed.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            for entry in document['denominators']:
                entry['counters'] = {key: 0 for key in entry['counters']}
                entry['expected_total'] = 0
                entry['evidence'] = []
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['status'], STATUS_INVALID)
        self.assertFalse(result['real_recording_verified'])
        self.assertIn('reports a denominator of 0 records', ' '.join(result['errors']))

    def test_a_non_measured_evaluation_on_a_zero_total_stage_is_allowed(self):
        # A stage that did not apply may legitimately be evaluated against nothing, as
        # long as the record does not also claim a measurement on it.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['evaluations'][0]['status'] = 'not_attempted'
            document['evaluations'][0]['reason'] = 'stage did not apply to this recording'
            for entry in document['denominators']:
                entry['counters'] = {key: 0 for key in entry['counters']}
                entry['counters']['not_applicable'] = 1
                entry['expected_total'] = 1
                entry['evidence'] = []
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertNotIn('reports a denominator of 0 records', ' '.join(result['errors']))
        self.assertNotIn('denominator of 0', ' '.join(result['errors']))

    def test_a_pcm_sample_reports_that_the_size_arithmetic_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['audio_size_arithmetic_not_applicable'], [])
        self.assertEqual([item['encoding'] for item in result['audio_size_arithmetic_applied']],
                         ['PCM_S16LE'])
        self.assertNotIn('Audio size arithmetic NOT applied to', render_report(result))

    def test_a_pcm_sample_whose_audio_declares_no_size_is_named_as_not_applied(self):
        # P2 regression: `applied` used to name the sample as soon as *any* audio
        # artifact declared a size, so a sample whose audio declared none was still read
        # as "its size relation was checked".
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['samples'][0]['artifacts'][0]['byte_length'] = None
            result = validate(document, verify_artifacts=True,
                              repository_root=Path(__file__).resolve().parent)
        self.assertEqual(result['audio_size_arithmetic_applied'], [])
        self.assertEqual(
            [item['sample_id'] for item in result['audio_size_arithmetic_not_applicable']],
            ['SAMPLE-0001'])


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
            document = materialize(record(), directory)
            record_path = Path(directory) / 'acceptance.json'
            record_path.write_text(json.dumps(document), encoding='utf-8')
            code, output = self._run(['acceptance', 'check', str(record_path),
                                      '--verify-artifacts',
                                      '--repository-root', str(Path(__file__).resolve().parent)])
            self.assertEqual(code, 0)
            self.assertIn('real_recording_verified=True', output)

    def test_check_does_not_exit_0_when_the_artifacts_were_never_verified(self):
        # P0 regression: this used to print real_recording_verified=True and exit 0
        # for a record whose authorized sample artifacts were never hashed.
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            document['samples'][0]['artifacts'][0]['location'] = str(
                Path(directory) / 'missing.wav')
            record_path = Path(directory) / 'acceptance.json'
            record_path.write_text(json.dumps(document), encoding='utf-8')
            code, output = self._run(['acceptance', 'check', str(record_path),
                                      '--verify-artifacts',
                                      '--repository-root', str(Path(__file__).resolve().parent)])
            self.assertEqual(code, 2)
            self.assertIn('real_recording_verified=False', output)
            self.assertIn('BLOCKING', output)

    def test_check_does_not_exit_0_without_requesting_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            document = materialize(record(), directory)
            record_path = Path(directory) / 'acceptance.json'
            record_path.write_text(json.dumps(document), encoding='utf-8')
            code, output = self._run(['acceptance', 'check', str(record_path),
                                      '--repository-root', str(Path(__file__).resolve().parent)])
            self.assertEqual(code, 2)
            self.assertIn('real_recording_verified=False', output)
            self.assertIn('artifact verification was not requested', output)

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
