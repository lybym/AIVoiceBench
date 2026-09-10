"""Tests for ImportRun provenance graph integrity.

Validates that post-ASR stage artifacts have correct parent references,
not all pointing to normalized_audio.
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


class ProvenanceGraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wav = Path(self.tmp.name) / 'test.wav'
        samples = silence(500) + tone(500) + silence(500) + tone(500) + silence(500)
        write_wav(self.wav, samples)

    def _find_manifest(self):
        for p in Path(self.tmp.name).rglob('manifest.json'):
            if '.run-locks' not in str(p):
                return p
        return None

    def _get_artifacts(self):
        p = self._find_manifest()
        manifest = json.loads(p.read_text(encoding='utf-8'))
        return {a['artifact_id']: a for a in manifest['artifacts']}

    def test_acoustic_parent_is_normalized(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        arts = self._get_artifacts()
        acoustic = [a for a in arts.values() if a['kind'] == 'acoustic-segments']
        self.assertTrue(acoustic, 'acoustic-segments artifact must exist')
        for a in acoustic:
            parents = a['parent_artifact_ids']
            parent_kinds = [arts.get(pid, {}).get('kind', '?') for pid in parents]
            self.assertIn('normalized_audio', parent_kinds,
                         f'acoustic parent should be normalized_audio, got {parent_kinds}')

    def test_fusion_parent_is_acoustic(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        arts = self._get_artifacts()
        fusion = [a for a in arts.values() if a['kind'] == 'fused-segments']
        self.assertTrue(fusion)
        for a in fusion:
            parents = a['parent_artifact_ids']
            parent_kinds = [arts.get(pid, {}).get('kind', '?') for pid in parents]
            self.assertIn('acoustic-segments', parent_kinds,
                         f'fusion parent should include acoustic-segments, got {parent_kinds}')

    def test_turns_parent_is_fusion_not_normalized(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        arts = self._get_artifacts()
        turns = [a for a in arts.values() if a['kind'] == 'turns']
        self.assertTrue(turns)
        for a in turns:
            parents = a['parent_artifact_ids']
            parent_kinds = [arts.get(pid, {}).get('kind', '?') for pid in parents]
            self.assertNotIn('normalized_audio', parent_kinds,
                           'turns should not directly depend on normalized_audio')
            self.assertIn('fused-segments', parent_kinds,
                         'turns parent should be fused-segments')

    def test_all_parents_exist(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        arts = self._get_artifacts()
        for art_id, art in arts.items():
            for pid in art['parent_artifact_ids']:
                self.assertIn(pid, arts,
                              f'artifact {art["kind"]} references unknown parent {pid}')

    def test_artifact_hash_verifies(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs2'), synthetic=True)
        # Find manifest in runs2
        for p in Path(self.tmp.name).rglob('manifest.json'):
            if '.run-locks' in str(p):
                continue
            run_dir = p.parent
            manifest = json.loads(p.read_text(encoding='utf-8'))
            for art in manifest['artifacts']:
                art_path = run_dir / art['path']
                if art_path.exists():
                    actual = digest(art_path)
                    self.assertEqual(actual, art['sha256'],
                                     f'hash mismatch for {art["kind"]} {art["artifact_id"][:12]}')
            break

    def test_immutable_evidence_not_overwritten(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        p = self._find_manifest()
        run_dir = p.parent
        manifest = json.loads(p.read_text(encoding='utf-8'))
        # Try to modify a registered artifact
        acoustic_art = next(a for a in manifest['artifacts'] if a['kind'] == 'acoustic-segments')
        art_path = run_dir / acoustic_art['path']
        original_content = art_path.read_bytes()
        # Tamper
        art_path.write_bytes(original_content + b' tampered')
        # Re-import should detect the mismatch
        from aivoicebench.import_artifacts import recording_run_errors
        errors = recording_run_errors(manifest, run_dir)
        self.assertTrue(any('integrity' in e.lower() or 'mismatch' in e.lower() for e in errors),
                        'tampered artifact should be detected')
        # Restore
        art_path.write_bytes(original_content)

    def test_provenance_chain_traces_to_root(self):
        import_recording(str(self.wav), str(Path(self.tmp.name) / 'runs'), synthetic=True)
        arts = self._get_artifacts()
        # Trace turns back through the chain
        turns = next(a for a in arts.values() if a['kind'] == 'turns')
        chain = []
        current = turns
        while current['parent_artifact_ids']:
            for pid in current['parent_artifact_ids']:
                parent = arts.get(pid)
                if parent:
                    chain.append(parent['kind'])
                    current = parent
                    break
            else:
                break
        self.assertIn('fused-segments', chain)
        self.assertIn('acoustic-segments', chain)
        self.assertIn('normalized_audio', chain)
        self.assertIn('original_recording', chain)


if __name__ == '__main__':
    unittest.main()
