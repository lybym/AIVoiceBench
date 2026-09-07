"""Synthetic transport fixtures; no external network calls or real recording."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid
import wave

from aivoicebench.providers import ProviderFailure, read_invocation
from aivoicebench.volcengine_asr import HTTPReply, SignedURLPublication, VolcengineASRJobs


KEY = 'fixture-credential-canary'
PUT = 'https://storage.example/audio.wav?signature=put-canary'
GET = 'https://storage.example/audio.wav?signature=get-canary'


class FixtureTransport:
    def __init__(self):
        self.calls = []
        self.audio = b''
        self.responses = []

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method, url, headers, body))
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio)
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(code, payload=None):
    return HTTPReply(200, {'x-api-status-code': code}, json.dumps(payload or {}).encode())


class CloudJobsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'normalized.wav'
        with wave.open(str(self.source), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(3200))
        self.transport = FixtureTransport()
        self.publication = SignedURLPublication(PUT, GET, 'storage.example', self.transport)
        self.provider = VolcengineASRJobs(self.root, KEY, self.transport)

    def submit(self):
        self.transport.responses.append(reply('20000000'))
        return self.provider.submit(self.source, self.publication)[0]

    def test_publish_submit_queue_then_complete_retains_native_speaker(self):
        job = self.submit()
        self.transport.responses.append(reply('20000002'))
        self.assertEqual(self.provider.query(job)[0], 'partial')
        response = {'result': {'utterances': [{'start_time': 10, 'end_time': 80,
                     'text': '你好', 'additions': {'speaker': '1'}}]}}
        self.transport.responses.append(reply('20000000', response))
        status, native = self.provider.query(job)
        self.assertEqual(status, 'complete')
        self.assertEqual(json.loads(native.read_bytes()), response)
        for directory in (self.root / 'provider-calls').iterdir():
            self.assertNotEqual(read_invocation(directory, self.root)['status'], 'pending')
        for path in self.root.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.bin'):
                content = path.read_bytes()
                for secret in [KEY, PUT, GET]:
                    self.assertNotIn(secret.encode(), content)
        self.assertNotIn('speaker_role', native.read_text())

    def test_unknown_submission_outcome_keeps_uuid_for_query_no_resubmit(self):
        self.transport.responses.append(RuntimeError(KEY))
        with self.assertRaises(ProviderFailure):
            self.provider.submit(self.source, self.publication)
        job = next((self.root / 'cloud-jobs').glob('*/job.json'))
        self.transport.responses.append(reply('20000001'))
        self.assertEqual(self.provider.query(job)[0], 'partial')
        urls = [c[1] for c in self.transport.calls]
        self.assertEqual(sum(u.endswith('/submit') for u in urls), 1)

    def test_returned_task_id_is_used_for_query(self):
        task = str(uuid.uuid4())
        self.transport.responses.append(reply('20000000', {'task_id': task}))
        job, _ = self.provider.submit(self.source, self.publication)
        self.transport.responses.append(reply('20000002'))
        self.provider.query(job)
        self.assertEqual(self.transport.calls[-1][2]['X-Api-Request-Id'], task)

    def test_silence_is_insufficient_not_device_timeout(self):
        job = self.submit()
        self.transport.responses.append(reply('20000003'))
        self.assertEqual(self.provider.query(job)[0], 'insufficient_evidence')

    def test_http_success_without_service_status_is_failed(self):
        self.transport.responses.append(HTTPReply(200, {}, b'{}'))
        job, status = self.provider.submit(self.source, self.publication)
        self.assertEqual(status, 'failed')
        with self.assertRaises(ProviderFailure):
            self.provider.query(job)

    def test_readback_mismatch_stops_before_asr(self):
        original = self.transport.request
        def corrupted(method, *args, **kwargs):
            if method == 'GET':
                return HTTPReply(200, {}, b'wrong audio')
            return original(method, *args, **kwargs)
        self.transport.request = corrupted
        with self.assertRaises(ProviderFailure):
            self.provider.submit(self.source, self.publication)
        self.assertFalse(any(c[0] == 'POST' for c in self.transport.calls))

    def test_changed_source_blocks_resume(self):
        job = self.submit()
        self.source.write_bytes(b'changed')
        with self.assertRaises(ProviderFailure):
            self.provider.query(job)

    def test_wrong_object_host_or_scheme_rejected(self):
        for get in ['https://storage.example/other.wav', 'http://storage.example/audio.wav',
                    'https://evil.example/audio.wav', 'https://user:pass@storage.example/audio.wav']:
            with self.subTest(url=get), self.assertRaises(ProviderFailure):
                SignedURLPublication(PUT, get, 'storage.example')

    def test_echoed_credential_is_not_persisted(self):
        self.transport.responses.append(reply('20000000', {'echo': KEY}))
        with self.assertRaises(ProviderFailure):
            self.provider.submit(self.source, self.publication)
        self.assertFalse(list(self.root.rglob('native-response.bin')))
        for path in self.root.rglob('*.json'):
            self.assertNotIn(KEY, path.read_text())

    def test_echoed_url_signature_is_not_persisted(self):
        self.transport.responses.append(reply('20000000', {'echo': 'get-canary'}))
        with self.assertRaises(ProviderFailure):
            self.provider.submit(self.source, self.publication)
        self.assertFalse(list(self.root.rglob('native-response.bin')))

    def test_changed_receipt_cannot_query_an_unrelated_task(self):
        job = self.submit()
        receipt = job.parent / 'submission.json'
        document = json.loads(receipt.read_bytes())
        document['task_id'] = str(uuid.uuid4())
        receipt.write_text(json.dumps(document))
        with self.assertRaises(ProviderFailure):
            self.provider.query(job)

    def test_missing_key_causes_no_network(self):
        with self.assertRaises(ProviderFailure):
            VolcengineASRJobs(self.root, None, self.transport)
        self.assertEqual(self.transport.calls, [])


if __name__ == '__main__':
    unittest.main()
