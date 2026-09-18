"""Normalization behavior for imperfect but successful Volcengine ASR replies."""

import json
import shutil
import unittest
import uuid
import wave
from pathlib import Path

from aivoicebench.cloud_transport import HTTPReply
from aivoicebench.providers import ProviderFailure, read_invocation
from aivoicebench.volcengine_asr import VolcengineASRProvider
from aivoicebench.volcengine_asr import STANDARD_QUERY_API, STANDARD_SUBMIT_API


_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def _canonical_wav(path, amplitude=0):
    """Write one second of canonical PCM16/16 kHz/mono WAV.

    ``amplitude`` distinguishes two otherwise identical-length recordings by
    content, which is what the recording-identity guard compares.
    """
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        if amplitude:
            import array
            import math
            samples = array.array('h', (int(amplitude * math.sin(2 * math.pi * 440 * i / 16000))
                                        for i in range(16000)))
            audio.writeframes(samples.tobytes())
        else:
            audio.writeframes(b'\x00\x00' * 1600)


class SeedTransport:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method, url, headers, body))
        reply = next(self.replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class VolcengineASRNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.provider = VolcengineASRProvider('/tmp', 'test-key')

    def test_invalid_word_offsets_keep_utterance_as_partial(self):
        reply = {
            'result': {
                'text': '这是在 ng 还是啥？',
                'utterances': [{
                    'text': '这是在 ng 还是啥？',
                    'start_time': 163000,
                    'end_time': 164320,
                    'words': [
                        {'text': '这', 'start_time': 163000, 'end_time': 163160, 'confidence': 0},
                        {'text': '是', 'start_time': -1, 'end_time': -1, 'confidence': 0},
                    ],
                }],
            },
        }

        segments, gaps = self.provider.normalize([json.dumps(reply)], 200000)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['text'], '这是在 ng 还是啥？')
        self.assertEqual(segments[0]['start_ms'], 163000)
        self.assertEqual(segments[0]['end_ms'], 164320)
        self.assertEqual(segments[0]['words'], [])
        self.assertEqual(len(gaps), 1)
        self.assertIn('invalid word timestamps', gaps[0]['reason'])

    def test_valid_word_offsets_are_preserved(self):
        reply = {
            'result': {
                'text': '你好。',
                'utterances': [{
                    'text': '你好。',
                    'start_time': 100,
                    'end_time': 500,
                    'words': [
                        {'text': '你', 'start_time': 100, 'end_time': 250, 'confidence': 0.9},
                        {'text': '好', 'start_time': 250, 'end_time': 500, 'confidence': 0.8},
                    ],
                }],
            },
        }

        segments, gaps = self.provider.normalize([json.dumps(reply)], 1000)

        self.assertEqual(gaps, [])
        self.assertEqual([word['text'] for word in segments[0]['words']], ['你', '好'])


class SeedStandardLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audio = self.root / 'input.wav'
        _canonical_wav(self.audio)
        self.success = HTTPReply(200, {'x-api-status-code': '20000000'}, json.dumps({
            'result': {'text': '你好', 'utterances': [{
                'text': '你好', 'start_time': 0, 'end_time': 100,
                'words': [], 'additions': {'speaker': '1'},
            }]},
        }, ensure_ascii=False).encode())

    def _provider(self, transport):
        return VolcengineASRProvider(
            self.root, 'test-key', endpoint=STANDARD_SUBMIT_API,
            resource_id='volc.seedasr.auc', timeout=2, transport=transport,
            transport_config={'audio_transport': 'inline', 'inline_max_bytes': 1_000_000})

    def test_seed_standard_submits_then_queries_with_speaker_info(self):
        transport = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'),
            self.success,
        ])

        output = self._provider(transport).transcribe(self.audio)

        self.assertEqual([call[1] for call in transport.calls],
                         [STANDARD_SUBMIT_API, STANDARD_QUERY_API])
        request_body = json.loads(transport.calls[0][3])
        self.assertIs(request_body['request']['enable_speaker_info'], True)
        self.assertEqual(json.loads(output.raw_messages[0])['result']['text'], '你好')

    def test_interrupted_seed_job_is_queried_without_second_submit(self):
        interrupted = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'),
            KeyboardInterrupt(),
        ])
        with self.assertRaises(KeyboardInterrupt):
            self._provider(interrupted).transcribe(self.audio)

        request = next((self.root / 'provider-calls').glob('CALL-*/request.json'))
        request_id = json.loads(request.read_text(encoding='utf-8'))['request_id']
        recovered = SeedTransport([self.success])

        output = self._provider(recovered).transcribe(self.audio)

        self.assertEqual(len(recovered.calls), 1)
        self.assertEqual(recovered.calls[0][1], STANDARD_QUERY_API)
        self.assertEqual(recovered.calls[0][2]['X-Api-Request-Id'], request_id)
        self.assertEqual(json.loads(output.raw_messages[0])['result']['text'], '你好')
        result = json.loads(next((self.root / 'provider-calls').glob('CALL-*/result.json')).read_text())
        self.assertEqual(result['status'], 'complete')
        self.assertGreaterEqual(result['latency_ms'], 0)

    def test_poll_timeout_stays_pending_and_resume_queries_same_job(self):
        pending = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'),
            HTTPReply(200, {'x-api-status-code': '20000001'}, b'{}'),
        ])
        with self.assertRaisesRegex(Exception, 'still pending'):
            self._provider(pending).transcribe(self.audio)

        call_dir = next((self.root / 'provider-calls').glob('CALL-*'))
        self.assertFalse((call_dir / 'result.json').exists())
        request_id = json.loads((call_dir / 'request.json').read_text())['request_id']

        recovered = SeedTransport([self.success])
        self._provider(recovered).transcribe(self.audio)

        self.assertEqual([call[1] for call in recovered.calls], [STANDARD_QUERY_API])
        self.assertEqual(recovered.calls[0][2]['X-Api-Request-Id'], request_id)
        self.assertEqual(json.loads((call_dir / 'result.json').read_text())['status'], 'complete')


