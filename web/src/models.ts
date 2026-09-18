/* AIVoiceBench — Browser Station shared type and contract layer.
 *
 * This module is the TypeScript source of `aivoicebench/static/models.js`
 * (compiled by `npm run build`). It is loaded before `app.js` and `voice_test.js`
 * as a classic script, so it shares the page's global script scope: the
 * interfaces below are visible to every other Browser Station source file, and
 * the runtime values it installs are the ones the model-settings page used to
 * define directly.
 *
 * Two groups live here:
 *
 * 1. **Model settings view** — the profiles/routes editor (unchanged behaviour).
 * 2. **Browser Station measurement contracts** — explicit types for the objects
 *    that carry measurement risk on the browser side: PCM audio frames and their
 *    sequence/sample counter, VAD/control observations, capture integrity
 *    (dropped/gap/duplicate/stale), requested media constraints vs actual
 *    `MediaTrackSettings`, the control/WebSocket lifecycle and the Active Voice
 *    Test Run/Turn lifecycle.
 *
 * A type declaration is not evidence. These interfaces constrain the browser
 * implementation; they do not create Measurement truth, and `sample_index /
 * sample_rate` remains the primary acoustic timebase
 * (docs/25-active-measurement.md).
 *
 * Ownership boundary: domain *types* and pure helpers only. Anything that
 * touches the registry, a browser device or a Provider Credential belongs to
 * `app.ts` / `voice_test.ts`; nothing in this file may read a secret or
 * decorate a request with one.
 */

// ---------------------------------------------------------------------------
// Shared view vocabulary
// ---------------------------------------------------------------------------

/** Status vocabulary shared by the analysis and Voice Test views. */
const labels: Record<string, string> = {
  partial: '部分完成',
  complete: '已完成',
  failed: '处理失败',
  insufficient_evidence: '证据不足',
  pending: '等待处理',
  observed: '已观测',
  unknown: '待确认',
  low_confidence: '待复核',
};

/** Chinese label for a status code; the raw code is the fallback. */
function statusLabel(status: string | null | undefined): string {
  if (!status) return '';
  return labels[status] || status;
}

// ---------------------------------------------------------------------------
// Model settings view
// ---------------------------------------------------------------------------

/** Protocol name → the purposes that protocol can actually serve. */
const protocolCaps: Record<string, string[]> = {
  openai_chat: ['judge'],
  volcengine_tts: ['tts'],
  volcengine_asr: ['asr', 'diarization'],
  custom_speech: ['tts', 'asr', 'diarization'],
};

/** Purpose → display label used by the routing table and the capability list. */
const purposes: Record<string, string> = {
  tts: '语音生成',
  asr: '语音识别',
  diarization: '说话人分析',
  judge: '结果分析',
};

/** Server-side fields that must never be echoed back in a write payload. */
const MODEL_SERVER_ONLY_FIELDS: readonly string[] = [
  'credential_configured',
  'adapter_status',
  'adapter_capabilities',
];

/** Drop read-only server decorations before posting a profile back. */
function cleanProfile(profile: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(profile).filter(([key]) => !MODEL_SERVER_ONLY_FIELDS.includes(key)),
  );
}

// ---------------------------------------------------------------------------
// Browser Station measurement contracts
//
// The Browser Station is the on-site measurement endpoint. The types below
// describe the objects it produces and consumes. They describe the existing wire
// contract (docs/25-active-measurement.md) rather than replacing it: the
// server-side payload definitions remain authoritative, and these types are
// checked in the Browser Station build/typecheck gate.
// ---------------------------------------------------------------------------

/** Which Active Voice Test mode a run belongs to. */
type VoiceTestMode = 'fixed' | 'free';

/**
 * Result of the server-side capability precheck (`GET /api/voice-test/capabilities/{mode}`).
 * `lost` is produced inside the page when a session id is no longer known so a
 * backend restart is reported as "restart", never as "configure".
 */
interface CapabilityReport {
  ready: boolean;
  missing?: string[];
  missing_names?: string[];
  capture_mode?: CaptureMode;
  lost?: boolean;
}

/** Observation path a Free Voice Test run resolved to. */
type CaptureMode = 'streaming' | 'turn_file';

