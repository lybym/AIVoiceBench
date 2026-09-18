"""Tests for external provider/storage configuration loaders (Issue #87).

Covers YAML schema validation, missing/unreadable files, invalid profiles,
missing credential env, transport threshold selection, storage cross-reference,
secret redaction, and SQLite migration/conflict rules.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from aivoicebench.cloud_transport import HTTPReply
from aivoicebench.config_loaders import (load_providers_config, load_storage_config,
    resolve_credentials, resolve_external_config, resolve_runtime,
    PROVIDERS_SCHEMA_VERSION, STORAGE_SCHEMA_VERSION)
from aivoicebench.model_settings import ModelSettings, SettingsError, DEFAULT_INLINE_MAX_BYTES
from aivoicebench.providers import ProviderFailure
from aivoicebench.tos_adapter import validate_storage_store, TOSStorageConfig
from aivoicebench.volcengine_asr import STANDARD_QUERY_API, STANDARD_SUBMIT_API

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


def _seed_standard_providers_yaml():
    """A providers.yaml whose File ASR profile is the Seed ASR 2.0 standard contract."""
    data = _providers_yaml()
    profile = next(p for p in data['profiles'] if p['id'] == 'volc-file-asr')
    profile['name'] = 'Doubao Seed ASR 2.0 Standard'
    profile['base_url'] = STANDARD_SUBMIT_API
    profile['parameters'].update({'resource_id': 'volc.seedasr.auc',
                                  'file_mode': 'seed_standard',
                                  'audio_transport': 'inline'})
    profile['parameters'].pop('object_storage_ref', None)
    return data


def _canonical_wav(path):
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\x00\x00' * 1600)


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

    def test_divergent_asr_and_diarization_routes_rejected(self):
        """``providers.yaml`` may not split the two routes (PRD-F006, Issue #22).

        Speaker labels come out of the File ASR request, so a diarization route that
        points elsewhere would publish a contract status for a call that never ran.
        Both directions are refused, and the error names both profiles.
        """
        def file_asr(pid, endpoint, resource_id, file_mode):
            return {'id': pid, 'name': pid, 'provider': 'volcengine',
                    'protocol': 'volcengine_asr', 'model': 'bigmodel', 'base_url': endpoint,
                    'enabled': True, 'credential_env': 'VOLCENGINE_SPEECH_API_KEY',
                    'capabilities': ['asr', 'diarization'],
                    'parameters': {'resource_id': resource_id, 'file_mode': file_mode}}
        flash = file_asr('volc-flash', 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash',
                         'volc.bigasr.auc_turbo', 'flash')
        seed = file_asr('volc-seed', STANDARD_SUBMIT_API, 'volc.seedasr.auc', 'seed_standard')
        for asr_id, diarization_id in (('volc-flash', 'volc-seed'), ('volc-seed', 'volc-flash')):
            with self.subTest(asr=asr_id, diarization=diarization_id):
                data = _providers_yaml()
                data['profiles'] = [data['profiles'][0], flash, seed]
                data['routes'] = {'tts': None, 'asr': asr_id, 'streaming_asr': None,
                                  'diarization': diarization_id, 'judge': 'judge'}
                _write_yaml(self.providers_path, data)
                with self.assertRaises(ValueError) as caught:
                    load_providers_config(self.providers_path)
                self.assertIn(asr_id, str(caught.exception))
                self.assertIn(diarization_id, str(caught.exception))
        # The same profile on both routes still loads.
        data = _providers_yaml()
        data['profiles'] = [data['profiles'][0], seed]
        data['routes'] = {'tts': None, 'asr': 'volc-seed', 'streaming_asr': None,
                          'diarization': 'volc-seed', 'judge': 'judge'}
        _write_yaml(self.providers_path, data)
        self.assertEqual(load_providers_config(self.providers_path)['routes']['diarization'], 'volc-seed')

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


class SeedStandardProfileResolutionTests(unittest.TestCase):
    """Issue #22 items 1 and 4: the Seed standard path is asserted, not just documented.

    The profile is resolved through the real external-config path
    (``ModelSettings.capture()`` with an external ``providers.yaml``) and the
    resulting ASR provider runs against a scripted transport: no network, no real
    credentials.
    """

    def setUp(self):
        self.tmp_dir = _mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.root = Path(self.tmp_dir)
        self.providers_path = self.root / 'providers.yaml'
        self.store = ModelSettings(self.root / 'settings')
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.audio = self.evidence / 'input.wav'
        _canonical_wav(self.audio)
        self.success = json.dumps({'result': {'text': '你好', 'utterances': [
            {'text': '你好', 'start_time': 0, 'end_time': 100, 'words': [],
             'additions': {'speaker': '1'}}]}}, ensure_ascii=False).encode()

    def _external_env(self):
        return patch.dict(os.environ, {
            'AIVOICEBENCH_PROVIDERS_CONFIG': str(self.providers_path),
            'AIVOICEBENCH_STORAGE_CONFIG': str(self.root / 'absent-storage.yaml'),
            'TEST_JUDGE_KEY': '', 'VOLCENGINE_SPEECH_API_KEY': 'test-api-key'})

    def test_external_seed_standard_profile_runs_in_standard_mode(self):
        _write_yaml(self.providers_path, _seed_standard_providers_yaml())
        calls = []
        replies = iter([HTTPReply(200, {'x-api-status-code': '20000000'}, b'{}'),
                        HTTPReply(200, {'x-api-status-code': '20000000'}, self.success)])

        def scripted_request(method, url, headers, body=None, **kwargs):
            calls.append((method, url, headers, body))
            return next(replies)

        with self._external_env():
            snapshot, providers = self.store.capture()
            self.assertTrue(snapshot['config_source'].startswith('external:'))
            self.assertIsNotNone(providers.asr)
            with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                       side_effect=scripted_request):
                provider = providers.asr(self.root / 'evidence')
                output = provider.transcribe(self.audio)

        self.assertEqual(provider.mode, 'seed_standard')
        self.assertEqual([call[1] for call in calls], [STANDARD_SUBMIT_API, STANDARD_QUERY_API])
        request_body = json.loads(calls[0][3])
        self.assertIs(request_body['request']['enable_speaker_info'], True)
        self.assertEqual(calls[0][2]['X-Api-Resource-Id'], 'volc.seedasr.auc')
        self.assertIn('enable_speaker_info', output.profile['interface_contract_verified'])
        self.assertEqual(output.profile['capability_contract_pending']
                         ['speaker_separation']['interface_contract'], 'verified')
        self.assertNotIn('test-api-key', json.dumps(snapshot))

    def test_file_mode_contradicting_the_endpoint_fails_closed(self):
        """A declared file_mode that the endpoint cannot honour is refused, not inferred."""
        data = _seed_standard_providers_yaml()
        profile = next(p for p in data['profiles'] if p['id'] == 'volc-file-asr')
        profile['base_url'] = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'
        profile['parameters']['resource_id'] = 'volc.bigasr.auc_turbo'
        # file_mode stays seed_standard while the endpoint is the flash contract.
        _write_yaml(self.providers_path, data)

        with self._external_env():
            _snapshot, providers = self.store.capture()
            with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                       side_effect=AssertionError('a refused contract must not send a request')):
                with self.assertRaises(ProviderFailure):
                    providers.asr(self.root / 'evidence')

    def test_file_mode_contradiction_reaches_the_run_stage_reason(self):
        """A configuration error must stay readable in the Run, not be genericised away.

        The cause is raised while the ASR provider is constructed inside the stage
        operation.  Every stage reason used to be rewritten to
        ``'ProviderFailure: processor failed; retained local artifacts'``, which
        described the *type* of failure and hid the contradiction the operator has
        to fix.  No request is built and nothing is billed.
        """
        from aivoicebench.import_pipeline import import_recording
        from aivoicebench.model_settings import RunProviders

        data = _seed_standard_providers_yaml()
        profile = next(p for p in data['profiles'] if p['id'] == 'volc-file-asr')
        profile['base_url'] = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash'
        profile['parameters']['resource_id'] = 'volc.bigasr.auc_turbo'  # file_mode stays seed_standard
        _write_yaml(self.providers_path, data)

        with self._external_env():
            snapshot, providers = self.store.capture()
            _directory, manifest = import_recording(
                self.audio, self.root / 'runs', synthetic=True, model_snapshot=snapshot,
                providers=RunProviders(asr=providers.asr, diarization=providers.diarization))

        stage = manifest['stages']['asr']
        self.assertEqual(stage['status'], 'failed')
        self.assertNotIn('processor failed', stage['reason'])
        self.assertIn('file_mode=seed_standard', stage['reason'])
        self.assertIn('flash', stage['reason'])
        self.assertEqual(list((self.root / 'runs').rglob('provider-calls/CALL-*')), [],
                         'a contract refused before the request must not file an invocation')
        # The report is still produced, so the reason is inspectable in the Run.
        self.assertTrue((_directory / 'analysis' / manifest['analysis_id'] / 'report.md').is_file())


if __name__ == '__main__':
    unittest.main()
