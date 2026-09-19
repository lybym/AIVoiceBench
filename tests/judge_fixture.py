"""Synthetic Timeline/Turns/Fused fixtures for the Issue #10 Judge contract tests.

No audio, provider or network: the fixtures are canonical documents that satisfy
``timeline_errors``/``turns_errors`` so a Judge citation either resolves against
real (synthetic) evidence or is correctly rejected. Fixture boundaries are
declared synthetic so nothing here can be mistaken for a measurement.
"""

from aivoicebench.llm import MockLLMProvider

RUN_ID = 'RUN-JUDGE-FIXTURE'
CASE_ID = 'CASE-JUDGE-FIXTURE'
ANALYSIS_ID = 'ANALYSIS-JUDGE-FIXTURE'


def make_timeline(events, evidence, tracks=None, artifacts=None, status='complete', gaps=()):
    return {
        'schema_version': '2.0.0', 'run_id': RUN_ID, 'case_id': CASE_ID,
        'case_version': '0.0.0', 'attempt': 1,
        'execution_kind': 'synthetic',
        'time_base': {'kind': 'run_monotonic_ms',
                      'origin': 'Synthetic zero; not a wall clock or hardware capture'},
        'run_snapshot': {'device': None, 'hardware': None, 'firmware': None, 'model': None,
                         'prompt': None, 'environment': None,
                         'test_assets': {'golden_set_id': None, 'golden_set_version': None,
                                         'case_sha256': None, 'asset_sha256': {}}},
        'status': status, 'gaps': list(gaps),
        'tracks': tracks if tracks is not None else [TRACK],
        'artifacts': artifacts if artifacts is not None else [ARTIFACT],
        'evidence': evidence, 'events': events,
    }


TRACK = {
    'track_id': 'TRACK-mix', 'role': 'room_mix', 'clock_id': 'CLOCK-fixture',
    'sample_rate_hz': 16000,
    'sync': {'status': 'synthetic', 'offset_ms': 0, 'uncertainty_ms': 0,
             'max_drift_ppm': 0, 'calibration_id': 'fixture-not-measured'},
}

ARTIFACT = {
    'artifact_id': 'ART-fixture', 'kind': 'audio', 'path': 'fixtures/mix.wav',
    'sha256': '1' * 64, 'origin': 'synthetic', 'track_id': 'TRACK-mix', 'duration_ms': 30000,
}


def make_evidence(evidence_id, start_ms, end_ms=None, source='audio_signal', confidence=0.8):
    return {
        'schema_version': '1.0.0', 'evidence_id': evidence_id, 'artifact_id': 'ART-fixture',
        'track_id': 'TRACK-mix', 'time_base': 'run_monotonic_ms',
        'start_ms': start_ms, 'end_ms': start_ms if end_ms is None else end_ms,
        'source': source, 'confidence': confidence,
        'confidence_source': 'acoustic_boundary' if source == 'audio_signal' else 'none',
    }


def make_event(event_id, event_type, start_ms, evidence_ids, turn_id='TURN-0001',
               response_id='RESP-0001', source='audio_signal', end_ms=None):
    return {
        'schema_version': '2.0.0', 'event_id': event_id, 'run_id': RUN_ID, 'case_id': CASE_ID,
        'turn_id': turn_id, 'response_id': response_id, 'type': event_type,
        'start_ms': start_ms, 'end_ms': start_ms if end_ms is None else end_ms,
        'source': source, 'observation_scope': 'black_box', 'confidence': 0.8,
        'confidence_source': 'acoustic_boundary' if source == 'audio_signal' else None,
        'uncertainty_ms': None, 'evidence_ids': list(evidence_ids),
    }


def dialogue_timeline(interruption=False):
    """One tester→device turn with two measured device boundaries.

    The device answers 2.5 s after the tester stops, and a second device boundary
    exists, so both a latency finding candidate and a two-boundary feedback
    interval can be selected from real boundaries rather than estimated from text
    length.
    """
    evidence = [
        make_evidence('EVD-TESTER-START', 500, source='manual_annotation', confidence=0.9),
        make_evidence('EVD-TESTER-END', 1000, source='manual_annotation', confidence=0.9),
        make_evidence('EVD-DEVICE-START', 3500),
        make_evidence('EVD-DEVICE-END', 5000),
    ]
    events = [
        make_event('EVT-TESTER-START', 'tester_speech_start', 500, ['EVD-TESTER-START']),
        make_event('EVT-TESTER-END', 'tester_speech_end', 1000, ['EVD-TESTER-END']),
        make_event('EVT-DEVICE-START', 'device_speech_start', 3500, ['EVD-DEVICE-START']),
        make_event('EVT-DEVICE-END', 'device_speech_end', 5000, ['EVD-DEVICE-END']),
    ]
    if interruption:
        # `interrupt_start` is an interval event in the timeline contract, so it
        # needs its paired end or the timeline is not a complete observation.
        evidence.append(make_evidence('EVD-INTERRUPT', 4200, source='manual_annotation',
                                      confidence=0.9))
        evidence.append(make_evidence('EVD-INTERRUPT-END', 4400, source='manual_annotation',
                                      confidence=0.9))
        events.append(make_event('EVT-INTERRUPT', 'interrupt_start', 4200, ['EVD-INTERRUPT']))
        events.append(make_event('EVT-INTERRUPT-END', 'interrupt_end', 4400,
                                 ['EVD-INTERRUPT-END']))
        events.sort(key=lambda event: event['start_ms'])
    return make_timeline(events, evidence)