/**
 * Why a run is on the labelled fallback instead of real-time Streaming ASR.
 * `configured_turn_file` is an explicit operator choice and is always shown as
 * such; a fallback is never presented as streaming.
 */
type CaptureFallbackReason = 'configured_turn_file' | 'configured' | string;

/** Server-reported capture plan for a turn (`play.capture`). */
interface CapturePlan {
  mode: CaptureMode;
  fallback_reason?: CaptureFallbackReason | null;
}

// ---------------------------------------------------------------- audio frames

/**
 * One PCM frame posted by the AudioWorklet to the main thread.
 *
 * `pcm` is a transferable ArrayBuffer of mono, 16-bit signed little-endian
 * samples. `samples` is the number of valid samples in this frame (the final
 * frame of a capture may be shorter than `frameSamples`).
 *
 * This is *not* the wire frame: the main thread prefixes a 4-byte big-endian
 * sequence number before sending it over the binary audio socket.
 */
interface WorkletPcmFrame {
  type: 'frame';
  samples: number;
  pcm: ArrayBuffer;
}

/** The worklet finished and will produce no further frames. */
interface WorkletStoppedMessage {
  type: 'stopped';
}

/** Main thread → worklet control messages. */
interface WorkletControlMessage {
  type: 'stop';
}

/** Every message the PCM capture worklet can post. */
type WorkletOutboundMessage = WorkletPcmFrame | WorkletStoppedMessage;

/**
 * Frame sequence header: a 32-bit big-endian sequence number in the first four
 * bytes of every binary audio-socket frame. Byte order is part of the wire
 * contract and must not change with the migration.
 */
interface AudioFrameHeader {
  /** Monotonic index of this frame within the capture; starts at 0. */
  sequence: number;
  /** Header length in bytes (the sequence number is 32 bits). */
  headerBytes: 4;
}

/** Binary audio-socket frame header length in bytes. */
const AUDIO_FRAME_HEADER_BYTES = 4;

/** Bytes of PCM payload in a header-plus-payload frame of this total size. */
function audioFramePayloadBytes(totalBytes: number): number {
  return Math.max(0, totalBytes - AUDIO_FRAME_HEADER_BYTES);
}

/** Format of the PCM the capture socket carries. */
interface AudioStreamFormat {
  /** Recogniser-configured sample rate; the worklet resamples to this. */
  sampleRate: 16000;
  /** Downmixed channel count; the recogniser consumes one channel. */
  channels: 1;
  /** Signed PCM bit depth. */
  bits: 16;
}

/** `capture_started`: the page announces the audio format it will stream.
 *
 * The socket payload keeps the snake_case field names of the wire contract, so
 * this message spells the `AudioStreamFormat` fields out explicitly. */
interface CaptureStartedMessage {
  type: 'capture_started';
  turn_id: string | null;
  sample_rate: AudioStreamFormat['sampleRate'];
  channels: AudioStreamFormat['channels'];
  bits: AudioStreamFormat['bits'];
}
/** `capture_stopped`: the page ends the capture for a turn. */
interface CaptureStoppedMessage {
  type: 'capture_stopped';
  reason: CaptureStopReason | string;
}

/** Why a capture was finalised. */
type CaptureStopReason = 'speech_end' | 'vad_timeout' | 'cannot_confirm_response_end' | string;

/**
 * `capture_result`: the backend's reading of the capture that just ended.
 *
 * The server — not the page's own transcript payload — decides which capture
 * belongs to which turn. A failed or empty capture is reported as
 * `empty_transcript`/`failure`, never as a successful empty answer.
 */
interface CaptureResultMessage {
  type: 'capture_result';
  turn_id?: string | null;
  stream_id?: string | null;
  final_text?: string | null;
  empty_transcript?: boolean;
  failure?: string | null;
  /** The backend refuses late provider output: the page must refuse it too. */
  stale?: boolean;
  stale_reason?: string;
  ignored?: boolean;
  reason?: string;
}

/** `capture_error`: the capture path could not run at all. */
interface CaptureErrorMessage {
  type: 'capture_error';
  error: string;
}

/** Messages the page accepts on the binary audio socket. */
type AudioSocketMessage =
  | { type: 'capture_ready' }
  | { type: 'partial_transcript'; text: string; turn_id?: string | null }
  | { type: 'final_transcript'; text: string; turn_id?: string | null }
  | CaptureResultMessage
  | CaptureErrorMessage
  | { type: 'stale_transcript'; reason?: string; turn_id?: string | null; session_id?: string };

