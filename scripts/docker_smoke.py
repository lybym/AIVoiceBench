"""Synthetic container smoke: version, codecs, state and playback. No cloud calls."""
import json
import os
import sys
import subprocess
import tempfile
import time
import wave
from pathlib import Path
import requests
from aivoicebench.version import VERSION

base='http://127.0.0.1:8000'
for attempt in range(30):
    try:
        health=requests.get(base+'/health',timeout=2)
        health.raise_for_status()
        break
    except requests.RequestException:
        time.sleep(1)
else:
    raise RuntimeError('Container did not become healthy')
assert health.json()['version']==VERSION
assert requests.get(base+'/openapi.json',timeout=5).json()['info']['version']==VERSION
history_path=Path(os.environ.get('AIVOICEBENCH_OUTPUT','/data/output'))/'synthetic-restart-check.json'
if '--verify-history' in sys.argv:
    before=json.loads(history_path.read_text(encoding='utf-8'))
    for run_id,expected in before['runs'].items():
        actual=requests.get(base+'/api/runs/'+run_id,timeout=5).json()
        assert actual==expected, run_id
        assert requests.get(base+actual['audio_url'],timeout=5).status_code==200
        from aivoicebench.import_artifacts import recording_run_errors
        directory=history_path.parent/run_id
        manifest=json.loads((directory/'manifest.json').read_text())
        assert not recording_run_errors(manifest,directory)
        assert actual['analysis_id']==manifest['analysis_id']==expected['analysis_id'], run_id
        for name,stage in actual['stages'].items():
            assert stage['status'] and (stage['reason'] or stage['output_artifact_ids']), name
        for ref in actual.get('invocation_refs') or []:
            assert (directory/ref['path']).is_file(), ref['path']
        if run_id not in (before.get('revisions') or {}):
            continue
        # A container restart must reconstruct the *current* AnalysisRevision, the
        # whole stage ledger, the evidence links (workbench/role review) and the
        # report from the persistent volume - not from process memory.
        assert actual['analysis_id']==before['revisions'][run_id], run_id
        assert actual['stages']==expected['stages'], run_id
        review=actual.get('role_review') or {}
        # The saved human decision set is still the one this revision published as its
        # current decision set, and the revision it produced is the revision the
        # restart restored. `revision.analysis_id` records the revision the decision was
        # applied to (the anonymous import), not the revision it produced.
        assert review.get('status')=='complete_review', run_id
        assert review.get('revision'), run_id
        assert review['revision']['revision_index']==len(review['history'])>=1, run_id
        assert review['revision']['decisions']==before['decisions'][run_id], run_id
        assert review.get('gate',{}).get('role_dependent_stages_blocked') is False, run_id
        assert actual.get('workbench'), 'the restart lost the evidence links'
        assert (directory/'analysis'/actual['analysis_id']/'report.md').is_file(), run_id
        assert actual['report_md']==expected['report_md'], run_id
        assert actual['report_md'], run_id
    assert requests.get(base+'/api/models',timeout=5).json()==before['models']
    assert any(d.get('transcript',{}).get('segments') for d in before['runs'].values())
    print('PASS: Docker restart preserved transcript, runs, configuration, artifact hashes, '
          'current AnalysisRevision, role gate, evidence links and report (synthetic)')
    raise SystemExit(0)
settings=requests.get(base+'/api/models',timeout=5).json()
profile={'id':'smoke','name':'Synthetic configuration','provider':'test','protocol':'openai_chat',
    'model':'synthetic','base_url':'https://example.com/v1','credential_env':'',
    'enabled':False,'capabilities':['judge'],'parameters':{}}
payload={'expected_revision':settings['revision'],'profiles':[profile],
    'routes':dict.fromkeys(['tts','asr','diarization','judge']), 'secrets':{'smoke':'synthetic-secret-canary'}}
response=requests.post(base+'/api/models',json=payload,timeout=5)
response.raise_for_status()
assert 'synthetic-secret-canary' not in response.text
assert response.json()['profiles'][0]['credential_configured']
response=requests.post(base+'/api/models',json={'expected_revision':response.json()['revision'],
    'profiles':[],'routes':payload['routes']},timeout=5)
