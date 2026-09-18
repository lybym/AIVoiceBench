import io,json,shutil,subprocess,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from aivoicebench import api
from aivoicebench.version import VERSION
from aivoicebench.runner import digest

REPO_ROOT = Path(__file__).resolve().parent.parent

class WebReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.patch=patch.object(api,'OUTPUT_ROOT',self.root);self.patch.start()
        self.client=TestClient(api.app)
    def tearDown(self):
        self.client.close();self.patch.stop();self.tmp.cleanup()
    def test_version_and_ui(self):
        self.assertEqual(self.client.get('/health').json()['version'],VERSION)
        self.assertEqual(self.client.get('/openapi.json').json()['info']['version'],VERSION)
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertIn('app.js',self.client.get('/').text)
    def test_fixed_voice_generation_has_live_progress_contract(self):
        script = self.client.get('/static/voice_test.js').text
        self.assertIn('vt-generation-progress', script)
        # The progress line is an aria-live status region. `voice_test.js` is now
        # the compiled artifact of `web/src/voice_test.ts`, so the exact spacing is
        # asserted on the TypeScript source and the attribute itself on the served
        # script.
        self.assertIn("setAttribute('aria-live', 'polite')",
                      (REPO_ROOT / 'web' / 'src' / 'voice_test.ts').read_text(encoding='utf-8'))
        self.assertIn("setAttribute('aria-live'", script)
        self.assertIn('startSynthesisProgress', script)
        self.assertIn('/api/voice-test/sessions/${sessionId}', script)
        self.assertIn('setFixedGenerationBusy(true)', script)
    def test_audio_qa_note_renders_stage_state_without_measurements(self):
        """The report must not silently drop QA: the stage state renders on its own.

        A Run reads its canonical QA across AnalysisRevisions, so a resumed Run can
        legitimately have a QA ledger entry while the measurements are unavailable to
        the reader. The UI must show that state instead of rendering nothing. This is a
        source-level contract check: a full browser run of `audioQaNote` needs a driver
        this environment does not provide. `app.js` is now the compiled artifact of
        `web/src/app.ts`, so the report tab is checked on the served script and the
        rendering rules on the TypeScript source that produces it.
        """
        script = self.client.get('/static/app.js').text
        self.assertIn('audioQaNote(data)', script, 'the report tab must render the QA note')
        self.assertIn('未获得音频质量测量', script)
        self.assertIn('本记录没有可读取的音频质量测量值', script)
        source = (REPO_ROOT / 'web' / 'src' / 'app.ts').read_text(encoding='utf-8')
        self.assertIn('const qa = data.audio_qa || {};', source)
        # Absent measurements must not short-circuit the whole section.
        self.assertIn('if (!measurements && !stage.status && !stage.reason) return', source)
        self.assertNotIn('if (!measurements.duration_ms && !conditions.length) return', source)

    def test_legacy_report_without_timeline(self):
        d=self.root/'RUN-legacy';d.mkdir()
        (d/'report.json').write_text(json.dumps({'run_summary':{'status':'partial'},'metrics':{'metrics':[]}}))
        self.assertEqual(self.client.get('/api/runs').json()['runs'][0]['status'],'partial')
        detail=self.client.get('/api/runs/RUN-legacy').json()
        self.assertEqual(detail['status'],'partial');self.assertIsInstance(detail['metrics'],list)
    def test_reject_unsupported(self):
        self.assertEqual(self.client.post('/api/analyze',files={'file':('bad.txt',b'x')}).status_code,400)
        self.assertFalse(list(self.root.iterdir()))
    def test_corrupt_recording_preserves_run(self):
        r=self.client.post('/api/analyze',files={'file':('bad.mp3',b'not audio')})
        self.assertEqual(r.status_code,200);d=r.json();self.assertEqual(d['status'],'failed')
        m=json.loads((self.root/d['run_id']/'manifest.json').read_text())
        self.assertTrue(m['original_sha256'])
        self.assertEqual(self.client.get('/api/runs').json()['runs'][0]['status'],'failed')
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'media tools unavailable')
    def test_all_codecs_end_to_end(self):
        source=self.root/'sample.wav'
        with wave.open(str(source),'wb') as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\x00\x00'*16000)
        for ext in ['wav','mp3','m4a']:
            with self.subTest(ext=ext):
                path=source if ext=='wav' else self.root/('sample.'+ext)
                if ext!='wav':subprocess.run(['ffmpeg','-y','-v','error','-i',str(source),str(path)],check=True)
                r=self.client.post('/api/analyze',files={'file':(path.name,path.read_bytes())},data={'device':'Synthetic fixture'})
                self.assertEqual(r.status_code,200,r.text);d=r.json();self.assertEqual(d['status'],'partial')
                root=self.root/d['run_id'];m=json.loads((root/'manifest.json').read_text())
                self.assertEqual(m['original_sha256'],digest(path))
                self.assertTrue(any(a['kind']=='normalized_audio' and a['parent_artifact_ids'] for a in m['artifacts']))
                detail=self.client.get('/api/runs/'+d['run_id']).json()
                self.assertEqual(d['status'],detail['status']);self.assertEqual(d['report_md'],detail['report_md'])
                self.assertEqual(self.client.get(d['audio_url']).status_code,200)
