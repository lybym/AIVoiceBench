"""Acoustic-boundary contract and provider-selection tests (Issue #23).

Scope split:

* :class:`ProviderSelectionTests`, :class:`PolicyResolutionTests`,
  :class:`ModelDocumentShapeTests`, :class:`AsrBoundaryIsolationTests` and
  :class:`AcousticContractRegressionTests` need no optional runtime. They cover the
  issue's software-verifiable acceptance clauses about provider selection,
  provenance, the ASR-boundary boundary and the ``AcousticSegments 1.0.0`` contract.
* :class:`SileroDetectionTests`, :class:`SileroPolicyTests`,
  :class:`SileroReplayTests` and :class:`SileroCliTests` exercise the real model and
  are gated by ``tests/silero_gate.py`` (skip by default, hard failure under
  ``AIVOICEBENCH_REQUIRE_SILERO_TESTS=1``).

All audio here is deterministically generated (``tests/fixtures/synthetic_audio.py``).
It is never reported as real-recording speech evidence.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fixtures import synthetic_audio as sa
from tests.silero_gate import require_available

from aivoicebench.acoustic import (
    AcousticError, EnergyVadSegmenter, resolve_segmenter, segment_audio,
)
from aivoicebench.runner import write_json
from aivoicebench.silero_vad import (
    NON_MEASUREMENT_FIELDS, SILERO_METHOD, SILERO_POLICY_NOTES,
    SILERO_PROCESSOR_VERSION, SILERO_VAD_MODEL_SHA256_6_2_2, SILERO_WINDOW_SAMPLES,
    SileroPolicyError, SileroUnavailableError, SileroVadPolicy, SileroVadSegmenter,
    normalized_evidence, resolve_policy,
)
from aivoicebench.validation import acoustic_errors


def _model_block(result):
    """The model/runtime provenance block, as the emitted document carries it."""
    return result.document_processor()['model']


def _base_model_document():
    """A hand-written model-VAD document for shape tests (no runtime needed)."""
    return {
        'schema_version': '1.0.0',
        'document_id': 'ACOUSTIC-shape-test',
        'source': {
            'path': 'normalized.wav', 'sha256': 'a' * 64, 'duration_ms': 3000,
            'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE',
        },
        'processor': {
            'method': SILERO_METHOD,
            'processor_version': SILERO_PROCESSOR_VERSION,
            'parameters': {
                'frame_samples': 512, 'hop_samples': 512, 'threshold': 0.5,
                'negative_threshold': 0.35, 'min_speech_ms': 250,
                'min_silence_ms': 100, 'merge_gap_ms': 0, 'pre_roll_ms': 30,
                'post_roll_ms': 30,
            },
            'model': {
                'name': 'silero_vad', 'version': '6.2.0',
                'sha256': SILERO_VAD_MODEL_SHA256_6_2_2,
                'source': 'silero-vad PyPI wheel 6.2.2', 'runtime': 'onnxruntime',
                'runtime_version': '1.30.0',
                'execution_provider': 'CPUExecutionProvider',
                'sample_rate_hz': 16000, 'window_samples': 512, 'context_samples': 64,
            },
        },
        'status': 'complete',
        'reason': None,
        'segments': [{
            'segment_id': 'SEG-0000', 'start_ms': 500.0, 'end_ms': 1000.0,
            'confidence': 0.8, 'source': 'acoustic', 'method': SILERO_METHOD,
            'uncertainty_ms': 32.0,
            'frame_stats': {
                'peak_speech_probability': 0.95, 'mean_speech_probability': 0.8,
                'min_speech_probability': 0.55, 'threshold': 0.5,
                'frames_above_threshold': 12, 'frame_count': 16,
            },
        }],
    }


class ProviderSelectionTests(unittest.TestCase):
    """AC: provider selection is explicit; a model provider never degrades silently."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'clip.wav'
        # Distinct tone bursts with real silence: the legacy energy VAD must keep
        # finding segments exactly as before this change.
        sa.write_wav(self.source, sa.concatenate(
            sa.silence(400), sa.tone(600), sa.silence(400), sa.tone(500), sa.silence(300)))

    def test_energy_is_the_default_and_unchanged(self):
        document, out_path = segment_audio(self.source, Path(self.tmp.name) / 'e.json')
        self.assertEqual(document['processor']['method'], 'energy_vad')
        self.assertEqual(document['processor']['processor_version'], '1.0.0')
        self.assertEqual(document['schema_version'], '1.0.0')
        self.assertEqual(acoustic_errors(document), [])
        self.assertIsNotNone(out_path)

    def test_explicit_energy_name_selects_the_legacy_implementation(self):
        document, _ = segment_audio(self.source, vad='energy')
        self.assertEqual(document['processor']['method'], 'energy_vad')

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(AcousticError):
            segment_audio(self.source, vad='webrtc')

    def test_requesting_silero_without_the_runtime_raises_instead_of_falling_back(self):
        """No silent fallback: an unavailable model provider must fail loudly."""
        with patch.dict(os.environ, {'AIVOICEBENCH_SILERO_MODEL': str(
                Path(self.tmp.name) / 'missing.onnx')}):
            with self.assertRaises(SileroUnavailableError):
                segment_audio(self.source, vad='silero')
        # The energy path is untouched by the failed model request.
        document, _ = segment_audio(self.source)
        self.assertEqual(document['processor']['method'], 'energy_vad')

    def test_policy_argument_only_applies_to_the_model_provider(self):
        with self.assertRaises(AcousticError):
            segment_audio(self.source, vad='energy', policy='1.0.0')
        with self.assertRaises(AcousticError):
            segment_audio(self.source, policy='1.0.0')
        with self.assertRaises(AcousticError):
            segment_audio(self.source, segmenter=EnergyVadSegmenter(), policy='1.0.0')

    def test_segmenter_and_vad_are_mutually_exclusive(self):
        with self.assertRaises(AcousticError):
            segment_audio(self.source, segmenter=EnergyVadSegmenter(), vad='energy')

    def test_energy_vad_document_has_no_model_block(self):
        document, _ = segment_audio(self.source)
        self.assertNotIn('model', document['processor'])
        self.assertIn('frame_stats', document['segments'][0])
        self.assertIn('peak_rms', document['segments'][0]['frame_stats'])

    def test_resolve_segmenter_defaults_to_energy(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AIVOICEBENCH_ACOUSTIC_PROVIDER', None)
            self.assertIsInstance(resolve_segmenter(), EnergyVadSegmenter)
            self.assertIsInstance(resolve_segmenter('energy'), EnergyVadSegmenter)

    def test_resolve_segmenter_accepts_the_environment_default(self):
        with patch.dict(os.environ, {'AIVOICEBENCH_ACOUSTIC_PROVIDER': 'silero',
                                     'AIVOICEBENCH_SILERO_MODEL': str(
                                         Path(self.tmp.name) / 'absent.onnx')}):
            with self.assertRaises(SileroUnavailableError):
                resolve_segmenter()

    def test_resolve_segmenter_rejects_an_unknown_provider(self):
        with self.assertRaises(AcousticError):
            resolve_segmenter('webrtc')

    def test_resolve_segmenter_rejects_a_policy_for_the_energy_provider(self):
        with self.assertRaises(AcousticError):
            resolve_segmenter('energy', '1.0.0')

    def test_missing_onnxruntime_fails_instead_of_falling_back(self):
        """A missing runtime is an explicit failure at every entry point."""
        with patch.dict(sys.modules, {'onnxruntime': None}):
            with self.assertRaises(SileroUnavailableError):
                segment_audio(self.source, vad='silero')


class PolicyResolutionTests(unittest.TestCase):
    """The boundary policy is versioned, validated and never an upstream default."""

    def test_default_policy_is_versioned_and_recorded_as_uncalibrated(self):
        policy, provenance = resolve_policy()
        self.assertEqual(policy.version, '1.0.0')
        self.assertEqual(policy.name, 'silero_boundary_policy/1.0.0')
        self.assertFalse(provenance['is_canonical_measurement_policy'])
        self.assertIn('NOT calibrated', provenance['note'])
        self.assertEqual(provenance['note'], SILERO_POLICY_NOTES)

    def test_named_policy_version_resolves(self):
        policy, provenance = resolve_policy('1.0.0')
        self.assertEqual(policy.version, '1.0.0')
        self.assertEqual(provenance['selected_by'], 'named_version')

    def test_environment_selects_the_policy_and_unknown_versions_are_rejected(self):
        with patch.dict(os.environ, {'AIVOICEBENCH_SILERO_POLICY': '9.9.9'}):
            with self.assertRaises(SileroPolicyError):
                resolve_policy()
        with patch.dict(os.environ, {'AIVOICEBENCH_SILERO_POLICY': '1.0.0'}):
            policy, provenance = resolve_policy()
            self.assertEqual(policy.version, '1.0.0')

    def test_invalid_policy_parameters_are_rejected(self):
        for kwargs in (
            {'threshold': 0.0},
            {'threshold': 1.0},
            {'threshold': float('nan')},
            {'negative_threshold': float('inf')},
            {'min_speech_ms': 0},
            {'min_silence_ms': -1},
            {'merge_gap_ms': -0.5},
            {'pre_roll_ms': float('nan')},
            {'post_roll_ms': -1},
            {'version': ''},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SileroPolicyError):
                    SileroVadPolicy(**kwargs)

    def test_hysteresis_thresholds_must_be_ordered(self):
        with self.assertRaises(SileroPolicyError):
            SileroVadPolicy(threshold=0.4, negative_threshold=0.6)

    def test_policy_rejects_non_policy_objects(self):
        with self.assertRaises(SileroPolicyError):
            resolve_policy(42)


class ModelDocumentShapeTests(unittest.TestCase):
    """AC4/AC6: provenance, uncertainty and the shared contract shape."""

    def test_model_document_validates_against_the_shared_contract(self):
        self.assertEqual(acoustic_errors(_base_model_document()), [])

    def test_model_document_without_threshold_is_rejected(self):
        document = _base_model_document()
        del document['processor']['parameters']['threshold']
        errors = acoustic_errors(document)
        self.assertTrue(any('threshold' in error for error in errors), errors)

    def test_energy_method_may_not_claim_model_provenance(self):
        document = _base_model_document()
        document['processor']['method'] = 'energy_vad'
        for segment in document['segments']:
            segment['method'] = 'energy_vad'
        errors = acoustic_errors(document)
        self.assertTrue(any('stdlib signal method' in error for error in errors), errors)

    def test_documented_contract_version_is_unchanged(self):
        self.assertEqual(_base_model_document()['schema_version'], '1.0.0')

    def test_identity_fields_are_explicitly_not_measurement(self):
        """Replay compares measurement fields; identity/location are excluded."""
        self.assertEqual(NON_MEASUREMENT_FIELDS, ('document_id', 'source.path'))
        document = _base_model_document()
        first = normalized_evidence(document)
        moved = _base_model_document()
        moved['document_id'] = 'ACOUSTIC-other'
        moved['source']['path'] = 'somewhere/else.wav'
        self.assertEqual(first, normalized_evidence(moved))
        # Every measurement field still participates in the comparison.
        changed = _base_model_document()
        changed['segments'][0]['end_ms'] = 1200.0
        self.assertNotEqual(first, normalized_evidence(changed))
        # A different recording identity is a measurement difference.
        other_audio = _base_model_document()
        other_audio['source']['sha256'] = 'b' * 64
        self.assertNotEqual(first, normalized_evidence(other_audio))

    def test_policy_and_provenance_fields_participate_in_replay_comparison(self):
        """A replay comparison must notice a changed processor or policy."""
        base = normalized_evidence(_base_model_document())
        for mutate in (
            lambda doc: doc['processor']['parameters'].__setitem__('threshold', 0.6),
            lambda doc: doc['processor']['parameters'].__setitem__('min_speech_ms', 400),
            lambda doc: doc['processor'].__setitem__('processor_version', '1.0.1'),
            lambda doc: doc['processor']['model'].__setitem__('sha256', 'e' * 64),
            lambda doc: doc['processor']['model'].__setitem__('runtime_version', '9.9.9'),
            lambda doc: doc['segments'][0]['frame_stats'].__setitem__(
                'peak_speech_probability', 0.6),
        ):
            mutated = _base_model_document()
            mutate(mutated)
            with self.subTest(mutate=mutate):
                self.assertNotEqual(base, normalized_evidence(mutated))


class AsrBoundaryIsolationTests(unittest.TestCase):
    """AC5: ASR/provider timestamps are never silently reused as acoustic boundaries."""

    def test_contract_has_no_asr_or_role_fields(self):
        document = _base_model_document()
        serialized = json.dumps(document)
        for forbidden in ('speaker_role', 'speaker_id', 'transcript', 'words',
                          'provider_timestamp', 'asr_'):
            self.assertNotIn(forbidden, serialized)

    def test_asr_method_names_are_rejected_as_acoustic_boundaries(self):
        for method in ('asr_final', 'provider_timestamp', 'diarization_span',
                       'llm_semantic_boundary', 'speaker_span'):
            with self.subTest(method=method):
                document = _base_model_document()
                document['processor']['method'] = method
                for segment in document['segments']:
                    segment['method'] = method
                del document['processor']['model']
                del document['processor']['parameters']['threshold']
                document['processor']['parameters'] = {
                    'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                    'threshold_factor': 0.15, 'threshold_mode': 'asr',
                    'min_speech_ms': 100, 'min_silence_ms': 200,
                }
                errors = acoustic_errors(document)
                self.assertTrue(any('not a signal-based acoustic' in error for error in errors),
                                errors)

    def test_segmenter_only_reads_audio_so_asr_output_cannot_enter(self):
        """The provider's input surface is a path; there is no ASR argument."""
        import inspect
        signature = inspect.signature(SileroVadSegmenter.segment)
        self.assertEqual(list(signature.parameters), ['self', 'path'])


class SileroDetectionTests(unittest.TestCase):
    """AC2: synthetic speech/silence/noise, ordering, short segments, invalid audio."""

    @classmethod
    def setUpClass(cls):
        require_available()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.segmenter = SileroVadSegmenter()

    def _wav(self, name, samples):
        path = Path(self.tmp.name) / name
        sa.write_wav(path, samples)
        return path

    def _two_segment_wav(self):
        return self._wav('two.wav', sa.concatenate(
            sa.silence(300), sa.speech_like(700), sa.silence(500),
            sa.speech_like(600), sa.silence(400)))

    def test_two_speech_regions_are_detected_in_order(self):
        result = self.segmenter.segment(self._two_segment_wav())
        self.assertEqual(result.status, 'complete')
        self.assertEqual(len(result.segments), 2)
        first, second = result.segments
        self.assertGreater(first.end_ms, first.start_ms)
        self.assertGreater(second.start_ms, first.end_ms)
        self.assertEqual(first.method, SILERO_METHOD)
        self.assertEqual(result.processor['method'], SILERO_METHOD)

    def test_boundaries_track_the_synthetic_speech_regions(self):
        """Coordinates are audio-relative ms and land near the real onsets."""
        # Sections: silence 300 | speech 700 | silence 500 | speech 600 | silence 400
        result = self.segmenter.segment(self._two_segment_wav())
        first, second = result.segments
        self.assertAlmostEqual(first.start_ms, 300.0, delta=60.0)
        self.assertAlmostEqual(first.end_ms, 1000.0, delta=80.0)
        self.assertAlmostEqual(second.start_ms, 1500.0, delta=80.0)
        self.assertAlmostEqual(second.end_ms, 2100.0, delta=90.0)

    def test_pure_silence_is_insufficient_evidence(self):
        result = self.segmenter.segment(self._wav('silence.wav', sa.silence(2000)))
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])
        self.assertTrue(result.reason)

    def test_explicit_noise_is_not_claimed_as_speech(self):
        """Noise has energy but no speech structure; a loudness VAD would fire."""
        result = self.segmenter.segment(self._wav('noise.wav', sa.white_noise(2000)))
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])
        self.assertLess(_model_block(result)['speech_probability_max'],
                        self.segmenter.policy.threshold)

    def test_loud_tone_is_not_claimed_as_speech(self):
        result = self.segmenter.segment(self._wav('tone.wav', sa.tone(2000, amplitude=0.9)))
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])

    def test_noise_surrounding_speech_does_not_extend_the_boundary(self):
        path = self._wav('noisy.wav', sa.concatenate(
            sa.white_noise(400), sa.speech_like(700), sa.white_noise(400)))
        result = self.segmenter.segment(path)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(len(result.segments), 1)
        segment = result.segments[0]
        self.assertGreater(segment.start_ms, 200.0)
        self.assertLess(segment.end_ms, 1400.0)

    def test_short_speech_below_the_policy_minimum_stays_unclaimed(self):
        path = self._wav('short.wav', sa.concatenate(
            sa.silence(400), sa.speech_like(150), sa.silence(400)))
        result = self.segmenter.segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])
        self.assertIn('minimum speech duration', result.reason)

    def test_speech_touching_the_artifact_start_is_clamped(self):
        path = self._wav('edge.wav', sa.concatenate(sa.speech_like(500), sa.silence(700)))
        result = self.segmenter.segment(path)
        self.assertEqual(result.status, 'complete')
        self.assertGreaterEqual(result.segments[0].start_ms, 0.0)
        for segment in result.segments:
            self.assertLessEqual(segment.end_ms, result.source['duration_ms'] + 0.001)

    def test_empty_audio_is_insufficient_evidence(self):
        path = Path(self.tmp.name) / 'empty.wav'
        import wave
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'')
        result = self.segmenter.segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])

    def test_audio_shorter_than_one_window_is_insufficient_evidence(self):
        path = self._wav('tiny.wav', sa.silence(10))
        result = self.segmenter.segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertIn('analysis window', result.reason)

    def test_non_canonical_audio_is_rejected(self):
        path = Path(self.tmp.name) / 'stereo.wav'
        import wave
        from array import array
        data = array('h', [0] * 100)
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(data.tobytes())
        with self.assertRaises(AcousticError):
            self.segmenter.segment(path)

    def test_non_16khz_audio_is_rejected(self):
        path = Path(self.tmp.name) / '8k.wav'
        sa.write_wav(path, sa.silence(1000), rate=8000)
        with self.assertRaises(AcousticError):
            self.segmenter.segment(path)

    def test_tail_padding_is_reported(self):
        """A partial final window is zero-padded, and the document says so."""
        # 300 + 700 + 333 ms = 1333 ms = 21328 samples, which is not a whole
        # number of 512-sample windows (21328 = 41 * 512 + 336).
        path = self._wav('tail.wav', sa.concatenate(sa.silence(300), sa.speech_like(700),
                                                    sa.silence(333)))
        result = self.segmenter.segment(path)
        self.assertEqual(result.source['duration_ms'], 1333.0)
        self.assertEqual(_model_block(result)['frames'], -(-21328 // SILERO_WINDOW_SAMPLES))
        self.assertTrue(_model_block(result)['tail_padded'])

    def test_whole_window_audio_is_not_reported_as_padded(self):
        """Exact multiples of the window must never claim padding."""
        for frames in (1, 6, 47):
            with self.subTest(frames=frames):
                path = self._wav(f'whole{frames}.wav',
                                 sa.silence(frames * SILERO_WINDOW_SAMPLES / 16000 * 1000))
                result = self.segmenter.segment(path)
                self.assertEqual(_model_block(result)['frames'], frames)
                self.assertFalse(_model_block(result)['tail_padded'])

    def test_one_sample_past_a_window_boundary_reports_padding(self):
        samples = sa.silence(6 * SILERO_WINDOW_SAMPLES / 16000 * 1000)
        path = self._wav('past.wav', sa.concatenate(samples, sa.silence(0.0625)))
        result = self.segmenter.segment(path)
        self.assertEqual(_model_block(result)['frames'], 7)
        self.assertTrue(_model_block(result)['tail_padded'])

    def test_no_window_can_be_evaluated_is_reported_as_such(self):
        path = self._wav('subwindow.wav', sa.silence(20))
        result = self.segmenter.segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])
        self.assertEqual(_model_block(result)['frames'], 0)
        self.assertFalse(_model_block(result)['tail_padded'])

    def test_every_segment_carries_provenance_and_uncertainty(self):
        result = self.segmenter.segment(self._two_segment_wav())
        parameters = result.processor['parameters']
        model = _model_block(result)
        self.assertEqual(model['sha256'], SILERO_VAD_MODEL_SHA256_6_2_2)
        self.assertEqual(model['runtime'], 'onnxruntime')
        self.assertTrue(model['runtime_version'])
        self.assertEqual(result.processor['sensitivity']['profile'],
                         'silero_boundary_policy/1.0.0')
        self.assertFalse(result.processor['sensitivity']['is_canonical_measurement_policy'])
        self.assertIn('threshold', parameters)
        for index, segment in enumerate(result.segments):
            self.assertEqual(segment.method, SILERO_METHOD)
            self.assertGreater(segment.uncertainty_ms, 0)
            stats = segment.to_dict(f'SEG-{index:04d}')['frame_stats']
            self.assertIn('peak_speech_probability', stats)
            self.assertIn('threshold', stats)
            self.assertGreaterEqual(segment.confidence, 0.0)
            self.assertLessEqual(segment.confidence, 1.0)

    def test_probability_summary_covers_every_frame(self):
        result = self.segmenter.segment(self._two_segment_wav())
        frames = _model_block(result)['frames']
        histogram = result.processor['parameters']['speech_probability_histogram']
        self.assertEqual(sum(histogram), frames)
        self.assertEqual(result.processor['parameters']['frames_above_threshold']
                         + result.processor['parameters']['frames_below_negative_threshold'],
                         frames)

    def test_interval_statistics_are_consistent(self):
        """Per-segment model statistics must be internally consistent."""
        result = self.segmenter.segment(self._two_segment_wav())
        self.assertTrue(result.segments)
        total = 0
        for segment in result.segments:
            stats = segment.frame_stats
            self.assertGreaterEqual(stats['frame_count'], 1)
            self.assertGreaterEqual(stats['peak_speech_probability'],
                                    stats['mean_speech_probability'])
            self.assertLessEqual(stats['min_speech_probability'],
                                 stats['mean_speech_probability'])
            self.assertLessEqual(stats['frames_above_threshold'], stats['frame_count'])
            self.assertEqual(stats['threshold'], self.segmenter.policy.threshold)
            total += stats['frame_count']
        self.assertGreater(total, 0)

    def test_document_merges_the_model_block_into_processor(self):
        document = self.segmenter.segment(self._two_segment_wav()).to_dict()
        self.assertEqual(document['processor']['model']['runtime'], 'onnxruntime')
        self.assertEqual(acoustic_errors(document), [])