response.raise_for_status()
with tempfile.TemporaryDirectory() as tmp:
    source=Path(tmp)/'synthetic.wav'
    with wave.open(str(source),'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b'\x00\x00'*16000)
    for suffix in ('wav','mp3','m4a'):
        path=source if suffix=='wav' else Path(tmp)/('synthetic.'+suffix)
        if suffix!='wav':
            subprocess.run(['ffmpeg','-v','error','-i',str(source),str(path)],check=True)
        r=requests.post(base+'/api/analyze',files={'file':(path.name,path.read_bytes())},
                        data={'device':'Synthetic container smoke'},timeout=60)
        r.raise_for_status(); result=r.json()
        assert result['status']=='partial', result
        detail=requests.get(base+'/api/runs/'+result['run_id'],timeout=5).json()
        assert detail['status']==result['status'] and isinstance(detail['metrics'],list)
        history=requests.get(base+'/api/runs',timeout=5).json()['runs']
        assert next(x for x in history if x['run_id']==result['run_id'])['status']=='partial'
        assert requests.get(base+result['audio_url'],timeout=5).status_code==200
print('PASS: version '+VERSION+', WAV/MP3/M4A, history/detail, audio playback (synthetic)')

if '--save-history' in sys.argv:
    # Test support is copied by the CI workflow, never included in release images.
    sys.path.insert(0,'/app/tests')
    from test_recording_backbone import Transport
    from aivoicebench.cloud_transport import SignedURLPublication
    from aivoicebench.diarization import ASRNativeDiarizationProvider
    from aivoicebench.volcengine_asr import VolcengineASRProvider
    from aivoicebench.model_settings import RunProviders
    from aivoicebench.import_pipeline import import_recording
    transport=Transport()
    publication=SignedURLPublication('https://storage.example/audio?put=secret',
        'https://storage.example/audio?get=secret','storage.example',transport)
    with tempfile.TemporaryDirectory() as tmp:
        source=Path(tmp)/'synthetic.wav'
        with wave.open(str(source),'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(b'\x00\x00'*16000)
        directory,manifest=import_recording(source,history_path.parent,synthetic=True,
            profile={'device':'Synthetic ASR restart fixture'},
            providers=RunProviders(
                asr=lambda root:VolcengineASRProvider(root,'secret-canary',
                    publication=publication,transport=transport),
                # Speaker clusters are derived from the preserved recognition response
                # (no second cloud call), exactly as the configured product route does.
                diarization=lambda root:ASRNativeDiarizationProvider()),
            model_snapshot={'synthetic_transport':True})
    run_id=manifest['run_id']
    # One saved human decision set creates a second AnalysisRevision through the real
    # Web API, so the restart check covers the *current* revision, the role gate, the
    # evidence links and the report instead of only the first anonymous import.
    review=requests.get(base+'/api/runs/'+run_id+'/role-review',timeout=5).json()
    clusters=[c['speaker_id'] for c in review['clusters']]
    assert clusters,'the synthetic Run produced no speaker cluster to review'
    mapping={cluster:('tester' if index==0 else 'unknown')
             for index,cluster in enumerate(clusters)}
    confirmed=requests.post(base+'/api/runs/'+run_id+'/role-review',
        json={'mapping':mapping,'reviewer':'container-smoke',
              'reason':'synthetic container restart check'},timeout=60)
    confirmed.raise_for_status()
    confirmed=confirmed.json()
    assert (confirmed.get('role_review') or {}).get('status')=='complete_review',confirmed.get('role_review')
    runs=requests.get(base+'/api/runs',timeout=5).json()['runs']
    before={'runs':{r['run_id']:requests.get(base+'/api/runs/'+r['run_id'],timeout=5).json() for r in runs},
            'models':requests.get(base+'/api/models',timeout=5).json(),
            'revisions':{run_id:confirmed['analysis_id']},
            'decisions':{run_id:mapping}}
    history_path.write_text(json.dumps(before,ensure_ascii=False),encoding='utf-8')
    print('Saved synthetic Run/transcript state and confirmed revision for restart verification')
