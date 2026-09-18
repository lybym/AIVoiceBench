"use strict";
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
const labels = {
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
function statusLabel(status) {
    if (!status)
        return '';
    return labels[status] || status;
}
// ---------------------------------------------------------------------------
// Model settings view
// ---------------------------------------------------------------------------
/** Protocol name → the purposes that protocol can actually serve. */
const protocolCaps = {
    openai_chat: ['judge'],
    volcengine_tts: ['tts'],
    volcengine_asr: ['asr', 'diarization'],
    custom_speech: ['tts', 'asr', 'diarization'],
};
/** Purpose → display label used by the routing table and the capability list. */
const purposes = {
    tts: '语音生成',
    asr: '语音识别',
    diarization: '说话人分析',
    judge: '结果分析',
};
/** Server-side fields that must never be echoed back in a write payload. */
const MODEL_SERVER_ONLY_FIELDS = [
    'credential_configured',
    'adapter_status',
    'adapter_capabilities',
];
/** Drop read-only server decorations before posting a profile back. */
function cleanProfile(profile) {
    return Object.fromEntries(Object.entries(profile).filter(([key]) => !MODEL_SERVER_ONLY_FIELDS.includes(key)));
}
/** Binary audio-socket frame header length in bytes. */
const AUDIO_FRAME_HEADER_BYTES = 4;
/** Bytes of PCM payload in a header-plus-payload frame of this total size. */
function audioFramePayloadBytes(totalBytes) {
    return Math.max(0, totalBytes - AUDIO_FRAME_HEADER_BYTES);
}
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
function encodeAudioFrameHeader(sequence) {
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
function frameCapturePayload(sequence, pcm) {
    const payload = new Uint8Array(pcm);
    const frame = new Uint8Array(AUDIO_FRAME_HEADER_BYTES + payload.length);
    frame.set(encodeAudioFrameHeader(sequence), 0);
    frame.set(payload, AUDIO_FRAME_HEADER_BYTES);
    return frame;
}
/** Is this an audio frame the main thread must forward? */
function isWorkletPcmFrame(message) {
    return !!message && message.type === 'frame';
}
// ------------------------------------------------------------ model settings view
let modelState = null;
let editingId = null;
let clearSecret = false;
async function modelsView() {
    view('models');
    closeModel();
    try {
        modelState = await request('/api/models');
        drawModels();
    }
    catch (error) {
        notify(error.message);
    }
}
function drawModels() {
    if (!modelState)
        return;
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
    $('model-list').querySelectorAll('[data-edit]').forEach(button => {
        button.onclick = () => editModel(button.dataset.edit);
    });
    $('model-list').querySelectorAll('[data-delete]').forEach(button => {
        button.onclick = () => void deleteModel(button.dataset.delete);
    });
}
function closeModel() {
    $('model-editor').hidden = true;
    editingId = null;
    clearSecret = false;
    $('model-form').reset();
}
/** Parameter editor specification: key → [label, input type, placeholder]. */
const paramSpec = {
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
function populateParams(values = {}) {
    const chat = $('model-protocol').value === 'openai_chat';
    $('model-params').innerHTML = Object.entries(paramSpec)
        .filter(([key]) => chat
        ? ['temperature', 'max_tokens', 'timeout_seconds'].includes(key)
        : !['temperature', 'max_tokens'].includes(key))
        .map(([key, [label, type, placeholder]]) => `<label>${label}<input data-param="${key}" type="${type}" ${type === 'number' ? 'step="any"' : ''} placeholder="${placeholder}" value="${esc(values[key] ?? '')}"></label>`)
        .join('');
}
function chooseCapabilities(selected) {
    const caps = protocolCaps[$('model-protocol').value] || [];
    $('model-capabilities').innerHTML = caps
        .map(capability => `<label><input type="checkbox" value="${capability}" ${(!selected || selected.includes(capability)) ? 'checked' : ''}> ${purposes[capability]}</label>`)
        .join('');
}
function editModel(id) {
    editingId = id || null;
    clearSecret = false;
    const form = $('model-form');
    form.reset();
    const profile = modelState?.profiles.find(item => item.id === id);
    $('editor-title').textContent = profile ? '编辑模型' : '添加模型';
    if (profile) {
        for (const [key, value] of Object.entries(profile)) {
            const element = form.elements.namedItem(key);
            if (element)
                element.value = String(value);
        }
    }
    $('model-editor').hidden = false;
    $('secret-state').textContent = profile?.credential_configured
        ? '已有密钥，留空不会覆盖。'
        : '尚未保存密钥。';
    chooseCapabilities(profile?.capabilities);
    populateParams(profile?.parameters);
    $('model-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('model-protocol').onchange = () => {
    chooseCapabilities();
    populateParams();
};
$('clear-secret').onclick = () => {
    clearSecret = true;
    const apiKey = $('model-form').elements.namedItem('api_key');
    if (apiKey)
        apiKey.value = '';
    $('secret-state').textContent = '保存后将清除本地密钥；环境变量不受影响。';
};
/**
 * Write profiles/routes/secrets back to the server.
 *
 * The request is bound to the revision that was read, so a concurrent edit is
 * refused by the server instead of silently overwriting it.
 */
async function commitModels(profiles, routes, secrets = {}) {
    modelState = await request('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            expected_revision: modelState ? modelState.revision : 0,
            profiles: profiles.map(profile => cleanProfile(profile)),
            routes,
            secrets,
        }),
    });
    drawModels();
    closeModel();
    notify('模型设置已保存，将应用于下一次运行。');
}
async function saveRoutes() {
    if (!modelState)
        return;
    try {
        const routes = Object.fromEntries(Object.keys(purposes).map(key => [key, $('route-' + key).value || null]));
        await commitModels(modelState.profiles, routes);
    }
    catch (error) {
        notify(error.message);
    }
}
async function deleteModel(id) {
    if (!modelState)
        return;
    try {
        const routes = Object.fromEntries(Object.entries(modelState.routes).map(([key, value]) => [key, value === id ? null : value]));
        await commitModels(modelState.profiles.filter(profile => profile.id !== id), routes);
    }
    catch (error) {
        notify(error.message);
    }
}
$('model-form').onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const id = editingId || 'model-' + Date.now().toString(36);
    const profile = {
        id,
        enabled: form.get('enabled') === 'true',
        capabilities: Array.from($('model-capabilities').querySelectorAll('input:checked')).map(input => input.value),
        parameters: {},
        name: '',
        provider: '',
        model: '',
    };
    for (const key of ['name', 'provider', 'protocol', 'model', 'base_url', 'credential_env']) {
        profile[key] = String(form.get(key)).trim();
    }
    $('model-params').querySelectorAll('input').forEach(input => {
        if (input.value.trim()) {
            profile.parameters[input.dataset.param] = input.type === 'number'
                ? Number(input.value)
                : input.value.trim();
        }
    });
    const key = String(form.get('api_key')).trim();
    const secrets = key ? { [id]: key } : (clearSecret ? { [id]: null } : {});
    const profiles = [...(modelState ? modelState.profiles.filter(item => item.id !== id) : []), profile];
    const routes = Object.fromEntries(Object.entries(modelState ? modelState.routes : {}).map(([routeKey, value]) => [routeKey, value === id && (!profile.enabled || !profile.capabilities.includes(routeKey)) ? null : value]));
    try {
        await commitModels(profiles, routes, secrets);
    }
    catch (error) {
        notify(error.message);
    }
};
