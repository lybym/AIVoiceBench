"use strict";
/* AIVoiceBench — Web Evidence Workbench (wavesurfer.js presentation layer).
 *
 * This file is the TypeScript source of `aivoicebench/static/workbench.js`. It is a
 * classic script, not a module: `index.html` loads it with
 * `<script src="/static/workbench.js">` after `app.js`, `models.js` and
 * `voice_test.js`, so every top-level declaration stays on the page's shared global
 * script scope (see `tests/test_web_station_build.py`).
 *
 * Responsibility boundary — PRD-F014, docs/26 §6, docs/22 “时间与事实边界”:
 *
 * - wavesurfer.js is only a renderer/interaction layer. This module never computes a
 *   metric, event, turn, role or speech boundary, never re-derives a region
 *   coordinate and never creates a Region from a local heuristic.
 * - Every region comes from `document.regions[]` exactly as the backend served it:
 *   `start_sec` / `end_sec` are passed through untouched (the only unit conversion is
 *   the one the backend already performed), and `metric.value` is displayed verbatim
 *   (`null` renders as `N/A`, never `0`).
 * - When `gate.role_dependent_available === false` the provisional banner and the
 *   `unavailable` list are rendered verbatim instead of inventing role-dependent
 *   results. A `role: "unknown"` / `uncertain` region keeps its own colour and label
 *   and is never presented as tester or device.
 * - The evidence panel only resolves *served* links: a record whose own `region_id`
 *   matches, or a transcript segment whose backend-resolved `region_id` names a
 *   published region. It never builds an id from a naming convention — ASR
 *   utterance ids and acoustic segment ids are different namespaces, so the
 *   backend, not this layer, decides which region a transcript row belongs to.
 * - `WB.selectEvidence` returns `null` for an unknown region id; no region is ever
 *   synthesised.
 *
 * The module is no-op safe: evaluating the compiled file only defines functions and
 * publishes `window.WB`, so a stripped DOM stub or a missing `WaveSurfer` global
 * cannot throw at load time.
 *
 * Verification levels: `scripts/verify-workbench-render.mjs` (Node gate) checks the
 * projection contract on a synthetic document, and `tests/test_web_workbench.py`
 * checks the vendored artifact/provenance contract. Neither is browser behaviour
 * evidence — real wavesurfer rendering still needs the real-browser acceptance run.
 */
/* ------------------------------------------------------------------ */
/* Vendor assets and presentation constants                            */
/* ------------------------------------------------------------------ */
/** Same-origin vendor path. No CDN is ever loaded at runtime. */
const WB_VENDOR_BASE = '/static/vendor/';
/** Vendor load order: core first, then the plugins that attach to its namespace. */
const WB_VENDOR_SCRIPTS = [
    WB_VENDOR_BASE + 'wavesurfer.min.js',
    WB_VENDOR_BASE + 'regions.min.js',
    WB_VENDOR_BASE + 'timeline.min.js',
];
/** Optional navigation helper for long recordings (5 minutes and above). */
const WB_MINIMAP_SCRIPT = WB_VENDOR_BASE + 'minimap.min.js';
const WB_MINIMAP_MIN_DURATION_MS = 300000;
/** Presentation-only height of the waveform canvas, in pixels. */
const WB_WAVEFORM_HEIGHT = 96;
/** Region colour per backend track id. Presentation only, never evidence. */
const WB_TRACK_COLORS = {
    acoustic: 'rgba(34, 116, 92, 0.20)',
    speaker: 'rgba(38, 94, 158, 0.20)',
    turn: 'rgba(112, 82, 160, 0.20)',
    event: 'rgba(190, 120, 30, 0.22)',
    metric: 'rgba(32, 128, 118, 0.20)',
    finding: 'rgba(172, 58, 84, 0.22)',
};
const WB_DEFAULT_REGION_COLOR = 'rgba(90, 104, 96, 0.20)';
/** A region whose role is unknown/uncertain must never look like tester/device. */
const WB_UNKNOWN_REGION_COLOR = 'rgba(120, 131, 124, 0.16)';
/** A provisional (role-dependent, unconfirmed) region gets its own muted colour. */
const WB_PROVISIONAL_REGION_COLOR = 'rgba(166, 142, 62, 0.14)';
/** Every panel this module writes into. Cleared as one unit on mount/unmount. */
const WB_PANEL_IDS = [
    'wb-gate', 'wb-unavailable', 'wb-tracks', 'wb-evidence',
    'wb-findings', 'wb-metrics', 'wb-events', 'wb-turns', 'wb-transcript',
    'wb-revision', 'wb-provenance', 'wb-abstentions',
];
/* ------------------------------------------------------------------ */
/* Small helpers (kept local so this file is standalone-loadable)      */
/* ------------------------------------------------------------------ */
/** HTML text escaping. Kept local: the Node render gate loads this file alone. */
const wbEsc = (value) => String(value ?? '')
    .replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));
