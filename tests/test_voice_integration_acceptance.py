"""Targeted integration acceptance on the **real page** (PRD-F021/F023).

These are the A–E cases of the integration round, driven through the real
``voice_test.js`` control layer, the real WebSocket/binary-audio protocol and the
real backend routes, with controlled (deterministic, non-cloud) providers.

Each case runs against **its own server profile**, because the variable under
test is *which capability is missing*:

=====================  ==========================================================
profile                what the server has
=====================  ==========================================================
``full``               scripted Streaming ASR + File ASR + LLM + TTS
``streaming-only``     scripted Streaming ASR, **no File ASR at all**
``no-asr``             LLM + TTS, **no ASR of either family**
``turn-file``          scripted File ASR, no Streaming ASR
``turn-file-fail``     a File ASR that always fails
=====================  ==========================================================

Everything here is ``software_verified`` + ``browser_verified`` with controlled
inputs. It is not real-cloud, real-speaker, real-microphone or real-device
evidence: the recogniser/LLM are stand-ins and the microphone is a synthetic
oscillator installed by the test. The page, the transport, the control logic and
the backend routes under test are the real ones — and when this module runs
inside the candidate image, they are the image's own files.

Set ``VT_ACCEPTANCE_PROFILE_BASES`` to a JSON object mapping each profile name to
an already-running server (this is how the release job drives servers started
**inside the candidate image**, one per profile). Without it, the module starts
its own servers from the current checkout.
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
PROFILE_BASES_ENV = os.environ.get('VT_ACCEPTANCE_PROFILE_BASES', '').strip()
REQUIRE_BROWSER = os.environ.get('VT_REQUIRE_BROWSER', '').strip() not in ('', '0', 'false', 'False')
PROFILES = ('full', 'streaming-only', 'no-asr', 'turn-file', 'turn-file-fail')
# The profile that carries the shortened observation-round bound used by case C.
FULL_PROFILE_ROUND_MAX_MS = 6000

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORT_ERROR = None
except Exception as error:  # pragma: no cover - environment dependent
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = str(error)


def _unavailable(message):
    if REQUIRE_BROWSER:
        raise RuntimeError(f'browser acceptance required but unavailable: {message}')
    raise unittest.SkipTest(message)


# Same controlled synthetic microphone as the control tests. Marked as test input.
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


def _get(base, path):
    with urllib.request.urlopen(f'{base}{path}', timeout=10) as response:
        return json.loads(response.read().decode())


def _wait_for_health(base, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return _get(base, '/health')
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f'server at {base} never became ready')


_STATE = {}


def _start_server(profile, *, round_max_ms=None, first_phrase_ms=1200):
    port = _free_port()
    log = open(Path(_STATE['tmp'].name) / f'server-{profile}.log', 'w+', encoding='utf-8')
    _STATE['logs'].append(log)
    argv = [sys.executable, str(REPO_ROOT / 'tests' / 'browser_server.py'),
            '--port', str(port), '--output', str(Path(_STATE['tmp'].name) / f'out-{profile}'),
            '--first-phrase-ms', str(first_phrase_ms), '--phrase-ms', '400',
            '--profile', profile]
    if round_max_ms:
        argv += ['--round-max-ms', str(round_max_ms)]
    proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT)
    _STATE['servers'].append(proc)
    _STATE['server_logs'][profile] = log
    base = f'http://127.0.0.1:{port}'
    deadline = time.time() + 40
    while time.time() < deadline:
        if proc.poll() is not None:
            log.flush()
            raise RuntimeError(f'{profile} server exited early: '
                               f'{Path(log.name).read_text(encoding="utf-8")[-1500:]}')
        try:
            _wait_for_health(base, timeout=2)
            break
        except Exception:
            time.sleep(0.25)
    else:
        log.flush()
        raise RuntimeError(f'{profile} server never became ready: '
                           f'{Path(log.name).read_text(encoding="utf-8")[-1500:]}')
    return base


def setUpModule():
    if PLAYWRIGHT_IMPORT_ERROR is not None:
        _unavailable(f'playwright unavailable: {PLAYWRIGHT_IMPORT_ERROR}')
    _STATE['tmp'] = tempfile.TemporaryDirectory()
    _STATE['servers'] = []
    _STATE['logs'] = []
    _STATE['server_logs'] = {}
    external = {}
    if PROFILE_BASES_ENV:
        try:
            external = json.loads(PROFILE_BASES_ENV)
        except ValueError as error:
            raise RuntimeError(f'VT_ACCEPTANCE_PROFILE_BASES is not valid JSON: {error}') from None
        missing = [name for name in PROFILES if not external.get(name)]
        if missing:
            raise RuntimeError('VT_ACCEPTANCE_PROFILE_BASES is missing: ' + ', '.join(missing))
    for name in PROFILES:
        if external:
            base = external[name].rstrip('/')
            try:
                _wait_for_health(base, timeout=30)
            except Exception as error:
                raise RuntimeError(f'{name} server at {base} is not ready: {error}') from None
            _STATE[name] = base
        elif name == 'full':
            _STATE[name] = _start_server('full', round_max_ms=FULL_PROFILE_ROUND_MAX_MS)
        else:
            _STATE[name] = _start_server(name)
    _STATE['playwright'] = sync_playwright().start()
    last_error = None
    for channel in ('chrome', 'msedge', None):
        try:
            _STATE['browser'] = (_STATE['playwright'].chromium.launch(channel=channel, headless=True,
                                 args=['--autoplay-policy=no-user-gesture-required',
                                       '--use-fake-ui-for-media-stream',
                                       '--use-fake-device-for-media-stream'])
                                 if channel else
                                 _STATE['playwright'].chromium.launch(headless=True,
                                 args=['--autoplay-policy=no-user-gesture-required',
                                       '--use-fake-ui-for-media-stream',
                                       '--use-fake-device-for-media-stream']))
            _STATE['channel'] = channel or 'chromium'
            break
        except Exception as error:  # pragma: no cover - environment dependent
            last_error = error
    else:
        _STATE['playwright'].stop()
        _unavailable(f'no Chromium-based browser available: {last_error}')
    print(f"[integration acceptance] profiles={list(PROFILES)} browser={_STATE['channel']} "
          f"external_servers={bool(external)}", flush=True)


def tearDownModule():
    browser = _STATE.pop('browser', None)
    if browser is not None:
        browser.close()
    playwright = _STATE.pop('playwright', None)
    if playwright is not None:
        playwright.stop()
    for proc in _STATE.pop('servers', []):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    for log in _STATE.pop('logs', []):
        log.close()
    tmp = _STATE.pop('tmp', None)
    if tmp is not None:
        tmp.cleanup()


class IntegrationAcceptanceTestCase(unittest.TestCase):
    PROFILE = 'full'

    def setUp(self):
        self.context = _STATE['browser'].new_context()
        self.addCleanup(self.context.close)
        self.context.add_init_script(CONTROLLED_MIC_INIT)
        self.page = self.context.new_page()
        self.page.set_default_timeout(20000)
        self.base = _STATE[self.PROFILE]
        if not PROFILE_BASES_ENV:
            self._assert_server_alive()

    def _assert_server_alive(self):
        """Fail with the server's own log instead of a bare connection error."""
        log = _STATE['server_logs'].get(self.PROFILE)
        if log is None:
            return
        log.flush()
        text = Path(log.name).read_text(encoding='utf-8', errors='replace')
        if 'ready' not in text or 'Traceback' in text or 'Error' in text:
            self.fail(f"{self.PROFILE} server log:\n{text[-3000:]}")
        try:
            _wait_for_health(self.base, timeout=3)
        except Exception as error:
            self.fail(f"{self.PROFILE} server is not answering ({error}); log:\n{text[-3000:]}")

    # ------------------------------------------------------------- helpers

    def open_free_mode(self, capture_mode=None, max_turns=None):
        self.page.goto(f'{self.base}/')
        self.page.click('#nav-voice-test')
        self.page.click('text=自由对话（大模型）')
        if capture_mode:
            self.page.select_option('#vt-free-capture-mode', capture_mode)
        if max_turns is not None:
            # The operator's own control: how many complete rounds to run.
            self.page.fill('#vt-free-max-turns', str(max_turns))
        self.page.fill('#vt-free-goal', '测试设备的天气查询能力')
        return self.page

    def state(self):
        return self.page.evaluate('window.VT.controlState()')

    def scripted(self):
        return _get(self.base, '/__test__/scripted')

    def wait_for_play_count(self, count, timeout=30000):
        self.page.wait_for_function(
            'n => { const s = window.VT.controlState(); return !!s && s.stats.playReceived >= n; }',
            arg=count, timeout=timeout)

    def wait_for_listening(self, timeout=25000):
        self.page.wait_for_function(
            '() => { const s = window.VT.controlState(); return !!s && s.listening === true; }',
            timeout=timeout)

    def wait_for_capture_ready(self, timeout=25000):
        """The streaming capture socket is open and the worklet is streaming.

        Raising the controlled microphone before this point would produce a round
        with no audio frames, which is a different case from the one under test.
        """
        self.page.wait_for_function(
            "() => { const el = document.getElementById('vt-capture-status');"
            " return !!el && el.textContent.includes('实时识别中'); }", timeout=timeout)

    def speak_and_stop(self, *, level=1.0, silence_level=0.0, speak_ms=800, silence_ms=2600,
                       streaming=False):
        """Raise the controlled mic, then let it fall back: a suspected answer."""
        if streaming:
            self.wait_for_capture_ready()
        self.wait_for_listening()
        self.page.evaluate(f'window.__vtMic.setLevel({level})')
        self.page.wait_for_timeout(speak_ms)
        self.page.evaluate(f'window.__vtMic.setLevel({silence_level})')
        self.page.wait_for_timeout(silence_ms)

    def record(self, session_id):
        return _get(self.base, f'/__test__/record/{session_id}')

    def server_state(self, session_id):
        """The server's own session snapshot (carries the provider-call audit)."""
        return _get(self.base, f'/__test__/state/{session_id}')