// -------------------------------------------------------------- integrity

/**
 * How captured audio was lost after the worklet produced it. Every counter is
 * kept locally so a lossy run is describable instead of silently truncated.
 *
 * `dropped_frames` is bounded backpressure (the socket's send queue exceeded
 * `CAPTURE_BACKPRESSURE_BYTES`); the counters are deliberately *not* folded into
 * a single "quality" number, because a dropped frame is not an acoustic
 * measurement.
 */
interface CaptureIntegrity {
  /** Frames the worklet produced and the page sent, in sequence order. */
  frames_sent: number;
  /** PCM bytes actually sent (frame payload only, header excluded). */
  bytes_sent: number;
  /** Next frame sequence number the page will use. */
  next_sequence: number;
  /** Frames refused because the socket send queue was over the bound. */
  dropped_frames: number;
  /** Last observed `WebSocket.bufferedAmount`, for diagnosing backpressure. */
  last_buffered_amount: number;
  /**
   * Detected gaps are recorded, never repaired: a missing sequence number is
   * missing audio and must stay visible.
   */
  gap_frames: number;
  /** Frames whose payload size did not match `2 * samples`. */
  malformed_frames: number;
  /** Transcript callbacks refused because they belonged to a closed turn/run. */
  stale_callbacks: number;
}

/**
 * Browser-side VAD/control observation snapshot.
 *
 * The RMS is a **time-domain** linear-PCM amplitude in -1..1, so a threshold is
 * a real amplitude comparison. The baseline is either the absolute floor or a
 * measured room noise floor, and `baseline_basis` says which — an unmeasured
 * baseline is never presented as measured.
 *
 * VAD only reports a *suspected* response: it does not identify the speaker and
 * does not measure a formal response latency (PRD-F020).
 */
interface ControlVadSnapshot {
  calibrated: boolean;
  baseline: number;
  baseline_basis: 'absolute_floor' | 'measured_noise_floor';
  start_threshold: number;
  end_threshold: number;
  clean_samples: number;
  /** Most recent RMS reading; `null` before the first tick. */
  last_rms: number | null;
}

/** `vad_diagnostics`: the calibrated thresholds, sent once per observation round. */
interface VadDiagnosticsMessage {
  type: 'vad_diagnostics';
  turn_id: string | null;
  detail: {
    rms: number;
    baseline: number;
    baseline_basis: ControlVadSnapshot['baseline_basis'];
    clean_samples: number;
    start_threshold: number;
    end_threshold: number;
    calibration_ms: number;
  };
}

/** Control events the page reports to the server for the turn it is observing. */
type ControlObservationMessage =
  | CaptureStartedMessage
  | CaptureStoppedMessage
  | { type: 'playback_started'; turn_id: string | null }
  | { type: 'playback_ended'; turn_id: string | null }
  | { type: 'playback_failed'; turn_id: string | null; reason: PlaybackFailureReason | string }
  | { type: 'playback_cancelled'; turn_id: string | null; reason: string }
  | { type: 'device_speech_start'; turn_id: string | null }
  | { type: 'device_speech_end'; turn_id: string | null }
  | { type: 'observation_timeout'; turn_id: string | null; wait_ms: number; reason: ObservationTimeoutReason | string }
  | { type: 'device_audio_ready'; turn_id: string | null; capture_id: string | null }
  | { type: 'device_audio_failed'; turn_id: string | null; reason: string; detail?: string }
  | { type: 'capture_result'; turn_id: string | null; stream_id: string | null; reason: string }
  | VadDiagnosticsMessage;

/** Why playback was reported as failed instead of completed. */
type PlaybackFailureReason =
  | 'audio_error'
  | 'play_rejected'
  | 'playback_did_not_start'
  | 'playback_stuck';

/**
 * Why an observation round ended without a confirmed answer. A silent device is
 * never recorded as the end of a response (PRD-F020).
 */
type ObservationTimeoutReason = 'no_response_observed' | 'cannot_confirm_response_end';

// ---------------------------------------------------------- media settings

