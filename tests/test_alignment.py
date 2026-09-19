"""Acoustic ↔ ASR speaker-span alignment fixtures.

Coverage required by the Issue: full overlap, partial overlap, one-to-many and
many-to-one mapping, boundary drift, overlapping (conflicting) clusters, low-energy
speech and no-match. Also asserts the two hard boundaries: alignment is
deterministic, and it never turns an anonymous cluster into a tester/device role.
"""

import json
import shutil
import unittest
import uuid
import wave
from pathlib import Path
from unittest.mock import patch

from aivoicebench.alignment import (
    DEFAULT_POLICY, align_speaker_spans, explain_metric_gap, resolve_policy)
from aivoicebench.fusion import apply_speakers, fuse
from aivoicebench.validation import schema_errors

_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def acoustic(segments, duration_ms=10000, sha='a' * 64):
    return {
        'schema_version': '1.0.0', 'document_id': 'ACOUSTIC-1',
        'source': {'sha256': sha, 'duration_ms': duration_ms},
        'processor': {'method': 'energy_vad', 'processor_version': '1.0.0', 'parameters': {}},
        'status': 'complete', 'reason': None, 'segments': segments,
    }


def aseg(segment_id, start, end, mean_rms=0.05, peak_rms=0.1):
    return {'segment_id': segment_id, 'start_ms': start, 'end_ms': end,
            'confidence': 0.9, 'uncertainty_ms': 30.0,
            'frame_stats': {'mean_rms': mean_rms, 'peak_rms': peak_rms,
                            'threshold_rms': 0.01, 'frame_count': 10}}


def diarization(spans):
    """spans: list of (speaker_id, start_ms, end_ms, confidence)."""
    return {
        'document_id': 'DIAR-1',
        'processor': {'provider': 'test', 'model': 'test'},
        'status': 'complete',
        'speaker_segments': [
            {'segment_id': f'SPK-{i:04d}', 'speaker_id': sid, 'start_ms': start,
             'end_ms': end, 'confidence': conf,
             'native_speaker_id': sid.rsplit('_', 1)[-1], 'timestamp_source': 'provider_utterance_estimate'}
            for i, (sid, start, end, conf) in enumerate(spans)
        ],
    }


def only(document):
    return document['alignments'][0]


class OverlapShapeTests(unittest.TestCase):
    def test_full_overlap_is_one_cluster_and_complete(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 900, 2100, 0.8)]))
        entry = only(doc)
        self.assertEqual(entry['state'], 'single_cluster')
        self.assertEqual(entry['matched_speaker_id'], 'speaker_0')
        self.assertEqual(entry['speaker_matches'][0]['overlap_ms'], 1000.0)
        self.assertEqual(entry['speaker_matches'][0]['overlap_ratio_of_acoustic'], 1.0)
        self.assertEqual(doc['status'], 'complete')
        self.assertIsNone(doc['reason'])
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_ms'], 0.0)
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_ratio'], 0.0)

    def test_partial_overlap_records_both_intervals_ratios_and_offsets(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 1500, 2500, 0.5)]))
        match = only(doc)['speaker_matches'][0]
        # Both source intervals survive, plus the intersection.
        self.assertEqual((match['asr_start_ms'], match['asr_end_ms']), (1500.0, 2500.0))
        self.assertEqual((match['overlap_start_ms'], match['overlap_end_ms']), (1500.0, 2000.0))
        self.assertEqual(match['overlap_ms'], 500.0)
        self.assertEqual(match['overlap_ratio_of_acoustic'], 0.5)
        self.assertEqual(match['overlap_ratio_of_speaker'], 0.5)
        # Signed offsets: the provider span starts later and ends later.
        self.assertEqual(match['start_offset_ms'], 500.0)
        self.assertEqual(match['end_offset_ms'], 500.0)
        self.assertEqual(only(doc)['state'], 'single_cluster')

    def test_one_to_many_across_clusters_is_multi_cluster(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 1000, 1500, 0.8), ('speaker_1', 1500, 2000, 0.7)]))
        entry = only(doc)
        self.assertEqual(entry['state'], 'multi_cluster')
        self.assertIsNone(entry['matched_speaker_id'])
        self.assertEqual(entry['distinct_cluster_count'], 2)
        self.assertEqual([c['speaker_id'] for c in entry['speaker_candidates']],
                         ['speaker_0', 'speaker_1'])
        # A multi-cluster segment is never reported as fully unmatched.
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_ms'], 0.0)

    def test_many_spans_of_one_cluster_is_still_single_cluster(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 1000, 1500, 0.8), ('speaker_0', 1500, 2000, 0.8)]))
        entry = only(doc)
        self.assertEqual(entry['state'], 'single_cluster')
        self.assertEqual(entry['matched_speaker_id'], 'speaker_0')
        self.assertEqual(entry['distinct_cluster_count'], 1)
        self.assertEqual(len(entry['speaker_matches']), 2)

    def test_many_acoustic_segments_against_one_span(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 1300), aseg('SEG-0002', 1300, 1800)]),
            diarization([('speaker_0', 900, 2000, 0.9)]))
        self.assertEqual([e['state'] for e in doc['alignments']],
                         ['single_cluster', 'single_cluster'])
        self.assertEqual([e['matched_speaker_id'] for e in doc['alignments']],
                         ['speaker_0', 'speaker_0'])
        # Per-cluster coverage counts each cluster's own speech denominator once.
        cluster = doc['diagnostics']['per_cluster'][0]
        self.assertEqual(cluster['speaker_speech_ms'], 1100.0)
        self.assertEqual(cluster['matched_ms'], 800.0)
        self.assertEqual(cluster['coverage_ratio'], 0.7273)


