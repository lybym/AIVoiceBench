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
        assert not recording_run_errors(json.loads((directory/'manifest.json').read_text()),directory)
    assert requests.get(base+'/api/models',timeout=5).json()==before['models']
    assert any(d.get('transcript',{}).get('segments') for d in before['runs'].values())
    print('PASS: Docker restart preserved transcript, runs, configuration and artifact hashes (synthetic)')
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
        import_recording(source,history_path.parent,synthetic=True,
            profile={'device':'Synthetic ASR restart fixture'},
            providers=RunProviders(asr=lambda root:VolcengineASRProvider(root,'secret-canary',
                publication=publication,transport=transport)),model_snapshot={'synthetic_transport':True})
    runs=requests.get(base+'/api/runs',timeout=5).json()['runs']
    before={'runs':{r['run_id']:requests.get(base+'/api/runs/'+r['run_id'],timeout=5).json() for r in runs},
            'models':requests.get(base+'/api/models',timeout=5).json()}
    history_path.write_text(json.dumps(before,ensure_ascii=False),encoding='utf-8')
    print('Saved synthetic Run/transcript state for restart verification')