class SeedStandardRecoveryIdempotencyTests(unittest.TestCase):
    """A partial recovery must be re-runnable without failing or destroying evidence.

    A worker interrupted after the recovery wrote ``native-response.json`` but
    before the terminal ``result.json`` used to make every later re-run raise
    ``FileExistsError`` and flip the audit to failed (Issue #22 item 5).
    """

    def setUp(self):
        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audio = self.root / 'input.wav'
        _canonical_wav(self.audio)
        self.success = HTTPReply(200, {'x-api-status-code': '20000000'}, json.dumps({
            'result': {'text': '你好', 'utterances': [{
                'text': '你好', 'start_time': 0, 'end_time': 100,
                'words': [], 'additions': {'speaker': '1'},
            }]},
        }, ensure_ascii=False).encode())

    def _provider(self, transport):
        return VolcengineASRProvider(
            self.root, 'test-key', endpoint=STANDARD_SUBMIT_API,
            resource_id='volc.seedasr.auc', timeout=2, transport=transport,
            transport_config={'audio_transport': 'inline', 'inline_max_bytes': 1_000_000})

    def _interrupted_submission(self):
        """Leave one submitted job with no terminal record, as a crash would."""
        interrupted = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'), KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self._provider(interrupted).transcribe(self.audio)
        call_dir = next((self.root / 'provider-calls').glob('CALL-*'))
        self.assertFalse((call_dir / 'result.json').exists())
        return call_dir

    def test_partial_recovery_with_identical_bytes_is_idempotent_success(self):
        call_dir = self._interrupted_submission()
        (call_dir / 'native-response.json').write_bytes(self.success.body)
        recovered = SeedTransport([self.success])

        output = self._provider(recovered).transcribe(self.audio)

        self.assertEqual([call[1] for call in recovered.calls], [STANDARD_QUERY_API])
        self.assertEqual((call_dir / 'native-response.json').read_bytes(), self.success.body)
        self.assertEqual(json.loads((call_dir / 'result.json').read_text())['status'], 'complete')
        self.assertEqual(json.loads(output.raw_messages[0])['result']['text'], '你好')

    def test_partial_recovery_with_different_bytes_fails_without_overwriting(self):
        call_dir = self._interrupted_submission()
        stale = b'{"result": {"text": "stale-evidence"}}'
        (call_dir / 'native-response.json').write_bytes(stale)
        recovered = SeedTransport([self.success])

        with self.assertRaises(ProviderFailure):
            self._provider(recovered).transcribe(self.audio)

        self.assertEqual((call_dir / 'native-response.json').read_bytes(), stale,
                         'differing retained evidence must never be overwritten')
        self.assertEqual(json.loads((call_dir / 'result.json').read_text())['status'], 'failed')


