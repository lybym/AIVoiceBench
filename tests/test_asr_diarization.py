"""Tests for the real ASR-native diarization provider.

Verifies:
- Speaker labels derived from an existing ASR native response
- No second cloud call is performed
- Unknown/missing labels stay unknown (no sequential fill, no alternation)
- Single speaker / multi speaker / consecutive same speaker / non-alternating
- speaker ID namespacing per recording scope
- Confidence is null (not reused from recognition confidence)
- Provider-defined unknown label handling
- Invocation/native response referencing
"""

import unittest

from aivoicebench.diarization import ASRNativeDiarizationProvider
from aivoicebench.model_settings import SettingsError, build_run_providers
from aivoicebench.validation import schema_errors
from aivoicebench.volcengine_asr import STANDARD_SUBMIT_API

FLASH_API = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'


def transcript(segments, duration_ms=5000, sha='a' * 64):
    return {
        'transcript_id': 'TSR-1', 'run_id': 'RUN-1', 'case_id': None,
        'measurement_scope': 'external_asr', 'time_base': 'audio_relative_ms',
        'time_mapping': {'status': 'unmapped', 'offset_ms': None, 'uncertainty_ms': None},
        'source': {'path': 'n.wav', 'sha256': sha, 'role': 'room_mix', 'selected_channel': 1,
                   'duration_ms': duration_ms, 'provider_input_path': 'n.wav',
                   'provider_input_sha256': sha},
        'provider_profile': {'provider': 'volcengine', 'library_version': '1', 'model_id': 'bigmodel',
                             'model_version': 'service-managed', 'model_sha256': None, 'config': {}},
        'raw_response': {'path': 'raw.json', 'sha256': 'b' * 64},
        'status': 'complete', 'gaps': [],
        'segments': segments,
    }


def seg(index, start, end, speaker):
    return {
        'segment_id': f'ASR-{index:04d}', 'text': f'utterance {index}',
        'start_ms': start, 'end_ms': end, 'timestamp_source': 'asr_provider',
        'timestamp_confidence': None,
        'speaker_id': str(speaker) if speaker is not None else None,
        'raw_message_index': 0, 'words': [],
    }


