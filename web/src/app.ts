/* AIVoiceBench — analysis workspace view.
 *
 * This file is the TypeScript source of `aivoicebench/static/app.js` (compiled by
 * `npm run build`). It is a classic script rather than a module on purpose: the
 * page loads it with `<script src="/static/app.js">`, and the server-rendered
 * `index.html` calls these functions through inline `onclick` attributes, so
 * every top-level declaration here stays on the page's global script scope, as
 * it did before the migration.
 *
 * It provides the shared view helpers (`$`, `esc`, `badge`, `notify`, `request`)
 * that `models.ts` and `voice_test.ts` also use.
 */

/** Element lookup. Throws when the page markup no longer provides the id. */
const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;

/** Minimal HTML text escaping for the interpolated view templates. */
const esc = (value: unknown): string => String(value ?? '')
  .replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[character] as string));

/** Status pill for a pipeline/analysis status code (`statusLabel` in models.ts). */
const badge = (status: string): string =>
  `<span class="badge ${['partial', 'complete', 'failed', 'observed', 'insufficient_evidence'].includes(status) ? status : ''}">${esc(statusLabel(status) || '待确认')}</span>`;

/** Analysis document currently on screen. */
let current: AnalysisDocument = null as unknown as AnalysisDocument;
/** File selected for import. */
let selected: File | null = null;
/** One import in flight at a time. */
let busy = false;

function notify(message = ''): void {
  const notice = $('notice') as HTMLElement;
  notice.hidden = !message;
  notice.textContent = message;
}

/**
 * JSON request helper.
 *
 * A non-2xx response becomes an `Error` carrying the server's `detail` when it is
 * a string, so callers only ever have to show one message.
 */
async function request<T = ApiDocument>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const data = await response.json() as ApiDocument;
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入或稍后重试');
  }
  return data as unknown as T;
}

/** Server error envelope. */
interface ApiDocument {
  detail?: unknown;
  [key: string]: unknown;
}

/** Top-level views the shell can show. */
type ViewName = 'home' | 'import' | 'voice-test' | 'analysis' | 'models';

const VIEW_TITLES: Record<ViewName, string> = {
  home: '分析记录',
  import: '导入录音',
  'voice-test': '语音对话测试',
  analysis: '录音分析',
  models: '模型管理',
};

const NAV_VIEWS: readonly ViewName[] = ['home', 'import', 'voice-test', 'models'];

function view(name: ViewName): void {
  (['home', 'import', 'voice-test', 'analysis', 'models'] as ViewName[])
    .forEach(candidate => { $(candidate).hidden = candidate !== name; });
  $('breadcrumb').textContent = VIEW_TITLES[name];
  NAV_VIEWS.forEach(candidate => {
    const button = $('nav-' + candidate);
    if (button) button.classList.toggle('active', name === candidate);
  });
  notify();
}

/** One Run in the analysis-records list. */
interface RunSummary {
  run_id: string;
  device?: string | null;
  created: number;
  status: string;
}

/** `GET /api/runs` */
interface RunList {
  runs: RunSummary[];
}

async function home(): Promise<void> {
  view('home');
  $('runs').innerHTML = '<div class="empty" role="status">正在读取分析记录…</div>';
  try {
    const data = await request<RunList>('/api/runs');
    $('count').textContent = `所有记录 · ${data.runs.length}`;
    $('runs').innerHTML = data.runs.length
      ? data.runs.map(run => `<button class="run" data-run="${esc(run.run_id)}"><span class="run-icon">≋</span><span class="run-text"><strong>${esc(run.device || '未命名设备')}</strong><small>${esc(new Date(run.created * 1000).toLocaleString('zh-CN'))} · ${esc(run.run_id)}</small></span>${badge(run.status)}<span class="quiet">→</span></button>`).join('')
      : '<div class="panel empty"><strong>你的第一份分析，从这里开始</strong>导入一段对话录音，建立可追溯的评测记录。</div>';
    $('runs').querySelectorAll<HTMLElement>('[data-run]').forEach(button => {
      button.onclick = () => openRun(button.dataset.run as string);
    });
  } catch (error) {
    $('runs').innerHTML = '';
    notify((error as Error).message);
  }
}

