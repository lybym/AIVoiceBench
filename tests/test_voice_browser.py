"""Browser-layer tests for fixed-dialogue control reliability (PRD-F020).

These tests drive the **real page** in a real browser: the real ``voice_test.js``
control layer, the real WebSocket protocol, a real ``Audio`` element playing the
real generated WAV, and the real in-browser VAD reading a real ``AnalyserNode``.

Controlled inputs (marked as such — they are NOT physical-device evidence):

* audio comes from a deterministic synthetic TTS stand-in
  (``tests/browser_server.py``), not a cloud provider;
* the microphone is a synthetic oscillator stream installed in the page, so the
  test can raise and lower the input level deterministically. The production VAD
  code path is unchanged and really runs.

Nothing here verifies a real speaker, a real microphone, or a physical AI
device. Those remain outstanding acceptance items.

Skipped automatically when Playwright or a Chromium-based browser is missing, so
a CI image without them still passes — **unless** ``VT_REQUIRE_BROWSER=1`` is
set, in which case an unavailable browser is a hard failure. The release
acceptance sets it so that "the browser module was skipped" can never be
reported as a pass.

Set ``VT_BROWSER_BASE_URL`` to drive an already-running server (for example the
test-only server started inside the candidate Docker image) instead of starting
a local one.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXTERNAL_BASE_URL = os.environ.get('VT_BROWSER_BASE_URL', '').strip()
REQUIRE_BROWSER = os.environ.get('VT_REQUIRE_BROWSER', '').strip() not in ('', '0', 'false', 'False')

# CI installs Playwright where it is expected to be present, so the browser test
# can import it from the same interpreter that runs the suite.
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - environment dependent
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = str(error)


def _unavailable(message):
    """Unavailable browser is a skip by default and a hard failure when required."""
    if REQUIRE_BROWSER:
        raise RuntimeError(f'browser acceptance required but unavailable: {message}')
    raise unittest.SkipTest(message)


# Installs a controllable synthetic microphone. Marked clearly as test input.
CONTROLLED_MIC_INIT = r"""
(() => {
  const state = { level: 0, calls: 0 };
  let ctx = null, osc = null, gain = null, dest = null;
  async function freshGraph() {
    if (osc) { try { osc.stop(); } catch (e) {} }
    if (ctx) { try { ctx.close(); } catch (e) {} }
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    dest = ctx.createMediaStreamDestination();
    osc = ctx.createOscillator();
    osc.type = 'sine';
    osc.frequency.value = 440;
    gain = ctx.createGain();
    gain.gain.value = state.level;
    osc.connect(gain);
    gain.connect(dest);
    osc.start();
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) {} }
  }
  window.__vtMic = {
    setLevel(value) { state.level = value; if (gain) gain.gain.value = value; },
    level() { return state.level; },
    calls() { return state.calls; },
    contextState() { return ctx ? ctx.state : null; },
  };
  navigator.mediaDevices.getUserMedia = async () => {
    state.calls += 1;
    await freshGraph();
    return dest.stream;
  };
})();
"""


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _launch_browser(playwright):
    last_error = None
    for channel in ('chrome', 'msedge', None):
        try:
            if channel:
                return playwright.chromium.launch(channel=channel, headless=True, args=[
                    '--autoplay-policy=no-user-gesture-required',
                    '--use-fake-ui-for-media-stream',
                    '--use-fake-device-for-media-stream',
                ]), channel
            return playwright.chromium.launch(headless=True, args=[
                '--autoplay-policy=no-user-gesture-required',
                '--use-fake-ui-for-media-stream',
                '--use-fake-device-for-media-stream',
            ]), 'chromium'
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


_STATE = {}


def setUpModule():
    if PLAYWRIGHT_IMPORT_ERROR is not None:
        _unavailable(f'playwright unavailable: {PLAYWRIGHT_IMPORT_ERROR}')

    tmp = tempfile.TemporaryDirectory()
    _STATE['tmp'] = tmp
    _STATE['server'] = None
    _STATE['server_log'] = None

    if EXTERNAL_BASE_URL:
        # Drive a server that already runs elsewhere (e.g. inside the candidate
        # image, started with the test-only controlled TTS).
        _STATE['port'] = None
        _STATE['base'] = EXTERNAL_BASE_URL.rstrip('/')
        try:
            _wait_for_health(_STATE['base'])
        except Exception as error:
            _unavailable(str(error))
    else:
        port = _free_port()
        _STATE['port'] = port
        _STATE['base'] = f'http://127.0.0.1:{port}'

        log = open(Path(tmp.name) / 'server.log', 'w+', encoding='utf-8')
        _STATE['server_log'] = log
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / 'tests' / 'browser_server.py'),
             '--port', str(port), '--output', str(Path(tmp.name) / 'out'),
             '--first-phrase-ms', '2500', '--phrase-ms', '250',
             '--no-response-timeout-ms', '2500'],
            cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT)
        _STATE['server'] = proc

        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                _unavailable(f'control test server exited early: {_read_log()}')
            try:
                _wait_for_health(_STATE['base'], timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        else:
            _unavailable(f'control test server never became ready: {_read_log()}')

    _STATE['playwright'] = sync_playwright().start()
    try:
        _STATE['browser'], _STATE['channel'] = _launch_browser(_STATE['playwright'])
    except Exception as error:
        _STATE['playwright'].stop()
        _unavailable(str(error))
    print(f"[browser acceptance] base={_STATE['base']} "
          f"browser={_STATE['channel']} {_STATE['browser'].version} "
          f"external_server={bool(EXTERNAL_BASE_URL)}", flush=True)


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


def _read_log():
    log = _STATE.get('server_log')
    if log is None:
        return '(no local server)'
    log.flush()
    try:
        return Path(log.name).read_text(encoding='utf-8')[-2000:]
    except Exception:
        return '(unreadable)'


class BrowserControlTestCase(unittest.TestCase):
    """Each test gets a fresh page, fresh session and fresh synthetic mic."""

    PHRASES = ['第一句：你好', '第二句：南京天气', '第三句：北京呢']
    FIRST_PHRASE_MS = 2500
    SHORT_PHRASE_MS = 250
    NO_RESPONSE_TIMEOUT_MS = 2500
    VAD_SPEAK_WAIT_MS = 700
    VAD_SILENCE_WAIT_MS = 2300

    def setUp(self):
        self.context = _STATE['browser'].new_context()
        self.addCleanup(self.context.close)
        self.context.add_init_script(CONTROLLED_MIC_INIT)
        self.page = self.context.new_page()
        self.page.set_default_timeout(20000)

    # ------------------------------------------------------------- helpers

    def api(self, path, method='GET'):
        request = urllib.request.Request(f"{_STATE['base']}{path}", method=method)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())

    def record(self, session_id):
        return self.api(f'/__test__/record/{session_id}')

    def state(self):
        return self.page.evaluate('window.VT.controlState()')

    def status_text(self):
        return self.page.inner_text('#vt-status')

    def prepare_fixed_run(self, phrases=None):
        """Load the page, generate the fixed phrases, and start the run."""
        self.page.goto(f"{_STATE['base']}/")
        self.page.click('#nav-voice-test')
        self.page.fill('#vt-phrases', '\n'.join(phrases or self.PHRASES))
        self.page.get_by_role('button', name='生成语音').click()
        expected = len(phrases or self.PHRASES)
        self.page.wait_for_function(
            "n => { const el = document.getElementById('vt-generation-progress');"
            " return !!el && el.textContent.includes(`已生成 ${n}/${n}`); }",
            arg=expected)
        self.page.click('#vt-start-fixed')
        self.page.wait_for_function(
            '() => { const s = window.VT.controlState();'
            ' return !!s && s.stats.playReceived >= 1 && !s.cancelled; }')
        self.page.wait_for_selector('#vt-stop-fixed:enabled')

    def wait_for_playing(self, timeout=20000):
        """Wait until phrase audio is actually playing in the page."""
        self.page.wait_for_function(
            '() => { const s = window.VT.controlState();'
            ' return !!s && s.has_audio === true && !s.cancelled; }',
            timeout=timeout)

    def wait_for_status(self, needle, timeout=25000):
        self.page.wait_for_function(
            'needle => document.getElementById("vt-status").textContent.includes(needle)',
            arg=needle, timeout=timeout)

    def wait_for_listening(self, timeout=20000):
        self.page.wait_for_function(
            '() => { const s = window.VT.controlState(); return !!s && s.listening === true; }',
            timeout=timeout)

    def suspect_response(self):
        """Raise the synthetic mic, then let it fall silent: a suspected answer.

        Waits until the page is actually listening first: during playback the
        control layer deliberately stops VAD, so input raised too early would be
        missed.
        """
        self.wait_for_listening()
        self.page.evaluate('window.__vtMic.setLevel(1.0)')
        self.page.wait_for_timeout(self.VAD_SPEAK_WAIT_MS)
        self.page.evaluate('window.__vtMic.setLevel(0.0)')
        self.page.wait_for_timeout(self.VAD_SILENCE_WAIT_MS)

    def wait_for_play_count(self, count, timeout=20000):
        self.page.wait_for_function(
            'n => { const s = window.VT.controlState(); return !!s && s.stats.playReceived >= n; }',
            arg=count, timeout=timeout)

    # --------------------------------------------------------------- tests

    def test_three_round_path_plays_observes_advances_and_completes(self):
        self.prepare_fixed_run()

        self.suspect_response()
        self.wait_for_play_count(2)
        self.suspect_response()
        self.wait_for_play_count(3)
        self.suspect_response()

        self.wait_for_status('测试完成')
        self.assertIn('all_phrases_done', self.status_text())

        state = self.state()
        self.assertEqual(state['stats']['playReceived'], 3)
        self.assertEqual(state['stats']['playbackStarted'], 3)
        self.assertEqual(state['stats']['playbackEnded'], 3)
        self.assertEqual(state['stats']['observations'], 6)  # start + end, three times
        self.assertEqual(state['stats']['timeouts'], 0)
        self.assertEqual(state['stats']['failures'], 0)
        self.assertTrue(state['finished'])

        record = self.record(state['session_id'])
        kinds = [event['kind'] for event in record['events']]
        self.assertEqual(kinds.count('play_issued'), 3)
        self.assertEqual(kinds.count('playback_started'), 3)
        self.assertEqual(kinds.count('playback_ended'), 3)
        self.assertEqual(kinds.count('observation_speech_start'), 3)
        self.assertEqual(kinds.count('observation_speech_end'), 3)
        self.assertIn('session_completed', kinds)

        run = [item for item in record['runs'] if item['current']][0]
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(len(run['turns']), 3)
        for index, turn in enumerate(run['turns']):
            self.assertEqual(turn['text'], self.PHRASES[index])
            self.assertTrue(turn['play_issued_at'])
            self.assertTrue(turn['playback_started_at'])
            self.assertEqual(turn['observation_basis'], 'browser_vad_rms')
            self.assertEqual(turn['closure_reason'], 'observation_speech_end')
            self.assertTrue(turn['closed'])

        blob = json.dumps(record, ensure_ascii=False)
        for forbidden in ('latency_ms', 'response_latency', 'speaker_role'):
            self.assertNotIn(forbidden, blob)

    def test_stop_during_playback_stops_audio_and_a_late_ended_does_not_resume(self):
        # A deliberately long first phrase removes any timing race between the
        # test pressing Stop and the audio reaching its natural end.
        phrases = ['[slow] 第一句：请讲一个很长的故事', '第二句：南京天气', '第三句：北京呢']
        self.prepare_fixed_run(phrases=phrases)
        self.wait_for_playing()
        before = self.state()
        self.assertTrue(before['has_audio'], 'audio should still be playing')
        # the control layer is waiting on this playback to finish
        self.assertTrue(before['pending_play_wait'], 'a playback wait should be outstanding')
        self.page.click('#vt-stop-fixed')

        self.wait_for_status('已停止')
        state = self.state()
        self.assertTrue(state['cancelled'])
        self.assertFalse(state['has_audio'])
        self.assertFalse(state['listening'])
        # stopping must END the playback wait, not merely silence the element
        self.assertFalse(state['pending_play_wait'], 'the playback wait must be finished')
        self.assertEqual(state['stats']['playWaitSettled'], 1,
                         'the wait must be settled exactly once')
        self.assertEqual(state['stats']['playWaitCancelled'], 1)
        self.assertFalse(self.page.is_disabled('#vt-start-fixed'))
        self.assertTrue(self.page.is_disabled('#vt-stop-fixed'))

        # a late `ended` from the cancelled audio must not restart listening,
        # advance the run, nor settle the same wait a second time
        self.page.wait_for_timeout(9000)
        after = self.state()
        self.assertEqual(after['stats']['playReceived'], 1)
        self.assertEqual(after['stats']['observations'], 0)
        self.assertFalse(after['listening'])
        self.assertEqual(after['stats']['playWaitSettled'], 1)
        self.assertEqual(after['stats']['playWaitCancelled'], 1)
        self.assertFalse(after['pending_play_wait'])
        self.assertIn('已停止', self.status_text())
        self.assertNotIn('等待设备回答', self.status_text())

        # calling stop again is harmless (the button itself is disabled, so this
        # exercises the handler the way a second click or a stale event would)
        before = self.state()
        self.page.evaluate('VT.stopFixedTest()')
        self.page.wait_for_timeout(300)
        repeated = self.state()
        self.assertTrue(repeated['cancelled'])
        self.assertEqual(repeated['stats']['playReceived'], before['stats']['playReceived'])
        self.assertIn('已停止', self.status_text())

        record = self.record(state['session_id'])
        self.assertEqual(record['status'], 'stopped')
        self.assertEqual(record['stop_reason'], 'user_stop')
        self.assertEqual(record['current_phrase_index'], 0)
        turn = [item for item in record['runs'] if item['current']][0]['turns'][0]
        self.assertEqual(turn['status'], 'cancelled')
        self.assertEqual(turn['closure_reason'], 'user_stop')
        # the audio was cancelled while still playing: the browser never
        # reported a natural end, so a late `ended` cannot be mistaken for one
        self.assertIsNone(turn['playback_ended_at'])
        kinds = [e['kind'] for e in record['events']]
        self.assertIn('playback_cancelled', kinds)
        self.assertNotIn('playback_ended', kinds)
        self.assertNotIn('observation_speech_end', kinds)

    def test_stop_while_waiting_for_a_response_does_not_advance(self):
        self.prepare_fixed_run()
        self.wait_for_listening()

        self.page.click('#vt-stop-fixed')
        self.wait_for_status('已停止')

        self.page.wait_for_timeout(1500)
        after = self.state()
        self.assertEqual(after['stats']['playReceived'], 1)
        self.assertFalse(after['listening'])

        record = self.record(after['session_id'])
        self.assertEqual(record['stop_reason'], 'user_stop')
        self.assertEqual(record['current_phrase_index'], 0)
        self.assertEqual(len([item for item in record['runs'] if item['current']][0]['turns']), 1)

    def test_no_response_timeout_is_a_timeout_not_an_answer(self):
        self.prepare_fixed_run()
        self.wait_for_listening()

        # never raise the synthetic mic: the device stays silent
        self.wait_for_status('未观察到设备回答', timeout=self.NO_RESPONSE_TIMEOUT_MS + 15000)
        self.wait_for_status('已停止')

        state = self.state()
        self.assertEqual(state['stats']['timeouts'], 1)
        self.assertEqual(state['stats']['observations'], 0)
        self.assertEqual(state['stats']['playReceived'], 1)
        self.assertFalse(state['listening'])
        self.assertNotIn('测试完成', self.status_text())

        record = self.record(state['session_id'])
        self.assertEqual(record['stop_reason'], 'no_response_timeout')
        turn = [item for item in record['runs'] if item['current']][0]['turns'][0]
        self.assertEqual(turn['observation'], 'no_response')
        self.assertEqual(turn['closure_reason'], 'no_response_timeout')
        kinds = [event['kind'] for event in record['events']]
        self.assertIn('observation_timeout', kinds)
        self.assertNotIn('observation_speech_start', kinds)
        self.assertNotIn('observation_speech_end', kinds)

    def test_audio_failure_exits_and_the_run_can_be_restarted(self):
        self.page.goto(f"{_STATE['base']}/")
        self.page.click('#nav-voice-test')
        self.page.fill('#vt-phrases', '\n'.join(self.PHRASES))
        self.page.context.route('**/audio/*', lambda route: route.abort())
        self.page.get_by_role('button', name='生成语音').click()
        self.page.wait_for_function(
            "() => { const el = document.getElementById('vt-generation-progress');"
            " return !!el && el.textContent.includes('已生成 3/3'); }")
        self.page.click('#vt-start-fixed')

        self.wait_for_status('测试中断')
        state = self.state()
        self.assertTrue(state['cancelled'])
        self.assertFalse(state['listening'])
        self.assertFalse(self.page.is_disabled('#vt-start-fixed'))
        self.assertIn('可重新开始', self.status_text())

        record = self.record(state['session_id'])
        kinds = [e['kind'] for e in record['events']]
        self.assertIn('playback_failed', kinds)
        self.assertIn('session_failed', kinds)

        # the same page can start a new run once the audio is reachable again
        self.page.context.unroute('**/audio/*')
        self.page.click('#vt-start-fixed')
        self.wait_for_play_count(1)
        self.assertEqual(self.state()['stats']['playReceived'], 1)

    def test_lost_session_prompts_a_restart_instead_of_hanging(self):
        self.prepare_fixed_run()
        session_id = self.state()['session_id']

        # simulate a backend restart: the in-memory session disappears
        self.api(f'/__test__/forget/{session_id}', method='POST')
        self.page.click('#vt-stop-fixed')
        self.wait_for_status('已停止')

        # the next attempt cannot find the session and must say so
        self.page.click('#vt-start-fixed')
        try:
            self.wait_for_status('会话已失效', timeout=15000)
        except Exception:
            raise AssertionError(
                f"no session-lost message: state={self.state()} status={self.status_text()!r}")
        self.assertFalse(self.page.is_disabled('#vt-start-fixed'))

    def test_each_restart_is_a_new_run_and_keeps_the_previous_one(self):
        self.prepare_fixed_run()
        self.suspect_response()
        self.wait_for_play_count(2)
        session_id = self.state()['session_id']
        first_turn_id = self.state()['turn_id']

        self.page.click('#vt-stop-fixed')
        self.wait_for_status('已停止')
        self.page.wait_for_timeout(400)

        self.page.click('#vt-start-fixed')
        self.wait_for_play_count(1)
        second = self.state()
        self.assertEqual(second['stats']['playReceived'], 1)  # counters belong to the new run
        self.assertNotEqual(second['turn_id'], first_turn_id)
        self.assertGreater(second['seq'], 1)

        self.page.click('#vt-stop-fixed')
        self.wait_for_status('已停止')

        record = self.record(session_id)
        self.assertEqual(len(record['runs']), 2)
        archived, current = record['runs']
        self.assertFalse(archived['current'])
        self.assertEqual(archived['run_index'], 1)
        self.assertEqual(len(archived['turns']), 2)  # turn 0 closed, turn 1 cancelled
        self.assertTrue(current['current'])
        self.assertEqual(current['run_index'], 2)


if __name__ == '__main__':
    unittest.main()
