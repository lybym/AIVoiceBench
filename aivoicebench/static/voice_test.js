/* AIVoiceBench — Active Voice Test (语音对话测试)
 *
 * Browser-side audio playback, microphone VAD, and WebSocket control.
 *
 * Fixed mode: user enters phrases, backend generates TTS, browser plays them
 *   in sequence, VAD detects a *suspected* device response, advances.
 *
 * Free mode: LLM agent generates phrases, browser plays, captures device
 *   audio, uploads for ASR, agent decides the next turn.
 *
 * Control rules (PRD-F020):
 * - Stopping is local and immediate. It does not wait for the backend, it
 *   cancels the current audio (so a late `ended` can never restart listening
 *   or advance the run), and it can be called repeatedly.
 * - Every browser report names the turn it belongs to. Reports for a closed
 *   turn, a stopped run, or an earlier run are dropped.
 * - A no-response timeout is reported as `observation_timeout`, never as the
 *   end of a response, so a silent device is not recorded as an answer.
 * - VAD only reports a *suspected* response. It does not identify the speaker
 *   and it does not measure a formal response latency.
 * - Bounded control waits keep a stuck audio element or a silent device from
 *   hanging a run. They are not product performance targets.
 */

const VT = (function () {
  let ws = null;
  let micStream = null;
  let audioContext = null;
  let analyser = null;
  let vadTimer = null;
  let vadState = 'idle'; // idle, listening, device_speaking
  let speechStartMs = 0;
  let silenceStartMs = 0;
  let session = null;
  let synthesisProgressTimer = null;
  let synthesisProgressPolling = false;

  // The active control run. Everything that can arrive late (WebSocket
  // messages, audio callbacks, VAD ticks) is validated against it.
  let activeRun = null;
  let runSeq = 0;

  // VAD thresholds (tunable)
  const VAD_THRESHOLD = 0.015; // RMS threshold for speech detection
  const VAD_SPEAK_MS = 300; // RMS must be above threshold for this long → speech start
  const VAD_SILENCE_MS = 1500; // RMS must be below threshold for this long → speech end
  const VAD_TIMEOUT_MS = 30000; // control bound: no suspected response within 30s

  // Bounded control waits (not performance targets).
  const PLAYBACK_START_TIMEOUT_MS = 10000;
  const PLAYBACK_MAX_DURATION_MS = 180000;

  function $(id) { return document.getElementById(id); }

  function notify(msg) {
    const el = $('vt-notice');
    if (el) { el.textContent = msg; el.hidden = !msg; }
  }

  function fixedGenerationElements() {
    const fixed = $('vt-fixed');
    const actions = fixed?.querySelector('.settings-actions');
    const button = $('vt-generate-fixed') || actions?.querySelector('button.primary');
    if (button) button.id = 'vt-generate-fixed';

    let progress = $('vt-generation-progress');
    if (!progress && actions) {
      progress = document.createElement('span');
      progress.id = 'vt-generation-progress';
      progress.className = 'quiet';
      progress.setAttribute('role', 'status');
      progress.setAttribute('aria-live', 'polite');
      progress.style.minHeight = '18px';
      actions.appendChild(progress);
    }
    return { button, progress };
  }

  function updateGenerationProgress(message) {
    const { progress } = fixedGenerationElements();
    if (progress) progress.textContent = message;
  }

  function setFixedGenerationBusy(busy) {
    const { button } = fixedGenerationElements();
    if (button) {
      button.disabled = busy;
      button.textContent = busy ? '正在生成…' : '生成语音';
    }
    ['vt-phrases', 'vt-device'].forEach(id => {
      const input = $(id);
      if (input) input.disabled = busy;
    });
  }

  function stopSynthesisProgress() {
    if (synthesisProgressTimer) clearInterval(synthesisProgressTimer);
    synthesisProgressTimer = null;
    synthesisProgressPolling = false;
  }

  function startSynthesisProgress(sessionId, total) {
    const startedAt = Date.now();
    const elapsed = () => Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    // A poll that is still in flight when synthesis finishes must not overwrite
    // the final status: `stopSynthesisProgress()` sets the timer to null, and
    // any response arriving after that is dropped.
    const cancelled = () => synthesisProgressTimer === null;
    const refresh = async () => {
      if (synthesisProgressPolling) return;
      synthesisProgressPolling = true;
      try {
        const response = await fetch(`/api/voice-test/sessions/${sessionId}`);
        if (cancelled()) return;
        if (response.ok) {
          const snapshot = await response.json();
          if (cancelled()) return;
          const complete = (snapshot.phrases || []).filter(p => p.status === 'ready').length;
          updateGenerationProgress(`正在调用 TTS 生成语音：${complete}/${total} 条已完成，已等待 ${elapsed()} 秒。`);
        }
      } catch (_) {
        if (cancelled()) return;
        updateGenerationProgress(`正在调用 TTS 生成语音，已等待 ${elapsed()} 秒；暂时无法读取进度。`);
      } finally {
        synthesisProgressPolling = false;
      }
    };
    updateGenerationProgress(`正在调用 TTS 生成语音：0/${total} 条已完成，已等待 0 秒。`);
    refresh();
    synthesisProgressTimer = setInterval(refresh, 800);
  }

  async function responseMessage(response) {
    try {
      const body = await response.json();
      return body.detail || body.message || `HTTP ${response.status}`;
    } catch (_) {
      const text = await response.text();
      return text || `HTTP ${response.status}`;
    }
  }

  // ------------------------------------------------------------------- run

  function newRun(sessionId, mode) {
    return {
      seq: ++runSeq,
      sessionId,
      mode,
      ws: null,
      opened: false,
      cancelled: false,
      finished: false,
      turnId: null,
      audio: null,
      // The playback currently being awaited. `cancelAudio()` ends it, so a
      // cancelled play never leaves an await (or its timers) pending.
      playback: null,
      pendingPlayWait: false,
      listening: false,
      bounds: {
        playback_start_timeout_ms: PLAYBACK_START_TIMEOUT_MS,
        playback_max_duration_ms: PLAYBACK_MAX_DURATION_MS,
        no_response_timeout_ms: VAD_TIMEOUT_MS,
      },
      stats: {
        playReceived: 0, playbackStarted: 0, playbackEnded: 0,
        observations: 0, timeouts: 0, ignored: 0, failures: 0, lateDropped: 0,
        playWaitSettled: 0, playWaitCancelled: 0,
      },
    };
  }

  function isActive(run) { return !!run && activeRun === run && !run.cancelled; }

  /* Read-only diagnostics of the control layer (used by tests and support). */
  function controlState() {
    if (!activeRun) return null;
    return {
      session_id: activeRun.sessionId,
      mode: activeRun.mode,
      seq: activeRun.seq,
      cancelled: activeRun.cancelled,
      finished: activeRun.finished,
      turn_id: activeRun.turnId,
      listening: activeRun.listening,
      has_audio: !!activeRun.audio,
      pending_play_wait: !!activeRun.pendingPlayWait,
      stats: Object.assign({}, activeRun.stats),
    };
  }

  function wsSend(run, payload) {
    if (!run || !run.ws || run.ws.readyState !== WebSocket.OPEN) return false;
    try {
      run.ws.send(JSON.stringify(payload));
      return true;
    } catch (_) {
      return false;
    }
  }

  function setRunningButtons(mode, running) {
    const start = $(mode === 'fixed' ? 'vt-start-fixed' : 'vt-start-free');
    const stop = $(mode === 'fixed' ? 'vt-stop-fixed' : 'vt-stop-free');
    if (start) start.disabled = running;
    if (stop) stop.disabled = !running;
  }

  function clearTimer(id) {
    if (id) clearTimeout(id);
    return null;
  }

  /* End the playback wait that is currently outstanding, exactly once.
   *
   * Stopping must finish the `await playAudio(...)` in `handlePlay`, not just
   * silence the audio element: otherwise the wait (and its timers) stay pending
   * forever. Returns true when a wait was actually ended by this call.
   */
  function cancelAudio(run, reason) {
    const playback = run && run.playback;
    if (!playback) {
      if (run) { run.audio = null; run.pendingPlayWait = false; }
      return false;
    }
    return playback.cancel(reason || 'cancelled');
  }

  function stopLocal(run, reason, options) {
    if (!run) return false;
    if (run.cancelled) return false; // idempotent
    run.cancelled = true;
    run.listening = false;
    // Ends the outstanding playback wait as well as the audio itself, so the
    // awaiting flow finishes instead of hanging.
    const hadAudio = cancelAudio(run, reason || 'user_stop');
    stopVAD();
    stopMic();
    if (hadAudio) {
      wsSend(run, { type: 'playback_cancelled', turn_id: run.turnId, reason: reason || 'user_stop' });
    }
    setRunningButtons(run.mode, false);
    if (!(options && options.silent)) {
      updateStatus(reason ? `已停止：${reason}` : '已停止');
    }
    return true;
  }

  function closeSocket(run) {
    const socket = run && run.ws;
    if (!socket) return;
    run.ws = null;
    try {
      socket.onmessage = null; socket.onclose = null; socket.onerror = null;
      socket.close();
    } catch (_) { /* ignore */ }
  }

  function stopRun(run) {
    if (!run) return;
    if (!stopLocal(run, null)) return; // already stopped: repeated clicks do nothing
    // Best effort only: a missing backend must never keep the UI from stopping.
    wsSend(run, { type: 'stop' });
    const socket = run.ws;
    setTimeout(() => { if (run.ws === socket) closeSocket(run); }, 250);
  }

  function failRun(run, reason) {
    if (!run || run.cancelled) return;
    run.stats.failures += 1;
    const labels = {
      audio_error: '音频资产不可用或无法解码',
      play_rejected: '浏览器拒绝播放',
      playback_did_not_start: '音频未开始播放',
      playback_stuck: '音频未在控制时限内结束播放',
      socket_closed: '连接已断开（后端可能已重启）',
      session_lost: '会话已失效（后端可能已重启）',
    };
    const text = labels[reason] || reason || '未知原因';
    stopLocal(run, null, { silent: true });
    run.finished = true;
    updateStatus(`测试中断：${text}。可重新开始。`);
    notify(`固定对话已中断：${text}。可重新开始。`);
    closeSocket(run);
  }

  // --------------------------------------------------------------- playback

  function playAudio(run, url) {
    return new Promise((resolve) => {
      stopVAD(); // do not listen to our own playback
      cancelAudio(run, 'replaced'); // end any previous wait exactly once

      const audio = new Audio(url);
      let settled = false;
      let startTimer = null;
      let maxTimer = null;

      const playback = {
        audio,
        // Settles the wait once: clears both timers and resolves the promise.
        settle(outcome, detail) {
          if (settled) return false;
          settled = true;
          startTimer = clearTimer(startTimer);
          maxTimer = clearTimer(maxTimer);
          if (run.playback === playback) {
            run.playback = null;
            run.audio = null;
          }
          run.pendingPlayWait = false;
          run.stats.playWaitSettled += 1;
          if (outcome === 'cancelled') run.stats.playWaitCancelled += 1;
          resolve({ outcome, detail });
          return true;
        },
        // Detach handlers first: a late `ended` must not restart listening or
        // advance the run after a stop.
        cancel(reason) {
          audio.onended = null;
          audio.onerror = null;
          audio.onplaying = null;
          try { audio.pause(); } catch (_) { /* ignore */ }
          try { audio.removeAttribute('src'); audio.load(); } catch (_) { /* ignore */ }
          return playback.settle('cancelled', reason);
        },
      };
      run.playback = playback;
      run.audio = audio;
      run.pendingPlayWait = true;

      const ownsAudio = () => isActive(run) && run.playback === playback;

      audio.onplaying = () => {
        if (!ownsAudio()) { run.stats.lateDropped += 1; return; }
        run.stats.playbackStarted += 1;
        wsSend(run, { type: 'playback_started', turn_id: run.turnId });
      };

      audio.onended = () => {
        // A late `ended` after a stop finds the wait already settled and does
        // nothing: no listening, no advance.
        if (!ownsAudio()) { run.stats.lateDropped += 1; return; }
        run.stats.playbackEnded += 1;
        wsSend(run, { type: 'playback_ended', turn_id: run.turnId });
        playback.settle('ended');
      };

      audio.onerror = () => {
        if (!ownsAudio()) { run.stats.lateDropped += 1; return; }
        wsSend(run, { type: 'playback_failed', turn_id: run.turnId, reason: 'audio_error' });
        playback.settle('failed', 'audio_error');
      };

      const startBound = Number(run.bounds.playback_start_timeout_ms) || PLAYBACK_START_TIMEOUT_MS;
      const maxBound = Number(run.bounds.playback_max_duration_ms) || PLAYBACK_MAX_DURATION_MS;

      startTimer = setTimeout(() => {
        if (settled || !isActive(run) || run.playback !== playback) return;
        if (audio.currentTime > 0 || !audio.paused) return;
        wsSend(run, { type: 'playback_failed', turn_id: run.turnId, reason: 'playback_did_not_start' });
        playback.settle('failed', 'playback_did_not_start');
      }, startBound);

      maxTimer = setTimeout(() => {
        if (settled || !isActive(run) || run.playback !== playback) return;
        audio.onended = null;
        try { audio.pause(); } catch (_) { /* ignore */ }
        wsSend(run, { type: 'playback_failed', turn_id: run.turnId, reason: 'playback_stuck' });
        playback.settle('failed', 'playback_stuck');
      }, maxBound);

      audio.play().catch(() => {
        if (!ownsAudio()) { run.stats.lateDropped += 1; return; }
        wsSend(run, { type: 'playback_failed', turn_id: run.turnId, reason: 'play_rejected' });
        playback.settle('failed', 'play_rejected');
      });
    });
  }

  let previewAudioElement = null;

  function previewAudio(url) {
    if (previewAudioElement) {
      try { previewAudioElement.pause(); } catch (_) { /* ignore */ }
    }
    const audio = new Audio(url);
    previewAudioElement = audio;
    audio.play().catch(() => notify('暂时无法试听该语音，请检查音频是否已生成。'));
  }

  // -------------------------------------------------------------------- VAD

  async function requestMic() {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      const source = audioContext.createMediaStreamSource(micStream);
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.5;
      source.connect(analyser);
      notify('');
      return true;
    } catch (e) {
      notify('麦克风权限被拒绝或不可用：' + e.message);
      return false;
    }
  }

  function stopMic() {
    if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
    if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
    if (audioContext) { audioContext.close(); audioContext = null; }
    analyser = null;
    vadState = 'idle';
  }

  function startVAD(run) {
    if (!analyser || !isActive(run)) return;
    run.listening = true;
    vadState = 'listening';
    speechStartMs = 0;
    silenceStartMs = 0;
    const startTime = Date.now();
    const timeoutMs = Number(run.bounds.no_response_timeout_ms) || VAD_TIMEOUT_MS;
    const data = new Uint8Array(analyser.frequencyBinCount);

    vadTimer = setInterval(() => {
      if (!isActive(run)) { stopVAD(); return; }
      if (vadState !== 'listening' && vadState !== 'device_speaking') return;

      analyser.getByteFrequencyData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length) / 255;
      const now = Date.now();

      if (vadState === 'listening') {
        if (rms > VAD_THRESHOLD) {
          if (!speechStartMs) speechStartMs = now;
          if (now - speechStartMs > VAD_SPEAK_MS) {
            vadState = 'device_speaking';
            silenceStartMs = 0;
            run.stats.observations += 1;
            wsSend(run, { type: 'device_speech_start', turn_id: run.turnId });
            updateStatus('检测到疑似回答（浏览器 VAD 提示，未确认说话人）。');
          }
        } else {
          speechStartMs = 0;
        }
        if (now - startTime > timeoutMs && !speechStartMs) {
          // A silent device is NOT the end of a response: report the timeout on
          // its own so the run cannot look like a completed answer.
          vadState = 'idle';
          run.stats.timeouts += 1;
          wsSend(run, { type: 'observation_timeout', turn_id: run.turnId, wait_ms: timeoutMs });
          updateStatus('未观察到设备回答（已达到控制等待上限）。');
          stopVAD();
        }
      } else if (vadState === 'device_speaking') {
        if (rms < VAD_THRESHOLD) {
          if (!silenceStartMs) silenceStartMs = now;
          if (now - silenceStartMs > VAD_SILENCE_MS) {
            vadState = 'listening';
            run.stats.observations += 1;
            wsSend(run, { type: 'device_speech_end', turn_id: run.turnId });
            updateStatus('等待下一轮…');
            stopVAD();
          }
        } else {
          silenceStartMs = 0;
        }
      }
    }, 50);
  }

  function stopVAD() {
    if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
    vadState = 'idle';
    if (activeRun) activeRun.listening = false;
  }

  function updateStatus(text) {
    const el = $('vt-status');
    if (el) el.textContent = text;
  }

  // ------------------------------------------------------------------ turns

  async function handlePlay(run, msg) {
    if (!isActive(run)) { run.stats.lateDropped += 1; return; }
    run.stats.playReceived += 1;
    run.turnId = msg.turn_id;
    if (msg.playback_start_timeout_ms) run.bounds.playback_start_timeout_ms = msg.playback_start_timeout_ms;
    if (msg.playback_max_duration_ms) run.bounds.playback_max_duration_ms = msg.playback_max_duration_ms;
    if (msg.no_response_timeout_ms) run.bounds.no_response_timeout_ms = msg.no_response_timeout_ms;

    updateStatus(`正在播放第 ${msg.phrase_index + 1} 句：${msg.text}`);
    const result = await playAudio(run, msg.audio_url);
    if (!isActive(run)) return; // stopped while playing: nothing further happens
    if (result.outcome === 'ended') {
      updateStatus('正在等待设备回答（仅观察疑似回答）。');
      startVAD(run);
    } else if (result.outcome === 'failed') {
      failRun(run, result.detail);
    }
  }

  // ------------------------------------------------------------- fixed mode

  async function createFixedSession() {
    const phrasesText = $('vt-phrases').value.trim();
    if (!phrasesText) { notify('请输入至少一句话术'); return; }
    const phrases = phrasesText.split('\n').map(s => s.trim()).filter(Boolean);
    if (phrases.length === 0) { notify('请输入至少一句话术'); return; }

    const device = $('vt-device')?.value || '';
    setFixedGenerationBusy(true);
    notify('正在创建会话…');
    updateGenerationProgress('正在创建测试会话…');
    try {
      const resp = await fetch('/api/voice-test/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'fixed', phrases, device }),
      });
      if (!resp.ok) throw new Error('创建会话失败：' + await responseMessage(resp));
      session = await resp.json();
      notify('');
      startSynthesisProgress(session.session_id, phrases.length);

      const synthResp = await fetch(`/api/voice-test/sessions/${session.session_id}/synthesize`, {
        method: 'POST',
      });
      if (!synthResp.ok) throw new Error('语音生成失败：' + await responseMessage(synthResp));
      const synthResult = await synthResp.json();
      session = { ...session, ...synthResult };
      const ready = (session.phrases || []).filter(p => p.status === 'ready').length;
      if (ready !== phrases.length) throw new Error(`语音生成不完整：${ready}/${phrases.length} 条已完成`);

      renderFixedPreview(session.phrases);
      $('vt-start-fixed').disabled = false;
      updateGenerationProgress(`已生成 ${ready}/${phrases.length} 条语音，可以试听或开始测试。`);
    } catch (error) {
      notify(error.message || '语音生成请求失败');
      updateGenerationProgress('语音生成未完成，请检查模型配置后重试。');
    } finally {
      stopSynthesisProgress();
      setFixedGenerationBusy(false);
    }
  }

  function renderFixedPreview(phrases) {
    const container = $('vt-preview');
    if (!container) return;
    container.innerHTML = '';
    phrases.forEach((phrase, i) => {
      const text = typeof phrase === 'string' ? phrase : phrase.text;
      const div = document.createElement('div');
      div.className = 'segment';
      div.innerHTML = `<button class="text-button" data-preview="${i}">试听 ${i + 1}</button><span>${esc(text)}</span>`;
      div.querySelector('[data-preview]').onclick = () => {
        previewAudio(`/api/voice-test/sessions/${session.session_id}/audio/${i}`);
      };
      container.appendChild(div);
    });
    container.hidden = false;
  }

  async function startFixedTest() {
    if (!session) { notify('请先生成语音'); return; }
    const hasMic = await requestMic();
    if (!hasMic) return;

    if (activeRun) stopRun(activeRun); // an old run must never keep control
    const run = newRun(session.session_id, 'fixed');
    activeRun = run;

    const wsUrl = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/api/voice-test/sessions/${session.session_id}/ws`;
    const socket = new WebSocket(wsUrl);
    run.ws = socket;

    socket.onopen = () => {
      if (!isActive(run)) return;
      run.opened = true;
      setRunningButtons('fixed', true);
      updateStatus('正在启动…');
      wsSend(run, { type: 'start' });
    };

    socket.onmessage = (event) => {
      if (!isActive(run)) { run.stats.lateDropped += 1; return; }
      let msg;
      try { msg = JSON.parse(event.data); } catch (_) { return; }
      if (msg.type === 'play') {
        handlePlay(run, msg);
      } else if (msg.type === 'listening') {
        updateStatus('检测到疑似回答（浏览器 VAD 提示，未确认说话人）。');
      } else if (msg.type === 'ignored') {
        run.stats.ignored += 1;
      } else if (msg.type === 'complete') {
        run.finished = true;
        stopLocal(run, null, { silent: true });
        updateStatus('测试完成：' + (msg.reason || ''));
      } else if (msg.type === 'stopped') {
        const timedOut = msg.reason === 'no_response_timeout';
        stopLocal(run, null, { silent: true });
        updateStatus(timedOut
          ? '已停止：未观察到设备回答（已达到控制等待上限）。'
          : '已停止');
      } else if (msg.type === 'failed') {
        failRun(run, msg.detail || msg.reason);
      } else if (msg.type === 'error') {
        failRun(run, msg.reason || '服务端错误');
      }
    };

    socket.onclose = (event) => {
      if (run.cancelled) return; // expected after a stop
      if (run.finished) { setRunningButtons('fixed', false); return; }
      // A socket that never opened means the backend refused the session
      // (unknown session, e.g. after a restart), not a mid-run drop.
      const lost = !run.opened || !!(event && event.code === 4004);
      failRun(run, lost ? 'session_lost' : 'socket_closed');
    };

    socket.onerror = () => {
      if (!isActive(run)) return;
      failRun(run, run.opened ? 'socket_closed' : 'session_lost');
    };
  }

  function stopFixedTest() {
    stopRun(activeRun && activeRun.mode === 'fixed' ? activeRun : null);
  }

  // -------------------------------------------------------------- free mode

  async function startFreeTest() {
    const goal = $('vt-free-goal').value.trim();
    if (!goal) { notify('请输入测试目标'); return; }
    const constraints = ($('vt-free-constraints').value || '').split('\n').map(s => s.trim()).filter(Boolean);
    const maxTurns = parseInt($('vt-free-max-turns').value || '10', 10);

    notify('正在创建会话…');
    const resp = await fetch('/api/voice-test/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'free', goal, constraints, max_turns: maxTurns }),
    });
    if (!resp.ok) { notify('创建会话失败'); return; }
    session = await resp.json();
    notify('');

    const hasMic = await requestMic();
    if (!hasMic) return;

    if (activeRun) stopRun(activeRun);
    const run = newRun(session.session_id, 'free');
    activeRun = run;

    const wsUrl = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/api/voice-test/sessions/${session.session_id}/ws`;
    const socket = new WebSocket(wsUrl);
    run.ws = socket;
    $('vt-free-log').innerHTML = '';

    socket.onopen = () => {
      if (!isActive(run)) return;
      run.opened = true;
      setRunningButtons('free', true);
      updateStatus('正在生成第一句话术…');
      wsSend(run, { type: 'start' });
    };

    socket.onmessage = (event) => {
      if (!isActive(run)) { run.stats.lateDropped += 1; return; }
      let msg;
      try { msg = JSON.parse(event.data); } catch (_) { return; }
      if (msg.type === 'play') {
        appendFreeLog('平台', msg.text);
        if (msg.device_text) appendFreeLog('设备', msg.device_text);
        handlePlay(run, msg);
      } else if (msg.type === 'listening') {
        updateStatus('检测到疑似回答（浏览器 VAD 提示，未确认说话人）。');
      } else if (msg.type === 'ignored') {
        run.stats.ignored += 1;
      } else if (msg.type === 'no_response') {
        appendFreeLog('系统', '未观察到设备回答（按策略继续）');
      } else if (msg.type === 'complete') {
        run.finished = true;
        stopLocal(run, null, { silent: true });
        appendFreeLog('系统', '测试结束：' + (msg.reason || ''));
        updateStatus('测试完成：' + (msg.reason || ''));
      } else if (msg.type === 'stopped') {
        const timedOut = msg.reason === 'no_response_timeout';
        stopLocal(run, null, { silent: true });
        appendFreeLog('系统', '已停止');
        updateStatus(timedOut
          ? '已停止：未观察到设备回答（已达到控制等待上限）。'
          : '已停止');
      } else if (msg.type === 'failed') {
        failRun(run, msg.detail || msg.reason);
      } else if (msg.type === 'error') {
        failRun(run, msg.reason || '服务端错误');
      }
    };

    socket.onclose = (event) => {
      if (run.cancelled) return;
      if (run.finished) { setRunningButtons('free', false); return; }
      const lost = !run.opened || !!(event && event.code === 4004);
      failRun(run, lost ? 'session_lost' : 'socket_closed');
    };

    socket.onerror = () => {
      if (!isActive(run)) return;
      failRun(run, run.opened ? 'socket_closed' : 'session_lost');
    };
  }

  function stopFreeTest() {
    stopRun(activeRun && activeRun.mode === 'free' ? activeRun : null);
  }

  function appendFreeLog(role, text) {
    const log = $('vt-free-log');
    if (!log) return;
    const div = document.createElement('div');
    div.className = 'segment';
    div.innerHTML = `<div><strong>${esc(role)}</strong><p>${esc(text)}</p></div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function esc(s) { return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

  // --- Public API ---
  return {
    createFixedSession,
    startFixedTest,
    stopFixedTest,
    startFreeTest,
    stopFreeTest,
    stopMic,
    controlState,
  };
})();

if (typeof window !== 'undefined') window.VT = VT;
