"""Synthetic container smoke: version, codecs, state and playback. No cloud calls."""
import json
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
