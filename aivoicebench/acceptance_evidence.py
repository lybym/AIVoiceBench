"""M1 authorized real-recording acceptance evidence (Issue #85).

Issue #85 is the final evidence gate for Recording Analysis / M1. Its acceptance
criteria are *evidence* criteria: authorized 5–20 minute real recordings, an
end-to-end traceable conclusion chain, explicit denominators that include
failures and abstentions, human annotations preserved separately from machine
originals, an immutable role-correction revision with recompute/diff proof, and a
final statement that separates ``software_verified`` / ``container_verified`` /
``browser_verified`` / ``real_cloud_verified`` / ``real_recording_verified``
from the gates that remain pending.

**This module does not manufacture that evidence.** It is the deterministic
checker and record keeper for it, in the same spirit as
:mod:`aivoicebench.vad_evaluation`: given an acceptance record it verifies that
each claimed gate is authorized by the evidence the claim requires, that the
denominators reconcile, that human review is not machine output in disguise, and
that no credential, signed URL or private recording would be committed. With no
authorized recording present it reports ``real_recording_pending`` — a claim of
``real_recording_verified`` backed only by fixtures, mocks, CI or machine
annotations is reported as an invalid claim, never as a pass.

The record format is ``schemas/acceptance-evidence.schema.json``
(``AcceptanceRecord 1.0.0``). The M1 refusal to close on synthetic fixtures,
mocks, preview releases or a single diagnostic recording is enforced here rather
than left to review discipline.
"""

import hashlib
import json
import re
from pathlib import Path

from .validation import schema_errors

#: Contract version of the acceptance record.
SCHEMA_VERSION = '1.0.0'

#: Evaluation policy version. Bump when a claim rule changes, because the pass or
#: fail of an acceptance record depends on the policy version that judged it.
EVIDENCE_POLICY_VERSION = '1.0.0'

#: The staged gates, in the order they become decidable.
GATES = (
    'software_verified',
    'container_verified',
    'browser_verified',
    'real_cloud_verified',
    'real_recording_verified',
)

#: The M1 authorized-recording duration band, in milliseconds (Issue #85 scope).
MIN_SAMPLE_MS = 5 * 60 * 1000
MAX_SAMPLE_MS = 20 * 60 * 1000

#: Sample provenance values that are *not* real recordings and can never
#: authorize the real-recording gate.
NON_REAL_SAMPLE_SOURCES = ('synthetic', 'fixture', 'imported_unknown')

#: Evidence kinds that describe material a human produced and decided on.
HUMAN_EVIDENCE_KINDS = ('human_review',)

#: Overall record status.
STATUS_COMPLETE = 'complete'
STATUS_REAL_RECORDING_PENDING = 'real_recording_pending'
STATUS_NO_AUTHORIZED_RECORDING = 'no_authorized_recording'
STATUS_INVALID = 'invalid'

#: Reasons reported instead of a result.
CLAIM_NOT_AUTHORIZED = 'claim_not_authorized'
UNRECONCILED_DENOMINATOR = 'unreconciled_denominator'
SECRET_FINDING = 'secret_finding'
ARTIFACT_HASH_MISMATCH = 'artifact_hash_mismatch'

REAL_AUDIO_SUFFIXES = ('.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac', '.opus', '.wma', '.amr')

_TOKEN_VALUE = re.compile(r'^(?:sk|rk|ak|pk)-[A-Za-z0-9_-]{12,}$')
_CREDENTIAL_VALUE = re.compile(r'(?:^|[^A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|'
                               r'gh[a-z]_[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{8,})')
#: Field *names* that hold credential material. This is deliberately narrower than
#: `validation._CREDENTIAL_KEYS`: an acceptance record legitimately carries an
#: `authorization` block ("who authorized this recording") and a `secret_scan`
#: style report, and neither is a credential. A field is only reported when its
#: name says it holds a key, secret or token.
_CREDENTIAL_KEY = re.compile(r'(?:^|_)(?:api_?key|apikey|access_key|access_key_id|secret_key|'
                             r'client_secret|app_secret|private_key|password|passwd|'
                             r'(?:access|refresh|auth|bearer|api|session)_token|token)(?:$|_)',
                             re.IGNORECASE)
#: Values whose text is a digest, not a secret: a 64-hex sha256 legitimately starts
#: with 'a'-'f', so a naive key scan must never read it as credential material.
_DIGEST_FIELD = re.compile(r'(?:^|_)(?:sha256|digest|hash|etag|md5)(?:$|_)', re.IGNORECASE)
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
#: A short-lived signed URL is a bearer credential for its validity window, so a
#: private recording's signed URL is credential material even though it is not a
#: long-lived key.
_SIGNED_URL = re.compile(r'(?:[?&](?:X-Amz-Signature|X-Amz-Credential|Signature|sig|'
                         r'X-Tos-Signature|volc-|Authorization)=)', re.IGNORECASE)


class AcceptanceError(ValueError):
    """The acceptance record is unusable (shape, identity or claim conflict)."""


