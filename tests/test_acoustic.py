"""Tests for acoustic speech segmentation (energy VAD).

Synthetic PCM16/16kHz/mono WAVs exercise actual signal detection.
These are generated signals, not real tester/device recordings.
"""

from array import array
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

from aivoicebench.acoustic import (
    AcousticError, EnergyVadSegmenter, _frame_energies, _percentile,
    _smooth_mask, segment_audio,
)
from aivoicebench.runner import write_json
from aivoicebench.validation import acoustic_errors, schema_errors


def write_wav(path, samples, rate=16000, channels=1, sample_width=2):
    """Write a PCM16 mono 16kHz WAV from an int16 sample list."""
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(rate)
        data = array('h', samples)
        if sys.byteorder != 'little':
            data.byteswap()
        audio.writeframes(data.tobytes())


def tone_samples(duration_ms, freq=440, amplitude=20000, rate=16000):
    """Generate a sine-tone segment as int16 samples."""
    import math
    n = int(duration_ms / 1000 * rate)
    return [int(amplitude * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def silence_samples(duration_ms, rate=16000):
    return [0] * int(duration_ms / 1000 * rate)


class EnergyVadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _wav(self, name, *sections):
        path = Path(self.tmp.name) / name
        samples = []
        for section in sections:
            samples.extend(section)
        write_wav(path, samples)
        return path

    def test_detects_two_speech_segments_separated_by_silence(self):
        path = self._wav('two.wav',
                         silence_samples(500),
                         tone_samples(500),
                         silence_samples(500),
                         tone_samples(500),
                         silence_samples(500))
        result = EnergyVadSegmenter(min_speech_ms=100, min_silence_ms=100).segment(path)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(len(result.segments), 2)
        seg1, seg2 = result.segments
        self.assertGreater(seg1.end_ms, seg1.start_ms)
        self.assertGreater(seg2.start_ms, seg1.end_ms)
        self.assertGreater(seg2.end_ms, seg2.start_ms)
        self.assertGreater(seg1.confidence, 0)
        self.assertLessEqual(seg1.confidence, 1)
        self.assertEqual(seg1.method, 'energy_vad')
        self.assertEqual(seg1.peak_rms, result.segments[0].peak_rms)
        # Segments should be roughly around the tone positions (500-1000ms and 1500-2000ms)
        self.assertGreater(seg1.start_ms, 300)
        self.assertLess(seg1.start_ms, 700)
        self.assertGreater(seg2.start_ms, 1200)
        self.assertLess(seg2.start_ms, 1700)

    def test_silent_audio_is_insufficient_evidence(self):
        path = self._wav('silent.wav', silence_samples(2000))
        result = EnergyVadSegmenter().segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])
        self.assertIn('silent', result.reason.lower())

    def test_short_burst_below_min_speech_is_filtered(self):
        path = self._wav('short.wav',
                         silence_samples(500),
                         tone_samples(50),  # 50ms tone < 200ms min
                         silence_samples(500))
        result = EnergyVadSegmenter(min_speech_ms=200, min_silence_ms=100).segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.segments, [])

    def test_merge_gap_combines_close_segments(self):
        # Two tones with only 50ms gap — should merge into one segment
        path = self._wav('merge.wav',
                         silence_samples(500),
                         tone_samples(300),
                         silence_samples(50),
                         tone_samples(300),
                         silence_samples(500))
        result = EnergyVadSegmenter(min_speech_ms=100, min_silence_ms=200,
                                    merge_gap_ms=100).segment(path)
        self.assertEqual(result.status, 'complete')
        self.assertEqual(len(result.segments), 1)

    def test_non_canonical_format_raises_error(self):
        path = Path(self.tmp.name) / 'stereo.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            data = array('h', tone_samples(1000) * 2)
            if sys.byteorder != 'little':
                data.byteswap()
            audio.writeframes(data.tobytes())
        with self.assertRaises(AcousticError):
            EnergyVadSegmenter().segment(path)

    def test_empty_wav_is_insufficient(self):
        path = Path(self.tmp.name) / 'empty.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'')
        result = EnergyVadSegmenter().segment(path)
        self.assertEqual(result.status, 'insufficient_evidence')

    def test_invalid_parameters_raise(self):
        with self.assertRaises(AcousticError):
            EnergyVadSegmenter(frame_ms=0)
        with self.assertRaises(AcousticError):
            EnergyVadSegmenter(hop_ms=50, frame_ms=30)  # hop > frame
        with self.assertRaises(AcousticError):
            EnergyVadSegmenter(threshold_factor=0)
        with self.assertRaises(AcousticError):
            EnergyVadSegmenter(threshold_factor=1.0)
        with self.assertRaises(AcousticError):
            EnergyVadSegmenter(min_speech_ms=0)

    def test_output_document_validates(self):
        path = self._wav('doc.wav',
                         silence_samples(500),
                         tone_samples(500),
                         silence_samples(500))
        segmenter = EnergyVadSegmenter(min_speech_ms=100, min_silence_ms=100)
        result = segmenter.segment(path)
        document = result.to_dict()
        self.assertEqual(acoustic_errors(document), [])

    def test_segment_audio_writes_json(self):
        path = self._wav('write.wav',
                         silence_samples(500),
                         tone_samples(500),
                         silence_samples(500))
        out = Path(self.tmp.name) / 'out.json'
        document, out_path = segment_audio(path, out)
        self.assertEqual(out_path, out.resolve())
        data = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], '1.0.0')
        self.assertEqual(data['status'], 'complete')
        self.assertGreater(len(data['segments']), 0)
        self.assertEqual(acoustic_errors(data), [])

    def test_source_metadata_is_recorded(self):
        path = self._wav('meta.wav', tone_samples(1000))
        result = EnergyVadSegmenter().segment(path)
        self.assertEqual(result.source['sample_rate_hz'], 16000)
        self.assertEqual(result.source['channels'], 1)
        self.assertEqual(result.source['encoding'], 'PCM_S16LE')
        self.assertGreater(result.source['duration_ms'], 900)
        self.assertEqual(len(result.source['sha256']), 64)

    def test_processor_parameters_are_recorded(self):
        path = self._wav('params.wav', tone_samples(1000))
        result = EnergyVadSegmenter(frame_ms=20, hop_ms=10, threshold_factor=0.2,
                                    min_speech_ms=150).segment(path)
        params = result.processor['parameters']
        self.assertEqual(params['frame_ms'], 20)
        self.assertEqual(params['hop_ms'], 10)
        self.assertEqual(params['threshold_factor'], 0.2)
        self.assertEqual(params['min_speech_ms'], 150)
        self.assertEqual(result.processor['method'], 'energy_vad')
        self.assertEqual(result.processor['processor_version'], '1.0.0')

    def test_uncertainty_is_hop_resolution(self):
        path = self._wav('unc.wav',
                         silence_samples(500),
                         tone_samples(500),
                         silence_samples(500))
        result = EnergyVadSegmenter(hop_ms=10, min_speech_ms=100, min_silence_ms=100).segment(path)
        self.assertEqual(result.status, 'complete')
        for seg in result.segments:
            self.assertAlmostEqual(seg.uncertainty_ms, 10.0, places=1)