class MissingAsrRefusalTests(IntegrationAcceptanceTestCase):
    """Case A: no ASR at all -> the page and the server both refuse, with no call."""

    PROFILE = 'no-asr'

    def test_page_refuses_before_the_microphone_is_raised(self):
        self.open_free_mode('streaming')
        self.page.click('#vt-start-free')
        self.page.wait_for_function(
            "() => document.getElementById('vt-notice').textContent.includes('尚不能启动')")
        notice = self.page.inner_text('#vt-notice')
        self.assertIn('实时语音识别', notice)
        self.assertIn('未发起模型或语音调用', notice)
        # No run was created and the microphone was never requested.
        self.assertIsNone(self.state())
        self.assertEqual(self.page.evaluate('window.__vtMic.calls()'), 0)
        self.assertEqual(self.scripted()['tts_call_count'], 0)

    def test_direct_start_paths_cannot_bypass_the_precheck(self):
        """The page's own origin tries the endpoint and the socket directly."""
        self.page.goto(f'{self.base}/')
        outcome = self.page.evaluate(
            """async () => {
              const base = location.origin;
              const created = await fetch(`${base}/api/voice-test/sessions`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode: 'free', goal: '绕过检查', capture_mode: 'streaming'}),
              });
              const session = await created.json();
              const started = await fetch(
                `${base}/api/voice-test/sessions/${session.session_id}/start`, {method: 'POST'});
              const detail = await started.json();
              const message = await new Promise((resolve) => {
                const ws = new WebSocket(
                  `ws://${location.host}/api/voice-test/sessions/${session.session_id}/ws`);
                ws.onopen = () => ws.send(JSON.stringify({type: 'start'}));
                ws.onmessage = (event) => { resolve(JSON.parse(event.data)); ws.close(); };
                ws.onerror = () => resolve({type: 'socket_error'});
              });
              return {status: started.status, detail, message, session_id: session.session_id};
            }""")
        self.assertEqual(outcome['status'], 409, outcome)
        self.assertEqual(outcome['detail']['detail']['reason'], 'capability_precheck_failed')
        self.assertIn('streaming_asr', outcome['detail']['detail']['missing'])
        self.assertEqual(outcome['message']['type'], 'blocked')
        self.assertIn('streaming_asr', outcome['message']['missing'])

        state = _get(self.base, f"/__test__/state/{outcome['session_id']}")
        self.assertEqual(state['provider_calls'], {'tts': 0, 'llm': 0, 'asr': 0})
        self.assertEqual(state['turns'], [])
        self.assertEqual(state['run_index'], 0)
        self.assertEqual(self.scripted()['tts_call_count'], 0)
        record = self.record(outcome['session_id'])
        self.assertIn('session_start_refused', [e['kind'] for e in record['events']])
        self.assertNotIn('play_issued', [e['kind'] for e in record['events']])