def sha256_file(path):
    """Digest one file. Used to bind a claim to the exact preserved artifact."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_record(document):
    """Validate the contract shape of an ``AcceptanceRecord`` document."""
    errors = schema_errors(document, 'acceptance-evidence')
    if errors:
        raise AcceptanceError('Invalid acceptance record: ' + '; '.join(errors))
    return document


def load_record_file(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as error:
        raise AcceptanceError(f'Cannot read acceptance record {path}: {error}') from None
    return load_record(document)


def _is_digest(text):
    return bool(text) and bool(re.fullmatch(r'[a-fA-F0-9]{32,}', text))


def _scan_text(text, location, findings):
    if not isinstance(text, str):
        return
    if _PRIVATE_KEY.search(text):
        findings.append({'location': location, 'category': 'private_key',
                         'detail': 'PEM private key material must never be persisted'})
    if _SIGNED_URL.search(text):
        findings.append({'location': location, 'category': 'signed_url',
                         'detail': 'a signed URL is a bearer credential for its validity window'})
    if _CREDENTIAL_VALUE.search(text):
        findings.append({'location': location, 'category': 'credential_shaped_value',
                         'detail': 'credential-shaped value must not be persisted'})


def _scan_record(node, path, findings):
    """Walk the record itself, reporting only locations.

    The record is what may be committed, so it is scanned independently of the
    filesystem: a credential pasted into an evidence reference is a leak even when
    no file is reachable locally.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            location = f'{path}/{key}'
            if isinstance(value, str) and value:
                before = len(findings)
                _scan_text(value, location, findings)
                # Only a value that carried no more specific material is judged by
                # its field name, so a signed URL is never downgraded to a generic
                # credential-shaped value.
                if len(findings) == before and not _DIGEST_FIELD.search(str(key)):
                    if _CREDENTIAL_KEY.search(str(key)) or _TOKEN_VALUE.match(value):
                        findings.append({'location': location,
                                         'category': 'credential_field',
                                         'detail': 'credential-shaped field must not be persisted'})
            else:
                _scan_text(value, location, findings)
            _scan_record(value, location, findings)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _scan_record(value, f'{path}/{index}', findings)
    elif isinstance(node, str):
        _scan_text(node, path, findings)


def scan_secrets(document, *, repository_root=None):
    """PRD-N004 evidence: no credential, signed URL or private recording leaks.

    Scans the record tree and, when given, a set of files/directories on disk. Only
    the location and category are reported: echoing the offending text would move
    the secret into the report.
    """
    findings = []
    _scan_record(document, '', findings)
    scan_roots = document.get('exposure_scan', {}).get('scan_roots') or []
    for entry in scan_roots:
        root = Path(entry)
        if not root.exists():
            continue
        targets = [root] if root.is_file() else sorted(p for p in root.rglob('*') if p.is_file())
        for target in targets:
            if target.suffix.lower() in REAL_AUDIO_SUFFIXES:
                findings.append({
                    'location': str(target),
                    'category': 'real_recording_in_git',
                    'detail': 'audio artifacts must stay outside the repository and outside release '
                              'artifacts',
                })
                continue
            if target.suffix.lower() not in ('.json', '.yaml', '.yml', '.txt', '.md', '.log', '.env'):
                continue
            try:
                text = target.read_text(encoding='utf-8-sig', errors='replace')
            except OSError:
                continue
            _scan_text(text, str(target), findings)
    if repository_root is not None:
        root_path = Path(repository_root)
        if root_path.exists():
            tracked = sorted(p for p in root_path.rglob('*') if p.is_file())
            for target in tracked:
                if target.suffix.lower() in REAL_AUDIO_SUFFIXES:
                    findings.append({
                        'location': str(target),
                        'category': 'real_recording_in_git',
                        'detail': 'audio artifacts must stay outside the repository',
                    })
    return findings


def _evidence_ids(entries):
    return [entry['evidence_id'] for entry in entries or []]


def _evidence_kinds(entries):
    return {entry['kind'] for entry in entries or []}


def _sample_index(document):
    return {sample['sample_id']: sample for sample in document.get('samples') or []}


def _is_authorized_real(sample):
    return sample['source'] in ('authorized_real_recording', 'authorized_field_recording')


def _artifact_paths(sample):
    return [artifact['location'] for artifact in sample.get('artifacts') or []]


def _gate(document, name):
    for gate in document.get('gates') or []:
        if gate['gate'] == name:
            return gate
    return None


def _all_evidence(document):
    """Every evidence reference declared anywhere in the record.

    Real-gate claims are authorized by these references, so they are collected in
    one place and can be cross-checked against the ids a traceability chain cites
    and against the samples a real claim must be bound to.
    """
    collected = []

    def add(entries, location):
        for index, entry in enumerate(entries or []):
            collected.append((f'{location}/{index}', entry))

    for key in ('evaluations', 'denominators'):
        for index, item in enumerate(document.get(key) or []):
            add(item.get('evidence'), f'/{key}/{index}/evidence')
    for index, correction in enumerate(document.get('manual_role_corrections') or []):
        add(correction.get('recompute_evidence'), f'/manual_role_corrections/{index}/recompute_evidence')
        add(correction.get('diff_evidence'), f'/manual_role_corrections/{index}/diff_evidence')
    for index, gate in enumerate(document.get('gates') or []):
        add(gate.get('evidence'), f'/gates/{index}/evidence')
    return collected


