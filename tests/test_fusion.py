"""Tests for segment fusion, turn building, and event detection.

Uses synthetic acoustic-segments JSON documents (not real audio files)
to test the fusion → turns → events → timeline pipeline.
"""

import json
from pathlib import Path
import tempfile
import unittest

from aivoicebench.fusion import (
    fuse, build_turns, detect_events, generate_timeline, analyze_segments,
    FusedSegment, Turn,
)
from aivoicebench.runner import write_json
from aivoicebench.validation import fused_errors, turns_errors, timeline_errors


def acoustic_doc(segments, duration_ms=None, sha='a' * 64):
    """Build a minimal AcousticSegments 1.0.0 document."""
    if duration_ms is None:
        duration_ms = max((s[1] for s in segments), default=0) if segments else 0
    return {
        'schema_version': '1.0.0',
        'document_id': 'ACOUSTIC-test',
        'source': {
            'path': 'test.wav', 'sha256': sha, 'duration_ms': duration_ms,
            'sample_rate_hz': 16000, 'channels': 1, 'encoding': 'PCM_S16LE',
        },
        'processor': {
            'method': 'energy_vad', 'processor_version': '1.0.0',
            'parameters': {'frame_ms': 30, 'hop_ms': 10, 'energy_metric': 'rms',
                           'threshold_factor': 0.15, 'threshold_mode': 'test',
                           'min_speech_ms': 100, 'min_silence_ms': 200},
        },
        'status': 'complete', 'reason': None,
        'segments': [{'segment_id': f'SEG-{i:04d}', 'start_ms': s[0], 'end_ms': s[1],
                       'confidence': 0.8, 'source': 'acoustic', 'method': 'energy_vad',
                       'uncertainty_ms': 10,
                       'frame_stats': {'peak_rms': 0.5, 'mean_rms': 0.3, 'threshold_rms': 0.1, 'frame_count': 40}}
                      for i, s in enumerate(segments)],
    }