class StreamingWithoutSignedUrlTests(IntegrationAcceptanceTestCase):
    """Case B: Streaming ASR is not blocked by the File ASR publication route."""

    PROFILE = 'streaming-only'

    def test_streaming_run_needs_no_file_asr_or_signed_url(self):
        self.open_free_mode('streaming')
        self.page.click('#vt-start-free')
        self.wait_for_play_count(1)
        state = self.state()
        self.assertEqual(state['capture_mode'], 'streaming')
        self.assertIn('实时', self.page.inner_text('#vt-capture-status'))

        # The confirmed label is shown when the capture is finalised; a later
        # round's live partial replaces it, so sample it instead of racing for it.
        self.page.evaluate(
            """() => {
              window.__vtSeenConfirmed = false;
              window.__vtConfirmedText = '';
              setInterval(() => {
                const el = document.getElementById('vt-device-transcript');
                if (el && el.textContent.includes('设备（确认）')) {
                  window.__vtSeenConfirmed = true;
                  window.__vtConfirmedText = el.textContent;
                }
              }, 50);
            }""")

        self.speak_and_stop(streaming=True)
        self.wait_for_play_count(2, timeout=40000)

        self.assertTrue(self.page.evaluate('window.__vtSeenConfirmed'),
                        'the page never showed a confirmed streaming transcript')
        self.assertIn('设备回答：南京明天晴', self.page.evaluate('window.__vtConfirmedText'))

        scripted = self.scripted()
        self.assertEqual(scripted['file_asr_calls'], [],
                         'the streaming path must not upload or file-recognise anything')
        self.assertGreater(scripted['streaming_audio_bytes'], 0,
                           'the page must really have streamed microphone audio')
        record = self.record(self.state()['session_id'])
        self.assertEqual(record['resolved_capture_mode'], 'streaming')
        self.assertIsNone(record['capture_fallback_reason'])
        observation = [e for e in record['events'] if e['kind'] == 'device_observation'][0]
        self.assertEqual(observation['detail']['capture_mode'], 'streaming')
        self.assertEqual(observation['detail']['final_source'], 'volcengine_streaming_asr')
        self.assertEqual(observation['detail']['text'], '设备回答：南京明天晴')
        finished = [e for e in record['events'] if e['kind'] == 'capture_finished']
        self.assertEqual(finished[0]['detail']['final_text'], '设备回答：南京明天晴')
        self.assertEqual(finished[0]['detail']['final_basis'], 'provider_endpoint')
        self.assertGreater(finished[0]['detail']['audio_bytes'], 0)


