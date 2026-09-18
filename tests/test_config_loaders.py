"""Tests for external provider/storage configuration loaders (Issue #87).

Covers YAML schema validation, missing/unreadable files, invalid profiles,
missing credential env, transport threshold selection, storage cross-reference,
secret redaction, and SQLite migration/conflict rules.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aivoicebench.config_loaders import (load_providers_config, load_storage_config,
    resolve_credentials, resolve_external_config, resolve_runtime,
    PROVIDERS_SCHEMA_VERSION, STORAGE_SCHEMA_VERSION)
from aivoicebench.model_settings import ModelSettings, SettingsError, DEFAULT_INLINE_MAX_BYTES
from aivoicebench.tos_adapter import validate_storage_store, TOSStorageConfig

# Workspace-rooted temp so the file sandbox permits writes.
_TMP_ROOT = Path(__file__).resolve().parent.parent / '.test-tmp'
_TMP_ROOT.mkdir(exist_ok=True)


def _mkdtemp():
    """Create a temp dir without tempfile.mkdtemp's restrictive chmod."""
    import uuid
    d = _TMP_ROOT / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _providers_yaml(profiles='all', routes=None):
    base = {
        'judge': {'id': 'judge', 'name': 'Test Judge', 'provider': 'custom',
                  'protocol': 'openai_chat', 'model': 'test-model',
                  'base_url': 'https://example.com/v1', 'enabled': True,
                  'credential_env': 'TEST_JUDGE_KEY', 'capabilities': ['judge'],
                  'parameters': {'temperature': 0.2, 'timeout_seconds': 15}},
        'asr': {'id': 'volc-file-asr', 'name': 'File ASR', 'provider': 'volcengine',
                'protocol': 'volcengine_asr', 'model': 'bigmodel',
                'base_url': 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash',
                'enabled': True, 'credential_env': 'VOLCENGINE_SPEECH_API_KEY',
                'capabilities': ['asr', 'diarization'],
                'parameters': {'resource_id': 'volc.bigasr.auc_turbo',
                               'timeout_seconds': 300, 'file_mode': 'flash',
                               'audio_transport': 'auto',
                               'inline_max_bytes': DEFAULT_INLINE_MAX_BYTES,
                               'object_storage_ref': 'tos-asr-temp'}},
    }
    selected = list(base.values()) if profiles == 'all' else [base[p] for p in profiles]
    default_routes = {'tts': None, 'asr': 'volc-file-asr', 'streaming_asr': None,
                      'diarization': 'volc-file-asr', 'judge': 'judge'}
    return {'schema_version': PROVIDERS_SCHEMA_VERSION, 'profiles': selected,
            'routes': routes or default_routes}


def _storage_yaml():
    return {'schema_version': STORAGE_SCHEMA_VERSION, 'stores': [{
        'id': 'tos-asr-temp', 'provider': 'tos',
        'endpoint': 'https://tos-cn-beijing.volces.com', 'region': 'cn-beijing',
        'bucket': 'private-bucket', 'prefix': 'aivoicebench/asr/',
        'credentials': {'access_key_env': 'TOS_ACCESS_KEY',
                        'secret_key_env': 'TOS_SECRET_KEY',
                        'session_token_env': 'TOS_SESSION_TOKEN'},
        'publication': {'presigned_get_ttl_seconds': 3600,
                        'delete_after_use': True, 'lifecycle_max_age_hours': 24}}]}


def _write_yaml(path, data):
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')


class ConfigLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = _mkdtemp()
        self.root = Path(self.tmp_dir)
        self.providers_path = self.root / 'providers.yaml'
        self.storage_path = self.root / 'storage.yaml'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_valid_providers_config_loads(self):
        _write_yaml(self.providers_path, _providers_yaml())
        config = load_providers_config(self.providers_path)
        self.assertEqual(config['schema_version'], PROVIDERS_SCHEMA_VERSION)
        self.assertEqual(len(config['profiles']), 2)
        self.assertEqual(config['routes']['asr'], 'volc-file-asr')

    def test_valid_storage_config_loads(self):
        _write_yaml(self.storage_path, _storage_yaml())
        config = load_storage_config(self.storage_path)
        self.assertEqual(config['schema_version'], STORAGE_SCHEMA_VERSION)
        self.assertEqual(len(config['stores']), 1)
        store = config['stores'][0]
        self.assertEqual(store['provider'], 'tos')
        self.assertEqual(store['publication']['presigned_get_ttl_seconds'], 3600)

    def test_missing_providers_file_returns_none(self):
        self.assertIsNone(resolve_external_config(self.root / 'nonexistent.yaml'))

    def test_missing_storage_file_allows_inline_transport(self):
        _write_yaml(self.providers_path, _providers_yaml())
        external = resolve_external_config(self.providers_path, self.root / 'nonexistent.yaml')
        self.assertIsNotNone(external)
        providers_config, storage_config, source = external
        self.assertIsNone(storage_config)
        self.assertIn('providers.yaml', source)

    def test_invalid_schema_version_rejected(self):
        data = _providers_yaml()
        data['schema_version'] = 99
        _write_yaml(self.providers_path, data)
        with self.assertRaises(ValueError):
            load_providers_config(self.providers_path)

    def test_unsupported_field_rejected(self):
        data = _providers_yaml()
        data['extra_field'] = 'bad'
        _write_yaml(self.providers_path, data)
        with self.assertRaises(ValueError):
            load_providers_config(self.providers_path)

    def test_invalid_audio_transport_rejected(self):
        data = _providers_yaml()
        data['profiles'][1]['parameters']['audio_transport'] = 'telepathy'
        _write_yaml(self.providers_path, data)
        with self.assertRaises(SettingsError):
            load_providers_config(self.providers_path)

    def test_transport_params_rejected_on_non_asr_protocol(self):
        data = _providers_yaml()
        data['profiles'][0]['parameters']['audio_transport'] = 'auto'
        _write_yaml(self.providers_path, data)
        with self.assertRaises(SettingsError):
            load_providers_config(self.providers_path)

    def test_inline_max_bytes_must_be_positive_int(self):
        data = _providers_yaml()
        data['profiles'][1]['parameters']['inline_max_bytes'] = 0
        _write_yaml(self.providers_path, data)
        with self.assertRaises(SettingsError):
            load_providers_config(self.providers_path)

    def test_object_storage_ref_cross_reference_validated(self):
        _write_yaml(self.providers_path, _providers_yaml())
        _write_yaml(self.storage_path, _storage_yaml())
        external = resolve_external_config(self.providers_path, self.storage_path)
        self.assertIsNotNone(external)
        # Now reference a non-existent store
        data = _providers_yaml()
        data['profiles'][1]['parameters']['object_storage_ref'] = 'missing-store'
        _write_yaml(self.providers_path, data)
        with self.assertRaises(ValueError):
            resolve_external_config(self.providers_path, self.storage_path)

    def test_credential_secret_value_rejected_in_storage(self):
        store = _storage_yaml()['stores'][0]
        store['credentials']['access_key_env'] = 'AKIA-SECRET-VALUE'
        with self.assertRaises(ValueError):
            validate_storage_store(store)

    def test_credential_env_reference_accepted(self):
        store = _storage_yaml()['stores'][0]
        validated = validate_storage_store(store)
        self.assertEqual(validated['credentials']['access_key_env'], 'TOS_ACCESS_KEY')

    def test_non_tos_provider_rejected(self):
        store = _storage_yaml()['stores'][0]
        store['provider'] = 's3'
        with self.assertRaises(ValueError):
            validate_storage_store(store)

    def test_ttl_range_validated(self):
        for ttl in (59, 86401, 'abc'):
            store = _storage_yaml()['stores'][0]
            store['publication']['presigned_get_ttl_seconds'] = ttl
            with self.assertRaises(ValueError):
                validate_storage_store(store)

    def test_resolve_credentials_from_env(self):
        _write_yaml(self.providers_path, _providers_yaml())
        config = load_providers_config(self.providers_path)
        with patch.dict(os.environ, {'TEST_JUDGE_KEY': 'judge-secret',
                                      'VOLCENGINE_SPEECH_API_KEY': 'asr-secret'}):
            keys = resolve_credentials(config)
            self.assertEqual(keys['judge'], 'judge-secret')
            self.assertEqual(keys['volc-file-asr'], 'asr-secret')

    def test_snapshot_has_no_secret_values(self):
        _write_yaml(self.providers_path, _providers_yaml())
        _write_yaml(self.storage_path, _storage_yaml())
        with patch.dict(os.environ, {'TEST_JUDGE_KEY': 'secret-canary',
                                      'VOLCENGINE_SPEECH_API_KEY': 'asr-canary',
                                      'TOS_ACCESS_KEY': 'tos-canary',
                                      'TOS_SECRET_KEY': 'tos-secret-canary'}):
            doc, providers = resolve_runtime(self.providers_path, self.storage_path)
            serialized = json.dumps(doc)
            for secret in ('secret-canary', 'asr-canary', 'tos-canary', 'tos-secret-canary'):
                self.assertNotIn(secret, serialized)
            self.assertEqual(doc['config_source'], f'external:{self.providers_path}')
            self.assertIn('asr_transport_config', doc)
            self.assertEqual(doc['asr_transport_config']['audio_transport'], 'auto')
            self.assertEqual(doc['asr_object_storage_ref'], 'tos-asr-temp')

    def test_transport_config_and_storage_ref_in_snapshot(self):
        _write_yaml(self.providers_path, _providers_yaml())
        _write_yaml(self.storage_path, _storage_yaml())
        with patch.dict(os.environ, {'TEST_JUDGE_KEY': '', 'VOLCENGINE_SPEECH_API_KEY': ''}):
            doc, _ = resolve_runtime(self.providers_path, self.storage_path)
            self.assertEqual(doc['asr_transport_config']['inline_max_bytes'], DEFAULT_INLINE_MAX_BYTES)
            self.assertEqual(doc['asr_transport_config']['audio_transport'], 'auto')
            self.assertEqual(doc['asr_object_storage_ref'], 'tos-asr-temp')
            self.assertFalse(doc['asr_legacy_publication_configured'])


class MigrationConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = _mkdtemp()
        self.root = Path(self.tmp_dir)
        self.store = ModelSettings(self.root)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_sqlite_alone_still_works(self):
        """SQLite path is active when no external config is present."""
        from aivoicebench.model_settings import build_run_providers
        doc, _ = self.store.capture()
        self.assertEqual(doc['config_source'], 'sqlite')

    def test_external_config_takes_precedence(self):
        """External config is the source of truth when its file exists."""
        providers_path = self.root / 'providers.yaml'
        storage_path = self.root / 'storage.yaml'
        _write_yaml(providers_path, _providers_yaml())
        _write_yaml(storage_path, _storage_yaml())
        with patch.dict(os.environ, {'AIVOICEBENCH_PROVIDERS_CONFIG': str(providers_path),
                                      'AIVOICEBENCH_STORAGE_CONFIG': str(storage_path),
                                      'TEST_JUDGE_KEY': '', 'VOLCENGINE_SPEECH_API_KEY': ''}):
            doc, _ = self.store.capture()
            self.assertTrue(doc['config_source'].startswith('external:'))

    def test_conflict_when_both_sqlite_and_external_active(self):
        """SQLite profiles + external file = explicit conflict, never silent merge."""
        from tests.test_model_settings import profile, payload
        self.store.update(payload())
        providers_path = self.root / 'providers.yaml'
        _write_yaml(providers_path, _providers_yaml())
        with patch.dict(os.environ, {'AIVOICEBENCH_PROVIDERS_CONFIG': str(providers_path),
                                      'AIVOICEBENCH_STORAGE_CONFIG': ''}):
            with self.assertRaises(SettingsError):
                self.store.capture()

    def test_update_blocked_when_external_active(self):
        """SQLite writes are read-only when external config is active."""
        providers_path = self.root / 'providers.yaml'
        _write_yaml(providers_path, _providers_yaml())
        with patch.dict(os.environ, {'AIVOICEBENCH_PROVIDERS_CONFIG': str(providers_path)}):
            from tests.test_model_settings import profile, payload
            with self.assertRaises(SettingsError):
                self.store.update(payload())

    def test_describe_reports_source(self):
        """describe() identifies the active configuration source."""
        providers_path = self.root / 'providers.yaml'
        _write_yaml(providers_path, _providers_yaml())
        with patch.dict(os.environ, {'AIVOICEBENCH_PROVIDERS_CONFIG': str(providers_path),
                                      'TEST_JUDGE_KEY': 'k', 'VOLCENGINE_SPEECH_API_KEY': 'k'}):
            desc = self.store.describe()
            self.assertTrue(desc['config_source'].startswith('external:'))
            self.assertFalse(desc['editable'])
            self.assertIn('storage_stores', desc)


if __name__ == '__main__':
    unittest.main()
