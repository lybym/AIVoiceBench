"""Tests for diarization provider and source attribution.

Tests that diarization outputs speaker IDs (NOT roles), and that
source attribution correctly returns 'unknown' for all speakers
without evidence — never inferring from first-speaker order.
"""

import json
import math
import sys
import unittest
from array import array
import wave
from pathlib import Path
import tempfile

from aivoicebench.diarization import (
    MockDiarizationProvider, SpeakerSegment, DiarizationResult,
    attribute_speakers,
)
from aivoicebench.runner import write_json
from aivoicebench.validation import schema_errors


def acoustic_doc(segments, duration_ms=None):
    if duration_ms is None:
        duration_ms = max((s[1] for s in segments), default=0) if segments else 0
    return {
        'schema_version': '1.0.0', 'document_id': 'ACOUSTIC-test',
        'source': {'path': 'test.wav', 'sha256': 'a' * 64, 'duration_ms': duration_ms,
                    'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE'},
        'processor': {'method': 'energy_vad', 'processor_version': '1.0.0',
                      'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                                     'threshold_factor': 0.15, 'threshold_mode': 'test',
                                     'min_speech_ms': 100, 'min_silence_ms': 200}},
        'status': 'complete', 'reason': None,
        'segments': [{'segment_id': f'SEG-{i:04d}', 'start_ms': s[0], 'end_ms': s[1],
                       'confidence': 0.8, 'source': 'acoustic', 'method': 'energy_vad',
                       'uncertainty_ms': 10,
                       'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}}
                      for i, s in enumerate(segments)],
    }


class MockDiarizationTests(unittest.TestCase):
    def test_two_speakers_clustered(self):
        ac = acoustic_doc([(500, 1000), (1200, 2000), (2500, 3000), (3500, 4000)])
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), ac['segments'])
        doc = result.to_dict()
        self.assertEqual(doc['status'], 'complete')
        self.assertEqual(len(doc['speaker_segments']), 4)
        ids = set(s['speaker_id'] for s in doc['speaker_segments'])
        self.assertIn('speaker_0', ids)
        self.assertIn('speaker_1', ids)

    def test_no_acoustic_segments(self):
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), [])
        doc = result.to_dict()
        self.assertEqual(doc['status'], 'insufficient_evidence')
        self.assertEqual(doc['speaker_segments'], [])

    def test_single_speaker(self):
        ac = acoustic_doc([(500, 2000)])
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), ac['segments'])
        doc = result.to_dict()
        ids = set(s['speaker_id'] for s in doc['speaker_segments'])
        self.assertEqual(ids, {'speaker_0'})

    def test_more_than_two_speakers(self):
        ac = acoustic_doc([(500, 1000), (1200, 1500), (2000, 2500), (3000, 3500), (4000, 4500), (5000, 5500)])
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), ac['segments'])
        doc = result.to_dict()
        ids = set(s['speaker_id'] for s in doc['speaker_segments'])
        self.assertGreaterEqual(len(ids), 2)

    def test_invocation_recorded(self):
        ac = acoustic_doc([(500, 1000), (1200, 2000)])
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), ac['segments'])
        doc = result.to_dict()
        inv = doc.get('invocation')
        self.assertIsNotNone(inv)
        self.assertTrue(inv['invocation_id'].startswith('CALL-'))
        self.assertIsNotNone(inv['latency_ms'])
        self.assertEqual(inv['status'], 'success')

    def test_schema_validates(self):
        ac = acoustic_doc([(500, 1000), (1200, 2000)])
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), ac['segments'])
        doc = result.to_dict()
        errors = schema_errors(doc, 'speaker-segments')
        self.assertEqual(errors, [], f'Schema errors: {errors}')

    def test_speaker_ids_are_not_roles(self):
        """Speaker IDs must be generic (speaker_0, speaker_1), NOT tester/device."""
        ac = acoustic_doc([(500, 1000), (1200, 2000)])
        provider = MockDiarizationProvider()
        result = provider.diarize(Path('test.wav'), ac['segments'])
        doc = result.to_dict()
        for seg in doc['speaker_segments']:
            self.assertNotIn(seg['speaker_id'], ('tester', 'device'),
                            'Diarization must output speaker IDs, not roles')