class FixedNoiseHandlingTests(IntegrationAcceptanceTestCase):
    """Case C: the corrected time-domain criterion and the bounded round exit."""

    PROFILE = 'full'
    PHRASES = ['第一句：你好', '第二句：南京天气', '第三句：北京呢']

    def prepare(self):
        self.page.goto(f'{self.base}/')
        self.page.click('#nav-voice-test')
        self.page.fill('#vt-phrases', '\n'.join(self.PHRASES))
        self.page.get_by_role('button', name='生成语音').click()
        self.page.wait_for_function(
            "() => { const el = document.getElementById('vt-generation-progress');"
            " return !!el && el.textContent.includes('已生成 3/3'); }")
        self.page.click('#vt-start-fixed')
        self.wait_for_play_count(1)

    def test_residual_noise_floor_still_ends_the_turn(self):
        self.prepare()
        # A real room is never digitally silent. Establish a non-zero floor, give
        # an answer above it, then fall back to the floor: the turn must end.
        self.wait_for_listening()
        self.page.evaluate('window.__vtMic.setLevel(0.05)')
        self.page.wait_for_timeout(1600)
        self.page.evaluate('window.__vtMic.setLevel(1.0)')
        self.page.wait_for_timeout(800)
        self.page.evaluate('window.__vtMic.setLevel(0.05)')

        self.wait_for_play_count(2, timeout=30000)
        state = self.state()
        self.assertEqual(state['stats']['timeouts'], 0)

        record = self.record(state['session_id'])
        # The thresholds the round actually used, as recorded at calibration time.
        diagnostics = [e for e in record['events'] if e['kind'] == 'vad_diagnostics'][0]
        self.assertEqual(diagnostics['source'], 'browser')
        self.assertEqual(diagnostics['detail']['evidence_scope'], 'control_evidence')
        self.assertEqual(diagnostics['detail']['baseline_basis'], 'measured_noise_floor')
        self.assertGreater(diagnostics['detail']['baseline'], 0.004)
        self.assertGreater(diagnostics['detail']['end_threshold'],
                           diagnostics['detail']['baseline'])

        turn = [item for item in record['runs'] if item['current']][0]['turns'][0]
        self.assertEqual(turn['closure_reason'], 'observation_speech_end')
        self.assertEqual(turn['observation'], 'speech_end')

    def test_sustained_noise_exits_at_the_control_bound_without_a_fake_answer(self):
        self.prepare()
        # The input never falls back: speech was detected, its end cannot be
        # confirmed, so control must exit explicitly rather than invent an answer.
        self.wait_for_listening()
        self.page.evaluate('window.__vtMic.setLevel(1.0)')
        self.page.wait_for_function(
            "() => { const s = window.VT.controlState();"
            " return !!s && s.stats.timeouts >= 1; }", timeout=30000)

        state = self.state()
        record = self.record(state['session_id'])
        timeouts = [e for e in record['events'] if e['kind'] == 'observation_timeout']
        self.assertEqual(timeouts[-1]['detail']['reason'], 'cannot_confirm_response_end')
        turn = [item for item in record['runs'] if item['current']][0]['turns'][0]
        self.assertEqual(turn['observation'], 'no_response')
        self.assertEqual(turn['closure_reason'], 'observation_end_unconfirmed')
        self.assertNotEqual(turn['status'], 'complete')
        self.assertEqual(state['stats']['playReceived'], 1,
                         'an unconfirmable answer end must not advance the run')

    def wait_for_function_contains(self, selector, needle, timeout=20000):
        self.page.wait_for_function(
            'args => { const el = document.querySelector(args[0]);'
            ' return !!el && el.textContent.includes(args[1]); }',
            arg=[selector, needle], timeout=timeout)

