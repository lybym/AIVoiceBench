"""Regression tests for unsubstantiated automatic evaluation; no network."""
import json
import unittest
from aivoicebench.llm_provider import OpenAICompatibleProvider, create_provider_from_config
from aivoicebench.llm import LLMJudge
from aivoicebench.fusion import fuse, build_turns, detect_events
from aivoicebench.metrics import compute_timeline_metrics


class EvidenceGuards(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider('fixture', 'https://example.invalid',
                                                 'fixture', api_key='fixture-only')

    def test_sdk_failure_is_failed_and_does_not_echo_secrets(self):
        def fail(*args):
            raise RuntimeError('secret-canary')
        self.provider._call_api = fail
        result, invocation = self.provider.complete('test', 'test', 'intent', {})
        self.assertEqual(invocation.status, 'failed')
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertNotIn('secret-canary', json.dumps(result))

    def test_invalid_json_and_unreferenced_decisions_never_become_observations(self):
        for text in ['not JSON', '{}', '{"decision":"ok","reason":"ok","status":"observed","confidence":0.8}']:
            self.provider._call_api = lambda *args: text
            result, invocation = self.provider.complete('test', 'test', 'intent', {})
            self.assertEqual(invocation.status, 'failed')
            self.assertEqual(result['status'], 'insufficient_evidence')

    def test_model_invented_timestamp_is_rejected(self):
        text = json.dumps({'decision':'answer','reason':'answer','status':'observed',
                           'confidence':0.8,'evidence_refs':['E1'],'meaningful_response_start_ms':1234})
        with self.assertRaises(ValueError):
            self.provider._parse_response(text, 'meaningful_response', {'evidence_refs':['E1']})

    def test_default_semantics_are_unavailable(self):
        for provider in [LLMJudge().provider, create_provider_from_config({}),
                         create_provider_from_config({'llm_provider':'mock'})]:
            result, invocation = provider.complete('', '', 'intent', {})
            self.assertEqual(result['status'], 'insufficient_evidence')
            self.assertEqual(invocation.status, 'not_configured')

    def test_two_unattributed_segments_cannot_report_400ms_device_latency(self):
        acoustic = {'source':{'duration_ms':4000,'sha256':'a'*64},'segments':[
            {'segment_id':'a','start_ms':100,'end_ms':800},
            {'segment_id':'b','start_ms':1200,'end_ms':2000}]}
        fused = fuse(acoustic)
        events, _, status, _ = detect_events(fused, build_turns(fused))
        self.assertEqual(status, 'insufficient_evidence')
        self.assertEqual(compute_timeline_metrics({'events':events})['status'], 'insufficient_evidence')
