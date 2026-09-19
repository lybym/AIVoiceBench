"""Tests for finding generation and report rendering."""

import json
from pathlib import Path
import tempfile
import unittest

from aivoicebench.findings import generate_findings, SEVERITY_MAP
from aivoicebench.report import render_markdown, render_report
from aivoicebench.runner import write_json
from aivoicebench.validation import finding_errors


def make_timeline(events, evidence_list):
    return {
        'schema_version': '2.0.0', 'run_id': 'RUN-t', 'case_id': 'CASE-t',
        'case_version': '0.0.0', 'attempt': 1, 'execution_kind': 'imported',
        'time_base': {'kind': 'run_monotonic_ms', 'origin': 'first_decoded_sample'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None,
                         'model': None, 'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': 'complete', 'gaps': [],
        'tracks': [{'track_id': 'TRACK-mix', 'role': 'room_mix', 'clock_id': 'CLOCK-1',
                     'sample_rate_hz': 16000, 'sync': {'status': 'uncalibrated',
                     'offset_ms': None, 'uncertainty_ms': None, 'max_drift_ppm': None,
                     'calibration_id': None}}],
        'artifacts': [{'artifact_id': 'ART-audio', 'kind': 'audio', 'path': 'test.wav',
                       'sha256': 'a' * 64, 'origin': 'imported', 'track_id': 'TRACK-mix',
                       'duration_ms': 5000}],
        'evidence': evidence_list, 'events': events,
    }


def make_evidence(eid, start, end, source='audio_signal'):
    return {'schema_version': '1.0.0', 'evidence_id': eid, 'artifact_id': 'ART-audio',
            'track_id': 'TRACK-mix', 'time_base': 'run_monotonic_ms',
            'start_ms': start, 'end_ms': end, 'source': source, 'confidence': 0.7}


def make_event(eid, etype, start, turn_id='TURN-0001', evidence_ids=None):
    return {'schema_version': '2.0.0', 'event_id': eid, 'run_id': 'RUN-t',
            'case_id': 'CASE-t', 'turn_id': turn_id, 'response_id': 'RESP-0001',
            'type': etype, 'start_ms': start, 'end_ms': start,
            'source': 'audio_signal', 'observation_scope': 'black_box',
            'confidence': 0.7, 'evidence_ids': evidence_ids or ['EV-001']}


class FindingGenerationTests(unittest.TestCase):
    def test_generates_finding_from_high_latency_candidate(self):
        # A judge result now always cites the references it relied on: the engine
        # refuses an observed candidate that cannot name its evidence, so there is
        # no "nearest evidence" fallback to fall back on.
        judge_results = [{
            'dimension': 'finding_candidate', 'decision': 'high_latency',
            'status': 'observed', 'confidence': 0.6,
            'reason': 'Latency 3000ms exceeds 2000ms',
            'finding_severity': 'medium', 'suspected_layer': 'llm',
            'attribution_confidence': 0.4, 'requires_log_verification': True,
            'turn_id': 'TURN-0001', 'evidence_refs': ['EV-T', 'EV-D'],
            'event_refs': ['E1', 'E2'],
        }]
        evidence = [make_evidence('EV-T', 700, 1000), make_evidence('EV-D', 4000, 4500)]
        # Interval events must be paired and each event's evidence must cover its
        # own interval: a Finding is only generated from a Timeline that is itself
        # valid, and this fixture is a canonical synthetic Timeline.
        events = [
            make_event('E0', 'tester_speech_start', 700, evidence_ids=['EV-T']),
            make_event('E1', 'tester_speech_end', 1000, evidence_ids=['EV-T']),
            make_event('E2', 'device_speech_start', 4000, evidence_ids=['EV-D']),
            make_event('E3', 'device_speech_end', 4500, evidence_ids=['EV-D']),
        ]
        timeline = make_timeline(events, evidence)
        metrics = {'metrics': [{'name': 'first_speech_latency_ms', 'value': 3000, 'status': 'observed'}]}

        findings = generate_findings(judge_results, timeline, metrics)
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f['kind'], 'defect')
        self.assertEqual(f['severity'], 'P2')
        self.assertEqual(f['status'], 'needs_verification')
        self.assertEqual(f['attribution_status'], 'suspected')
        self.assertIn('llm', f['suspected_layers'])
        self.assertTrue(f['requires_log_verification'])
        self.assertTrue(len(f['evidence_ids']) > 0)
        self.assertEqual(f['human_review']['status'], 'required')

    def test_no_finding_from_insufficient_candidate(self):
        judge_results = [{
            'dimension': 'finding_candidate', 'decision': 'no_finding',
            'status': 'insufficient_evidence', 'confidence': 0.5,
            'reason': 'No issues',
            'finding_severity': None, 'suspected_layer': None,
            'attribution_confidence': None, 'requires_log_verification': False,
        }]
        timeline = make_timeline([], [make_evidence('EV-001', 0, 100)])
        metrics = {'metrics': []}
        findings = generate_findings(judge_results, timeline, metrics)
        self.assertEqual(len(findings), 0)

    def test_finding_validates_against_finding_schema(self):
        judge_results = [{
            'dimension': 'finding_candidate', 'decision': 'false_endpoint_detected',
            'status': 'observed', 'confidence': 0.6,
            'reason': '1 false endpoint detected',
            'finding_severity': 'medium', 'suspected_layer': 'endpoint',
            'attribution_confidence': 0.5, 'requires_log_verification': True,
            'turn_id': 'TURN-0001', 'evidence_refs': ['EV-001'], 'event_refs': ['E1'],
        }]
        evidence = [make_evidence('EV-001', 700, 750)]
        events = [make_event('E1', 'possible_false_endpoint', 700)]
        timeline = make_timeline(events, evidence)
        metrics = {'metrics': [{'name': 'false_endpoint_detected', 'value': True, 'status': 'observed'}]}

        findings = generate_findings(judge_results, timeline, metrics, run_id='RUN-t')
        self.assertEqual(len(findings), 1)
        # Validate finding schema + structure
        f = findings[0]
        self.assertEqual(f['run_id'], 'RUN-t')
        self.assertEqual(f['case_id'], 'CASE-t')
        self.assertEqual(f['execution_kind'], 'imported')
        self.assertEqual(f['kind'], 'defect')
        self.assertEqual(f['severity'], 'P2')
        self.assertEqual(f['status'], 'needs_verification')
        self.assertTrue(len(f['evidence_ids']) > 0)
        self.assertIn('endpoint', f['suspected_layers'])

    def test_severity_mapping(self):
        self.assertEqual(SEVERITY_MAP['critical'], ('P0', 'defect'))
        self.assertEqual(SEVERITY_MAP['high'], ('P1', 'defect'))
        self.assertEqual(SEVERITY_MAP['medium'], ('P2', 'defect'))
        self.assertEqual(SEVERITY_MAP['low'], ('P3', 'defect'))
        self.assertEqual(SEVERITY_MAP['info'], (None, 'observation'))


