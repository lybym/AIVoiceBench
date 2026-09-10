"""Synthetic transports + real codec import. No real cloud/device acceptance."""
import copy
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from aivoicebench import api
from aivoicebench.cloud_transport import API, HTTPReply, SignedURLPublication
from aivoicebench.import_artifacts import recording_run_errors
from aivoicebench.model_settings import ModelSettings
from aivoicebench.providers import read_invocation
from aivoicebench.runner import digest
from aivoicebench.volcengine_asr import VolcengineASRProvider
from aivoicebench.validation import transcript_errors
from aivoicebench.run_lock import run_lock

REPLY = {'result': {'text': '你好', 'utterances': [
    {'text': '你好', 'start_time': 100, 'end_time': 600, 'words': [], 'additions': {'speaker':'1'}}]}}


class Transport:
    def __init__(self, mode='ok'):
        self.mode, self.calls, self.audio = mode, [], b''

    def request(self, method, url, headers, body=None, **kwargs):
        self.calls.append((method,url,headers,body))
        if method == 'PUT':
            self.audio = body
            return HTTPReply(200, {}, b'')
        if method == 'GET':
            return HTTPReply(200, {}, self.audio if self.mode!='bad-upload' else b'wrong bytes')
        if self.mode == 'timeout':
            raise TimeoutError('secret-canary')
        payload = copy.deepcopy(REPLY)
        if self.mode == 'bad-time': payload['result']['utterances'][0]['end_time']=99999
        if self.mode == 'echo': payload['error']='secret-canary'
        code = '45000001' if self.mode=='reject' else '20000000'
        return HTTPReply(200, {'x-api-status-code':code}, json.dumps(payload,ensure_ascii=False).encode())


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class BackboneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'sample.wav'
        with wave.open(str(self.source),'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\x00\x00'*16000)
        self.runs=self.root/'runs'
        self.runs.mkdir()
        self.patch=patch.object(api,'OUTPUT_ROOT',self.runs);self.patch.start();self.addCleanup(self.patch.stop)
        self.client=TestClient(api.app);self.addCleanup(self.client.close)
        profile={'id':'speech','name':'Synthetic service','provider':'volcengine','protocol':'volcengine_asr',
                 'model':'bigmodel','base_url':API,'capabilities':['asr','diarization'],'enabled':True,
                 'credential_env':'','parameters':{}}
        self.client.post('/api/models',json={'expected_revision':0,'profiles':[profile],
            'routes':dict(asr='speech',judge=None,tts=None,diarization='speech'),
            'secrets':{'speech':'secret-canary'}}).raise_for_status()
        env=patch.dict('os.environ',{'AIVOICEBENCH_AUDIO_PUT_URL':'https://storage.example/audio?put=secret',
            'AIVOICEBENCH_AUDIO_GET_URL':'https://storage.example/audio?get=secret',
            'AIVOICEBENCH_AUDIO_HOST':'storage.example'})
        env.start();self.addCleanup(env.stop)

    def upload(self, transport):
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',side_effect=transport.request):
            response=self.client.post('/api/analyze',files={'file':('sample.wav',self.source.read_bytes())})
        self.assertEqual(response.status_code,200,response.text)
        data=response.json();directory=self.runs/data['run_id']
        manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(recording_run_errors(manifest,directory),[])
        self.assertFalse((directory/'web-analysis').exists())
        for path in directory.rglob('*.json'):
            content=path.read_text(encoding='utf-8')
            self.assertNotIn('secret-canary',content)
            self.assertNotIn('?get=secret',content)
        return data,directory,manifest

    def test_upload_routes_cloud_transcript_ledger_and_restart(self):
        transport=Transport();data,directory,manifest=self.upload(transport)
        self.assertEqual(data['stages']['asr']['status'],'complete')
        self.assertEqual(data['status'],'partial')
        self.assertEqual(data['transcript']['segments'][0]['text'],'你好')
        self.assertIsNone(data['transcript']['segments'][0]['timestamp_confidence'])
        self.assertIsNone(data['transcript']['provider_profile']['model_sha256'])
        self.assertEqual(transcript_errors(data['transcript']),[])
        self.assertEqual(data['turns'],[]);self.assertEqual(data['events'],[])
        calls=[read_invocation(p,directory) for p in (directory/'provider-calls').iterdir()]
        self.assertEqual(len(calls),2)
        self.assertTrue(all(c['status']=='complete' and c['latency_ms']>=0 for c in calls))
        self.assertEqual([m for m,*_ in transport.calls],['PUT','GET','POST'])
        body=json.loads(transport.calls[-1][3]);self.assertTrue(body['request']['show_utterances'])
        self.assertFalse(body['request']['enable_ddc'])
        self.assertEqual(manifest['original_sha256'],digest(self.source))
        with TestClient(api.app) as restarted:
            self.assertEqual(restarted.get('/api/runs/'+data['run_id']).json()['transcript'],data['transcript'])
            self.assertEqual(restarted.get(data['audio_url']).status_code,200)
        readiness=ModelSettings(self.runs/'.model-settings').capture()[0]['readiness']
        self.assertEqual(readiness['asr'],'configured');self.assertEqual(readiness['diarization'],'not_integrated')

    def test_failures_preserve_native_and_run(self):
        for mode in ('reject','timeout','bad-time','bad-upload','echo'):
            with self.subTest(mode=mode):
                transport=Transport(mode);data,directory,manifest=self.upload(transport)
                self.assertEqual(data['stages']['asr']['status'],'failed')
                self.assertEqual(data['stages']['report']['status'],'complete')
                self.assertEqual(data['transcript'],{})
                self.assertLessEqual(sum(c[0]=='POST' for c in transport.calls),1)
                if mode in ('reject','bad-time'):
                    self.assertTrue(list((directory/'provider-calls').glob('*/native-response.json')))

    def test_retry_same_run_preserves_previous_revision_and_attempts(self):
        data,directory,manifest=self.upload(Transport('timeout'))
        originals={a['path']:a['sha256'] for a in manifest['artifacts']}
        transport=Transport()
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',side_effect=transport.request):
            response=self.client.post('/api/runs/'+data['run_id']+'/resume',json={'retry_asr':True})
        self.assertEqual(response.status_code,200,response.text)
        latest=response.json();self.assertEqual(latest['run_id'],data['run_id'])
        self.assertNotEqual(latest['analysis_id'],data['analysis_id'])
        self.assertEqual(latest['stages']['asr']['status'],'complete')
        for name,sha in originals.items():self.assertEqual(digest(directory/name),sha)
        calls=sorted([read_invocation(p,directory) for p in (directory/'provider-calls').iterdir()
                      if json.loads((p/'start.json').read_text())['provider']=='volcengine'],key=lambda c:c['attempt'])
        self.assertEqual([c['attempt'] for c in calls],[1,2])
        self.assertEqual(calls[0]['operation_id'],calls[1]['operation_id'])
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',side_effect=AssertionError('No repeat billing')):
            repeat=self.client.post('/api/runs/'+data['run_id']+'/resume',json={'retry_asr':True})
        self.assertEqual(repeat.status_code,200)
        self.assertEqual(repeat.json()['analysis_id'],latest['analysis_id'])
        current=json.loads((directory/'manifest.json').read_text())
        self.assertEqual(recording_run_errors(current,directory),[])

    def test_resume_after_report_interruption_reuses_completed_asr(self):
        data,directory,manifest=self.upload(Transport())
        manifest['stages']['report'].update(status='running',finished_at=None)
        (directory/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',side_effect=AssertionError('No repeat ASR')):
            r=self.client.post('/api/runs/'+data['run_id']+'/resume',json={'retry_asr':True})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['transcript'],data['transcript'])
        self.assertEqual(r.json()['stages']['report']['status'],'complete')
        self.assertNotEqual(r.json()['analysis_id'],data['analysis_id'])

    def test_resume_rejects_tampering_and_concurrent_run(self):
        data,directory,manifest=self.upload(Transport('timeout'))
        with run_lock(directory):
            r=self.client.post('/api/runs/'+data['run_id']+'/resume',json={'retry_asr':True})
            self.assertEqual(r.status_code,409)
        (directory/'original/source.wav').write_bytes(b'changed')
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',side_effect=AssertionError('No upload')):
            r=self.client.post('/api/runs/'+data['run_id']+'/resume',json={'retry_asr':True})
        self.assertEqual(r.status_code,409)


class CloudTranscriptTests(unittest.TestCase):
    def test_overlap_is_preserved_without_role_inference(self):
        provider=VolcengineASRProvider('.',None)
        data=copy.deepcopy(REPLY)
        data['result']['utterances'].append({'text':'等等','start_time':400,'end_time':800})
        segments,gaps=provider.normalize([json.dumps(data)],1000)
        self.assertEqual(len(segments),2);self.assertLess(segments[1]['start_ms'],segments[0]['end_ms'])
        self.assertNotIn('speaker_role',segments[0]);self.assertEqual(gaps,[])

    def test_text_without_timing_is_gap_not_fabricated_segment(self):
        provider=VolcengineASRProvider('.',None)
        segments,gaps=provider.normalize([json.dumps({'result':{'text':'没有时间'}})],1000)
        self.assertEqual(segments,[]);self.assertEqual(gaps[0]['text'],'没有时间')