class SeedStandardCrossRecordingRecoveryTests(unittest.TestCase):
    """Resume must recover this recording's job, never another recording's.

    ``_pending_standard_audit`` originally matched a pending call on
    ``resource_id`` alone, so a still-pending submission for recording A could be
    recovered while transcribing recording B and A's transcript would be filed as
    B's result — real speech from the wrong audio.  ``start.json`` records the
    submitted input's sha256, so recovery is now scoped to the current input.
    """

    def setUp(self):
        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audio_a = self.root / 'recording-a.wav'
        _canonical_wav(self.audio_a)
        self.audio_b = self.root / 'recording-b.wav'
        _canonical_wav(self.audio_b, amplitude=1200)
        self.response_a = HTTPReply(200, {'x-api-status-code': '20000000'}, json.dumps({
            'result': {'text': '这是录音 A 的识别结果', 'utterances': [{
                'text': '这是录音 A 的识别结果', 'start_time': 0, 'end_time': 100,
                'words': [], 'additions': {'speaker': '1'}}]},
        }, ensure_ascii=False).encode())
        self.response_b = HTTPReply(200, {'x-api-status-code': '20000000'}, json.dumps({
            'result': {'text': '这是录音 B 的识别结果', 'utterances': [{
                'text': '这是录音 B 的识别结果', 'start_time': 0, 'end_time': 100,
                'words': [], 'additions': {'speaker': '2'}}]},
        }, ensure_ascii=False).encode())

    def _provider(self, transport):
        return VolcengineASRProvider(
            self.root, 'test-key', endpoint=STANDARD_SUBMIT_API,
            resource_id='volc.seedasr.auc', timeout=2, transport=transport,
            transport_config={'audio_transport': 'inline', 'inline_max_bytes': 1_000_000})

    def _leave_recording_a_pending(self):
        """Submit A and lose the query reply, exactly as an interrupted worker does."""
        interrupted = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'), KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self._provider(interrupted).transcribe(self.audio_a)
        call_dir = next((self.root / 'provider-calls').glob('CALL-*'))
        self.assertFalse((call_dir / 'result.json').exists())
        start = json.loads((call_dir / 'start.json').read_text(encoding='utf-8'))
        submitted = [a for a in start['input_artifacts'] if a['path'].endswith('recording-a.wav')]
        self.assertEqual(len(submitted), 1, 'the pending call must record which audio was submitted')
        return call_dir, json.loads(
            (call_dir / 'request.json').read_text(encoding='utf-8'))['request_id']

    def test_other_recording_is_submitted_instead_of_reusing_the_pending_result(self):
        a_call_dir, a_request_id = self._leave_recording_a_pending()
        transport = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'), self.response_b])

        output = self._provider(transport).transcribe(self.audio_b)

        # B is submitted and polled under its own request id; A's job is never queried.
        self.assertEqual([call[1] for call in transport.calls],
                         [STANDARD_SUBMIT_API, STANDARD_QUERY_API])
        self.assertNotIn(a_request_id, [call[2].get('X-Api-Request-Id') for call in transport.calls])
        self.assertEqual(json.loads(output.raw_messages[0])['result']['text'], '这是录音 B 的识别结果')
        self.assertNotIn('录音 A', output.raw_messages[0])
        # A's pending job stays pending and queryable for its own recording.
        self.assertFalse((a_call_dir / 'result.json').exists())

    def test_same_recording_still_resumes_the_pending_job(self):
        """The guard must not break the recovery it exists to protect."""
        a_call_dir, a_request_id = self._leave_recording_a_pending()
        transport = SeedTransport([self.response_a])

        output = self._provider(transport).transcribe(self.audio_a)

        self.assertEqual([call[1] for call in transport.calls], [STANDARD_QUERY_API])
        self.assertEqual(transport.calls[0][2]['X-Api-Request-Id'], a_request_id)
        self.assertEqual(json.loads(output.raw_messages[0])['result']['text'], '这是录音 A 的识别结果')
        self.assertEqual(json.loads((a_call_dir / 'result.json').read_text())['status'], 'complete')