class SourceAttributionTests(unittest.TestCase):
    def _diar_doc(self, speakers):
        return {
            'document_id': 'DIAR-test',
            'speaker_segments': [
                {'segment_id': f'SPK-{i:04d}', 'speaker_id': sid,
                 'start_ms': i * 1000, 'end_ms': (i + 1) * 1000,
                 'confidence': 0.9, 'timing_source': 'audio_relative_ms',
                 'source': 'diarization', 'evidence_refs': []}
                for i, sid in enumerate(speakers)
            ],
            'status': 'complete',
        }

    def test_no_evidence_all_unknown(self):
        """Without evidence, ALL speakers are 'unknown' — no first-speaker inference."""
        diar = self._diar_doc(['speaker_0', 'speaker_1'])
        attr = attribute_speakers(diar)
        self.assertEqual(attr['status'], 'partial')
        self.assertEqual(len(attr['attributions']), 2)
        for a in attr['attributions']:
            self.assertEqual(a['role'], 'unknown')
            self.assertEqual(a['method'], 'none')
            self.assertEqual(a['confidence'], 0.0)

    def test_first_speaker_is_not_tester(self):
        """Critical: speaker_0 must NOT be auto-assigned as tester."""
        diar = self._diar_doc(['speaker_0', 'speaker_1'])
        attr = attribute_speakers(diar)
        spk0 = [a for a in attr['attributions'] if a['speaker_id'] == 'speaker_0'][0]
        self.assertEqual(spk0['role'], 'unknown')

    def test_explicit_mapping(self):
        """Explicit evidence can map speaker IDs to roles."""
        diar = self._diar_doc(['speaker_0', 'speaker_1'])
        mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
        attr = attribute_speakers(diar, explicit_mapping=mapping)
        self.assertEqual(attr['status'], 'complete')
        spk0 = [a for a in attr['attributions'] if a['speaker_id'] == 'speaker_0'][0]
        self.assertEqual(spk0['role'], 'tester')
        self.assertEqual(spk0['method'], 'explicit_evidence')
        self.assertEqual(spk0['confidence'], 1.0)

    def test_partial_explicit_mapping(self):
        diar = self._diar_doc(['speaker_0', 'speaker_1', 'speaker_2'])
        mapping = {'speaker_0': 'tester'}  # only one mapped
        attr = attribute_speakers(diar, explicit_mapping=mapping)
        self.assertEqual(attr['status'], 'partial')
        unmapped = [a for a in attr['attributions'] if a['role'] == 'unknown']
        self.assertEqual(len(unmapped), 2)

    def test_explicit_mapping_can_make_speaker_0_device(self):
        """Critical: explicit mapping CAN assign speaker_0 as device (not tester)."""
        diar = self._diar_doc(['speaker_0', 'speaker_1'])
        mapping = {'speaker_0': 'device', 'speaker_1': 'tester'}
        attr = attribute_speakers(diar, explicit_mapping=mapping)
        spk0 = [a for a in attr['attributions'] if a['speaker_id'] == 'speaker_0'][0]
        self.assertEqual(spk0['role'], 'device')

    def test_no_speakers(self):
        diar = {'speaker_segments': [], 'status': 'insufficient_evidence', 'document_id': 'DIAR-empty'}
        attr = attribute_speakers(diar)
        self.assertEqual(attr['status'], 'insufficient_evidence')
        self.assertEqual(attr['attributions'], [])

    def test_schema_validates(self):
        diar = self._diar_doc(['speaker_0', 'speaker_1'])
        attr = attribute_speakers(diar)
        errors = schema_errors(attr, 'source-attribution')
        self.assertEqual(errors, [], f'Schema errors: {errors}')

    def test_more_than_two_speakers_all_unknown(self):
        """Three+ speakers without evidence → all unknown."""
        diar = self._diar_doc(['speaker_0', 'speaker_1', 'speaker_2'])
        attr = attribute_speakers(diar)
        for a in attr['attributions']:
            self.assertEqual(a['role'], 'unknown')


if __name__ == '__main__':
    unittest.main()