class BoundaryDriftTests(unittest.TestCase):
    def test_drift_is_measured_against_the_configured_tolerance(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 1500, 2400, 0.8)]))
        drift = doc['diagnostics']['boundary_drift_ms']
        self.assertEqual(drift['matched_segment_count'], 1)
        self.assertEqual(drift['median_ms'], 900.0)  # |500| + |400|
        self.assertEqual(drift['max_ms'], 900.0)
        self.assertEqual(drift['tolerance_ms'], DEFAULT_POLICY['boundary_drift_tolerance_ms'])
        self.assertEqual(drift['beyond_tolerance_count'], 1)
        self.assertEqual(only(doc)['boundary_drift_ms'], 900.0)

    def test_drift_inside_tolerance_is_not_flagged(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 1040, 1960, 0.8)]))
        drift = doc['diagnostics']['boundary_drift_ms']
        self.assertEqual(drift['median_ms'], 80.0)
        self.assertEqual(drift['beyond_tolerance_count'], 0)


class ConflictAndNoMatchTests(unittest.TestCase):
    def test_conflicting_clusters_are_marked_conflict_not_split(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000)]),
            diarization([('speaker_0', 1000, 2000, 0.8), ('speaker_1', 1200, 1800, 0.7)]))
        entry = only(doc)
        self.assertEqual(entry['state'], 'conflict')
        self.assertIsNone(entry['matched_speaker_id'])
        self.assertEqual(doc['status'], 'partial')
        self.assertEqual(doc['diagnostics']['conflicted_acoustic_ms'], 1000.0)
        self.assertEqual(doc['diagnostics']['conflicted_acoustic_segment_ids'], ['SEG-0001'])
        self.assertIn('conflicting clusters', doc['reason'])

    def test_no_match_keeps_an_explicit_unmatched_state(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 5000, 6000)]),
            diarization([('speaker_0', 0, 1000, 0.8)]))
        entry = only(doc)
        self.assertEqual(entry['state'], 'unmatched')
        self.assertIsNone(entry['matched_speaker_id'])
        self.assertEqual(entry['speaker_matches'], [])
        self.assertEqual(doc['status'], 'partial')
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_ms'], 1000.0)
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_ratio'], 1.0)
        self.assertEqual(doc['diagnostics']['unmatched_speaker_ms'], 1000.0)
        self.assertEqual(doc['diagnostics']['unmatched_speaker_ratio'], 1.0)
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_segment_ids'], ['SEG-0001'])

    def test_no_speaker_spans_is_insufficient_evidence_not_silent_success(self):
        doc = align_speaker_spans(acoustic([aseg('SEG-0001', 1000, 2000)]),
                                  diarization([]))
        self.assertEqual(doc['status'], 'insufficient_evidence')
        self.assertEqual(only(doc)['state'], 'unmatched')
        self.assertIn('No ASR speaker spans', doc['reason'])


