"""Normalization behavior for imperfect but successful Volcengine ASR replies."""

import json
import shutil
import unittest
import uuid
import wave
from pathlib import Path

from aivoicebench.cloud_transport import HTTPReply
from aivoicebench.volcengine_asr import VolcengineASRProvider
from aivoicebench.volcengine_asr import STANDARD_QUERY_API, STANDARD_SUBMIT_API


_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def _canonical_wav(path):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
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


if __name__ == '__main__':
    unittest.main()
