"""Versioned model profiles and capability routes with write-only credentials.

SQLite transactions protect optimistic revisions across worker processes.
Secrets never enter descriptors, Run snapshots or provider error responses.
"""
import copy
import json
import math
import os
import re
import sqlite3
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

CAPABILITIES = {'tts': '语音生成', 'asr': '录音分析语音识别', 'streaming_asr': '主动测试实时语音识别',
                'diarization': '说话人分析', 'judge': '结果分析'}
# File ASR and Streaming ASR are separate purposes on purpose: Recording Analysis
# needs a whole-file recogniser, Active Voice Test needs a session-based one
# (PRD-F016). A single vague "default_asr" would hide that difference.
LEGACY_CAPABILITIES = ('tts', 'asr', 'diarization', 'judge')
PROTOCOLS = {'openai_chat': {'judge'}, 'volcengine_tts': {'tts'},
             'volcengine_asr': {'asr', 'diarization'},
             'volcengine_streaming_asr': {'streaming_asr'},
             'custom_speech': {'tts', 'asr', 'diarization'}}
PARAMETERS = {'temperature', 'max_tokens', 'timeout_seconds', 'voice', 'speed', 'volume', 'pitch',
              'sample_rate', 'format', 'resource_id', 'end_window_size', 'force_to_speech_time',
              'file_mode', 'audio_transport', 'inline_max_bytes', 'object_storage_ref'}
# File ASR transport modes (PRD-F005/F016, Issue #87).  inline submits Base64
# audio.data; object_storage uploads a private TOS object and hands a short-lived
# Presigned GET URL to the provider; auto selects by file size vs inline_max_bytes.
AUDIO_TRANSPORT_MODES = ('inline', 'object_storage', 'auto')
DEFAULT_INLINE_MAX_BYTES = 15728640  # 15 MiB; configurable, not a product constant

@dataclass(frozen=True)
class RunProviders:
    asr: object = field(default=None, repr=False)  # factory(evidence_root), initialized inside stage
    streaming_asr: object = field(default=None, repr=False)
    diarization: object = field(default=None, repr=False)
    judge: object = field(default=None, repr=False)
    tts: object = field(default=None, repr=False)


def adapter_available(profile, role):
    return ((role == 'judge' and profile['protocol'] == 'openai_chat') or
            (role == 'asr' and profile['protocol'] == 'volcengine_asr') or
            (role == 'streaming_asr' and profile['protocol'] == 'volcengine_streaming_asr') or
            (role == 'tts' and profile['protocol'] == 'volcengine_tts') or
            (role == 'diarization' and profile['protocol'] == 'volcengine_asr'))

class SettingsError(ValueError):
    pass

class RevisionConflict(SettingsError):
    pass


