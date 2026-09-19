"""AC3 evaluation-scaffold tests (Issue #23).

AC3 asks for a *manually annotated real recording* report. This environment has no
such sample set, and the scaffold must say so rather than produce a
zero-error result. These tests therefore cover two things:

1. the scaffold reports ``no_annotated_sample_set`` when nothing was supplied; and
2. the scoring arithmetic it would apply is correct and deterministic, verified on
   hand-computed synthetic inputs.

Passing these tests is **not** AC3 acceptance evidence. Any document produced here
is fixture-level software evidence only.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aivoicebench.vad_evaluation import (
    EMPTY_ANNOTATION_SET, EVALUATION_POLICY_VERSION, NO_ANNOTATED_SAMPLE_SET,
    AnnotationError, evaluate, evaluate_files, load_annotation,
)

AUDIO_SHA = 'c' * 64


def annotation_document(intervals=((500.0, 1000.0), (2000.0, 2600.0)), **overrides):
    document = {
        'schema_version': '1.0.0',
        'annotation_id': 'ANNOT-test-0001',
        'annotator': 'human-reviewer-A',
        'annotated_at': '2026-09-18T10:00:00+08:00',
        'source': {
            'path': 'real-recording.wav', 'sha256': AUDIO_SHA, 'duration_ms': 4000.0,
            'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE',
        },
        'boundary_uncertainty_ms': 20.0,
        'intervals': [{'start_ms': start, 'end_ms': end} for start, end in intervals],
        'notes': 'Fixture annotation used to verify the scoring arithmetic only.',
    }
    document.update(overrides)
    return document


def acoustic_document(segments, sha=AUDIO_SHA, duration_ms=4000.0, method='silero_vad'):
    return {
        'schema_version': '1.0.0',
        'document_id': 'ACOUSTIC-fixture',
        'source': {
            'path': 'real-recording.wav', 'sha256': sha, 'duration_ms': duration_ms,
            'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE',
        },
        'processor': {'method': method, 'processor_version': '1.0.0',
                      'parameters': {'frame_samples': 512, 'hop_samples': 512,
                                     'threshold': 0.5, 'min_speech_ms': 250,
                                     'min_silence_ms': 100}},
        'status': 'complete' if segments else 'insufficient_evidence',
        'reason': None if segments else 'no speech',
        'segments': [{
            'segment_id': f'SEG-{index:04d}', 'start_ms': start, 'end_ms': end,
            'confidence': 0.9, 'source': 'acoustic', 'method': method,
            'uncertainty_ms': 32.0,
            'frame_stats': {'peak_speech_probability': 0.95,
                            'mean_speech_probability': 0.8, 'threshold': 0.5,
                            'frames_above_threshold': 4, 'frame_count': 6},
        } for index, (start, end) in enumerate(segments)],
    }


class NoAnnotatedSampleSetTests(unittest.TestCase):
    """The scaffold must refuse to invent AC3 evidence."""

    def test_missing_sample_set_reports_not_performed(self):
        result = evaluate(None, [])
        self.assertEqual(result['status'], NO_ANNOTATED_SAMPLE_SET)
        self.assertFalse(result['has_annotated_sample_set'])
        self.assertIsNone(result['denominator'])
        self.assertIsNone(result['coverage'])
        self.assertIsNone(result['speech_start_error_ms'])
        self.assertIsNone(result['speech_end_error_ms'])
        self.assertIsNone(result['miss'])
        self.assertIsNone(result['false_alarm'])
        self.assertIn('no accuracy claim', result['reason'].lower())
        self.assertEqual(result['evaluation_policy_version'], EVALUATION_POLICY_VERSION)

    def test_annotation_without_scored_documents_is_not_an_evaluation(self):
        annotation = load_annotation(annotation_document())
        result = evaluate(annotation, [])
        self.assertEqual(result['status'], NO_ANNOTATED_SAMPLE_SET)
        self.assertIsNone(result['denominator'])
        self.assertTrue(result['has_annotated_sample_set'])

    def test_empty_annotation_set_is_reported_separately(self):
        annotation = load_annotation(annotation_document(intervals=()))
        result = evaluate(annotation, [])
        self.assertEqual(result['status'], EMPTY_ANNOTATION_SET)
        self.assertEqual(result['denominator']['annotated_intervals'], 0)
        self.assertIsNone(result['speech_start_error_ms'])

    def test_cli_without_annotation_exits_2_and_says_so(self):
        import contextlib
        import io
        from aivoicebench.__main__ import main
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = main(['vad-eval'])
        self.assertEqual(exit_code, 2)
        self.assertIn('NO_ANNOTATED_SAMPLE_SET', buffer.getvalue())


class AnnotationValidationTests(unittest.TestCase):
    """An unscorable annotation must fail loudly, not score against nothing."""

    def test_annotation_document_requires_the_recording_digest(self):
        document = annotation_document()
        del document['source']['sha256']
        with self.assertRaises(AnnotationError):
            load_annotation(document)

    def test_annotation_rejects_a_non_boolean_uncertainty(self):
        with self.assertRaises(AnnotationError):
            load_annotation(annotation_document(boundary_uncertainty_ms='about 20ms'))

    def test_annotation_rejects_a_negative_uncertainty(self):
        with self.assertRaises(AnnotationError):
            load_annotation(annotation_document(boundary_uncertainty_ms=-5.0))

    def test_annotation_rejects_unknown_members(self):
        with self.assertRaises(AnnotationError):
            load_annotation(annotation_document(accuracy='99%'))

    def test_annotation_rejects_reversed_intervals(self):
        with self.assertRaises(AnnotationError):
            load_annotation(annotation_document(intervals=((900.0, 600.0),)))

    def test_annotation_rejects_overlapping_intervals(self):
        with self.assertRaises(AnnotationError):
            load_annotation(annotation_document(intervals=((500.0, 1000.0), (900.0, 1200.0))))

    def test_annotation_rejects_intervals_beyond_the_recording(self):
        with self.assertRaises(AnnotationError):
            load_annotation(annotation_document(intervals=((500.0, 4500.0),)))

    def test_annotation_requires_an_annotator(self):
        document = annotation_document()
        document['annotator'] = ''
        with self.assertRaises(AnnotationError):
            load_annotation(document)

    def test_boundary_uncertainty_is_kept_separate_from_provider_uncertainty(self):
        annotation = load_annotation(annotation_document())
        self.assertEqual(annotation.boundary_uncertainty_ms, 20.0)
        result = evaluate(annotation, [acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)])])
        self.assertEqual(result['annotation']['boundary_uncertainty_ms'], 20.0)
        # The provider's window resolution stays on the segment, not on the annotation.
        document = acoustic_document([(500.0, 1000.0)])
        self.assertEqual(document['segments'][0]['uncertainty_ms'], 32.0)

    def test_documents_from_other_audio_are_rejected(self):
        annotation = load_annotation(annotation_document())
        with self.assertRaises(AnnotationError):
            evaluate(annotation, [acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)],
                                                    sha='d' * 64)])

    def test_mixed_provenance_across_documents_is_rejected(self):
        """One bad document in the set must not be scored silently."""
        annotation = load_annotation(annotation_document())
        documents = [acoustic_document([(500.0, 1000.0)]),
                     acoustic_document([(2000.0, 2600.0)], sha='d' * 64)]
        with self.assertRaises(AnnotationError):
            evaluate(annotation, documents)

    def test_unreadable_annotation_file_raises(self):
        with self.assertRaises(AnnotationError):
            evaluate_files(Path('does-not-exist.json'), [])


class ScoringTests(unittest.TestCase):
    """Scoring arithmetic, verified on hand-computed inputs."""

    def setUp(self):
        self.annotation = load_annotation(annotation_document())

    def test_perfect_predictions_produce_zero_error_and_zero_false_alarm(self):
        result = evaluate(self.annotation, [
            acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)])])
        self.assertEqual(result['status'], 'evaluated')
        self.assertEqual(result['denominator']['annotated_intervals'], 2)
        self.assertEqual(result['denominator']['intervals_with_a_match'], 2)
        self.assertEqual(result['speech_start_error_ms']['mean_signed_ms'], 0.0)
        self.assertEqual(result['speech_end_error_ms']['mean_signed_ms'], 0.0)
        self.assertEqual(result['miss']['missed_intervals'], 0)
        self.assertEqual(result['false_alarm']['false_alarm_segments'], 0)
        self.assertEqual(result['coverage']['matched_interval_ratio'], 1.0)
        self.assertEqual(result['coverage']['matched_speech_ratio'], 1.0)

    def test_signed_errors_report_direction_and_magnitude(self):
        # Predicted onset 50 ms late and offset 100 ms early on the first interval.
        result = evaluate(self.annotation, [
            acoustic_document([(550.0, 900.0), (2000.0, 2600.0)])])
        matches = {item['annotation_index']: item for item in result['per_document'][0]['matches']}
        self.assertEqual(matches[0]['start_error_ms'], 50.0)
        self.assertEqual(matches[0]['end_error_ms'], -100.0)
        self.assertEqual(result['speech_start_absolute_error_ms']['mean_ms'], 25.0)
        self.assertEqual(result['speech_end_absolute_error_ms']['mean_ms'], 50.0)

    def test_missed_interval_is_counted_with_speech_denominator(self):
        result = evaluate(self.annotation, [acoustic_document([(500.0, 1000.0)])])
        # First interval 500 ms, second 600 ms of the 1100 ms annotated speech.
        self.assertEqual(result['miss']['missed_intervals'], 1)
        self.assertEqual(result['miss']['missed_speech_ms'], 600.0)
        self.assertEqual(result['miss']['miss_ratio'], 0.5)
        self.assertEqual(result['coverage']['matched_interval_ratio'], 0.5)
        self.assertEqual(result['coverage']['matched_speech_ratio'], round(500 / 1100, 6))

    def test_false_alarm_is_counted_and_measured(self):
        result = evaluate(self.annotation, [
            acoustic_document([(500.0, 1000.0), (2000.0, 2600.0), (3200.0, 3500.0)])])
        self.assertEqual(result['false_alarm']['false_alarm_segments'], 1)
        self.assertEqual(result['false_alarm']['false_alarm_ms'], 300.0)
        self.assertEqual(result['miss']['missed_intervals'], 0)

    def test_segments_outside_declared_regions_are_unmeasurable_not_false_alarms(self):
        """A segment outside what a human annotated is unknown, not an error."""
        document = annotation_document()
        document['annotated_regions'] = [{'start_ms': 0.0, 'end_ms': 2800.0}]
        annotation = load_annotation(document)
        result = evaluate(annotation, [
            acoustic_document([(500.0, 1000.0), (2000.0, 2600.0), (3400.0, 3800.0)])])
        self.assertEqual(result['false_alarm']['false_alarm_segments'], 0)
        self.assertEqual(result['coverage']['segments_outside_annotated_regions'], 1)
        self.assertEqual(result['coverage']['annotated_region_ratio'], round(2800 / 4000, 6))

    def test_matching_is_one_to_one(self):
        """One wide prediction may not satisfy two annotated intervals."""
        document = annotation_document(intervals=((500.0, 800.0), (900.0, 1200.0)))
        annotation = load_annotation(document)
        result = evaluate(annotation, [acoustic_document([(400.0, 1300.0)])])
        self.assertEqual(result['denominator']['intervals_with_a_match'], 1)
        self.assertEqual(result['miss']['missed_intervals'], 1)

    def test_matching_prefers_the_largest_overlap(self):
        document = annotation_document(intervals=((1000.0, 2000.0),))
        annotation = load_annotation(document)
        result = evaluate(annotation, [acoustic_document([(1900.0, 2100.0), (1050.0, 1950.0)])])
        match = result['per_document'][0]['matches'][0]
        self.assertEqual(match['segment_id'], 'SEG-0001')

    def test_multi_document_denominator_stays_explicit(self):
        documents = [
            acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)]),
            acoustic_document([(500.0, 1000.0)]),
        ]
        result = evaluate(self.annotation, documents)
        self.assertEqual(result['denominator']['documents_evaluated'], 2)
        self.assertEqual(result['denominator']['expected_matches'], 4)
        self.assertEqual(result['denominator']['intervals_with_a_match'], 3)
        self.assertEqual(result['miss']['missed_intervals'], 1)
        self.assertEqual(result['coverage']['matched_interval_ratio'], 0.75)
        self.assertEqual(len(result['per_document']), 2)

    def test_per_document_records_the_method_that_produced_it(self):
        result = evaluate(self.annotation, [
            acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)], method='energy_vad')])
        self.assertEqual(result['per_document'][0]['method'], 'energy_vad')

    def test_evaluation_repeats_identically(self):
        documents = [acoustic_document([(520.0, 980.0), (2010.0, 2580.0)])]
        first = evaluate(self.annotation, documents)
        second = evaluate(self.annotation, documents)
        self.assertEqual(first, second)

    def test_single_interval_statistics_are_defined(self):
        document = annotation_document(intervals=((500.0, 1000.0),))
        annotation = load_annotation(document)
        result = evaluate(annotation, [acoustic_document([(500.0, 1000.0)])])
        self.assertEqual(result['speech_start_absolute_error_ms']['count'], 1)
        self.assertEqual(result['speech_start_absolute_error_ms']['p95_ms'], 0.0)
        self.assertEqual(result['speech_start_error_ms']['mean_signed_ms'], 0.0)

    def test_result_carries_its_policy_and_no_accuracy_claim(self):
        result = evaluate(self.annotation, [
            acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)])])
        self.assertEqual(result['evaluation_policy_version'], EVALUATION_POLICY_VERSION)
        self.assertIn('never be reported as', result['note'])
        self.assertEqual(result['errors'], [])

    def test_evaluate_files_round_trips_through_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            annotation_path = Path(tmp) / 'annotation.json'
            document_path = Path(tmp) / 'acoustic.json'
            annotation_path.write_text(json.dumps(annotation_document()), encoding='utf-8')
            document_path.write_text(json.dumps(
                acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)])), encoding='utf-8')
            result = evaluate_files(annotation_path, [document_path])
        self.assertEqual(result['status'], 'evaluated')
        self.assertEqual(result['coverage']['matched_interval_ratio'], 1.0)

    def test_evaluate_files_rejects_a_foreign_contract_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            annotation_path = Path(tmp) / 'annotation.json'
            document_path = Path(tmp) / 'acoustic.json'
            annotation_path.write_text(json.dumps(annotation_document()), encoding='utf-8')
            document = acoustic_document([(500.0, 1000.0)])
            document['schema_version'] = '2.0.0'
            document_path.write_text(json.dumps(document), encoding='utf-8')
            with self.assertRaises(AnnotationError):
                evaluate_files(annotation_path, [document_path])

    def test_cli_scores_a_supplied_annotation(self):
        import contextlib
        import io
        from aivoicebench.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            annotation_path = Path(tmp) / 'annotation.json'
            document_path = Path(tmp) / 'acoustic.json'
            out_path = Path(tmp) / 'evaluation.json'
            annotation_path.write_text(json.dumps(annotation_document()), encoding='utf-8')
            document_path.write_text(json.dumps(
                acoustic_document([(500.0, 1000.0), (2000.0, 2600.0)])), encoding='utf-8')
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = main(['vad-eval', '--annotation', str(annotation_path),
                                  '--acoustic', str(document_path),
                                  '--output', str(out_path)])
            written = json.loads(out_path.read_text(encoding='utf-8'))
        self.assertEqual(exit_code, 0)
        self.assertEqual(written['status'], 'evaluated')
        self.assertIn('EVALUATED', buffer.getvalue())


if __name__ == '__main__':
    unittest.main()