def _check_evidence_identity(document, errors):
    """Every evidence id must be unique and bound where a real claim requires it."""
    seen = {}
    for location, entry in _all_evidence(document):
        evidence_id = entry['evidence_id']
        if evidence_id in seen:
            errors.append(f'{location}/evidence_id: {evidence_id!r} is already declared at '
                          f'{seen[evidence_id]}; evidence ids must be unique so a reference is '
                          'unambiguous')
        else:
            seen[evidence_id] = location
    return seen


def _verify_artifact_hashes(document, errors, gaps):
    """Re-hash every declared artifact that is locally reachable.

    Both the sample artifacts and the human-review artifact are covered: the human
    layer is what the real-recording claim ultimately rests on, so a review artifact
    that cannot be hashed must not pass unchecked. An artifact that is not present
    locally cannot be re-hashed; that is a **blocking** gap, not an informational
    note, because the digest is the only control binding a declared sample to actual
    audio.
    """
    checked = 0
    targets = []
    for sample in document.get('samples') or []:
        for artifact in sample.get('artifacts') or []:
            targets.append((f'sample {sample["sample_id"]} artifact', artifact))
    for index, review in enumerate(document.get('human_reviews') or []):
        targets.append((f'human review {review["review_id"]} artifact', review['artifact']))
    for label, artifact in targets:
        location = artifact['location']
        path = Path(location)
        if not path.is_file():
            gaps.append(f'{label} {location} is not locally present, so its declared sha256 cannot '
                        'be re-verified')
            continue
        try:
            actual = sha256_file(path)
        except OSError as error:
            gaps.append(f'{label} {location} is unreadable, so its declared sha256 cannot be '
                        f're-verified: {error}')
            continue
        checked += 1
        declared = artifact['sha256']
        if actual != declared:
            # The contract already requires a digest, so a missing one cannot reach
            # here; a mismatch means the declared artifact is not the preserved one.
            errors.append(
                f'{label} {location}: declared sha256 does not match the preserved artifact')
    return checked


def _authorized_real_samples(document):
    return [sample for sample in document.get('samples') or [] if _is_authorized_real(sample)]


def _evidence_bound_to(entries, kind, sample_ids):
    """Evidence of ``kind`` that structurally names one of ``sample_ids``.

    Binding is an explicit ``sample_id`` field, not a substring of a path or id: a
    path that happens to contain a sample name must not authorize a real gate.
    """
    bound = []
    for entry in entries or []:
        if entry['kind'] != kind:
            continue
        if entry.get('sample_id') and entry['sample_id'] in sample_ids:
            bound.append(entry)
    return bound


def _check_real_cloud_gate(gate, document, errors, claim_checks):
    evidence = gate.get('evidence') or []
    kinds = _evidence_kinds(evidence)
    if 'real_cloud_invocation' not in kinds:
        errors.append(
            '/gates/real_cloud_verified: a real cloud gate requires at least one '
            '`real_cloud_invocation` evidence reference; a scripted transport, mock or fixture is '
            'not a cloud call')
        claim_checks.append({'gate': gate['gate'], 'authorized': False,
                             'reason': 'no real_cloud_invocation evidence'})
        return
    real_sample_ids = {sample['sample_id'] for sample in _authorized_real_samples(document)}
    other_sample_ids = {sample['sample_id'] for sample in document.get('samples') or []}
    if not real_sample_ids:
        errors.append(
            '/gates/real_cloud_verified: the real cloud gate is claimed but the record declares no '
            'authorized real recording to call the service for')
        claim_checks.append({'gate': gate['gate'], 'authorized': False,
                             'reason': 'no authorized real sample to bind the cloud call to'})
        return
    if not _evidence_bound_to(evidence, 'real_cloud_invocation', real_sample_ids):
        # Naming a synthetic or unrelated sample is the documented refusal: a cloud
        # call about audio that is not the authorized sample proves nothing about it.
        bound_elsewhere = _evidence_bound_to(evidence, 'real_cloud_invocation', other_sample_ids)
        reason = ('no `real_cloud_invocation` evidence is bound to an authorized real sample'
                  if not bound_elsewhere else
                  'the cloud evidence is bound to a non-authorized (synthetic/fixture) sample')
        errors.append(f'/gates/real_cloud_verified: {reason}; a real cloud claim must name the '
                      'authorized sample it was run for, and a synthetic sample cannot authorize a '
                      'real cloud gate')
        claim_checks.append({'gate': gate['gate'], 'authorized': False, 'reason': reason})
        return
    claim_checks.append({'gate': gate['gate'], 'authorized': True, 'reason': None})