def validate_profile(p):
    fields = {'id','name','provider','protocol','model','base_url','credential_env','enabled','capabilities','parameters'}
    if not isinstance(p,dict) or set(p)-fields:
        raise SettingsError('模型包含不支持的字段；密钥须通过独立写入字段保存')
    p=copy.deepcopy(p)
    for key in ('id','name','provider','protocol','model'):
        if not isinstance(p.get(key),str) or not p[key].strip() or len(p[key])>200:
            raise SettingsError('模型名称、服务商、协议和模型 ID 必须填写（最多 200 字符）')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',p['id']):
        raise SettingsError('无效的配置 ID')
    if p['protocol'] not in PROTOCOLS:
        raise SettingsError('不支持的服务协议')
    caps=p.get('capabilities')
    if not isinstance(caps,list) or not caps or any(not isinstance(c,str) or c not in PROTOCOLS[p['protocol']] for c in caps):
        raise SettingsError('模型用途与服务协议不匹配')
    p['enabled']=p.get('enabled',True)
    if type(p['enabled']) is not bool:
        raise SettingsError('启用状态必须是布尔值')
    url=p.get('base_url','')
    if not isinstance(url,str) or len(url)>1000:
        raise SettingsError('无效的服务地址')
    parsed=urlsplit(url)
    allowed_schemes = (('wss',) if p['protocol']=='volcengine_streaming_asr'
                       else ('http','https'))
    if parsed.scheme not in allowed_schemes or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        expected = 'WSS' if p['protocol']=='volcengine_streaming_asr' else 'HTTP(S)'
        raise SettingsError(f'服务地址必须为不含密钥、查询参数或账号密码的 {expected} 地址')
    if parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1','host.docker.internal'):
        raise SettingsError('远程服务必须使用 HTTPS；本地模型可使用 HTTP')
    p['base_url']=url.rstrip('/')
    env=p.get('credential_env','')
    if not isinstance(env,str) or (env and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}',env)):
        raise SettingsError('环境变量名应使用大写字母、数字和下划线')
    p['credential_env']=env
    params=p.get('parameters',{})
    if not isinstance(params,dict) or set(params)-PARAMETERS:
        raise SettingsError('包含不支持的模型参数')
    numeric_limits={'temperature':(0,2),'max_tokens':(1,131072),'timeout_seconds':(1,300),'sample_rate':(8000,96000)}
    if p['protocol']=='volcengine_tts':
        # V3 TTS uses signed integer percentage-like controls.  The service
        # itself remains authoritative for any model-specific restriction.
        numeric_limits.update({'speed':(-50,100),'volume':(-100,100),'pitch':(-100,100)})
    elif p['protocol']=='volcengine_streaming_asr':
        # Documented forced-endpointing bounds (PRD-F016 / docs/24-streaming-asr.md).
        numeric_limits.update({'end_window_size':(300,5000),'force_to_speech_time':(0,10000)})
    else:
        numeric_limits['speed']=(0.25,4)
    for key,limits in numeric_limits.items():
        if key in params and (type(params[key]) not in (int,float) or not math.isfinite(params[key]) or not limits[0]<=params[key]<=limits[1]):
            raise SettingsError('模型数值参数超出范围')
    for key in ('max_tokens','sample_rate','end_window_size','force_to_speech_time'):
        if key in params and type(params[key]) is not int:
            raise SettingsError('输出长度和采样率必须是整数')
    for key in ('voice','format','resource_id'):
        if key in params and (not isinstance(params[key],str) or len(params[key])>200):
            raise SettingsError('无效的语音参数')
    if p['protocol']=='volcengine_tts':
        missing=[key for key in ('resource_id','voice') if not params.get(key,'').strip()]
        if missing:
            raise SettingsError('火山 TTS 必须填写资源 ID 和音色 ID')
        if params.get('format','wav').lower()!='wav':
            raise SettingsError('主动语音测试的火山 TTS 格式必须为 wav')
    if p['protocol']=='volcengine_streaming_asr':
        if not params.get('resource_id','').strip():
            raise SettingsError('火山流式语音识别必须填写资源 ID')
    # File ASR transport policy (PRD-F005/F016, Issue #87).  These belong to the
    # volcengine_asr profile; they are rejected on other protocols so a stray
    # transport parameter cannot silently change a TTS or judge request.
    if p['protocol']=='volcengine_asr':
        file_mode=params.get('file_mode','flash')
        if file_mode!='flash':
            raise SettingsError('当前只支持极速版 File ASR（file_mode=flash）')
        audio_transport=params.get('audio_transport','auto')
        if audio_transport not in AUDIO_TRANSPORT_MODES:
            raise SettingsError('audio_transport 必须为 inline、object_storage 或 auto')
        inline_max_bytes=params.get('inline_max_bytes',DEFAULT_INLINE_MAX_BYTES)
        if type(inline_max_bytes) is not int or not 1<=inline_max_bytes<=60_000_000:
            raise SettingsError('inline_max_bytes 必须是 1–60000000 的整数')
        object_storage_ref=params.get('object_storage_ref','')
        if object_storage_ref and (not isinstance(object_storage_ref,str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',object_storage_ref)):
            raise SettingsError('object_storage_ref 必须是有效的存储适配器 ID')
    elif any(key in params for key in ('file_mode','audio_transport','inline_max_bytes','object_storage_ref')):
        raise SettingsError('File ASR transport 参数只适用于 volcengine_asr 协议')
    p['parameters']=params
    return p


def apply_route_defaults(document):
    """Fill purposes added after this document was written.

    A stored configuration written before Streaming ASR existed has no
    ``streaming_asr`` route. Filling it with ``None`` preserves the meaning of
    every existing route; the caller surfaces which keys were added so the change
    is visible rather than silent.
    """
    routes = document.get('routes')
    if not isinstance(routes, dict):
        routes = {}
    added = [role for role in CAPABILITIES if role not in routes]
    document['routes'] = {role: routes.get(role) for role in CAPABILITIES}
    return document, added


class ModelSettings:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True)
        if os.name!='nt':self.directory.chmod(0o700)
        self.path=self.directory/'credentials.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER, document TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS secrets (id TEXT PRIMARY KEY, value TEXT)')
            db.execute('INSERT OR IGNORE INTO state VALUES(1,0,?)',(json.dumps({'profiles':[],'routes':{c:None for c in CAPABILITIES}}),))
        if os.name!='nt':self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:yield db
        finally:db.close()

    def resolve(self):
        with self.connect() as db:
            db.execute('BEGIN')
            revision,raw=db.execute('SELECT revision,document FROM state WHERE id=1').fetchone()
            secrets=dict(db.execute('SELECT id,value FROM secrets'))
        doc=json.loads(raw)
        doc,added=apply_route_defaults(doc)
        self.routes_added=added
        resolved={}
        for p in doc['profiles']:
            resolved[p['id']]=os.environ.get(p['credential_env'],'') if p['credential_env'] else secrets.get(p['id'],'')
        return {'schema_version':'1.0.0','revision':revision,**doc},resolved

    def describe(self):
        from .config_loaders import resolve_external_config
        external = resolve_external_config()
        if external is not None:
            providers_config, storage_config, source = external
            doc = {'schema_version': '1.0.0', 'revision': None,
                   'profiles': providers_config['profiles'],
                   'routes': providers_config['routes'],
                   'capabilities': CAPABILITIES, 'applies': 'next_run',
                   'config_source': source, 'editable': False}
            if storage_config is not None:
                doc['storage_stores'] = [s['id'] for s in storage_config['stores']]
            else:
                doc['storage_stores'] = []
            keys = {}
            for p in doc['profiles']:
                env = p.get('credential_env', '')
                keys[p['id']] = bool(os.environ.get(env)) if env else False
            for p in doc['profiles']:
                p['credential_configured'] = keys[p['id']]
                p['adapter_status'] = 'available' if any(adapter_available(p, c) for c in p['capabilities']) else 'not_integrated'
                p['adapter_capabilities'] = [c for c in p['capabilities'] if adapter_available(p, c)]
            doc['config_source_note'] = 'Provider/storage configuration is sourced from external files; the SQLite store is read-only during migration.'
            return doc
        doc,keys=self.resolve()
        for p in doc['profiles']:
            p['credential_configured']=bool(keys[p['id']])
            p['adapter_status']='available' if any(adapter_available(p,c) for c in p['capabilities']) else 'not_integrated'
            p['adapter_capabilities']=[c for c in p['capabilities'] if adapter_available(p,c)]
        doc['capabilities']=CAPABILITIES
        doc['applies']='next_run'
        doc['config_source']='sqlite'
        doc['editable']=True
        if getattr(self,'routes_added',None):
            # Never migrate silently: report which purposes were defaulted.
            doc['routes_defaulted']=list(self.routes_added)
            doc['routes_defaulted_note']='新增用途按“未配置”填入，不改变既有用途的选择'
        return doc

    def update(self,payload):
        from .config_loaders import resolve_external_config
        if resolve_external_config() is not None:
            raise SettingsError('外置配置文件已激活，SQLite 设置为只读；请编辑外置文件或移除激活')
        if not isinstance(payload,dict) or set(payload)-{'expected_revision','profiles','routes','secrets'}:
            raise SettingsError('无效的模型设置请求')
        profiles=payload.get('profiles')
        if not isinstance(profiles,list) or len(profiles)>50:
            raise SettingsError('最多可配置 50 个模型')
        profiles=[validate_profile(p) for p in profiles]
        by_id={p['id']:p for p in profiles}
        if len(by_id)!=len(profiles):raise SettingsError('配置 ID 重复')
        routes=payload.get('routes')
        if not isinstance(routes,dict) or set(routes)-set(CAPABILITIES):
            raise SettingsError('必须明确配置各类用途')
        missing=[role for role in LEGACY_CAPABILITIES if role not in routes]
        if missing:
            raise SettingsError('必须明确配置四类用途')
        # A purpose added later may be omitted; it is stored as unconfigured
        # rather than defaulting to some existing profile.
        routes={role: routes.get(role) for role in CAPABILITIES}
        for role,selected in routes.items():
            if selected is not None and (not isinstance(selected,str) or selected not in by_id or not by_id[selected]['enabled'] or role not in by_id[selected]['capabilities']):
                raise SettingsError('默认模型不存在、未启用或不支持对应用途')
        secrets=payload.get('secrets',{})
        if not isinstance(secrets,dict) or set(secrets)-set(by_id) or any(v is not None and (not isinstance(v,str) or not v.strip() or len(v)>8192) for v in secrets.values()):
            raise SettingsError('无效的密钥更新')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            revision=db.execute('SELECT revision FROM state WHERE id=1').fetchone()[0]
            if type(payload.get('expected_revision')) is not int or payload['expected_revision']!=revision:
                raise RevisionConflict('配置已被其他页面修改，请刷新后重试')
            db.execute('UPDATE state SET revision=?,document=? WHERE id=1',(revision+1,json.dumps({'profiles':profiles,'routes':routes},ensure_ascii=False)))
            for key,value in secrets.items():
                if value is None:db.execute('DELETE FROM secrets WHERE id=?',(key,))
                else:db.execute('INSERT OR REPLACE INTO secrets VALUES(?,?)',(key,value))
            for (key,) in db.execute('SELECT id FROM secrets').fetchall():
                if key not in by_id:db.execute('DELETE FROM secrets WHERE id=?',(key,))
        return self.describe()

    def capture(self):
        """Resolve one immutable Run configuration and credentials once at Run start.

        External provider/storage configuration (providers.yaml / storage.yaml)
        is the target source of truth (PRD-F015, Issue #87).  When it is active
        it takes precedence over the SQLite store.  The two sources are never
        silently merged: if both carry substantive configuration the caller is
        told which is active and the inactive one is read-only.
        """
        from .config_loaders import resolve_external_config
        external = resolve_external_config()
        if external is not None:
            providers_config, storage_config, source = external
            # Migration boundary: if the SQLite store also carries saved profiles,
            # that is a bounded-migration conflict, not a silent merge.
            sqlite_doc = self.resolve()[0]
            sqlite_has_profiles = bool(sqlite_doc['profiles'])
            if sqlite_has_profiles:
                raise SettingsError(
                    'SQLite 模型设置与外置配置文件同时存在；请清空 SQLite 设置或移除外置文件，'
                    '不要静默合并两个来源 (PRD-F015 migration)')
            from .config_loaders import resolve_credentials
            keys = resolve_credentials(providers_config)
            doc, providers = build_run_providers(providers_config, keys, storage_config)
            doc['config_source'] = source
            return doc, providers
        doc, keys = self.resolve()
        doc, providers = build_run_providers(doc, keys)
        doc['config_source'] = 'sqlite'
        return doc, providers


