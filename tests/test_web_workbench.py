"""Evidence Workbench vendor and artifact guard (PRD-F014, PRD-N001/N002/N006).

The Web Evidence Workbench draws the backend's persisted regions with a vendored
wavesurfer.js bundle served from the same origin. Three things must hold, and none of
them is a style preference:

1. **Provenance.** The vendored bytes are the pinned 7.12.12 release, and the manifest
   records a sha256 for every file the browser can load.
2. **Same origin only.** The page may never reach a CDN or any other host for the
   library: the browser holds no provider credential and the Docker image must run
   without network access to a third party.
3. **No browser-side recomputation.** The compiled renderer reproduces the backend
   document verbatim instead of recomputing metrics, boundaries or roles.

**This module is an artifact-level no-recomputation guard, not browser behaviour
evidence.** It reads files, hashes them and runs one Node projection gate; it says
nothing about waveform pixels, Regions, Timeline rendering, seeking or zoom. Those
still need a real-browser run (the container acceptance path tracked by Issue #85), so
no result in this module may be reported as `real_recording_verified` or as a completed
browser acceptance.

The projection gate needs Node: an absent toolchain is a skip by default and a hard
failure when ``VT_REQUIRE_WEB_BUILD=1`` (CI sets it), matching
`tests/test_web_station_build.py`. The nested Node call writes to a file descriptor
rather than a pipe, because piped stdio is denied to child processes in some sandboxes
and a denied pipe would turn a real failure into an empty string.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from aivoicebench import api

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / 'aivoicebench' / 'static'
VENDOR_DIR = STATIC_DIR / 'vendor'
VENDOR_MANIFEST = VENDOR_DIR / 'VENDOR.json'
RENDER_GATE = REPO_ROOT / 'scripts' / 'verify-workbench-render.mjs'

#: Pinned release. A version bump must update the manifest, this constant and the
#: vendored bytes together.
WAVESURFER_VERSION = '7.12.12'
WAVESURFER_TARBALL_SHA1 = 'f402d88f56091d09e045c98f48075859b37719dd'

#: Hosts that would make the page depend on a third party at runtime.
FORBIDDEN_LIBRARY_HOSTS = ('cdn.jsdelivr', 'unpkg.com', 'cdnjs', 'https://', 'http://')

REQUIRE_WEB_BUILD = os.environ.get('VT_REQUIRE_WEB_BUILD', '').strip() not in ('', '0', 'false', 'False')


def _unavailable(message):
    """An absent Node toolchain is a skip unless the gate is required."""
    if REQUIRE_WEB_BUILD:
        raise RuntimeError(f'Evidence Workbench gate required but unavailable: {message}')
    raise unittest.SkipTest(message)


@contextlib.contextmanager
def _scratch_directory(label):
    """A writable scratch directory for one gate run.

    `tempfile.TemporaryDirectory()` is deliberately not used: some CI/container
    sandboxes hand a `tempfile`-created directory an ACL that denies even its own
    creator, which would turn "the gate could not write its runner script" into a
    reported failure instead of a skip. The repository already keeps its throwaway tree
    under the git-ignored `.test-tmp/` (see `scripts/verify-web-build.mjs`).
    """
    root = REPO_ROOT / '.test-tmp' / label
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def _outside_repo_scratch_directory(label):
    """A scratch root outside this checkout, for the missing-artifact probe.

    `verify-workbench-render.mjs` resolves the artifact relative to its own location,
    so a copy of it placed here must observe a repository with no compiled Workbench.

    The directory is created with `os.makedirs` rather than `tempfile.mkdtemp` for the
    sandbox reason documented above.
    """
    root = Path(tempfile.gettempdir()) / f'aivoicebench-{label}'
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_node(script_path, cwd):
    """Run a Node script with stdio discarded (never captured through a pipe)."""
    node = shutil.which('node')
    if node is None:
        _unavailable('node is not installed')
    return node, subprocess.run([node, str(script_path)], cwd=str(cwd),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _gate_output(script_path, cwd):
    """Return `(exit_code, captured_output)` of one Node gate.

    The nested Node call writes its stdout/stderr straight to a file descriptor rather
    than to a pipe: piped stdio is denied to child processes in some sandboxes, and a
    denied pipe would silently turn a real gate result into an empty string.
    """
    with _scratch_directory('web-workbench-gate') as scratch:
        output = scratch / 'output.txt'
        status = scratch / 'status.txt'
        runner = scratch / 'run.mjs'
        runner.write_text(
            "import { spawnSync } from 'node:child_process';\n"
            "import { closeSync, openSync, writeFileSync } from 'node:fs';\n"
            f"const fd = openSync({json.dumps(str(output))}, 'w');\n"
            f"const r = spawnSync(process.execPath, [{json.dumps(str(script_path))}, '--json'],"
            f" {{ cwd: {json.dumps(str(cwd))}, stdio: ['ignore', fd, fd] }});\n"
            "closeSync(fd);\n"
            f"writeFileSync({json.dumps(str(status))}, String(r.status));\n",
            encoding='utf-8')
        _run_node(runner, cwd)
        text = output.read_text(encoding='utf-8') if output.exists() else ''
        code = int(status.read_text(encoding='utf-8').strip() or '-1') if status.exists() else -1
    return code, text


def _render_gate_json():
    if shutil.which('node') is None:
        _unavailable('node is not installed')
    code, text = _gate_output(RENDER_GATE, REPO_ROOT)
    try:
        return json.loads(text)
    except ValueError:
        raise AssertionError(
            f'verify-workbench-render.mjs produced no JSON result (exit {code}): {text[-2000:]!r}')


def _vendor_manifest():
    return json.loads(VENDOR_MANIFEST.read_text(encoding='utf-8'))


def _sha256(path):
    digest = hashlib.sha256()
    digest.update(Path(path).read_bytes())
    return digest.hexdigest()


class WorkbenchVendorProvenanceTests(unittest.TestCase):
    """The vendored library is the recorded, pinned release and nothing else."""

    def test_the_vendor_manifest_records_the_pinned_release(self):
        manifest = _vendor_manifest()
        self.assertEqual(manifest.get('schema_version'), '1.0.0')
        components = manifest.get('components') or []
        self.assertTrue(components, 'VENDOR.json must record at least one component')
        wavesurfer = components[0]
        self.assertEqual(wavesurfer.get('name'), 'wavesurfer.js')
        self.assertEqual(wavesurfer.get('version'), WAVESURFER_VERSION)
        self.assertEqual(wavesurfer.get('license'), 'BSD-3-Clause')
        self.assertEqual(wavesurfer.get('tarball_sha1'), WAVESURFER_TARBALL_SHA1)
        # The registry integrity is either the exact string or explicitly unknown;
        # an invented hash is never acceptable.
        integrity = wavesurfer.get('npm_integrity')
        self.assertTrue(integrity is None or str(integrity).startswith('sha512-'),
                        'npm_integrity must be the registry sha512 string or null')
        self.assertIn(f'wavesurfer.js-{WAVESURFER_VERSION}.tgz', str(wavesurfer.get('source') or ''))
        self.assertIn('never a measurement source', str(wavesurfer.get('note') or ''))

    def test_every_recorded_vendor_file_exists_with_its_recorded_sha256(self):
        files = _vendor_manifest()['components'][0].get('files') or []
        self.assertTrue(files, 'the manifest must record the vendored files')
        recorded = set()
        for entry in files:
            relative = str(entry['path'])
            recorded.add(relative)
            with self.subTest(path=relative):
                self.assertFalse(relative.startswith('/') or '..' in relative,
                                 'a recorded vendor path must stay inside static/')
                target = STATIC_DIR / relative
                self.assertTrue(target.is_file(), f'{relative} is recorded but missing')
                self.assertEqual(_sha256(target), entry['sha256'],
                                 f'{relative} does not match its recorded sha256')
        # The three scripts the renderer needs, plus the licence, must be recorded.
        for required in ('vendor/wavesurfer.min.js', 'vendor/regions.min.js',
                         'vendor/timeline.min.js', 'vendor/LICENSE.wavesurfer.js'):
            self.assertIn(required, recorded)

    def test_no_file_in_the_vendor_tree_is_unrecorded(self):
        """A hand-added bundle would otherwise ship without provenance."""
        recorded = {str(entry['path']) for entry in
                    _vendor_manifest()['components'][0].get('files') or []}
        for path in sorted(VENDOR_DIR.rglob('*')):
            if not path.is_file() or path.name == 'VENDOR.json':
                continue
            relative = path.relative_to(STATIC_DIR).as_posix()
            self.assertIn(relative, recorded, f'{relative} ships without a recorded sha256')


class WorkbenchSameOriginTests(unittest.TestCase):
    """The library is vendored, pinned and loaded from this origin only."""

    def _compiled_artifacts(self):
        artifacts = sorted(path for path in STATIC_DIR.iterdir()
                           if path.is_file() and path.suffix == '.js')
        self.assertTrue(artifacts, 'the compiled Browser Station artifacts must exist')
        return artifacts

    def test_no_cdn_or_external_library_host_is_reachable_from_the_page(self):
        page = (STATIC_DIR / 'index.html').read_text(encoding='utf-8')
        for path in [STATIC_DIR / 'index.html'] + self._compiled_artifacts():
            text = path.read_text(encoding='utf-8')
            with self.subTest(asset=path.name):
                for host in FORBIDDEN_LIBRARY_HOSTS:
                    self.assertNotIn(host, text,
                                     f'{path.name} must not reference an external library host')
        sources = re.findall(r'<script src="([^"]*)"', page)
        self.assertTrue(sources, 'index.html must declare its classic scripts')
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(source.startswith('/static/'),
                                'every page script must be same-origin under /static/')

    def test_the_workbench_loads_the_vendor_bundle_from_the_same_origin(self):
        script = (STATIC_DIR / 'workbench.js').read_text(encoding='utf-8')
        # The renderer builds every source from one same-origin base literal.
        self.assertIn("'/static/vendor/'", script,
                      'the renderer must resolve the vendored bundle under /static/vendor/')
        for name in ('wavesurfer.min.js', 'regions.min.js', 'timeline.min.js'):
            self.assertIn(f"'{name}'", script,
                          'the renderer must load every vendored script by its own file name')
        # The library is created by an injected classic script, not by an import.
        self.assertIn("createElement('script')", script)
        self.assertIn('/api/runs/', script, 're-reading evidence must go through the backend API')


class WorkbenchDeliveryTests(unittest.TestCase):
    """The served bytes and the projection gate, against the real FastAPI app."""

    def setUp(self):
        self._scratch = _scratch_directory('web-workbench-delivery')
        self._tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)
        self._patch = patch.object(api, 'OUTPUT_ROOT', Path(self._tmp))
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def test_the_served_vendor_bundle_is_the_committed_file(self):
        for name in ('wavesurfer.min.js', 'regions.min.js', 'timeline.min.js',
                     'LICENSE.wavesurfer.js'):
            with self.subTest(name=name):
                response = self.client.get(f'/static/vendor/{name}')
                self.assertEqual(response.status_code, 200)
                served = response.content
                committed = (VENDOR_DIR / name).read_bytes()
                self.assertEqual(served, committed,
                                 f'the served {name} must be the committed vendor artifact')

    def test_the_served_manifest_matches_the_committed_provenance(self):
        response = self.client.get('/static/vendor/VENDOR.json')
        self.assertEqual(response.status_code, 200)
        served = json.loads(response.text)
        self.assertEqual(served, _vendor_manifest())

    def test_the_render_gate_reports_the_verbatim_projection(self):
        result = _render_gate_json()
        self.assertTrue(result.get('ok'), result.get('log'))
        self.assertEqual(result.get('errors'), [])
        checks = result.get('checks') or []
        self.assertTrue(checks, 'the render gate must run its checks')
        for entry in checks:
            self.assertTrue(entry.get('ok'), entry)
        names = [str(entry.get('name')) for entry in checks]
        for wanted in ('regionsFromDocument', 'metricRows', 'selectEvidence', 'state()',
                       'exactly one Region per served region',
                       'seeks to its own persisted start_sec',
                       'does not move the player',
                       'provisional mount creates no region'):
            self.assertTrue(any(wanted in name for name in names),
                            f'the render gate must check {wanted!r}: {names}')

    def test_the_render_gate_fails_loudly_when_the_artifact_is_missing(self):
        """'No compiled renderer' must never be reported as a pass."""
        with _outside_repo_scratch_directory('workbench-gate-probe') as root:
            (root / 'scripts').mkdir()
            copy = root / 'scripts' / 'verify-workbench-render.mjs'
            shutil.copyfile(RENDER_GATE, copy)
            code, text = _gate_output(copy, root)
            self.assertNotEqual(code, 0,
                                'the render gate must exit non-zero without its artifact')
            self.assertIn('artifact_unreadable', text)


if __name__ == '__main__':
    unittest.main()
