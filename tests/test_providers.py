"""Synthetic contract tests; no cloud account, upload, model or device involved."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
import wave

from aivoicebench.asr import ProviderOutput, normalize_vosk, transcribe_file
from aivoicebench.asr_audit import RecordedASRProvider
from aivoicebench.providers import InvocationAudit, ProviderIdentity, ProviderFailure, invocation_errors, read_invocation
from aivoicebench.validation import load_document, transcript_errors


CONFIG = {'type': 'object', 'additionalProperties': False, 'required': ['words'],
          'properties': {'words': {'const': True}}}
IDENTITY = ProviderIdentity('synthetic', 'fixture', 'test-1')
PROFILE = dict(provider='synthetic', model_id='fixture', library_version='test-1',
               model_version='fixture-1', model_sha256='0' * 64, config={'words': True})


class SyntheticASR:
    def transcribe(self, path):
        return ProviderOutput(copy.deepcopy(PROFILE), [json.dumps({'text': '测试', 'result': [
            {'word': '测试', 'start': 0.01, 'end': 0.08, 'conf': 0.8}]}, ensure_ascii=False)])

    def normalize(self, messages, duration):
        return normalize_vosk(messages, duration)


class ProviderAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source.wav'
        with wave.open(str(self.source), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(bytes(3200))

    def audit(self, **kwargs):
        return InvocationAudit(self.root, IDENTITY, {'words': True}, CONFIG, [self.source], **kwargs)

    def wrapped(self, provider=None):
        return RecordedASRProvider(provider or SyntheticASR(), self.root, IDENTITY, PROFILE, CONFIG)

    def test_started_record_survives_interruption(self):
        audit = self.audit()
        start = load_document(audit.directory / 'start.json')
        self.assertEqual(start['status'], 'pending')
        self.assertIsNone(start['latency_ms'])
        self.assertFalse((audit.directory / 'result.json').exists())
        self.assertEqual(invocation_errors(start, self.root), [])

    def test_complete_keeps_original_start_and_hashes(self):
        audit = self.audit()
        before = (audit.directory / 'start.json').read_bytes()
        result = audit.finish('complete', [self.source])
        self.assertEqual(before, (audit.directory / 'start.json').read_bytes())
        self.assertGreaterEqual(result['latency_ms'], 0)
        self.assertIsNotNone(result['finished_at'])
        self.assertEqual(invocation_errors(result, self.root), [])
        with self.assertRaises(ValueError):
            audit.finish('failed', failure_code='timeout')

    def test_reader_resolves_pending_and_terminal_without_rewriting(self):
        audit = self.audit()
        self.assertEqual(read_invocation(audit.directory, self.root)['status'], 'pending')
        audit.finish('failed', failure_code='timeout')
        self.assertEqual(read_invocation(audit.directory, self.root)['status'], 'failed')

    def test_reader_rejects_result_attached_to_different_operation(self):
        audit = self.audit()
        result = audit.finish('complete', [self.source])
        result['operation_id'] = 'different-operation'
        (audit.directory / 'result.json').write_text(json.dumps(result), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'immutable start'):
            read_invocation(audit.directory, self.root)

    def test_completion_needs_output_evidence(self):
        audit = self.audit()
        with self.assertRaises(ValueError):
            audit.finish('complete')
        self.assertFalse((audit.directory / 'result.json').exists())

    def test_failure_needs_classification(self):
        audit = self.audit()
        with self.assertRaises(ValueError):
            audit.finish('failed')
        self.assertEqual(audit.finish('failed', failure_code='unavailable')['status'], 'failed')

    def test_retries_are_distinct_attempts_with_shared_operation(self):
        first = self.audit(operation_id='OP-test', attempt=1)
        first.finish('failed', failure_code='timeout')
        second = self.audit(operation_id='OP-test', attempt=2)
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual(first.record['operation_id'], second.record['operation_id'])
        self.assertEqual(second.record['attempt'], 2)

    def test_changed_input_cannot_claim_valid_result(self):
        audit = self.audit()
        self.source.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            audit.finish('complete', [self.source])
        self.assertTrue(invocation_errors(audit.record, self.root))

    def test_external_or_missing_artifacts_rejected(self):
        for path in [self.root / 'missing', self.root.parent]:
            with self.assertRaises(ValueError):
                InvocationAudit(self.root, IDENTITY, {'words': True}, CONFIG, [path])

    def test_read_validation_detects_tampering_and_traversal(self):
        result = self.audit().finish('complete', [self.source])
        result['output_artifacts'][0]['sha256'] = '1' * 64
        self.assertTrue(invocation_errors(result, self.root))
        result['input_artifacts'][0]['path'] = '../outside.wav'
        self.assertTrue(invocation_errors(result, self.root))

    def test_config_rejects_credentials_and_unknown_fields_without_echo(self):
        for key in ['api_key', 'Authorization', 'access-token', 'nested_secret', 'unexpected']:
            config = {'words': True, key: 'credential-canary'}
            with self.assertRaises(ValueError) as error:
                InvocationAudit(self.root, IDENTITY, config, CONFIG, [self.source])
            self.assertNotIn('credential-canary', str(error.exception))
        self.assertFalse((self.root / 'provider-calls').exists())

    def test_config_and_schema_are_detached_from_caller_mutation(self):
        config, schema = {'words': True}, copy.deepcopy(CONFIG)
        audit = InvocationAudit(self.root, IDENTITY, config, schema, [self.source])
        config['words'] = False
        schema['properties'].clear()
        result = audit.finish('complete', [self.source])
        self.assertTrue(result['config']['words'])
        self.assertIn('words', result['config_schema']['properties'])

    def test_open_config_schema_rejected(self):
        for schema in [{'type': 'object'}, {'type': 'object', 'additionalProperties': False,
                       'properties': {'nested': {'type': 'object'}}}]:
            with self.assertRaises(ValueError):
                InvocationAudit(self.root, IDENTITY, {}, schema, [self.source])

    def test_signed_endpoint_is_not_logged(self):
        identity = ProviderIdentity('synthetic', 'fixture', 'test',
                                    endpoint='https://example.com/api?token=credential-canary')
        with self.assertRaises(ValueError) as error:
            InvocationAudit(self.root, identity, {'words': True}, CONFIG, [self.source])
        self.assertNotIn('credential-canary', str(error.exception))

    def test_asr_wrapper_preserves_native_text_and_legacy_contract(self):
        output, transcript = transcribe_file(self.source, self.wrapped(), self.root / 'asr', 'room_mix')
        self.assertEqual(transcript_errors(transcript), [])
        self.assertIsNone(transcript['segments'][0]['timestamp_confidence'])
        call = next((self.root / 'provider-calls').iterdir())
        self.assertEqual(load_document(call / 'native-response.json'), load_document(output / 'raw-provider.json'))
        result = load_document(call / 'result.json')
        self.assertEqual(invocation_errors(result, self.root), [])
        self.assertEqual(load_document(call / 'provider-profile.json'), PROFILE)

    def test_sdk_exception_does_not_leak_into_legacy_error_report(self):
        class Broken(SyntheticASR):
            def transcribe(self, path):
                raise RuntimeError('credential-canary https://secret.example/signed-url')
        with self.assertRaises(ProviderFailure):
            transcribe_file(self.source, self.wrapped(Broken()), self.root / 'asr')
        for path in self.root.rglob('*.json'):
            self.assertNotIn('credential-canary', path.read_text(encoding='utf-8'))
        call = next((self.root / 'provider-calls').iterdir())
        self.assertEqual(load_document(call / 'result.json')['status'], 'failed')

    def test_normalization_failure_retains_completed_call_and_native_response(self):
        class Broken(SyntheticASR):
            def normalize(self, messages, duration):
                raise RuntimeError('credential-canary')
        with self.assertRaises(ProviderFailure):
            transcribe_file(self.source, self.wrapped(Broken()), self.root / 'asr')
        call = next((self.root / 'provider-calls').iterdir())
        self.assertEqual(load_document(call / 'result.json')['status'], 'complete')
        self.assertTrue((call / 'native-response.json').is_file())
        self.assertFalse(list(self.root.rglob('transcript.json')))

    def test_changed_profile_rejected_before_native_persistence(self):
        class Changed(SyntheticASR):
            def transcribe(self, path):
                result = super().transcribe(path)
                result.profile['config']['api_key'] = 'credential-canary'
                return result
        with self.assertRaises(ProviderFailure):
            self.wrapped(Changed()).transcribe(self.source)
        for path in self.root.rglob('*.json'):
            self.assertNotIn('credential-canary', path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