class SileroPolicyTests(unittest.TestCase):
    """The decision is driven by the versioned policy, not by upstream defaults."""

    @classmethod
    def setUpClass(cls):
        require_available()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gap_path = Path(self.tmp.name) / 'gap.wav'
        sa.write_wav(self.gap_path, sa.concatenate(
            sa.silence(300), sa.speech_like(400), sa.silence(700),
            sa.speech_like(400), sa.silence(400)))
        self.short_path = Path(self.tmp.name) / 'short.wav'
        sa.write_wav(self.short_path, sa.concatenate(
            sa.silence(400), sa.speech_like(150), sa.silence(400)))

    def test_merge_gap_policy_changes_the_segment_count(self):
        split = SileroVadSegmenter(policy=SileroVadPolicy(merge_gap_ms=0.0)).segment(self.gap_path)
        merged = SileroVadSegmenter(policy=SileroVadPolicy(merge_gap_ms=800.0)).segment(self.gap_path)
        self.assertEqual(len(split.segments), 2)
        self.assertEqual(len(merged.segments), 1)

    def test_min_silence_policy_changes_the_boundary(self):
        short = SileroVadSegmenter(policy=SileroVadPolicy(min_silence_ms=100.0)).segment(self.gap_path)
        long = SileroVadSegmenter(policy=SileroVadPolicy(min_silence_ms=1500.0)).segment(self.gap_path)
        self.assertEqual(len(short.segments), 2)
        self.assertEqual(len(long.segments), 1)

    def test_min_speech_policy_changes_acceptance(self):
        strict = SileroVadSegmenter(policy=SileroVadPolicy(min_speech_ms=250.0)).segment(self.short_path)
        permissive = SileroVadSegmenter(policy=SileroVadPolicy(min_speech_ms=120.0)).segment(self.short_path)
        self.assertEqual(strict.status, 'insufficient_evidence')
        self.assertEqual(permissive.status, 'complete')
        self.assertEqual(len(permissive.segments), 1)

    def test_hysteresis_policy_changes_the_release_point(self):
        wide = SileroVadSegmenter(policy=SileroVadPolicy(negative_threshold=0.05)).segment(self.gap_path)
        narrow = SileroVadSegmenter(policy=SileroVadPolicy(negative_threshold=0.49)).segment(self.gap_path)
        self.assertEqual(len(wide.segments), len(narrow.segments))
        self.assertLess(narrow.segments[0].end_ms, wide.segments[0].end_ms)

    def test_roll_policy_changes_the_reported_boundary(self):
        bare = SileroVadSegmenter(policy=SileroVadPolicy(pre_roll_ms=0.0, post_roll_ms=0.0)).segment(self.gap_path)
        rolled = SileroVadSegmenter(
            policy=SileroVadPolicy(pre_roll_ms=120.0, post_roll_ms=120.0)).segment(self.gap_path)
        self.assertLessEqual(rolled.segments[0].start_ms, bare.segments[0].start_ms)
        self.assertGreaterEqual(rolled.segments[0].end_ms, bare.segments[0].end_ms)

    def test_threshold_change_is_recorded_and_changes_onset(self):
        strict = SileroVadSegmenter(
            policy=SileroVadPolicy(threshold=0.99, negative_threshold=0.9)).segment(self.gap_path)
        self.assertEqual(strict.processor['parameters']['threshold'], 0.99)
        self.assertGreater(strict.segments[0].start_ms, 300.0)

    def test_policy_identity_is_recorded_in_the_document(self):
        result = SileroVadSegmenter(policy='1.0.0').segment(self.gap_path)
        document = result.to_dict()
        self.assertEqual(document['processor']['sensitivity']['profile'],
                         'silero_boundary_policy/1.0.0')
        self.assertEqual(document['processor']['parameters']['min_speech_ms'], 250.0)
        self.assertEqual(acoustic_errors(document), [])