/** A requested audio constraint; `exact`/`ideal` are preserved as given. */
interface AudioConstraintRequest {
  deviceId?: ConstrainDOMString;
  sampleRate?: ConstrainULong;
  channelCount?: ConstrainULong;
  echoCancellation?: ConstrainBoolean;
  noiseSuppression?: ConstrainBoolean;
  autoGainControl?: ConstrainBoolean;
  latency?: ConstrainDouble;
}

/** What the browser actually granted, read from `MediaTrackSettings`. */
interface AudioTrackSettingsSnapshot {
  deviceId?: string;
  sampleRate?: number;
  channelCount?: number;
  echoCancellation?: boolean;
  noiseSuppression?: boolean;
  autoGainControl?: boolean;
  latency?: number;
}

/**
 * Provenance record for the browser audio path.
 *
 * Browser audio enhancement (AEC / Noise Suppression / AGC) may be applied even
 * when it was requested off, so the *actual* settings are recorded next to the
 * requested constraints and are never assumed. `constraints` is what the page
 * asked for; `settings` is what the browser reports. A missing `settings` field
 * is recorded as unknown, not as "off".
 *
 * Provenance is collected only through this contract; it is not a Measurement
 * Event and does not replace an `Evidence` record.
 */
interface MediaProvenanceSnapshot {
  /** `navigator.mediaDevices.getUserMedia` constraints actually requested. */
  constraints: AudioConstraintRequest;
  /** `MediaStreamTrack.getSettings()` for the live microphone track. */
  settings: AudioTrackSettingsSnapshot;
  /** `MediaStreamTrack.getCapabilities()` when the browser exposes it. */
  capabilities?: Record<string, unknown>;
  userAgent: string;
  /** Effective audio context rate, which may differ from the requested rate. */
  audioContextSampleRate?: number;
}

// ------------------------------------------------------- control lifecycle

/**
 * Liveness of the control socket that drives a run. `lost` means the backend no
 * longer knows the session (e.g. after a restart) and the operator must restart
 * the run rather than reconfigure it.
 */
type ControlSocketState = 'idle' | 'connecting' | 'open' | 'closed' | 'lost';

/** How a Run ended, as reported by the control socket. */
type RunStopReason =
  | 'all_phrases_done'
  | 'user_stop'
  | 'no_response_timeout'
  | 'max_turns'
  | 'agent_stop'
  | string;

/** Control messages the server sends on the session control socket. */
type ControlMessage =
  | PlayMessage
  | { type: 'listening' }
  | { type: 'ignored' }
  | { type: 'no_response' }
  | { type: 'blocked'; missing?: string[]; missing_names?: string[] }
  | { type: 'capture_mode'; mode: CaptureMode; fallback_reason?: CaptureFallbackReason | null }
  | { type: 'complete'; reason?: RunStopReason }
  | { type: 'stopped'; reason?: RunStopReason }
  | { type: 'failed'; reason?: string; detail?: string }
  | { type: 'error'; reason?: string };

/** `play`: play this phrase, then observe the device for the turn. */
interface PlayMessage {
  type: 'play';
  turn_id: string;
  phrase_index: number;
  text: string;
  audio_url: string;
  capture_id?: string | null;
  capture?: CapturePlan | null;
  device_text?: string | null;
  playback_start_timeout_ms?: number;
  playback_max_duration_ms?: number;
  no_response_timeout_ms?: number;
  round_observation_max_ms?: number;
}

/** Bounded control waits for one run; not product performance targets. */
interface ControlBounds {
  playback_start_timeout_ms: number;
  playback_max_duration_ms: number;
  no_response_timeout_ms: number;
  round_observation_max_ms: number;
}

/** Counter set used to prove stop/timeout/late-event behaviour in tests. */
interface RunStats {
  playReceived: number;
  playbackStarted: number;
  playbackEnded: number;
  observations: number;
  timeouts: number;
  ignored: number;
  failures: number;
  lateDropped: number;
  playWaitSettled: number;
  playWaitCancelled: number;
  staleLogged?: number;
}

/**
 * Read-only diagnostics of the control layer.
 *
 * This is the support/test surface (`window.VT.controlState()`), not a
 * Measurement Event: it reports control facts so a stopped, timed-out or
 * late-event run is explainable.
 */
