"""Silero VAD runtime probes (Issue #23).

These probes answer "is the model actually running and deciding from the signal",
separate from the adapter's contract tests in ``test_silero_vad.py``. They are
deliberately isolated because they need the optional VAD runtime, while the
contract/evaluation tests do not.

The gate mirrors the repository's other optional-dependency gates: an absent
runtime is a skip by default, and a hard failure when
``AIVOICEBENCH_REQUIRE_SILERO_TESTS=1``, which CI sets so "the model gate was
skipped" can never be reported as a pass.
"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fixtures import synthetic_audio as sa
from tests.silero_gate import require_available

from aivoicebench.silero_vad import (
    SILERO_VAD_MODEL_SHA256_6_2_2, SileroUnavailableError, SileroVadSegmenter,
    inspect_model,
)


class SileroRuntimeProbeTests(unittest.TestCase):
    """The model must be running and must react to the signal, not a constant."""

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

    def test_weight_file_matches_the_audited_wheel(self):
        model = inspect_model()
        self.assertEqual(model['sha256'], SILERO_VAD_MODEL_SHA256_6_2_2)
        self.assertEqual(model['name'], 'silero_vad')
        self.assertEqual(model['window_samples'], 512)
        self.assertEqual(model['sample_rate_hz'], 16000)
        self.assertEqual(model['context_samples'], 64)

    def test_model_outputs_vary_with_the_signal(self):
        """A constant output would make every boundary meaningless."""
        speech = self.segmenter.segment(
            self._wav('speech.wav', sa.concatenate(sa.silence(200), sa.speech_like(900),
                                                   sa.silence(400))))
        quiet = self.segmenter.segment(self._wav('silence.wav', sa.silence(2000)))
        speech_peak = speech.document_processor()['model']['speech_probability_max']
        quiet_peak = quiet.document_processor()['model']['speech_probability_max']
        self.assertGreater(speech_peak, 0.9)
        self.assertLess(quiet_peak, 0.1)
        self.assertGreater(speech_peak - quiet_peak, 0.5)

    def test_speech_like_fixture_yields_a_boundary_and_non_speech_does_not(self):
        speech = self.segmenter.segment(
            self._wav('speech2.wav', sa.concatenate(sa.silence(300), sa.speech_like(800),
                                                    sa.silence(400))))
        noise = self.segmenter.segment(self._wav('noise.wav', sa.white_noise(2000)))
        loud_tone = self.segmenter.segment(self._wav('tone.wav', sa.tone(2000)))
        self.assertEqual(speech.status, 'complete')
        self.assertEqual(len(speech.segments), 1)
        # Energy is not speech: a loud non-speech signal must not be claimed as a
        # speech boundary by a model provider.
        self.assertEqual(noise.status, 'insufficient_evidence')
        self.assertEqual(loud_tone.status, 'insufficient_evidence')
        self.assertLess(noise.document_processor()['model']['speech_probability_max'],
                        self.segmenter.policy.threshold)
        self.assertLess(loud_tone.document_processor()['model']['speech_probability_max'],
                        self.segmenter.policy.threshold)

    def test_probabilities_repeat_exactly_with_a_fresh_session(self):
        """The runtime is single-threaded and CPU-only, so replay is bit-exact."""
        path = self._wav('replay.wav', sa.concatenate(sa.silence(200), sa.speech_like(600),
                                                      sa.silence(300)))
        first = self.segmenter.segment(path)
        second = SileroVadSegmenter().segment(path)
        self.assertEqual(first.document_processor()['model']['speech_probability_mean'],
                         second.document_processor()['model']['speech_probability_mean'])
        self.assertEqual(first.document_processor()['model']['speech_probability_max'],
                         second.document_processor()['model']['speech_probability_max'])
        self.assertEqual(first.document_processor()['model']['speech_probability_min'],
                         second.document_processor()['model']['speech_probability_min'])
        self.assertEqual([segment.to_dict('S') for segment in first.segments],
                         [segment.to_dict('S') for segment in second.segments])

    def test_probability_histogram_counts_every_window(self):
        path = self._wav('hist.wav', sa.concatenate(sa.silence(200), sa.speech_like(700),
                                                    sa.silence(300)))
        result = self.segmenter.segment(path)
        model = result.document_processor()['model']
        histogram = result.processor['parameters']['speech_probability_histogram']
        self.assertEqual(len(histogram), 10)
        self.assertEqual(sum(histogram), model['frames'])

    def test_corrupt_weight_file_fails_instead_of_falling_back(self):
        """A wrong or truncated weight file must raise, never silently degrade."""
        broken = Path(self.tmp.name) / 'silero_vad.onnx'
        broken.write_bytes(b'not the audited weights')
        with self.assertRaises(SileroUnavailableError):
            SileroVadSegmenter(model=broken)

    def test_missing_model_override_fails_instead_of_falling_back(self):
        import os
        with patch.dict(os.environ, {'AIVOICEBENCH_SILERO_MODEL': str(
                Path(self.tmp.name) / 'absent.onnx')}):
            with self.assertRaises(SileroUnavailableError):
                SileroVadSegmenter()

    def test_missing_onnxruntime_fails_instead_of_falling_back(self):
        """The runtime is a hard requirement, not something to route around."""
        segmenter = SileroVadSegmenter()
        with patch.dict(sys.modules, {'onnxruntime': None}):
            with self.assertRaises(SileroUnavailableError) as caught:
                segmenter.runtime()
        self.assertIn('onnxruntime', str(caught.exception))

    def test_missing_onnxruntime_also_fails_through_segment(self):
        subject = Path(self.tmp.name) / 'runtime.wav'
        sa.write_wav(subject, sa.concatenate(sa.silence(300), sa.speech_like(600),
                                             sa.silence(300)))
        segmenter = SileroVadSegmenter()
        with patch.dict(sys.modules, {'onnxruntime': None}):
            with self.assertRaises(SileroUnavailableError):
                segmenter.segment(subject)


if __name__ == '__main__':
    unittest.main()