/** Element lookup that tolerates a stripped DOM stub. */
function wbNode(id) {
    if (typeof document === 'undefined' || !document || typeof document.getElementById !== 'function')
        return null;
    return document.getElementById(id);
}
/** `undefined`/`null` become `''`; every other value is rendered verbatim. */
function wbString(value) {
    return value === null || value === undefined ? '' : String(value);
}
/** `undefined` becomes `null`; every other value is passed through unchanged. */
function wbStringOrNull(value) {
    return value === null || value === undefined ? null : String(value);
}
/** Only the documented `undefined -> null` normalisation, nothing else. */
function wbOrNull(value) {
    return value === null || value === undefined ? null : value;
}
/** A served id list, or `null` when the backend did not publish one. */
function wbList(value) {
    return Array.isArray(value) ? value.map(item => String(item)) : null;
}
/** `N/A` for a missing value; nested objects are shown as their JSON text. */
function wbValueText(value) {
    if (value === null || value === undefined)
        return 'N/A';
    if (typeof value === 'object') {
        try {
            return JSON.stringify(value);
        }
        catch {
            return String(value);
        }
    }
    return String(value);
}
/** Boolean evidence flags as an explicit yes/no/unknown, never a guess. */
function wbFlag(value) {
    if (value === true)
        return '是';
    if (value === false)
        return '否';
    return 'N/A';
}
/** Error text for the status line. */
function wbMessageOf(error) {
    return error instanceof Error ? error.message : String(error);
}
function wbTruncate(value, limit) {
    return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
}
/** `start_sec` / `end_sec` pass through byte-for-byte; a non-number is `null`. */
function wbNumberOrNull(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
/** Role label. `unknown` stays a deliberate state, never tester/device. */
function wbRoleLabel(role) {
    const name = wbString(role);
    if (name === 'tester')
        return '测试者（用户人工确认）';
    if (name === 'device')
        return 'AI 设备（用户人工确认）';
    if (name === 'unknown')
        return '角色待确认（用户明确选择未知）';
    return 'N/A';
}
/* ------------------------------------------------------------------ */
/* Projections                                                         */
/* ------------------------------------------------------------------ */
/**
 * One row per `document.regions[]`, in the served order.
 *
 * `start`/`end` are the document's `start_sec`/`end_sec` unchanged — this module
 * never converts or re-derives a boundary. `uncertain`/`provisional` are coerced to
 * booleans so the renderer can pick a distinct style; every other field is passed
 * through as served.
 */
function wbRegionsFromDocument(document) {
    const regions = document && Array.isArray(document.regions) ? document.regions : [];
    return regions.map(region => ({
        region_id: wbString(region.region_id),
        track_id: wbStringOrNull(region.track_id),
        kind: wbStringOrNull(region.kind),
        label: wbStringOrNull(region.label),
        start: wbNumberOrNull(region.start_sec),
        end: wbNumberOrNull(region.end_sec),
        role: wbStringOrNull(region.role),
        role_basis: wbStringOrNull(region.role_basis),
        evidence_class: wbStringOrNull(region.evidence_class),
        status: wbStringOrNull(region.status),
        confidence: wbNumberOrNull(region.confidence),
        uncertainty: wbStringOrNull(region.uncertainty),
        uncertain: region.uncertain === true,
        provisional: region.provisional === true,
        envelope_of: wbNumberOrNull(region.envelope_of),
        source: region.source || null,
    }));
}
/** Verbatim projection of `document.metrics`, in order. */
function wbMetricRows(document) {
    const metrics = document && Array.isArray(document.metrics) ? document.metrics : [];
    return metrics.map(metric => ({
        metric_id: wbStringOrNull(metric.metric_id),
        name: wbStringOrNull(metric.name),
        value: wbOrNull(metric.value),
        unit: wbStringOrNull(metric.unit),
        status: wbStringOrNull(metric.status),
        reason: wbStringOrNull(metric.reason),
        turn_id: wbStringOrNull(metric.turn_id),
        region_id: wbStringOrNull(metric.region_id),
        span_origin: wbStringOrNull(metric.span_origin),
        evidence_ids: wbList(metric.evidence_ids),
        provisional: wbOrNull(metric.provisional),
    }));
}
/** Verbatim projection of `document.findings`, in order. */
function wbFindingRows(document) {
    const findings = document && Array.isArray(document.findings) ? document.findings : [];
    return findings.map(finding => ({
        finding_id: wbStringOrNull(finding.finding_id),
        title: wbStringOrNull(finding.title),
        severity: wbStringOrNull(finding.severity),
        status: wbStringOrNull(finding.status),
        confidence: wbOrNull(finding.confidence),
        description: wbStringOrNull(finding.description),
        region_id: wbStringOrNull(finding.region_id),
        span_origin: wbStringOrNull(finding.span_origin),
        requires_log_verification: wbOrNull(finding.requires_log_verification),
        human_review: finding.human_review || null,
        evidence_ids: wbList(finding.evidence_ids),
        event_ids: wbList(finding.event_ids),
        metric_ids: wbList(finding.metric_ids),
        turn_ids: wbList(finding.turn_ids),
        provisional: wbOrNull(finding.provisional),
    }));
}
/** Verbatim projection of `document.events`, in order. */
function wbEventRows(document) {
    const events = document && Array.isArray(document.events) ? document.events : [];
    return events.map(event => ({
        event_id: wbStringOrNull(event.event_id),
        type: wbStringOrNull(event.type),
        start_ms: wbOrNull(event.start_ms),
        end_ms: wbOrNull(event.end_ms),
        source: wbStringOrNull(event.source),
        confidence: wbOrNull(event.confidence),
        turn_id: wbStringOrNull(event.turn_id),
        region_id: wbStringOrNull(event.region_id),
        evidence_ids: wbList(event.evidence_ids),
    }));
}
/** Verbatim projection of `document.turns`, in order. */
function wbTurnRows(document) {
    const turns = document && Array.isArray(document.turns) ? document.turns : [];
    return turns.map(turn => ({
        turn_id: wbStringOrNull(turn.turn_id),
        start_ms: wbOrNull(turn.start_ms),
        end_ms: wbOrNull(turn.end_ms),
        region_id: wbStringOrNull(turn.region_id),
        has_interruption: wbOrNull(turn.has_interruption),
        has_overlap: wbOrNull(turn.has_overlap),
    }));
}
/** Verbatim projection of `document.transcript`, in order. */
function wbTranscriptRows(document) {
    const segments = document && Array.isArray(document.transcript) ? document.transcript : [];
    return segments.map(segment => ({
        segment_id: wbStringOrNull(segment.segment_id),
        region_id: wbStringOrNull(segment.region_id),
        region_ids: Array.isArray(segment.region_ids)
            ? segment.region_ids.map(value => wbString(value)).filter(value => value !== '')
            : null,
        region_basis: wbStringOrNull(segment.region_basis),
        region_span_matches: typeof segment.region_span_matches === 'boolean'
            ? segment.region_span_matches
            : null,
        start_ms: wbOrNull(segment.start_ms),
        end_ms: wbOrNull(segment.end_ms),
        text: wbStringOrNull(segment.text),
        speaker_id: wbStringOrNull(segment.speaker_id),
        speaker_role: wbStringOrNull(segment.speaker_role),
        timestamp_source: wbStringOrNull(segment.timestamp_source),
    }));
}
/**
 * The served region a transcript segment belongs to, or `null`.
 *
 * The backend resolves the link and publishes it as `region_id`, because a
 * transcript segment id and an acoustic segment id are different evidence
 * namespaces (`ASR-####` versus `SEG-*`): no naming convention joins them, and this
 * layer must not invent one. The served id is returned only when it names a region
 * the backend also published, so an unresolvable id can never become a link.
 */
function wbTranscriptRegionId(document, row) {
    const wanted = wbStringOrNull(row.region_id);
    if (!wanted)
        return null;
    const regions = document && Array.isArray(document.regions) ? document.regions : [];
    for (const region of regions) {
        if (wbStringOrNull(region.region_id) === wanted)
            return wanted;
    }
    return null;
}
/* ------------------------------------------------------------------ */
/* Module state                                                        */
/* ------------------------------------------------------------------ */
let wbRunId = '';
let wbDocument = null;
let wbLoaded = false;
let wbError = null;
let wbStateValue = null;
let wbSelectedRegionId = null;
let wbStatusLines = [];
let wbVendorPromise = null;
/** Bumped by every mount/unmount so an in-flight mount cannot render stale data. */
let wbMountToken = 0;
let wbPlayer = null;
let wbPlayerReady = false;
let wbPlayerPlaying = false;
let wbPlayerTime = 0;
const wbPlugins = { regions: null, timeline: null, minimap: null };
function wbWaveSurferGlobal() {
    if (typeof window === 'undefined' || !window)
        return null;
    const candidate = window.WaveSurfer;
    return candidate && typeof candidate.create === 'function' ? candidate : null;
}
/* ------------------------------------------------------------------ */
/* Late-loading the vendored, same-origin library                      */
/* ------------------------------------------------------------------ */
/**
 * Inject one same-origin vendor `<script>` and await its load event.
 *
 * Nothing is fetched from a CDN: the only accepted sources are the pinned files
 * under `/static/vendor/`, served by the same FastAPI/Docker static chain as the
 * rest of the page.
 */
function wbLoadScript(source) {
    return new Promise((resolve, reject) => {
        if (typeof document === 'undefined' || !document || typeof document.createElement !== 'function') {
            reject(new Error('当前环境没有可用的 document，无法加载 ' + source));
            return;
        }
        const existing = document.querySelector('script[data-wb-vendor="' + source + '"]');
        if (existing) {
            const state = existing.dataset.wbLoaded;
            if (state === 'ready') {
                resolve();
                return;
            }
            if (state === 'failed') {
                reject(new Error('无法加载 ' + source));
                return;
            }
            existing.addEventListener('load', () => resolve());
            existing.addEventListener('error', () => reject(new Error('无法加载 ' + source)));
            return;
        }
        const script = document.createElement('script');
        script.src = source;
        script.async = false;
        script.dataset.wbVendor = source;
        script.addEventListener('load', () => { script.dataset.wbLoaded = 'ready'; resolve(); });
        script.addEventListener('error', () => { script.dataset.wbLoaded = 'failed'; reject(new Error('无法加载 ' + source)); });
        const host = document.head || document.body;
        if (!host) {
            reject(new Error('文档缺少可插入 vendor 脚本的位置'));
            return;
        }
        host.appendChild(script);
    });
}
/** Load core, then regions, then timeline (plugin order matters), then Minimap. */
function wbLoadVendor() {
    if (wbVendorPromise)
        return wbVendorPromise;
    const chain = WB_VENDOR_SCRIPTS.reduce((previous, source) => previous.then(() => wbLoadScript(source)), Promise.resolve());
    wbVendorPromise = chain
        .then(() => wbLoadScript(WB_MINIMAP_SCRIPT).catch(() => undefined))
        .catch(error => { wbVendorPromise = null; throw error; });
    return wbVendorPromise;
}
/* ------------------------------------------------------------------ */
/* State                                                              */
/* ------------------------------------------------------------------ */
/** Shape check. A document that is not the backend's is treated as missing. */
function wbIsDocument(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value))
        return false;
    const candidate = value;
    if (typeof candidate.schema_version !== 'string' || !candidate.schema_version)
        return false;
    if (candidate.regions != null && !Array.isArray(candidate.regions))
        return false;
    if (candidate.tracks != null && !Array.isArray(candidate.tracks))
        return false;
    if (candidate.metrics != null && !Array.isArray(candidate.metrics))
        return false;
    return true;
}
function wbBuildState() {
    const document = wbDocument;
    return {
        runId: wbString(document ? document.run_id : null) || wbRunId,
        documentId: document ? wbStringOrNull(document.document_id) : null,
        gate: document ? document.gate || null : null,
        revision: document ? document.revision || null : null,
        regionCount: wbRegionsFromDocument(document).length,
        availableTracks: document && Array.isArray(document.tracks) ? document.tracks.slice() : [],
        unavailable: document && Array.isArray(document.unavailable) ? document.unavailable.slice() : [],
        loaded: wbLoaded && document !== null,
        error: wbError,
    };
}
/**
 * Store one backend workbench document.
 *
 * A missing or malformed document is recorded as an explicit error state *and*
 * thrown: the module never invents a document, a track, a region or a coordinate.
 * `WB.state()` stays readable after the throw, carrying `loaded: false` and `error`.
 */
