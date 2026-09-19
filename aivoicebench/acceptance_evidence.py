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
from datetime import datetime
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


#: Files that are never credentials: they are generated, vendored or non-textual,
#: and text-scanning them costs time without adding coverage. Kept deliberately
#: small so the scan is *broader* than an extension allowlist rather than narrower
#: than it: anything not named here is decoded and scanned as text.
_TEXT_SCAN_SKIP_SUFFIXES = (
    '.pyc', '.pyo', '.so', '.dll', '.dylib', '.exe', '.bin', '.zip', '.gz', '.tar', '.whl',
    '.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.mp4', '.mov', '.woff', '.woff2',
    '.ttf', '.eot', '.map',
)

#: Directories whose contents are environment state, dependencies or build output
#: rather than repository material. They are not descended into, so their files are
#: never counted in ``repository_files_inspected`` — the walk reports what it actually
#: examined rather than a total that includes material it never looked at. This is a
#: bounded, named policy, not the previous situation where an unread extension was
#: counted as inspected and then silently skipped.
_TEXT_SCAN_SKIP_DIRECTORIES = frozenset({
    '.git', '.hg', '.svn', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox', '.venv',
    '__pycache__', 'node_modules', 'venv', 'dist', 'build', '.next', '.test-tmp',
})

#: A single file larger than this is not evidence material and is not read.
_MAX_SCANNED_FILE_BYTES = 8 * 1024 * 1024

#: Files that legitimately contain credential-*shaped* text because they *define* or
#: *exercise* this control: the detection patterns themselves, and the tests that
#: assert a signed URL, a PEM block or a `sk-…` value is detected. Reporting them as
#: leaked credentials would be a false positive, and silently dropping their lines
#: would hide a real one, so they are named here and disclosed in the result. This is
#: the only exclusion in the scan: every other file the walk reaches is read.
#: Entries are repository-relative paths, not basenames, so a copy of one of these
#: names placed elsewhere in the tree is scanned like any other file.
DETECTION_POLICY_SOURCE_PATHS = frozenset({
    'aivoicebench/acceptance_evidence.py',
    'tests/test_acceptance_evidence.py',
    'tests/test_file_asr_transport.py',
    'tests/test_judge_contract.py',
    'tests/test_workbench.py',
})


def _is_detection_policy_source(path):
    """Whether ``path`` is a copy of the detection policy's own source.

    Matched on the trailing repository-relative path (``tests/test_...py``) rather
    than on the bare basename, so a same-named file elsewhere in a walked tree is
    still scanned instead of being silently exempt.
    """
    if path.suffix.lower() != '.py':
        return False
    parts = path.resolve().parts
    for entry in DETECTION_POLICY_SOURCE_PATHS:
        tail = tuple(entry.split('/'))
        if len(parts) >= len(tail) and tuple(parts[-len(tail):]) == tail:
            return True
    return False


def _scan_target(path, findings):
    """Classify and scan one candidate file; report why it was not scanned.

    Returns ``None`` when the file was scanned (or found), otherwise a short reason
    string. A file that is skipped must be *accounted for*: a control that reports
    "clean" has to be able to say which material it actually looked at, otherwise an
    unread extension is indistinguishable from a clean one.
    """
    suffix = path.suffix.lower()
    if suffix in REAL_AUDIO_SUFFIXES:
        findings.append({
            'location': str(path),
            'category': 'real_recording_in_git',
            'detail': 'audio artifacts must stay outside the repository and outside release '
                      'artifacts',
        })
        return None
    if suffix in _TEXT_SCAN_SKIP_SUFFIXES:
        return 'binary/unscannable'
    if _is_detection_policy_source(path):
        return 'detection policy source'
    try:
        if path.stat().st_size > _MAX_SCANNED_FILE_BYTES:
            return 'oversized'
    except OSError as error:
        return f'unreadable: {error}'
    try:
        text = path.read_text(encoding='utf-8-sig', errors='strict')
    except UnicodeDecodeError:
        return 'binary/unscannable'
    except OSError as error:
        return f'unreadable: {error}'
    _scan_text(text, str(path), findings)
    return None