class SileroReplayTests(unittest.TestCase):
    """AC1: same Artifact + same processor/policy replays to the same evidence."""

    @classmethod
    def setUpClass(cls):
        require_available()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'replay.wav'
        sa.write_wav(self.path, sa.concatenate(
            sa.silence(300), sa.speech_like(700), sa.silence(500), sa.speech_like(600),
            sa.silence(400)))

    def test_repeated_segmentation_is_field_identical(self):
        first = normalized_evidence(SileroVadSegmenter().segment(self.path).to_dict())
        second = normalized_evidence(SileroVadSegmenter().segment(self.path).to_dict())
        self.assertEqual(first, second)

    def test_repeated_segmentation_in_one_instance_is_field_identical(self):
        segmenter = SileroVadSegmenter()
        first = normalized_evidence(segmenter.segment(self.path).to_dict())
        second = normalized_evidence(segmenter.segment(self.path).to_dict())
        self.assertEqual(first, second)

    def test_only_identity_fields_differ(self):
        first = SileroVadSegmenter().segment(self.path).to_dict()
        second = SileroVadSegmenter().segment(self.path).to_dict()
        self.assertNotEqual(first['document_id'], second['document_id'])
        differing = [key for key in first if first[key] != second[key]]
        self.assertEqual(differing, ['document_id'])

    def test_different_audio_produces_different_evidence(self):
        other = Path(self.tmp.name) / 'other.wav'
        sa.write_wav(other, sa.concatenate(
            sa.silence(100), sa.speech_like(300), sa.silence(900),
            sa.speech_like(500), sa.silence(200)))
        first = normalized_evidence(SileroVadSegmenter().segment(self.path).to_dict())
        second = normalized_evidence(SileroVadSegmenter().segment(other).to_dict())
        self.assertNotEqual(first, second)

    def test_policy_change_breaks_byte_equality_of_evidence(self):
        default = normalized_evidence(SileroVadSegmenter().segment(self.path).to_dict())
        changed = normalized_evidence(SileroVadSegmenter(
            policy=SileroVadPolicy(pre_roll_ms=200.0)).segment(self.path).to_dict())
        self.assertNotEqual(default, changed)

    def test_replay_document_validates_and_records_the_source_digest(self):
        document = SileroVadSegmenter().segment(self.path).to_dict()
        self.assertEqual(acoustic_errors(document), [])
        self.assertEqual(len(document['source']['sha256']), 64)
        self.assertEqual(document['source']['encoding'], 'PCM_S16LE')