function wbLoad(runId, document) {
    wbRunId = wbString(runId);
    if (!wbIsDocument(document)) {
        wbDocument = null;
        wbLoaded = false;
        wbError = '证据工作台文档缺失或不合法：后端未返回 workbench 文档；浏览器不会在本地合成证据、区间或坐标。';
        wbStateValue = wbBuildState();
        throw new Error(wbError);
    }
    wbDocument = document;
    wbLoaded = true;
    wbError = null;
    wbStateValue = wbBuildState();
    return wbStateValue;
}
/* ------------------------------------------------------------------ */
/* Status and panel plumbing                                           */
/* ------------------------------------------------------------------ */
function wbRenderStatus() {
    const node = wbNode('wb-status');
    if (!node)
        return;
    node.innerHTML = wbStatusLines
        .map(line => '<span class="wb-status-line">' + wbEsc(line) + '</span>')
        .join('');
}
function wbSetStatus(...lines) {
    wbStatusLines = lines.filter(line => !!line);
    wbRenderStatus();
}
function wbAppendStatus(line) {
    if (!line)
        return;
    wbStatusLines.push(line);
    wbRenderStatus();
}
function wbRenderPlayerState() {
    const node = wbNode('wb-player');
    if (!node)
        return;
    const state = wbPlayerState();
    if (!state) {
        node.textContent = '';
        return;
    }
    // The playhead is reported from the player's own events. It is an interaction
    // position, not evidence: no persisted coordinate is read from or written to it.
    node.textContent = '播放位置 ' + state.currentTime.toFixed(2) + ' / ' + state.duration.toFixed(2) + ' s · '
        + (state.ready ? '波形已就绪' : '波形加载中')
        + (state.timeline ? ' · Timeline' : '')
        + (state.minimap ? ' · Minimap' : '')
        + '（浏览器播放位置只用于交互，不覆盖后端坐标）';
}
function wbClearPanels() {
    WB_PANEL_IDS.forEach(id => {
        const node = wbNode(id);
        if (node)
            node.innerHTML = '';
    });
    const player = wbNode('wb-player');
    if (player)
        player.textContent = '';
}
/* ------------------------------------------------------------------ */
/* Table rendering                                                     */
/* ------------------------------------------------------------------ */
function wbTable(headers, rows) {
    if (!rows.length)
        return '';
    return '<div class="table-wrap"><table><thead><tr>'
        + headers.map(header => '<th>' + wbEsc(header) + '</th>').join('')
        + '</tr></thead><tbody>'
        + rows.map(cells => '<tr>' + cells.map(cell => '<td>' + cell + '</td>').join('') + '</tr>').join('')
        + '</tbody></table></div>';
}
function wbKeyValueTable(pairs) {
    if (!pairs.length)
        return '';
    return '<div class="table-wrap"><table><tbody>'
        + pairs.map(pair => '<tr><th>' + wbEsc(pair[0]) + '</th><td>' + wbEsc(pair[1]) + '</td></tr>').join('')
        + '</tbody></table></div>';
}
/** A click target that selects an id the backend actually served. */
function wbButton(label, regionId) {
    if (!regionId)
        return wbEsc(label);
    return '<button class="text-button wb-row" data-wb-region="' + wbEsc(regionId) + '">' + wbEsc(label) + '</button>';
}
function wbWireRowButtons(node) {
    node.querySelectorAll('[data-wb-region]').forEach(button => {
        button.onclick = () => { wbSelectEvidence(button.dataset.wbRegion || null); };
    });
}
function wbSetPanel(id, title, headers, rows) {
    const node = wbNode(id);
    if (!node)
        return;
    node.innerHTML = '<h3>' + wbEsc(title) + '</h3>'
        + (rows.length ? wbTable(headers, rows) : '<p class="quiet">后端未提供这类证据。</p>');
    wbWireRowButtons(node);
}
function wbDetailTable(title, detail) {
    if (!detail || typeof detail !== 'object')
        return '';
    const keys = Object.keys(detail).sort();
    if (!keys.length)
        return '';
    return '<h4>' + wbEsc(title) + '</h4>'
        + wbKeyValueTable(keys.map(key => [key, wbValueText(detail[key])]));
}
/* ------------------------------------------------------------------ */
/* Panels                                                              */
/* ------------------------------------------------------------------ */
function wbRenderGate(document) {
    const node = wbNode('wb-gate');
    if (!node)
        return;
    const gate = document.gate || null;
    const roleDependent = !!(gate && gate.role_dependent_available === true);
    const status = wbValueText(gate ? gate.status : null);
    const viewKind = wbValueText(gate ? gate.view_kind : null);
    node.className = 'wb-gate ' + (roleDependent ? 'wb-gate-confirmed' : 'wb-gate-provisional');
    node.innerHTML = roleDependent
        ? '<strong>角色确认视图（' + wbEsc(viewKind) + '）</strong>'
            + '<p>gate.status = ' + wbEsc(status) + '。turn / metric / finding 证据来自后端已保存的人工角色修订。</p>'
        : '<strong>临时视图 provisional · 角色依赖证据不可用</strong>'
            + '<p>gate.status = ' + wbEsc(status) + ' · view_kind = ' + wbEsc(viewKind) + '</p>'
            + (gate && gate.reason ? '<p>原因：' + wbEsc(wbString(gate.reason)) + '</p>' : '')
            + '<p>后端未发布 turn / metric / finding 轨道与区间，因此这里不会出现任何依赖确认角色的结论。'
            + '浏览器不会从声学或聚类证据推断 tester/device，请先完成人工角色确认。</p>';
}
function wbRenderUnavailable(document) {
    const node = wbNode('wb-unavailable');
    if (!node)
        return;
    const items = Array.isArray(document.unavailable) ? document.unavailable : [];
    const rows = items.map(item => [
        wbValueText(item.stage), wbValueText(item.status), wbValueText(item.reason),
    ]);
    node.innerHTML = '<h3>不可用阶段（后端原文）</h3>'
        + (rows.length
            ? wbTable(['阶段', '状态', '原因'], rows)
            : '<p class="quiet">后端未报告不可用阶段。</p>')
        + '<p>阶段失败与弃权在这里显式列出，避免出现无法解释的空白。</p>';
}
function wbRegionColorForTrack(trackId) {
    return WB_TRACK_COLORS[trackId] || WB_DEFAULT_REGION_COLOR;
}
/** Distinct style for unknown / uncertain / provisional evidence. */
function wbRegionStyleClass(row) {
    if (row.role === 'unknown' || row.uncertain)
        return 'unknown';
    if (row.provisional)
        return 'provisional';
    return 'track';
}
function wbRegionColor(row) {
    if (row.role === 'unknown' || row.uncertain)
        return WB_UNKNOWN_REGION_COLOR;
    if (row.provisional)
        return WB_PROVISIONAL_REGION_COLOR;
    return wbRegionColorForTrack(wbString(row.track_id));
}
function wbRegionContent(row) {
    const styleClass = wbRegionStyleClass(row);
    const tags = [];
    if (row.role === 'unknown')
        tags.push('角色待确认');
    if (row.uncertain)
        tags.push('不确定');
    if (row.provisional)
        tags.push('provisional');
    const label = wbTruncate(wbString(row.label) || wbString(row.kind) || row.region_id, 32);
    return '<span class="wb-region-tag wb-region-tag-' + styleClass + '">'
        + wbEsc(label) + (tags.length ? ' · ' + wbEsc(tags.join(' / ')) : '')
        + '</span>';
}
function wbRenderTracks(document) {
    const node = wbNode('wb-tracks');
    if (!node)
        return;
    const tracks = Array.isArray(document.tracks) ? document.tracks : [];
    if (!tracks.length) {
        node.innerHTML = '<h3>证据轨道</h3><p class="quiet">后端未提供 track 列表。</p>';
        return;
    }
    node.innerHTML = '<h3>证据轨道（后端原文）</h3><div class="wb-legend">'
        + tracks.map(track => {
            const id = wbString(track.track_id);
            return '<span class="wb-legend-item"><i class="wb-swatch" style="background:'
                + wbEsc(wbRegionColorForTrack(id)) + '"></i>'
                + wbEsc(wbValueText(track.label)) + ' <small>' + wbEsc(id) + ' · '
                + wbEsc(wbValueText(track.evidence_class)) + '</small></span>';
        }).join('')
        + '</div>'
        + '<p class="quiet">轨道来自后端；浏览器不新增轨道、不改坐标。角色为 unknown / uncertain / provisional 的区间'
        + '使用独立配色并标注，绝不显示为测试者或设备。</p>';
}
function wbLinkedRows(regionId) {
    const document = wbDocument;
    if (!document)
        return [];
    const rows = [];
    wbTurnRows(document).forEach(row => {
        if (wbStringOrNull(row.region_id) === regionId) {
            rows.push(['对话轮次', wbButton(wbValueText(row.turn_id), regionId),
                wbValueText(row.start_ms) + ' – ' + wbValueText(row.end_ms) + ' ms']);
        }
    });
    wbEventRows(document).forEach(row => {
        if (wbStringOrNull(row.region_id) === regionId) {
            rows.push(['事件', wbButton(wbValueText(row.event_id), regionId),
                wbValueText(row.type) + ' · ' + wbValueText(row.start_ms) + ' – ' + wbValueText(row.end_ms) + ' ms']);
        }
    });
    wbMetricRows(document).forEach(row => {
        if (wbStringOrNull(row.region_id) === regionId) {
            rows.push(['指标', wbButton(wbValueText(row.metric_id), regionId),
                wbValueText(row.name) + ' = ' + wbValueText(row.value) + ' ' + wbValueText(row.unit)]);
        }
    });
    wbFindingRows(document).forEach(row => {
        if (wbStringOrNull(row.region_id) === regionId) {
            rows.push(['Finding', wbButton(wbValueText(row.finding_id), regionId),
                wbValueText(row.title) + ' · ' + wbValueText(row.severity)]);
        }
    });
    wbTranscriptRows(document).forEach(row => {
        if (wbTranscriptRegionId(document, row) === regionId) {
            // The backend decides which relation holds between the served region and the
            // utterance; the panel repeats exactly that one and never upgrades a partial
            // overlap into containment.
            const relation = wbRegionRelationLabel(row);
            rows.push(['转写', wbButton(wbValueText(row.segment_id), regionId),
                wbValueText(row.text) + relation]);
        }
    });
    return rows;
}
/**
 * The served relation between a transcript segment and its region, as a label.
 *
 * The backend distinguishes equality, containment, partial overlap and an unknown
 * span, so the panel must not collapse them: asserting "the utterance is a sub-interval"
 * for a region that is actually *narrower* than the utterance states the opposite of
 * what the evidence says.
 */
