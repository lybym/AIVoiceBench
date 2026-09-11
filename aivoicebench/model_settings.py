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

CAPABILITIES = {'tts': '语音生成', 'asr': '语音识别', 'diarization': '说话人分析', 'judge': '结果分析'}
PROTOCOLS = {'openai_chat': {'judge'}, 'volcengine_tts': {'tts'},
             'volcengine_asr': {'asr', 'diarization'}, 'custom_speech': {'tts', 'asr', 'diarization'}}
PARAMETERS = {'temperature', 'max_tokens', 'timeout_seconds', 'voice', 'speed', 'volume', 'pitch', 'sample_rate', 'format', 'resource_id'}

@dataclass(frozen=True)
class RunProviders:
    asr: object = field(default=None, repr=False)  # factory(evidence_root), initialized inside stage
    diarization: object = field(default=None, repr=False)
    judge: object = field(default=None, repr=False)
    tts: object = field(default=None, repr=False)


def adapter_available(profile, role):
    return ((role == 'judge' and profile['protocol'] == 'openai_chat') or
            (role == 'asr' and profile['protocol'] == 'volcengine_asr') or
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
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SettingsError('服务地址必须为不含密钥、查询参数或账号密码的 HTTP(S) 地址')
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
    else:
        numeric_limits['speed']=(0.25,4)
    for key,limits in numeric_limits.items():
        if key in params and (type(params[key]) not in (int,float) or not math.isfinite(params[key]) or not limits[0]<=params[key]<=limits[1]):
            raise SettingsError('模型数值参数超出范围')
    for key in ('max_tokens','sample_rate'):
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
    p['parameters']=params
    return p


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
        resolved={}
        for p in doc['profiles']:
            resolved[p['id']]=os.environ.get(p['credential_env'],'') if p['credential_env'] else secrets.get(p['id'],'')
        return {'schema_version':'1.0.0','revision':revision,**doc},resolved

    def describe(self):
        doc,keys=self.resolve()
        for p in doc['profiles']:
            p['credential_configured']=bool(keys[p['id']])
            p['adapter_status']='available' if any(adapter_available(p,c) for c in p['capabilities']) else 'not_integrated'
            p['adapter_capabilities']=[c for c in p['capabilities'] if adapter_available(p,c)]
        doc['capabilities']=CAPABILITIES
        doc['applies']='next_run'
        return doc

    def update(self,payload):
        if not isinstance(payload,dict) or set(payload)-{'expected_revision','profiles','routes','secrets'}:
            raise SettingsError('无效的模型设置请求')
        profiles=payload.get('profiles')
        if not isinstance(profiles,list) or len(profiles)>50:
            raise SettingsError('最多可配置 50 个模型')
        profiles=[validate_profile(p) for p in profiles]
        by_id={p['id']:p for p in profiles}
        if len(by_id)!=len(profiles):raise SettingsError('配置 ID 重复')
        routes=payload.get('routes')
        if not isinstance(routes,dict) or set(routes)!=set(CAPABILITIES):raise SettingsError('必须明确配置四类用途')
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
        """Resolve one immutable Run configuration and credentials once at Run start."""
        doc,keys=self.resolve()
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
        diarization_factory=None
        tts_factory=None
        asr=by_id.get(doc['routes']['asr'])
        if asr and asr['enabled'] and adapter_available(asr, 'asr'):
            from .volcengine_asr import VolcengineASRProvider
            publication_config=tuple(os.environ.get(k,'') for k in ('AIVOICEBENCH_AUDIO_PUT_URL','AIVOICEBENCH_AUDIO_GET_URL','AIVOICEBENCH_AUDIO_HOST'))
            def asr_factory(root):
                from .cloud_transport import SignedURLPublication
                return VolcengineASRProvider(root, keys[asr['id']], model=asr['model'],
                    endpoint=asr['base_url'], resource_id=asr['parameters'].get('resource_id','volc.bigasr.auc_turbo'),
                    timeout=asr['parameters'].get('timeout_seconds',300),
                    publication=lambda: SignedURLPublication(*publication_config))
            doc['asr_publication_configured']=all(bool(os.environ.get(k)) for k in
                ('AIVOICEBENCH_AUDIO_PUT_URL','AIVOICEBENCH_AUDIO_GET_URL','AIVOICEBENCH_AUDIO_HOST'))
            # Diarization reuses the same configured ASR profile: the cloud call
            # already returns speaker labels, so no separate endpoint or second
            # recognition submission is required.
            diarization=by_id.get(doc['routes']['diarization'])
            if diarization and diarization['enabled'] and diarization['protocol']=='volcengine_asr':
                from .diarization import ASRNativeDiarizationProvider
                def diarization_factory(root):
                    return ASRNativeDiarizationProvider(
                        provider_name=diarization['provider'] or 'volcengine',
                        model=diarization['model'] or 'bigmodel',
                        resource_id=diarization['parameters'].get('resource_id','volc.bigasr.auc_turbo'))
        # TTS V3 SSE for Active Voice Test.  Profile parameters intentionally
        # carry current account-specific resource/voice identifiers rather than
        # code defaults; credentials remain in the secret store/environment.
        tts=by_id.get(doc['routes']['tts'])
        if tts and tts['enabled'] and adapter_available(tts, 'tts'):
            from .volcengine_tts import VolcengineTTSProvider
            _tts_profile=tts
            _tts_key=keys[tts['id']]
            def tts_factory(root):
                params=_tts_profile['parameters']
                return VolcengineTTSProvider(root, _tts_key,
                    endpoint=_tts_profile['base_url'],
                    model=_tts_profile['model'],
                    resource_id=params['resource_id'],
                    voice_type=params['voice'],
                    speech_rate=params.get('speed',0),
                    loudness_rate=params.get('volume',0),
                    pitch_rate=params.get('pitch',0),
                    audio_format=params.get('format','wav'),
                    sample_rate=params.get('sample_rate',16000),
                    timeout=params.get('timeout_seconds',30))
        if doc['revision']==0:
            doc['legacy_environment']={'provider':os.environ.get('AIVOICEBENCH_LLM_PROVIDER','none'),
                'model':os.environ.get('AIVOICEBENCH_LLM_MODEL',''),
                'note':'Existing environment configuration applies until settings are first saved'}
            provider=None
        return doc,RunProviders(asr=asr_factory, tts=tts_factory, diarization=diarization_factory, judge=provider)
