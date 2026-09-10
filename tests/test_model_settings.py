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
        self.assertEqual(provider.api_key,'secret-canary');self.assertEqual(provider.model,'test-model')
        self.assertEqual(provider.timeout_seconds,15);self.assertNotIn('secret-canary',json.dumps(snapshot))
        self.store.update(payload(rev=1));self.assertEqual(self.store.capture()[1].api_key,'secret-canary')
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
            self.assertEqual(provider.api_key,'env-secret');self.assertNotIn('env-secret',json.dumps(snapshot))
    def test_deleted_profile_and_secret(self):
        d=payload();d['secrets']={'judge':'secret'};self.store.update(d)
        d={'expected_revision':1,'profiles':[],'routes':dict.fromkeys(['tts','asr','diarization','judge'])}
        self.store.update(d)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM secrets').fetchone()[0],0)
    def test_not_integrated_speech_is_not_claimed_ready(self):
        p=profile();p.update(protocol='volcengine_tts',capabilities=['tts'])
        d=payload(p);d['routes']['judge']=None;d['routes']['tts']='judge';self.store.update(d)
        snap,provider=self.store.capture();self.assertEqual(snap['readiness']['tts'],'not_integrated')
        self.assertEqual(self.store.describe()['profiles'][0]['adapter_status'],'not_integrated')
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
            if not run.called:
                self.skipTest('Normalization tools unavailable')
            self.assertEqual(run.call_args.kwargs['provider'].model,'test-model')
            directory=self.root/response.json()['run_id']
            snapshot=(directory/'model-config.json').read_text()
            self.assertNotIn('run-secret-canary',snapshot)
            manifest=json.loads((directory/'manifest.json').read_text())
            artifact=next(a for a in manifest['artifacts'] if a['kind']=='model_configuration')
            self.assertEqual(artifact['sha256'],digest(directory/'model-config.json'))
            d=payload(rev=1);d['profiles'][0]['model']='new-model'
            client.post('/api/models',json=d)
            self.assertEqual(snapshot,(directory/'model-config.json').read_text())