def _check_real_recording_gate(gate, document, errors, claim_checks, verify_artifacts):
    evidence = gate.get('evidence') or []
    kinds = _evidence_kinds(evidence)
    reasons = []
    real_samples = _authorized_real_samples(document)
    real_sample_ids = {sample['sample_id'] for sample in real_samples}
    all_sample_ids = {sample['sample_id'] for sample in document.get('samples') or []}
    if not real_samples:
        reasons.append('no authorized real recording is recorded in this acceptance record')
    if 'human_review' not in kinds:
        reasons.append('the real-recording gate requires `human_review` evidence; a machine '
                       'annotation or a synthetic fixture cannot authorize it')
    elif real_sample_ids and not _evidence_bound_to(evidence, 'human_review', real_sample_ids):
        if _evidence_bound_to(evidence, 'human_review', all_sample_ids):
            reasons.append('the human review evidence is bound to a non-authorized '
                           '(synthetic/fixture) sample')
        else:
            reasons.append('no human review evidence is bound to an authorized real sample; a review '
                           'that does not name the sample it reviewed cannot authorize it')
    for sample in real_samples:
        duration = sample['duration_ms']
        if duration < MIN_SAMPLE_MS or duration > MAX_SAMPLE_MS:
            reasons.append(
                f'sample {sample["sample_id"]} is {round(duration / 1000.0, 3)}s, outside the '
                f'authorized 5–20 minute band')
    if (document.get('exposure_scan') or {}).get('clean') is not True:
        reasons.append('the exposure scan is not clean, so the record cannot be released as '
                       'acceptance evidence')
    if reasons:
        errors.append('/gates/real_recording_verified: ' + '; '.join(reasons))
        claim_checks.append({'gate': gate['gate'], 'authorized': False, 'reason': '; '.join(reasons)})
        return
    if not verify_artifacts:
        # The digest is the only control binding the declared recording to actual
        # audio. A gate may not be called verified while that control was never
        # exercised; the caller must opt into verification explicitly, and an
        # unverified run is a refusal to authorize rather than a pass.
        claim_checks.append({
            'gate': gate['gate'], 'authorized': False,
            'reason': 'artifact verification was not requested (run `acceptance check '
                      '--verify-artifacts`), so the authorized sample artifacts were never hashed',
        })
        return
    claim_checks.append({'gate': gate['gate'], 'authorized': True, 'reason': None})


def _check_gates(document, errors, verify_artifacts):
    claim_checks = []
    present = [gate['gate'] for gate in document.get('gates') or []]
    for name in GATES:
        if name not in present:
            errors.append(f'/gates: the {name} gate must be stated explicitly, including when it is '
                          'not reached; an omitted gate is not a pending gate')
    if len(present) != len(set(present)):
        errors.append('/gates: each gate may appear at most once')
    for index, gate in enumerate(document.get('gates') or []):
        name = gate['gate']
        if gate['state'] != 'verified':
            # A gate that is not claimed verified cannot make a false claim. It must
            # still say what is missing, otherwise the pendency is not auditable.
            if not gate.get('unresolved') and not gate.get('reason'):
                errors.append(f'/gates/{index}: a {gate["state"]} gate must record what is missing '
                              '(unresolved or reason); "not reached" without a gap is not auditable')
            continue
        evidence = gate.get('evidence') or []
        if not evidence:
            errors.append(f'/gates/{index}: {name} is claimed verified without any evidence '
                          'reference')
            claim_checks.append({'gate': name, 'authorized': False,
                                 'reason': 'verified without evidence'})
            continue
        if name == 'software_verified':
            allowed = {'ci_run', 'documentation', 'container_run', 'browser_run', 'human_review'}
            if not (_evidence_kinds(evidence) & allowed):
                errors.append(f'/gates/{index}: {name} requires software evidence')
                claim_checks.append({'gate': name, 'authorized': False,
                                     'reason': 'no software evidence'})
                continue
            claim_checks.append({'gate': name, 'authorized': True, 'reason': None})
        elif name == 'container_verified':
            if 'container_run' not in _evidence_kinds(evidence):
                errors.append(f'/gates/{index}: {name} requires `container_run` evidence')
                claim_checks.append({'gate': name, 'authorized': False,
                                     'reason': 'no container_run evidence'})
                continue
            claim_checks.append({'gate': name, 'authorized': True, 'reason': None})
        elif name == 'browser_verified':
            if 'browser_run' not in _evidence_kinds(evidence):
                errors.append(f'/gates/{index}: {name} requires `browser_run` evidence')
                claim_checks.append({'gate': name, 'authorized': False,
                                     'reason': 'no browser_run evidence'})
                continue
            claim_checks.append({'gate': name, 'authorized': True, 'reason': None})
        elif name == 'real_cloud_verified':
            _check_real_cloud_gate(gate, document, errors, claim_checks)
        elif name == 'real_recording_verified':
            _check_real_recording_gate(gate, document, errors, claim_checks, verify_artifacts)
    return claim_checks


