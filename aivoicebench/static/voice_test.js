/* AIVoiceBench — Active Voice Test (语音对话测试)
 *
 * Browser-side audio playback, microphone VAD, and WebSocket control.
 *
 * Fixed mode: user enters phrases, backend generates TTS, browser plays them
 *   in sequence, VAD detects device responses, advances to next phrase.
 *
 * Free mode: LLM agent generates phrases, browser plays, captures device
 *   audio, uploads for ASR, agent decides next turn.
 */

const VT = (function () {
  let ws = null;
  let micStream = null;
  let audioContext = null;
  let analyser = null;
  let vadTimer = null;
  let vadState = 'idle'; // idle, playing, listening, device_speaking
  let speechStartMs = 0;
  let silenceStartMs = 0;
  let currentAudio = null;
  let session = null;
  let synthesisProgressTimer = null;
  let synthesisProgressPolling = false;

  // VAD thresholds (tunable)
  const VAD_THRESHOLD = 0.015; // RMS threshold for speech detection
  const VAD_SPEAK_MS = 300; // RMS must be above threshold for this long → speech start
  const VAD_SILENCE_MS = 1500; // RMS must be below threshold for this long → speech end
  const VAD_TIMEOUT_MS = 30000; // If no speech detected in 30s → timeout

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
    const refresh = async () => {
      if (synthesisProgressPolling) return;
      synthesisProgressPolling = true;
      try {
        const response = await fetch(`/api/voice-test/sessions/${sessionId}`);
        if (response.ok) {
          const snapshot = await response.json();
          const complete = (snapshot.phrases || []).filter(p => p.status === 'ready').length;
          updateGenerationProgress(`正在调用 TTS 生成语音：${complete}/${total} 条已完成，已等待 ${elapsed()} 秒。`);
        }
      } catch (_) {
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

  function startVAD() {
    if (!analyser) return;
    vadState = 'listening';
    speechStartMs = 0;
    silenceStartMs = 0;
    const startTime = Date.now();
    const data = new Uint8Array(analyser.frequencyBinCount);

    vadTimer = setInterval(() => {
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
            wsSend({ type: 'device_speech_start' });
            updateStatus('设备回答中…');
          }
        } else {
          speechStartMs = 0;
        }
        // Timeout: no speech detected
        if (now - startTime > VAD_TIMEOUT_MS && !speechStartMs) {
          vadState = 'idle';
          wsSend({ type: 'device_speech_end' });
          updateStatus('未检测到设备回答（超时）');
        }
      } else if (vadState === 'device_speaking') {
        if (rms < VAD_THRESHOLD) {
          if (!silenceStartMs) silenceStartMs = now;
          if (now - silenceStartMs > VAD_SILENCE_MS) {
            vadState = 'listening';
            wsSend({ type: 'device_speech_end' });
            updateStatus('等待下一轮…');
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
  }

  function updateStatus(text) {
    const el = $('vt-status');
    if (el) el.textContent = text;
  }

  function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  async function playAudio(url) {
    return new Promise((resolve, reject) => {
      // Stop VAD during playback to avoid self-detection
      stopVAD();
      if (currentAudio) { currentAudio.pause(); currentAudio = null; }

      currentAudio = new Audio(url);
      currentAudio.onended = () => {
        currentAudio = null;
        updateStatus('监听设备回答…');
        startVAD();
        resolve();
      };
      currentAudio.onerror = (e) => {
        currentAudio = null;
        reject(new Error('音频播放失败'));
      };
      currentAudio.play().catch(reject);
    });
  }

  // --- Fixed mode ---
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
        playAudio(`/api/voice-test/sessions/${session.session_id}/audio/${i}`);
      };
      container.appendChild(div);
    });
    container.hidden = false;
  }

  async function startFixedTest() {
    if (!session) { notify('请先生成语音'); return; }
    const hasMic = await requestMic();
    if (!hasMic) return;

    // Connect WebSocket
    const wsUrl = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/api/voice-test/sessions/${session.session_id}/ws`;
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      $('vt-start-fixed').disabled = true;
      $('vt-stop-fixed').disabled = false;
      updateStatus('正在启动…');
      wsSend({ type: 'start' });
    };

    ws.onmessage = async (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'play') {
        updateStatus(`播放第 ${msg.phrase_index + 1} 句：${esc(msg.text)}`);
        await playAudio(msg.audio_url);
      } else if (msg.type === 'listening') {
        updateStatus('设备回答中…');
      } else if (msg.type === 'complete') {
        updateStatus('测试完成：' + (msg.reason || ''));
        stopMic();
        $('vt-start-fixed').disabled = false;
        $('vt-stop-fixed').disabled = true;
      } else if (msg.type === 'stopped') {
        updateStatus('已停止：' + (msg.reason || ''));
        stopMic();
        $('vt-start-fixed').disabled = false;
        $('vt-stop-fixed').disabled = true;
      } else if (msg.type === 'error') {
        updateStatus('错误：' + esc(msg.reason || ''));
        stopMic();
        $('vt-start-fixed').disabled = false;
        $('vt-stop-fixed').disabled = true;
      }
    };

    ws.onclose = () => {
      stopMic();
      $('vt-start-fixed').disabled = false;
      $('vt-stop-fixed').disabled = true;
    };

    ws.onerror = () => {
      notify('WebSocket 连接失败');
      stopMic();
    };
  }

  function stopFixedTest() {
    wsSend({ type: 'stop' });
    stopMic();
  }

  // --- Free mode ---
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

    const wsUrl = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/api/voice-test/sessions/${session.session_id}/ws`;
    ws = new WebSocket(wsUrl);
    $('vt-free-log').innerHTML = '';

    ws.onopen = () => {
      $('vt-start-free').disabled = true;
      $('vt-stop-free').disabled = false;
      updateStatus('正在生成第一句话术…');
      wsSend({ type: 'start' });
    };

    ws.onmessage = async (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'play') {
        appendFreeLog('平台', msg.text);
        if (msg.device_text) appendFreeLog('设备', msg.device_text);
        updateStatus(`播放：${esc(msg.text)}`);
        await playAudio(msg.audio_url);
      } else if (msg.type === 'listening') {
        updateStatus('设备回答中…');
      } else if (msg.type === 'complete') {
        updateStatus('测试完成：' + (msg.reason || ''));
        appendFreeLog('系统', '测试结束：' + (msg.reason || ''));
        stopMic();
        $('vt-start-free').disabled = false;
        $('vt-stop-free').disabled = true;
      } else if (msg.type === 'stopped') {
        updateStatus('已停止');
        appendFreeLog('系统', '用户停止');
        stopMic();
        $('vt-start-free').disabled = false;
        $('vt-stop-free').disabled = true;
      } else if (msg.type === 'error') {
        updateStatus('错误：' + esc(msg.reason || ''));
        appendFreeLog('系统', '错误：' + esc(msg.reason || ''));
        stopMic();
        $('vt-start-free').disabled = false;
        $('vt-stop-free').disabled = true;
      }
    };

    ws.onclose = () => {
      stopMic();
      $('vt-start-free').disabled = false;
      $('vt-stop-free').disabled = true;
    };

    ws.onerror = () => {
      notify('WebSocket 连接失败');
      stopMic();
    };
  }

  function stopFreeTest() {
    wsSend({ type: 'stop' });
    stopMic();
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

  // --- Captured audio for free mode ASR ---
  let mediaRecorder = null;
  let recordedChunks = [];

  async function startRecording() {
    if (!micStream) return;
    recordedChunks = [];
    mediaRecorder = new MediaRecorder(micStream);
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = async () => {
      const blob = new Blob(recordedChunks, { type: 'audio/webm' });
      const formData = new FormData();
      formData.append('audio', blob, 'device-response.webm');
      const resp = await fetch(`/api/voice-test/sessions/${session.session_id}/device-audio`, {
        method: 'POST',
        body: blob,
      });
      if (resp.ok) {
        const result = await resp.json();
        wsSend({ type: 'device_audio_ready', transcript: result.transcript || '' });
      }
    };
    mediaRecorder.start();
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.stop();
    }
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
  };
})();