class FileDowngradeTests(IntegrationAcceptanceTestCase):
    """Case D: the explicitly chosen downgrade decodes, converts and fails loudly."""

    PROFILE = 'turn-file'

    def test_downgrade_decodes_browser_audio_before_file_asr(self):
        self.open_free_mode('turn_file')
        self.page.click('#vt-start-free')
        self.wait_for_play_count(1)
        self.assertEqual(self.state()['capture_mode'], 'turn_file')
        self.assertIn('降级', self.page.inner_text('#vt-capture-status'))
        self.assertIn('不是实时', self.page.inner_text('#vt-capture-status'))

        self.speak_and_stop(silence_ms=3200)
        self.wait_for_play_count(2, timeout=40000)

        calls = self.scripted()['file_asr_calls']
        self.assertTrue(calls, 'File ASR was never called on the uploaded recording')
        self.assertEqual(calls[0]['sample_rate'], 16000)
        self.assertEqual(calls[0]['channels'], 1)
        self.assertEqual(calls[0]['sample_width'], 2)
        self.assertGreater(calls[0]['frames'], 0)

        state = self.state()
        record = self.record(state['session_id'])
        self.assertEqual(record['resolved_capture_mode'], 'turn_file')
        self.assertEqual(record['capture_fallback_reason'], 'configured_turn_file')
        observation = [e for e in record['events'] if e['kind'] == 'device_observation'][0]
        self.assertEqual(observation['detail']['capture_mode'], 'turn_file')
        self.assertEqual(observation['detail']['final_basis'], 'file_asr_fallback')