function wbRegionRelationLabel(row) {
    switch (row.region_basis) {
        case 'fused_acoustic_segment_container':
            return ' · 容器区间（该话语为其子区间，非等值）';
        case 'fused_acoustic_segment_partial_overlap':
            return ' · 区间与话语部分重叠（非包含）';
        case 'fused_acoustic_segment_span_unknown':
            return ' · 区间关系未知（缺少可解析区间）';
        default:
            return '';
    }
}
/** The synchronized evidence panel: every value is a served field, verbatim. */
function wbRenderEvidence(row) {
    const node = wbNode('wb-evidence');
    if (!node)
        return;
    if (!row) {
        node.innerHTML = '<h3>证据详情</h3><p class="quiet">尚未选择区间。点击下方任一记录或波形区间，'
            + '这里显示后端持久化的证据字段（角色、角色依据、置信度、不确定性、状态与来源）。</p>';
        return;
    }
    const document = wbDocument;
    const raw = document && Array.isArray(document.regions)
        ? document.regions.filter(candidate => wbString(candidate.region_id) === row.region_id)[0] || null
        : null;
    const source = row.source;
    const facts = [
        ['region_id', row.region_id],
        ['track_id', wbValueText(row.track_id)],
        ['kind', wbValueText(row.kind)],
        ['label', wbValueText(row.label)],
        ['start（后端 start_sec）', wbValueText(row.start) + ' s'],
        ['end（后端 end_sec）', wbValueText(row.end) + ' s'],
        ['角色', wbRoleLabel(row.role)],
        ['角色依据 role_basis', wbValueText(row.role_basis)],
        ['证据类别 evidence_class', wbValueText(row.evidence_class)],
        ['状态 status', wbValueText(row.status)],
        ['置信度 confidence', wbValueText(row.confidence)],
        ['不确定性 uncertainty', wbValueText(row.uncertainty)],
        ['uncertain', String(row.uncertain)],
        ['provisional', String(row.provisional)],
        ['envelope_of（后端声明）', wbValueText(row.envelope_of)],
    ];
    const sourcePairs = source ? [
        ['source.document', wbValueText(source.document)],
        ['source.document_id', wbValueText(source.document_id)],
        ['source.processor.name', wbValueText(source.processor ? source.processor.name : null)],
        ['source.processor.version', wbValueText(source.processor ? source.processor.version : null)],
        ['source.artifact_ref', wbValueText(source.artifact_ref)],
        ['source.evidence_ids', wbValueText(source.evidence_ids)],
        ['source.event_ids', wbValueText(source.event_ids)],
        ['source.metric_ids', wbValueText(source.metric_ids)],
        ['source.turn_ids', wbValueText(source.turn_ids)],
    ] : [];
    const linked = wbLinkedRows(row.region_id);
    node.innerHTML = '<h3>证据详情</h3>'
        + wbKeyValueTable(facts)
        + (sourcePairs.length ? '<h4>来源（后端原文）</h4>' + wbKeyValueTable(sourcePairs) : '')
        + (raw && raw.detail ? wbDetailTable('持久化 detail', raw.detail) : '')
        + (linked.length ? '<h4>引用该区间的后端记录</h4>' + wbTable(['类型', '标识', '摘要'], linked) : '')
        + '<p class="quiet">以上字段全部来自后端文档；浏览器只读取与展示，不计算指标、轮次、角色或边界。</p>';
    wbWireRowButtons(node);
}
function wbRenderTables(document) {
    wbSetPanel('wb-findings', 'Findings', ['Finding', '严重度', '状态', '置信度', '来源解析', '说明'], wbFindingRows(document).map(row => [
        wbButton(wbValueText(row.title || row.finding_id), wbStringOrNull(row.region_id)),
        wbValueText(row.severity),
        wbValueText(row.status) + (row.provisional ? ' · provisional' : ''),
        wbValueText(row.confidence),
        wbValueText(row.span_origin),
        wbValueText(row.description),
    ]));
    wbSetPanel('wb-metrics', '指标证据（值按后端原样显示，缺失为 N/A）', ['指标', '值', '单位', '状态', '来源解析', '原因'], wbMetricRows(document).map(row => [
        wbButton(wbValueText(row.name || row.metric_id), wbStringOrNull(row.region_id)),
        wbValueText(row.value),
        wbValueText(row.unit),
        wbValueText(row.status) + (row.provisional ? ' · provisional' : ''),
        wbValueText(row.span_origin),
        wbValueText(row.reason),
    ]));
    wbSetPanel('wb-events', '事件时间线', ['事件', '类型', 'start_ms', 'end_ms', '来源', '置信度'], wbEventRows(document).map(row => [
        wbButton(wbValueText(row.event_id), wbStringOrNull(row.region_id)),
        wbValueText(row.type),
        wbValueText(row.start_ms),
        wbValueText(row.end_ms),
        wbValueText(row.source),
        wbValueText(row.confidence),
    ]));
    wbSetPanel('wb-turns', '对话轮次', ['轮次', 'start_ms', 'end_ms', '打断', '重叠'], wbTurnRows(document).map(row => [
        wbButton(wbValueText(row.turn_id), wbStringOrNull(row.region_id)),
        wbValueText(row.start_ms),
        wbValueText(row.end_ms),
        wbFlag(row.has_interruption),
        wbFlag(row.has_overlap),
    ]));
    wbSetPanel('wb-transcript', '转写（时间戳为 ASR 估计）', ['片段', 'start_ms', 'end_ms', '说话人', '角色（后端）', '区间关联', '文本'], wbTranscriptRows(document).map(row => [
        wbButton(wbValueText(row.segment_id), document ? wbTranscriptRegionId(document, row) : null),
        wbValueText(row.start_ms),
        wbValueText(row.end_ms),
        wbValueText(row.speaker_id),
        wbValueText(row.speaker_role),
        // Served verbatim: a container region is never shown as an exact match.
        wbValueText(row.region_basis),
        wbValueText(row.text),
    ]));
}
function wbRenderRevision(document) {
    const node = wbNode('wb-revision');
    if (!node)
        return;
    const revision = document.revision || null;
    if (!revision) {
        node.innerHTML = '<h3>人工修订</h3><p class="quiet">本 Run 尚无已保存的人工角色修订（revision = null）。</p>';
        return;
    }
    const pairs = [
        ['analysis_id', wbValueText(revision.analysis_id)],
        ['revision_index', wbValueText(revision.revision_index)],
        ['revision_id', wbValueText(revision.revision_id)],
        ['复核人 reviewer', wbValueText(revision.reviewer)],
        ['说明 reason', wbValueText(revision.reason)],
        ['创建时间 created_at', wbValueText(revision.created_at)],
        ['mapping_sha256', wbValueText(revision.mapping_sha256)],
        ['上一修订', wbValueText(revision.previous_revision_ref ?? revision.previous_revision_id)],
    ];
    const decisions = revision.decisions || null;
    const decisionRows = decisions
        ? Object.keys(decisions).sort().map(key => [wbValueText(key), wbValueText(decisions[key])])
        : [];
    // The backend publishes the revision history either beside the revision or at the
    // document top level; both are accepted, and neither is reconstructed locally.
    const history = (Array.isArray(revision.history) && revision.history.length
        ? revision.history
        : (Array.isArray(document.revision_history) ? document.revision_history : []));
    node.innerHTML = '<h3>人工修订</h3>'
        + wbKeyValueTable(pairs)
        + '<h4>角色决定 decisions</h4>'
        + (decisionRows.length ? wbTable(['聚类', '角色'], decisionRows) : '<p class="quiet">本次修订没有记录角色决定。</p>')
        + '<h4>修订历史</h4>'
        + (history.length
            ? wbTable(['revision_id', '#', '复核人', '创建时间'], history.map(entry => [
                wbValueText(entry.revision_id), wbValueText(entry.revision_index),
                wbValueText(entry.reviewer), wbValueText(entry.created_at),
            ]))
            : '<p class="quiet">后端未提供修订历史。</p>');
}
function wbRenderProvenance(document) {
    const node = wbNode('wb-provenance');
    if (!node)
        return;
    const provenance = document.provenance || null;
    if (!provenance) {
        node.innerHTML = '<h3>Provenance</h3><p class="quiet">后端未提供 provenance。</p>';
        return;
    }
    const artifacts = Array.isArray(provenance.artifacts) ? provenance.artifacts : [];
    const invocations = Array.isArray(provenance.provider_invocations) ? provenance.provider_invocations : [];
    const policies = Array.isArray(provenance.policies) ? provenance.policies : [];
    node.innerHTML = '<h3>Provenance</h3>'
        + wbKeyValueTable([
            ['run_id', wbValueText(provenance.run_id)],
            ['analysis_id', wbValueText(provenance.analysis_id)],
        ])
        + '<h4>Artifacts</h4>'
        + (artifacts.length
            ? wbTable(['artifact_id', 'kind', 'sha256', 'processor'], artifacts.map(item => [
                wbValueText(item.artifact_id), wbValueText(item.kind),
                wbValueText(item.sha256), wbValueText(item.processor),
            ]))
            : '<p class="quiet">后端未提供 artifact 列表。</p>')
        + wbDetailTable('模型配置快照（不含密钥）', provenance.model_configuration)
        + '<h4>Provider 调用（非密钥字段）</h4>'
        + (invocations.length
            ? wbTable(['#', '记录'], invocations.map((item, index) => [String(index), wbValueText(item)]))
            : '<p class="quiet">本次修订没有 provider 调用记录。</p>')
        + '<h4>Policies</h4>'
        + (policies.length
            ? wbTable(['#', '记录'], policies.map((item, index) => [String(index), wbValueText(item)]))
            : '<p class="quiet">后端未提供 policy 记录。</p>');
}
function wbRenderAbstentions(document) {
    const node = wbNode('wb-abstentions');
    if (!node)
        return;
    const abstentions = document.abstentions || null;
    const gap = abstentions ? abstentions.metrics_gap || null : null;
    const reasons = gap && Array.isArray(gap.reasons) ? gap.reasons : [];
    node.innerHTML = '<h3>指标可用性与弃权</h3>'
        + wbKeyValueTable([['metrics_gap.status', wbValueText(gap ? gap.status : null)]])
        + (reasons.length
            ? wbTable(['原因代码', '涉及片段数', '说明'], reasons.map(reason => [
                wbValueText(reason.code), wbValueText(reason.count), wbValueText(reason.detail),
            ]))
            : '<p class="quiet">后端未报告指标缺口原因。</p>')
        + '<p>以上原因由后端记录，浏览器不解释、不补算，也不用 0 或成功占位符填充空指标。</p>';
}
function wbRenderUnavailableDocument(message) {
    const root = wbNode('workbench');
    if (root)
        root.hidden = false;
    const gate = wbNode('wb-gate');
    if (gate) {
        gate.className = 'wb-gate wb-gate-missing';
        gate.innerHTML = '<strong>证据工作台不可用</strong><p>' + wbEsc(message) + '</p>'
            + '<p>浏览器不会在本地合成区间、指标或角色；请先在后端完成该 Run 的证据分析，或使用「刷新证据」重试。</p>';
    }
    wbSetStatus('未加载证据文档：' + message);
}
function wbRenderAll(document) {
    const root = wbNode('workbench');
    if (root)
        root.hidden = false;
    wbRenderGate(document);
    wbRenderUnavailable(document);
    wbRenderTracks(document);
    wbRenderTables(document);
    wbRenderRevision(document);
    wbRenderProvenance(document);
    wbRenderAbstentions(document);
    wbRenderEvidence(null);
    const gate = document.gate || null;
    wbSetStatus('文档 ' + wbValueText(document.document_id) + ' · schema ' + wbValueText(document.schema_version)
        + ' · analysis ' + wbValueText(document.analysis_id)
        + ' · 区间 ' + wbRegionsFromDocument(document).length + ' 条（后端返回，未在浏览器重算）', '视图 view_kind=' + wbValueText(gate ? gate.view_kind : null)
        + ' · 角色依赖证据' + (gate && gate.role_dependent_available === true ? '可用' : '不可用'));
}
/* ------------------------------------------------------------------ */
/* Player                                                             */
/* ------------------------------------------------------------------ */
function wbPlayerState() {
    if (!wbPlayer)
        return null;
    let duration = 0;
    try {
        duration = wbPlayer.getDuration();
    }
    catch {
        duration = 0;
    }
    return {
        ready: wbPlayerReady,
        playing: wbPlayerPlaying,
        currentTime: wbPlayerTime,
        duration: Number.isFinite(duration) ? duration : 0,
        regions: wbPlugins.regions ? wbPlugins.regions.getRegions().length : 0,
        timeline: wbPlugins.timeline !== null,
        minimap: wbPlugins.minimap !== null,
        selected: wbSelectedRegionId,
    };
}
function wbDestroyPlayer() {
    const player = wbPlayer;
    wbPlayer = null;
    wbPlayerReady = false;
    wbPlayerPlaying = false;
    wbPlayerTime = 0;
    wbPlugins.regions = null;
    wbPlugins.timeline = null;
    wbPlugins.minimap = null;
    if (!player)
        return;
    try {
        player.destroy();
    }
    catch { /* the container is already gone */ }
    ['wb-waveform', 'wb-timeline', 'wb-minimap'].forEach(id => {
        const node = wbNode(id);
        if (node)
            node.innerHTML = '';
    });
    const minimap = wbNode('wb-minimap');
    if (minimap)
        minimap.hidden = true;
}
function wbHighlightRegion(regionId) {
    const regions = wbPlugins.regions;
    if (regions) {
        regions.getRegions().forEach(region => {
            const element = region.element;
            if (!element || !element.classList)
                return;
            element.classList.toggle('wb-region-selected', wbStringOrNull(region.id) === regionId);
        });
    }
    document.querySelectorAll('[data-wb-region]').forEach(button => {
        button.classList.toggle('wb-selected', (button.dataset.wbRegion || null) === regionId);
    });
}
/**
 * Seek to the region's persisted `start` and synchronize the panels.
 *
 * Returns the selected row, or `null` for an id that is not in the loaded document —
 * a region is never synthesised for an unknown id.
 */
