"""Browser Station TypeScript build and static-delivery contract (PRD-F020/F021/F023/F025).

The Browser Station is written in TypeScript (`web/src/*.ts`) and ships the
compiled JavaScript from `aivoicebench/static/`, which is what FastAPI serves and
what the Docker image contains. That split is only safe while three things hold,
and this module is the enforcement:

1. **The compiled artifacts are the build output.** They must typecheck under
   `strict` and be byte-identical to a fresh compile, so a source change that was
   never rebuilt cannot ship the previous browser logic.
2. **No second hand-written JavaScript source.** The migrated entry points exist
   only as TypeScript; a hand-maintained `.js` twin would reintroduce the
   field-drift risk the migration removed.
3. **The delivered artifacts still behave.** The served files must keep the
   control layer, the generation-progress contract and the QA/role-review
   rendering the existing browser acceptance and container checks rely on, with no
   module wrapper (the page loads them as classic scripts sharing one global
   scope); the compiled scripts must still evaluate together in one global scope.

These are artifact-level checks, not browser behaviour evidence: real behaviour
stays with `tests/test_voice_browser.py`, `test_voice_integration_acceptance.py`
and the container acceptance run in the release workflow.

The gates shell out to `scripts/verify-web-build.mjs` and
`scripts/smoke-web-station.mjs`, so they need Node and the pinned TypeScript. Like
the browser tests, an absent toolchain is a skip by default - and a hard failure
when ``VT_REQUIRE_WEB_BUILD=1``, which CI sets so "the build gate was skipped" can
never be reported as a pass.

The vendored wavesurfer.js bundle that the Evidence Workbench loads from
`/static/vendor/` is guarded separately by `tests/test_web_workbench.py`.
"""

import contextlib
import json
import os
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
SOURCE_DIR = REPO_ROOT / 'web' / 'src'
VERIFY_SCRIPT = REPO_ROOT / 'scripts' / 'verify-web-build.mjs'
SMOKE_SCRIPT = REPO_ROOT / 'scripts' / 'smoke-web-station.mjs'

REQUIRE_WEB_BUILD = os.environ.get('VT_REQUIRE_WEB_BUILD', '').strip() not in ('', '0', 'false', 'False')

#: Browser Station entry points: TypeScript source -> the served artifact.
ENTRY_POINTS = {
    'app.ts': 'app.js',
    'models.ts': 'models.js',
    'voice_test.ts': 'voice_test.js',
    'workbench.ts': 'workbench.js',
    'pcm_capture_worklet.ts': 'pcm_capture_worklet.js',
}

#: Static assets that stayed hand-written because they are not browser logic.
#: The vendored wavesurfer.js bundle lives in `static/vendor/` and is covered by
#: `tests/test_web_workbench.py`.
NON_COMPILED_ASSETS = ('index.html', 'app.css')

#: Compiled entry points the page loads with <script src>.
PAGE_SCRIPTS = ('app.js', 'models.js', 'voice_test.js', 'workbench.js')