def scan_secrets(document, *, repository_root=None):
    """PRD-N004 evidence: no credential, signed URL or private recording leaks.

    Scans the record tree and, when given, a set of files/directories on disk. Only
    the location and category are reported: echoing the offending text would move
    the secret into the report.

    Returns ``(findings, unchecked_roots, inspected, unscanned, skipped_directories)``.
    ``unchecked_roots`` names every declared or requested root that could not be walked:
    a control that was never exercised must be reported as unexercised rather than as
    clean, because a typo'd or stale path would otherwise read as "scanned, nothing
    found". ``inspected`` counts the files examined for material, ``unscanned`` names
    the files that were walked but could not be decoded as text, and
    ``skipped_directories`` names the environment/build directories the walk did not
    descend into. Every file the walk reached therefore lands in exactly one bucket —
    scanned, found, or unscanned — and every directory it passed over is named, so
    "no findings" can never be an artefact of what the scan chose not to read.

    A root named more than once (declared in ``exposure_scan.scan_roots`` *and*
    passed as ``--repository-root``) is walked once. The inspected count is documented
    evidence that the control ran, so it must be the number of distinct files
    examined, not the number of times a file was named.
    """
    findings = []
    unchecked = []
    unscanned = []
    skipped_directories = []
    inspected = 0
    _scan_record(document, '', findings)
    scan_roots = document.get('exposure_scan', {}).get('scan_roots') or []
    roots = list(scan_roots)
    if repository_root is not None:
        roots.append(str(repository_root))
    seen_roots = set()
    for entry in roots:
        root = Path(entry)
        if not root.is_dir():
            unchecked.append(str(entry))
            continue
        identity = str(root.resolve())
        if identity in seen_roots:
            continue
        seen_roots.add(identity)
        # One walk, partitioned: a directory in the skip list is recorded and not
        # descended into, everything else is a scan target.
        targets = []
        for path in root.rglob('*'):
            if path.is_dir():
                if path.name in _TEXT_SCAN_SKIP_DIRECTORIES:
                    skipped_directories.append(str(path))
            elif not _is_under_skipped_directory(path, root):
                targets.append(path)
        targets.sort()
        inspected += len(targets)
        for target in targets:
            reason = _scan_target(target, findings)
            if reason is not None:
                unscanned.append({'location': str(target), 'reason': reason})
    return findings, unchecked, inspected, unscanned, sorted(set(skipped_directories))


def _is_under_skipped_directory(path, root):
    """Whether ``path`` sits below a directory the text scan does not enter."""
    try:
        relative = path.relative_to(root)
    except ValueError:  # pragma: no cover - rglob never yields an outside path
        return False
    return any(part in _TEXT_SCAN_SKIP_DIRECTORIES for part in relative.parts[:-1])


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


def _artifact_size(path):
    """The actual size of a preserved artifact, or ``None`` when unreadable."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _verify_artifact_hashes(document, errors, gaps):
    """Re-hash every declared artifact that is locally reachable.

    Both the sample artifacts and the human-review artifact are covered: the human
    layer is what the real-recording claim ultimately rests on, so a review artifact
    that cannot be hashed must not pass unchecked. An artifact that is not present
    locally cannot be re-hashed; that is a **blocking** gap, not an informational
    note, because the digest is the only control binding a declared sample to actual
    audio.

    A declared ``byte_length`` is reconciled with the preserved file in the same
    pass. The digest alone would let a record declare any size while still pointing
    at the right bytes, and because ``duration_ms`` is also author-declared, an
    unreconciled size would leave every quantity describing "a 5–20 minute
    recording" unchecked except the digest.
    """
    checked = 0
    targets = []
    for sample in document.get('samples') or []:
        for artifact in sample.get('artifacts') or []:
            targets.append((f'sample {sample["sample_id"]} artifact', artifact, True))
    for index, review in enumerate(document.get('human_reviews') or []):
        # The human-review artifact shape carries no byte_length, so only the digest is
        # reconciled for it; the sample artifacts are where the declared size lives.
        targets.append((f'human review {review["review_id"]} artifact', review['artifact'], False))
    for label, artifact, has_declared_size in targets:
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
        declared_size = artifact.get('byte_length') if has_declared_size else None
        if declared_size is not None:
            actual_size = _artifact_size(path)
            if actual_size is not None and actual_size != declared_size:
                errors.append(
                    f'{label} {location}: declared byte_length {declared_size} does not match the '
                    f'preserved artifact ({actual_size} bytes), so the declared size is not the '
                    'size of the artifact it is bound to')
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
    elif real_sample_ids:
        # The entry must be one of the human artifacts the record declares, matched by
        # digest and not only by path. Otherwise arbitrary bytes reachable at some
        # location (with a matching self-declared digest) can carry the entire
        # real-recording claim while `human_reviews[]` — the layer PRD-N002 says the
        # claim rests on — says something else entirely.
        declared_reviews = {review['artifact']['sha256']: review['review_id']
                            for review in document.get('human_reviews') or []}
        for entry in _evidence_bound_to(evidence, 'human_review', real_sample_ids):
            digest = entry.get('sha256')
            if digest is None:
                reasons.append(
                    f'human review evidence {entry["evidence_id"]} declares no sha256, so it cannot '
                    'be shown to be one of the declared human review artifacts')
            elif digest not in declared_reviews:
                reasons.append(
                    f'human review evidence {entry["evidence_id"]} at {entry["location"]} is not '
                    'any declared human_reviews artifact (no declared review carries that digest); '
                    'a gate authorized by material outside the preserved human layer is not '
                    'authorized by human review')
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


def _parse_timestamp(text):
    """Parse an ISO-8601 timestamp, or ``None`` when it is absent or unusable.

    A timestamp this module cannot parse is not silently treated as ordered: the
    caller only compares values that both parsed.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None


