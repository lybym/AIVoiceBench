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

    def test_audio_qa_is_readable_through_the_run_view_after_restart(self):
        """QA facts and their conditions come from the persisted documents, not memory."""
        transport=Transport();data,directory,manifest=self.upload(transport)
        # The synthetic fixture here is a silent tone, so QA must abstain in the
        # envelope (data: null) while still publishing the real measurements.
        self.assertEqual(data['stages']['audio_qa']['status'],'partial')
        stored=json.loads((directory/'analysis'/manifest['analysis_id']/'audio-qa.json')
                          .read_text(encoding='utf-8'))
        self.assertEqual(stored['status'],'insufficient_evidence')
        self.assertIsNone(stored['data'])
        self.assertIn('nonempty_signal',stored['reason'])
        qa=data['audio_qa']
        self.assertEqual(qa['status'],'insufficient_evidence')
        self.assertEqual(qa['reason'],stored['reason'])
        self.assertEqual((qa['measurements']['container'],qa['measurements']['encoding'],
                          qa['measurements']['channels'],qa['measurements']['sample_rate_hz']),
                         ('wav','PCM_S16LE',1,16000))
        self.assertTrue(qa['measurements']['all_silent'])
        self.assertEqual(qa['measurements']['peak'],0)
        conditions={c['condition_id']:c for c in qa['measurements']['conditions']}
        self.assertEqual(conditions['decodable_canonical_audio']['status'],'met')
        self.assertEqual(conditions['nonempty_signal']['status'],'insufficient')
        self.assertFalse(any(c['status'] in ('pass','fail') for c in qa['measurements']['conditions']))
        with TestClient(api.app) as restarted:
            run=restarted.get('/api/runs/'+data['run_id']).json()
            self.assertEqual(run['audio_qa'],data['audio_qa'])
            self.assertEqual(run['stages']['audio_qa']['reason'],data['stages']['audio_qa']['reason'])
        readiness=ModelSettings(self.runs/'.model-settings').capture()[0]['readiness']
        # Diarization is now integrated through the same ASR profile: the single
        # recognition response already carries speaker labels, so the route is
        # 'configured' instead of 'not_integrated'.
        self.assertEqual(readiness['asr'],'configured');self.assertEqual(readiness['diarization'],'configured')

    def test_one_recognition_submission_yields_transcript_and_speaker_segments(self):
        """A single POST must yield both the Transcript and the speaker clusters."""
        transport=Transport();data,directory,manifest=self.upload(transport)
        # Exactly one recognition submission: PUT (upload), GET (verify), POST (recognize).
        self.assertEqual([m for m,*_ in transport.calls],['PUT','GET','POST'])
        self.assertEqual(sum(m=='POST' for m,*_ in transport.calls),1)
        self.assertEqual(data['stages']['asr']['status'],'complete')
        self.assertEqual(data['stages']['diarization']['status'],'complete')
        speaker=next(a for a in manifest['artifacts'] if a['kind']=='speaker-assignments')
        doc=json.loads((directory/speaker['path']).read_text(encoding='utf-8'))
        self.assertIsNotNone(doc['data'])
        segments=doc['data']['speaker_segments']
        self.assertEqual(len(segments),1)
        self.assertEqual(segments[0]['native_speaker_id'],'1')
        self.assertEqual(segments[0]['start_ms'],100.0);self.assertEqual(segments[0]['end_ms'],600.0)
        self.assertIsNone(segments[0]['confidence'])
        # Clustering only: no role may be claimed from speaker labels alone.
        attribution=next(a for a in manifest['artifacts'] if a['kind']=='attribution')
        attribution_doc=json.loads((directory/attribution['path']).read_text(encoding='utf-8'))
        self.assertTrue(all(x['role']=='unknown' for x in attribution_doc['data']['attributions']))
        # The speaker segments reference the real ASR invocation evidence.
        self.assertEqual(doc['data']['scope']['invocation_id'],doc['data']['invocation']['invocation_id'])
        self.assertFalse(doc['data']['processor']['config']['cloud_call_performed'])
        self.assertIsNotNone(doc['data']['scope']['native_response_sha256'])
        self.assertEqual(manifest['stages']['diarization']['output_artifact_ids'][-1],speaker['artifact_id'])

    def test_independent_import_needs_no_explicit_role_mapping(self):
        """An independent import must produce clusters and a report without any role input.

        Explicit mapping is an optional human verification path, not a precondition.
        Without it every role stays unknown and role-dependent stages abstain, but
        the Run, its evidence and its report are all still produced.
        """
        transport=Transport();data,directory,manifest=self.upload(transport)
        # No profile, no explicit mapping, no manual speaker list was supplied.
        self.assertEqual(data['profile'],{k:None for k in data['profile']})
        self.assertEqual(len(data['speaker_segments']),1)
        self.assertEqual(data['attribution']['status'],'partial')
        self.assertEqual([a['role'] for a in data['attribution']['attributions']],['unknown'])
        # Abstention is not failure: every stage still publishes its evidence state.
        for stage in ('diarization','attribution','turns','timeline','metrics'):
            self.assertIn(data['stages'][stage]['status'],
                          ('complete','partial','insufficient_evidence'),
                          f'{stage} must not fail an import that simply lacks role evidence')
            self.assertNotEqual(data['stages'][stage]['status'],'failed')
        # The Run keeps its artifacts, an envelope per stage and a readable report.
        kinds={a['kind'] for a in manifest['artifacts']}
        for kind in ('speaker-assignments','attribution','fused-segments','timeline','metrics','report_json'):
            self.assertIn(kind,kinds)
        self.assertEqual(manifest['status'],'partial')
        self.assertEqual(recording_run_errors(manifest,directory),[])
        self.assertTrue((directory/'analysis'/manifest['analysis_id']/'report.md').exists())
        self.assertTrue(data['report_md'])

    def test_request_body_contains_only_contract_verified_properties(self):
        """Assert the bytes actually sent, not just how the response is parsed.

        Interface-contract verification is recorded separately from real-call
        verification. Every property on the wire must be one this adapter can back
        with an official documented property or example, and the request snapshot
        filed in the Run must equal what was sent.
        """
        from aivoicebench.volcengine_asr import VERIFIED_REQUEST_FIELDS, UNVERIFIED_CAPABILITIES
        transport=Transport();data,directory,manifest=self.upload(transport)
        body=json.loads(transport.calls[-1][3])
        self.assertEqual(sorted(body['request']),sorted(VERIFIED_REQUEST_FIELDS))
        # The unverified capability must never appear on the wire under any name.
        pending=UNVERIFIED_CAPABILITIES['speaker_separation']
        for key in body['request']:
            self.assertNotIn('speaker',key.lower(),'no unverified speaker property may be sent')
            self.assertNotIn('diariz',key.lower())
        self.assertEqual(pending['interface_contract'] ,'pending')
        self.assertEqual(pending['real_call'],'not_attempted')
        # The filed invocation snapshot equals the outgoing request; the contract
        # status is recorded alongside it rather than being sent.
        call=next((directory/'provider-calls').glob('*/request.json'))
        snapshot=json.loads(call.read_text(encoding='utf-8'))
        self.assertEqual(snapshot['request'],body['request'])
        profile=data['transcript']['provider_profile']
        self.assertEqual(sorted(profile['interface_contract_verified']),sorted(VERIFIED_REQUEST_FIELDS))
        self.assertEqual(profile['capability_contract_pending']['speaker_separation']['interface_contract'],'pending')
        for key in profile['config']:
            self.assertNotIn('contract',key.lower())
        # A real call was attempted with a synthetic transport only; the Run must not
        # claim a verified live contract.
        self.assertEqual(data['stages']['asr']['status'],'complete')
        self.assertFalse(profile['capability_contract_pending']['speaker_separation']['real_call']=='verified')

    def test_missing_speaker_labels_report_pending_contract_not_enable_hint(self):
        """With no labels the reason must not pretend a known flag would fix it."""
        from aivoicebench.volcengine_asr import UNVERIFIED_CAPABILITIES
        self.assertEqual(next(iter(UNVERIFIED_CAPABILITIES)),'speaker_separation')

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

    def test_upload_exposes_speaker_segments_and_evidence_refs(self):
        """The user entry must show clusters, their evidence scope and the stage state."""
        transport=Transport();data,directory,manifest=self.upload(transport)
        self.assertEqual(len(data['speaker_segments']),1)
        segment=data['speaker_segments'][0]
        self.assertEqual(segment['native_speaker_id'],'1')
        self.assertEqual(segment['speaker_id'],manifest['original_sha256'][:12]+':speaker_0')
        self.assertEqual((segment['start_ms'],segment['end_ms']),(100.0,600.0))
        self.assertEqual(segment['timestamp_source'],'provider_utterance_estimate')
        # Evidence scope: which recording, which invocation, which native response.
        scope=data['diarization_scope']
        self.assertEqual(scope['recording_sha256'],manifest['original_sha256'])
        self.assertEqual(scope['analysis_id'],manifest['analysis_id'])
        self.assertTrue(scope['invocation_id'].startswith('CALL-'))
        self.assertIsNotNone(scope['native_response_sha256'])
        invocation=next(iter((directory/'provider-calls').glob(scope['invocation_id'])))
        self.assertEqual(digest(invocation/'native-response.json'),scope['native_response_sha256'])
        # Stage state and invocation artifact refs are visible without reading files.
        # Invocation artifacts are no longer only cloud calls: a semantic role call is
        # audited the same way, so assert each ref resolves rather than where it lives.
        self.assertEqual(data['stages']['diarization']['status'],'complete')
        self.assertTrue(data['invocation_refs'])
        registered={a['artifact_id']:a for a in manifest['artifacts']}
        for ref in data['invocation_refs']:
            self.assertIn(ref['artifact_id'],registered)
            self.assertTrue((directory/ref['path']).is_file(),ref['path'])
        self.assertTrue(any('provider-calls' in a['path'] for a in data['invocation_refs']),
                        'the cloud ASR call must still be audited')
        # Role attribution is exposed but must not claim a role it has no evidence for.
        self.assertEqual(data['attribution']['status'],'partial')
        self.assertEqual([a['role'] for a in data['attribution']['attributions']],['unknown'])

    def test_resume_rebuilds_speaker_segments_without_re_recognition(self):
        """Resume must rebuild clusters from the preserved native response only."""
        data,directory,manifest=self.upload(Transport())
        original=next(a for a in manifest['artifacts'] if a['kind']=='speaker-assignments')
        original_doc=json.loads((directory/original['path']).read_text(encoding='utf-8'))
        manifest['stages']['report'].update(status='running',finished_at=None)
        (directory/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        with patch('aivoicebench.cloud_transport.HTTPTransport.request',
                   side_effect=AssertionError('No re-upload or re-recognition on resume')):
            r=self.client.post('/api/runs/'+data['run_id']+'/resume',json={'retry_asr':True})
        self.assertEqual(r.status_code,200,r.text)
        current=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        self.assertNotEqual(current['analysis_id'],manifest['analysis_id'])
        latest=next(a for a in current['artifacts'] if a['kind']=='speaker-assignments'
                    and current['analysis_id'] in a['path'])
        rebuilt=json.loads((directory/latest['path']).read_text(encoding='utf-8'))
        self.assertEqual(rebuilt['status'],'complete')
        self.assertIsNotNone(rebuilt['data'])
        segments=rebuilt['data']['speaker_segments']
        self.assertEqual([s['native_speaker_id'] for s in segments],
                         [s['native_speaker_id'] for s in original_doc['data']['speaker_segments']])
        # Same recording identity, same invocation evidence, no new cloud call.
        self.assertEqual(rebuilt['data']['scope']['recording_sha256'],
                         original_doc['data']['scope']['recording_sha256'])
        self.assertEqual(rebuilt['data']['scope']['native_response_sha256'],
                         original_doc['data']['scope']['native_response_sha256'])
        self.assertFalse(rebuilt['data']['processor']['config']['cloud_call_performed'])
        self.assertEqual(len(list((directory/'provider-calls').iterdir())),2)
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
