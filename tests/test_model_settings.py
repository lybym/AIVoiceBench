import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from aivoicebench import api
from aivoicebench.model_settings import ModelSettings,SettingsError,RevisionConflict


def profile(id='judge'):
    return {'id':id,'name':'测试模型','provider':'custom','protocol':'openai_chat',
        'model':'test-model','base_url':'https://example.com/v1','enabled':True,
        'credential_env':'','capabilities':['judge'],'parameters':{'temperature':0.2,'timeout_seconds':15}}

def payload(p=None,rev=0):
    return {'expected_revision':rev,'profiles':[p or profile()],
        'routes':{'tts':None,'asr':None,'diarization':None,'judge':(p or profile())['id']}}

class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=ModelSettings(self.root)
    def tearDown(self):self.tmp.cleanup()
    def test_secret_redaction_persistence_and_snapshot(self):
        data=payload();data['secrets']={'judge':'secret-canary'}
        public=self.store.update(data)
        self.assertNotIn('secret-canary',json.dumps(public));self.assertTrue(public['profiles'][0]['credential_configured'])
        snapshot,provider=ModelSettings(self.root).capture()
        self.assertEqual(provider.judge.api_key,'secret-canary');self.assertEqual(provider.judge.model,'test-model')
        self.assertEqual(provider.judge.timeout_seconds,15);self.assertNotIn('secret-canary',json.dumps(snapshot))
        self.store.update(payload(rev=1));self.assertEqual(self.store.capture()[1].judge.api_key,'secret-canary')
    def test_stale_revision_is_atomic(self):
        self.store.update(payload())
        with self.assertRaises(RevisionConflict):self.store.update(payload())
        self.assertEqual(self.store.describe()['revision'],1)
    def test_invalid_role_and_parameters(self):
        for change in [{'capabilities':['tts']},{'parameters':{'temperature':float('nan')}},
            {'parameters':{'api_key':'secret'}},{'base_url':'https://x.com/?key=secret'},
            {'base_url':'https://secret@x.com'},{'base_url':'http://remote.example.com'},
            {'enabled':'yes'},{'credential_env':'bad-key'}]:
            with self.subTest(change=change):
                with self.assertRaises(SettingsError):self.store.update(payload({**profile(),**change}))
        self.assertEqual(self.store.describe()['revision'],0)
    def test_environment_takes_precedence_without_echo(self):
        p=profile();p['credential_env']='MODEL_TEST_KEY'
        with patch.dict(os.environ,{'MODEL_TEST_KEY':'env-secret'}):
            self.store.update(payload(p));snapshot,provider=self.store.capture()
            self.assertEqual(provider.judge.api_key,'env-secret');self.assertNotIn('env-secret',json.dumps(snapshot))
    def test_deleted_profile_and_secret(self):
        d=payload();d['secrets']={'judge':'secret'};self.store.update(d)
        d={'expected_revision':1,'profiles':[],'routes':dict.fromkeys(['tts','asr','diarization','judge'])}
        self.store.update(d)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM secrets').fetchone()[0],0)
    def test_tts_speech_adapter_is_integrated(self):
        """TTS is now wired (PRD-F016/F020); readiness reflects the adapter."""
        p=profile();p.update(protocol='volcengine_tts',capabilities=['tts'],
            parameters={'resource_id':'configured-resource','voice':'configured-voice','format':'wav'})
        d=payload(p);d['routes']['judge']=None;d['routes']['tts']='judge';self.store.update(d)
        snap,provider=self.store.capture()
        # The adapter exists now; readiness is 'configured' (key is stored) not 'not_integrated'
        self.assertNotEqual(snap['readiness']['tts'],'not_integrated',
                            'volcengine_tts is now an integrated adapter')
        self.assertEqual(snap['readiness']['tts'],'credential_missing')
        self.assertNotEqual(self.store.describe()['profiles'][0]['adapter_status'],'not_integrated')
        # The TTS factory is populated
        self.assertIsNotNone(provider.tts)
        configured=provider.tts(self.root)
        self.assertEqual(configured.resource_id,'configured-resource')
        self.assertEqual(configured.voice_type,'configured-voice')
        self.assertEqual(configured.audio_format,'wav')

    def test_tts_v3_requires_explicit_resource_voice_and_wav(self):
        for parameters in ({}, {'resource_id':'resource'}, {'voice':'voice'},
                           {'resource_id':'resource','voice':'voice','format':'pcm'}):
            p=profile();p.update(protocol='volcengine_tts',capabilities=['tts'],parameters=parameters)
            d=payload(p);d['routes']['judge']=None;d['routes']['tts']=p['id']
            with self.subTest(parameters=parameters), self.assertRaises(SettingsError):
                self.store.update(d)

    def test_streaming_asr_requires_secure_websocket_endpoint(self):
        p=profile('streaming')
        p.update(protocol='volcengine_streaming_asr', capabilities=['streaming_asr'],
                 model='bigmodel',
                 base_url='wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async',
                 parameters={'resource_id':'volc.seedasr.sauc.duration',
                             'end_window_size':800, 'force_to_speech_time':1000})
        d=payload(p);d['routes']['judge']=None;d['routes']['streaming_asr']=p['id']
        self.store.update(d)
        snapshot,providers=self.store.capture()
        self.assertEqual(snapshot['readiness']['streaming_asr'],'credential_missing')
        self.assertIsNotNone(providers.streaming_asr)
        configured=providers.streaming_asr(self.root)
        self.assertEqual(configured.endpoint,p['base_url'])
        self.assertEqual(configured.resource_id,'volc.seedasr.sauc.duration')
        for invalid in ('https://openspeech.bytedance.com/api/v3/sauc/bigmodel_async',
                        'ws://127.0.0.1/asr'):
            bad={**p,'base_url':invalid}
            with self.subTest(base_url=invalid),self.assertRaises(SettingsError):
                ModelSettings(self.root/'invalid').update(payload(bad))
    def test_api_redacts_invalid_input_and_rejects_cross_origin(self):
        with patch.object(api,'OUTPUT_ROOT',self.root),TestClient(api.app) as client:
            d=payload();d['secrets']={'judge':'secret-canary'}
            response=client.post('/api/models',json=d)
            self.assertEqual(response.status_code,200);self.assertNotIn('secret-canary',response.text)
            self.assertNotIn('secret-canary',client.get('/api/models').text)
            self.assertEqual(client.post('/api/models',json=d).status_code,409)
            self.assertEqual(client.post('/api/models',json=d,headers={'Origin':'https://evil.example'}).status_code,403)
            d=payload(rev=1);d['profiles'][0]['api_key']='secret-canary'
            response=client.post('/api/models',json=d);self.assertEqual(response.status_code,400)
            self.assertNotIn('secret-canary',response.text)
    def test_no_network_on_save_or_capture(self):
        with patch('requests.post',side_effect=AssertionError('Unexpected network')):
            self.store.update(payload());self.store.describe();self.store.capture()

    def test_divergent_asr_and_diarization_routes_are_refused_at_save(self):
        """Speaker clustering reads labels from the File ASR call itself (PRD-F006).

        Distinct ``asr``/``diarization`` profiles used to be accepted, which let the
        diarization document claim a speaker-contract status derived from a profile
        that never transcribed.  The stored configuration is now rejected before it
        is persisted, so an API caller cannot save the inconsistent pair.
        """
        def asr_profile(pid,endpoint,resource_id,file_mode):
            return {'id':pid,'name':pid,'provider':'volcengine','protocol':'volcengine_asr',
                    'model':'bigmodel','base_url':endpoint,'enabled':True,'credential_env':'',
                    'capabilities':['asr','diarization'],
                    'parameters':{'resource_id':resource_id,'file_mode':file_mode,
                                  'audio_transport':'inline'}}
        flash=asr_profile('flash','https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash',
                          'volc.bigasr.auc_turbo','flash')
        seed=asr_profile('seed','https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit',
                         'volc.seedasr.auc','seed_standard')
        for asr_id,diarization_id in (('flash','seed'),('seed','flash')):
            with self.subTest(asr=asr_id,diarization=diarization_id):
                self.assertEqual(self.store.describe()['revision'],0)
                with self.assertRaises(SettingsError):
                    self.store.update({'expected_revision':0,'profiles':[flash,seed],
                        'routes':{'tts':None,'asr':asr_id,'diarization':diarization_id,'judge':None}})
        # A matching pair is still accepted and persisted.
        self.store.update({'expected_revision':0,'profiles':[flash,seed],
            'routes':{'tts':None,'asr':'seed','diarization':'seed','judge':None}})
        self.assertEqual(self.store.describe()['revision'],1)

    def test_web_run_freezes_selected_profile_without_secret(self):
        import io,wave
        from aivoicebench.runner import digest
        data=io.BytesIO()
        with wave.open(data,'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\x00\x00'*16000)
        with patch.object(api,'OUTPUT_ROOT',self.root),TestClient(api.app) as client:
            d=payload();d['secrets']={'judge':'run-secret-canary'}
            self.assertEqual(client.post('/api/models',json=d).status_code,200)
            with patch('aivoicebench.pipeline.run_full_pipeline',return_value={}) as run:
                response=client.post('/api/analyze',files={'file':('fixture.wav',data.getvalue())})
            self.assertEqual(response.status_code,200)
            self.assertFalse(run.called, 'Web must not create a second analysis pipeline')
            directory=self.root/response.json()['run_id']
            manifest=json.loads((directory/'manifest.json').read_text())
            config_path=directory/next(a['path'] for a in manifest['artifacts'] if a['kind']=='model_configuration')
            snapshot=config_path.read_text(encoding='utf-8')
            self.assertNotIn('run-secret-canary',snapshot)
            manifest=json.loads((directory/'manifest.json').read_text())
            artifact=next(a for a in manifest['artifacts'] if a['kind']=='model_configuration')
            self.assertEqual(artifact['sha256'],digest(config_path))
            d=payload(rev=1);d['profiles'][0]['model']='new-model'
            client.post('/api/models',json=d)
            self.assertEqual(snapshot,config_path.read_text(encoding='utf-8'))