def _check_gate_timestamp_order(document, index, name, gate, errors):
    """A verification cannot predate what it verified, nor postdate the record.

    Requiring a timestamp only established that the field was filled. PRD-N007's
    finalization-state concerns need the claim to sit on a reconstructible timeline, so
    the verification time is ordered against the recording's own authorization time and
    the time the record was written.

    The control is **fail-closed**: a timestamp that is present but unparseable is an
    error rather than a comparison that is quietly skipped, and an authorized real
    sample that declares no authorization time is reported as the reason its ordering
    could not be checked. Skipping the comparison silently would leave the gate
    authorized while the control that is supposed to bound it never ran — the exact
    "unexercised control reported as exercised" failure this module exists to prevent.
    """
    raw_verified_at = gate.get('verified_at')
    verified_at = _parse_timestamp(raw_verified_at)
    if verified_at is None:
        errors.append(f'/gates/{index}: {name} declares verified_at {raw_verified_at!r}, which is '
                      'not a parseable timestamp, so its position on the acceptance timeline '
                      'cannot be checked')
        return
    raw_recorded_at = document.get('recorded_at')
    recorded_at = _parse_timestamp(raw_recorded_at)
    if recorded_at is None:
        errors.append(f'/recorded_at: {raw_recorded_at!r} is not a parseable timestamp, so '
                      f'{name} cannot be ordered against the time the record was written')
    elif verified_at > recorded_at:
        errors.append(f'/gates/{index}: {name} is verified at {raw_verified_at}, after the '
                      f'record was written ({raw_recorded_at}); a verification cannot '
                      'postdate the record that reports it')
    for sample in _authorized_real_samples(document):
        raw_authorized_at = (sample.get('authorization') or {}).get('authorized_at')
        authorized_at = _parse_timestamp(raw_authorized_at)
        if authorized_at is None:
            errors.append(
                f'/gates/{index}: {name} cannot be ordered against sample {sample["sample_id"]} '
                f'because its authorization.authorized_at is {raw_authorized_at!r}, which is not a '
                'parseable timestamp; a gate whose verification time cannot be placed after the '
                'recording was authorized has not been shown to be verifiable')
        elif verified_at < authorized_at:
            errors.append(
                f'/gates/{index}: {name} is verified at {raw_verified_at}, before sample '
                f'{sample["sample_id"]} was authorized ({raw_authorized_at}); a gate cannot be '
                'verified against a recording that was not yet authorized')
        if authorized_at is None:
            # The unorderable authorization time is already reported above; comparing the
            # review against it would only restate that.
            continue
        for review in document.get('human_reviews') or []:
            if review.get('sample_id') not in (None, sample['sample_id']):
                continue
            raw_annotated_at = review.get('annotated_at')
            annotated_at = _parse_timestamp(raw_annotated_at)
            if annotated_at is None:
                errors.append(
                    f'/gates/{index}: {name} cannot be ordered against human review '
                    f'{review["review_id"]} because its annotated_at is {raw_annotated_at!r}, which '
                    'is not a parseable timestamp')
                continue
            if annotated_at < authorized_at:
                errors.append(
                    f'/human_reviews: {review["review_id"]} was annotated at {raw_annotated_at}, '
                    f'before sample {sample["sample_id"]} was authorized ({raw_authorized_at}); the '
                    'human layer cannot have judged a recording that was not yet authorized')
            if verified_at < annotated_at:
                errors.append(
                    f'/gates/{index}: {name} is verified at {raw_verified_at}, before human review '
                    f'{review["review_id"]} was annotated ({raw_annotated_at}); the gate depends on '
                    'that review, so it cannot be verified first')


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
        if not (gate.get('verified_at') or '').strip():
            # A verified claim that carries no time cannot be ordered against the
            # recording it judges, so the acceptance would not be reconstructible.
            errors.append(f'/gates/{index}: {name} is claimed verified without a verified_at '
                          'timestamp; a verification that cannot be placed in time is not '
                          'auditable')
            claim_checks.append({'gate': name, 'authorized': False,
                                 'reason': 'verified without a timestamp'})
            continue
        _check_gate_timestamp_order(document, index, name, gate, errors)
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


