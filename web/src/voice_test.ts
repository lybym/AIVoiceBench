/* AIVoiceBench — Active Voice Test (语音对话测试)
 *
 * This file is the TypeScript source of `aivoicebench/static/voice_test.js`
 * (compiled by `npm run build`). It is a classic script rather than a module: the
 * page loads it with `<script src="/static/voice_test.js">` and the test suite
 * calls `window.VT.controlState()`, so the exported `VT` object stays a window
 * global, as it was before the migration.
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
 *
 * The types this file uses for audio frames, VAD observations, capture
 * integrity, media provenance, control lifecycle and Run/Turn state come from
 * `models.ts` (the Browser Station contract layer). They describe the existing
 * wire contract; they are not a new source of Measurement fact.
 */

/** Options the page passes to the PCM capture worklet. */
interface PcmCaptureProcessorOptions {
  targetRate: number;
  frameSamples: number;
}

/** The subset of `AudioContext` the capture path needs, including the worklet. */
interface AudioContextWithWorklet extends AudioContext {
  audioWorklet: AudioWorklet;
}

/** `AudioContext` constructor that accepts a forced sample rate. */
type AudioContextFactory = new (options?: AudioContextOptions) => AudioContext;

/** Browser vendor-prefixed audio context, still present in some Chrome builds. */
interface AudioContextWindow extends Window {
  webkitAudioContext?: AudioContextFactory;
  AudioContext: AudioContextFactory;
  BaseAudioContext?: { prototype: object };
}

/** Diagnostic outcome of one awaited playback. */
interface PlaybackResult {
  outcome: PlaybackOutcome;
  detail?: string;
}

/**
 * One awaited stimulus playback.
 *
 * `settle` finishes the promise exactly once (clearing both control timers);
 * `cancel` detaches the audio handlers first, so a late `ended` from a cancelled
 * element can never restart listening or advance the run.
 */
interface PlaybackHandle {
  audio: HTMLAudioElement;
  settle(outcome: PlaybackOutcome, detail?: string): boolean;
  cancel(reason: string): boolean;
}

/** Streaming capture state for one turn (real-time Streaming ASR). */
interface StreamingCapture {
  mode: 'streaming';
  ws: WebSocket | null;
  context: AudioContext | null;
  node: AudioWorkletNode | null;
  /** Next frame sequence number; the wire header is this value, big-endian. */
  sequence: number;
  frames: number;
  bytes: number;
  droppedFrames: number;
  started: boolean;
  stopping: boolean;
  closed: boolean;
  /** Rate the browser actually gave the capture context (may differ from 16 kHz). */
  contextRate: number | null;
  result?: CaptureResultMessage | null;
  error?: string;
  partials?: number;
  resultPromise?: Promise<CaptureResultMessage | null>;
  resolveResult?: (message: CaptureResultMessage | null) => void;
  /**
   * Bound on the *server's* finalisation of this capture. It is armed when
   * `capture_stopped` is sent, never when the capture opens: a device that takes
   * longer than the bound to answer must not pre-resolve the wait, because the
   * control round then advances on a result the backend has not published yet
   * (Issue #113).
   */
  finaliseTimer?: ReturnType<typeof setTimeout> | null;
}

/** Labelled-fallback (whole-turn) capture state for one turn. */
interface TurnFileCapture {
  mode: 'turn_file';
  recorder: MediaRecorder | null;
  chunks: Blob[];
  started: boolean;
  stopping?: boolean;
  captureId: string | null;
  turnId: string | null;
  mimeType: string;
  /** Only a turn closed by an observed answer end is uploaded. */
  complete: boolean;
}

/** Either capture path for the active turn. */
type TurnCapture = StreamingCapture | TurnFileCapture;

/**
 * The active control run.
 *
 * Everything that can arrive late (WebSocket messages, audio callbacks, VAD
 * ticks) is validated against this object, and `seq` makes a run replaceable
 * without letting a callback from the previous run touch the new one.
 */
interface VoiceRun {
  seq: number;
  sessionId: string;
  mode: VoiceTestMode;
  ws: WebSocket | null;
  opened: boolean;
  cancelled: boolean;
  finished: boolean;
  turnId: string | null;
  /** Free mode: the device-transcript line currently on screen. */
  deviceText: { text: string; kind: 'partial' | 'final'; turnId: string | null; confirmed: boolean } | null;
  audio: HTMLAudioElement | null;
  playback: PlaybackHandle | null;
  pendingPlayWait: boolean;
  /** Free mode: whether the turn's capture has already been finalised. */
  pendingCaptureFinish?: boolean;
  listening: boolean;
  captureMode: CaptureMode | null;
  captureFallback: CaptureFallbackReason | null;
  capture: TurnCapture | null;
  captureId?: string | null;
  bounds: ControlBounds;
  vad: ControlVadSnapshot | null;
  /** Control-socket liveness, including a session the backend no longer knows. */
  socketState: ControlSocketState;
  /** Polls the session snapshot while a run is live, to surface run progress. */
  progressTimer?: ReturnType<typeof setInterval> | null;
  /** Last progress reason shown, so the log is not repeated every poll. */
  progressReason?: string | null;
  integrity: CaptureIntegrity;
  stats: RunStats;
}

/** The page's own session snapshot (`GET /api/voice-test/sessions/{id}`). */
interface SynthesisSnapshot {
  phrases?: { status?: string }[];
}