interface ControlStateSnapshot {
  session_id: string | null;
  mode: VoiceTestMode;
  seq: number;
  cancelled: boolean;
  finished: boolean;
  turn_id: string | null;
  listening: boolean;
  has_audio: boolean;
  pending_play_wait: boolean;
  capture_mode: CaptureMode | null;
  capture_fallback_reason: CaptureFallbackReason | null;
  has_capture: boolean;
  vad: ControlVadSnapshot | null;
  stats: RunStats;
}

/** Outcome of awaiting one stimulus playback. */
type PlaybackOutcome = 'ended' | 'failed' | 'cancelled';

// ---------------------------------------------------------------- helpers

/** The page's own read-only view of its Browser Station contracts, for tests. */
const BROWSER_STATION_CONTRACT = Object.freeze({
  /** Recogniser target rate; the worklet resamples the microphone to this. */
  captureTargetRate: 16000,
  /** 200 ms at 16 kHz. */
  captureFrameSamples: 3200,
  /** Binary audio-socket sequence header size in bytes. */
  audioFrameHeaderBytes: 4,
  /** Bound on `WebSocket.bufferedAmount` before frames are dropped. */
  captureBackpressureBytes: 262144,
  /** Bound on waiting for a capture result for one turn. */
  captureResultTimeoutMs: 15000,
  /** Control bound: stop waiting for a suspected response after this long. */
  noResponseTimeoutMs: 30000,
  /** Control escape hatch once speech was detected. */
  roundObservationMaxMs: 90000,
});

/** 4-byte big-endian frame sequence header, byte for byte as sent on the wire. */
function encodeAudioFrameHeader(sequence: number): Uint8Array {
  const header = new Uint8Array(AUDIO_FRAME_HEADER_BYTES);
  header[0] = (sequence >>> 24) & 0xff;
  header[1] = (sequence >>> 16) & 0xff;
  header[2] = (sequence >>> 8) & 0xff;
  header[3] = sequence & 0xff;
  return header;
}

/**
 * Prefix one PCM payload with its sequence header.
 *
 * Kept next to the contract so the framing rule has a single definition; the
 * binary layout (4-byte big-endian sequence, then little-endian signed 16-bit
 * PCM) is unchanged by the TypeScript migration.
 */
function frameCapturePayload(sequence: number, pcm: ArrayBufferLike): Uint8Array {
  const payload = new Uint8Array(pcm);
  const frame = new Uint8Array(AUDIO_FRAME_HEADER_BYTES + payload.length);
  frame.set(encodeAudioFrameHeader(sequence), 0);
  frame.set(payload, AUDIO_FRAME_HEADER_BYTES);
  return frame;
}

/** Is this an audio frame the main thread must forward? */
function isWorkletPcmFrame(message: WorkletOutboundMessage | undefined | null): message is WorkletPcmFrame {
  return !!message && message.type === 'frame';
}

/** `GET/POST /api/models` document. */
interface ModelState {
  revision: number;
  profiles: ModelProfile[];
  routes: Record<string, string | null>;
}

/** One configured model. Server-only fields are read but never written back. */
interface ModelProfile {
  id: string;
  name: string;
  provider: string;
  protocol?: string;
  model: string;
  base_url?: string;
  credential_env?: string;
  enabled: boolean;
  capabilities: string[];
  parameters: Record<string, string | number>;
  credential_configured?: boolean;
  adapter_status?: string;
  adapter_capabilities?: string[];
}

/** One Voice Test session as returned by the session API. */
interface VoiceSession {
  session_id: string;
  phrases?: { text?: string; status?: string }[];
  [key: string]: unknown;
}

// ------------------------------------------------------------ model settings view

let modelState: ModelState | null = null;
let editingId: string | null = null;
let clearSecret = false;

async function modelsView(): Promise<void> {
  view('models');
  closeModel();
  try {
    modelState = await request<ModelState>('/api/models');
    drawModels();
  } catch (error) {
    notify((error as Error).message);
  }
}