#: Bytes per sample for encoding names that describe raw, uncompressed PCM. The M1
#: acceptance samples are recorded as raw PCM, so for these encodings the record's own
#: `duration_ms`, `sample_rate_hz` and `channels` imply the artifact's size. Encodings
#: that are compressed or containerised (mp3, m4a, opus, …) are deliberately absent:
#: no such arithmetic exists for them, and inventing one would be exactly the
#: fabricated-measurement failure this checker exists to prevent.
PCM_ENCODING_BYTES_PER_SAMPLE = {
    'PCM_S16LE': 2,
    'PCM_S16BE': 2,
    'PCM_U8': 1,
    'PCM_S8': 1,
    'PCM_S24LE': 3,
    'PCM_S24BE': 3,
    'PCM_S32LE': 4,
    'PCM_S32BE': 4,
    'PCM_F32LE': 4,
    'PCM_F32BE': 4,
}

#: Rounding allowance when comparing a declared size with the size the record's own
#: sample parameters imply, expressed as a fraction of a second of audio.
PCM_SIZE_TOLERANCE_MS = 1


def _implied_pcm_bytes(sample):
    """The artifact size the sample's own declared parameters imply, or ``None``.

    Returns ``None`` for any encoding this module has no size arithmetic for, so an
    unknown or compressed encoding is never silently treated as if it had been
    checked.
    """
    encoding = str(sample.get('encoding') or '').upper()
    bytes_per_sample = PCM_ENCODING_BYTES_PER_SAMPLE.get(encoding)
    if bytes_per_sample is None:
        return None
    channels = sample.get('channels')
    rate = sample.get('sample_rate_hz')
    duration = sample.get('duration_ms')
    if not isinstance(channels, int) or not isinstance(rate, int) \
            or not isinstance(duration, (int, float)):
        return None
    return rate * channels * bytes_per_sample * (duration / 1000.0)


def _check_declared_audio_size(document, errors):
    """The declared artifact size must agree with the sample's own parameters.

    A record states a duration, a sample rate, a channel count and an encoding, and
    separately states the artifact's `byte_length`. For raw PCM those two statements
    are arithmetically related, so a 48-byte artifact cannot be eight minutes of
    16 kHz mono S16LE audio (~15.36 MB). The size check alone would accept that pair,
    because both numbers are the author's; this closes the gap between them without
    decoding any audio and without claiming anything about encodings that have no
    such arithmetic.

    Returns ``(applied, not_applicable)``, each a list of ``(sample_id, encoding)``.
    The control is only applicable to encodings this module has size arithmetic for and
    to samples that declare an audio artifact with a size, so *not applying* it is a
    normal outcome — and one that must be named. A record's own free-text ``encoding``
    field decides whether this control runs at all, so an undisclosed skip would let a
    sample sidestep the check by spelling its encoding differently while still
    reporting "every control ran".
    """
    applied = []
    not_applicable = []
    for index, sample in enumerate(document.get('samples') or []):
        audio_artifacts = [artifact for artifact in sample.get('artifacts') or []
                           if artifact['kind'] == 'audio']
        if not audio_artifacts:
            continue
        implied = _implied_pcm_bytes(sample)
        if implied is None:
            not_applicable.append((sample['sample_id'], sample['encoding']))
            continue
        sample_applied = False
        for position, artifact in enumerate(sample.get('artifacts') or []):
            if artifact['kind'] != 'audio':
                continue
            declared = artifact.get('byte_length')
            if declared is None:
                # This artifact declared no size, so no relation was checked for it.
                # Reported per sample, where the assertion is about the sample.
                continue
            sample_applied = True
            tolerance = sample['sample_rate_hz'] * sample['channels'] \
                * PCM_ENCODING_BYTES_PER_SAMPLE[str(sample['encoding']).upper()] \
                * (PCM_SIZE_TOLERANCE_MS / 1000.0)
            if abs(declared - implied) > tolerance:
                errors.append(
                    f'/samples/{index}/artifacts/{position}: declared byte_length {declared} '
                    f'contradicts the sample\'s own parameters ({sample["encoding"]}, '
                    f'{sample["sample_rate_hz"]} Hz, {sample["channels"]} channel(s), '
                    f'{round(sample["duration_ms"] / 1000.0, 3)}s), which imply about '
                    f'{int(round(implied))} bytes; a declared size that cannot describe the '
                    'declared recording is not evidence of it')
        if sample_applied:
            applied.append((sample['sample_id'], sample['encoding']))
        else:
            # The encoding had arithmetic but no audio artifact declared a size for it
            # to be applied to, which is a non-application and is named as one.
            not_applicable.append((sample['sample_id'], sample['encoding']))
    return applied, not_applicable