class ASRNativeDiarizationTests(unittest.TestCase):
    def setUp(self):
        self.provider = ASRNativeDiarizationProvider()

    def test_two_speakers_non_alternating(self):
        """Non-alternating order must be preserved exactly as the provider reported."""
        doc = transcript([
            seg(0, 500, 1000, 2),   # provider speaker '2'
            seg(1, 1200, 1800, 2),  # same speaker continues
            seg(2, 2000, 2500, 7),  # different speaker
            seg(3, 2700, 3200, 2),  # back to first
        ])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        self.assertEqual(result.status, 'complete')
        segs = result.to_dict()['speaker_segments']
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[0]['speaker_id'], segs[1]['speaker_id'])
        self.assertEqual(segs[0]['speaker_id'], segs[3]['speaker_id'])
        self.assertNotEqual(segs[0]['speaker_id'], segs[2]['speaker_id'])
        # Native labels preserved
        self.assertEqual(segs[0]['native_speaker_id'], '2')
        self.assertEqual(segs[2]['native_speaker_id'], '7')

    def test_single_speaker(self):
        doc = transcript([seg(0, 0, 500, 1), seg(1, 600, 900, 1)])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        segs = result.to_dict()['speaker_segments']
        self.assertEqual({s['speaker_id'] for s in segs}, {segs[0]['speaker_id']})

    def test_missing_speaker_labels_is_insufficient(self):
        """No speaker labels at all → insufficient_evidence, never invented."""
        doc = transcript([seg(0, 0, 500, None), seg(1, 600, 900, None)])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.to_dict()['speaker_segments'], [])
        self.assertIn('speaker label', result.reason.lower())

    def test_partial_unknown_labels_stay_unknown(self):
        doc = transcript([seg(0, 0, 500, 1), seg(1, 600, 900, None), seg(2, 1000, 1400, 1)])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        segs = result.to_dict()['speaker_segments']
        self.assertEqual(result.status, 'partial')
        self.assertIsNone(segs[1]['native_speaker_id'])
        self.assertIn('unknown', segs[1]['speaker_id'])
        # Must NOT be assigned speaker_0/speaker_1 by position
        self.assertNotEqual(segs[1]['speaker_id'], segs[0]['speaker_id'])
        self.assertNotEqual(segs[1]['speaker_id'], segs[2]['speaker_id'])

    def test_speaker_id_namespaced_per_recording(self):
        """speaker_0 in one recording is not the same person as in another."""
        doc_a = transcript([seg(0, 0, 500, 0)], sha='a' * 64)
        doc_b = transcript([seg(0, 0, 500, 0)], sha='c' * 64)
        a = self.provider.diarize_from_transcript(doc_a, audio_sha256='a' * 64)
        b = self.provider.diarize_from_transcript(doc_b, audio_sha256='c' * 64)
        id_a = a.to_dict()['speaker_segments'][0]['speaker_id']
        id_b = b.to_dict()['speaker_segments'][0]['speaker_id']
        self.assertNotEqual(id_a, id_b, 'local speaker IDs must be scope-bound')

    def test_confidence_is_null_not_reused(self):
        """Recognition/timestamp confidence must never become clustering confidence."""
        doc = transcript([
            {'segment_id': 'ASR-0001', 'text': 'x', 'start_ms': 0, 'end_ms': 500,
             'timestamp_source': 'asr_provider', 'timestamp_confidence': 0.99,
             'speaker_id': '1', 'raw_message_index': 0,
             'words': [{'text': 'x', 'start_ms': 0, 'end_ms': 500, 'recognition_confidence': 0.98}]},
        ])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        speaker_segment = result.to_dict()['speaker_segments'][0]
        self.assertIsNone(speaker_segment['confidence'])

    def test_no_cloud_call_performed(self):
        """Derivation must not report a cloud call."""
        doc = transcript([seg(0, 0, 500, 1)])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        info = result.to_dict()['processor']
        self.assertFalse(info['config']['cloud_call_performed'])
        self.assertEqual(info['config']['derivation'], 'asr_native_speaker_labels')

    def test_invocation_reference_preserved(self):
        doc = transcript([seg(0, 0, 500, 1)])
        result = self.provider.diarize_from_transcript(
            doc, audio_sha256='a' * 64, invocation_id='provider-calls/CALL-abc',
            native_response_sha256='d' * 64, analysis_id='ANALYSIS-1')
        out = result.to_dict()
        self.assertEqual(out['scope']['invocation_id'], 'provider-calls/CALL-abc')
        self.assertEqual(out['scope']['native_response_sha256'], 'd' * 64)
        self.assertEqual(out['scope']['analysis_id'], 'ANALYSIS-1')

    def test_timestamp_source_is_provider_estimate(self):
        doc = transcript([seg(0, 0, 500, 1)])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        speaker_segment = result.to_dict()['speaker_segments'][0]
        self.assertEqual(speaker_segment['timing_source'], 'audio_relative_ms')
        self.assertEqual(speaker_segment['timestamp_source'], 'provider_utterance_estimate')

    def test_empty_transcript_is_insufficient(self):
        doc = transcript([])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertEqual(result.to_dict()['speaker_segments'], [])

    def test_schema_valid(self):
        doc = transcript([
            seg(0, 500, 1000, 2), seg(1, 1200, 1800, 7), seg(2, 2000, 2400, None),
        ])
        result = self.provider.diarize_from_transcript(
            doc, audio_sha256='a' * 64, invocation_id='CALL-1',
            native_response_sha256='d' * 64, analysis_id='ANALYSIS-1')
        out = result.to_dict()
        errors = schema_errors(out, 'speaker-segments')
        self.assertEqual(errors, [], f'schema errors: {errors}')

    def test_raw_position_preserved(self):
        doc = transcript([seg(0, 0, 500, 1), seg(1, 600, 900, 1)])
        result = self.provider.diarize_from_transcript(doc, audio_sha256='a' * 64)
        segs = result.to_dict()['speaker_segments']
        self.assertEqual(segs[0]['raw_utterance_index'], 0)
        self.assertEqual(segs[1]['raw_utterance_index'], 1)
        self.assertEqual(segs[0]['raw_message_index'], 0)