function drawModels(): void {
  if (!modelState) return;
  const state = modelState;
  $('settings-revision').textContent = '配置版本 ' + state.revision + ' · 下次运行生效';
  $('model-hint').textContent = state.revision === 0
    ? '尚未保存模型配置，分析仍沿用服务器环境变量。保存后以本页默认模型为准。'
    : '未选择的用途保持未配置；火山 TTS 使用 API Key + v3 SSE，须填写资源 ID 与音色 ID。';
  $('routes').innerHTML = Object.entries(purposes).map(([key, label]) => {
    const options = state.profiles
      .filter(profile => profile.enabled && profile.capabilities.includes(key))
      .map(profile => `<option value="${esc(profile.id)}" ${state.routes[key] === profile.id ? 'selected' : ''}>${esc(profile.name)}${!profile.adapter_capabilities?.includes(key) ? ' · 待接入' : ''}</option>`)
      .join('');
    return `<label>${label}<select id="route-${key}"><option value="">未配置</option>${options}</select></label>`;
  }).join('');
  $('model-list').innerHTML = state.profiles.length
    ? state.profiles.map(profile => `<div class="panel model-card"><div class="title-row"><div><h2>${esc(profile.name)}</h2><p>${esc(profile.provider)} · ${esc(profile.model)}</p></div><button class="text-button" data-edit="${esc(profile.id)}">编辑 →</button></div><div class="model-tags"><span class="badge">${profile.capabilities.map(capability => purposes[capability]).join(' / ')}</span>${badge(!profile.enabled ? 'pending' : profile.adapter_status === 'not_integrated' ? 'pending' : profile.credential_configured ? 'complete' : 'insufficient_evidence')}<span class="quiet">${!profile.enabled ? '已停用' : profile.adapter_status === 'not_integrated' ? '适配器待接入' : profile.credential_configured ? '密钥已配置 · 尚未验证连通' : '尚未配置密钥'}</span></div><button class="text-button remove-model" data-delete="${esc(profile.id)}">移除配置</button></div>`).join('')
    : '<div class="panel empty"><strong>还没有模型配置</strong>添加服务与模型，再为任务选择默认模型。</div>';
  $('model-list').querySelectorAll<HTMLElement>('[data-edit]').forEach(button => {
    button.onclick = () => editModel(button.dataset.edit);
  });
  $('model-list').querySelectorAll<HTMLElement>('[data-delete]').forEach(button => {
    button.onclick = () => void deleteModel(button.dataset.delete as string);
  });
}

function closeModel(): void {
  ($('model-editor') as HTMLElement).hidden = true;
  editingId = null;
  clearSecret = false;
  ($('model-form') as HTMLFormElement).reset();
}

/** Parameter editor specification: key → [label, input type, placeholder]. */
const paramSpec: Record<string, [string, string, string]> = {
  temperature: ['Temperature', 'number', '0.3'],
  max_tokens: ['最大输出 Token', 'number', '4096'],
  timeout_seconds: ['超时（秒）', 'number', '30'],
  voice: ['音色 ID', 'text', ''],
  speed: ['语速（火山：-50 至 100）', 'number', '0'],
  volume: ['音量（火山：-100 至 100）', 'number', '0'],
  pitch: ['音高（火山：-100 至 100）', 'number', '0'],
  sample_rate: ['采样率', 'number', '16000'],
  format: ['音频格式（火山 TTS：wav）', 'text', 'wav'],
  resource_id: ['资源 ID', 'text', ''],
};

function populateParams(values: Record<string, string | number> = {}): void {
  const chat = ($('model-protocol') as HTMLSelectElement).value === 'openai_chat';
  $('model-params').innerHTML = Object.entries(paramSpec)
    .filter(([key]) => chat
      ? ['temperature', 'max_tokens', 'timeout_seconds'].includes(key)
      : !['temperature', 'max_tokens'].includes(key))
    .map(([key, [label, type, placeholder]]) =>
      `<label>${label}<input data-param="${key}" type="${type}" ${type === 'number' ? 'step="any"' : ''} placeholder="${placeholder}" value="${esc(values[key] ?? '')}"></label>`)
    .join('');
}

function chooseCapabilities(selected?: string[]): void {
  const caps = protocolCaps[($('model-protocol') as HTMLSelectElement).value] || [];
  $('model-capabilities').innerHTML = caps
    .map(capability => `<label><input type="checkbox" value="${capability}" ${(!selected || selected.includes(capability)) ? 'checked' : ''}> ${purposes[capability]}</label>`)
    .join('');
}