def _check_samples(document, errors, observations):
    for index, sample in enumerate(document.get('samples') or []):
        location = f'/samples/{index}'
        if sample['duration_ms'] <= 0:
            errors.append(f'{location}/duration_ms: a recording must have a positive duration')
        if sample['source'] in NON_REAL_SAMPLE_SOURCES:
            # Honest labelling: a synthetic sample is allowed in the record, but it
            # is reported as not being real-recording evidence.
            observations.append(
                f'sample {sample["sample_id"]} is {sample["source"]} and cannot authorize the '
                'real-recording gate')
        for artifact in sample.get('artifacts') or []:
            path = Path(artifact['location'])
            if artifact['in_git']:
                if path.suffix.lower() in REAL_AUDIO_SUFFIXES:
                    errors.append(
                        f'{location}/artifacts: {artifact["kind"]} artifact {artifact["location"]} '
                        'is marked in_git; a private recording must never be committed')
                elif artifact['kind'] in ('transcript', 'annotation', 'report', 'invocation_audit'):
                    errors.append(
                        f'{location}/artifacts: {artifact["kind"]} artifact {artifact["location"]} '
                        'is marked in_git; generated personal reports and provider output must stay '
                        'outside Git')


def _check_human_reviews(document, errors):
    machine_digests = set()
    for sample in document.get('samples') or []:
        for artifact in sample.get('artifacts') or []:
            machine_digests.add(artifact['sha256'])
    for index, review in enumerate(document.get('human_reviews') or []):
        location = f'/human_reviews/{index}'
        artifact = review['artifact']
        if artifact['in_git']:
            # Human review material is either the annotated audio or the human's own
            # annotation record; both are private material that must stay outside Git,
            # so the flag is rejected outright rather than only for audio suffixes.
            errors.append(
                f'{location}/artifact: human review material (audio or annotation) must not be '
                'committed to Git')
        if artifact['derived_from_machine_outputs']:
            errors.append(
                f'{location}/artifact/derived_from_machine_outputs: a review whose material was '
                'produced by a machine and merely confirmed is not human annotation; record the '
                'human-produced artifact instead')
        if not review['preserved_separately_from_machine_originals']:
            errors.append(
                f'{location}: human review must be preserved separately from machine originals '
                '(PRD-N002); a review that overwrites the machine output is not a separate layer')
        if artifact['sha256'] in machine_digests:
            errors.append(
                f'{location}/artifact/sha256: the review artifact is byte-identical to a machine '
                'sample artifact, so it is not a separate human layer')
        if not artifact['location'].strip():
            errors.append(f'{location}/artifact/location: must name the preserved review material')


def _check_evaluations(document, errors, gaps, observations):
    sample_ids = {sample['sample_id'] for sample in document.get('samples') or []}
    for index, evaluation in enumerate(document.get('evaluations') or []):
        location = f'/evaluations/{index}'
        for referenced in evaluation.get('sample_ids') or []:
            if referenced not in sample_ids:
                errors.append(f'{location}/sample_ids: {referenced!r} is not a sample in this record')
        status = evaluation['status']
        if status == 'measured' and not evaluation.get('evidence'):
            errors.append(
                f'{location}: a measured evaluation must cite its evidence; a measurement without '
                'a preserved artifact is an unsourced number')
        if status in ('partial', 'abstained', 'failed', 'not_attempted') and not (
                evaluation.get('reason') or evaluation.get('unresolved')):
            errors.append(
                f'{location}: a {status} evaluation must state its reason or its unresolved items; '
                'an unexplained gap cannot be distinguished from an omission')
        if status in ('partial', 'abstained', 'failed') and not evaluation.get('evidence'):
            gaps.append(
                f'evaluation {evaluation["evaluation_id"]} is {status} without preserved evidence')
        if status == 'not_attempted':
            observations.append(
                f'evaluation {evaluation["evaluation_id"]} was not attempted and proves nothing')


#: Which denominator stage each evaluation kind is measured on. A measured
#: evaluation whose stage was never reported means the failure/unknown/abstention
#: counts for that stage are missing, which is exactly the success-only denominator
#: the M1 gate forbids.
EVALUATION_STAGE = {
    'import_qa': 'import_normalization_qa',
    'asr_native_speaker': 'file_asr',
    'acoustic_boundary': 'acoustic_boundary',
    'speaker_alignment': 'speaker_alignment',
    'role_review_workload': 'role_review',
    'metric_eligibility': 'metrics',
    'semantic_judge': 'semantic_judge',
    'human_role_correction': 'attribution',
    'browser_navigation': 'browser_navigation',
    'report_traceability': 'report',
}


def _check_denominators(document, errors, gaps):
    seen = set()
    for index, entry in enumerate(document.get('denominators') or []):
        location = f'/denominators/{index}'
        if entry['stage'] in seen:
            errors.append(f'{location}: stage {entry["stage"]} is reported twice')
        seen.add(entry['stage'])
        counters = entry['counters']
        total = sum(counters.values())
        if total != entry['expected_total']:
            errors.append(
                f'{location}: counters sum to {total} but expected_total is {entry["expected_total"]}; '
                'an outcome excluded from the counters hides records from the denominator')
        if entry['expected_total'] > 0 and not entry.get('evidence'):
            errors.append(
                f'{location}: a stage evaluated against {entry["expected_total"]} record(s) must cite '
                'the evidence it was evaluated from')
    if not seen:
        gaps.append('no stage denominator was reported at all, so failures, unknowns and abstentions '
                    'cannot be counted (the M1 gate forbids a success-only denominator)')
    return seen


