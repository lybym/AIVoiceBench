"""Evidence-linked, revision-scoped report contract (PRD-F013, PRD-N001/N002/N006).

The report layer has to answer, from one persisted AnalysisRevision:

- which revision and which manual role decision produced it;
- whether it is a provisional/diagnostic view or a role-confirmed one;
- which evidence is deterministic, which is semantic and which is human-reviewed;
- where each published region sits on the audio timeline, with its artifact refs;
- which stages failed, abstained or never ran, and why a metric list is empty.

Two properties matter most and are pinned here: a report never overwrites an
earlier revision's report, and reporting is deterministic for a given revision
(no invented timestamp or per-call identifier, and the report never lists its own
output as evidence), so regenerating cannot silently change what a reviewer read.

The document-only fixture runs everywhere; the media-dependent end-to-end shape
stays with ``tests/test_role_review.py`` and real recording acceptance with #85.
"""

import json
import tempfile
import unittest
from pathlib import Path

from aivoicebench.import_artifacts import ImportRun
from aivoicebench.import_report import EVIDENCE_CLASSES, build_report_payload, write_import_report
from tests.test_workbench import (EVIDENCE_END_MS, EVIDENCE_START_MS, METRIC_VALUE,
                                  build_document_run)


def reopen_run(directory):
    """A Run object bound to an already persisted revision (as the pipeline holds it)."""
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    run = ImportRun.__new__(ImportRun)
    run.directory = directory
    run.manifest = manifest
    run.analysis = directory / 'analysis' / manifest['analysis_id']
    return run