function editModel(id?: string | null): void {
  editingId = id || null;
  clearSecret = false;
  const form = $('model-form') as HTMLFormElement;
  form.reset();
  const profile = modelState?.profiles.find(item => item.id === id);
  $('editor-title').textContent = profile ? '编辑模型' : '添加模型';
  if (profile) {
    for (const [key, value] of Object.entries(profile)) {
      const element = form.elements.namedItem(key) as HTMLInputElement | null;
      if (element) element.value = String(value);
    }
  }
  ($('model-editor') as HTMLElement).hidden = false;
  $('secret-state').textContent = profile?.credential_configured
    ? '已有密钥，留空不会覆盖。'
    : '尚未保存密钥。';
  chooseCapabilities(profile?.capabilities);
  populateParams(profile?.parameters);
  $('model-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

($('model-protocol') as HTMLSelectElement).onchange = () => {
  chooseCapabilities();
  populateParams();
};

$('clear-secret').onclick = () => {
  clearSecret = true;
  const apiKey = ($('model-form') as HTMLFormElement).elements.namedItem('api_key') as HTMLInputElement | null;
  if (apiKey) apiKey.value = '';
  $('secret-state').textContent = '保存后将清除本地密钥；环境变量不受影响。';
};

/**
 * Write profiles/routes/secrets back to the server.
 *
 * The request is bound to the revision that was read, so a concurrent edit is
 * refused by the server instead of silently overwriting it.
 */
async function commitModels(
  profiles: ModelProfile[],
  routes: Record<string, string | null>,
  secrets: Record<string, string | null> = {},
): Promise<void> {
  modelState = await request<ModelState>('/api/models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      expected_revision: modelState ? modelState.revision : 0,
      profiles: profiles.map(profile => cleanProfile(profile as unknown as Record<string, unknown>)),
      routes,
      secrets,
    }),
  });
  drawModels();
  closeModel();
  notify('模型设置已保存，将应用于下一次运行。');
}

async function saveRoutes(): Promise<void> {
  if (!modelState) return;
  try {
    const routes = Object.fromEntries(Object.keys(purposes).map(key =>
      [key, ($('route-' + key) as HTMLSelectElement).value || null]));
    await commitModels(modelState.profiles, routes);
  } catch (error) {
    notify((error as Error).message);
  }
}

async function deleteModel(id: string): Promise<void> {
  if (!modelState) return;
  try {
    const routes = Object.fromEntries(Object.entries(modelState.routes).map(([key, value]) =>
      [key, value === id ? null : value]));
    await commitModels(modelState.profiles.filter(profile => profile.id !== id), routes);
  } catch (error) {
    notify((error as Error).message);
  }
}

($('model-form') as HTMLFormElement).onsubmit = async event => {
  event.preventDefault();
  const form = new FormData(event.target as HTMLFormElement);
  const id = editingId || 'model-' + Date.now().toString(36);
  const profile: ModelProfile = {
    id,
    enabled: form.get('enabled') === 'true',
    capabilities: Array.from($('model-capabilities').querySelectorAll<HTMLInputElement>('input:checked')).map(input => input.value),
    parameters: {},
    name: '',
    provider: '',
    model: '',
  };
  for (const key of ['name', 'provider', 'protocol', 'model', 'base_url', 'credential_env'] as const) {
    (profile as unknown as Record<string, string>)[key] = String(form.get(key)).trim();
  }
  $('model-params').querySelectorAll<HTMLInputElement>('input').forEach(input => {
    if (input.value.trim()) {
      profile.parameters[input.dataset.param as string] = input.type === 'number'
        ? Number(input.value)
        : input.value.trim();
    }
  });
  const key = String(form.get('api_key')).trim();
  const secrets: Record<string, string | null> = key ? { [id]: key } : (clearSecret ? { [id]: null } : {});
  const profiles = [...(modelState ? modelState.profiles.filter(item => item.id !== id) : []), profile];
  const routes = Object.fromEntries(Object.entries(modelState ? modelState.routes : {}).map(([routeKey, value]) =>
    [routeKey, value === id && (!profile.enabled || !profile.capabilities.includes(routeKey)) ? null : value]));
  try {
    await commitModels(profiles, routes, secrets);
  } catch (error) {
    notify((error as Error).message);
  }
};