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

from aivoicebench.fusion import build_turns, detect_events, generate_timeline
from aivoicebench.metrics import compute_timeline_metrics


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

        fused = _read_json(analysis / 'fused-segments.json')['data']
        self.assertTrue(fused['segments'])
        self.assertTrue({'tester', 'device', 'unknown'} <= {
            segment['speaker_role'] for segment in fused['segments']})
        turns = build_turns(fused)
        self.assertEqual(turns['turns'],
                         _read_json(analysis / 'turns.json')['data']['turns'])

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
        self.assertIn(timeline['status'], ('complete', 'partial'))
        self.assertIn(metrics['status'],
                      ('observed', 'partial', 'insufficient_evidence'))
        if timeline['status'] != 'complete':
            self.assertTrue(timeline['gaps'])
        # Current Issue #115 baseline: this mixed-role Run abstains globally.
        # Deliberately do not assert zero events/metrics: the fix should make this
        # same replay pass while producing eligible, traceable results.
        print(f'Issue #115 replay: {len(fused["segments"])} fused segments, '
              f'{len(turns["turns"])} turns, {len(events)} events, '
              f'{metrics["counts"]["observed"]} observed metrics; '
              f'timeline={timeline["status"]}, metrics={metrics["status"]}')


if __name__ == '__main__':
    unittest.main()