class ReportCase:
    """Shared setup: one document-only Run, its report and both rendered files."""

    gate = 'complete_review'

    def build_report(self, gate=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.manifest = build_document_run(
            Path(self._tmp.name) / 'runs', gate=gate or self.gate)
        self.run = reopen_run(self.directory)
        write_import_report(self.run)
        self.payload = json.loads((self.run.analysis / 'report.json').read_text(encoding='utf-8'))
        self.markdown = (self.run.analysis / 'report.md').read_text(encoding='utf-8')


class ReportProjectionTests(ReportCase, unittest.TestCase):
    """A role-confirmed revision publishes all three evidence classes."""

    def setUp(self):
        self.build_report()

    def test_report_is_written_inside_its_own_revision(self):
        self.assertEqual(self.payload['report_scope'], 'analysis_revision')
        self.assertEqual(self.payload['analysis_id'], self.manifest['analysis_id'])
        self.assertTrue((self.directory / 'analysis' / self.manifest['analysis_id']
                         / 'report.md').is_file())
        self.assertEqual(self.payload['workbench_document_id'],
                         build_report_payload(self.run)['workbench_document_id'])

    def test_report_separates_the_three_evidence_classes(self):
        classes = self.payload['evidence_classes']
        self.assertEqual(set(EVIDENCE_CLASSES) | {'other'}, set(classes))
        deterministic = {entry['kind'] for entry in classes['deterministic']}
        self.assertTrue({'acoustic-segments', 'timeline', 'metrics'} <= deterministic)
        semantic = {entry['kind'] for entry in classes['semantic']}
        self.assertTrue({'transcript', 'findings'} <= semantic)
        human = {entry['kind'] for entry in classes['human_reviewed']}
        self.assertEqual(human, {'speaker-role-mapping'})
        # The gate/review surface is derived state, not a human decision artifact.
        other = {entry['kind'] for entry in classes['other']}
        self.assertIn('speaker-role-review', other)
        for entry in classes['deterministic'] + classes['semantic'] + classes['human_reviewed']:
            self.assertTrue(entry['sha256'], entry)
        self.assertIn('## 证据分级', self.markdown)
        self.assertIn('human_reviewed', self.markdown)

    def test_report_never_lists_its_own_output_as_evidence(self):
        kinds = {entry['kind'] for entry in self.payload['artifacts']}
        self.assertNotIn('report_json', kinds)
        self.assertNotIn('report_markdown', kinds)
        provenance_kinds = {item['kind'] for item in self.payload['provenance']['artifacts']}
        self.assertNotIn('report_json', provenance_kinds)
        self.assertNotIn('report_markdown', provenance_kinds)

    def test_evidence_index_keeps_the_persisted_intervals(self):
        index = {region['region_id']: region for region in self.payload['evidence_index']}
        self.assertEqual((index['event:EVT-1']['start_ms'], index['event:EVT-1']['end_ms']),
                         (1200.0, 1213.75))
        self.assertEqual((index['metric:MET-1']['start_ms'], index['metric:MET-1']['end_ms']),
                         (EVIDENCE_START_MS, EVIDENCE_END_MS))
        self.assertIn('## 证据索引', self.markdown)
        self.assertIn('event:EVT-1', self.markdown)

    def test_report_names_the_manual_revision_and_separates_it_from_the_machine_output(self):
        revision = self.payload['revision']
        self.assertEqual(revision['revision_id'], 'REV-abcdef123456')
        self.assertEqual(revision['reviewer'], 'zhang')
        self.assertTrue(self.payload['gate']['role_dependent_available'])
        self.assertIn('## 报告范围与修订', self.markdown)
        self.assertIn('REV-abcdef123456', self.markdown)
        self.assertIn('本报告只描述上述 revision', self.markdown)

    def test_observed_metric_value_travels_verbatim(self):
        regions = {region['region_id']: region for region in self.payload['evidence_index']}
        self.assertIn('metric:MET-1', regions)
        stored = json.loads((self.run.analysis / 'metrics.json').read_text(encoding='utf-8'))['data']
        self.assertEqual(stored['metrics'][0]['value'], METRIC_VALUE)
        self.assertIn(str(METRIC_VALUE), self.markdown)

    def test_report_generation_is_idempotent_for_one_revision(self):
        before = (self.run.analysis / 'report.json').read_bytes()
        before_md = (self.run.analysis / 'report.md').read_bytes()
        write_import_report(self.run)
        self.assertEqual((self.run.analysis / 'report.json').read_bytes(), before)
        self.assertEqual((self.run.analysis / 'report.md').read_bytes(), before_md)


class ProvisionalReportTests(ReportCase, unittest.TestCase):
    """Before the manual role decision the report is diagnostic and says so."""

    gate = 'awaiting_role_review'

    def setUp(self):
        self.build_report()

    def test_provisional_report_is_labelled_and_omits_role_dependent_evidence(self):
        self.assertFalse(self.payload['gate']['role_dependent_available'])
        self.assertEqual(self.payload['gate']['status'], 'awaiting_role_review')
        self.assertIsNone(self.payload['revision'])
        self.assertEqual(
            {entry['kind'] for entry in self.payload['evidence_classes']['human_reviewed']}, set())
        tracks = {region['track_id'] for region in self.payload['evidence_index']}
        self.assertEqual(tracks, {'acoustic', 'speaker'})
        self.assertIn('provisional 视图', self.markdown)
        self.assertIn('awaiting_role_review', self.markdown)

    def test_abstaining_stages_are_listed_instead_of_omitted(self):
        stages = {item['stage'] for item in self.payload['unavailable']}
        self.assertTrue({'turns', 'timeline', 'metrics', 'findings'} <= stages)
        self.assertIn('## 未完成、失败与弃权', self.markdown)
        self.assertIn('awaiting_role_review', self.markdown)

    def test_empty_metric_list_is_explained_by_evidence(self):
        gap = self.payload['abstentions']['metrics_gap']
        codes = {reason['code'] for reason in gap['reasons']}
        self.assertIn('roles_not_confirmed', codes)
        self.assertIn('roles_not_confirmed', self.markdown)
        self.assertIn('## 指标可用性', self.markdown)

    def test_no_metric_region_is_synthesised_for_a_blocked_run(self):
        self.assertEqual(self.payload['gate']['view_kind'], 'provisional')
        self.assertEqual(self.payload['metrics'], [])
        self.assertEqual([region for region in self.payload['evidence_index']
                          if region['kind'] == 'metric'], [])
        abstaining = [artifact for artifact in self.payload['artifacts']
                      if artifact['kind'] == 'metrics']
        self.assertEqual(len(abstaining), 1, 'the abstaining metrics envelope stays registered')
        self.assertIn('## 指标结果', self.markdown)
        self.assertIn('本次 revision 没有 MetricResult', self.markdown)


class ReportEscapingTests(ReportCase, unittest.TestCase):
    """Profile text is escaped; a report must never carry executable markup."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.manifest = build_document_run(
            Path(self._tmp.name) / 'runs', gate='complete_review',
            profile={'device': 'A|<script>alert(1)</script>', 'supplier': '测试供应商'})
        self.run = reopen_run(self.directory)
        write_import_report(self.run)
        self.markdown = (self.run.analysis / 'report.md').read_text(encoding='utf-8')

    def test_profile_values_are_escaped(self):
        self.assertNotIn('<script>', self.markdown)
        self.assertIn('测试供应商', self.markdown)


class ReportEvidenceIntegrityTests(unittest.TestCase):
    """A report must not present unreadable evidence as absent evidence."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.manifest = build_document_run(Path(self._tmp.name) / 'runs')
        self.run = reopen_run(self.directory)

    def test_a_corrupt_document_is_reported_as_an_incomplete_chain(self):
        (self.run.analysis / 'timeline.json').write_text('{"events": [', encoding='utf-8')
        payload, markdown = self._write()
        self.assertEqual(payload['evidence_integrity']['status'], 'incomplete')
        self.assertEqual(payload['evidence_integrity']['unreadable_documents'], ['timeline.json'])
        stages = {item['stage']: item for item in payload['unavailable']}
        self.assertEqual(stages['timeline']['status'], 'unreadable')
        self.assertEqual(stages['timeline']['kind'], 'failed')
        # The corrupt document is left exactly as found: the report records the gap and
        # does not rewrite the evidence it could not read.
        self.assertEqual((self.run.analysis / 'timeline.json').read_text(encoding='utf-8'),
                         '{"events": [')
        # The gap is prose a reviewer reads, not an empty table they have to interpret.
        self.assertIn('证据链不完整', markdown)
        self.assertIn('timeline.json', markdown)
        # A stage that merely has not run is a different category from one that abstained.
        self.assertIn('`not_run`', markdown)

    def test_an_unreadable_judge_document_is_reported_instead_of_ignored(self):
        (self.run.analysis / 'judge-results.json').write_text('not json', encoding='utf-8')
        payload, markdown = self._write()
        self.assertEqual(payload['evidence_integrity']['status'], 'incomplete')
        self.assertIn('judge-results.json', payload['evidence_integrity']['unreadable_documents'])
        self.assertIn('judge-results.json', markdown)

    def test_a_readable_revision_reports_an_intact_chain(self):
        payload, markdown = self._write()
        self.assertEqual(payload['evidence_integrity']['status'], 'ok')
        self.assertEqual(payload['evidence_integrity']['unreadable_documents'], [])
        self.assertNotIn('证据链不完整', markdown)

    def test_a_skipped_duplicate_record_is_banner_reported_too(self):
        # A skipped record makes the chain incomplete exactly as an unreadable document
        # does: the banner must match `evidence_integrity.status`, not just one cause.
        path = self.run.analysis / 'timeline.json'
        timeline = json.loads(path.read_text(encoding='utf-8'))
        timeline['data']['events'] = [timeline['data']['events'][0], dict(timeline['data']['events'][0])]
        path.write_text(json.dumps(timeline, ensure_ascii=False), encoding='utf-8')
        payload, markdown = self._write()
        self.assertEqual(payload['evidence_integrity']['status'], 'incomplete')
        self.assertEqual(payload['evidence_integrity']['skipped_records'],
                         [{'document': 'timeline.json', 'region_id': 'event:EVT-1'}])
        self.assertIn('证据链不完整', markdown)
        self.assertIn('因 id 重复未能发布', markdown)

    def _write(self):
        _, payload, _ = write_import_report(self.run)
        return payload, (self.run.analysis / 'report.md').read_text(encoding='utf-8')


class ReportRevisionLifecycleTests(unittest.TestCase):
    """A new human revision must not overwrite the previous revision's report."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory, self.manifest = build_document_run(Path(self._tmp.name) / 'runs',
                                                           gate='awaiting_role_review')
        self.first = reopen_run(self.directory)
        write_import_report(self.first)
        self.first_markdown = (self.first.analysis / 'report.md').read_bytes()
        self.first_json = (self.first.analysis / 'report.json').read_bytes()

    def test_a_second_revision_gets_its_own_report_and_leaves_the_first_untouched(self):
        # Simulate the pipeline's new AnalysisRevision: a new analysis directory
        # under the same Run, exactly as resume/apply_role_mapping create one.
        second_id = 'ANALYSIS-' + 'f' * 32
        second_root = self.directory / 'analysis' / second_id
        second_root.mkdir()
        for name in ('acoustic-segments.json', 'speaker-assignments.json', 'alignment.json',
                     'transcript.json', 'fused-segments.json', 'role-review.json',
                     'audio-metadata.json', 'normalized.wav'):
            source = self.first.analysis / name
            if source.is_file():
                (second_root / name).write_bytes(source.read_bytes())
        manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        manifest['analysis_id'] = second_id
        (self.directory / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

        second = reopen_run(self.directory)
        write_import_report(second)

        # The new revision has its own report...
        self.assertTrue((second_root / 'report.md').is_file())
        payload = json.loads((second_root / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['analysis_id'], second_id)
        # ...and the earlier revision's report is byte-identical.
        self.assertEqual((self.first.analysis / 'report.md').read_bytes(), self.first_markdown)
        self.assertEqual((self.first.analysis / 'report.json').read_bytes(), self.first_json)
        self.assertNotEqual(self.first.analysis, second_root)
        self.assertNotEqual(json.loads(self.first_json.decode('utf-8'))['analysis_id'], second_id)


if __name__ == '__main__':
    unittest.main()