def _check_denominator_coverage(document, errors, stages):
    """A measured stage must have its denominator reported.

    An omitted stage is indistinguishable from "nothing failed there", which is how
    a success-only denominator gets built by omission. Each measured evaluation
    therefore requires the denominator of the stage it was measured on. A record that
    claims the real-recording gate must also report at least one evaluation; a record
    that claims nothing is allowed to report nothing, and its emptiness is visible as
    a pending gate rather than as an invalid claim.
    """
    measured = [evaluation for evaluation in document.get('evaluations') or []
                if evaluation['status'] in ('measured', 'partial')]
    claims_real_recording = (_gate(document, 'real_recording_verified') or {}).get(
        'state') == 'verified'
    if claims_real_recording and not (document.get('evaluations') or []):
        errors.append('/evaluations: a record claiming the real-recording gate must report at least '
                      'one evaluation; a record with no evaluation reports no measurement at all')
    for evaluation in measured:
        stage = EVALUATION_STAGE[evaluation['kind']]
        if stage not in stages:
            errors.append(
                f'/denominators: the {stage} stage has no reported denominator, but evaluation '
                f'{evaluation["evaluation_id"]} was measured on it; an omitted stage is '
                'indistinguishable from "nothing failed", which is a success-only denominator')


def _check_traceability(document, errors, gaps, declared_evidence, observations):
    for index, chain in enumerate(document.get('traceability') or []):
        location = f'/traceability/{index}'
        interval = chain['audio_interval']
        if interval['end_ms'] <= interval['start_ms']:
            errors.append(f'{location}/audio_interval: end_ms must exceed start_ms')
        if not chain['evidence_ids']:
            errors.append(f'{location}/evidence_ids: a conclusion without an evidence id is not '
                          'traceable (PRD-N001)')
        if not chain.get('turn_id') and not chain.get('event_id'):
            gaps.append(
                f'chain {chain["chain_id"]} cites neither a Turn nor an Event, so the conclusion is '
                'not yet traced to a conversational unit')
        if not chain.get('model') and not chain.get('policy'):
            gaps.append(
                f'chain {chain["chain_id"]} names no model or policy version, so the producing '
                'configuration is unresolved')
        for evidence_id in chain['evidence_ids']:
            if evidence_id not in declared_evidence:
                errors.append(
                    f'{location}/evidence_ids: {evidence_id!r} is not declared as evidence anywhere '
                    'in this record, so the conclusion does not resolve to a preserved artifact')


def _check_manual_role_corrections(document, errors, gaps):
    for index, correction in enumerate(document.get('manual_role_corrections') or []):
        location = f'/manual_role_corrections/{index}'
        if correction['prior_analysis_id'] == correction['new_analysis_id']:
            errors.append(
                f'{location}: a saved role-mapping change must create a new AnalysisRevision; '
                'prior_analysis_id and new_analysis_id are identical')
        if correction['prior_result_preserved'] is not True:
            errors.append(
                f'{location}/prior_result_preserved: the prior result must be preserved; a correction '
                'that overwrites it loses the original evidence (PRD-N002)')
        if not correction.get('recompute_evidence'):
            errors.append(f'{location}: a correction must cite its recompute evidence')
        if not correction.get('diff_evidence'):
            errors.append(f'{location}: a correction must cite its downstream diff evidence')
        if correction['recompute_scope'] == 'recomputed_recognition':
            errors.append(
                f'{location}/recompute_scope: applying a role decision must not recompute '
                'recognition/clustering evidence; that would re-bill the provider and replace the '
                'original evidence')
        elif correction['recompute_scope'] == 'unknown':
            gaps.append(
                f'correction {correction["correction_id"]} does not state which stages were '
                'recomputed')


def _check_human_review_coverage(document, errors, gaps):
    real_samples = _authorized_real_samples(document)
    if not real_samples:
        return
    reviewed = {review.get('sample_id') for review in document.get('human_reviews') or []}
    for sample in real_samples:
        if sample['sample_id'] not in reviewed:
            gaps.append(
                f'sample {sample["sample_id"]} has no human review bound to it, so its conclusions '
                'rest on machine output alone')