def _check_artifact_identity(document, errors, gaps):
    """An authorized sample must be backed by its own artifact, once.

    Two declared authorized recordings that resolve to the same bytes are one
    recording counted twice, and Issue #85's scope is "one or more authorized
    recordings" — so the sample count is a reported result and cannot be inflated by
    pointing two sample ids at one file. This is the same rule already enforced one
    level up for the human-review layer, and the checker already holds the digests
    needed to enforce it here.

    The audio artifact of an authorized real sample must also declare a
    ``byte_length``: of everything such a sample claims, the artifact size is the one
    quantity this checker can actually measure, so leaving it out would exempt the
    recording from the single size control available.
    """
    digest_owners = {}
    for index, sample in enumerate(document.get('samples') or []):
        location = f'/samples/{index}'
        sample_id = sample['sample_id']
        seen_here = {}
        for artifact in sample.get('artifacts') or []:
            digest = artifact['sha256']
            if digest in seen_here:
                errors.append(
                    f'{location}/artifacts/{artifact["kind"]}: declares the same sha256 as the '
                    f'{seen_here[digest]} artifact, so the sample repeats one preserved file '
                    'instead of declaring distinct material')
            else:
                seen_here[digest] = artifact['kind']
            digest_owners.setdefault(digest, []).append((sample_id, artifact['kind'], location))
            if artifact['kind'] == 'audio' and _is_authorized_real(sample) \
                    and artifact.get('byte_length') is None:
                gaps.append(
                    f'sample {sample_id} audio artifact {artifact["location"]} declares no '
                    'byte_length, so its declared size cannot be reconciled with the preserved '
                    'file')
    for digest, owners in digest_owners.items():
        sample_ids = {sample_id for sample_id, _, _ in owners}
        if len(sample_ids) > 1:
            detail = ', '.join(f'{sample_id} ({kind})' for sample_id, kind, _ in owners)
            errors.append(
                f'/samples: artifact sha256 {digest[:12]}… is declared by more than one sample '
                f'({detail}), so the distinct authorized recordings are one file counted more than '
                'once; each authorized sample must be bound to its own preserved artifact')

    # The same rule one layer up: #85 requires human review sufficient to evaluate
    # several distinct things (speaker coverage, manual role assignment, acoustic
    # boundaries), so one preserved annotation reused verbatim as two reviews
    # overstates the human layer exactly as one file counted as two recordings would.
    review_digest_owners = {}
    for index, review in enumerate(document.get('human_reviews') or []):
        review_digest_owners.setdefault(review['artifact']['sha256'], []).append(
            (review['review_id'], review['kind'], f'/human_reviews/{index}'))
    for digest, owners in review_digest_owners.items():
        review_ids = {review_id for review_id, _, _ in owners}
        if len(review_ids) > 1:
            detail = ', '.join(f'{review_id} ({kind})' for review_id, kind, _ in owners)
            errors.append(
                f'/human_reviews: artifact sha256 {digest[:12]}… is declared by more than one review '
                f'({detail}), so distinct human reviews are one annotation file counted more than '
                'once; each review must preserve its own annotation artifact')


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


#: The stages every M1 real-recording acceptance must account for. A stage that is
#: absent is indistinguishable from a stage where nothing failed, which is how a
#: success-only denominator is built by omission, so a complete record names all of
#: them (`not_applicable` is a valid answer).
M1_STAGES = (
    'import_normalization_qa',
    'file_asr',
    'acoustic_boundary',
    'speaker_clustering',
    'speaker_alignment',
    'role_review',
    'attribution',
    'fusion',
    'turns',
    'timeline',
    'metrics',
    'semantic_judge',
    'findings',
    'report',
    'browser_navigation',
)


def _check_evidence_references(document, errors, gaps):
    """Resolve evidence references that authorize a real gate to a real artifact.

    A gate is only as real as the artifact behind it. Under ``--verify-artifacts``
    every reference that authorizes the real-cloud or real-recording gate, and every
    denominator that reports a stage outcome, must resolve to a locally present file
    whose digest matches the declared ``evidence.sha256``. A reference that resolves
    to nothing is a blocking gap; a digest mismatch is an error.
    """
    targets = []
    for index, gate in enumerate(document.get('gates') or []):
        if gate['gate'] in ('real_cloud_verified', 'real_recording_verified') \
                and gate['state'] == 'verified':
            for position, entry in enumerate(gate.get('evidence') or []):
                targets.append((f'/gates/{index}/evidence/{position}', entry))
    # An evaluation's evidence is the artifact a measurement rests on, so it is resolved
    # on the same terms as the gate and denominator evidence. Leaving it out was the one
    # place a `measured` claim could cite a path that resolves to nothing.
    for index, evaluation in enumerate(document.get('evaluations') or []):
        for position, entry in enumerate(evaluation.get('evidence') or []):
            targets.append((f'/evaluations/{index}/evidence/{position}', entry))
    for index, entry in enumerate(document.get('denominators') or []):
        for position, item in enumerate(entry.get('evidence') or []):
            targets.append((f'/denominators/{index}/evidence/{position}', item))
    for location, entry in targets:
        path = Path(entry['location'])
        if not path.is_file():
            gaps.append(f'{location} ({entry["evidence_id"]}) does not resolve to a locally present '
                        'artifact, so the claim it authorizes is backed by nothing checkable')
            continue
        digest = entry.get('sha256')
        if digest is None:
            gaps.append(f'{location} ({entry["evidence_id"]}) is present but declares no sha256, so '
                        'it is not bound to the artifact')
            continue
        try:
            actual = sha256_file(path)
        except OSError as error:
            gaps.append(f'{location} ({entry["evidence_id"]}) is unreadable: {error}')
            continue
        if actual != digest:
            errors.append(
                f'{location} ({entry["evidence_id"]}): declared sha256 does not match the preserved '
                'artifact')