class LowEnergyTests(unittest.TestCase):
    def test_low_energy_segment_is_distributed_and_reported_unmatched(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000, mean_rms=0.09),
                      aseg('SEG-0002', 3000, 3400, mean_rms=0.00002)]),
            diarization([('speaker_0', 900, 2100, 0.8)]))
        low = doc['diagnostics']['low_energy']
        self.assertEqual(low['segment_count'], 1)
        self.assertEqual(low['speech_ms'], 400.0)
        self.assertEqual(low['unmatched_ms'], 400.0)
        self.assertEqual(low['unmatched_ratio'], 1.0)
        by_id = {e['acoustic_segment_id']: e for e in doc['alignments']}
        self.assertTrue(by_id['SEG-0002']['low_energy'])
        self.assertFalse(by_id['SEG-0001']['low_energy'])
        # The quiet miss is measurable, and it is not reported as speech absence.
        self.assertEqual(doc['diagnostics']['unmatched_acoustic_ms'], 400.0)

    def test_missing_energy_evidence_is_not_a_low_energy_finding(self):
        """A model-provider boundary reports probabilities, not RMS.

        Treating the absent RMS as zero would mark the model boundary itself as a
        quiet device. The distribution must instead exclude it and say so.
        """
        model_segment = {'segment_id': 'SEG-0009', 'start_ms': 3000, 'end_ms': 3600,
                         'confidence': 0.9, 'uncertainty_ms': 32.0,
                         'frame_stats': {'peak_speech_probability': 0.9,
                                         'mean_speech_probability': 0.7,
                                         'threshold': 0.5, 'frame_count': 18,
                                         'frames_above_threshold': 12}}
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000, mean_rms=0.19),
                      aseg('SEG-0002', 4000, 4500, mean_rms=0.21),
                      model_segment]),
            diarization([('speaker_0', 900, 2100, 0.8), ('speaker_0', 2900, 3700, 0.8),
                         ('speaker_0', 3900, 4600, 0.8)]))
        low = doc['diagnostics']['low_energy']
        by_id = {e['acoustic_segment_id']: e for e in doc['alignments']}
        # The model segment carries no RMS at all; it is neither low nor high.
        self.assertIsNone(by_id['SEG-0009']['acoustic_mean_rms'])
        self.assertFalse(by_id['SEG-0009']['low_energy'])
        # The distribution is computed from the two measured segments only, so its
        # p25 threshold (0.195) can only ever mark SEG-0001, never the model segment.
        self.assertEqual(low['segment_count'], 1)
        self.assertTrue(by_id['SEG-0001']['low_energy'])
        self.assertAlmostEqual(low['threshold_mean_rms'], 0.195, places=6)
        self.assertEqual({e['acoustic_segment_id']: e['mean_rms']
                          for e in low['energy_evidence']},
                         {'SEG-0001': 0.19, 'SEG-0002': 0.21, 'SEG-0009': None})
        self.assertIn('null', low['note'])

    def test_low_energy_is_never_derived_from_a_missing_reading(self):
        """Without the fix, every model segment would land in the distribution as 0.0."""
        model_segment = {'segment_id': 'SEG-0001', 'start_ms': 1000, 'end_ms': 1600,
                         'confidence': 0.9, 'uncertainty_ms': 32.0,
                         'frame_stats': {'peak_speech_probability': 0.9,
                                         'mean_speech_probability': 0.7,
                                         'threshold': 0.5, 'frame_count': 18,
                                         'frames_above_threshold': 12}}
        doc = align_speaker_spans(acoustic([model_segment]),
                                  diarization([('speaker_0', 900, 1700, 0.8)]))
        low = doc['diagnostics']['low_energy']
        self.assertEqual(low['segment_count'], 0)
        self.assertEqual(low['speech_ms'], 0.0)
        self.assertIsNone(low['threshold_mean_rms'])
        self.assertEqual(low['energy_evidence'],
                         [{'acoustic_segment_id': 'SEG-0001', 'mean_rms': None}])

    def test_all_segments_without_energy_evidence_reports_no_threshold(self):
        model_segment = {'segment_id': 'SEG-0001', 'start_ms': 1000, 'end_ms': 1600,
                         'confidence': 0.9, 'uncertainty_ms': 32.0,
                         'frame_stats': {'peak_speech_probability': 0.9,
                                         'mean_speech_probability': 0.7,
                                         'threshold': 0.5, 'frame_count': 18,
                                         'frames_above_threshold': 12}}
        doc = align_speaker_spans(acoustic([model_segment]),
                                  diarization([('speaker_0', 900, 1700, 0.8)]))
        low = doc['diagnostics']['low_energy']
        self.assertIsNone(low['threshold_mean_rms'])
        self.assertEqual(low['segment_count'], 0)
        self.assertEqual(low['speech_ms'], 0)
        self.assertEqual(schema_errors(doc, 'speaker-alignment'), [])

    def test_measured_zero_energy_is_still_a_low_energy_finding(self):
        """A real (energy-provider) zero reading stays a finding, unlike absence."""
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000, mean_rms=0.0)]),
            diarization([('speaker_0', 900, 2100, 0.8)]))
        low = doc['diagnostics']['low_energy']
        self.assertTrue(only(doc)['low_energy'])
        self.assertEqual(low['segment_count'], 1)
        self.assertEqual(low['speech_ms'], 1000.0)

    def test_low_energy_evidence_is_recorded_in_the_document(self):
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000, mean_rms=0.09)]),
            diarization([('speaker_0', 900, 2100, 0.8)]))
        low = doc['diagnostics']['low_energy']
        self.assertEqual(low['energy_evidence'],
                         [{'acoustic_segment_id': 'SEG-0001', 'mean_rms': 0.09}])
        self.assertEqual(schema_errors(doc, 'speaker-alignment'), [])

    def test_document_without_energy_evidence_still_validates(self):
        """The new member is optional, so an older document shape stays legal."""
        doc = align_speaker_spans(
            acoustic([aseg('SEG-0001', 1000, 2000, mean_rms=0.09)]),
            diarization([('speaker_0', 900, 2100, 0.8)]))
        del doc['diagnostics']['low_energy']['energy_evidence']
        self.assertEqual(schema_errors(doc, 'speaker-alignment'), [])

    def test_low_energy_profile_never_changes_the_canonical_policy(self):
        """A diagnostic sensitivity run is marked; it is not the measurement policy."""
        from aivoicebench.acoustic import EnergyVadSegmenter, resolve_sensitivity
        canonical = resolve_sensitivity(None)
        self.assertTrue(canonical['is_canonical_measurement_policy'])
        self.assertEqual(canonical['profile'], 'canonical')
        self.assertEqual(canonical['overrides'], {})
        quiet = resolve_sensitivity('quiet_device')
        self.assertFalse(quiet['is_canonical_measurement_policy'])
        self.assertIn('must not be reported as the canonical measurement', quiet['note'])
        overridden = resolve_sensitivity('canonical', {'threshold_factor': 0.05})
        self.assertFalse(overridden['is_canonical_measurement_policy'])
        self.assertEqual(overridden['overrides'], {'threshold_factor': 0.05})
        segmenter = EnergyVadSegmenter.from_profile('quiet_device')
        self.assertFalse(segmenter.sensitivity['is_canonical_measurement_policy'])

    def test_unknown_profile_and_override_are_rejected(self):
        from aivoicebench.acoustic import AcousticError, resolve_sensitivity
        with self.assertRaises(AcousticError):
            resolve_sensitivity('invented_profile')
        with self.assertRaises(AcousticError):
            resolve_sensitivity('canonical', {'unknown_parameter': 1.0})
        with self.assertRaises(AcousticError):
            resolve_sensitivity('canonical', {'min_speech_ms': -5.0})
        with self.assertRaises(ValueError):
            resolve_policy({'unknown_policy_field': 1.0})
        with self.assertRaises(ValueError):
            resolve_policy({'overlap_basis': 'provider_timing'})


