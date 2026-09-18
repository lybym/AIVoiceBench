"""Browser-layer test for the manual speaker-role gate (PRD-F006/F012-F014).

Drives the **real page** against the **real API** in a real browser: the real
``app.js`` review panel, the real ``POST /api/runs/{run_id}/role-review`` route and
the real reanalysis path. The Run comes from ``tests/role_review_fixture.py``, whose
transcript and speaker clusters are produced by a scripted transport.

What this proves: an anonymous-cluster Run shows the review surface, refuses an
incomplete submission, and after a complete human decision presents role-dependent
results in a new revision. What it does **not** prove: real recognition quality,
real speaker separation, or physical-device behaviour. Those stay with #85.

Skipped when Playwright or a Chromium-based browser is missing unless
``VT_REQUIRE_BROWSER=1`` is set, matching the existing browser acceptance policy.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRE_BROWSER = os.environ.get('VT_REQUIRE_BROWSER', '').strip() not in ('', '0', 'false', 'False')

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - environment dependent
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = str(error)

_STATE = {}


def _unavailable(message):
    """Unavailable browser is a skip by default and a hard failure when required."""
    if REQUIRE_BROWSER:
        raise RuntimeError(f'browser acceptance required but unavailable: {message}')
    raise unittest.SkipTest(message)


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _launch_browser(playwright):
    last_error = None
    for channel in ('chrome', 'msedge', None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True), channel
            return playwright.chromium.launch(headless=True), 'chromium'
        except Exception as error:  # pragma: no cover - environment dependent
            last_error = error
    raise RuntimeError(f'no Chromium-based browser available: {last_error}')


def _wait_for_health(base, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'{base}/health', timeout=3) as response:
                return json.loads(response.read().decode())
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f'server at {base} never became ready')


def _read_log():
    log = _STATE.get('server_log')
    if log is None:
        return '(no local server)'
    log.flush()
    try:
        return Path(log.name).read_text(encoding='utf-8')[-2000:]
    except Exception:
        return '(unreadable)'


def setUpModule():
    if PLAYWRIGHT_IMPORT_ERROR is not None:
        _unavailable(f'playwright unavailable: {PLAYWRIGHT_IMPORT_ERROR}')
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        _unavailable('FFmpeg is required to build the Run fixture')

    from tests.role_review_fixture import build_anonymous_run

    tmp = tempfile.TemporaryDirectory()
    _STATE['tmp'] = tmp
    _STATE['server'] = None
    _STATE['server_log'] = None
    # Every test gets its own pristine Run inside this shared Run root, because
    # saving a mapping deliberately mutates the Run's current AnalysisRevision.
    _STATE['data_root'] = Path(tmp.name) / 'data'
    try:
        runs, run_id, _transport = build_anonymous_run(_STATE['data_root'])
    except Exception as error:  # pragma: no cover - environment dependent
        _unavailable(f'could not build the Run fixture: {error}')
    _STATE['runs'] = runs

    port = _free_port()
    _STATE['port'] = port
    _STATE['base'] = f'http://127.0.0.1:{port}'
    log = open(Path(tmp.name) / 'server.log', 'w+', encoding='utf-8')
    _STATE['server_log'] = log
    _STATE['server'] = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / 'tests' / 'browser_server.py'),
         '--port', str(port), '--output', str(runs)],
        cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT)
    try:
        _wait_for_health(_STATE['base'])
    except Exception as error:
        _unavailable(f'analysis test server never became ready: {error} {_read_log()}')

    _STATE['playwright'] = sync_playwright().start()
    try:
        _STATE['browser'], _STATE['channel'] = _launch_browser(_STATE['playwright'])
    except Exception as error:
        _STATE['playwright'].stop()
        _unavailable(str(error))
    print(f"[role review browser] base={_STATE['base']} browser={_STATE['channel']} "
          f"run_root={_STATE['runs']}", flush=True)


def tearDownModule():
    browser = _STATE.pop('browser', None)
    if browser is not None:
        browser.close()
    playwright = _STATE.pop('playwright', None)
    if playwright is not None:
        playwright.stop()
    server = _STATE.pop('server', None)
    if server is not None:
        server.terminate()
        try:
            server.wait(timeout=10)
        except Exception:
            server.kill()
    log = _STATE.pop('server_log', None)
    if log is not None:
        log.close()
    tmp = _STATE.pop('tmp', None)
    if tmp is not None:
        tmp.cleanup()


class RoleReviewBrowserTests(unittest.TestCase):
    def setUp(self):
        from tests.role_review_fixture import build_anonymous_run

        # A fresh Run per test: saving a mapping advances the Run's revision, so a
        # shared Run would let one test decide the clusters another test reviews.
        _runs, self.run_id, self.transport = build_anonymous_run(_STATE['data_root'])
        self.page = _STATE['browser'].new_page()
        self.addCleanup(self.page.close)
        self.base = _STATE['base']

    def open_run(self):
        self.page.goto(f'{self.base}/', wait_until='domcontentloaded')
        self.page.evaluate('openRun', self.run_id)
        self.page.wait_for_selector('#detail h3', timeout=15000)

    def review_document(self):
        with urllib.request.urlopen(
                f'{self.base}/api/runs/{self.run_id}/role-review', timeout=10) as response:
            return json.loads(response.read().decode())

    def test_anonymous_run_shows_the_review_surface_and_blocks_roles(self):
        self.open_run()
        detail = self.page.inner_text('#detail')
        self.assertIn('人工确认说话人角色', detail)
        self.assertIn('等待人工确认角色', detail)
        # Both clusters are listed with a deliberate 未知 option, and no role is set.
        self.assertEqual(self.page.locator('[data-cluster]').count(), 2)
        counted = self.page.locator('[data-cluster] input[value="unknown"]').count()
        self.assertEqual(counted, 2)
        self.assertEqual(self.page.locator('[data-cluster] input:checked').count(), 0)
        # The gate reason is stated on the segments tab, and the metrics tab explains
        # the blank table instead of showing zeros.
        self.assertIn('为何没有指标', detail)
        self.page.click('[data-tab="metrics"]')
        self.page.wait_for_selector('#detail table', timeout=10000)
        metrics = self.page.inner_text('#detail')
        self.assertIn('暂无可计算指标', metrics)
        self.assertIn('roles_not_confirmed', metrics)

    def test_incomplete_selection_is_refused_and_saves_nothing(self):
        self.open_run()
        # Decide only the first cluster: the page must refuse, not guess.
        self.page.locator('[data-cluster]').first.locator('input[value="tester"]').check()
        self.page.fill('#role-reviewer', 'zhang')
        self.page.click('#role-save')
        self.page.wait_for_function(
            "document.getElementById('notice').hidden === false", timeout=15000)
        self.assertIn('每个说话人聚类', self.page.inner_text('#notice'))
        self.assertEqual(self.review_document()['status'], 'awaiting_role_review')
        self.assertIsNone(self.review_document()['revision'])

    def test_complete_decision_creates_a_revision_and_reruns_role_stages(self):
        before = self.review_document()
        clusters = [c['speaker_id'] for c in before['clusters']]
        self.open_run()
        nodes = self.page.locator('[data-cluster]')
        nodes.nth(0).locator('input[value="tester"]').check()
        nodes.nth(1).locator('input[value="device"]').check()
        self.page.fill('#role-reviewer', 'zhang')
        self.page.fill('#role-reason', '听音比对后确认')
        self.page.click('#role-save')
        self.page.wait_for_function(
            "document.body.innerText.includes('角色已确认')", timeout=60000)

        detail = self.page.inner_text('#detail')
        self.assertIn('角色已确认', detail)
        self.assertIn('复核人 zhang', detail)
        self.assertIn('当前修订 REV-1', detail)

        after = self.review_document()
        self.assertEqual(after['status'], 'complete_review')
        self.assertEqual(after['revision']['revision_index'], 1)
        self.assertEqual(after['revision']['reviewer'], 'zhang')
        self.assertEqual(after['revision']['decisions'],
                         {clusters[0]: 'tester', clusters[1]: 'device'})

        # The saved revision drove a new AnalysisRevision in which role-dependent
        # stages really ran. The revision records the base revision it was applied to.
        with urllib.request.urlopen(
                f'{self.base}/api/runs/{self.run_id}', timeout=30) as response:
            view = json.loads(response.read().decode())
        self.assertEqual(after['revision']['analysis_id'], before['analysis_id'])
        self.assertNotEqual(view['analysis_id'], before['analysis_id'])
        roles = {segment['speaker_role'] for segment in view['fused_segments']}
        self.assertIn('tester', roles)
        self.assertIn('device', roles)
        self.assertNotEqual(view['stages']['turns']['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