@contextlib.contextmanager
def _scratch_directory(label):
    """A writable scratch directory for one gate run.

    `tempfile.TemporaryDirectory()` is deliberately not used. Some CI/container
    sandboxes hand a `tempfile`-created directory an ACL that denies even its own
    creator, which turns "the gate could not write its runner script" into a reported
    test failure instead of a skip. The repository already keeps its throwaway build
    tree under the git-ignored `.test-tmp/` (see `scripts/verify-web-build.mjs`), so
    the scratch tree lives there and the mechanism is unchanged: a runner script is
    written to disk, and Node writes its structured JSON result to a second file.
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
    """A scratch root that Node's module resolution cannot climb out of.

    `verify-web-build.mjs` resolves the pinned `typescript` through Node's directory
    walk, so the "the compiler is missing" probe has to live *outside* this checkout;
    inside it, `node_modules/typescript` would be found in an ancestor directory and
    the probe would stop testing what it claims to test. The directory is created with
    `os.makedirs` rather than `tempfile.mkdtemp` for the same sandbox reason as above.
    """
    root = Path(tempfile.gettempdir()) / f'aivoicebench-{label}'
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _unavailable(message):
    """An absent Node/TypeScript toolchain is a skip unless the gate is required."""
    if REQUIRE_WEB_BUILD:
        raise RuntimeError(f'Browser Station build gate required but unavailable: {message}')
    raise unittest.SkipTest(message)


def _run_node(script_path, cwd):
    """Run a Node script with stdio discarded.

    Child output is deliberately not captured through a pipe: the sandbox used by
    some contributors denies piped stdio to child processes, and the gate must not
    depend on that. Node itself writes the structured result to a file.
    """
    node = shutil.which('node')
    if node is None:
        _unavailable('node is not installed')
    return node, subprocess.run([node, str(script_path)], cwd=str(cwd),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _node_json(script_path):
    """Return the structured result of a Browser Station Node gate.

    The nested Node call writes its stdout/stderr straight to a file descriptor
    rather than to a pipe. Piped stdio is denied to child processes in some sandboxes,
    and a denied pipe would silently turn a real gate result into an empty string -
    which is exactly the "a gate that did not run must not look like a pass" failure
    this module exists to prevent.
    """
    with _scratch_directory('web-station-node-gate') as scratch:
        output = scratch / 'result.json'
        status = scratch / 'status.txt'
        runner = scratch / 'run.mjs'
        runner.write_text(
            "import { spawnSync } from 'node:child_process';\n"
            "import { closeSync, openSync, writeFileSync } from 'node:fs';\n"
            f"const fd = openSync({json.dumps(str(output))}, 'w');\n"
            f"const r = spawnSync(process.execPath, [{json.dumps(str(script_path))}, '--json'],"
            f" {{ cwd: {json.dumps(str(REPO_ROOT))}, stdio: ['ignore', fd, fd] }});\n"
            "closeSync(fd);\n"
            f"writeFileSync({json.dumps(str(status))}, String(r.status));\n",
            encoding='utf-8')
        _run_node(runner, REPO_ROOT)
        text = output.read_text(encoding='utf-8') if output.exists() else ''
    try:
        return json.loads(text)
    except ValueError:
        raise AssertionError(f'{script_path.name} produced no JSON result: {text[-2000:]!r}')


def _verify_web_build_json():
    if not (REPO_ROOT / 'node_modules' / 'typescript').exists():
        _unavailable('the pinned TypeScript is not installed (run `npm ci`)')
    return _node_json(VERIFY_SCRIPT)


def _smoke_station_errors():
    node = shutil.which('node')
    if node is None:
        _unavailable('node is not installed')
    return _node_json(SMOKE_SCRIPT).get('errors', ['smoke check produced no JSON'])


class BrowserStationBuildGateTests(unittest.TestCase):
    def test_every_entry_point_is_typescript_with_compiled_output(self):
        for source, artifact in ENTRY_POINTS.items():
            with self.subTest(source=source):
                self.assertTrue((SOURCE_DIR / source).is_file(),
                                f'{source} must be the maintained source')
                self.assertTrue((STATIC_DIR / artifact).is_file(),
                                f'{artifact} must be the build output of {source}')

    def test_no_hand_written_javascript_twin_remains(self):
        """The migrated logic must not also exist as a hand-written .js source."""
        for source in ENTRY_POINTS:
            stem = source[:-3]
            self.assertFalse((STATIC_DIR / f'{stem}.ts').exists(),
                             'TypeScript sources live in web/src, not beside the artifacts')
            self.assertFalse((SOURCE_DIR / f'{stem}.js').exists(),
                             'a compiled .js must not be checked in beside its TypeScript source')
        # Only the compiled entry points plus index.html/app.css live in static/.
        served_assets = sorted(
            path.name for path in STATIC_DIR.iterdir()
            if path.is_file() and path.suffix in ('.js', '.html', '.css'))
        self.assertEqual(served_assets,
                         sorted(list(ENTRY_POINTS.values()) + list(NON_COMPILED_ASSETS)))

    def test_compiled_artifacts_are_plain_global_scripts(self):
        """The page loads them with <script src>, so there must be no module wrapper."""
        forbidden = ('Object.defineProperty(exports', 'define.amd', 'System.register(',
                     '__esModule', 'export default', 'import {')
        for artifact in PAGE_SCRIPTS:
            text = (STATIC_DIR / artifact).read_text(encoding='utf-8')
            with self.subTest(artifact=artifact):
                for token in forbidden:
                    self.assertNotIn(token, text,
                                     f'{artifact} must stay a classic script sharing the page scope')
                # TypeScript-only declarations must not survive into the artifact.
                for line in text.splitlines():
                    stripped = line.strip()
                    self.assertFalse(stripped.startswith('interface '),
                                     f'{artifact} still contains a TypeScript interface')
                    self.assertFalse(stripped.startswith('type ') and stripped.endswith(';'),
                                     f'{artifact} still contains a TypeScript type alias')

    def test_compiled_artifacts_share_one_global_script_scope(self):
        """The compiled scripts must load together as classic scripts.

        A module wrapper, a duplicated top-level name or a reference dropped during
        the migration would still typecheck per file but break the page. The smoke
        check evaluates all three in one V8 context with a DOM stub and asserts the
        expected globals and the published `window.VT` surface.
        """
        self.assertEqual(_smoke_station_errors(), [])

    def test_compiled_artifacts_are_deterministic_and_current(self):
        result = _verify_web_build_json()
        self.assertTrue(result.get('ok'), result.get('log'))
        self.assertEqual(result.get('typecheck_errors'), [])
        self.assertEqual(result.get('stale'), [], 'run `npm run build` and commit the output')
        self.assertEqual(result.get('missing'), [], 'run `npm run build` and commit the output')
        self.assertEqual(result.get('orphans'), [])
        self.assertEqual(result.get('stale_sources'), [])
        self.assertEqual(sorted(result.get('compiled_files') or []),
                         sorted(ENTRY_POINTS.values()))

    def test_missing_compiler_fails_the_gate_instead_of_passing_silently(self):
        """A missing TypeScript install must fail; 'no compiler' is not a pass."""
        with _outside_repo_scratch_directory('missing-compiler-probe') as tmp:
            root = tmp
            (root / 'scripts').mkdir()
            (root / 'web' / 'src').mkdir(parents=True)
            (root / 'package.json').write_text('{"name":"probe","private":true}', encoding='utf-8')
            (root / 'tsconfig.json').write_text(
                json.dumps({'compilerOptions': {'outDir': 'out', 'rootDir': 'web/src'},
                            'include': ['web/src/**/*.ts']}), encoding='utf-8')
            (root / 'web' / 'src' / 'probe.ts').write_text('const value: number = 1;\n', encoding='utf-8')
            # The verification script is copied unchanged; only its location differs,
            # so it resolves `typescript` from this root and must not find it.
            shutil.copyfile(VERIFY_SCRIPT, root / 'scripts' / 'verify-web-build.mjs')
            (root / '.test-tmp').mkdir()

            result_path = root / 'probe-output.json'
            status_path = root / 'probe-status.txt'
            runner = root / 'runner.mjs'
            runner.write_text(
                "import { spawnSync } from 'node:child_process';\n"
                "import { closeSync, openSync, writeFileSync } from 'node:fs';\n"
                f"const fd = openSync({json.dumps(str(result_path))}, 'w');\n"
                "const r = spawnSync(process.execPath, ['./scripts/verify-web-build.mjs', '--json'],"
                " { cwd: process.cwd(), env: { ...process.env, NODE_PATH: 'nonexistent' },"
                "   stdio: ['ignore', fd, fd] });\n"
                "closeSync(fd);\n"
                f"writeFileSync({json.dumps(str(status_path))}, String(r.status));\n",
                encoding='utf-8')
            _node, completed = _run_node(runner, root)
            self.assertEqual(completed.returncode, 0, 'the probe runner must not fail')
            code = int((status_path.read_text(encoding='utf-8').strip() or '0'))
            out = result_path.read_text(encoding='utf-8') if result_path.exists() else ''
            self.assertNotEqual(code, 0,
                                'verify-web-build.mjs must exit non-zero without TypeScript')
            self.assertIn('typescript_not_installed', out)


class BrowserStationDeliveryContractTests(unittest.TestCase):
    """The compiled artifacts must keep the contracts the browser tests rely on."""

    def setUp(self):
        self._scratch = _scratch_directory('web-station-delivery')
        self._tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)
        self._patch = patch.object(api, 'OUTPUT_ROOT', Path(self._tmp))
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)

    def test_index_still_loads_the_compiled_entry_points(self):
        page = self.client.get('/').text
        for artifact in PAGE_SCRIPTS:
            self.assertIn(f'/static/{artifact}', page)
        # The worklet is loaded by the capture path, never by the page itself.
        self.assertNotIn('pcm_capture_worklet.js', page)

    def test_served_artifacts_are_the_build_output(self):
        for artifact in ENTRY_POINTS.values():
            with self.subTest(artifact=artifact):
                response = self.client.get(f'/static/{artifact}')
                self.assertEqual(response.status_code, 200)
                # Compare content, not checkout line endings: the working tree may
                # hold CRLF while the served file comes from the git blob.
                served = response.text.replace('\r\n', '\n')
                built = (STATIC_DIR / artifact).read_text(encoding='utf-8').replace('\r\n', '\n')
                self.assertEqual(served, built)

    def test_control_layer_contracts_survive_compilation(self):
        script = self.client.get('/static/voice_test.js').text
        for token in ('window.VT', 'controlState', 'startFixedTest', 'startFreeTest',
                      'stopFixedTest', 'stopFreeTest', 'pcm-capture', 'capture_started',
                      'capture_stopped', 'device_speech_start', 'device_speech_end',
                      'observation_timeout', 'playback_cancelled', 'vad_diagnostics',
                      'no_response_observed', 'cannot_confirm_response_end',
                      'streaming', 'turn_file', 'configured_turn_file'):
            self.assertIn(token, script, f'compiled control layer lost {token!r}')
        # The audio framing constants the backend expects must still be present.
        for token in ('16000', '3200', '262144', 'CAPTURE_BACKPRESSURE_BYTES'):
            self.assertIn(token, script)

    def test_binary_frame_framing_is_unchanged(self):
        """The 4-byte big-endian sequence header is a wire contract (PRD-F023)."""
        script = self.client.get('/static/voice_test.js').text
        self.assertIn('frameCapturePayload', script)
        self.assertIn('AUDIO_FRAME_HEADER_BYTES', script)
        models = self.client.get('/static/models.js').text
        for shift in ('>>> 24', '>>> 16', '>>> 8'):
            self.assertIn(shift, models, 'the big-endian sequence header must keep its byte order')
        self.assertIn('function frameCapturePayload', models)

    def test_generation_progress_contract_survives_compilation(self):
        script = self.client.get('/static/voice_test.js').text
        self.assertIn('vt-generation-progress', script)
        self.assertIn('aria-live', script)
        self.assertIn('startSynthesisProgress', script)
        self.assertIn('/api/voice-test/sessions/${sessionId}', script)
        self.assertIn('setFixedGenerationBusy(true)', script)

    def test_qa_and_role_review_contracts_survive_compilation(self):
        script = self.client.get('/static/app.js').text
        self.assertIn('audioQaNote(data)', script, 'the report tab must render the QA note')
        self.assertIn('data.audio_qa', script)
        # Absent measurements must not short-circuit the whole section.
        self.assertIn('if (!measurements && !stage.status && !stage.reason)', script)
        self.assertIn('未获得音频质量测量', script)
        self.assertIn('本记录没有可读取的音频质量测量值', script)
        # The manual role gate stays a first-class surface.
        self.assertIn('roleReviewNote(data)', script)
        self.assertIn('role-save', script)
        self.assertIn('请为每个说话人聚类选择角色（未知也是明确选择）', script)

    def test_speaker_cluster_statistic_counts_clusters_not_segments(self):
        """Speaker segments and speaker clusters are different facts (Issue #113).

        Reporting the diarization document's entry count as the cluster count made a
        5-cluster recording display 65 clusters. Both the source and the delivered
        artifact must derive the statistic from distinct ``speaker_id`` values.
        """
        source = (REPO_ROOT / 'web' / 'src' / 'app.ts').read_text(encoding='utf-8')
        script = self.client.get('/static/app.js').text
        for text in (source, script):
            self.assertIn('distinctClusterIds', text)
            self.assertIn('countClusters(data.speaker_segments)', text)
        # The defect's shape must not come back: the segment list's length is not a
        # cluster count anywhere in the analysis view.
        for text in (source, script):
            self.assertNotIn("['说话人聚类', (data.speaker_segments || []).length]", text)
            self.assertNotIn('聚类数 \' + (data.speaker_segments', text)

    def test_async_role_rebuild_contract_survives_compilation(self):
        """The page tracks the accepted operation instead of blocking on the save."""
        script = self.client.get('/static/app.js').text
        for token in ('trackRoleReview', 'role-review/operations', 'operation_id',
                      'operation_in_progress', 'role-save-progress'):
            self.assertIn(token, script, f'compiled analysis view lost {token!r}')
        # A completed human decision is never presented as a promise that metrics
        # will be generated.
        self.assertIn('downstreamAbstentionNote', script)
        self.assertIn('人工决策与下游指标是两件事', script)

    def test_workbench_contract_survives_compilation(self):
        """The compiled Evidence Workbench keeps its published surface (PRD-F014).

        This is the artifact half of the contract; the projection behaviour itself is
        checked by `scripts/verify-workbench-render.mjs` and the vendored library's
        provenance by `tests/test_web_workbench.py`.
        """
        script = self.client.get('/static/workbench.js').text
        for token in ('window.WB', 'regionsFromDocument', 'metricRows', 'findingRows',
                      'eventRows', 'turnRows', 'transcriptRows', 'selectEvidence',
                      'playerState', 'mount', 'unmount', 'state'):
            self.assertIn(token, script, f'compiled workbench lost {token!r}')
        # Evidence is re-read from the backend, never recomputed in the browser.
        self.assertIn('evidence-workbench', script)
        # The library is loaded lazily from the same origin, never from a CDN.
        self.assertIn('/static/vendor/', script)
        # No credential literal may be baked into the renderer.
        for token in ('ARK_API_KEY=', 'OPENAI_API_KEY=', 'sk-', 'Bearer ', 'api_key'):
            self.assertNotIn(token, script)

    def test_model_settings_contract_survives_compilation(self):
        script = self.client.get('/static/models.js').text
        for token in ('modelsView', 'commitModels', 'expected_revision', 'credential_env', 'api_key'):
            self.assertIn(token, script)

    def test_provider_secrets_never_enter_the_artifacts(self):
        """No credential literal or secret value may be baked into the bundle."""
        for artifact in ENTRY_POINTS.values():
            text = (STATIC_DIR / artifact).read_text(encoding='utf-8')
            with self.subTest(artifact=artifact):
                for token in ('ARK_API_KEY=', 'OPENAI_API_KEY=', 'sk-', 'Bearer '):
                    self.assertNotIn(token, text)


if __name__ == '__main__':
    unittest.main()