class FileASRContractModeTests(unittest.TestCase):
    """``file_mode`` is authoritative-and-cross-checked (Issue #22 item 1)."""

    def test_absent_mode_still_infers_from_endpoint_and_resource(self):
        flash = VolcengineASRProvider('/tmp', 'test-key')
        self.assertEqual(flash.mode, 'flash')
        self.assertNotIn('enable_speaker_info', flash.config)

        standard = VolcengineASRProvider('/tmp', 'test-key', endpoint=STANDARD_SUBMIT_API,
                                        resource_id='volc.seedasr.auc')
        self.assertEqual(standard.mode, 'seed_standard')
        self.assertIs(standard.config['enable_speaker_info'], True)

    def test_matching_explicit_mode_is_accepted(self):
        flash = VolcengineASRProvider('/tmp', 'test-key', file_mode='flash')
        self.assertEqual(flash.mode, 'flash')
        self.assertNotIn('enable_speaker_info', flash.config)

        standard = VolcengineASRProvider('/tmp', 'test-key', endpoint=STANDARD_SUBMIT_API,
                                        resource_id='volc.seedasr.auc', file_mode='seed_standard')
        self.assertEqual(standard.mode, 'seed_standard')
        self.assertIs(standard.config['enable_speaker_info'], True)

    def test_explicit_seed_standard_with_flash_endpoint_is_refused(self):
        with self.assertRaises(ProviderFailure) as caught:
            VolcengineASRProvider('/tmp', 'test-key', file_mode='seed_standard')
        message = str(caught.exception)
        self.assertIn('seed_standard', message)
        self.assertIn('flash', message)

    def test_explicit_flash_with_standard_endpoint_is_refused(self):
        with self.assertRaises(ProviderFailure) as caught:
            VolcengineASRProvider('/tmp', 'test-key', endpoint=STANDARD_SUBMIT_API,
                                  resource_id='volc.seedasr.auc', file_mode='flash')
        self.assertIn('flash', str(caught.exception))

    def test_unknown_mode_value_is_refused(self):
        with self.assertRaises(ProviderFailure):
            VolcengineASRProvider('/tmp', 'test-key', file_mode='seed_standard_v2')


class FileASRFailureStateTests(unittest.TestCase):
    """Acceptance criterion 5 for File ASR: provider failure states stay inspectable.

    The equivalent Streaming ASR cases live in ``tests/test_streaming_asr.py``;
    these assert the persisted invocation record itself, with no API run.
    """

    def setUp(self):
        self.root = _TMP_ROOT / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.audio = self.root / 'input.wav'
        _canonical_wav(self.audio)

    def _provider(self, transport, key='test-key'):
        return VolcengineASRProvider(
            self.root, key, transport=transport,
            transport_config={'audio_transport': 'inline', 'inline_max_bytes': 1_000_000})

    def _only_failed_call(self):
        directories = list((self.root / 'provider-calls').glob('CALL-*'))
        self.assertEqual(len(directories), 1)
        record = read_invocation(directories[0], self.root)
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['failure_code'], 'provider_error')
        self.assertIsNotNone(record['finished_at'])
        return directories[0], record

    def test_missing_credential_persists_failed_invocation_without_request(self):
        transport = SeedTransport([])
        with self.assertRaises(ProviderFailure):
            self._provider(transport, key='').transcribe(self.audio)

        # The public message is deliberately generic (no provider text is echoed);
        # the inspectable evidence is the persisted invocation itself.
        self.assertEqual(transport.calls, [], 'a missing credential must not reach the network')
        directory, record = self._only_failed_call()
        self.assertTrue((directory / 'start.json').exists())
        self.assertEqual([artifact['path'] for artifact in record['input_artifacts']],
                         ['input.wav'])
        self.assertEqual(record['config']['model_name'], 'bigmodel')
        self.assertNotIn('api_key', record['config'])

    def test_status_code_rejection_persists_native_evidence_and_failure(self):
        transport = SeedTransport([
            HTTPReply(200, {'x-api-status-code': '45000001'},
                      b'{"code":45000001,"message":"request rejected"}')])
        with self.assertRaises(ProviderFailure):
            self._provider(transport).transcribe(self.audio)

        self.assertEqual([call[0] for call in transport.calls], ['POST'])
        directory, record = self._only_failed_call()
        self.assertEqual(json.loads((directory / 'native-response.json').read_text())['code'], 45000001)
        self.assertEqual(json.loads((directory / 'response-status.json').read_text())['status_code'], '45000001')
        persisted = {artifact['path'] for artifact in record['output_artifacts']}
        self.assertIn('native-response.json', ' '.join(persisted))

    def test_unavailable_resource_failure_is_persisted(self):
        transport = SeedTransport([
            HTTPReply(403, {'x-api-status-code': '45000002'},
                      b'{"code":45000002,"message":"resource not granted"}')])
        with self.assertRaises(ProviderFailure):
            self._provider(transport).transcribe(self.audio)

        directory, _record = self._only_failed_call()
        self.assertEqual(json.loads((directory / 'response-status.json').read_text())['http_status'], 403)

    def test_unsupported_contract_is_refused_before_any_call(self):
        transport = SeedTransport([])
        with self.assertRaises(ProviderFailure):
            VolcengineASRProvider(self.root, 'test-key',
                                  endpoint='https://example.invalid/api/v3/auc/bigmodel/submit',
                                  transport=transport)
        self.assertEqual(transport.calls, [])
        self.assertFalse((self.root / 'provider-calls').exists(),
                         'a refused contract must not create an invocation at all')


if __name__ == '__main__':
    unittest.main()