class SileroCliTests(unittest.TestCase):
    """The CLI exposes the provider explicitly and reports it in its output."""

    @classmethod
    def setUpClass(cls):
        require_available()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from aivoicebench.__main__ import main
        self.main = main
        self.path = Path(self.tmp.name) / 'cli.wav'
        sa.write_wav(self.path, sa.concatenate(
            sa.silence(300), sa.speech_like(700), sa.silence(400)))

    def test_cli_silero_writes_a_model_document(self):
        out = Path(self.tmp.name) / 'acoustic.json'
        exit_code = self.main(['acoustic', str(self.path), '--vad', 'silero',
                               '--output', str(out)])
        self.assertEqual(exit_code, 0)
        document = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(document['processor']['method'], SILERO_METHOD)
        self.assertEqual(document['schema_version'], '1.0.0')
        self.assertEqual(acoustic_errors(document), [])

    def test_cli_energy_default_is_unchanged(self):
        out = Path(self.tmp.name) / 'energy.json'
        exit_code = self.main(['acoustic', str(self.path), '--output', str(out),
                               '--min-speech-ms', '100', '--min-silence-ms', '100'])
        self.assertEqual(exit_code, 0)
        document = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(document['processor']['method'], 'energy_vad')

    def test_cli_validate_accepts_the_model_document(self):
        out = Path(self.tmp.name) / 'acoustic.json'
        self.main(['acoustic', str(self.path), '--vad', 'silero', '--output', str(out)])
        self.assertEqual(self.main(['validate', str(out), '--kind', 'acoustic-segments']), 0)

    def test_cli_silero_with_missing_model_exits_1(self):
        out = Path(self.tmp.name) / 'missing.json'
        with patch.dict(os.environ, {'AIVOICEBENCH_SILERO_MODEL':
                                     str(Path(self.tmp.name) / 'absent.onnx')}):
            exit_code = self.main(['acoustic', str(self.path), '--vad', 'silero',
                                   '--output', str(out)])
        self.assertEqual(exit_code, 1)

    def test_cli_rejects_an_unknown_policy_version(self):
        out = Path(self.tmp.name) / 'badpolicy.json'
        exit_code = self.main(['acoustic', str(self.path), '--vad', 'silero',
                               '--policy', '9.9.9', '--output', str(out)])
        self.assertEqual(exit_code, 1)