const VT = (function () {
  let micStream: MediaStream | null = null;
  let audioContext: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let vadTimer: ReturnType<typeof setInterval> | null = null;
  let vadState: 'idle' | 'listening' | 'device_speaking' = 'idle';
  let speechStartMs = 0;
  let silenceStartMs = 0;
  let session: VoiceSession | null = null;
  let synthesisProgressTimer: ReturnType<typeof setInterval> | null = null;
  let synthesisProgressPolling = false;

  // Labelled-fallback (whole-turn) recording state. Only used when the operator
  // explicitly chose that downgrade path.
  let mediaRecorder: MediaRecorder | null = null;
  let recording: TurnFileCapture | null = null;
  let uploadAbort: AbortController | null = null;

  // The active control run. Everything that can arrive late (WebSocket
  // messages, audio callbacks, VAD ticks) is validated against it.
  let activeRun: VoiceRun | null = null;
  let runSeq = 0;

  // VAD thresholds (tunable)
  //
  // The RMS is computed from the **time-domain** signal (`getFloatTimeDomainData`)
  // in linear -1..1 units. The previous implementation read the frequency-domain
  // bins and divided by 255, which is not an amplitude and made an absolute
  // threshold meaningless: a non-zero noise floor could keep a turn "speaking"
  // forever, and a quiet room could look like speech. An absolute floor is still
  // only a starting point — the environment's own noise floor raises it.
  const VAD_FLOOR = 0.004; // absolute linear-PCM RMS floor
  const VAD_SPEAK_MS = 300; // RMS must be above the start threshold for this long → speech start
  const VAD_SILENCE_MS = 1500; // RMS must be below the end threshold for this long → speech end
  const VAD_TIMEOUT_MS = 30000; // control bound: no suspected response within 30s
  const VAD_CALIBRATION_MS = 1200; // short pre-observation noise-floor estimate
  const VAD_CALIBRATION_MAX_RMS = 0.05; // samples at or above this are treated as speech, not floor
  const VAD_ROUND_MAX_MS = 90000; // control escape hatch once speech was detected

  // Bounded control waits (not performance targets).
  const PLAYBACK_START_TIMEOUT_MS = 10000;
  const PLAYBACK_MAX_DURATION_MS = 180000;

  function $(id: string): HTMLElement | null { return document.getElementById(id); }

  function notify(msg: string): void {
    const el = $('vt-notice');
    if (el) { el.textContent = msg; el.hidden = !msg; }
  }

  function fixedGenerationElements(): { button: HTMLButtonElement | null; progress: HTMLElement | null } {
    const fixed = $('vt-fixed');
    const actions = fixed?.querySelector('.settings-actions');
    const button = ($('vt-generate-fixed') || actions?.querySelector('button.primary')) as HTMLButtonElement | null;
    if (button) button.id = 'vt-generate-fixed';

    let progress = $('vt-generation-progress') as HTMLElement | null;
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

  function updateGenerationProgress(message: string): void {
    const { progress } = fixedGenerationElements();
    if (progress) progress.textContent = message;
  }

  function setFixedGenerationBusy(busy: boolean): void {
    const { button } = fixedGenerationElements();
    if (button) {
      button.disabled = busy;
      button.textContent = busy ? '正在生成…' : '生成语音';
    }
    ['vt-phrases', 'vt-device'].forEach(id => {
      const input = $(id) as HTMLInputElement | null;
      if (input) input.disabled = busy;
    });
  }

  function stopSynthesisProgress(): void {
    if (synthesisProgressTimer) clearInterval(synthesisProgressTimer);
    synthesisProgressTimer = null;
    synthesisProgressPolling = false;
  }

  function startSynthesisProgress(sessionId: string, total: number): void {
    const startedAt = Date.now();
    const elapsed = (): number => Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    // A poll that is still in flight when synthesis finishes must not overwrite
    // the final status: `stopSynthesisProgress()` sets the timer to null, and
    // any response arriving after that is dropped.
    const cancelled = (): boolean => synthesisProgressTimer === null;
    const refresh = async (): Promise<void> => {
      if (synthesisProgressPolling) return;
      synthesisProgressPolling = true;
      try {
        const response = await fetch(`/api/voice-test/sessions/${sessionId}`);
        if (cancelled()) return;
        if (response.ok) {
          const snapshot = await response.json() as SynthesisSnapshot;
          if (cancelled()) return;
          const complete = (snapshot.phrases || []).filter(phrase => phrase.status === 'ready').length;
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
    void refresh();
    synthesisProgressTimer = setInterval(() => { void refresh(); }, 800);
  }

  async function responseMessage(response: Response): Promise<string> {
    try {
      const body = await response.json() as { detail?: string; message?: string };
      return body.detail || body.message || `HTTP ${response.status}`;
    } catch (_) {
      const text = await response.text();
      return text || `HTTP ${response.status}`;
    }
  }

  // ------------------------------------------------------------------- run

  function newRun(sessionId: string, mode: VoiceTestMode): VoiceRun {
    return {
      seq: ++runSeq,
      sessionId,
      mode,
      ws: null,
      opened: false,
      cancelled: false,
      finished: false,
      turnId: null,
      // Free mode: the device-transcript line currently on screen, and the turn
      // it belongs to. A late callback must never overwrite it, and a run that
      // ends without confirming the round clears it instead of leaving a stale
      // "confirmed answer" visible (see `showDeviceText` / `clearDeviceText`).
      deviceText: null,
      audio: null,
      // The playback currently being awaited. `cancelAudio()` ends it, so a
      // cancelled play never leaves an await (or its timers) pending.
      playback: null,
      pendingPlayWait: false,
      listening: false,
      // Free mode: how the device's answer becomes text. The server decides once
      // per run and the mode is always shown; a fallback is never presented as
      // streaming.
      captureMode: null,
      captureFallback: null,
      capture: null,
      bounds: {
        playback_start_timeout_ms: PLAYBACK_START_TIMEOUT_MS,
        playback_max_duration_ms: PLAYBACK_MAX_DURATION_MS,
        no_response_timeout_ms: VAD_TIMEOUT_MS,
        round_observation_max_ms: VAD_ROUND_MAX_MS,
      },
      vad: null,
      socketState: 'idle',
      integrity: {
        frames_sent: 0,
        bytes_sent: 0,
        next_sequence: 0,
        dropped_frames: 0,
        last_buffered_amount: 0,
        gap_frames: 0,
        malformed_frames: 0,
        stale_callbacks: 0,
      },
      stats: {
        playReceived: 0, playbackStarted: 0, playbackEnded: 0,
        observations: 0, timeouts: 0, ignored: 0, failures: 0, lateDropped: 0,
        playWaitSettled: 0, playWaitCancelled: 0,
      },
    };
  }

  /**
   * Is this run the one currently in control, and still live?
   *
   * Deliberately a plain boolean rather than a type predicate: `run` is often a
   * callback-captured binding that the surrounding flow reassigns, and a
   * predicate would narrow a non-nullable constant to `never` in that position.
   */
  function isActive(run: VoiceRun | null | undefined): boolean {
    return !!run && activeRun === run && !run.cancelled;
  }

  /* Read-only diagnostics of the control layer (used by tests and support). */
  function controlState(): ControlStateSnapshot | null {
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
      capture_mode: activeRun.captureMode,
      capture_fallback_reason: activeRun.captureFallback,
      has_capture: !!activeRun.capture,
      vad: activeRun.vad ? Object.assign({}, activeRun.vad) : null,
      stats: Object.assign({}, activeRun.stats),
    };
  }

  /**
   * Read-only capture integrity for the active run.
   *
   * Dropped/gap/malformed/stale counters are reported as facts; they are never
   * folded into a quality or accuracy claim.
   */
  function integrityState(): CaptureIntegrity | null {
    if (!activeRun) return null;
    const integrity = activeRun.integrity;
    const capture = activeRun.capture;
    if (capture && capture.mode === 'streaming') {
      integrity.frames_sent = capture.frames;
      integrity.bytes_sent = capture.bytes;
      integrity.next_sequence = capture.sequence;
      integrity.dropped_frames = capture.droppedFrames;
    }
    return Object.assign({}, integrity);
  }

  /** Send one control/observation message for this run; false when it cannot be sent. */
  function wsSend(run: VoiceRun | null, payload: ControlObservationMessage): boolean {
    if (!run || !run.ws || run.ws.readyState !== WebSocket.OPEN) return false;
    try {
      run.ws.send(JSON.stringify(payload));
      return true;
    } catch (_) {
      return false;
    }
  }

  function setRunningButtons(mode: VoiceTestMode, running: boolean): void {
    const start = $(mode === 'fixed' ? 'vt-start-fixed' : 'vt-start-free') as HTMLButtonElement | null;
    const stop = $(mode === 'fixed' ? 'vt-stop-fixed' : 'vt-stop-free') as HTMLButtonElement | null;
    if (start) start.disabled = running;
    if (stop) stop.disabled = !running;
  }

  function clearTimer(id: ReturnType<typeof setTimeout> | null): null {
    if (id) clearTimeout(id);
    return null;
  }

  /* End the playback wait that is currently outstanding, exactly once.
   *
   * Stopping must finish the `await playAudio(...)` in `handlePlay`, not just
   * silence the audio element: otherwise the wait (and its timers) stay pending
   * forever. Returns true when a wait was actually ended by this call.
   */
  function cancelAudio(run: VoiceRun | null, reason: string): boolean {
    const playback = run && run.playback;
    if (!playback) {
      if (run) { run.audio = null; run.pendingPlayWait = false; }
      return false;
    }
    return playback.cancel(reason || 'cancelled');
  }

  function stopLocal(run: VoiceRun | null, reason: string | null, options?: { silent?: boolean }): boolean {
    if (!run) return false;
    if (run.cancelled) return false; // idempotent
    run.cancelled = true;
    run.listening = false;
    stopProgressPolling(run);
    // Ends the outstanding playback wait as well as the audio itself, so the
    // awaiting flow finishes instead of hanging.
    const hadAudio = cancelAudio(run, reason || 'user_stop');
    teardownCapture(run);
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
  function closeSocket(run: VoiceRun | null): void {
    const socket = run && run.ws;
    if (!socket) return;
    run.ws = null;
    run.socketState = 'closed';
    try {
      socket.onmessage = null; socket.onclose = null; socket.onerror = null;
      socket.close();
    } catch (_) { /* ignore */ }
  }

  /* Surface the server's run progress while a free-mode run is live.
   *
   * The control socket only speaks when something happens, so a round that is
   * waiting — or one the server refused to advance — was invisible: the page
   * looked like it had simply stopped refreshing (Issue #113). This reads the
   * server's own progress projection and reports the phase plus the concrete
   * reason. It is control state, never a measurement.
   */
  const PROGRESS_POLL_MS = 3000;
  const PROGRESS_PHASE_LABELS: Record<string, string> = {
    idle: '空闲（无待观察轮次）',
    play_issued: '已下发播报，等待播放回报',
    awaiting_device_observation: '等待设备回答观察',
    capturing_device_audio: '正在采集并识别设备回答',
    capture_finalised: '识别已结束，等待本轮推进',
  };
  /** Waiting states the operator already sees on screen; not repeated in the log. */
  const QUIET_PROGRESS_REASONS = ['awaiting_device_observation', 'capture_in_progress'];

  function stopProgressPolling(run: VoiceRun | null): void {
    if (run && run.progressTimer) clearInterval(run.progressTimer as ReturnType<typeof setInterval>);
    if (run) run.progressTimer = null;
  }

  async function pollProgress(run: VoiceRun): Promise<void> {
    if (!isActive(run) || run.finished) return;
    let snapshot: VoiceSession | null = null;
    try {
      const response = await fetch(`/api/voice-test/sessions/${run.sessionId}`);
      if (!response.ok) return;
      snapshot = await response.json() as VoiceSession;
    } catch (_) {
      return;
    }
    if (!isActive(run) || run.finished) return;
    const progress = snapshot && snapshot.progress;
    if (!progress || !progress.reason_code || progress.reason_code === run.progressReason) return;
    run.progressReason = progress.reason_code;
    if (QUIET_PROGRESS_REASONS.indexOf(progress.reason_code) !== -1) return;
    const label = PROGRESS_PHASE_LABELS[progress.phase] || progress.phase;
    appendFreeLog('系统', `当前阶段：${label}（${progress.reason}）`);
    captureStatus(`当前阶段：${label}（${progress.reason}）`);
  }

  function startProgressPolling(run: VoiceRun): void {
    stopProgressPolling(run);
    run.progressReason = null;
    run.progressTimer = setInterval(() => { void pollProgress(run); }, PROGRESS_POLL_MS);
  }

  function stopRun(run: VoiceRun | null): void {
    if (!run) return;
    if (!stopLocal(run, null)) return; // already stopped: repeated clicks do nothing
    // A model call already in flight cannot be cancelled; the control socket is
    // busy awaiting it, so tell the server through the bounded HTTP path as
    // well. A late result then sees a stopped session and must not request TTS
    // or playback.
    fetch(`/api/voice-test/sessions/${run.sessionId}/stop`, { method: 'POST' }).catch(() => {});
    // Best effort only: a missing backend must never keep the UI from stopping.
    wsSend(run, { type: 'stop' });
    const socket = run.ws;
    setTimeout(() => { if (run.ws === socket) closeSocket(run); }, 250);
  }

  function failRun(run: VoiceRun | null, reason: string): void {
    if (!run || run.cancelled) return;
    run.stats.failures += 1;
    const failureLabels: Record<string, string> = {
      audio_error: '音频资产不可用或无法解码',
      play_rejected: '浏览器拒绝播放',
      playback_did_not_start: '音频未开始播放',
      playback_stuck: '音频未在控制时限内结束播放',
      socket_closed: '连接已断开（后端可能已重启）',
      session_lost: '会话已失效（后端可能已重启）',
    };
    const text = failureLabels[reason] || reason || '未知原因';
    // An interrupted run has no confirmed answer for the round on screen.
    clearDeviceText(run);
    stopLocal(run, null, { silent: true });
    run.finished = true;
    updateStatus(`测试中断：${text}。可重新开始。`);
    notify(`固定对话已中断：${text}。可重新开始。`);
    closeSocket(run);
  }

  // --------------------------------------------------------------- playback

  function playAudio(run: VoiceRun, url: string): Promise<PlaybackResult> {
    return new Promise(resolve => {
      stopVAD(); // do not listen to our own playback
      cancelAudio(run, 'replaced'); // end any previous wait exactly once

      const audio = new Audio(url);
      let settled = false;
      let startTimer: ReturnType<typeof setTimeout> | null = null;
      let maxTimer: ReturnType<typeof setTimeout> | null = null;

      const playback: PlaybackHandle = {
        audio,
        // Settles the wait once: clears both timers and resolves the promise.
        settle(outcome: PlaybackOutcome, detail?: string): boolean {
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
        cancel(reason: string): boolean {
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

      const ownsAudio = (): boolean => isActive(run) && run.playback === playback;

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

  let previewAudioElement: HTMLAudioElement | null = null;

  function previewAudio(url: string): void {
    if (previewAudioElement) {
      try { previewAudioElement.pause(); } catch (_) { /* ignore */ }
    }
    const audio = new Audio(url);
    previewAudioElement = audio;
    audio.play().catch(() => notify('暂时无法试听该语音，请检查音频是否已生成。'));
  }

  // ------------------------------------------- streaming capture (PRD-F023)

  const CAPTURE_TARGET_RATE = 16000;
  const CAPTURE_FRAME_SAMPLES = 3200; // 200 ms at 16 kHz
  const CAPTURE_BACKPRESSURE_BYTES = 262144;
  /** Wait bound for the server to finalise a stopped capture, on top of its own drain bound. */
  const CAPTURE_FINALISE_MARGIN_MS = 15000;

  function captureStatus(text: string): void {
    const el = $('vt-capture-status');
    if (el) el.textContent = text;
  }
  /* The device-transcript line always describes the round being observed *now*.
   *
   * `turnId` is the run's current turn: a callback for anything else is not a
   * current observation, and a callback that arrives after the run stopped must
   * never rewrite this line as a confirmed answer for a round the trace says was
   * not answered. (The server also marks late provider output as stale; both
   * sides refuse it.)
   */
  function showDeviceText(text: string | null, kind: 'partial' | 'final', turnId: string | null): void {
    const label = kind === 'final' ? '设备（确认）' : '设备（实时）';
    const el = $('vt-device-transcript');
    if (el) el.textContent = text ? `${label}：${text}` : '';
    if (activeRun) {
      activeRun.deviceText = text
        ? { text, kind, turnId: turnId || null, confirmed: kind === 'final' }
        : null;
    }
  }

  /* Drop the round's transcript line: it no longer describes anything current. */
  function clearDeviceText(run: VoiceRun | null): void {
    const el = $('vt-device-transcript');
    if (el) el.textContent = '';
    if (run) run.deviceText = null;
  }

  /* A transcript that arrived for a finished run (or a turn the page is no
   * longer observing) is not an answer. Report it as ignored, with the session,
   * the turn and the reason, and leave the round's text untouched. */
  function noteStaleTranscript(run: VoiceRun | null, reason: string, message?: AudioSocketMessage | null): void {
    if (run) {
      run.stats.lateDropped += 1;
      run.integrity.stale_callbacks += 1;
    }
    // A burst of late partials must not flood the log or the status line.
    if (run && (run.stats.staleLogged || 0) >= 3) return;
    if (run) run.stats.staleLogged = (run.stats.staleLogged || 0) + 1;
    const sessionId = (run && run.sessionId) || (message && (message as { session_id?: string }).session_id) || '未知会话';
    const turnId = (message && ((message as { turn_id?: string }).turn_id || (message as { turnId?: string }).turnId))
      || (run && run.turnId) || '未知轮次';
    appendFreeLog('系统',
      `已忽略迟到识别结果（${reason}；会话 ${sessionId} / 轮次 ${turnId}），未更新本轮文本。`);
    captureStatus('已忽略迟到识别结果（未确认为本轮回答）。');
  }

  function captureIsStreaming(run: VoiceRun | null): boolean {
    return !!run && run.mode === 'free' && run.captureMode === 'streaming';
  }

  function sendAudioFrame(run: VoiceRun, pcmBuffer: ArrayBuffer): void {
    const capture = run.capture;
    if (!capture || capture.mode !== 'streaming' || !capture.started || !isActive(run)) return;
    const socket = capture.ws;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    // Backpressure: never grow an unbounded send queue.
    run.integrity.last_buffered_amount = socket.bufferedAmount;
    if (socket.bufferedAmount > CAPTURE_BACKPRESSURE_BYTES) {
      capture.droppedFrames += 1;
      run.integrity.dropped_frames = capture.droppedFrames;
      return;
    }
    // One definition of the wire framing lives in models.ts: 4-byte big-endian
    // sequence header followed by the PCM payload.
    const frame = frameCapturePayload(capture.sequence, pcmBuffer);
    socket.send(frame.buffer as ArrayBuffer);
    capture.sequence += 1;
    capture.frames += 1;
    capture.bytes += frame.length - AUDIO_FRAME_HEADER_BYTES;
    run.integrity.frames_sent = capture.frames;
    run.integrity.bytes_sent = capture.bytes;
    run.integrity.next_sequence = capture.sequence;
  }

  function teardownCapture(run: VoiceRun | null): void {
    const capture = run && run.capture;
    if (!capture) return;
    if (capture.mode === 'streaming' && capture.node) {
      try { capture.node.port.postMessage({ type: 'stop' } satisfies WorkletControlMessage); } catch (_) { /* ignore */ }
      try { capture.node.port.onmessage = null; } catch (_) { /* ignore */ }
      try { capture.node.disconnect(); } catch (_) { /* ignore */ }
    }
    if (capture.mode === 'streaming' && capture.context) {
      try { void capture.context.close(); } catch (_) { /* ignore */ }
    }
    if (capture.mode === 'turn_file' && capture.recorder && capture.recorder.state === 'recording') {
      try { capture.recorder.stop(); } catch (_) { /* ignore */ }
    }
    if (capture.mode === 'streaming') {
      const socket = capture.ws;
      if (socket) {
        capture.ws = null;
        try { socket.onmessage = null; socket.onclose = null; socket.onerror = null; socket.close(); }
        catch (_) { /* ignore */ }
      }
    }
    run.capture = null;
  }

  async function startStreamingCapture(run: VoiceRun): Promise<void> {
    if (!captureIsStreaming(run)) { startTurnFileCapture(run); return; }
    if (!micStream) { failRun(run, 'mic_disconnected'); return; }
    const capture: StreamingCapture = {
      mode: 'streaming', ws: null, context: null, node: null, sequence: 0, frames: 0,
      bytes: 0, droppedFrames: 0, started: false, stopping: false, result: null,
      closed: false, contextRate: null,
    };
    run.capture = capture;
    capture.resultPromise = new Promise<CaptureResultMessage | null>(resolve => { capture.resolveResult = resolve; });
    // No result timer is armed here: the capture is open for as long as the
    // device takes to answer. The bound is armed when this round is stopped (see
    // `stopCapture`), so a slow answer can never pre-resolve the wait.
    try {
      const audioWindow = window as AudioContextWindow;
      const Context = audioWindow.AudioContext || audioWindow.webkitAudioContext;
      // Ask for the recogniser's rate; the worklet resamples if the browser
      // insists on its own, and the actual context rate is shown to the user.
      const context = new Context({ sampleRate: CAPTURE_TARGET_RATE });
      capture.context = context;
      capture.contextRate = context.sampleRate;
      await (context as AudioContextWithWorklet).audioWorklet.addModule('/static/pcm_capture_worklet.js');
      const source = context.createMediaStreamSource(micStream);
      const node = new AudioWorkletNode(context, 'pcm-capture', {
        processorOptions: { targetRate: CAPTURE_TARGET_RATE, frameSamples: CAPTURE_FRAME_SAMPLES } satisfies PcmCaptureProcessorOptions,
      });
      const silent = context.createGain();
      silent.gain.value = 0; // keep the graph pulled without echoing to speakers
      source.connect(node);
      node.connect(silent);
      silent.connect(context.destination);
      capture.node = node;
      node.port.onmessage = (event: MessageEvent) => {
        const data = (event.data || {}) as WorkletOutboundMessage;
        if (data.type === 'frame') sendAudioFrame(run, data.pcm);
      };
    } catch (error) {
      captureStatus('连续音频采集不可用，未发送任何音频。');
      failRun(run, 'audio_capture_failed');
      return;
    }
    const url = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}`
      + `/api/voice-test/sessions/${run.sessionId}/audio`;
    const socket = new WebSocket(url);
    socket.binaryType = 'arraybuffer';
    capture.ws = socket;
    socket.onopen = () => {
      if (!isActive(run)) { teardownCapture(run); return; }
      socket.send(JSON.stringify({
        type: 'capture_started', turn_id: run.turnId,
        sample_rate: CAPTURE_TARGET_RATE, channels: 1, bits: 16,
      } satisfies CaptureStartedMessage));
    };
    socket.onmessage = (event: MessageEvent) => {
      let message: AudioSocketMessage;
      try { message = JSON.parse(event.data as string) as AudioSocketMessage; } catch (_) { return; }
      if (!isActive(run) || run.finished) {
        // The run already ended: a late callback must not update this session's
        // UI, and it is reported as ignored rather than shown as the answer.
        noteStaleTranscript(run, run && run.finished ? 'run_finished' : 'run_inactive', message);
        return;
      }
      const messageTurn = (message as { turn_id?: string; turnId?: string }).turn_id
        || (message as { turnId?: string }).turnId || null;
      if (messageTurn && run.turnId && messageTurn !== run.turnId) {
        noteStaleTranscript(run, 'stale_turn', message);
        return;
      }
      const loose = message as { ignored?: boolean; stale?: boolean; stale_reason?: string; reason?: string };
      if (loose.ignored || loose.stale || message.type === 'stale_transcript') {
        noteStaleTranscript(run, loose.stale_reason || loose.reason || 'stale', message);
        if (message.type === 'capture_result') {
          // The flow still has to finish; only the display is refused.
          capture.result = message;
          clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
          (capture.resolveResult as (value: CaptureResultMessage | null) => void)(message);
        }
        return;
      }
      if (message.type === 'capture_ready') {
        capture.started = true;
        const rate = capture.contextRate === CAPTURE_TARGET_RATE
          ? '' : `（浏览器采集 ${capture.contextRate} Hz，已重采样到 16 kHz）`;
        captureStatus(`实时识别中：设备回答会边识别边显示${rate}`);
      } else if (message.type === 'partial_transcript') {
        showDeviceText(message.text, 'partial', run.turnId);
        capture.partials = (capture.partials || 0) + 1;
      } else if (message.type === 'final_transcript') {
        showDeviceText(message.text, 'final', run.turnId);
      } else if (message.type === 'capture_result') {
        capture.result = message;
        if (message.final_text) showDeviceText(message.final_text, 'final', run.turnId);
        captureStatus(message.empty_transcript
          ? `本轮未获得设备文本${message.failure ? `（${message.failure}）` : ''}。`
          : '本轮实时识别完成。');
        clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
        (capture.resolveResult as (value: CaptureResultMessage | null) => void)(message);
      } else if (message.type === 'capture_error') {
        capture.error = message.error;
        captureStatus(`实时识别不可用：${message.error}`);        clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
        (capture.resolveResult as (value: CaptureResultMessage | null) => void)(null);
      }
    };
    socket.onerror = () => {
      if (!isActive(run)) return;
      captureStatus('实时识别连接失败。');
      clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
      (capture.resolveResult as (value: CaptureResultMessage | null) => void)(null);
    };
    socket.onclose = () => {
      if (capture.result || capture.closed) return;
      clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
      (capture.resolveResult as (value: CaptureResultMessage | null) => void)(capture.result || null);
    };
  }

  /* Labelled fallback path (capture_mode = turn_file).
   *
   * The browser records the whole answer and uploads it for File ASR. The
   * upload carries a capture identity so the server — not the browser's
   * transcript payload — decides which capture belongs to which turn, and a
   * failed upload/recognition is reported as a failure, never as an empty
   * (successful-looking) transcript that would advance the conversation.
   */
  function startTurnFileCapture(run: VoiceRun): void {
    if (!micStream || typeof MediaRecorder === 'undefined') {
      captureStatus('本机浏览器不支持整轮录音，降级路径不可用。');
      failRun(run, 'recorder_unavailable');
      return;
    }
    const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
      .find(type => MediaRecorder.isTypeSupported(type));
    if (!mimeType) {
      captureStatus('浏览器没有可用的录音格式，降级路径不可用。');
      failRun(run, 'recorder_format_unavailable');
      return;
    }
    const capture: TurnFileCapture = {
      mode: 'turn_file', recorder: null, chunks: [], started: false,
      captureId: run.captureId || null, turnId: run.turnId, mimeType, complete: false,
    };
    run.capture = capture;
    recording = capture;
    try {
      const recorder = new MediaRecorder(micStream, { mimeType, audioBitsPerSecond: 64000 });
      capture.recorder = recorder;
      mediaRecorder = recorder;
      recorder.ondataavailable = (event: BlobEvent) => { if (event.data.size > 0) capture.chunks.push(event.data); };
      recorder.onstop = async () => {
        const item = capture;
        run.capture = null;
        if (recording === item) recording = null;
        if (mediaRecorder === item.recorder) mediaRecorder = null;
        if (!item.complete || !isActive(run)) return;
        const blob = new Blob(item.chunks, { type: item.mimeType });
        if (blob.size < 512) {
          captureStatus('本轮录音过短，未上传，也未记为回答。');
          wsSend(run, { type: 'device_audio_failed', turn_id: item.turnId,
                        reason: 'capture_too_small' });
          return;
        }
        captureStatus('降级模式：正在上传整轮录音并做文件识别…');
        uploadAbort = new AbortController();
        try {
          const response = await fetch(
            `/api/voice-test/sessions/${run.sessionId}/device-audio`,
            {
              method: 'POST', body: blob, signal: uploadAbort.signal,
              headers: {
                'Content-Type': item.mimeType,
                'X-Voice-Capture-Id': item.captureId || '',
                'X-Voice-Turn-Id': item.turnId || '',
                'X-Voice-Mime-Type': item.mimeType,
              },
            });
          if (!response.ok) {
            const detail = await responseMessage(response);
            if (isActive(run)) {
              captureStatus(`文件识别失败：${detail}`);
              wsSend(run, { type: 'device_audio_failed', turn_id: item.turnId,
                            reason: `upload_http_${response.status}`, detail: detail });
            }
            return;
          }
          const body = await response.json() as { status?: string; capture_id?: string };
          if (!isActive(run)) return;
          if (body.status !== 'recognized') {
            captureStatus(`文件识别未产出有效文本（${body.status || 'unknown'}）。`);
            wsSend(run, { type: 'device_audio_failed', turn_id: item.turnId,
                          reason: `asr_${body.status || 'unrecognized'}` });
            return;
          }
          wsSend(run, { type: 'device_audio_ready', turn_id: item.turnId,
                        capture_id: body.capture_id || null });
        } catch (_) {
          if (isActive(run)) {
            captureStatus('上传或识别请求失败。');
            wsSend(run, { type: 'device_audio_failed', turn_id: item.turnId,
                          reason: 'upload_failed' });
          }
        } finally {
          uploadAbort = null;
        }
      };
      recorder.start();
      capture.started = true;
      captureStatus('降级模式：整轮录音 + 文件识别（不是实时 Streaming ASR）。');
    } catch (_) {
      captureStatus('录音不可用。');
      failRun(run, 'audio_capture_failed');
    }
  }

  async function stopCapture(run: VoiceRun | null, reason: string): Promise<CaptureResultMessage | null> {
    const capture = run && run.capture;
    if (!capture || capture.stopping) return null;
    capture.stopping = true;
    if (capture.mode === 'turn_file') {
      // Only a turn that ended on an observed answer end is uploaded; a stop or
      // a control-bound exit marks the recording incomplete and it is discarded.
      capture.complete = (reason === 'speech_end');
      if (capture.recorder && capture.recorder.state === 'recording') capture.recorder.stop();
      return null;
    }
    capture.closed = true;
    if (capture.node) {
      try { capture.node.port.postMessage({ type: 'stop' } satisfies WorkletControlMessage); } catch (_) { /* ignore */ }
    }
    if (capture.ws && capture.ws.readyState === WebSocket.OPEN) {
      capture.ws.send(JSON.stringify({ type: 'capture_stopped', reason: reason || 'speech_end' } satisfies CaptureStoppedMessage));
      // The server now finalises the recogniser and may drain for up to its own
      // no-response bound. Grant that bound plus a margin before giving up, so a
      // normal finalisation is never reported as "no result".
      const serverDrainMs = Number(run && run.bounds.no_response_timeout_ms) || 30000;
      if (capture.finaliseTimer) clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
      capture.finaliseTimer = setTimeout(() => {
        if (capture.result) return;
        captureStatus('服务端未在控制时限内结束本轮识别。');
        (capture.resolveResult as (message: CaptureResultMessage | null) => void)(null);
      }, serverDrainMs + CAPTURE_FINALISE_MARGIN_MS);
    } else {
      clearTimeout(capture.finaliseTimer as ReturnType<typeof setTimeout>);
      if (capture.resolveResult) capture.resolveResult(null);
    }
    return capture.resultPromise || null;
  }

  // -------------------------------------------------------------------- VAD

  async function requestMic(): Promise<boolean> {
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const Context = (window as AudioContextWindow).AudioContext || (window as AudioContextWindow).webkitAudioContext;
      audioContext = new (Context as AudioContextFactory)();
      const source = audioContext.createMediaStreamSource(micStream);
      analyser = audioContext.createAnalyser();
      // A time-domain RMS needs a window long enough to cover several pitch
      // periods; 512 samples at 48 kHz is ~10 ms and made the reading jittery.
      analyser.fftSize = 2048;
      analyser.smoothingTimeConstant = 0.5;
      source.connect(analyser);
      notify('');
      return true;
    } catch (e) {
      notify('麦克风权限被拒绝或不可用：' + (e as Error).message);
      return false;
    }
  }

  function stopMic(): void {
    if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
    if (uploadAbort) { try { uploadAbort.abort(); } catch (_) { /* ignore */ } uploadAbort = null; }
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      // A stop is not a completed answer: the recorder must not upload it.
      if (recording) recording.complete = false;
      try { mediaRecorder.stop(); } catch (_) { /* ignore */ }
    }
    recording = null;
    mediaRecorder = null;
    if (micStream) { micStream.getTracks().forEach(track => track.stop()); micStream = null; }
    if (audioContext) { void audioContext.close(); audioContext = null; }
    analyser = null;
    vadState = 'idle';
  }

  /* One VAD tick's amplitude, as linear PCM RMS in -1..1.
   *
   * Time domain, not frequency domain: this is the amplitude the thresholds are
   * expressed in, so "louder than the room" is a real comparison.
   */
  function timeDomainRms(data: Float32Array): number {
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
    return Math.sqrt(sum / data.length);
  }  function startVAD(run: VoiceRun): void {
    if (!analyser || !isActive(run)) return;
    const analyserNode = analyser;
    run.listening = true;
    vadState = 'listening';
    speechStartMs = 0;
    silenceStartMs = 0;
    const startTime = Date.now();
    const timeoutMs = Number(run.bounds.no_response_timeout_ms) || VAD_TIMEOUT_MS;
    const roundMaxMs = Number(run.bounds.round_observation_max_ms) || VAD_ROUND_MAX_MS;
    const data = new Float32Array(analyserNode.fftSize);
    const noise: number[] = [];
    let calibrated = false;
    // Provisional thresholds until the room's own floor is known. Detection is
    // NOT paused during calibration: the device may answer immediately, and the
    // capture is already running, so a dead window would lose the speech start.
    let startThreshold = VAD_FLOOR * 3;
    let endThreshold = VAD_FLOOR * 1.8;
    run.vad = { calibrated: false, baseline: VAD_FLOOR, baseline_basis: 'absolute_floor',
                start_threshold: startThreshold, end_threshold: endThreshold,
                clean_samples: 0, last_rms: null };

    vadTimer = setInterval(() => {
      if (!isActive(run)) { stopVAD(); return; }
      if (vadState !== 'listening' && vadState !== 'device_speaking') return;
      const vad = run.vad as ControlVadSnapshot;

      analyserNode.getFloatTimeDomainData(data);
      const rms = timeDomainRms(data);
      const now = Date.now();
      vad.last_rms = rms;

      if (!calibrated) {
        // Speech must not be folded into the noise floor.
        if (rms < VAD_CALIBRATION_MAX_RMS) noise.push(rms);
        vad.clean_samples = noise.length;
        if (now - startTime >= VAD_CALIBRATION_MS) {
          if (noise.length >= 8) {
            noise.sort((a, b) => a - b);
            const baseline = Math.max(VAD_FLOOR, noise[Math.floor(noise.length * 0.8)]);
            startThreshold = Math.max(VAD_FLOOR * 3, baseline * 3);
            endThreshold = Math.max(VAD_FLOOR * 1.8, baseline * 1.8);
            vad.baseline = baseline;
            vad.baseline_basis = 'measured_noise_floor';
          }
          // Too few quiet samples means the input never rested; say so and keep
          // the absolute floor rather than inventing a baseline.
          calibrated = true;
          vad.calibrated = true;
          vad.start_threshold = startThreshold;
          vad.end_threshold = endThreshold;
          wsSend(run, {
            type: 'vad_diagnostics', turn_id: run.turnId,
            detail: {
              rms: rms, baseline: vad.baseline, baseline_basis: vad.baseline_basis,
              clean_samples: noise.length,
              start_threshold: startThreshold, end_threshold: endThreshold,
              calibration_ms: VAD_CALIBRATION_MS,
            },
          });
        }
        // Detection itself is not paused while the floor is being estimated: the
        // device may answer immediately, and a dead window would lose the start.
      }

      if (vadState === 'listening') {
        if (rms > startThreshold) {
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
          wsSend(run, { type: 'observation_timeout', turn_id: run.turnId, wait_ms: timeoutMs,
                        reason: 'no_response_observed' });
          updateStatus('未观察到设备回答（已达到控制等待上限）。');
          stopVAD();
          if (run.mode === 'free') void finishFreeTurn(run, 'vad_timeout');
        }
      } else if (vadState === 'device_speaking') {
        if (rms < endThreshold) {
          if (!silenceStartMs) silenceStartMs = now;
          if (now - silenceStartMs > VAD_SILENCE_MS) {
            vadState = 'listening';
            run.stats.observations += 1;
            wsSend(run, { type: 'device_speech_end', turn_id: run.turnId });
            updateStatus('等待下一轮…');
            stopVAD();
            // Free mode: the answer's text comes from the capture, so the turn
            // advances only once the recogniser has been finalised.
            if (run.mode === 'free') void finishFreeTurn(run, 'speech_end');
          }
        } else {
          silenceStartMs = 0;
        }
        if (now - startTime > roundMaxMs) {
          // Speech was detected but its end cannot be confirmed (sustained
          // noise, continuous speech, or a stuck input). Exit explicitly at the
          // control bound; this is never reported as a completed answer.
          vadState = 'idle';
          run.stats.timeouts += 1;
          wsSend(run, { type: 'observation_timeout', turn_id: run.turnId,
                        reason: 'cannot_confirm_response_end', wait_ms: roundMaxMs });
          updateStatus('无法确认回答结束，已按控制上限停止本轮（未记为回答完成）。');
          stopVAD();
          if (run.mode === 'free') void finishFreeTurn(run, 'cannot_confirm_response_end');
        }
      }
    }, 50);
  }

  function stopVAD(): void {
    if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
    vadState = 'idle';
    if (activeRun) activeRun.listening = false;
  }

  function updateStatus(text: string): void {
    const el = $('vt-status');
    if (el) el.textContent = text;
  }

  // ------------------------------------------------------------------ turns

  /* Free mode: the answer's text decides when the turn is over, so the capture
   * is finalised first and the control socket is told to read the result the
   * backend already holds. */
  async function finishFreeTurn(run: VoiceRun, reason: string): Promise<void> {
    if (!isActive(run) || run.pendingCaptureFinish) return;
    run.pendingCaptureFinish = true;
    const capture = run.capture;
    const result = await stopCapture(run, reason);
    if (!isActive(run)) return;
    if (capture && capture.mode === 'turn_file') return; // recorder.onstop drives it
    wsSend(run, { type: 'capture_result', turn_id: run.turnId,
                  stream_id: (result && result.stream_id) || null,
                  reason: reason });
    run.pendingCaptureFinish = false;
  }

  async function handlePlay(run: VoiceRun, msg: PlayMessage): Promise<void> {
    if (!isActive(run)) { run.stats.lateDropped += 1; return; }
    run.stats.playReceived += 1;
    run.turnId = msg.turn_id;
    run.captureId = msg.capture_id || null;
    run.pendingCaptureFinish = false;
    if (msg.capture && msg.capture.mode) {
      run.captureMode = msg.capture.mode;
      run.captureFallback = msg.capture.fallback_reason || null;
    }
    if (msg.playback_start_timeout_ms) run.bounds.playback_start_timeout_ms = msg.playback_start_timeout_ms;
    if (msg.playback_max_duration_ms) run.bounds.playback_max_duration_ms = msg.playback_max_duration_ms;
    if (msg.no_response_timeout_ms) run.bounds.no_response_timeout_ms = msg.no_response_timeout_ms;
    if (msg.round_observation_max_ms) run.bounds.round_observation_max_ms = msg.round_observation_max_ms;

    updateStatus(`正在播放第 ${msg.phrase_index + 1} 句：${msg.text}`);
    const result = await playAudio(run, msg.audio_url);
    if (!isActive(run)) return; // stopped while playing: nothing further happens
    if (result.outcome === 'ended') {
      updateStatus('正在等待设备回答（仅观察疑似回答）。');
      // Free mode needs the device's words, so capture and VAD run together:
      // VAD decides "started/ended speaking", the recogniser supplies the text.
      if (run.mode === 'free') void startStreamingCapture(run);
      startVAD(run);
    } else if (result.outcome === 'failed') {
      failRun(run, result.detail as string);
    }
  }

  // ------------------------------------------------------------- fixed mode

  async function createFixedSession(): Promise<void> {
    const phrasesText = ($('vt-phrases') as HTMLTextAreaElement).value.trim();
    if (!phrasesText) { notify('请输入至少一句话术'); return; }
    const phrases = phrasesText.split('\n').map(s => s.trim()).filter(Boolean);
    if (phrases.length === 0) { notify('请输入至少一句话术'); return; }

    const device = ($('vt-device') as HTMLInputElement | null)?.value || '';
    // Fixed mode reuses already-generated audio without requiring ASR or an LLM;
    // only a run that must synthesize new audio needs TTS.
    const browserMissing = browserCapability('fixed', null);
    const report = await fetchCapability('fixed', null, null);
    if (browserMissing.length || !report || !report.ready) {
      notify(`语音生成尚不能启动：${capabilityText(report, browserMissing)}。未发起语音调用。`);
      return;
    }
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
      session = await resp.json() as VoiceSession;
      notify('');
      startSynthesisProgress(session.session_id, phrases.length);

      const synthResp = await fetch(`/api/voice-test/sessions/${session.session_id}/synthesize`, {
        method: 'POST',
      });
      if (!synthResp.ok) throw new Error('语音生成失败：' + await responseMessage(synthResp));
      const synthResult = await synthResp.json() as Partial<VoiceSession>;
      session = { ...session, ...synthResult };
      const ready = (session.phrases || []).filter(phrase => phrase.status === 'ready').length;
      if (ready !== phrases.length) throw new Error(`语音生成不完整：${ready}/${phrases.length} 条已完成`);

      renderFixedPreview(session.phrases || []);
      ($('vt-start-fixed') as HTMLButtonElement).disabled = false;
      updateGenerationProgress(`已生成 ${ready}/${phrases.length} 条语音，可以试听或开始测试。`);
    } catch (error) {
      notify((error as Error).message || '语音生成请求失败');
      updateGenerationProgress('语音生成未完成，请检查模型配置后重试。');
    } finally {
      stopSynthesisProgress();
      setFixedGenerationBusy(false);
    }
  }

  function renderFixedPreview(phrases: { text?: string; status?: string }[] | string[]): void {
    const container = $('vt-preview');
    if (!container) return;
    container.innerHTML = '';
    phrases.forEach((phrase, i) => {
      const text = typeof phrase === 'string' ? phrase : phrase.text;
      const div = document.createElement('div');
      div.className = 'segment';
      div.innerHTML = `<button class="text-button" data-preview="${i}">试听 ${i + 1}</button><span>${esc(text)}</span>`;
      (div.querySelector('[data-preview]') as HTMLButtonElement).onclick = () => {
        previewAudio(`/api/voice-test/sessions/${(session as VoiceSession).session_id}/audio/${i}`);
      };
      container.appendChild(div);
    });
    container.hidden = false;
  }

  /* ------------------------------------------------------- capability precheck
   *
   * "Configured" is not the same as "callable", and a probe that spends money is
   * not run here. The server reports which required items are missing for the
   * mode the operator chose; the page checks the browser's own audio capability
   * separately. A run whose required items are missing is refused **before** the
   * microphone is raised and before any model or speech call is made.
   */
  function requestedFreeCaptureMode(): CaptureMode {
    const select = $('vt-free-capture-mode') as HTMLSelectElement | null;
    return (select && select.value as CaptureMode) || 'streaming';
  }

  /* AudioWorklet availability, checked without touching the accessor:
   * `AudioContext.prototype.audioWorklet` is a getter that throws when it is read
   * off the prototype instead of a context instance ("Illegal invocation"), so
   * the property is only tested with `in`. */
  function hasAudioWorklet(Context: AudioContextFactory | undefined): boolean {
    if (!Context) return false;
    const base = (window as AudioContextWindow).BaseAudioContext;
    return (!!Context.prototype && 'audioWorklet' in Context.prototype)
      || (!!(base && base.prototype) && 'audioWorklet' in base.prototype);
  }

  function browserCapability(mode: VoiceTestMode, captureMode: CaptureMode | null): string[] {
    const missing: string[] = [];
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      missing.push('浏览器麦克风接口');
    }
    if (mode === 'free') {
      const Context = (window as AudioContextWindow).AudioContext || (window as AudioContextWindow).webkitAudioContext;
      if (captureMode === 'turn_file') {
        if (typeof MediaRecorder === 'undefined') missing.push('浏览器整轮录音（MediaRecorder）');
      } else if (!Context || !hasAudioWorklet(Context)) {
        missing.push('浏览器连续采集（AudioWorklet）');
      }
    }
    return missing;
  }

  function capabilityText(report: CapabilityReport | null, browserMissing: string[]): string {
    const parts: string[] = [];
    if (report) {
      const names = (report.missing_names || report.missing || []).join('、');
      parts.push(report.ready
        ? `服务端能力检查通过（${report.capture_mode === 'turn_file' ? '降级：整轮录音 + 文件识别' : '实时 Streaming ASR'}）`
        : `服务端缺少：${names}`);
    }
    if (browserMissing && browserMissing.length) parts.push(`浏览器缺少：${browserMissing.join('、')}`);
    return parts.join('；');
  }

  async function fetchCapability(mode: VoiceTestMode, captureMode: CaptureMode | null, sessionId: string | null): Promise<CapabilityReport | null> {
    const params = new URLSearchParams();
    if (captureMode) params.set('capture_mode', captureMode);
    if (sessionId) params.set('session_id', sessionId);
    const query = params.toString();
    let response: Response;
    try {
      response = await fetch(`/api/voice-test/capabilities/${mode}${query ? `?${query}` : ''}`);
    } catch (_) {
      return null;
    }
    // A session the backend no longer knows about is reported as lost, not as a
    // capability problem: the page must say "restart" rather than "configure".
    if (response.status === 404 && sessionId) return { ready: false, lost: true };
    if (!response.ok) return null;
    try {
      return await response.json() as CapabilityReport;
    } catch (_) {
      return null;
    }
  }

  /* Check without starting anything (the 检查能力 button and the test hooks). */
  async function checkCapability(): Promise<{ report: CapabilityReport | null; browserMissing: string[]; text: string }> {
    const freeView = $('vt-free');
    const mode: VoiceTestMode = freeView && !freeView.hidden ? 'free' : 'fixed';
    const captureMode = mode === 'free' ? requestedFreeCaptureMode() : null;
    const report = await fetchCapability(mode, captureMode, session ? session.session_id : null);
    const browserMissing = browserCapability(mode, captureMode);
    const text = capabilityText(report, browserMissing);
    const el = $('vt-capability');
    if (el) el.textContent = text;
    notify(report && report.ready && !browserMissing.length ? '' : `能力检查未通过：${text}。未发起模型或语音调用。`);
    return { report, browserMissing, text };
  }

  async function startFixedTest(): Promise<void> {
    if (!session) { notify('请先生成语音'); return; }
    const browserMissing = browserCapability('fixed', null);
    const report = await fetchCapability('fixed', null, session.session_id);
    if (report && report.lost) {
      // The backend restarted and lost the session: say so and stay restartable.
      setRunningButtons('fixed', false);
      updateStatus('会话已失效（后端可能已重启）。请重新生成语音后再开始。');
      notify('会话已失效（后端可能已重启）。');
      return;
    }
    if (browserMissing.length || !report || !report.ready) {
      notify(`固定对话尚不能启动：${capabilityText(report, browserMissing)}。未发起模型或语音调用。`);
      return;
    }
    const hasMic = await requestMic();
    if (!hasMic) return;

    if (activeRun) stopRun(activeRun); // an old run must never keep control
    const run = newRun(session.session_id, 'fixed');
    activeRun = run;

    const wsUrl = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/api/voice-test/sessions/${session.session_id}/ws`;
    const socket = new WebSocket(wsUrl);
    run.ws = socket;
    run.socketState = 'connecting';

    socket.onopen = () => {
      if (!isActive(run)) return;
      run.opened = true;
      run.socketState = 'open';
      setRunningButtons('fixed', true);
      updateStatus('正在启动…');
      wsSend(run, { type: 'start' });
    };

    socket.onmessage = (event: MessageEvent) => {
      if (!isActive(run)) { run.stats.lateDropped += 1; return; }
      let msg: ControlMessage;
      try { msg = JSON.parse(event.data as string) as ControlMessage; } catch (_) { return; }
      if (msg.type === 'play') {
        void handlePlay(run, msg);
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
        failRun(run, msg.detail || msg.reason || '');
      } else if (msg.type === 'error') {
        failRun(run, msg.reason || '服务端错误');
      }
    };

    socket.onclose = (event: CloseEvent) => {
      if (run.cancelled) return; // expected after a stop
      if (run.finished) { setRunningButtons('fixed', false); return; }
      // A socket that never opened means the backend refused the session
      // (unknown session, e.g. after a restart), not a mid-run drop.
      const lost = !run.opened || !!(event && event.code === 4004);
      run.socketState = lost ? 'lost' : 'closed';
      failRun(run, lost ? 'session_lost' : 'socket_closed');
    };

    socket.onerror = () => {
      if (!isActive(run)) return;
      failRun(run, run.opened ? 'socket_closed' : 'session_lost');
    };
  }

  function stopFixedTest(): void {
    stopRun(activeRun && activeRun.mode === 'fixed' ? activeRun : null);
  }

  // -------------------------------------------------------------- free mode

  async function startFreeTest(): Promise<void> {
    const goal = ($('vt-free-goal') as HTMLTextAreaElement).value.trim();
    if (!goal) { notify('请输入测试目标'); return; }
    const constraints = ((($('vt-free-constraints') as HTMLTextAreaElement).value) || '').split('\n').map(s => s.trim()).filter(Boolean);
    const maxTurns = parseInt(($('vt-free-max-turns') as HTMLInputElement).value || '10', 10);
    // The downgrade is an explicit operator choice, never an automatic switch.
    const captureMode = requestedFreeCaptureMode();

    // Refuse before raising the microphone and before any model or speech call.
    const browserMissing = browserCapability('free', captureMode);
    const report = await fetchCapability('free', captureMode, null);
    const capabilityEl = $('vt-capability');
    const text = capabilityText(report, browserMissing);
    if (capabilityEl) capabilityEl.textContent = text;
    if (browserMissing.length || !report || !report.ready) {
      notify(`自由对话尚不能启动：${text}。未发起模型或语音调用。`);
      return;
    }

    notify('正在创建会话…');
    const resp = await fetch('/api/voice-test/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'free', goal, constraints, max_turns: maxTurns,
                             capture_mode: captureMode }),
    });
    if (!resp.ok) { notify('创建会话失败：' + await responseMessage(resp)); return; }
    session = await resp.json() as VoiceSession;
    notify('');

    const hasMic = await requestMic();
    if (!hasMic) return;

    if (activeRun) stopRun(activeRun);
    const run = newRun(session.session_id, 'free');
    activeRun = run;

    const wsUrl = `ws${location.protocol === 'https:' ? 's' : ''}://${location.host}/api/voice-test/sessions/${session.session_id}/ws`;
    const socket = new WebSocket(wsUrl);
    run.ws = socket;
    run.socketState = 'connecting';
    const freeLog = $('vt-free-log');
    if (freeLog) freeLog.innerHTML = '';

    socket.onopen = () => {
      if (!isActive(run)) return;
      run.opened = true;
      run.socketState = 'open';
      setRunningButtons('free', true);
      updateStatus('正在生成第一句话术…');
      startProgressPolling(run);
      wsSend(run, { type: 'start' });
    };

    socket.onmessage = (event: MessageEvent) => {
      if (!isActive(run)) { run.stats.lateDropped += 1; return; }
      let msg: ControlMessage;
      try { msg = JSON.parse(event.data as string) as ControlMessage; } catch (_) { return; }
      if (msg.type === 'play') {
        appendFreeLog('平台', msg.text);
        if (msg.device_text) appendFreeLog('设备', msg.device_text);
        void handlePlay(run, msg);
      } else if (msg.type === 'blocked') {
        // The server refused the start: nothing was called, nothing is running.
        run.finished = true;
        stopLocal(run, null, { silent: true });
        const names = (msg.missing_names || msg.missing || []).join('、');
        updateStatus(`自由对话已拒绝启动（缺少 ${names}）。未发起模型或语音调用。`);
        appendFreeLog('系统', `拒绝启动：缺少 ${names}`);
        notify(`自由对话尚不能启动：缺少 ${names}。未发起模型或语音调用。`);
      } else if (msg.type === 'capture_mode') {
        // Say which observation path this run uses; never imply streaming when
        // the run is on the labelled fallback.
        run.captureMode = msg.mode;
        run.captureFallback = msg.fallback_reason || null;
        if (msg.mode === 'streaming') {
          captureStatus('本轮使用实时 Streaming ASR 观察设备回答。');
          appendFreeLog('系统', '观察方式：实时 Streaming ASR（Control Evidence）');
        } else {
          const why = msg.fallback_reason === 'configured_turn_file'
            ? '操作者显式选择降级' : (msg.fallback_reason || '按配置选择');
          captureStatus(`本轮使用降级路径：整轮录音 + 文件识别（${why}），不是实时 Streaming ASR。`);
          appendFreeLog('系统', `观察方式：降级（整轮录音 + 文件识别，${why}）`);
        }
      } else if (msg.type === 'listening') {
        updateStatus('检测到疑似回答（浏览器 VAD 提示，未确认说话人）。');
      } else if (msg.type === 'ignored') {
        run.stats.ignored += 1;
        // A refused observation is counted, never silently dropped: an ignored
        // message explains why a round did not advance (Issue #113). Two rounds are
        // *not* reported this way: one whose finalisation outlived the control bound
        // ends as an explicit `failed` with `capture_finalisation_timeout`, and one
        // whose capture never existed at all stays on its observation-policy bound
        // while the session snapshot names it (`progress.reason_code =
        // capture_result_without_capture`).
      } else if (msg.type === 'no_response') {
        appendFreeLog('系统', '未观察到设备回答（按策略继续）');
      } else if (msg.type === 'complete') {
        run.finished = true;
        stopLocal(run, null, { silent: true });
        // The run ended because the last answer was observed and recorded, so
        // the confirmed line still describes a real round.
        appendFreeLog('系统', '测试结束：' + (msg.reason || ''));
        updateStatus('测试完成：' + (msg.reason || ''));
      } else if (msg.type === 'stopped') {
        const timedOut = msg.reason === 'no_response_timeout';
        stopLocal(run, null, { silent: true });
        // A stopped run has no confirmed answer for the round on screen: drop the
        // line instead of leaving a text that the trace does not treat as one.
        clearDeviceText(run);
        appendFreeLog('系统', '已停止' + (timedOut ? '：未观察到设备回答' : ''));
        updateStatus(timedOut
          ? '已停止：未观察到设备回答（已达到控制等待上限）。'
          : `已停止：${msg.reason || ''}`);
      } else if (msg.type === 'failed') {
        failRun(run, msg.detail || msg.reason || '');
      } else if (msg.type === 'error') {
        failRun(run, msg.reason || '服务端错误');
      }
    };

    socket.onclose = (event: CloseEvent) => {
      if (run.cancelled) return;
      if (run.finished) { setRunningButtons('free', false); return; }
      const lost = !run.opened || !!(event && event.code === 4004);
      run.socketState = lost ? 'lost' : 'closed';
      failRun(run, lost ? 'session_lost' : 'socket_closed');
    };

    socket.onerror = () => {
      if (!isActive(run)) return;
      failRun(run, run.opened ? 'socket_closed' : 'session_lost');
    };
  }

  function stopFreeTest(): void {
    stopRun(activeRun && activeRun.mode === 'free' ? activeRun : null);
  }

  function appendFreeLog(role: string, text: string): void {
    const log = $('vt-free-log');
    if (!log) return;
    const div = document.createElement('div');
    div.className = 'segment';
    div.innerHTML = `<div><strong>${esc(role)}</strong><p>${esc(text)}</p></div>`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  // --- Public API ---
  return {
    createFixedSession,
    startFixedTest,
    stopFixedTest,
    startFreeTest,
    stopFreeTest,
    checkCapability,
    stopMic,
    controlState,
    integrityState,
  };
})();

if (typeof window !== 'undefined') {
  /** Browser Station control layer, published for the page and the browser tests. */
  (window as Window & { VT?: typeof VT }).VT = VT;
}