def _check_exposure_scan_findings(document, errors):
    """A record may not declare material it also declares clean.

    ``exposure_scan.clean`` is the record's own claim about PRD-N004, and
    ``exposure_scan.findings`` is the record's own list of what it found. Only the
    first was ever read, so a record could carry a ``real_recording_in_git`` finding
    *and* `clean: true` and still be reported ``complete`` — every gate it claims would
    rest on exactly the condition this gate exists to reject. The list is now read: a
    recorded finding means the scan is not clean, and the only way to claim clean is to
    record nothing.
    """
    declared_scan = document.get('exposure_scan') or {}
    recorded = declared_scan.get('findings') or []
    if not recorded:
        return
    categories = sorted({str(item.get('category')) for item in recorded})
    message = (f'/exposure_scan/findings: the record itself declares {len(recorded)} exposure '
               f'finding(s) ({", ".join(categories)}); a record that records material which must '
               'not be committed cannot be acceptance evidence, because every gate it claims '
               'rests on the same scan')
    if declared_scan.get('clean') is True:
        message += (' — and it declares `clean: true` at the same time: a clean scan is an empty '
                    'findings list, so the declaration contradicts the list it ships with')
    errors.append(message)


def _check_exposure_roots(document, errors, gaps, unchecked_roots, repository_root):
    """A control that was not exercised is not a passed control."""
    if repository_root is None:
        gaps.append('the repository was not scanned for committed recordings '
                    '(run `acceptance check --repository-root <repo>`), so the PRD-N004 '
                    'no-committed-audio control was not exercised')
    else:
        requested = str(repository_root)
        if not Path(requested).is_dir():
            errors.append(f'--repository-root {requested}: the requested repository root does not '
                          'exist or is not a directory, so the PRD-N004 no-committed-audio control '
                          'was never exercised')
    declared = (document.get('exposure_scan') or {}).get('scan_roots') or []
    unresolved_declared = [entry for entry in declared if not Path(entry).is_dir()]
    if unresolved_declared:
        gaps.append('declared exposure_scan.scan_roots entries do not resolve to directories, so '
                    'the recorded clean scan covers less than it claims: '
                    + ', '.join(unresolved_declared))
    if unchecked_roots and not unresolved_declared and repository_root is None:
        # Defensive: any root that could not be walked must surface, whatever the
        # reason, rather than being silently treated as clean.
        gaps.append('these scan roots could not be walked and were therefore not scanned: '
                    + ', '.join(sorted(set(unchecked_roots))))


def _check_denominators(document, errors, gaps):
    seen = set()
    totals = {}
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
        totals[entry['stage']] = entry['expected_total']
    if not seen:
        gaps.append('no stage denominator was reported at all, so failures, unknowns and abstentions '
                    'cannot be counted (the M1 gate forbids a success-only denominator)')
    return seen, totals