class DiarizationContractStatusTests(unittest.TestCase):
    """The published contract status must match the resolved profile (Issue #22 item 2).

    A Seed-standard profile really sends ``enable_speaker_info``, so reporting
    ``interface_contract_pending`` for it contradicts the transcript's own provider
    profile. Whether a *real call* returned labels remains a separate fact.
    """

    def test_verified_status_is_published_and_never_claimed_from_labels(self):
        # The request contract, not the observed labels: a Seed-standard profile
        # really sends enable_speaker_info, which is *why* the status may be
        # verified. What a caller may not do is infer the contract from labels.
        seed = ASRNativeDiarizationProvider(resource_id='volc.seedasr.auc', contract_status='verified')
        seed_result = seed.diarize_from_transcript(
            transcript([seg(0, 0, 500, 1)]), audio_sha256='a' * 64)
        seed_config = seed_result.to_dict()['processor']['config']
        self.assertEqual(seed_config['interface_contract_status'], 'verified')
        self.assertTrue(seed_config['labeled_utterances_observed'])
        self.assertFalse(seed_config['cloud_call_performed'])
        self.assertNotIn('real_call', seed_config)

        # The same provider parameterised as an unverified profile publishes
        # `interface_contract_pending` while observing exactly the same labels.
        # Observed labels are therefore not what promotes the status.
        pending = ASRNativeDiarizationProvider(contract_status='interface_contract_pending')
        pending_config = pending.diarize_from_transcript(
            transcript([seg(0, 0, 500, 1)]), audio_sha256='a' * 64).to_dict()['processor']['config']
        self.assertEqual(pending_config['interface_contract_status'], 'interface_contract_pending')
        self.assertEqual(pending_config['labeled_utterances_observed'], True)

        # Emitting labels is all this derivation may claim. It performs no cloud
        # call, so no published field may assert that a real call was verified —
        # that fact stays in the ASR profile's capability contract, which follows
        # the resolved mode rather than the labels a response happened to carry.
        self.assertNotIn('real_call', pending_config)
        self.assertNotIn('real_call', seed_result.to_dict())

    def test_verified_profile_without_labels_does_not_claim_unverified_contract(self):
        provider = ASRNativeDiarizationProvider(
            resource_id='volc.seedasr.auc', contract_status='verified')
        result = provider.diarize_from_transcript(
            transcript([seg(0, 0, 500, None)]), audio_sha256='a' * 64)
        self.assertEqual(result.status, 'insufficient_evidence')
        self.assertNotIn('unverified', result.reason)
        self.assertIn('verified', result.reason)

    def _asr_profile(self, profile_id, endpoint, resource_id, file_mode):
        parameters = {'resource_id': resource_id}
        if file_mode:
            parameters['file_mode'] = file_mode
        return {'id': profile_id, 'name': profile_id, 'provider': 'volcengine',
                'protocol': 'volcengine_asr', 'model': 'bigmodel', 'base_url': endpoint,
                'enabled': True, 'credential_env': '', 'capabilities': ['asr', 'diarization'],
                'parameters': parameters}

    def _snapshot(self, asr_profile, diarization_profile=None):
        """One snapshot whose File ASR and diarization routes may pick different profiles.

        The SQLite and external-config paths both funnel through
        ``build_run_providers``, so this is the shared chokepoint for the rule.
        """
        profiles = [asr_profile] + ([diarization_profile] if diarization_profile else [])
        return {'schema_version': '1.0.0', 'revision': 1, 'profiles': profiles,
                'routes': {'asr': asr_profile['id'],
                           'diarization': (diarization_profile or asr_profile)['id'],
                           'judge': None, 'tts': None, 'streaming_asr': None}}

    def test_resolved_profile_decides_the_reported_status(self):
        """Derive the status from the profile whose request really runs."""
        cases = (
            (STANDARD_SUBMIT_API, 'volc.seedasr.auc', 'seed_standard', 'verified'),
            (FLASH_API, 'volc.bigasr.auc_turbo', 'flash', 'interface_contract_pending'),
            (FLASH_API, 'volc.bigasr.auc_turbo', None, 'interface_contract_pending'),
        )
        for endpoint, resource_id, file_mode, expected in cases:
            with self.subTest(file_mode=file_mode):
                profile = self._asr_profile('file-asr', endpoint, resource_id, file_mode)
                _, providers = build_run_providers(self._snapshot(profile), {'file-asr': 'test-key'})
                diarizer = providers.diarization(None)
                result = diarizer.diarize_from_transcript(
                    transcript([seg(0, 0, 500, 1)]), audio_sha256='a' * 64)
                self.assertEqual(
                    result.to_dict()['processor']['config']['interface_contract_status'], expected)
                self.assertEqual(diarizer.contract_status, expected)

    def test_divergent_routes_are_refused_in_both_directions(self):
        """``verified`` must be impossible when a different profile transcribes.

        The diarization document used to take its contract status from its own
        route while the transcript came from the ``asr`` route.  An operator could
        therefore point ``asr`` at the Flash contract (which never sends
        ``enable_speaker_info``) and ``diarization`` at a Seed-standard profile and
        get a ``verified`` interface contract published for a request that never
        carried the switch.  Both routes are now required to agree, so the false
        provenance claim cannot be configured at all.
        """
        flash = self._asr_profile('flash-asr', FLASH_API, 'volc.bigasr.auc_turbo', 'flash')
        seed = self._asr_profile('seed-asr', STANDARD_SUBMIT_API, 'volc.seedasr.auc', 'seed_standard')
        for asr_profile, diarization_profile in ((flash, seed), (seed, flash)):
            with self.subTest(asr=asr_profile['id'], diarization=diarization_profile['id']):
                snapshot = self._snapshot(asr_profile, diarization_profile)
                with self.assertRaises(SettingsError) as caught:
                    build_run_providers(snapshot, {'flash-asr': 'test-key', 'seed-asr': 'test-key'})
                message = str(caught.exception)
                self.assertIn(asr_profile['id'], message)
                self.assertIn(diarization_profile['id'], message)

    def test_identical_route_keeps_the_truthful_status_for_both_modes(self):
        """The same-profile case still reports the mode that really runs."""
        for endpoint, resource_id, file_mode, expected in (
                (FLASH_API, 'volc.bigasr.auc_turbo', 'flash', 'interface_contract_pending'),
                (STANDARD_SUBMIT_API, 'volc.seedasr.auc', 'seed_standard', 'verified')):
            with self.subTest(file_mode=file_mode):
                profile = self._asr_profile('file-asr', endpoint, resource_id, file_mode)
                _, providers = build_run_providers(self._snapshot(profile), {'file-asr': 'test-key'})
                self.assertEqual(providers.diarization(None).contract_status, expected)

    def test_diarization_without_a_usable_asr_route_is_refused(self):
        """No ASR route means no request to read labels from, so nothing to claim."""
        seed = self._asr_profile('seed-asr', STANDARD_SUBMIT_API, 'volc.seedasr.auc', 'seed_standard')
        snapshot = self._snapshot(seed)
        snapshot['routes']['asr'] = None
        with self.assertRaises(SettingsError):
            build_run_providers(snapshot, {'seed-asr': 'test-key'})

    def test_unconfigured_diarization_route_is_still_allowed(self):
        """A File ASR route with no speaker-clustering purpose keeps working."""
        flash = self._asr_profile('flash-asr', FLASH_API, 'volc.bigasr.auc_turbo', 'flash')
        snapshot = self._snapshot(flash)
        snapshot['routes']['diarization'] = None
        _, providers = build_run_providers(snapshot, {'flash-asr': 'test-key'})
        self.assertIsNone(providers.diarization)
        self.assertIsNotNone(providers.asr)


if __name__ == '__main__':
    unittest.main()