class AcousticHelperTests(unittest.TestCase):
    def test_percentile_single_value(self):
        self.assertAlmostEqual(_percentile([42.0], 50), 42.0)

    def test_percentile_basic(self):
        values = sorted([1.0, 2.0, 3.0, 4.0, 5.0])
        p50 = _percentile(values, 50)
        self.assertAlmostEqual(p50, 3.0)

    def test_smooth_mask_merges_short_gaps(self):
        mask = [True, True, False, False, True, True]
        result = _smooth_mask(mask, merge_gap_frames=3)
        self.assertEqual(result, [True, True, True, True, True, True])

    def test_smooth_mask_preserves_long_gaps(self):
        mask = [True, True, False, False, False, False, False, True]
        result = _smooth_mask(mask, merge_gap_frames=2)
        self.assertEqual(result, [True, True, False, False, False, False, False, True])

    def test_frame_energies_silence_is_near_zero(self):
        samples = silence_samples(100)
        energies, positions = _frame_energies(samples, 160, 160)
        self.assertTrue(len(energies) > 0)
        for e in energies:
            self.assertLess(e, 1e-5)

    def test_frame_energies_tone_is_nonzero(self):
        samples = tone_samples(100)
        energies, positions = _frame_energies(samples, 160, 160)
        self.assertTrue(len(energies) > 0)
        for e in energies:
            self.assertGreater(e, 0.01)