function wbSelectEvidence(regionId) {
    const wanted = wbString(regionId);
    const document = wbDocument;
    if (!wanted || !document)
        return null;
    const row = wbRegionsFromDocument(document).filter(candidate => candidate.region_id === wanted)[0] || null;
    if (!row)
        return null;
    wbSelectedRegionId = wanted;
    if (wbPlayer && typeof row.start === 'number') {
        try {
            wbPlayer.setTime(row.start);
        }
        catch { /* not seekable yet */ }
        // Zoom is presentation-only: it never changes a persisted coordinate. The target
        // width is a UI choice, and the region span is the backend's own difference.
        if (typeof row.end === 'number' && row.end > row.start) {
            const span = row.end - row.start;
            if (span > 0) {
                try {
                    wbPlayer.zoom(Math.max(1, Math.min(2000, 600 / span)));
                }
                catch { /* zoom unavailable */ }
            }
        }
    }
    wbHighlightRegion(wanted);
    wbRenderEvidence(row);
    wbRenderPlayerState();
    return row;
}
function wbCreatePlayer(document) {
    const waveSurfer = wbWaveSurferGlobal();
    if (!waveSurfer) {
        wbAppendStatus('wavesurfer.js 未加载：波形与区间交互不可用。坐标、来源与表格仍然可用，浏览器不会本地绘制证据。');
        return;
    }
    const container = wbNode('wb-waveform');
    const audio = document.audio || null;
    const url = audio ? wbString(audio.url) : '';
    if (!container || !url) {
        wbAppendStatus('后端未提供可播放的音频 artifact URL：未创建波形。');
        return;
    }
    wbDestroyPlayer();
    const player = waveSurfer.create({
        container,
        url,
        height: WB_WAVEFORM_HEIGHT,
        waveColor: '#9dc3b2',
        progressColor: '#22745c',
        normalize: true,
    });
    wbPlayer = player;
    const regions = waveSurfer.Regions;
    if (regions) {
        const plugin = player.registerPlugin(regions.create());
        wbPlugins.regions = plugin;
        wbAddRegions(plugin, document);
        plugin.on('region-clicked', region => { wbSelectEvidence(wbStringOrNull(region.id)); });
    }
    else {
        wbAppendStatus('regions 插件未加载：区间只以表格形式列出。');
    }
    const timeline = waveSurfer.Timeline;
    if (timeline) {
        wbPlugins.timeline = player.registerPlugin(timeline.create({ height: 22 }));
    }
    else {
        wbAppendStatus('timeline 插件未加载：未绘制时间轴。');
    }
    const durationMs = audio ? wbNumberOrNull(audio.duration_ms) : null;
    if (durationMs !== null && durationMs >= WB_MINIMAP_MIN_DURATION_MS) {
        // Long recordings only. The plugin is vendored alongside the core; when it is
        // absent the workbench says so instead of substituting a local implementation.
        if (waveSurfer.Minimap) {
            wbPlugins.minimap = player.registerPlugin(waveSurfer.Minimap.create({ height: 40 }));
            const node = wbNode('wb-minimap');
            if (node)
                node.hidden = false;
        }
        else {
            wbAppendStatus('录音时长 ' + String(durationMs) + ' ms ≥ 5 分钟，但 Minimap 插件未加载：未启用长录音导航。');
        }
    }
    player.on('ready', () => { wbPlayerReady = true; wbRenderPlayerState(); });
    player.on('play', () => { wbPlayerPlaying = true; wbRenderPlayerState(); });
    player.on('pause', () => { wbPlayerPlaying = false; wbRenderPlayerState(); });
    player.on('finish', () => { wbPlayerPlaying = false; wbRenderPlayerState(); });
    player.on('timeupdate', value => {
        const seconds = wbNumberOrNull(value);
        wbPlayerTime = seconds === null ? 0 : seconds;
        wbRenderPlayerState();
    });
    player.on('error', error => { wbAppendStatus('音频加载失败：' + wbMessageOf(error)); });
    wbRenderPlayerState();
}
/** One Region per backend region. Never a local heuristic, never a re-derived span. */
function wbAddRegions(plugin, document) {
    const rows = wbRegionsFromDocument(document);
    let skipped = 0;
    rows.forEach(row => {
        if (typeof row.start !== 'number' || typeof row.end !== 'number') {
            skipped += 1;
            return;
        }
        plugin.addRegion({
            id: row.region_id,
            start: row.start,
            end: row.end,
            content: wbRegionContent(row),
            color: wbRegionColor(row),
            drag: false,
            resize: false,
        });
    });
    if (skipped) {
        wbAppendStatus(skipped + ' 个区间缺少后端坐标，未在波形上绘制（坐标不会被本地推导）。');
    }
}
/* ------------------------------------------------------------------ */
/* Mount / unmount                                                     */
/* ------------------------------------------------------------------ */
async function wbFetchWorkbench(runId) {
    if (!runId)
        throw new Error('缺少 run_id，无法读取证据工作台文档。');
    const response = await fetch('/api/runs/' + encodeURIComponent(runId) + '/evidence-workbench');
    const payload = await response.json();
    if (!response.ok) {
        const detail = payload.detail;
        throw new Error(typeof detail === 'string' && detail
            ? detail
            : '读取证据工作台失败（HTTP ' + String(response.status) + '）');
    }
    return payload;
}
/**
 * Render the workbench for one analysis document.
 *
 * The workbench member of `GET /api/runs/{run_id}` is used when the backend served
 * it; otherwise the dedicated endpoint is read. Both are the backend's document —
 * nothing is recomputed locally, and a Run with no readable evidence is reported as
 * unavailable instead of being filled with invented regions.
 */
