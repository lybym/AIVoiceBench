"""Opt-in, read-only downstream replay of the private Issue #115 recording.

The source Run stays outside Git. This test only consumes its persisted ASR,
diarization, human role decision and fused evidence; it never calls a provider.
Set ``AIVOICEBENCH_REAL_RUN_DIR`` to the private Run directory, then run
``py -3.12 -m unittest tests.test_real_run_downstream -v``. Without the
explicit opt-in, this test is skipped.
"""

import json
import os
from pathlib import Path
import unittest

from aivoicebench.alignment import align_speaker_spans
from aivoicebench.diarization import attribute_speakers
from aivoicebench.fusion import (apply_speakers, build_turns, detect_events, fuse,
                                 generate_timeline)
from aivoicebench.metrics import compute_timeline_metrics
from aivoicebench.validation import metric_errors, schema_errors, timeline_errors


def _run_directory():
    configured = os.environ.get('AIVOICEBENCH_REAL_RUN_DIR')
    return Path(configured).resolve() if configured else None


def _read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


@unittest.skipUnless(_run_directory() is not None and _run_directory().is_dir(),
                     'Private Run absent; set AIVOICEBENCH_REAL_RUN_DIR to opt in')
class RealRunDownstreamReplayTests(unittest.TestCase):
    def test_reviewed_evidence_replays_without_asr_or_judge(self):
        directory = _run_directory()
        manifest = _read_json(directory / 'manifest.json')
        self.assertEqual(manifest['run_id'], directory.name)
        analysis = directory / 'analysis' / manifest['analysis_id']
        review = _read_json(directory / 'role-review' / 'role-mapping-REV-0001.json')
        self.assertIn('unknown', review['decisions'].values())
        self.assertIn('tester', review['decisions'].values())
        self.assertIn('device', review['decisions'].values())
        self.assertEqual(_read_json(analysis / 'role-review.json')['status'],
                         'complete_review')

        # Check that the replay starts from actual saved recognition and clustering,
        # rather than a generated transcript or a new provider request.
        transcript = _read_json(analysis / 'transcript.json')
        speakers = _read_json(analysis / 'speaker-assignments.json')
        self.assertTrue(transcript['data'].get('segments'))
        self.assertTrue(speakers['data'].get('speaker_segments'))

        acoustic = _read_json(analysis / 'acoustic-segments.json')['data']
        attribution = attribute_speakers(
            speakers['data'], explicit_mapping=review['decisions'], human_role_review=True)
        alignment = align_speaker_spans(acoustic, speakers['data'], transcript_doc=transcript['data'])
        fused = fuse(acoustic, transcript['data'])
        apply_speakers(fused, speakers['data'], attribution, alignment)
        self.assertEqual(schema_errors(attribution, 'source-attribution'), [])
        self.assertEqual(schema_errors(alignment, 'speaker-alignment'), [])
        self.assertEqual(schema_errors(fused, 'fused-segments'), [])
        self.assertTrue(fused['segments'])
        self.assertTrue({'tester', 'device', 'unknown'} <= {
            segment['speaker_role'] for segment in fused['segments']})
        turns = build_turns(fused)
        segment_order = {seg['segment_id']: index
                         for index, seg in enumerate(fused['segments'])}
        unknown_indexes = {index for index, seg in enumerate(fused['segments'])
                           if seg['speaker_role'] == 'unknown'}
        for turn in turns['turns']:
            indexes = [segment_order[sid] for sid in
                       turn['tester_segment_ids'] + turn['device_segment_ids']]
            self.assertFalse(any(min(indexes) < index < max(indexes)
                                 for index in unknown_indexes),
                             f'unknown evidence splits turn {turn["turn_id"]}')

        identity = {'run_id': manifest['run_id'],
                    'case_id': manifest.get('case_ref') or 'CASE-auto',
                    'execution_kind': manifest['execution_kind']}
        events, evidence, status, reason = detect_events(
            fused, turns, run_id=identity['run_id'], case_id=identity['case_id'])
        timeline = generate_timeline(fused, turns, events, evidence, status,
                                     reason, **identity)
        scoped_timeline = dict(timeline, analysis_id=manifest['analysis_id'])
        metrics = compute_timeline_metrics(scoped_timeline)

        self.assertEqual(timeline['run_id'], manifest['run_id'])
        self.assertEqual(len(timeline['events']), len(events))
        self.assertGreater(len(turns['turns']), 0)
        self.assertGreater(len(events), 0)
        self.assertEqual(timeline['status'], 'partial')
        self.assertGreater(len(timeline['gaps']), 0)
        self.assertGreater(metrics['counts']['observed'], 0)
        coverage = next(item for item in metrics['metrics'] if item['name'] == 'coverage')
        self.assertLessEqual(coverage['aggregation']['sample_count'],
                             coverage['aggregation']['total_count'])
        self.assertEqual(coverage['aggregation']['total_count'], len(turns['turns']))
        self.assertEqual(timeline_errors(timeline), [])
        self.assertEqual(schema_errors(timeline, 'event-timeline'), [])
        for metric in metrics['metrics']:
            self.assertEqual(metric_errors(metric, timeline), [])
        # A future change that bridges an unknown interval or erases all
        # confirmed evidence must fail here. Exact sample counts stay private.


if __name__ == '__main__':
    unittest.main()