def _store_to_config(store):
    """Convert a validated storage store dict into a TOSStorageConfig."""
    from .tos_adapter import TOSStorageConfig
    pub = store['publication']
    creds = store['credentials']
    return TOSStorageConfig(
        id=store['id'], endpoint=store['endpoint'], region=store['region'],
        bucket=store['bucket'], prefix=store['prefix'],
        access_key_env=creds['access_key_env'], secret_key_env=creds['secret_key_env'],
        session_token_env=creds['session_token_env'],
        presigned_get_ttl_seconds=pub['presigned_get_ttl_seconds'],
        delete_after_use=pub['delete_after_use'],
        lifecycle_max_age_hours=pub['lifecycle_max_age_hours'])


def build_run_providers(doc, keys, storage_config=None):
    """Build RunProviders factory closures from a resolved configuration.

    Shared by the SQLite path (``storage_config=None`` → legacy publication)
    and the external-config path (``storage_config`` provides TOS adapters).

    ``doc`` is a resolved configuration dict with ``profiles`` and ``routes``.
    ``keys`` maps profile id → resolved credential value.  The returned ``doc``
    carries only non-secret provenance; credentials and signed URLs never enter
    the snapshot.
    """
    by_id={p['id']:p for p in doc['profiles']}
    from .llm import UnavailableLLMProvider
    from .llm_provider import OpenAICompatibleProvider
    provider=UnavailableLLMProvider()
    selected=by_id.get(doc['routes']['judge'])
    if selected and selected['enabled'] and selected['protocol']=='openai_chat':
        params=selected['parameters']
        provider=OpenAICompatibleProvider(selected['provider'],selected['base_url'],selected['model'],
            api_key=keys[selected['id']],api_key_env=selected['credential_env'],
            temperature=params.get('temperature',0.3),max_tokens=params.get('max_tokens',4096),
            timeout_seconds=params.get('timeout_seconds',30))
    # Presence and adapter readiness are evidence, not a network connectivity claim.
    doc['readiness']={role:('not_configured' if not by_id.get(route) else
        'not_integrated' if not adapter_available(by_id[route], role) else
        'configured' if keys[route] else 'credential_missing') for role,route in doc['routes'].items()}
    asr_factory=None
    streaming_asr_factory=None
    diarization_factory=None
    tts_factory=None
    asr=by_id.get(doc['routes']['asr'])
    if asr and asr['enabled'] and adapter_available(asr, 'asr'):
        from .volcengine_asr import VolcengineASRProvider
        _asr_profile=asr
        _asr_key=keys[asr['id']]
        _asr_params=asr['parameters']
        _transport_config={
            'audio_transport':_asr_params.get('audio_transport','auto'),
            'inline_max_bytes':_asr_params.get('inline_max_bytes',DEFAULT_INLINE_MAX_BYTES)}
        _storage_ref=_asr_params.get('object_storage_ref','')
        _storage_stores={}
        if storage_config is not None:
            for store in storage_config['stores']:
                _storage_stores[store['id']]=store
        _legacy_keys=('AIVOICEBENCH_AUDIO_PUT_URL','AIVOICEBENCH_AUDIO_GET_URL','AIVOICEBENCH_AUDIO_HOST')
        def asr_factory(root, _profile=_asr_profile, _key=_asr_key, _params=_asr_params,
                        _transport=_transport_config, _ref=_storage_ref, _stores=_storage_stores,
                        _legacy=_legacy_keys):
            from .cloud_transport import SignedURLPublication
            storage_adapter=None
            if _ref and _ref in _stores:
                from .tos_adapter import TOSStorageAdapter
                _store_cfg=_store_to_config(_stores[_ref])
                storage_adapter=lambda root, _cfg=_store_cfg: TOSStorageAdapter(_cfg)
            # Legacy migration fallback: when no storage adapter is configured and
            # the fixed signed-URL env vars are set, use SignedURLPublication for
            # object-storage transport.  This is no longer the production path
            # (Issue #87) but keeps existing deployments working during migration.
            publication=None
            if storage_adapter is None and all(os.environ.get(k) for k in _legacy):
                publication=lambda _cfg=tuple(os.environ.get(k,'') for k in _legacy): \
                    SignedURLPublication(*_cfg)
            return VolcengineASRProvider(root, _key, model=_profile['model'],
                endpoint=_profile['base_url'], resource_id=_params.get('resource_id','volc.bigasr.auc_turbo'),
                timeout=_params.get('timeout_seconds',300),
                transport_config=_transport, storage_adapter_factory=storage_adapter,
                publication=publication)
        doc['asr_transport_config']=dict(_transport_config)
        doc['asr_object_storage_ref']=_storage_ref or None
        # Legacy publication variables are no longer required in the production
        # path (PRD-F005, Issue #87).  Report whether they remain set only for
        # migration diagnostics; they do not gate inline transport.
        doc['asr_legacy_publication_configured']=all(bool(os.environ.get(k)) for k in
            ('AIVOICEBENCH_AUDIO_PUT_URL','AIVOICEBENCH_AUDIO_GET_URL','AIVOICEBENCH_AUDIO_HOST'))
        # Diarization reuses the same configured ASR profile: the cloud call
        # already returns speaker labels, so no separate endpoint or second
        # recognition submission is required.
        diarization=by_id.get(doc['routes']['diarization'])
        if diarization and diarization['enabled'] and diarization['protocol']=='volcengine_asr':
            from .diarization import ASRNativeDiarizationProvider
            def diarization_factory(root, _dia=diarization):
                return ASRNativeDiarizationProvider(
                    provider_name=_dia['provider'] or 'volcengine',
                    model=_dia['model'] or 'bigmodel',
                    resource_id=_dia['parameters'].get('resource_id','volc.bigasr.auc_turbo'))
    # Streaming ASR for Active Voice Test. Separate from the file ASR route:
    # different lifecycle, different evidence class (PRD-F016).
    streaming=by_id.get(doc['routes'].get('streaming_asr'))
    if streaming and streaming['enabled'] and adapter_available(streaming, 'streaming_asr'):
        from .volcengine_streaming_asr import VolcengineStreamingASRProvider
        _stream_profile=streaming
        _stream_key=keys[streaming['id']]
        def streaming_asr_factory(root, _sp=_stream_profile, _sk=_stream_key):
            params=_sp['parameters']
            return VolcengineStreamingASRProvider(
                root, _sk,
                endpoint=_sp['base_url'] or
                'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel',
                resource_id=params.get('resource_id','volc.bigasr.sauc.duration'),
                model=_sp['model'] or 'bigmodel',
                timeout=params.get('timeout_seconds',300),
                end_window_size=params.get('end_window_size',800),
                force_to_speech_time=params.get('force_to_speech_time',1000))
    # TTS V3 SSE for Active Voice Test.  Profile parameters intentionally
    # carry current account-specific resource/voice identifiers rather than
    # code defaults; credentials remain in the secret store/environment.
    tts=by_id.get(doc['routes']['tts'])
    if tts and tts['enabled'] and adapter_available(tts, 'tts'):
        from .volcengine_tts import VolcengineTTSProvider
        _tts_profile=tts
        _tts_key=keys[tts['id']]
        def tts_factory(root, _tp=_tts_profile, _tk=_tts_key):
            params=_tp['parameters']
            return VolcengineTTSProvider(root, _tk,
                endpoint=_tp['base_url'],
                model=_tp['model'],
                resource_id=params['resource_id'],
                voice_type=params['voice'],
                speech_rate=params.get('speed',0),
                loudness_rate=params.get('volume',0),
                pitch_rate=params.get('pitch',0),
                audio_format=params.get('format','wav'),
                sample_rate=params.get('sample_rate',16000),
                timeout=params.get('timeout_seconds',30))
    if doc.get('revision')==0:
        doc['legacy_environment']={'provider':os.environ.get('AIVOICEBENCH_LLM_PROVIDER','none'),
            'model':os.environ.get('AIVOICEBENCH_LLM_MODEL',''),
            'note':'Existing environment configuration applies until settings are first saved'}
        provider=None
    return doc,RunProviders(asr=asr_factory, tts=tts_factory, diarization=diarization_factory,
                            streaming_asr=streaming_asr_factory, judge=provider)