class AcousticContractRegressionTests(unittest.TestCase):
    """The additive contract change must not alter the legacy energy document."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_energy_document_still_validates(self):
        path = Path(self.tmp.name) / 'silent.wav'
        sa.write_wav(path, sa.silence(2000))
        document = EnergyVadSegmenter().segment(path).to_dict()
        self.assertEqual(acoustic_errors(document), [])

    def test_energy_document_with_sensitivity_still_validates(self):
        path = Path(self.tmp.name) / 'tone.wav'
        sa.write_wav(path, sa.concatenate(sa.silence(400), sa.tone(600), sa.silence(400)))
        document = EnergyVadSegmenter.from_profile('quiet_device').segment(path).to_dict()
        self.assertFalse(document['processor']['sensitivity']['is_canonical_measurement_policy'])
        self.assertEqual(acoustic_errors(document), [])

    def test_energy_frame_stats_shape_unchanged(self):
        path = Path(self.tmp.name) / 'tone2.wav'
        sa.write_wav(path, sa.concatenate(sa.silence(400), sa.tone(600), sa.silence(400)))
        document = EnergyVadSegmenter(min_speech_ms=100, min_silence_ms=100).segment(path).to_dict()
        self.assertEqual(document['status'], 'complete')
        stats = document['segments'][0]['frame_stats']
        self.assertEqual(sorted(stats), ['frame_count', 'mean_rms', 'peak_rms',
                                         'threshold_rms'])

    def test_energy_parameters_variant_still_requires_no_threshold(self):
        from aivoicebench.validation import acoustic_errors as validate
        path = Path(self.tmp.name) / 'tone3.wav'
        sa.write_wav(path, sa.concatenate(sa.silence(400), sa.tone(600), sa.silence(400)))
        document = EnergyVadSegmenter(min_speech_ms=100, min_silence_ms=100).segment(path).to_dict()
        self.assertNotIn('threshold', document['processor']['parameters'])
        self.assertEqual(validate(document), [])


if __name__ == '__main__':
    unittest.main()