def validate(document, *, verify_artifacts=False, repository_root=None):
    """Judge an acceptance record against the M1 claim rules.

    Returns a report naming the record's overall status, each gate's authorized
    state, the gaps that remain, and the invalid claims if any. ``status='invalid'``
    means a claim is not authorized by its evidence; ``real_recording_pending``
    means the record is sound but the real-recording gate is still open.

    ``gaps`` are **blocking**: a record cannot be reported as ``complete`` while any
    of them is open, and the CLI therefore cannot exit 0 with them. ``observations``
    are informational statements about the record (for example "this sample is
    synthetic") that do not by themselves block a genuinely complete record.
    """
    load_record(document)
    errors = []
    gaps = []
    observations = []
    claim_checks = _check_gates(document, errors, verify_artifacts)
    _check_samples(document, errors, observations)
    _check_human_reviews(document, errors)
    _check_evaluations(document, errors, gaps, observations)
    declared_evidence = _check_evidence_identity(document, errors)
    stages = _check_denominators(document, errors, gaps)
    _check_denominator_coverage(document, errors, stages)
    _check_traceability(document, errors, gaps, declared_evidence, observations)
    _check_manual_role_corrections(document, errors, gaps)
    _check_human_review_coverage(document, errors, gaps)

    secret_findings = scan_secrets(document, repository_root=repository_root)
    declared_scan = document.get('exposure_scan') or {}
    for finding in secret_findings:
        errors.append(f'/exposure_scan: {finding["category"]} at {finding["location"]}')
    if declared_scan.get('clean') is True and secret_findings:
        errors.append('/exposure_scan/clean: the record declares a clean scan but the scan found '
                      'material that must not be committed')
    if declared_scan.get('clean') is not True and not secret_findings:
        gaps.append('the exposure scan is not recorded as clean; run the scan before the record '
                    'is used as acceptance evidence')
    if repository_root is None:
        # The committed-audio control is opt-in, and an unperformed control is not a
        # passed control: the verdict must say the repository was never scanned.
        gaps.append('the repository was not scanned for committed recordings '
                    '(run `acceptance check --repository-root <repo>`), so the PRD-N004 '
                    'no-committed-audio control was not exercised')

    artifacts_checked = None
    if verify_artifacts:
        artifacts_checked = _verify_artifact_hashes(document, errors, gaps)

    real_gate = _gate(document, 'real_recording_verified') or {'state': 'not_reached'}
    real_authorized = any(item['gate'] == 'real_recording_verified' and item['authorized']
                          for item in claim_checks)
    real_samples = _authorized_real_samples(document)
    # A record is only complete when *every* gate is verified and authorized, every
    # blocking gap is closed, and no artifact check failed. The real-recording gate
    # is necessary but not sufficient: a complete M1 acceptance must also show the
    # software, container, browser and real-cloud states it claims, and each gate's
    # own unresolved list must be empty.
    gates_open = []
    for name in GATES:
        entry = _gate(document, name) or {'gate': name, 'state': 'not_reached'}
        if entry['state'] != 'verified':
            gates_open.append(f'the {name} gate is {entry["state"]}')
        elif not any(item['gate'] == name and item['authorized'] for item in claim_checks):
            gates_open.append(f'the {name} gate is not authorized by its evidence')
        for item in entry.get('unresolved') or []:
            gates_open.append(f'{name}: {item}')
    if errors:
        status = STATUS_INVALID
    elif not gates_open and not gaps and real_authorized:
        status = STATUS_COMPLETE
    elif not real_samples:
        status = STATUS_NO_AUTHORIZED_RECORDING
    else:
        status = STATUS_REAL_RECORDING_PENDING

    gate_status = []
    for name in GATES:
        gate = _gate(document, name) or {'gate': name, 'state': 'not_reached', 'evidence': []}
        authorized = None
        if gate['state'] == 'verified':
            authorized = any(item['gate'] == name and item['authorized'] for item in claim_checks)
        gate_status.append({
            'gate': name,
            'state': gate['state'],
            'claim_authorized': authorized,
            'evidence_count': len(gate.get('evidence') or []),
            'unresolved': list(gate.get('unresolved') or []),
            'reason': gate.get('reason'),
        })

    return {
        'evidence_policy_version': EVIDENCE_POLICY_VERSION,
        'record_id': document['record_id'],
        'issue_url': document['issue_url'],
        'code_commit': document['code_commit'],
        'status': status,
        # Only a complete record may be reported as a real-recording verification.
        # The flag is derived from the status, never from the record's own claim.
        'real_recording_verified': status == STATUS_COMPLETE,
        'gates': gate_status,
        'pending_gates': [item['gate'] for item in gate_status if item['state'] != 'verified'],
        'unauthorized_claims': [item for item in claim_checks if not item['authorized']],
        'authorized_samples': [sample['sample_id'] for sample in real_samples],
        'sample_count': len(document.get('samples') or []),
        'stages_reported': sorted(stages),
        'artifacts_hashed': artifacts_checked,
        'artifacts_verification_requested': verify_artifacts,
        'repository_scanned': repository_root is not None,
        'secret_findings': secret_findings,
        # Blocking gaps and non-blocking observations are reported separately so a
        # reader can tell "this must be fixed before acceptance" from "this is a fact
        # about the record".
        'gaps': gaps,
        'observations': observations,
        'real_recording_open_items': gates_open,
        'errors': errors,
        'note': ('An acceptance record may only state a gate it can authorize. `real_recording_'
                 'verified` requires authorized 5–20 minute recordings, human review preserved '
                 'separately from machine originals, verified artifact digests and a clean exposure '
                 'scan of a scanned repository; synthetic fixtures, mocks, CI runs, preview releases '
                 'and machine annotations never substitute for it. This checker verifies that the '
                 'declared digests match local files and that the claims are internally authorized; '
                 'it cannot prove a declared sample really is an authorized real recording.'),
    }


def validate_file(path, *, verify_artifacts=False, repository_root=None):
    return validate(load_record_file(path), verify_artifacts=verify_artifacts,
                    repository_root=repository_root)