class ReportRenderingTests(unittest.TestCase):
    def test_render_markdown_has_all_sections(self):
        profile = {'device': 'TestDevice', 'supplier': 'TestSupplier'}
        fused_doc = {
            'segments': [{'segment_id': 'FSEG-0000', 'start_ms': 500, 'end_ms': 1000,
                          'speaker_role': 'tester', 'speaker_confidence': 0.5,
                          'speaker_source': 'heuristic', 'timing_source': 'acoustic',
                          'text': '你好', 'acoustic_segment_id': 'SEG-0', 'asr_segment_id': None}],
            'source': {'audio_sha256': 'a' * 64, 'duration_ms': 5000,
                       'acoustic_document_id': 'ACOUSTIC-test'},
            'attribution': {'strategy': 'alternating_heuristic', 'confidence': 0.5},
        }
        turns_doc = {'turns': [{'turn_id': 'TURN-0001', 'tester_segment_ids': ['FSEG-0000'],
                               'device_segment_ids': [], 'response_id': None,
                               'start_ms': 500, 'end_ms': 1000,
                               'tester_speech_start_ms': 500, 'tester_speech_end_ms': 1000,
                               'device_speech_start_ms': None, 'device_speech_end_ms': None,
                               'has_interruption': False, 'has_overlap': False}]}
        timeline = make_timeline([], [make_evidence('EV-001', 500, 1000)])
        metrics = {'status': 'observed', 'metrics': [
            {'name': 'first_speech_latency_ms', 'value': 500, 'unit': 'ms', 'status': 'observed',
             'turn_id': 'TURN-0001', 'evidence_ids': [], 'event_ids': []}]}
        judge_data = {'results': [{'dimension': 'intent', 'decision': 'greeting',
                                   'confidence': 0.7, 'status': 'observed', 'intent_label': 'greeting',
                                   'reason': 'Classified as greeting'}],
                      'invocations': [{'provider': 'mock', 'model': 'mock-llm-v1'}]}
        findings = []

        md = render_markdown(profile, fused_doc, turns_doc, timeline, metrics, judge_data, findings)
        self.assertIn('# AIVoiceBench 录音分析报告', md)
        self.assertIn('## 运行摘要', md)
        self.assertIn('## 设备信息', md)
        self.assertIn('## 音频分段', md)
        self.assertIn('## 对话轮次', md)
        self.assertIn('## LLM 语义评估', md)
        self.assertIn('## 证据', md)
        self.assertIn('## 溯源', md)

    def test_render_report_writes_files(self):
        profile = {'device': 'Test'}
        fused_doc = {'segments': [], 'source': {'audio_sha256': 'a' * 64, 'duration_ms': 0,
                       'acoustic_document_id': 'ACOUSTIC-t'}, 'attribution': {'strategy': 'none', 'confidence': 0}}
        turns_doc = {'turns': []}
        timeline = make_timeline([], [])
        metrics = {'status': 'insufficient_evidence', 'metrics': []}
        judge_data = {'results': [], 'invocations': []}
        findings = []

        with tempfile.TemporaryDirectory() as tmp:
            md_path, json_path = render_report(
                profile, fused_doc, turns_doc, timeline, metrics, judge_data, findings, tmp)
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            md_content = md_path.read_text(encoding='utf-8')
            self.assertIn('AIVoiceBench', md_content)
            json_data = json.loads(json_path.read_text(encoding='utf-8'))
            self.assertEqual(json_data['schema_version'], '1.0.0')

    def test_markdown_includes_findings(self):
        fused_doc = {'segments': [], 'source': {'audio_sha256': 'a' * 64, 'duration_ms': 0,
                       'acoustic_document_id': 'ACOUSTIC-t'}, 'attribution': {'strategy': 'none', 'confidence': 0}}
        turns_doc = {'turns': []}
        timeline = make_timeline([], [make_evidence('EV-001', 0, 100)])
        metrics = {'status': 'observed', 'metrics': []}
        judge_data = {'results': [], 'invocations': []}
        findings = [{
            'schema_version': '2.0.0', 'finding_id': 'FIND-001', 'run_id': 'RUN-t',
            'case_id': 'CASE-t', 'execution_kind': 'imported', 'kind': 'defect',
            'title': 'High Latency', 'severity': 'P2', 'status': 'needs_verification',
            'description': 'Latency exceeded threshold', 'expected_behavior': 'Fast response',
            'actual_behavior': 'Slow response', 'confidence': 0.6,
            'attribution_status': 'suspected', 'attribution_confidence': 0.4,
            'suspected_layers': ['llm'], 'requires_log_verification': True,
            'evidence_ids': ['EV-001'], 'event_ids': [], 'metric_ids': [],
            'human_review': {'status': 'required', 'reviewer': None, 'reviewed_at': None},
            'origin': 'manual', 'regression_case_candidate': False,
        }]
        md = render_markdown({}, fused_doc, turns_doc, timeline, metrics, judge_data, findings)
        self.assertIn('## Findings', md)
        self.assertIn('High Latency', md)
        self.assertIn('P2', md)
        self.assertIn('llm', md)


if __name__ == '__main__':
    unittest.main()