def fused_document(interruption=False):
    segments = [
        {'segment_id': 'FSEG-0000', 'start_ms': 500, 'end_ms': 1000,
         'speaker_role': 'tester', 'speaker_id': 'cluster-0',
         'speaker_cluster_confidence': 0.9, 'role_attribution_confidence': 0.9,
         'role_attribution': 'explicit', 'acoustic_boundary_confidence': 0.8,
         'acoustic_uncertainty_ms': None, 'speaker_source': 'asr',
         'timing_source': 'acoustic', 'text': '今天天气怎么样？',
         'acoustic_segment_id': 'SEG-0000', 'asr_segment_id': None,
         'speaker_evidence': [], 'speaker_candidates': [], 'segment_origin': 'fused',
         'asr_start_ms': None, 'asr_end_ms': None, 'asr_speaker_id': None,
         'text_attribution': 'asr', 'start_boundary_source': 'audio_signal',
         'end_boundary_source': 'audio_signal'},
        {'segment_id': 'FSEG-0001', 'start_ms': 3500, 'end_ms': 5000,
         'speaker_role': 'device', 'speaker_id': 'cluster-1',
         'speaker_cluster_confidence': 0.9, 'role_attribution_confidence': 0.9,
         'role_attribution': 'explicit', 'acoustic_boundary_confidence': 0.8,
         'acoustic_uncertainty_ms': None, 'speaker_source': 'asr',
         'timing_source': 'acoustic',
         'text': '嗯……好的，让我看看。南京今天天气晴朗，气温25度。',
         'acoustic_segment_id': 'SEG-0001', 'asr_segment_id': None,
         'speaker_evidence': [], 'speaker_candidates': [], 'segment_origin': 'fused',
         'asr_start_ms': None, 'asr_end_ms': None, 'asr_speaker_id': None,
         'text_attribution': 'asr', 'start_boundary_source': 'audio_signal',
         'end_boundary_source': 'audio_signal'},
    ]
    if interruption:
        # The tester interrupts with a new intent; compliance is about whether the
        # following response honours it.
        segments.append(
            {'segment_id': 'FSEG-0002', 'start_ms': 4200, 'end_ms': 4400,
             'speaker_role': 'tester', 'speaker_id': 'cluster-0',
             'speaker_cluster_confidence': 0.9, 'role_attribution_confidence': 0.9,
             'role_attribution': 'explicit', 'acoustic_boundary_confidence': 0.8,
             'acoustic_uncertainty_ms': None, 'speaker_source': 'asr',
             'timing_source': 'acoustic', 'text': '不对，我想听南京今天的天气',
             'acoustic_segment_id': 'SEG-0002', 'asr_segment_id': None,
             'speaker_evidence': [], 'speaker_candidates': [], 'segment_origin': 'fused',
             'asr_start_ms': None, 'asr_end_ms': None, 'asr_speaker_id': None,
             'text_attribution': 'asr', 'start_boundary_source': 'audio_signal',
             'end_boundary_source': 'audio_signal'})
    return {
        'schema_version': '1.0.0', 'document_id': 'FUSED-fixture',
        'source': {'acoustic_document_id': 'ACOUSTIC-fixture',
                   'transcript_document_id': None, 'audio_sha256': '1' * 64,
                   'duration_ms': 30000},
        'attribution': {'strategy': 'explicit_user_mapping', 'provider': None,
                        'confidence': 0.9, 'note': 'fixture roles are explicit'},
        'status': 'complete', 'reason': None, 'unattributed_texts': [],
        'segments': segments,
    }


def turns_document(interruption=False):
    turn = {
        'turn_id': 'TURN-0001', 'tester_segment_ids': ['FSEG-0000'],
        'device_segment_ids': ['FSEG-0001'], 'response_id': 'RESP-0001',
        'start_ms': 500, 'end_ms': 5000,
        'tester_speech_start_ms': 500, 'tester_speech_end_ms': 1000,
        'device_speech_start_ms': 3500, 'device_speech_end_ms': 5000,
        'has_interruption': interruption, 'has_overlap': interruption,
        'interrupted_response_id': 'RESP-0001' if interruption else None,
        'interrupting_segment_ids': ['FSEG-0002'] if interruption else [],
    }
    return {'schema_version': '1.0.0', 'document_id': 'TURNS-fixture',
            'fused_document_id': 'FUSED-fixture', 'status': 'complete', 'reason': None,
            'turns': [turn]}


def fixture_provider(**kwargs):
    return MockLLMProvider(**kwargs)