def blank_record(record_id, *, issue_url, recorded_by, product_version, code_commit,
                 prd_refs=('PRD-F001', 'PRD-N001'), recorded_at=None, deployment='not_recorded'):
    """Build an unpopulated acceptance record.

    Every gate is ``not_reached`` with the missing input named. A blank record is a
    template for the authorized real-recording pass: it deliberately claims nothing,
    so it can never be mistaken for a completed acceptance.
    """
    from datetime import datetime, timezone
    stamp = recorded_at or datetime.now(timezone.utc).isoformat()
    gates = []
    for name in GATES:
        gates.append({
            'gate': name,
            'state': 'not_reached',
            'verified_at': None,
            'evidence': [],
            'unresolved': [f'no evidence recorded for {name} in this blank template'],
            'reason': 'not attempted: a blank acceptance record claims nothing',
        })
    return {
        'schema_version': SCHEMA_VERSION,
        'record_id': record_id,
        'issue_url': issue_url,
        'recorded_at': stamp,
        'recorded_by': recorded_by,
        'prd_refs': list(prd_refs),
        'product_version': product_version,
        'code_commit': code_commit,
        'environment': {'deployment': deployment},
        'samples': [],
        'human_reviews': [],
        'evaluations': [],
        'denominators': [],
        'traceability': [],
        'manual_role_corrections': [],
        'gates': gates,
        'exposure_scan': {'scanned_at': None, 'scan_roots': [], 'clean': False, 'findings': []},
        'notes': ('Blank M1 acceptance template (#85). It records no measurement and claims no gate. '
                  'Populate it only from an authorized real-recording pass; never from synthetic '
                  'fixtures, mocks or CI alone.'),
    }


def render_report(result):
    """Render the acceptance judgement as Markdown for a human acceptance record."""
    lines = []
    lines.append(f'# M1 acceptance evidence — {result["record_id"]}')
    lines.append('')
    lines.append(f'- Issue: {result["issue_url"]}')
    lines.append(f'- Code commit: `{result["code_commit"]}`')
    lines.append(f'- Evidence policy version: `{result["evidence_policy_version"]}`')
    lines.append(f'- Status: **{result["status"]}**')
    lines.append(f'- `real_recording_verified`: **{"yes" if result["real_recording_verified"] else "no"}**')
    hashed = result['artifacts_hashed']
    lines.append(f'- Artifact verification requested: '
                 f'**{"yes" if result["artifacts_verification_requested"] else "no"}**'
                 + (f' ({hashed} artifact(s) re-hashed)' if result['artifacts_verification_requested']
                    else ''))
    lines.append(f'- Repository scanned for committed recordings: '
                 f'**{"yes" if result["repository_scanned"] else "no"}**')
    lines.append('')
    lines.append('## Gates')
    lines.append('')
    lines.append('| Gate | State | Claim authorized | Evidence refs |')
    lines.append('| --- | --- | --- | --- |')
    for gate in result['gates']:
        authorized = '—' if gate['claim_authorized'] is None else ('yes' if gate['claim_authorized'] else '**no**')
        lines.append(f'| `{gate["gate"]}` | {gate["state"]} | {authorized} | {gate["evidence_count"]} |')
    lines.append('')
    lines.append('## Open gates')
    lines.append('')
    if result['real_recording_open_items']:
        for item in result['real_recording_open_items']:
            lines.append(f'- {item}')
    else:
        lines.append('- None: every gate is verified and authorized by its evidence.')
    lines.append('')
    lines.append('## Authorized samples')
    lines.append('')
    if result['authorized_samples']:
        for sample_id in result['authorized_samples']:
            lines.append(f'- `{sample_id}`')
    else:
        lines.append('- None. No authorized real recording is recorded, so no real-recording gate can '
                     'be claimed.')
    lines.append('')
    lines.append('## Unauthorized claims')
    lines.append('')
    if result['unauthorized_claims']:
        for item in result['unauthorized_claims']:
            lines.append(f'- `{item["gate"]}`: {item["reason"]}')
    else:
        lines.append('- None.')
    lines.append('')
    lines.append('## Blocking gaps')
    lines.append('')
    if result['gaps']:
        for item in result['gaps']:
            lines.append(f'- {item}')
    else:
        lines.append('- None.')
    lines.append('')
    lines.append('## Observations (non-blocking)')
    lines.append('')
    if result['observations']:
        for item in result['observations']:
            lines.append(f'- {item}')
    else:
        lines.append('- None.')
    lines.append('')
    lines.append('## Errors')
    lines.append('')
    if result['errors']:
        for item in result['errors']:
            lines.append(f'- {item}')
    else:
        lines.append('- None.')
    lines.append('')
    lines.append('> A gate is only ever reported at the state its evidence authorizes. Software, '
                 'container, browser, real-cloud and real-recording verification are separate claims '
                 'and none substitutes for another. A blocking gap means the record may not be '
                 'reported as complete. This checker verifies that declared digests match local files '
                 'and that claims are internally authorized; it cannot prove a declared sample really '
                 'is an authorized real recording.')
    return '\n'.join(lines) + '\n'
