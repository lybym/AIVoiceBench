"""One persisted-Run projection for every surface that reads a Recording Analysis Run.

Issue #27 requires the Web UI, the API and the CLI to operate on the same persisted
Run/AnalysisRevision model and to return consistent Run/revision/status semantics for
the same data. That is only true if there is exactly **one** projection: a second
reader that re-derives `status`, stage states, evidence links or reports from the same
directory would be a second analysis semantics, and the two would drift.

This module is that single projection. It has no web dependency, so it can be used
from the API process and from the CLI process alike:

* :func:`read_run_view` — the same document `GET /api/runs/{run_id}` serves and the
  CLI prints.
* :func:`list_run_views` — the same rows `GET /api/runs` serves.
* :func:`project_workbench` — the wavesurfer Evidence Workbench projection, with the
  three-state contract (absent / projection failed / projected) preserved.

Nothing here computes a metric, event, role or report. Every value is read from the
Run's own persisted documents; a missing document stays missing instead of becoming a
default that looks like measured evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Prefix every Run directory uses. Used to enumerate Runs without trusting arbitrary
#: directory names inside the output root.
RUN_PREFIX = 'RUN-'


class RunViewError(ValueError):
    """A Run view cannot be produced for this directory at all.

    Only *structural* absence is raised this way (the analysis path escapes the Run).
    A partially built or damaged Run is still returned with its real gaps, because
    "this Run has no readable evidence" and "this Run could not be read" are
    different answers. Callers translate this into their own surface: the API
    answers 404, the CLI exits non-zero with the reason.
    """


def read_json(path):
    """Read one JSON document, or ``{}`` when it is absent or unparseable.

    An unparseable document is reported as its own state by the projections that
    need that distinction (``workbench``/``report``); this helper is only for
    documents whose absence is already the answer (for example an optional
    ``web-status.json``).
    """
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def project_workbench(directory, *, tolerant=True):
    """Persisted-evidence projection for the Evidence Workbench.

    Only *absence* becomes an absent document: a Run whose directory carries no
    manifest, no ``analysis_id`` or no AnalysisRevision has no workbench, and the
    caller says so with 404. A projection that fails for any other reason is a defect
    in the evidence, not an absence of it, and must never be reported as "this record
    has no reviewable evidence" — with ``tolerant=False`` the caller sees the real error
    so it can be surfaced as a failure instead. The projection itself never computes a
    metric, event or role.
    """
    # Imported here so a caller that patches `aivoicebench.workbench.build_workbench`
    # (the fault-injection seam for the three-state contract) is honoured.
    from .workbench import NoWorkbench, build_workbench
    try:
        return build_workbench(directory)
    except NoWorkbench:
        return {}
    except (OSError, ValueError):
        if tolerant:
            return {}
        raise


def read_run_view(directory, *, include_workbench=True):
    """The canonical read view of one Recording Analysis Run's current revision.

    The returned mapping is the Run/revision/status semantics every surface must
    agree on. ``analysis_id`` is the current AnalysisRevision; ``stages`` is the
    stage ledger with each processor's explicit state and reason; every listed
    artifact reference resolves inside the Run. Raises :class:`RunViewError` when no
    view can exist for this directory.
    """
    directory = Path(directory)
    manifest = read_json(directory / 'manifest.json')
    unified = manifest.get('workflow') == 'recording_import' and not (directory / 'web-analysis').exists()
    root = directory / 'analysis' / manifest['analysis_id'] if unified else (
        directory / 'web-analysis' if (directory / 'web-analysis').is_dir() else directory)
    if not root.resolve().is_relative_to(directory.resolve()):
        raise RunViewError('Invalid analysis path')
    report = read_json(root / 'report.json')
    manifest = read_json(directory / 'manifest.json')
    timeline = read_json(root / 'timeline.json') or report.get('timeline', {})
    status_doc = read_json(directory / 'web-status.json')
    status = manifest.get('status') if unified else (status_doc.get('status') or report.get('run_summary', {}).get('status')
              or timeline.get('status') or manifest.get('status') or 'partial')

    def items(name, key, fallback):
        doc = read_json(root / (name + '.json')) or report.get(fallback, {})
        if isinstance(doc, dict) and unified:
            doc = doc.get('data') or {}
        return doc if isinstance(doc, list) else doc.get(key, [])

    def document(name):
        doc = read_json(root / (name + '.json'))
        if isinstance(doc, dict) and unified:
            return doc.get('data') or {}
        return doc if isinstance(doc, dict) else {}

    def published(name):
        """Read a registered document that is not a stage envelope.

        The alignment artifact is published as its own schema-validated document
        (like the Audio QA conditions), so it has no `data` member to unwrap.
        Reading it through the envelope helper would silently report an empty
        alignment next to a Run that has one.
        """
        doc = read_json(root / (name + '.json'))
        return doc if isinstance(doc, dict) else {}

    def audio_qa():
        """QA facts for the canonical artifact, across the Run's AnalysisRevisions.

        A completed QA envelope carries the measurements as its data. An abstaining
        envelope cannot carry data, so the measurements are read from the registered
        canonical metadata document or from the separately published condition
        document. Either way these are measurements and validity conditions, never a
        recognition, accuracy or acceptance verdict.

        Canonical audio QA is measured once per Run and preserved by `resume`, which
        deliberately keeps the ingestion/normalization/audio_qa stage state while
        creating a new analysis directory without re-emitting the QA envelope. Reading
        only the current revision would therefore report "never measured QA" next to a
        `partial` QA ledger. The current revision's envelope wins when it exists; the
        manifest's latest registered `audio-qa` artifact is the fallback.

        A Run measured before conditions existed has measurements but no conditions.
        It is reported as `unassessed` rather than as an empty list, so an absent
        condition set can never be mistaken for a satisfied one.
        """
        def view(envelope, measurements):
            resolved = dict(measurements)
            if 'conditions' not in resolved:
                resolved['conditions'] = [{
                    'condition_id': 'conditions_version', 'status': 'unassessed',
                    'basis': 'this Run predates recorded Audio QA conditions',
                    'limitation': 'Absent conditions are not satisfied conditions; re-import to measure them'}]
            return {'status': envelope.get('status'), 'reason': envelope.get('reason'),
                    'measurements': resolved}
        if not unified:
            return {}
        registered = {item['artifact_id']: item for item in manifest.get('artifacts', [])}
        current = read_json(root / 'audio-qa.json')
        envelope = current if isinstance(current, dict) and current else None
        if envelope is None:
            latest = next((item for item in reversed(manifest.get('artifacts', []))
                           if item['kind'] == 'audio-qa'), None)
            if latest is None:
                return {}
            candidate = read_json(directory / latest['path'])
            if not isinstance(candidate, dict) or not candidate:
                return {}
            # The measurements live in the documents this envelope references, exactly as
            # they do for the current revision, so follow its own refs.
            envelope, registered_refs = candidate, list(candidate.get('artifact_refs', []))
        else:
            registered_refs = list(envelope.get('artifact_refs', []))
        data = envelope.get('data')
        if isinstance(data, dict) and data:
            return view(envelope, data)
        for ref in registered_refs:
            artifact = registered.get(ref)
            if artifact is None:
                continue
            path = directory / artifact['path']
            if not path.is_file() or artifact['kind'] not in ('audio_qa_conditions', 'audio_metadata'):
                continue
            published_document = read_json(path)
            measurements = (published_document.get('measurements') if artifact['kind'] == 'audio_qa_conditions'
                            else published_document.get('normalized'))
            if isinstance(measurements, dict) and measurements:
                return view(envelope, measurements)
        return {}

    if unified:
        timeline = timeline.get('data') or {}
    diarization_doc = document('speaker-assignments')
    alignment_doc = published('alignment')
    fused_segments = items('fused-segments', 'segments', 'fused_segments')
    metrics_items = items('metrics', 'metrics', 'metrics')
    # A blank metric list must be explainable from evidence, with counts, instead of
    # looking like the UI dropped fields. This is computed from the same documents
    # the reader can open, and it never invents a measurement.
    from .alignment import explain_metric_gap
    metrics_gap = explain_metric_gap(
        {'segments': fused_segments}, timeline, {'metrics': metrics_items}, alignment_doc)

    def role_review():
        """The manual role gate for this Run's current AnalysisRevision.

        The revision's own published document is authoritative because it records the
        gate state that revision was produced under. A Run analysed before the gate
        existed has no such document, so an equivalent surface is built from its own
        preserved speaker clusters rather than reported as missing.
        """
        document_payload = read_json(root / 'role-review.json')
        if isinstance(document_payload, dict) and document_payload:
            return document_payload
        if not unified:
            return {}
        from .role_review import build_role_review
        try:
            return build_role_review(directory)
        except (OSError, ValueError):
            return {}

    return dict(transcript=(read_json(root / 'transcript.json').get('data') or {}) if unified else {},
        workbench=project_workbench(directory) if include_workbench else {},
        stages=manifest.get('stages', {}), analysis_id=manifest.get('analysis_id', ''),
        invocation_refs=[a for a in manifest.get('artifacts', []) if a['kind'] == 'provider_invocation'],
        run_id=directory.name, status=status, reason=status_doc.get('reason'),
        profile=read_json(root / 'profile.json') or manifest.get('profile', {}),
        fused_segments=fused_segments,
        acoustic_segments=items('acoustic-segments', 'segments', 'acoustic_segments'),
        speaker_segments=diarization_doc.get('speaker_segments', []),
        diarization_scope=diarization_doc.get('scope', {}),
        attribution=document('attribution'),
        alignment={'status': alignment_doc.get('status'), 'reason': alignment_doc.get('reason'),
                   'document_id': alignment_doc.get('document_id'),
                   'policy': alignment_doc.get('policy'),
                   'processor': alignment_doc.get('processor'),
                   'diagnostics': alignment_doc.get('diagnostics')} if alignment_doc else {},
        metrics_gap=metrics_gap,
        role_review=role_review(),
        audio_qa=audio_qa(),
        turns=items('turns', 'turns', 'turns'), events=timeline.get('events', []),
        timeline=timeline, metrics=metrics_items,
        judge_results=items('judge-results', 'results', 'judge_results'),
        findings=items('findings', 'findings', 'findings'),
        report_md=(root / 'report.md').read_text(encoding='utf-8') if (root / 'report.md').exists() else '',
        audio_url='/api/runs/' + directory.name + '/audio')


def run_directories(root):
    """Every Run directory directly under ``root``, newest first.

    Only names carrying the Run prefix and real directories are enumerated: a stray
    file or an unrelated directory is not a Run and must not be reported as one.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    entries = [entry for entry in root.iterdir()
               if entry.is_dir() and entry.name.startswith(RUN_PREFIX) and not entry.is_symlink()]
    return sorted(entries, key=lambda entry: entry.stat().st_mtime, reverse=True)


def list_run_views(root, *, include_workbench=False):
    """The Run listing, derived from the same projection as one Run's detail view."""
    rows = []
    for entry in run_directories(root):
        data = read_run_view(entry, include_workbench=include_workbench)
        rows.append(dict(run_id=entry.name, status=data['status'], analysis_id=data['analysis_id'],
                         device=data['profile'].get('device'), created=entry.stat().st_mtime))
    return rows