class FuseTests(unittest.TestCase):
    def test_missing_speaker_evidence_remains_unknown(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000), (2500, 3000), (3500, 4000)])
        fused = fuse(doc)
        self.assertEqual(fused['status'], 'partial')
        self.assertEqual([s['speaker_role'] for s in fused['segments']], ['unknown'] * 4)
        self.assertEqual(fused['attribution']['strategy'], 'none')
        self.assertEqual(build_turns(fused)['turns'], [])
        events, _, status, _ = detect_events(fused, build_turns(fused))
        self.assertEqual(events, [])
        self.assertEqual(status, 'insufficient_evidence')

    def test_empty_acoustic_is_insufficient(self):
        doc = acoustic_doc([])
        fused = fuse(doc)
        self.assertEqual(fused['status'], 'insufficient_evidence')
        self.assertEqual(fused['segments'], [])

    def test_fused_document_validates(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        fused = fuse(doc)
        self.assertEqual(fused_errors(fused), [])

    def test_transcript_text_merged(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        transcript = {
            'transcript_id': 'TSR-test', 'run_id': None, 'case_id': None,
            'measurement_scope': 'external_asr', 'time_base': 'audio_relative_ms',
            'time_mapping': {'status': 'unmapped', 'offset_ms': None, 'uncertainty_ms': None},
            'source': {'path': 't.wav', 'sha256': 'b' * 64, 'role': 'room_mix',
                        'selected_channel': 1, 'duration_ms': 2000,
                        'provider_input_path': 't.wav', 'provider_input_sha256': 'b' * 64},
            'provider_profile': {'provider': 'test', 'library_version': '1',
                                  'model_id': 'm', 'model_version': '1', 'model_sha256': 'c' * 64, 'config': {}},
            'raw_response': {'path': 'r.json', 'sha256': 'd' * 64},
            'status': 'complete', 'gaps': [],
            'segments': [{'segment_id': 'ASR-0', 'text': '今天天气怎么样', 'start_ms': 520, 'end_ms': 980,
                           'timestamp_source': 'asr_provider', 'timestamp_confidence': 0.9,
                           'speaker_id': None, 'raw_message_index': 0,
                           'words': [{'text': '今天', 'start_ms': 520, 'end_ms': 600, 'recognition_confidence': 0.9}]}],
        }
        fused = fuse(doc, transcript)
        self.assertEqual(fused['segments'][0]['text'], '今天天气怎么样')
        self.assertEqual(fused['segments'][0]['timing_source'], 'fused')
        self.assertEqual(fused['segments'][0]['asr_segment_id'], 'ASR-0')


def attributed_fixture(doc):
    """Explicit synthetic roles for testing the downstream turn/event consumer."""
    fused = fuse(doc)
    for segment, role in zip(fused['segments'], ['tester', 'device'] * len(fused['segments'])):
        segment.update(speaker_role=role, speaker_source='manual', speaker_confidence=1.0)
    return fused


class TurnBuilderTests(unittest.TestCase):
    def test_simple_turn_pair(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000), (2500, 3000), (3500, 4000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        self.assertEqual(turns['status'], 'complete')
        self.assertEqual(len(turns['turns']), 2)
        t1, t2 = turns['turns']
        self.assertEqual(len(t1['tester_segment_ids']), 1)
        self.assertEqual(len(t1['device_segment_ids']), 1)
        self.assertEqual(t1['tester_speech_start_ms'], 500)
        self.assertEqual(t1['tester_speech_end_ms'], 1000)
        self.assertEqual(t1['device_speech_start_ms'], 1200)
        self.assertEqual(t1['device_speech_end_ms'], 2000)
        self.assertEqual(t1['response_id'], 'RESP-0001')
        self.assertFalse(t1['has_interruption'])

    def test_turns_document_validates(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        self.assertEqual(turns_errors(turns), [])

    def test_empty_fused_is_insufficient(self):
        fused = fuse(acoustic_doc([]))
        turns = build_turns(fused)
        self.assertEqual(turns['status'], 'insufficient_evidence')

    def test_device_without_tester_is_orphan(self):
        doc = acoustic_doc([(500, 1000)])  # Only one segment → tester
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        self.assertEqual(len(turns['turns']), 1)
        self.assertEqual(len(turns['turns'][0]['device_segment_ids']), 0)


class EventDetectionTests(unittest.TestCase):
    def test_detects_speech_boundaries(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        self.assertEqual(status, 'complete')
        types = [e['type'] for e in events]
        self.assertIn('tester_speech_start', types)
        self.assertIn('tester_speech_end', types)
        self.assertIn('device_speech_start', types)
        self.assertIn('device_speech_end', types)

    def test_detects_silence_between_segments(self):
        doc = acoustic_doc([(500, 1000), (1500, 2000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        silences = [e for e in events if e['type'] == 'silence']
        self.assertEqual(len(silences), 1)
        self.assertAlmostEqual(silences[0]['start_ms'], 1000)
        self.assertAlmostEqual(silences[0]['end_ms'], 1500)

    def test_detects_timeout_for_long_silence(self):
        doc = acoustic_doc([(500, 1000), (7000, 8000)])  # 6s gap
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns, timeout_ms=5000)
        timeouts = [e for e in events if e['type'] == 'timeout']
        self.assertEqual(len(timeouts), 1)

    def test_detects_response_events(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        response_starts = [e for e in events if e['type'] == 'response_start']
        response_ends = [e for e in events if e['type'] == 'response_end']
        self.assertEqual(len(response_starts), 1)
        self.assertEqual(len(response_ends), 1)

    def test_detects_possible_false_endpoint(self):
        # Short tester segment (200ms) followed by immediate device response
        doc = acoustic_doc([(500, 700), (750, 1500), (2000, 3000), (3200, 4000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns, false_endpoint_ms=300)
        feps = [e for e in events if e['type'] == 'possible_false_endpoint']
        self.assertEqual(len(feps), 1)

    def test_events_have_evidence_refs(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        self.assertTrue(len(evidence) > 0)
        for ev in events:
            self.assertTrue(len(ev['evidence_ids']) > 0)
            self.assertEqual(ev['schema_version'], '2.0.0')

    def test_events_sorted_by_time(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000), (2500, 3000), (3500, 4000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, _, _, _ = detect_events(fused, turns)
        for i in range(1, len(events)):
            self.assertGreaterEqual(events[i]['start_ms'], events[i - 1]['start_ms'])

    def test_empty_fused_is_insufficient(self):
        fused = fuse(acoustic_doc([]))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        self.assertEqual(status, 'insufficient_evidence')


class TimelineGenerationTests(unittest.TestCase):
    def test_timeline_validates(self):
        doc = acoustic_doc([(500, 1000), (1200, 2000)])
        fused = attributed_fixture(doc)
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        timeline = generate_timeline(fused, turns, events, evidence, status, reason)
        self.assertEqual(timeline['schema_version'], '2.0.0')
        self.assertEqual(timeline['execution_kind'], 'imported')
        self.assertEqual(len(timeline['tracks']), 1)
        self.assertEqual(timeline['tracks'][0]['role'], 'room_mix')

    def test_insufficient_timeline_is_partial(self):
        fused = fuse(acoustic_doc([]))
        turns = build_turns(fused)
        events, evidence, status, reason = detect_events(fused, turns)
        timeline = generate_timeline(fused, turns, events, evidence, status, reason)
        self.assertEqual(timeline['status'], 'partial')
        self.assertTrue(len(timeline['gaps']) > 0)


class FusionCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from aivoicebench.__main__ import main
        self.main = main

    def test_cli_full_pipeline(self):
        acoustic_path = Path(self.tmp.name) / 'acoustic.json'
        doc = acoustic_doc([(500, 1000), (1200, 2000), (2500, 3000), (3500, 4000)])
        write_json(acoustic_path, doc)
        out = Path(self.tmp.name) / 'out'
        exit_code = self.main(['fusion', str(acoustic_path), '--output', str(out)])
        self.assertEqual(exit_code, 2)
        fused = json.loads((out / 'fused-segments.json').read_text(encoding='utf-8'))
        turns = json.loads((out / 'turns.json').read_text(encoding='utf-8'))
        timeline = json.loads((out / 'timeline.json').read_text(encoding='utf-8'))
        self.assertEqual(fused['status'], 'partial')
        self.assertEqual(len(turns['turns']), 0)
        self.assertEqual(timeline['events'], [])


if __name__ == '__main__':
    unittest.main()