def _check_denominator_coverage(document, errors, stages, stage_totals):
    """A declared evaluation must have its denominator reported, and it must not be empty.

    An omitted stage is indistinguishable from "nothing failed there", which is how
    a success-only denominator gets built by omission. This applies to **every**
    declared evaluation, not only the measured ones: a `failed`, `abstained` or
    `not_attempted` evaluation is exactly the failure/unknown/abstention that #85
    requires to be counted, so it needs its denominator too. A record that claims the
    real-recording gate must report at least one evaluation and must account for
    every M1 stage; a record that claims nothing may report nothing, and its
    emptiness is visible as a pending gate rather than as an invalid claim.

    The dual is also enforced: a stage that was evaluated against **zero** records
    cannot have produced a measurement. Reporting every stage as an empty denominator
    satisfies "every stage is accounted for" while nothing was actually evaluated, so
    a `measured` evaluation whose stage has ``expected_total = 0`` is refused.
    """
    evaluations = list(document.get('evaluations') or [])
    claims_real_recording = (_gate(document, 'real_recording_verified') or {}).get(
        'state') == 'verified'
    if claims_real_recording:
        if not evaluations:
            errors.append('/evaluations: a record claiming the real-recording gate must report at '
                          'least one evaluation; a record with no evaluation reports no measurement '
                          'at all')
        missing = [stage for stage in M1_STAGES if stage not in stages]
        if missing:
            errors.append(
                '/denominators: an M1 acceptance must account for every stage, including the stages '
                'where nothing failed or that did not apply; these stages are absent, so their '
                'failures, unknowns and abstentions are unreported: ' + ', '.join(missing))
    for evaluation in evaluations:
        stage = EVALUATION_STAGE.get(evaluation['kind'])
        if stage is None:
            # A kind the schema allows but no stage maps: report it rather than
            # crashing, so a future evaluation kind cannot turn a check into a traceback.
            errors.append(f'/evaluations: evaluation {evaluation["evaluation_id"]} has kind '
                          f'{evaluation["kind"]!r}, which maps to no denominator stage; add the '
                          'mapping so its failures and abstentions are counted')
            continue
        if stage not in stages:
            errors.append(
                f'/denominators: the {stage} stage has no reported denominator, but evaluation '
                f'{evaluation["evaluation_id"]} is declared with status {evaluation["status"]!r}; an '
                'omitted stage is indistinguishable from "nothing failed", which is a success-only '
                'denominator')
        elif evaluation['status'] == 'measured' and not stage_totals.get(stage):
            errors.append(
                f'/evaluations: evaluation {evaluation["evaluation_id"]} is declared '
                f'`measured`, but its stage {stage} reports a denominator of 0 records; a '
                'measurement cannot rest on a denominator that was evaluated against nothing, and '
                'a record of all-empty denominators accounts for every stage while measuring none')


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
    _check_artifact_identity(document, errors, gaps)
    audio_size_applied, audio_size_not_applicable = _check_declared_audio_size(document, errors)
    _check_evaluations(document, errors, gaps, observations)
    declared_evidence = _check_evidence_identity(document, errors)
    stages, stage_totals = _check_denominators(document, errors, gaps)
    _check_denominator_coverage(document, errors, stages, stage_totals)
    _check_exposure_scan_findings(document, errors)
    _check_traceability(document, errors, gaps, declared_evidence, observations)
    _check_manual_role_corrections(document, errors, gaps)
    _check_human_review_coverage(document, errors, gaps)

    secret_findings, unchecked_roots, files_inspected, unscanned_files, skipped_directories = \
        scan_secrets(document, repository_root=repository_root)
    declared_scan = document.get('exposure_scan') or {}
    for finding in secret_findings:
        errors.append(f'/exposure_scan: {finding["category"]} at {finding["location"]}')
    if declared_scan.get('clean') is True and secret_findings:
        errors.append('/exposure_scan/clean: the record declares a clean scan but the scan found '
                      'material that must not be committed')
    if declared_scan.get('clean') is not True and not secret_findings:
        gaps.append('the exposure scan is not recorded as clean; run the scan before the record '
                    'is used as acceptance evidence')
    _check_exposure_roots(document, errors, gaps, unchecked_roots, repository_root)
    # A file the walk reached but could not read is not a clean file: it is an
    # unexercised part of the control. Files that are *provably* not text (their bytes
    # are not valid UTF-8, or they are a declared binary format) cannot hold a plaintext
    # credential, so they are reported as coverage but do not block; a file that could
    # not be opened or is too large to read is a control failure and does block.
    files_not_text = []
    policy_sources = []
    for entry in unscanned_files:
        if entry['reason'] == 'binary/unscannable':
            files_not_text.append(entry['location'])
        elif entry['reason'] == 'detection policy source':
            policy_sources.append(entry['location'])
        else:
            errors.append(f'/exposure_scan: {entry["location"]} was reached by the scan but not '
                          f'read ({entry["reason"]}), so it is unverified rather than clean')

    artifacts_checked = None
    if verify_artifacts:
        artifacts_checked = _verify_artifact_hashes(document, errors, gaps)
        _check_evidence_references(document, errors, gaps)

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
        # Reported so a human-layer count cannot be inflated invisibly (the digest rule
        # above rejects the reuse; this makes the extent auditable either way).
        'human_review_count': len(document.get('human_reviews') or []),
        'stages_reported': sorted(stages),
        'artifacts_hashed': artifacts_checked,
        'artifacts_verification_requested': verify_artifacts,
        # Only a root that was actually walked counts as scanned; a missing or
        # non-directory root is an error above, never a reported "scanned: yes".
        'repository_scanned': bool(repository_root is not None
                                   and Path(str(repository_root)).is_dir()),
        'repository_root': str(repository_root) if repository_root is not None else None,
        'repository_files_inspected': files_inspected,
        'repository_files_unread': sorted(files_not_text),
        'detection_policy_sources_skipped': sorted(policy_sources),
        # Named so "clean" cannot silently mean "I did not look": the walk does not
        # descend into environment/build directories, and which ones it passed over is
        # part of the coverage claim.
        'repository_directories_skipped': skipped_directories,
        'unchecked_scan_roots': unchecked_roots,
        'secret_findings': secret_findings,
        # Blocking gaps and non-blocking observations are reported separately so a
        # reader can tell "this must be fixed before acceptance" from "this is a fact
        # about the record".
        'gaps': gaps,
        'observations': observations,
        'real_recording_open_items': gates_open,
        # Which parts of a verified real-recording claim were checked against measured
        # material, and which only against a value the record declares about itself. A
        # declared-only condition is still a real gate condition, but a reader is
        # entitled to see that the checker reconciled it with the record rather than
        # with the recording.
        'declared_only_controls': [
            'sample duration_ms (the authorized 5–20 minute band): evaluated against the '
            'declared value only; the checker does not decode the audio, so a declared '
            'duration is not a measured one',
            'sample source/device/authorization: the record states these; the checker can '
            'refuse a synthetic label but cannot confirm a recording is authorized',
            'exposure_scan.clean: the record declares this flag and the checker never rewrites '
            'it; when --repository-root is given the scan re-derives the finding independently '
            'and a declared clean contradicts a detected finding as an error rather than '
            'updating the flag',
            'audio size vs the sample\'s own parameters: the arithmetic relation only exists '
            'for the raw PCM encodings in `PCM_ENCODING_BYTES_PER_SAMPLE`, so it is applied '
            'to the samples named in `audio_size_arithmetic_applied` and not to those in '
            '`audio_size_arithmetic_not_applicable` (compressed/containerised or unrecognised '
            'encodings). Those samples declare a duration that no size relation corroborates, '
            'so no arithmetic was invented for them; this is a named non-application, not a '
            'passed control',
        ],
        # Which samples the size arithmetic actually ran for, so "the control ran" is a
        # fact about named samples rather than an inference from a green verdict.
        'audio_size_arithmetic_applied': [
            {'sample_id': sample_id, 'encoding': encoding}
            for sample_id, encoding in audio_size_applied],
        'audio_size_arithmetic_not_applicable': [
            {'sample_id': sample_id, 'encoding': encoding}
            for sample_id, encoding in audio_size_not_applicable],
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
                 f'**{"yes" if result["repository_scanned"] else "no"}**'
                 + (f' (root `{result["repository_root"]}`, '
                    f'{result["repository_files_inspected"]} file(s) inspected)'
                    if result['repository_scanned'] else ''))
    if result['unchecked_scan_roots']:
        lines.append(f'- Scan roots that could not be walked: '
                     f'{", ".join(f"`{entry}`" for entry in result["unchecked_scan_roots"])}')
    if result.get('repository_files_unread'):
        lines.append(f'- Files the scan reached but could not decode as text: '
                     f'{len(result["repository_files_unread"])} (reported as unverified coverage, '
                     'not as clean)')
    if result.get('detection_policy_sources_skipped'):
        lines.append(f'- Detection-policy sources excluded from the scan: '
                     f'{len(result["detection_policy_sources_skipped"])} file(s) that define or '
                     'exercise the detection patterns themselves, so a match in them is the '
                     'control, not a leak')
    if result.get('repository_directories_skipped'):
        names = sorted({Path(entry).name
                        for entry in result['repository_directories_skipped']})
        lines.append(f'- Directories the scan did not descend into: '
                     f'{", ".join(f"`{name}`" for name in names)} '
                     f'({len(result["repository_directories_skipped"])} in total); their contents '
                     'are not covered by this scan')
    lines.append('')
    lines.append('## Conditions checked against declared values')
    lines.append('')
    lines.append('These gate conditions are reconciled with what the record states rather than with '
                 'a measurement of the recording. They are real conditions, but a pass here does not '
                 'mean the checker observed them:')
    lines.append('')
    for item in result.get('declared_only_controls') or []:
        lines.append(f'- {item}')
    if result.get('audio_size_arithmetic_not_applicable'):
        described = ', '.join(f'`{item["sample_id"]}` ({item["encoding"]})'
                              for item in result['audio_size_arithmetic_not_applicable'])
        lines.append(f'- Audio size arithmetic NOT applied to: {described}. For these samples '
                     'no relation between the declared duration and the declared artifact size '
                     'was checked, because none exists for their encoding.')
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