class FileDowngradeFailureTests(IntegrationAcceptanceTestCase):
    """Case D (failure): recognition failure is a failure, not an empty answer."""

    PROFILE = 'turn-file-fail'

    def test_recognition_failure_does_not_advance_the_conversation(self):
        self.open_free_mode('turn_file')
        self.page.click('#vt-start-free')
        self.wait_for_play_count(1)
        self.speak_and_stop(silence_ms=3200)

        self.page.wait_for_function(
            "() => { const el = document.getElementById('vt-status');"
            " return !!el && el.textContent.includes('测试中断'); }", timeout=40000)
        state = self.state()
        record = self.record(state['session_id'])
        self.assertEqual(record['status'], 'failed')
        self.assertIn('device_audio_failed', record['stop_reason'])
        kinds = [e['kind'] for e in record['events']]
        self.assertIn('device_audio_failed', kinds)
        self.assertNotIn('device_observation', kinds)
        finished = [e for e in record['events'] if e['kind'] == 'fallback_upload_finished']
        self.assertEqual(finished[-1]['detail']['status'], 'asr_failed')
        turn = [item for item in record['runs'] if item['current']][0]['turns'][0]
        self.assertEqual(turn['status'], 'failed')
        self.assertNotIn(turn['status'], ('complete', 'observed'))
        self.assertEqual(len([e for e in record['events'] if e['kind'] == 'play_issued']), 1)


class FreeModeThreeRoundTests(IntegrationAcceptanceTestCase):
    """Case E: three consecutive rounds through the real page and transport."""

    PROFILE = 'full'

    def test_three_consecutive_rounds_use_the_real_capture_path(self):
        self.open_free_mode('streaming', max_turns=3)
        self.page.click('#vt-start-free')
        self.wait_for_play_count(1)
        session_id = self.state()['session_id']

        for expected in (2, 3):
            self.speak_and_stop(streaming=True)
            self.wait_for_play_count(expected, timeout=40000)
            self.page.wait_for_function(
                "n => (document.getElementById('vt-free-log')||{}).textContent"
                ".split('平台').length - 1 >= n", arg=expected - 1, timeout=30000)

        # The third answer is observed, and only then does the turn budget end
        # the run: the last question is answered, not abandoned.
        self.speak_and_stop(streaming=True)
        self.page.wait_for_function(
            "() => { const el = document.getElementById('vt-status');"
            " return !!el && (el.textContent.includes('测试完成')"
            " || el.textContent.includes('已停止')); }", timeout=40000)

        state = self.state()
        self.assertEqual(state['stats']['playReceived'], 3)
        self.assertEqual(state['stats']['failures'], 0)
        record = self.record(session_id)
        self.assertEqual(record['resolved_capture_mode'], 'streaming')
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['stop_reason'], 'max_turns')
        self.assertIsNone(record['awaiting_turn_id'])
        turns = [item for item in record['runs'] if item['current']][0]['turns']
        roles = [turn['role'] for turn in turns]
        self.assertEqual(roles.count('platform'), 3, roles)
        self.assertEqual(roles.count('device'), 3, roles)
        self.assertTrue(all(turn['closed'] for turn in turns), turns)
        observations = [e for e in record['events'] if e['kind'] == 'device_observation']
        self.assertEqual(len(observations), 3)
        for observation in observations:
            self.assertEqual(observation['detail']['capture_mode'], 'streaming')
            self.assertTrue(observation['detail']['text'])
        # The device text really crossed into the agent's context.
        device_turns = [e for e in record['events'] if e['kind'] == 'device_transcript']
        self.assertEqual(len(device_turns), 3)
        scripted = self.scripted()
        self.assertEqual(scripted['file_asr_calls'], [])
        self.assertGreaterEqual(scripted['streaming_audio_bytes'], 3 * 3200,
                                'each round must have streamed real audio frames')
        # Exactly three questions were synthesized and sent: the budget did not
        # buy itself a hidden extra round, and the last answer was not skipped.
        audit = self.server_state(session_id)['provider_calls']
        self.assertEqual(audit['tts'], 3, audit)
        self.assertEqual([e['detail']['text'] for e in record['events']
                          if e['kind'] == 'play_issued'],
                         ['第1个问题', '第2个问题', '第3个问题'])


if __name__ == '__main__':
    unittest.main()