class AcousticValidationTests(unittest.TestCase):
    def test_valid_document_passes(self):
        doc = {
            'schema_version': '1.0.0',
            'document_id': 'ACOUSTIC-test',
            'source': {'path': 'test.wav', 'sha256': 'a' * 64, 'duration_ms': 1000,
                        'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE'},
            'processor': {'method': 'energy_vad', 'processor_version': '1.0.0',
                           'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                                          'threshold_factor': 0.15, 'threshold_mode': 'test',
                                          'min_speech_ms': 100, 'min_silence_ms': 200}},
            'status': 'complete', 'reason': None,
            'segments': [{'segment_id': 'SEG-0000', 'start_ms': 100, 'end_ms': 500,
                           'confidence': 0.8, 'source': 'acoustic', 'method': 'energy_vad',
                           'uncertainty_ms': 10,
                           'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}}]}
        self.assertEqual(acoustic_errors(doc), [])

    def test_overlapping_segments_rejected(self):
        doc = {
            'schema_version': '1.0.0', 'document_id': 't', 'status': 'complete', 'reason': None,
            'source': {'path': 't.wav', 'sha256': 'a' * 64, 'duration_ms': 2000,
                        'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE'},
            'processor': {'method': 'energy_vad', 'processor_version': '1.0.0',
                          'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                                         'threshold_factor': 0.15, 'threshold_mode': 't',
                                         'min_speech_ms': 100, 'min_silence_ms': 200}},
            'segments': [
                {'segment_id': 'SEG-0000', 'start_ms': 100, 'end_ms': 500, 'confidence': 0.8,
                 'source': 'acoustic', 'method': 'energy_vad', 'uncertainty_ms': 10,
                 'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}},
                {'segment_id': 'SEG-0001', 'start_ms': 300, 'end_ms': 600, 'confidence': 0.7,
                 'source': 'acoustic', 'method': 'energy_vad', 'uncertainty_ms': 10,
                 'frame_stats': {'peak_rms': 0.4, 'mean_rms': 0.2, 'threshold_rms': 0.1, 'frame_count': 30}}]}
        errors = acoustic_errors(doc)
        self.assertTrue(any('overlap' in e for e in errors))

    def test_insufficient_with_segments_rejected(self):
        doc = {
            'schema_version': '1.0.0', 'document_id': 't', 'status': 'insufficient_evidence',
            'reason': 'silent', 'source': {'path': 't.wav', 'sha256': 'a' * 64, 'duration_ms': 2000,
                        'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE'},
            'processor': {'method': 'energy_vad', 'processor_version': '1.0.0',
                          'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                                         'threshold_factor': 0.15, 'threshold_mode': 't',
                                         'min_speech_ms': 100, 'min_silence_ms': 200}},
            'segments': [{'segment_id': 'SEG-0000', 'start_ms': 100, 'end_ms': 500, 'confidence': 0.8,
                          'source': 'acoustic', 'method': 'energy_vad', 'uncertainty_ms': 10,
                          'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}}]}
        errors = acoustic_errors(doc)
        self.assertTrue(len(errors) > 0, 'insufficient_evidence with segments must be rejected')

    def test_segment_beyond_duration_rejected(self):
        doc = {
            'schema_version': '1.0.0', 'document_id': 't', 'status': 'complete', 'reason': None,
            'source': {'path': 't.wav', 'sha256': 'a' * 64, 'duration_ms': 500,
                        'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE'},
            'processor': {'method': 'energy_vad', 'processor_version': '1.0.0',
                          'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                                         'threshold_factor': 0.15, 'threshold_mode': 't',
                                         'min_speech_ms': 100, 'min_silence_ms': 200}},
            'segments': [{'segment_id': 'SEG-0000', 'start_ms': 100, 'end_ms': 600, 'confidence': 0.8,
                          'source': 'acoustic', 'method': 'energy_vad', 'uncertainty_ms': 10,
                          'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}}]}
        errors = acoustic_errors(doc)
        self.assertTrue(any('exceeds' in e.lower() for e in errors))


class AcousticCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from aivoicebench.__main__ import main
        self.main = main

    def test_cli_detects_segments(self):
        path = Path(self.tmp.name) / 'cli.wav'
        write_wav(path, silence_samples(500) + tone_samples(500) + silence_samples(500))
        out = Path(self.tmp.name) / 'out.json'
        exit_code = self.main(['acoustic', str(path), '--output', str(out),
                               '--min-speech-ms', '100', '--min-silence-ms', '100'])
        self.assertEqual(exit_code, 0)
        data = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(data['status'], 'complete')

    def test_cli_silent_returns_exit_2(self):
        path = Path(self.tmp.name) / 'silent.wav'
        write_wav(path, silence_samples(2000))
        out = Path(self.tmp.name) / 'out.json'
        exit_code = self.main(['acoustic', str(path), '--output', str(out)])
        self.assertEqual(exit_code, 2)

    def test_cli_directory_output_creates_document(self):
        path = Path(self.tmp.name) / 'silent-directory.wav'
        write_wav(path, silence_samples(2000))
        out = Path(self.tmp.name) / 'nested' / 'results'
        exit_code = self.main(['acoustic', str(path), '--output', str(out)])
        self.assertEqual(exit_code, 2)
        documents = list(out.glob('ACOUSTIC-*.json'))
        self.assertEqual(len(documents), 1)
        self.assertEqual(json.loads(documents[0].read_text(encoding='utf-8'))['status'],
                         'insufficient_evidence')

    def test_validate_acoustic_segments(self):
        path = Path(self.tmp.name) / 'doc.json'
        doc = {
            'schema_version': '1.0.0', 'document_id': 'ACOUSTIC-cli',
            'source': {'path': 'test.wav', 'sha256': 'b' * 64, 'duration_ms': 1000,
                        'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE'},
            'processor': {'method': 'energy_vad', 'processor_version': '1.0.0',
                           'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                                          'threshold_factor': 0.15, 'threshold_mode': 'test',
                                          'min_speech_ms': 100, 'min_silence_ms': 200}},
            'status': 'complete', 'reason': None,
            'segments': [{'segment_id': 'SEG-0000', 'start_ms': 100, 'end_ms': 500,
                           'confidence': 0.8, 'source': 'acoustic', 'method': 'energy_vad',
                           'uncertainty_ms': 10,
                           'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}}]}
        write_json(path, doc)
        exit_code = self.main(['validate', str(path), '--kind', 'acoustic-segments'])
        self.assertEqual(exit_code, 0)


if __name__ == '__main__':
    unittest.main()
