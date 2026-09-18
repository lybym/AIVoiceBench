"""Tests for File ASR inline|object_storage|auto transport (Issue #87).

Covers transport threshold selection, inline Base64 audio.data, private TOS
upload + Presigned GET + cleanup audit, missing-storage behavior, and
secret/signed-URL redaction.  Uses a controlled TOS client double — no real
network calls; real Volcengine/TOS verification is recorded separately.
"""

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
import wave
import shutil
from pathlib import Path
from unittest.mock import patch

from aivoicebench.cloud_transport import API, HTTPReply
from aivoicebench.model_settings import DEFAULT_INLINE_MAX_BYTES
from aivoicebench.tos_adapter import TOSStorageAdapter, TOSStorageConfig
from aivoicebench.volcengine_asr import VolcengineASRProvider
from aivoicebench.providers import ProviderFailure

# Workspace-rooted temp so the file sandbox permits writes.
_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def _mkdtemp():
    """Create a temp dir without tempfile.mkdtemp's restrictive chmod."""
    import uuid
    d = _TMP_ROOT / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


REPLY = {'result': {'text': '你好世界', 'utterances': [
    {'text': '你好世界', 'start_time': 100, 'end_time': 600,
     'words': [], 'additions': {'speaker': '1'}}]}}


def _canonical_wav(path, frames=16000):
    """Create a canonical PCM16/16kHz/mono WAV."""
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\x00\x00' * frames)


class MockTOSClient:
    """In-memory TOS client double — no network access."""

    def __init__(self):
        self.objects = {}
        self.uploaded = []
        self.deleted = []

    def put_object(self, bucket, key, content):
        self.objects[key] = content
        self.uploaded.append(key)
        return {'version_id': 'test'}

    def pre_signed_url(self, method, bucket, key, expires):
        return f'https://tos.example.com/{bucket}/{key}?expires={expires}&sig=SECRET-PRESIGNED-TOKEN'

    def delete_object(self, bucket, key):
        if key in self.objects:
            del self.objects[key]
        self.deleted.append(key)
        return {'deleted': True}


class MockTransport:
    """HTTP transport that captures the outgoing request body."""

    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method, url, headers, body))
        return HTTPReply(200, {'x-api-status-code': '20000000'},
                         json.dumps(REPLY, ensure_ascii=False).encode())


class FileASRTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = _mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.root = Path(self.tmp_dir)
        self.small = self.root / 'small.wav'
        _canonical_wav(self.small, frames=16000)  # ~32 KiB

    def _provider(self, *, transport_config, storage_adapter_factory=None, key='test-key'):
        transport = MockTransport()
        provider = VolcengineASRProvider(
            self.root, key, endpoint=API, transport=transport,
            transport_config=transport_config,
            storage_adapter_factory=storage_adapter_factory)
        return provider, transport

    def _tos_adapter(self, client=None):
        config = TOSStorageConfig(
            id='tos-test', endpoint='https://tos.example.com', region='cn-beijing',
            bucket='private-bucket', prefix='asr/',
            access_key_env='TOS_AK', secret_key_env='TOS_SK', session_token_env='',
            presigned_get_ttl_seconds=300, delete_after_use=True, lifecycle_max_age_hours=24)
        client = client or MockTOSClient()
        with patch.dict(os.environ, {'TOS_AK': 'ak-value', 'TOS_SK': 'sk-value'}):
            return TOSStorageAdapter(config, client_factory=lambda: client), client

    # --- inline transport ---

    def test_inline_small_file_uses_audio_data_no_storage(self):
        """auto mode with a small file sends audio.data, no object storage."""
        provider, transport = self._provider(transport_config={
            'audio_transport': 'auto', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES})
        provider._transcribe(self.small)
        self.assertEqual([c[0] for c in transport.calls], ['POST'])
        body = json.loads(transport.calls[0][3])
        self.assertIn('data', body['audio'])
        self.assertNotIn('url', body['audio'])
        # The Base64 data decodes back to the canonical WAV bytes.
        decoded = base64.b64decode(body['audio']['data'])
        self.assertEqual(decoded, self.small.read_bytes())

    def test_explicit_inline_mode_always_uses_audio_data(self):
        """inline mode ignores file size and always sends audio.data."""
        provider, transport = self._provider(transport_config={
            'audio_transport': 'inline', 'inline_max_bytes': 1})
        provider._transcribe(self.small)
        body = json.loads(transport.calls[0][3])
        self.assertIn('data', body['audio'])

    def test_inline_transport_proof_audited(self):
        """The transport choice and source hash are auditable."""
        provider, _ = self._provider(transport_config={
            'audio_transport': 'inline', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES})
        provider._transcribe(self.small)
        transport_json = list((self.root / 'provider-calls').glob('*/transport.json'))
        self.assertTrue(transport_json, 'transport.json audit proof must exist')
        proof = json.loads(transport_json[0].read_text())
        self.assertEqual(proof['transport'], 'inline')
        self.assertEqual(proof['source_sha256'],
                         hashlib.sha256(self.small.read_bytes()).hexdigest())

    # --- object storage transport ---

    def test_auto_large_file_uses_object_storage(self):
        """auto mode with a file over inline_max_bytes uses TOS + Presigned GET."""
        adapter, client = self._tos_adapter()
        provider, transport = self._provider(transport_config={
            'audio_transport': 'auto', 'inline_max_bytes': 1024},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        provider._transcribe(self.small)
        body = json.loads(transport.calls[0][3])
        self.assertIn('url', body['audio'])
        self.assertNotIn('data', body['audio'])
        self.assertIn('sig=SECRET-PRESIGNED-TOKEN', body['audio']['url'])
        self.assertEqual(len(client.uploaded), 1)

    def test_explicit_object_storage_mode_uses_tos(self):
        """object_storage mode always uploads to TOS regardless of size."""
        adapter, client = self._tos_adapter()
        provider, transport = self._provider(transport_config={
            'audio_transport': 'object_storage', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        provider._transcribe(self.small)
        body = json.loads(transport.calls[0][3])
        self.assertIn('url', body['audio'])
        self.assertEqual(len(client.uploaded), 1)

    def test_object_storage_cleanup_after_success(self):
        """The private object is deleted after a successful ASR call."""
        adapter, client = self._tos_adapter()
        provider, _ = self._provider(transport_config={
            'audio_transport': 'object_storage', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        provider._transcribe(self.small)
        self.assertEqual(len(client.deleted), 1, 'cleanup must delete the private object')

    def test_object_storage_cleanup_after_failure(self):
        """Cleanup runs even when the ASR call fails (lifecycle backstop)."""
        adapter, client = self._tos_adapter()
        transport = MockTransport()

        def fail_request(*args, **kwargs):
            raise ProviderFailure('simulated failure')
        transport.request = fail_request
        provider = VolcengineASRProvider(
            self.root, 'test-key', endpoint=API, transport=transport,
            transport_config={'audio_transport': 'object_storage', 'inline_max_bytes': 1},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        with self.assertRaises(ProviderFailure):
            provider._transcribe(self.small)
        self.assertEqual(len(client.deleted), 1, 'cleanup must run after failure')

    # --- missing storage config ---

    def test_missing_storage_blocks_only_object_storage_not_inline(self):
        """Missing storage adapter blocks object_storage but not inline requests."""
        provider, transport = self._provider(transport_config={
            'audio_transport': 'auto', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES})
        # Small file → inline, no storage needed, succeeds.
        provider._transcribe(self.small)
        self.assertEqual([c[0] for c in transport.calls], ['POST'])

        # Large file → object_storage, no adapter → fails clearly.
        provider2, _ = self._provider(transport_config={
            'audio_transport': 'object_storage', 'inline_max_bytes': 1})
        with self.assertRaises(ProviderFailure):
            provider2._transcribe(self.small)

    # --- secret / signed-URL redaction ---

    def test_presigned_url_not_persisted_in_audit(self):
        """The Presigned GET URL must never appear in any audit artifact."""
        adapter, _ = self._tos_adapter()
        provider, _ = self._provider(transport_config={
            'audio_transport': 'object_storage', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        provider._transcribe(self.small)
        for path in (self.root / 'provider-calls').rglob('*.json'):
            content = path.read_text(encoding='utf-8')
            self.assertNotIn('SECRET-PRESIGNED-TOKEN', content,
                             f'Presigned URL leaked into {path.name}')

    def test_request_json_records_transport_choice_without_url(self):
        """request.json carries transport metadata, not the signed URL."""
        adapter, _ = self._tos_adapter()
        provider, _ = self._provider(transport_config={
            'audio_transport': 'object_storage', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        provider._transcribe(self.small)
        request_json = list((self.root / 'provider-calls').glob('*/request.json'))
        self.assertTrue(request_json)
        snapshot = json.loads(request_json[0].read_text())
        self.assertEqual(snapshot['transport'], 'object_storage')
        self.assertEqual(snapshot['audio_transport_config']['audio_transport'], 'object_storage')
        self.assertNotIn('url', json.dumps(snapshot))
        self.assertNotIn('SECRET-PRESIGNED-TOKEN', json.dumps(snapshot))

    def test_transport_proof_records_object_key_and_cleanup(self):
        """Object key, source hash and cleanup status are auditable."""
        adapter, client = self._tos_adapter()
        provider, _ = self._provider(transport_config={
            'audio_transport': 'object_storage', 'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES},
            storage_adapter_factory=lambda root, _a=adapter: _a)
        provider._transcribe(self.small)
        transport_json = list((self.root / 'provider-calls').glob('*/transport.json'))
        proof = json.loads(transport_json[0].read_text())
        self.assertEqual(proof['transport'], 'object_storage')
        self.assertEqual(proof['storage_adapter'], 'tos-test')
        self.assertTrue(proof['object_key'])
        self.assertEqual(proof['source_sha256'],
                         hashlib.sha256(self.small.read_bytes()).hexdigest())
        cleanup_json = list((self.root / 'provider-calls').glob('*/transport-cleanup.json'))
        self.assertTrue(cleanup_json, 'cleanup audit must exist')
        cleanup = json.loads(cleanup_json[0].read_text())
        self.assertEqual(cleanup['status'], 'deleted')


if __name__ == '__main__':
    unittest.main()