function importView(): void {
  view('import');
}

function voiceTestView(): void {
  view('voice-test');
  vtTab('fixed');
}

function vtTab(name: string): void {
  document.querySelectorAll<HTMLElement>('[data-vt-tab]').forEach(button => {
    button.setAttribute('aria-selected', String(button.dataset.vtTab === name));
  });
  $('vt-fixed').hidden = name !== 'fixed';
  $('vt-free').hidden = name !== 'free';
}

/** Accepted recording formats for import. */
const IMPORT_EXTENSIONS = /\.(wav|mp3|m4a)$/i;

function choose(file: File | null | undefined): void {
  if (!file) return;
  if (!IMPORT_EXTENSIONS.test(file.name)) {
    notify('请选择 WAV、MP3 或 M4A 文件');
    return;
  }
  selected = file;
  $('file-label').textContent = file.name;
  $('file-size').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB · 已选择，准备分析`;
  notify();
}

($('file') as HTMLInputElement).onchange = event => choose((event.target as HTMLInputElement).files?.[0]);
$('drop').ondragover = event => { event.preventDefault(); $('drop').classList.add('drag'); };
$('drop').ondragleave = () => $('drop').classList.remove('drag');
$('drop').ondrop = event => {
  event.preventDefault();
  $('drop').classList.remove('drag');
  choose((event as DragEvent).dataTransfer?.files?.[0]);
  ($('file') as HTMLInputElement).required = false;
};

/** Optional device-information fields collected with an import. */
const fields: Record<string, string> = {
  device: '设备名称',
  hardware: '硬件版本',
  firmware: '固件版本',
  model: 'AI 模型',
  prompt: '提示词版本',
  supplier: '供应商',
  environment: '测试环境',
  notes: '备注',
};

$('fields').innerHTML = Object.entries(fields)
  .map(([key, label]) => `<label>${label}<input name="${key}" maxlength="4000" placeholder="填写${label}"></label>`)
  .join('');

($('upload-form') as HTMLFormElement).onsubmit = async event => {
  event.preventDefault();
  if (!selected || busy) return;
  busy = true;
  ($('submit') as HTMLButtonElement).disabled = true;
  $('submit').textContent = '正在分析…';
  $('progress').textContent = '正在保存、转换与分析录音，请保持页面打开。';
  notify();
  const form = new FormData($('upload-form') as HTMLFormElement);
  form.set('file', selected);
  try {
    const data = await request<AnalysisDocument>('/api/analyze', { method: 'POST', body: form });
    render(data);
  } catch (error) {
    notify((error as Error).message);
  } finally {
    busy = false;
    ($('submit') as HTMLButtonElement).disabled = false;
    $('submit').textContent = '开始分析 →';
    $('progress').textContent = '录音将保存在当前服务的数据目录中。';
  }
};

async function openRun(id: string): Promise<void> {
  try {
    render(await request<AnalysisDocument>('/api/runs/' + encodeURIComponent(id)));
  } catch (error) {
    notify((error as Error).message);
  }
}

/** One acoustic or fused speech segment of a Run. */
interface SpeechSegment {
  start_ms: number;
  end_ms: number;
  text?: string | null;
  speaker_id?: string | null;
  speaker_role?: string | null;
  text_attribution?: string | null;
  role_attribution?: { needs_review?: boolean } | null;
}

/** Anonymous speaker cluster produced by the diarization provider. */
interface SpeakerCluster {
  speaker_id: string;
  native_speaker_id?: string | null;
}

/** Recorded role attribution for one speaker cluster. */
interface RoleAttribution {
  speaker_id: string;
  role: string;
  method?: string;
  confidence_basis?: string;
  needs_review?: boolean;
  reason?: string;
}

/** One MetricResult as rendered by the metrics tab. */
interface MetricResultView {
  name: string;
  value?: number | string | null;
  unit?: string | null;
  status: string;
}

/** One Finding as rendered by the findings tab. */
interface FindingView {
  title?: string;
  dimension?: string;
  description?: string;
  reason?: string;
  status: string;
}

/** One Judge/LLM dimension result. */
interface JudgeResultView {
  dimension: string;
  decision: string;
  reason: string;
  status: string;
}

/** Per-stage status/reason ledger entry. */
interface StageLedgerEntry {
  status?: string;
  reason?: string;
}

/**
 * `GET /api/runs/{run_id}` — the analysis document rendered by every tab.
 *
 * Fields stay optional because the page must keep working for a Run written by an
 * older revision of the pipeline (the partial document is still evidence).
 */
interface AnalysisDocument {
  run_id: string;
  status: string;
  reason?: string | null;
  audio_url?: string | null;
  report_md?: string | null;
  profile?: { device?: string | null } | null;
  transcript?: { segments?: SpeechSegment[] } | null;
  fused_segments: SpeechSegment[];
  acoustic_segments: SpeechSegment[];
  speaker_segments?: SpeakerCluster[];
  diarization_scope?: { invocation_id?: string } | null;
  turns: unknown[];
  attribution?: { attributions?: RoleAttribution[]; conflicts?: { speaker_id: string }[] } | null;
  alignment?: {
    status?: string;
    reason?: string;
    document_id?: string;
    policy?: { policy_version?: string; overlap_basis?: string };
    diagnostics?: {
      acoustic_segment_count?: number;
      unmatched_acoustic_ms?: number | null;
      unmatched_acoustic_ratio?: number | null;
      unmatched_speaker_ms?: number | null;
      unmatched_speaker_ratio?: number | null;
      conflicted_acoustic_segment_ids?: string[];
      low_energy?: { segment_count?: number; unmatched_ms?: number | null };
      per_cluster?: { speaker_id: string; coverage_ratio?: number | null }[];
    };
  } | null;
  metrics_gap?: { status?: string; reasons?: { code: string; count: number; detail: string }[] } | null;
  audio_qa?: {
    measurements?: {
      duration_ms?: number;
      encoding?: string;
      channels?: number;
      sample_rate_hz?: number;
      conditions?: { condition_id: string; status: string; basis?: string; limitation?: string }[];
    } | null;
  } | null;
  role_review?: {
    status?: string;
    reason?: string;
    revision?: { revision_index: number; reviewer: string; reason?: string; analysis_id?: string } | null;
    clusters?: {
      speaker_id: string;
      native_speaker_id?: string | null;
      segment_count: number;
      speech_ms: number;
      decision?: string | null;
      representative_intervals?: { start_ms: number }[];
      transcript_snippets?: { text: string }[];
    }[];
    awaiting_decision_for?: string[];
    diff?: { changed?: { speaker_id: string; from: string; to: string }[] };
  } | null;
  stages?: Record<string, StageLedgerEntry>;
  metrics: MetricResultView[];
  findings: FindingView[];
  judge_results: JudgeResultView[];
  analysis_id?: string;
}

function render(data: AnalysisDocument): void {
  current = data;
  view('analysis');
  $('analysis-title').textContent = data.profile?.device || '录音分析';
  $('run-id').textContent = data.run_id;
  $('status').innerHTML = badge(data.status);
  ($('audio') as HTMLAudioElement).src = data.audio_url || '/api/runs/' + encodeURIComponent(data.run_id) + '/audio';
  const segmentCount = data.transcript?.segments?.length || data.fused_segments.length || data.acoustic_segments.length;
  $('summary').innerHTML = [
    ['语音片段', segmentCount],
    ['说话人聚类', (data.speaker_segments || []).length],
    ['对话轮次', data.turns.length],
    ['已观测指标', data.metrics.filter(metric => metric.status === 'observed' && metric.value != null).length],
    ['待复核发现', data.findings.length],
  ].map(([label, count]) => `<div class="stat">${label}<b>${count}</b></div>`).join('');
  tab('segments');
  if (data.reason) notify(data.reason);
}

/** Canonical metric name → display label. */
const metricNames: Record<string, string> = {
  first_speech_latency_ms: '首次语音时延',
  feedback_latency_ms: '首次反馈时延',
  meaningful_response_latency_ms: '有效回答时延',
  turn_gap_ms: '轮次间隔',
  overlap_duration_ms: '重叠时长',
  overlap_ratio: '重叠比例',
  barge_in_stop_latency_ms: '打断停止时延',
  barge_in_success: '打断成功',
  false_endpoint_candidate: '错误端点（候选）',
  false_endpoint: '错误端点',
};

/** Speaker role → display label. `unknown` stays a deliberate state. */
const roleNames: Record<string, string> = {
  tester: '测试者',
  device: 'AI 设备',
  unknown: '角色待确认',
};

/** How a role attribution was produced. Machine proposals are labelled as such. */
const roleMethods: Record<string, string> = {
  explicit_evidence: '用户人工指定',
  semantic_attribution: '历史机器提议（不用于新分析）',
  human_attribution: '人工复核',
  none: '等待人工确认',
};

function roleEvidence(attribution: RoleAttribution): string {
  // A model's own confidence is not a calibrated accuracy; never show it as one.
  const bits = [roleMethods[attribution.method || ''] || attribution.method || '无依据'];
  if (attribution.confidence_basis === 'uncalibrated_model_self_report') bits.push('模型自评置信度，非校准正确率');
  else if (attribution.confidence_basis === 'explicit_user_evidence') bits.push('显式证据');
  else if (attribution.confidence_basis === 'human_review') bits.push('人工复核');
  if (attribution.needs_review) bits.push('待复核');
  return bits.join(' · ');
}

function attributionNote(data: AnalysisDocument): string {
  const document = data.attribution || {};
  const items = document.attributions || [];
  if (!items.length) return '';
  const rows = items.map(attribution => `<div class="segment"><div><strong>${esc(String(attribution.speaker_id).split(':').pop())}</strong> → ${esc(roleNames[attribution.role] || attribution.role)}<p>${esc(roleEvidence(attribution))}</p><p>${esc(attribution.reason || '')}</p></div></div>`).join('');
  const conflicts = (document.conflicts || []).map(conflict => `<p>聚类 ${esc(String(conflict.speaker_id).split(':').pop())} 存在历史角色冲突；新分析只接受用户保存的人工角色。</p>`).join('');
  return '<h3>角色归属</h3><p>请由用户根据音频与转写证据确认每个聚类是测试者、AI 设备或未知。完成并保存前，不生成后续指标和正式测试报告。</p>' + conflicts + rows;
}

function diarizationNote(data: AnalysisDocument): string {
  const stage = data.stages?.diarization || {};
  const scope = data.diarization_scope || {};
  const count = (data.speaker_segments || []).length;
  if (!count) return '<p>说话人聚类：' + esc(statusLabel(stage.status) || '未运行') + ' · ' + esc(stage.reason || '尚未获得说话人分离证据') + '</p>';
  const natives = [...new Set((data.speaker_segments || []).map(segment => segment.native_speaker_id || '未知'))];
  return '<p>说话人聚类：' + esc(statusLabel(stage.status)) + ' · 聚类数 ' + count + ' · 服务原生标签 ' + esc(natives.join('、')) +
    (scope.invocation_id ? ' · 依据调用 ' + esc(scope.invocation_id) : '') +
    '</p><p>聚类只说明“哪些片段属于同一说话人”，不代表已确认谁是测试者、谁是设备。角色需人工复核或显式证据。</p>';
}

function audioQaNote(data: AnalysisDocument): string {
  // Facts measured on the canonical artifact. These are never a recognition,
  // accuracy or acceptance verdict, so no pass/fail wording is used. The stage
  // state and reason are rendered even when no measurements are readable, so an
  // abstention (or a Run whose QA document is missing) is never silently dropped.
  const qa = data.audio_qa || {};
  const measurements = qa.measurements || null;
  const conditions = (measurements && measurements.conditions) || [];
  const stage = data.stages?.audio_qa || {};
  if (!measurements && !stage.status && !stage.reason) return '';
  const facts = measurements && measurements.duration_ms
    ? `时长 ${(measurements.duration_ms / 1000).toFixed(1)} s · ${esc(measurements.encoding || '')} · ${esc(measurements.channels ?? '')} 声道 · ${esc(measurements.sample_rate_hz ?? '')} Hz`
    : '';
  const rows = conditions.map(condition => `<li>${esc(condition.condition_id)}：${esc(condition.status)} — ${esc(condition.basis || '')}<small>${esc(condition.limitation || '')}</small></li>`).join('');
  const header = [facts, stage.status ? esc(statusLabel(stage.status)) : ''].filter(Boolean).join(' · ');
  return '<h3>音频质量检查</h3><p>' + (header || '未获得音频质量测量') + '</p>' +
    (stage.reason ? '<p>' + esc(stage.reason) + '</p>' : '') +
    (rows ? '<ul>' + rows + '</ul>' : '') +
    (!measurements ? '<p>本记录没有可读取的音频质量测量值。</p>' : '') +
    '<p>以上只报告测量事实与有效性条件，不构成识别质量、准确率或验收结论。</p>';
}

function alignmentNote(data: AnalysisDocument): string {
  // Acoustic boundaries and ASR speaker spans are independent evidence. This panel
  // reports the recorded overlap/coverage facts behind every assignment or abstention,
  // so a blank metric table is explainable instead of looking like dropped data. It
  // never turns a coverage measurement into an accuracy or acceptance claim.
  const alignment = data.alignment || {};
  const gap = data.metrics_gap || {};
  const diagnostics = alignment.diagnostics || {};
  if (!alignment.document_id && !((gap.reasons || []).length)) return '';
  const ms = (value: number | null | undefined): string => value == null ? '—' : (Number(value) / 1000).toFixed(1) + ' s';
  const pct = (value: number | null | undefined): string => value == null ? '—' : (Number(value) * 100).toFixed(1) + '%';
  let html = '<h3>声学片段与说话人跨度对齐</h3>';
  html += '<p>对齐状态：' + esc(statusLabel(alignment.status) || '未运行') + (alignment.reason ? ' · ' + esc(alignment.reason) : '') + '</p>';
  if (alignment.policy) html += '<p>策略 ' + esc(alignment.policy.policy_version || '') + ' · 依据 ' + esc(alignment.policy.overlap_basis || '') + '。声学边界与 ASR 时间戳互不替代，未匹配与冲突状态均保留。</p>';
  if (diagnostics.acoustic_segment_count != null) {
    const low = diagnostics.low_energy || {};
    html += '<div class="table-wrap"><table><thead><tr><th>对齐事实</th><th>值</th></tr></thead><tbody>' +
      `<tr><td>未匹配 acoustic 时长</td><td>${ms(diagnostics.unmatched_acoustic_ms)}（${pct(diagnostics.unmatched_acoustic_ratio)}）</td></tr>` +
      `<tr><td>未匹配 speaker 时长</td><td>${ms(diagnostics.unmatched_speaker_ms)}（${pct(diagnostics.unmatched_speaker_ratio)}）</td></tr>` +
      `<tr><td>冲突片段</td><td>${(diagnostics.conflicted_acoustic_segment_ids || []).length}</td></tr>` +
      `<tr><td>低能量片段（其中未匹配）</td><td>${low.segment_count ?? 0}（${ms(low.unmatched_ms)}）</td></tr>` +
      '</tbody></table></div>';
  }
  const clusters = diagnostics.per_cluster || [];
  if (clusters.length) html += '<p>逐聚类覆盖：' + clusters.map(cluster => esc(String(cluster.speaker_id).split(':').pop() as string) + ' ' + pct(cluster.coverage_ratio)).join(' · ') + '</p>';
  const reasons = gap.reasons || [];
  if (reasons.length && gap.status !== 'observed') html += '<h4>为何没有指标</h4><ul>' + reasons.map(reason => `<li>${esc(reason.code)}（涉及 ${reason.count} 个片段）：${esc(reason.detail)}</li>`).join('') + '</ul>';
  html += '<p>以上为覆盖率测量事实，用于定位低音量设备缺口，不构成识别准确率或验收结论。</p>';
  return html;
}
function roleReviewNote(data: AnalysisDocument): string {
  // Manual speaker-role gate. Anonymous clusters are never mapped automatically:
  // the user listens to each cluster and records an explicit decision, including a
  // deliberate 「未知」. Nothing role-dependent is presented before that decision.
  const review = data.role_review || {};
  const clusters = review.clusters || [];
  if (!clusters.length) return '';
  const statusText = ({
    awaiting_role_review: '等待人工确认角色',
    incomplete_review: '确认未完成',
    complete_review: '角色已确认',
  } as Record<string, string>)[review.status || ''] || review.status || '';
  const revision = review.revision;
  const rows = clusters.map(cluster => {
    const label = String(cluster.speaker_id).split(':').pop();
    const intervals = (cluster.representative_intervals || []).map(interval => `<button class="text-button" data-time="${Number(interval.start_ms) / 1000}">${(Number(interval.start_ms) / 1000).toFixed(2)}s</button>`).join(' ');
    const snippets = (cluster.transcript_snippets || []).map(snippet => '<p>' + esc(snippet.text) + '</p>').join('');
    const options = ['tester', 'device', 'unknown'].map(role => `<label><input type="radio" name="role-${esc(cluster.speaker_id)}" value="${role}"${cluster.decision === role ? ' checked' : ''}> ${esc(roleNames[role])}</label>`).join(' ');
    const pending = (review.awaiting_decision_for || []).includes(cluster.speaker_id) ? ' <small>尚未确认</small>' : '';
    return `<div class="segment" data-cluster="${esc(cluster.speaker_id)}"><div><strong>聚类 ${esc(label)}</strong> <small>原生标签 ${esc(cluster.native_speaker_id ?? '未知')} · ${cluster.segment_count} 段 · ${(Number(cluster.speech_ms) / 1000).toFixed(1)} s</small>${pending}<p>${options}</p>${intervals ? '<p>试听区间：' + intervals + '</p>' : ''}${snippets}</div></div>`;
  }).join('');
  const diff = review.diff || {};
  const changed = (diff.changed || []).map(change => `${esc(String(change.speaker_id).split(':').pop())}：${esc(roleNames[change.from] || change.from)} → ${esc(roleNames[change.to] || change.to)}`).join('；');
  let head = '<h3>人工确认说话人角色</h3><p>' + esc(statusText) + (review.reason ? ' · ' + esc(review.reason) : '') + '</p>';
  head += '<p>ASR 只给出匿名聚类；测试者、AI 设备或未知必须由人根据音频与转写证据确认。系统不调用 LLM 判断角色，也不按发言顺序推断。每个聚类都必须明确选择，「未知」是有效决定。</p>';
  if (revision) head += '<p>当前修订 REV-' + esc(revision.revision_index) + ' · 复核人 ' + esc(revision.reviewer) + (revision.reason ? ' · ' + esc(revision.reason) : '') + '</p>';
  if (changed) head += '<p>相对上一修订的变化：' + changed + '</p>';
  const controls = '<div class="segment"><div><label>复核人<input id="role-reviewer" maxlength="200" placeholder="填写复核人身份"></label><label>说明<input id="role-reason" maxlength="2000" placeholder="可选：本次判断依据"></label><button class="text-button" id="role-save">保存角色并重新分析</button></div></div>';
  return head + rows + controls + '<p>保存会创建新的 AnalysisRevision，不覆盖机器原件或上一次人工决定；保存后从 Attribution 向下确定性重跑。</p>';
}

/** Analysis tabs the detail panel can show. */
type AnalysisTab = 'segments' | 'metrics' | 'findings' | 'report';

function tab(name: AnalysisTab): void {
  document.querySelectorAll<HTMLElement>('[data-tab]').forEach(button =>
    button.setAttribute('aria-selected', String(button.dataset.tab === name)));
  const data = current;
  let html = '';
  if (name === 'segments') {
    const segments = data.transcript?.segments?.length
      ? data.transcript.segments
      : (data.fused_segments.length ? data.fused_segments : data.acoustic_segments);
    const byNative = new Map((data.speaker_segments || []).map(segment => [String(segment.native_speaker_id), segment]));
    const roles = new Map(((data.attribution || {}).attributions || []).map(attribution => [attribution.speaker_id, attribution.role]));
    const clusterOf = (segment: SpeechSegment): SpeakerCluster | undefined => byNative.get(String(segment.speaker_id));
    html = '<h2>转写与语音片段</h2><p>转写时间是 ASR 估计，非精确声学边界；说话人聚类与角色判定是两件事。</p>' +
      diarizationNote(data) + roleReviewNote(data) + attributionNote(data) + alignmentNote(data) +
      segments.map(segment => {
        const cluster = clusterOf(segment);
        const role = (cluster && roles.get(cluster.speaker_id)) || segment.speaker_role || 'unknown';
        const clusterInfo = cluster
          ? '<small>聚类 ' + esc(String(cluster.speaker_id).split(':').pop() as string) + '（服务原生标签 ' + esc(String(cluster.native_speaker_id)) + '）</small>'
          : '';
        const review = segment.role_attribution?.needs_review ? ' <small>待复核</small>' : '';
        return `<div class="segment"><button data-time="${Number(segment.start_ms) / 1000}">${(Number(segment.start_ms) / 1000).toFixed(2)} – ${(Number(segment.end_ms) / 1000).toFixed(2)} s</button><div><strong>${esc(roleNames[role] || '角色待确认')}</strong>${review}${clusterInfo}${segment.text_attribution === 'ambiguous_spans_speakers' ? '<small>该句跨越多个说话人，未归属给任一角色</small>' : ''}<p>${esc(segment.text || '暂无转写文本')}</p></div></div>`;
      }).join('');
    if (!segments.length) html += '<div class="empty">没有可用的语音片段，请查看报告中的处理状态。</div>';
  }
  if (name === 'metrics') {
    html = '<h2>评测指标</h2><p>缺少可靠证据时不显示数值，也不将其计为通过。</p><div class="table-wrap"><table><thead><tr><th>指标</th><th>结果</th><th>状态</th></tr></thead><tbody>' +
      data.metrics.map(metric => `<tr><td>${esc(metricNames[metric.name] || metric.name)}</td><td>${metric.value == null ? '—' : esc(metric.value) + ' ' + esc(metric.unit || '')}</td><td>${badge(metric.status)}</td></tr>`).join('') +
      '</tbody></table></div>' +
      (data.metrics.length ? '' : '<div class="empty">暂无可计算指标</div>' + alignmentNote(data));
  }
  if (name === 'findings') {
    html = '<h2>发现与语义评估</h2>' +
      data.findings.map(finding => `<div class="segment"><div><strong>${esc(finding.title || finding.dimension || '待复核发现')}</strong><p>${esc(finding.description || finding.reason || '')}</p>${badge(finding.status)}</div></div>`).join('') +
      (data.findings.length ? '' : '<p>暂无可确认的问题结论。这不代表设备已通过评测。</p>') +
      data.judge_results.map(judge => `<div class="segment"><div><strong>${esc(judge.dimension)} · ${esc(judge.decision)}</strong><p>${esc(judge.reason)}</p>${badge(judge.status)}</div></div>`).join('');
  }
  if (name === 'report') {
    html = (data.stages?.asr && data.stages.asr.status !== 'complete'
      ? '<p>ASR：' + esc(statusLabel(data.stages.asr.status)) + ' · ' + esc(data.stages.asr.reason || '') + '</p><button class="text-button" id="retry-asr">重试转写（可能再次计费）</button>'
      : '') +
      audioQaNote(data) +
      '<h2>完整报告</h2><button class="text-button" id="download">下载 Markdown ↓</button><pre>' + esc(data.report_md || '分析报告尚未生成。原始导入证据已保留。') + '</pre>';
  }
  $('detail').innerHTML = html;
  if ($('retry-asr')) {
    $('retry-asr').onclick = async () => {
      if (busy) return;
      busy = true;
      ($('retry-asr') as HTMLButtonElement).disabled = true;
      try {
        render(await request<AnalysisDocument>('/api/runs/' + encodeURIComponent(data.run_id) + '/resume', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ retry_asr: true }),
        }));
      } catch (error) {
        notify((error as Error).message);
      } finally {
        busy = false;
        if ($('retry-asr')) ($('retry-asr') as HTMLButtonElement).disabled = false;
      }
    };
  }
  if ($('role-save')) {
    $('role-save').onclick = async () => {
      if (busy) return;
      const mapping: Record<string, string> = {};
      let missing = 0;
      document.querySelectorAll<HTMLElement>('[data-cluster]').forEach(node => {
        const picked = node.querySelector<HTMLInputElement>('input[type=radio]:checked');
        const id = node.dataset.cluster as string;
        if (picked) mapping[id] = picked.value;
        else missing += 1;
      });
      if (missing) {
        notify('请为每个说话人聚类选择角色（未知也是明确选择）');
        return;
      }
      const reviewer = (($('role-reviewer') as HTMLInputElement | null)?.value || '').trim();
      if (!reviewer) {
        notify('请填写复核人身份');
        return;
      }
      busy = true;
      ($('role-save') as HTMLButtonElement).disabled = true;
      try {
        render(await request<AnalysisDocument>('/api/runs/' + encodeURIComponent(data.run_id) + '/role-review', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mapping,
            reviewer,
            reason: (($('role-reason') as HTMLInputElement | null)?.value || '').trim(),
          }),
        }));
      } catch (error) {
        notify((error as Error).message);
      } finally {
        busy = false;
        if ($('role-save')) ($('role-save') as HTMLButtonElement).disabled = false;
      }
    };
  }
  $('detail').querySelectorAll<HTMLElement>('[data-time]').forEach(button => {
    button.onclick = () => {
      const audio = $('audio') as HTMLAudioElement;
      audio.currentTime = Number(button.dataset.time);
      audio.play().catch(() => notify('暂时无法播放音频，请检查音频是否已成功标准化。'));
    };
  });
  if ($('download')) {
    $('download').onclick = () => {
      const url = URL.createObjectURL(new Blob([data.report_md as string], { type: 'text/markdown;charset=utf-8' }));
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = data.run_id + '.md';
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
  }
}

request<{ version: string }>('/health')
  .then(data => { $('version').textContent = '版本 ' + data.version; })
  .catch(() => { $('version').textContent = '服务未连接'; });
home();