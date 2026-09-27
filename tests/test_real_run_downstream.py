"""Opt-in, read-only downstream replay of the private Issue #115 recording.

The source Run stays outside Git. This test only consumes its persisted ASR,
diarization, human role decision and fused evidence; it never calls a provider.
Run with ``py -3.12 -m unittest tests.test_real_run_downstream -v``. Set
``AIVOICEBENCH_REAL_RUN_DIR`` when the private Run is not in the sibling
``aivoicebench-data/data/output`` directory. Missing data skips the test.
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


RUN_ID = 'RUN-8f584eb322494ac99767e1a248e4b0bd'
DEFAULT_RUN_DIR = (Path(__file__).resolve().parents[2] / 'aivoicebench-data'
                   / 'data' / 'output' / RUN_ID)


def _run_directory():
    configured = os.environ.get('AIVOICEBENCH_REAL_RUN_DIR')
    return Path(configured).resolve() if configured else DEFAULT_RUN_DIR


def _read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


@unittest.skipUnless(_run_directory().is_dir(),
                     'Private Run absent; set AIVOICEBENCH_REAL_RUN_DIR to opt in')
class RealRunDownstreamReplayTests(unittest.TestCase):
    def test_reviewed_evidence_replays_without_asr_or_judge(self):
        directory = _run_directory()
        manifest = _read_json(directory / 'manifest.json')
        self.assertEqual(manifest['run_id'], RUN_ID)
        analysis = directory / 'analysis' / manifest['analysis_id']
        review = _read_json(directory / 'role-review' / 'role-mapping-REV-0001.json')
        self.assertEqual(len(review['decisions']), 5)
        self.assertEqual({key.split(':')[-1]: value
                          for key, value in review['decisions'].items()}, {
            'speaker_0': 'tester', 'speaker_1': 'tester',
            'speaker_2': 'unknown', 'speaker_3': 'device',
            'speaker_4': 'device',
        })
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

        identity = {'run_id': RUN_ID,
                    'case_id': manifest.get('case_ref') or 'CASE-auto',
                    'execution_kind': manifest['execution_kind']}
        events, evidence, status, reason = detect_events(
            fused, turns, run_id=identity['run_id'], case_id=identity['case_id'])
        timeline = generate_timeline(fused, turns, events, evidence, status,
                                     reason, **identity)
        scoped_timeline = dict(timeline, analysis_id=manifest['analysis_id'])
        metrics = compute_timeline_metrics(scoped_timeline)

        self.assertEqual(timeline['run_id'], RUN_ID)
        self.assertEqual(len(timeline['events']), len(events))
        self.assertEqual(len(fused['segments']), 195)
        self.assertEqual(len(turns['turns']), 45)
        self.assertEqual(len(events), 283)
        self.assertEqual(timeline['status'], 'partial')
        self.assertEqual(len(timeline['gaps']), 89)
        self.assertEqual(metrics['counts']['observed'], 26)
        self.assertEqual(len(metrics['metrics']), 327)
        coverage = next(item for item in metrics['metrics'] if item['name'] == 'coverage')
        self.assertEqual(coverage['aggregation']['sample_count'], 5)
        self.assertEqual(coverage['aggregation']['total_count'], 45)
        self.assertEqual(timeline_errors(timeline), [])
        self.assertEqual(schema_errors(timeline, 'event-timeline'), [])
        for metric in metrics['metrics']:
            self.assertEqual(metric_errors(metric, timeline), [])
        # Pin the eligible results from this authorized Run. A future change that
        # bridges an unknown interval or erases confirmed evidence must fail here.
        print(f'Issue #115 replay: {len(fused["segments"])} fused segments, '
              f'{len(turns["turns"])} turns, {len(events)} events, '
              f'{metrics["counts"]["observed"]} observed metrics; '
              f'timeline={timeline["status"]}, metrics={metrics["status"]}')


if __name__ == '__main__':
    unittest.main()