class DeterminismAndRoleBoundaryTests(unittest.TestCase):
    def inputs(self):
        return (acoustic([aseg('SEG-0001', 1000, 2000), aseg('SEG-0002', 3000, 3400, 0.00002)]),
                diarization([('speaker_0', 1000, 1500, 0.8), ('speaker_1', 1500, 2100, 0.7)]))

    def test_alignment_is_deterministic(self):
        acoustic_doc, diarization_doc = self.inputs()
        first = align_speaker_spans(acoustic_doc, diarization_doc)
        second = align_speaker_spans(acoustic_doc, diarization_doc)
        self.assertNotEqual(first['document_id'], second['document_id'])
        first.pop('document_id')
        second.pop('document_id')
        self.assertEqual(first, second)

    def test_alignment_never_assigns_a_tester_or_device_role(self):
        """No alignment entry may carry a role value, only cluster identity."""
        acoustic_doc, diarization_doc = self.inputs()
        doc = align_speaker_spans(acoustic_doc, diarization_doc)

        def values(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    yield key
                    yield from values(child)
            elif isinstance(node, list):
                for child in node:
                    yield from values(child)
            else:
                yield node

        found = list(values(doc['alignments']))
        self.assertNotIn('tester', found)
        self.assertNotIn('device', found)
        self.assertNotIn('speaker_role', found)
        # Every matched speaker is still the anonymous provider cluster.
        matched = {item['speaker_id'] for entry in doc['alignments']
                   for item in entry['speaker_matches']}
        self.assertTrue(all(value.startswith('speaker_') for value in matched))

    def test_document_is_schema_valid(self):
        acoustic_doc, diarization_doc = self.inputs()
        doc = align_speaker_spans(acoustic_doc, diarization_doc)
        self.assertEqual(schema_errors(doc, 'speaker-alignment'), [])


class FusionIntegrationTests(unittest.TestCase):
    def fused(self):
        return fuse(acoustic([aseg('SEG-0001', 1000, 2000)]))

    def test_unmatched_segment_keeps_no_speaker_instead_of_the_nearest_one(self):
        acoustic_doc = acoustic([aseg('SEG-0001', 1000, 2000)])
        diarization_doc = diarization([('speaker_0', 5000, 6000, 0.8)])
        fused = fuse(acoustic_doc)
        apply_speakers(fused, diarization_doc, None,
                       align_speaker_spans(acoustic_doc, diarization_doc))
        seg = fused['segments'][0]
        self.assertIsNone(seg['speaker_id'])
        self.assertEqual(seg['speaker_evidence'], 'none')
        self.assertEqual(seg['speaker_candidates'], [])

    def test_conflicting_alignment_abstains_in_fusion(self):
        acoustic_doc = acoustic([aseg('SEG-0001', 1000, 2000)])
        diarization_doc = diarization([('speaker_0', 1000, 2000, 0.8),
                                       ('speaker_1', 1200, 1800, 0.7)])
        fused = fuse(acoustic_doc)
        apply_speakers(fused, diarization_doc, None,
                       align_speaker_spans(acoustic_doc, diarization_doc))
        seg = fused['segments'][0]
        self.assertIsNone(seg['speaker_id'])
        self.assertEqual(seg['speaker_evidence'], 'ambiguous_overlap')

    def test_multi_cluster_alignment_splits_in_fusion(self):
        acoustic_doc = acoustic([aseg('SEG-0001', 1000, 2000)])
        diarization_doc = diarization([('speaker_0', 1000, 1500, 0.8),
                                       ('speaker_1', 1500, 2000, 0.7)])
        fused = fuse(acoustic_doc)
        apply_speakers(fused, diarization_doc, None,
                       align_speaker_spans(acoustic_doc, diarization_doc))
        self.assertEqual([s['speaker_id'] for s in fused['segments']],
                         ['speaker_0', 'speaker_1'])

    def test_passed_alignment_matches_the_internal_default(self):
        """Routing fusion through alignment must not change the decision."""
        acoustic_doc = acoustic([aseg('SEG-0001', 1000, 2000),
                                 aseg('SEG-0002', 3000, 3400)])
        diarization_doc = diarization([('speaker_0', 1000, 1500, 0.8),
                                       ('speaker_1', 1500, 2100, 0.7),
                                       ('speaker_0', 2900, 3500, 0.6)])
        with_doc = fuse(acoustic_doc)
        apply_speakers(with_doc, diarization_doc, None,
                       align_speaker_spans(acoustic_doc, diarization_doc))
        without_doc = fuse(acoustic_doc)
        apply_speakers(without_doc, diarization_doc, None)
        # Only the randomly generated document identity may differ.
        self.assertEqual(with_doc['segments'], without_doc['segments'])
        self.assertEqual(with_doc['attribution'], without_doc['attribution'])
        self.assertEqual(with_doc['status'], without_doc['status'])
        self.assertEqual(with_doc['unattributed_texts'], without_doc['unattributed_texts'])


class MetricsGapExplanationTests(unittest.TestCase):
    def test_absence_is_explained_with_counts_and_no_zero_placeholder(self):
        fused = {'segments': [
            {'segment_id': 'FSEG-0000', 'speaker_id': None, 'speaker_role': 'unknown',
             'speaker_evidence': 'none'},
            {'segment_id': 'FSEG-0001', 'speaker_id': 'speaker_0', 'speaker_role': 'unknown',
             'speaker_evidence': 'single_cluster'}]}
        alignment = {'diagnostics': {'unmatched_acoustic_ms': 400.0, 'unmatched_speaker_ms': 200.0}}
        gap = explain_metric_gap(fused, {'events': []}, {'metrics': []}, alignment)
        self.assertEqual(gap['status'], 'insufficient_evidence')
        self.assertEqual(gap['observed_metric_count'], 0)
        codes = {reason['code']: reason['count'] for reason in gap['reasons']}
        self.assertEqual(codes['segments_without_speaker_span'], 1)
        self.assertEqual(codes['roles_not_confirmed'], 2)
        self.assertEqual(codes['no_timeline_events'], 0)
        self.assertEqual(gap['unmatched_acoustic_ms'], 400.0)
        self.assertEqual(gap['unmatched_speaker_ms'], 200.0)

    def test_observed_metrics_end_the_explanation(self):
        gap = explain_metric_gap(
            {'segments': []}, {'events': [{'event_id': 'E'}]},
            {'metrics': [{'name': 'turn_gap_ms', 'status': 'observed', 'value': 120.0}]})
        self.assertEqual(gap['status'], 'observed')
        self.assertEqual(gap['observed_metric_count'], 1)
        self.assertEqual(gap['reasons'], [])


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class ImportChainAlignmentTests(unittest.TestCase):
    """The Run must publish the alignment artifact and explain a blank metric table."""

    def setUp(self):
        import math
        import sys
        from array import array
        from aivoicebench.cloud_transport import API, HTTPReply
        from aivoicebench.diarization import ASRNativeDiarizationProvider
        from aivoicebench.import_pipeline import import_recording
        from aivoicebench.model_settings import RunProviders
        from aivoicebench.volcengine_asr import VolcengineASRProvider

        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        source = self.root / 'dialogue.wav'
        samples = []
        for value in ([0] * 8000 + [18000] * 8000 + [0] * 8000 + [18000] * 8000 + [0] * 8000):
            samples.append(int(18000 * math.sin(value)) if value else 0)
        data = array('h', samples)
        if sys.byteorder != 'little':
            data.byteswap()
        with wave.open(str(source), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(data.tobytes())

        reply = {'result': {'text': '今天天气怎么样 北京今天晴', 'utterances': [
            {'text': '今天天气怎么样', 'start_time': 500, 'end_time': 1000,
             'words': [], 'additions': {'speaker': '1'}},
            {'text': '北京今天晴', 'start_time': 1500, 'end_time': 2000,
             'words': [], 'additions': {'speaker': '2'}},
        ]}}

        class Transport:
            def __init__(self):
                self.calls, self.audio = [], b''

            def request(self, method, url, headers, body=None, **kwargs):
                self.calls.append((method, url, headers, body))
                if method == 'PUT':
                    self.audio = body
                    return HTTPReply(200, {}, b'')
                if method == 'GET':
                    return HTTPReply(200, {}, self.audio)
                return HTTPReply(200, {'x-api-status-code': '20000000'},
                                 json.dumps(reply, ensure_ascii=False).encode())

        self.transport = Transport()
        providers = RunProviders(
            asr=lambda root: VolcengineASRProvider(root, 'synthetic-key', endpoint=API,
                transport_config={'audio_transport': 'inline', 'inline_max_bytes': 1_000_000}),
            diarization=lambda root: ASRNativeDiarizationProvider())
        env = patch.dict('os.environ', {
            'AIVOICEBENCH_AUDIO_PUT_URL': 'https://storage.example/audio?put=1',
            'AIVOICEBENCH_AUDIO_GET_URL': 'https://storage.example/audio?get=1',
            'AIVOICEBENCH_AUDIO_HOST': 'storage.example'})
        env.start()
        self.addCleanup(env.stop)
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=self.transport.request):
            self.directory, self.manifest = import_recording(
                source, self.root / 'runs', synthetic=True, providers=providers)

    def documents(self):
        root = self.directory / 'analysis' / self.manifest['analysis_id']
        return {name: json.loads((root / f'{name}.json').read_text(encoding='utf-8'))
                for name in ('alignment', 'fused-segments', 'metrics', 'speaker-assignments')}

    def test_run_publishes_a_registered_alignment_artifact(self):
        artifact = next((a for a in self.manifest['artifacts']
                         if a['kind'] == 'speaker-alignment'), None)
        self.assertIsNotNone(artifact, 'the Run must register the alignment evidence')
        self.assertEqual(
            self.manifest['stages']['fusion']['processor']['alignment']['document_id'],
            self.documents()['alignment']['document_id'])

    def test_blank_metrics_are_explained_by_recorded_alignment_counts(self):
        docs = self.documents()
        diagnostics = docs['alignment']['diagnostics']
        self.assertEqual(diagnostics['acoustic_segment_count'], 2)
        self.assertEqual(diagnostics['speaker_span_count'], 2)
        self.assertEqual(diagnostics['per_cluster'][0]['coverage_ratio'], 1.0)
        # Roles are user-owned, so metrics must abstain rather than show zeros.
        self.assertEqual(docs['metrics']['status'], 'insufficient_evidence')
        self.assertIsNone(docs['metrics']['data'])

        from aivoicebench.api import _load_run
        view = _load_run(self.directory)
        self.assertEqual(view['alignment']['document_id'], docs['alignment']['document_id'])
        self.assertEqual(view['metrics_gap']['status'], 'insufficient_evidence')
        codes = {reason['code'] for reason in view['metrics_gap']['reasons']}
        self.assertIn('roles_not_confirmed', codes)
        serialized = json.dumps(view['metrics_gap'], ensure_ascii=False)
        self.assertNotIn('tester', serialized)

    def test_report_states_why_no_metric_is_available(self):
        report = (self.directory / 'analysis' / self.manifest['analysis_id']
                  / 'report.md').read_text(encoding='utf-8')
        self.assertIn('## 指标可用性', report)
        self.assertIn('roles_not_confirmed', report)
        self.assertIn('未匹配 acoustic 时长', report)
        self.assertIn('逐聚类覆盖', report)


if __name__ == '__main__':
    unittest.main()