async function wbMount(analysisDocument) {
    wbUnmount();
    const token = wbMountToken;
    const runId = wbString(analysisDocument ? analysisDocument.run_id : null);
    const root = wbNode('workbench');
    if (root)
        root.hidden = false;
    wbClearPanels();
    wbSetStatus('正在读取后端证据工作台文档…');
    const hosted = analysisDocument && analysisDocument.workbench ? analysisDocument.workbench : null;
    let loaded;
    try {
        loaded = hosted || await wbFetchWorkbench(runId);
    }
    catch (error) {
        if (wbMountToken !== token)
            return;
        wbRunId = runId;
        wbDocument = null;
        wbLoaded = false;
        wbError = wbMessageOf(error);
        wbStateValue = wbBuildState();
        wbRenderUnavailableDocument(wbError);
        return;
    }
    if (wbMountToken !== token)
        return;
    try {
        wbLoad(runId, loaded);
    }
    catch (error) {
        if (wbMountToken !== token)
            return;
        wbRenderUnavailableDocument(wbMessageOf(error));
        return;
    }
    const document = wbDocument;
    if (!document)
        return;
    wbRenderAll(document);
    try {
        await wbLoadVendor();
    }
    catch (error) {
        if (wbMountToken !== token)
            return;
        wbAppendStatus('wavesurfer.js 未加载：' + wbMessageOf(error) + '（表格与坐标仍然可用，浏览器不会本地绘制波形真值）。');
        return;
    }
    if (wbMountToken !== token)
        return;
    wbCreatePlayer(document);
}
/** Tear the renderer down and forget the loaded document. Safe to call at any time. */
function wbUnmount() {
    wbMountToken += 1;
    wbDestroyPlayer();
    wbDocument = null;
    wbLoaded = false;
    wbError = null;
    wbStateValue = null;
    wbSelectedRegionId = null;
    wbStatusLines = [];
    wbClearPanels();
    const root = wbNode('workbench');
    if (root)
        root.hidden = true;
}
/** Re-read the backend document for the current Run. Never a local recomputation. */
async function wbRefresh() {
    const runId = wbRunId;
    const button = wbNode('wb-refresh');
    if (!runId) {
        wbSetStatus('尚未加载任何 Run，无法刷新证据。');
        return;
    }
    if (button)
        button.disabled = true;
    wbSetStatus('正在从后端重新读取证据文档（GET /api/runs/{run_id}/evidence-workbench）…');
    try {
        const document = await wbFetchWorkbench(runId);
        await wbMount({ run_id: runId, workbench: document });
    }
    catch (error) {
        wbAppendStatus('刷新失败：' + wbMessageOf(error));
    }
    finally {
        const again = wbNode('wb-refresh');
        if (again)
            again.disabled = false;
    }
}
function wbBindRefresh() {
    const button = wbNode('wb-refresh');
    if (button)
        button.onclick = () => { void wbRefresh(); };
}
const WB = {
    load: wbLoad,
    regionsFromDocument: wbRegionsFromDocument,
    metricRows: wbMetricRows,
    findingRows: wbFindingRows,
    eventRows: wbEventRows,
    turnRows: wbTurnRows,
    transcriptRows: wbTranscriptRows,
    state: () => wbStateValue,
    selectEvidence: wbSelectEvidence,
    mount: wbMount,
    unmount: wbUnmount,
    playerState: wbPlayerState,
    refresh: wbRefresh,
};
if (typeof window !== 'undefined' && window) {
    window.WB = WB;
}
else {
    globalThis.WB = WB;
}
wbBindRefresh();
