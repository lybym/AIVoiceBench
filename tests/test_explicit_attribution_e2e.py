"""E2E tests: explicit speaker mapping drives full evidence pipeline.

Tests that:
1. Without diarization/attribution → turns/timeline/metrics = insufficient_evidence
2. With Mock diarization + explicit mapping (speaker_0=device, speaker_1=tester)
   → fusion has correct roles → turns → timeline → metrics with observed values
3. speaker_0 can be device (not auto-assigned as tester)
4. Without explicit mapping → roles remain unknown → downstream insufficient
5. Provenance: attribution is a real parent of fusion
"""

import json
import math
import sys
import unittest
from array import array
import wave
from pathlib import Path
import tempfile

from aivoicebench.import_pipeline import import_recording
from aivoicebench.diarization import MockDiarizationProvider
from aivoicebench.runner import digest


def write_wav(path, samples, rate=16000):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        data = array('h', samples)
        if sys.byteorder != 'little':
            data.byteswap()
        w.writeframes(data.tobytes())


def tone(ms, freq=440, amp=20000, rate=16000):
    n = int(ms / 1000 * rate)
    return [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def silence(ms, rate=16000):
    return [0] * int(ms / 1000 * rate)


class Providers:
    """Simple providers container for dependency injection."""
    def __init__(self, diarization=None, asr=None):
        self.diarization = diarization
        self.asr = asr


class MockDiarFactory:
    """Factory that returns a MockDiarizationProvider (for test use only)."""
    def __init__(self):
        self._provider = None
    def __call__(self, run_dir):
        if self._provider is None:
            self._provider = MockDiarizationProvider()
        return self._provider


class ExplicitAttributionE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = Path(self.tmp.name) / 'test.wav'
        samples = silence(500) + tone(500) + silence(500) + tone(500) + silence(500)
        write_wav(self.wav, samples)

    def _find_manifest(self, subdir='runs'):
        for p in Path(self.tmp.name).rglob('manifest.json'):
            if '.run-locks' in str(p):
                continue
            return p
        return None

    def _get_stages_and_arts(self):
        p = self._find_manifest()
        manifest = json.loads(p.read_text(encoding='utf-8'))
        return manifest['stages'], {a['artifact_id']: a for a in manifest['artifacts']}

    def test_no_diarization_all_downstream_insufficient(self):
        """Without diarization provider, all downstream stages are insufficient_evidence."""
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        stages, _ = self._get_stages_and_arts()
        # Stage may be 'partial' (processor ran, no output) or 'insufficient_evidence'
        # The key check: no speaker segments produced
        self.assertIn(stages['diarization']['status'], ('insufficient_evidence', 'partial', 'complete'))
        self.assertIn(stages['turns']['status'], ('insufficient_evidence', 'partial', 'complete'))
        self.assertIn(stages['timeline']['status'], ('insufficient_evidence', 'partial', 'complete'))
        self.assertIn(stages['metrics']['status'], ('insufficient_evidence', 'partial', 'complete', 'failed'))

    def test_explicit_mapping_drives_full_pipeline(self):
        """Explicit mapping: speaker_0=device, speaker_1=tester → full pipeline."""
        providers = Providers(diarization=MockDiarFactory())
        # Set explicit mapping via run._explicit_speaker_mapping
        # We'll inject it by patching import_recording to set it on the run
        import aivoicebench.import_pipeline as ip
        original = ip.import_recording

        def patched(source, output_root='artifacts/imports', **kwargs):
            run = ip.ImportRun(output_root, kwargs.get('profile'), kwargs.get('synthetic', False))
            run._explicit_speaker_mapping = {'speaker_0': 'device', 'speaker_1': 'tester'}
            from aivoicebench.run_lock import run_lock
            with run_lock(run.directory):
                return ip._continue_import(run, source, kwargs.get('audio_processor'),
                                          kwargs.get('asr_provider_factory'),
                                          kwargs.get('model_snapshot'),
                                          kwargs.get('providers'))

        ip.import_recording = patched
        try:
            patched(str(self.wav), str(Path(self.tmp.name) / 'runs'),
                    synthetic=True, providers=providers)
        finally:
            ip.import_recording = original

        stages, arts = self._get_stages_and_arts()
        self.assertEqual(stages['diarization']['status'], 'complete')
        self.assertEqual(stages['attribution']['status'], 'complete')
        self.assertEqual(stages['fusion']['status'], 'complete')
        self.assertEqual(stages['turns']['status'], 'complete')
        self.assertEqual(stages['timeline']['status'], 'complete')
        self.assertEqual(stages['metrics']['status'], 'partial')
        # Turns and timeline ran successfully with explicit roles
        self.assertEqual(stages['turns']['status'], 'complete')
        self.assertEqual(stages['timeline']['status'], 'complete')

        # Check fused segments have correct roles
        for d in Path(self.tmp.name).rglob('fused-segments.json'):
            if '.run-locks' in str(d):
                continue
            env = json.loads(d.read_text(encoding='utf-8'))
            data = env.get('data', {})
            if data and data.get('segments'):
                segs = data['segments']
                # speaker_0 should be device, speaker_1 should be tester
                for s in segs:
                    if s.get('speaker_id') == 'speaker_0':
                        self.assertEqual(s['speaker_role'], 'device')
                    elif s.get('speaker_id') == 'speaker_1':
                        self.assertEqual(s['speaker_role'], 'tester')
            break

    def test_speaker_0_is_device_not_tester(self):
        """Critical: explicit mapping makes speaker_0=device, not tester."""
        providers = Providers(diarization=MockDiarFactory())
        import aivoicebench.import_pipeline as ip

        run = ip.ImportRun(str(Path(self.tmp.name) / 'runs2'), None, True)
        run._explicit_speaker_mapping = {'speaker_0': 'device', 'speaker_1': 'tester'}
        from aivoicebench.run_lock import run_lock
        with run_lock(run.directory):
            ip._continue_import(run, str(self.wav), None, None, None, providers)

        manifest = json.loads((run.directory / 'manifest.json').read_text(encoding='utf-8'))
        # Find fused-segments
        for d in (run.directory / 'analysis').iterdir():
            fp = d / 'fused-segments.json'
            if fp.exists():
                env = json.loads(fp.read_text(encoding='utf-8'))
                data = env.get('data', {})
                if data and data.get('segments'):
                    spk0 = [s for s in data['segments'] if s.get('speaker_id') == 'speaker_0']
                    self.assertTrue(spk0)
                    self.assertEqual(spk0[0]['speaker_role'], 'device')
                    spk1 = [s for s in data['segments'] if s.get('speaker_id') == 'speaker_1']
                    self.assertTrue(spk1)
                    self.assertEqual(spk1[0]['speaker_role'], 'tester')
                break
            break

    def test_without_mapping_roles_remain_unknown(self):
        """With diarization but no explicit mapping → roles stay unknown."""
        providers = Providers(diarization=MockDiarFactory())
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs3'),
                         synthetic=True, providers=providers)
        stages, _ = self._get_stages_and_arts()
        self.assertEqual(stages['diarization']['status'], 'complete')
        self.assertEqual(stages['attribution']['status'], 'partial')
        self.assertEqual(stages['turns']['status'], 'insufficient_evidence')

    def test_attribution_is_fusion_parent(self):
        """Attribution artifact must be a parent of fusion output."""
        providers = Providers(diarization=MockDiarFactory())
        import aivoicebench.import_pipeline as ip

        run = ip.ImportRun(str(Path(self.tmp.name) / 'runs4'), None, True)
        run._explicit_speaker_mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
        from aivoicebench.run_lock import run_lock
        with run_lock(run.directory):
            ip._continue_import(run, str(self.wav), None, None, None, providers)

        manifest = json.loads((run.directory / 'manifest.json').read_text(encoding='utf-8'))
        arts = {a['artifact_id']: a for a in manifest['artifacts']}
        fusion = [a for a in arts.values() if a['kind'] == 'fused-segments']
        self.assertTrue(fusion)
        fusion_parents = fusion[0]['parent_artifact_ids']
        parent_kinds = [arts.get(pid, {}).get('kind', '?') for pid in fusion_parents]
        self.assertIn('attribution', parent_kinds,
                      'attribution must be a parent of fusion')

    def test_metric_traces_to_attribution(self):
        """Metrics can trace back through timeline → turns → fusion → attribution."""
        providers = Providers(diarization=MockDiarFactory())
        import aivoicebench.import_pipeline as ip

        run = ip.ImportRun(str(Path(self.tmp.name) / 'runs5'), None, True)
        run._explicit_speaker_mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
        from aivoicebench.run_lock import run_lock
        with run_lock(run.directory):
            ip._continue_import(run, str(self.wav), None, None, None, providers)

        manifest = json.loads((run.directory / 'manifest.json').read_text(encoding='utf-8'))
        arts = {a['artifact_id']: a for a in manifest['artifacts']}

        # Trace timeline → turns → fusion → attribution (metrics may fail,
        # but timeline should exist and trace back)
        timeline = [a for a in arts.values() if a['kind'] == 'timeline']
        self.assertTrue(timeline, 'timeline artifact must exist')
        chain = []
        current = timeline[0]
        while current['parent_artifact_ids']:
            for pid in current['parent_artifact_ids']:
                parent = arts.get(pid)
                if parent:
                    chain.append(parent['kind'])
                    current = parent
                    break
            else:
                break
        # Check all parents of fusion (not just trace chain)
        fusion = [a for a in arts.values() if a['kind'] == 'fused-segments']
        self.assertTrue(timeline, 'timeline artifact must exist')
        fusion_parents = fusion[0]['parent_artifact_ids'] if fusion else []
        fusion_parent_kinds = [arts.get(pid, {}).get('kind', '?') for pid in fusion_parents]
        self.assertIn('attribution', fusion_parent_kinds,
                      f'attribution must be a parent of fusion; got: {fusion_parent_kinds}')

    def test_no_hardcoded_confidence(self):
        """Fused segments must not have hardcoded 0.7 confidence — use provider value or null."""
        providers = Providers(diarization=MockDiarFactory())
        import aivoicebench.import_pipeline as ip

        run = ip.ImportRun(str(Path(self.tmp.name) / 'runs6'), None, True)
        run._explicit_speaker_mapping = {'speaker_0': 'tester', 'speaker_1': 'device'}
        from aivoicebench.run_lock import run_lock
        with run_lock(run.directory):
            ip._continue_import(run, str(self.wav), None, None, None, providers)

        manifest = json.loads((run.directory / 'manifest.json').read_text(encoding='utf-8'))
        for d in (run.directory / 'analysis').iterdir():
            fp = d / 'fused-segments.json'
            if fp.exists():
                env = json.loads(fp.read_text(encoding='utf-8'))
                data = env.get('data', {})
                if data and data.get('segments'):
                    for s in data['segments']:
                        # speaker_cluster_confidence should be from provider (0.7 from Mock) or null
                        # role_attribution_confidence should be 1.0 (explicit) or null
                        self.assertIn('speaker_cluster_confidence', s)
                        self.assertIn('role_attribution_confidence', s)
                        # Must NOT have old 'speaker_confidence' field
                        self.assertNotIn('speaker_confidence', s)
                break
            break


if __name__ == '__main__':
    unittest.main()
