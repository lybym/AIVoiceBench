/* AIVoiceBench — deterministic Evidence Workbench projection gate.
 *
 * Usage:
 *   node scripts/verify-workbench-render.mjs          # human readable
 *   node scripts/verify-workbench-render.mjs --json   # machine readable (used by tests)
 *   node scripts/verify-workbench-render.mjs --document <workbench.json>
 *                                                     # check a real build_workbench() output
 *
 * `aivoicebench/static/workbench.js` is the compiled renderer of
 * `web/src/workbench.ts`. The browser may only *display* persisted AIVoiceBench
 * evidence (PRD-F014, docs/26 §6): it must never recompute a metric, event, turn,
 * role or boundary, and it must never invent a Region for an id the backend did not
 * serve. Those are exactly the properties that a typecheck cannot prove, so this
 * gate evaluates the compiled artifact in a fresh V8 context with a minimal DOM stub
 * and a synthetic workbench document whose numbers are deliberately distinctive and
 * non-round (`1234.5`, `7890.25`, `432.75`): any unit conversion, rounding or local
 * re-derivation shows up as a value mismatch rather than passing silently.
 *
 * The gate is structural, not browser behaviour evidence: the `mount()` integration
 * section runs the renderer's own code against a DOM stub and a stand-in wavesurfer,
 * so it says nothing about real waveform pixels, real seeking or the real Regions
 * plugin. Real wavesurfer behaviour still needs a real-browser run (Issue #85 / the
 * container acceptance path).
 */

import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STATIC = join(ROOT, 'aivoicebench', 'static');
const ARTIFACT = join(STATIC, 'workbench.js');
const asJson = process.argv.includes('--json');
const documentFlag = process.argv.indexOf('--document');
const servedDocumentPath = documentFlag === -1 ? null : process.argv[documentFlag + 1];
/** Region count of the `--document` projection, for the summary line. */
let servedRegionCount = 0;

/** Ids `index.html` provides. The gate only needs the workbench panels. */
const ELEMENT_IDS = [
  'workbench', 'wb-status', 'wb-refresh', 'wb-gate', 'wb-player', 'wb-waveform',
  'wb-timeline', 'wb-minimap', 'wb-tracks', 'wb-evidence', 'wb-findings', 'wb-metrics',
  'wb-events', 'wb-turns', 'wb-transcript', 'wb-revision', 'wb-provenance',
  'wb-abstentions', 'wb-unavailable',
];

function makeElement(id) {
  const listeners = new Map();
  const element = {
    id,
    hidden: false,
    disabled: false,
    value: '',
    textContent: '',
    innerHTML: '',
    className: '',
    dataset: {},
    style: {},
    children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {},
    getAttribute: () => null,
    removeAttribute() {},
    addEventListener(type, handler) {
      const registered = listeners.get(type) || [];
      registered.push(handler);
      listeners.set(type, registered);
    },
    removeEventListener() {},
    fire(type) { (listeners.get(type) || []).forEach(handler => handler({ type })); },
    appendChild(child) {
      element.children.push(child);
      // A browser fires `load` once a same-origin script has executed. The stub does
      // the same so the renderer's lazy vendor loading can be exercised rather than
      // silently hanging on an event that never arrives.
      if (child && typeof child.fire === 'function') setTimeout(() => child.fire('load'), 0);
      return child;
    },
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  return element;
}

/** Records every call the renderer makes into the stand-in player/plugin. */
const seen = { creates: [], regions: [], seeks: [], zooms: [] };

function buildSandbox() {
  const elements = new Map(ELEMENT_IDS.map(id => [id, makeElement(id)]));
  const document = {
    getElementById: id => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: tag => makeElement(`created-${tag}`),
    head: makeElement('head'),
    body: makeElement('body'),
  };
  // A stand-in for the vendored library. It lives in the sandbox so the renderer's
  // own code (region construction, seeking, zooming) is what gets executed; the real
  // library's rendering is out of scope for a Node gate.
  const regionsPlugin = {
    handlers: {},
    records: [],
    addRegion(options) {
      seen.regions.push({
        id: options.id, start: options.start, end: options.end,
        drag: options.drag, resize: options.resize, color: options.color,
        content: String(options.content || ''),
      });
      const handle = { id: options.id, start: options.start, end: options.end, element: makeElement(`region-${options.id}`), remove() {} };
      regionsPlugin.records.push(handle);
      return handle;
    },
    getRegions: () => regionsPlugin.records,
    on(event, handler) { regionsPlugin.handlers[event] = handler; return () => {}; },
    clearRegions() { regionsPlugin.records.length = 0; },
  };
  const player = {
    on() { return () => {}; },
    registerPlugin(plugin) { return plugin; },
    setTime(time) { seen.seeks.push(time); },
    zoom(minPxPerSec) { seen.zooms.push(minPxPerSec); },
    getDuration: () => 90,
    play: () => Promise.resolve(),
    pause() {},
    destroy() {},
  };
  const sandbox = {
    document,
    console: { log() {}, warn() {}, error() {} },
    setTimeout,
    clearTimeout,
    Date,
    JSON,
    Math,
    Object,
    Array,
    Map,
    Set,
    Promise,
    Number,
    String,
    Boolean,
    Error,
    RegExp,
    // The gate never touches the network: the workbench reads the document it was
    // handed, and re-reading is an explicit user action.
    fetch: () => Promise.reject(new Error('network is not available in the render gate')),
    WaveSurfer: {
      create(options) {
        seen.creates.push({ url: options.url, normalize: options.normalize, height: options.height });
        return player;
      },
      Regions: { create: () => regionsPlugin },
      Timeline: { create: () => ({ destroy() {} }) },
      Minimap: { create: () => ({ destroy() {} }) },
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return { sandbox, elements };
}

/* --- synthetic backend document ------------------------------------------- */

const REGION_A = {
  region_id: 'event:EVT-1',
  track_id: 'event',
  kind: 'event',
  label: '合成事件 A',
  start_ms: 1234500.0,
  end_ms: 7890250.0,
  start_sec: 1234.5,
  end_sec: 7890.25,
  role: null,
  role_basis: null,
  evidence_class: 'deterministic',
  status: 'observed',
  confidence: 0.5,
  uncertainty: null,
  uncertain: false,
  provisional: false,
  envelope_of: null,
  source: {
    document: 'timeline.json',
    document_id: 'timeline-synthetic',
    processor: { name: 'synthetic-processor', version: '3.1.4' },
    artifact_ref: 'ART-timeline',
    evidence_ids: ['EV-1'],
    event_ids: ['EVT-1'],
    metric_ids: [],
    turn_ids: [],
  },
  detail: { segment_id: 'SEG-1', note: '合成 detail' },
};

const REGION_B = {
  region_id: 'speaker:speaker_0:0',
  track_id: 'speaker',
  kind: 'speaker_segment',
  label: 'speaker_0',
  start_ms: 0.5,
  end_ms: 2000.5,
  start_sec: 0.0005,
  end_sec: 2.0005,
  role: 'unknown',
  role_basis: null,
  evidence_class: 'deterministic',
  status: 'observed',
  confidence: null,
  uncertainty: '说话人角色尚未由用户确认',
  uncertain: true,
  provisional: true,
  envelope_of: null,
  source: {
    document: 'speaker-assignments.json',
    document_id: 'speakers-synthetic',
    processor: null,
    artifact_ref: null,
    evidence_ids: [],
    event_ids: [],
    metric_ids: [],
    turn_ids: [],
  },
  detail: { segment_id: 'SEG-2' },
};

/** A role-confirmed `turn` region: the panel must show these served fields verbatim. */
const REGION_C = {
  region_id: 'turn:TURN-1',
  track_id: 'turn',
  kind: 'turn',
  label: 'TURN-1',
  start_ms: 4500.0,
  end_ms: 9250.0,
  start_sec: 4.5,
  end_sec: 9.25,
  role: 'device',
  role_basis: 'user_review',
  evidence_class: 'deterministic',
  status: 'observed',
  confidence: 0.625,
  uncertainty: null,
  uncertain: false,
  provisional: false,
  envelope_of: null,
  source: {
    document: 'turns.json',
    document_id: 'turns-synthetic',
    processor: { name: 'synth-turn-processor', version: '2.0.0' },
    artifact_ref: 'ART-turns',
    evidence_ids: [],
    event_ids: [],
    metric_ids: [],
    turn_ids: ['TURN-1'],
  },
  detail: { turn_id: 'TURN-1', has_interruption: false, note: '合成轮次' },
};

/** A `finding` region. The envelope span is the backend's, never re-derived here. */
const REGION_D = {
  region_id: 'finding:FND-1',
  track_id: 'finding',
  kind: 'finding',
  label: '合成发现',
  start_ms: 12500.0,
  end_ms: 18250.0,
  start_sec: 12.5,
  end_sec: 18.25,
  role: null,
  role_basis: null,
  evidence_class: 'semantic',
  status: 'needs_verification',
  confidence: 0.25,
  uncertainty: null,
  uncertain: false,
  provisional: false,
  envelope_of: 1,
  source: {
    document: 'findings.json',
    document_id: 'findings-synthetic',
    processor: { name: 'synth-finding-processor', version: '1.1.0' },
    artifact_ref: 'ART-findings',
    evidence_ids: ['EV-1'],
    event_ids: ['EVT-1'],
    metric_ids: ['MET-1'],
    turn_ids: [],
  },
  detail: { finding_id: 'FND-1', title: '合成发现', note: '合成 finding detail' },
};

/** A `metric` region whose value must be displayed verbatim. */
const REGION_E = {
  region_id: 'metric:MET-1',
  track_id: 'metric',
  kind: 'metric',
  label: 'first_speech_latency_ms = 432.75',
  start_ms: 20750.0,
  end_ms: 24125.0,
  start_sec: 20.75,
  end_sec: 24.125,
  role: null,
  role_basis: null,
  evidence_class: 'deterministic',
  status: 'observed',
  confidence: null,
  uncertainty: null,
  uncertain: false,
  provisional: false,
  envelope_of: 1,
  source: {
    document: 'metrics.json',
    document_id: 'metrics-synthetic',
    processor: { name: 'synth-metric-processor', version: '4.2.0' },
    artifact_ref: 'ART-metrics',
    evidence_ids: ['EV-1'],
    event_ids: [],
    metric_ids: ['MET-1'],
    turn_ids: [],
  },
  detail: { metric_id: 'MET-1', name: 'first_speech_latency_ms', value: 432.75 },
};

/** A second `event` region so per-id navigation cannot pass by accident. */
const REGION_F = {
  region_id: 'event:EVT-2',
  track_id: 'event',
  kind: 'event',
  label: 'device_speech_end',
  start_ms: 30500.0,
  end_ms: 33875.0,
  start_sec: 30.5,
  end_sec: 33.875,
  role: null,
  role_basis: null,
  evidence_class: 'deterministic',
  status: 'observed',
  confidence: null,
  uncertainty: null,
  uncertain: false,
  provisional: false,
  envelope_of: null,
  source: {
    document: 'timeline.json',
    document_id: 'timeline-synthetic',
    processor: { name: 'synthetic-processor', version: '3.1.4' },
    artifact_ref: 'ART-timeline',
    evidence_ids: ['EV-2'],
    event_ids: ['EVT-2'],
    metric_ids: [],
    turn_ids: [],
  },
  detail: { event_id: 'EVT-2', type: 'device_speech_end' },
};

const DOCUMENT = {
  schema_version: '1.0.0',
  document_id: 'WORKBENCH-synthetic',
  run_id: 'RUN-synthetic',
  analysis_id: 'ANALYSIS-synthetic',
  gate: {
    status: 'complete_review',
    reason: 'synthetic gate',
    role_dependent_available: true,
    view_kind: 'role_confirmed',
  },
  revision: null,
  audio: {
    artifact_id: 'ART-audio',
    kind: 'normalized_audio',
    sha256: 'synthetic',
    duration_ms: 90000.0,
    sample_rate: 16000,
    url: '/api/runs/RUN-synthetic/audio',
  },
  tracks: [
    { track_id: 'event', label: '事件时间线', evidence_class: 'deterministic' },
    { track_id: 'speaker', label: '说话人聚类', evidence_class: 'deterministic' },
    { track_id: 'turn', label: '对话轮次', evidence_class: 'deterministic' },
    { track_id: 'metric', label: '指标证据', evidence_class: 'deterministic' },
    { track_id: 'finding', label: 'Findings', evidence_class: 'semantic' },
  ],
  regions: [REGION_A, REGION_B, REGION_C, REGION_D, REGION_E, REGION_F],
  metrics: [
    {
      metric_id: 'MET-1', name: 'first_speech_latency_ms', value: 432.75, unit: 'ms',
      status: 'observed', reason: null, turn_id: 'TURN-1', region_id: 'metric:MET-1',
      span_origin: 'evidence', evidence_ids: ['EV-1'], provisional: false,
    },
    {
      metric_id: 'MET-2', name: 'overlap_ratio', value: null, unit: null,
      status: 'insufficient_evidence', reason: '没有可用证据', turn_id: null, region_id: null,
      span_origin: 'turn', evidence_ids: [], provisional: false,
    },
  ],
  findings: [
    {
      finding_id: 'FND-1', title: '合成发现', severity: 'observation', status: 'needs_verification',
      confidence: 0.25, description: '合成说明', region_id: 'finding:FND-1', span_origin: 'event',
      requires_log_verification: true, human_review: {}, evidence_ids: ['EV-1'],
      event_ids: ['EVT-1'], metric_ids: ['MET-1'], turn_ids: [], provisional: false,
    },
  ],
  events: [
    {
      event_id: 'EVT-1', type: 'device_speech_start', start_ms: 1234500.0, end_ms: 1234500.0,
      source: 'silero', confidence: null, turn_id: null, region_id: 'event:EVT-1',
      evidence_ids: ['EV-1'],
    },
    {
      event_id: 'EVT-2', type: 'device_speech_end', start_ms: 30500.0, end_ms: 33875.0,
      source: 'silero', confidence: 0.5, turn_id: 'TURN-1', region_id: 'event:EVT-2',
      evidence_ids: ['EV-2'],
    },
  ],
  turns: [
    {
      turn_id: 'TURN-1', region_id: 'turn:TURN-1', has_interruption: false, has_overlap: true,
      start_ms: 4500.0, end_ms: 9250.0,
    },
  ],
  transcript: [
    {
      // Production shape: the ASR utterance id (`ASR-####`) and the acoustic region's
      // own `detail.segment_id` (`SEG-*`) are different evidence namespaces, so the
      // backend publishes the resolved link as `region_id` and this layer must use it.
      segment_id: 'ASR-0001', region_id: 'event:EVT-1', region_ids: ['event:EVT-1'],
      region_basis: 'fused_acoustic_segment',
      start_ms: 1234500.0, end_ms: 1300000.0, text: '合成文本',
      speaker_id: 'speaker_0', speaker_role: null, timestamp_source: 'asr',
    },
    {
      // Deliberately carries an id that *does* equal a served region's
      // `detail.segment_id` while publishing no `region_id`: matching on the persisted
      // detail id must never create a link.
      segment_id: 'SEG-1', region_id: null, region_ids: [], region_basis: 'unresolved',
      start_ms: 2000000.0, end_ms: 2100000.0, text: '无对应区间',
      speaker_id: 'speaker_1', speaker_role: null, timestamp_source: 'asr',
    },
  ],
  unavailable: [{ stage: 'judge', status: 'insufficient_evidence', reason: '合成缺失' }],
  abstentions: {
    metrics_gap: { status: 'insufficient_evidence', reasons: [{ code: 'unmatched', count: 3, detail: '合成原因' }] },
    findings: [],
    rejected_findings: [],
    stages: [],
  },
  provenance: {
    run_id: 'RUN-synthetic',
    analysis_id: 'ANALYSIS-synthetic',
    artifacts: [{ artifact_id: 'ART-audio', kind: 'normalized_audio', sha256: 'synthetic', processor: null }],
    provider_invocations: [],
    model_configuration: { artifact_ref: 'ART-config', routes: {} },
    policies: [],
    processors: ['synthetic-processor'],
  },
};

/* --- checks --------------------------------------------------------------- */

const checks = [];
const errors = [];

function check(name, run) {
  let detail = '';
  let ok = false;
  try {
    const result = run();
    ok = result === true;
    if (typeof result === 'string') { ok = false; detail = result; }
  } catch (error) {
    ok = false;
    detail = `threw ${error && error.message ? error.message : String(error)}`;
  }
  checks.push({ name, ok, detail });
  if (!ok) errors.push(detail ? `${name}: ${detail}` : name);
}

let source = '';
try {
  source = readFileSync(ARTIFACT, 'utf8');
} catch (error) {
  const payload = {
    ok: false,
    reason: 'artifact_unreadable',
    checks: [],
    errors: [`${ARTIFACT} could not be read: ${error.message}. Run \`npm run build\` first.`],
    log: [`${ARTIFACT} could not be read: ${error.message}. Run \`npm run build\` first.`],
  };
  if (asJson) process.stdout.write(JSON.stringify(payload, null, 2) + '\n');
  else process.stdout.write(payload.log.join('\n') + '\n');
  process.exit(1);
}

const built = buildSandbox();
const sandbox = vm.createContext(built.sandbox);
const elements = built.elements;
let evaluated = true;
try {
  vm.runInContext(source, sandbox, { filename: 'workbench.js' });
} catch (error) {
  evaluated = false;
  errors.push(`workbench.js failed to evaluate as a classic script: ${error.message}`);
}

function inContext(expression) {
  return vm.runInContext(expression, sandbox);
}

/**
 * Check the renderer against a **real** `build_workbench()` projection.
 *
 * The synthetic document above is deliberately shaped by this gate; this mode takes a
 * document the Python backend actually produced, so the two cannot drift apart. It
 * asserts the same invariants (served coordinates drawn verbatim, one seek per
 * navigable record, no link the backend did not publish) without any synthetic
 * constants.
 */
async function runServedDocumentChecks(path) {
  let served = null;
  try {
    served = JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    errors.push(`--document ${path} could not be read: ${error.message}`);
    return;
  }
  const documentRegions = Array.isArray(served.regions) ? served.regions : [];
  servedRegionCount = documentRegions.length;
  check('the served document is a real workbench projection', () => {
    if (!served || typeof served !== 'object') return 'the document is not an object';
    if (!served.document_id) return 'the document carries no document_id';
    if (!Array.isArray(served.regions)) return 'the document carries no regions array';
    if (!Array.isArray(served.transcript)) return 'the document carries no transcript array';
    return true;
  });
  sandbox.__served = { run_id: served.run_id, workbench: served };
  let mountError = '';
  try {
    await vm.runInContext('window.WB.mount(__served)', sandbox);
  } catch (error) {
    mountError = error && error.message ? error.message : String(error);
  }
  check('mount() renders the real projection without throwing', () =>
    mountError ? `mount() threw ${mountError}` : true);

  check('every served region is drawn at its own served start_sec/end_sec', () => {
    if (seen.regions.length !== documentRegions.length) {
      return `expected ${documentRegions.length} drawn regions, saw ${seen.regions.length}`;
    }
    for (let index = 0; index < documentRegions.length; index += 1) {
      const drawn = seen.regions[index];
      const expected = documentRegions[index];
      if (drawn.id !== expected.region_id) {
        return `region ${index} is ${drawn.id} instead of ${expected.region_id}`;
      }
      if (drawn.start !== expected.start_sec || drawn.end !== expected.end_sec) {
        return `region ${expected.region_id} drew ${drawn.start}..${drawn.end} instead of `
          + `${expected.start_sec}..${expected.end_sec}`;
      }
    }
    if (!documentRegions.length) return 'the served document published no region to check';
    return true;
  });

  const targets = [];
  for (const member of ['findings', 'metrics', 'events', 'turns']) {
    for (const row of (Array.isArray(served[member]) ? served[member] : [])) {
      if (row && row.region_id) targets.push(String(row.region_id));
    }
  }
  const uniqueTargets = [...new Set(targets)];
  let navigationError = '';
  for (const regionId of uniqueTargets) {
    const expected = documentRegions.find(region => region.region_id === regionId);
    if (!expected) {
      navigationError = `${regionId} is referenced but was never published as a region`;
      break;
    }
    sandbox.__targetId = regionId;
    const before = seen.seeks.length;
    const row = await vm.runInContext('window.WB.selectEvidence(__targetId)', sandbox);
    const seeks = seen.seeks.slice(before);
    if (!row || row.region_id !== regionId) {
      navigationError = `${regionId} did not resolve to its served region`;
      break;
    }
    if (seeks.length !== 1 || seeks[0] !== expected.start_sec) {
      navigationError = `${regionId} seeked ${JSON.stringify(seeks)} instead of ${expected.start_sec}`;
      break;
    }
  }
  check('every record with a served region_id navigates to that region exactly once', () => {
    if (navigationError) return navigationError;
    if (!uniqueTargets.length) return 'the served document published no navigable record';
    return true;
  });

  check('a transcript row is navigable exactly when the backend published its region_id', () => {
    const published = new Set(documentRegions.map(region => String(region.region_id)));
    const html = elements.get('wb-transcript').innerHTML;
    const rows = Array.isArray(served.transcript) ? served.transcript : [];
    if (!rows.length) return 'the served document published no transcript row to check';
    for (const row of rows) {
      const label = String(row.segment_id == null ? '' : row.segment_id);
      const linked = new RegExp(`<button[^>]*data-wb-region="([^"]*)"[^>]*>${label}</button>`).test(html);
      if (row.region_id && published.has(String(row.region_id))) {
        if (!linked) {
          return `transcript ${label} carries region_id ${row.region_id} but is not navigable`;
        }
      } else if (linked) {
        return `transcript ${label} has no published region_id yet was made navigable`;
      }
    }
    return true;
  });
}

if (servedDocumentPath) {
  await runServedDocumentChecks(servedDocumentPath);
} else if (evaluated) {
  check('publishes window.WB with the documented surface', () => {
    const surface = inContext('Object.keys(window.WB || {}).sort().join(",")');
    const expected = ['eventRows', 'findingRows', 'load', 'metricRows', 'mount', 'playerState',
                      'regionsFromDocument', 'selectEvidence', 'state', 'transcriptRows',
                      'turnRows', 'unmount'];
    const missing = expected.filter(key => !surface.split(',').includes(key));
    return missing.length ? `window.WB is missing ${missing.join(', ')}` : true;
  });

  check('state() is null before any document is loaded', () =>
    inContext('window.WB.state()') === null);

  check('playerState() is null before a player exists', () =>
    inContext('window.WB.playerState()') === null);

  check('regionsFromDocument() keeps the served order and coordinates', () => {
    sandbox.__doc = DOCUMENT;
    const rows = inContext('window.WB.regionsFromDocument(__doc)');
    if (!Array.isArray(rows)) return 'regionsFromDocument did not return an array';
    if (rows.length !== DOCUMENT.regions.length) {
      return `expected ${DOCUMENT.regions.length} rows, got ${rows.length}`;
    }
    for (let index = 0; index < rows.length; index += 1) {
      const served = DOCUMENT.regions[index];
      const row = rows[index];
      if (row.region_id !== served.region_id) return `row ${index} region_id ${row.region_id}`;
      if (row.start !== served.start_sec) return `row ${index} start ${row.start} != ${served.start_sec}`;
      if (row.end !== served.end_sec) return `row ${index} end ${row.end} != ${served.end_sec}`;
      if (row.role !== served.role) return `row ${index} role ${row.role} != ${served.role}`;
      if (row.evidence_class !== served.evidence_class) {
        return `row ${index} evidence_class ${row.evidence_class}`;
      }
    }
    const starts = rows.map(row => row.start);
    if (!starts.includes(1234.5) || !rows.map(row => row.end).includes(7890.25)) {
      return 'the distinctive persisted coordinates did not survive the projection';
    }
    return true;
  });

  check('regionsFromDocument() returns [] for a document with no regions', () => {
    sandbox.__empty = { schema_version: '1.0.0', regions: [] };
    const rows = inContext('window.WB.regionsFromDocument(__empty)');
    return Array.isArray(rows) && rows.length === 0 ? true : `got ${JSON.stringify(rows)}`;
  });

  check('regionsFromDocument() tolerates missing optional members', () => {
    sandbox.__sparse = { schema_version: '1.0.0' };
    const rows = inContext('window.WB.regionsFromDocument(__sparse)');
    if (!Array.isArray(rows) || rows.length !== 0) return `got ${JSON.stringify(rows)}`;
    const nullDocument = inContext('window.WB.regionsFromDocument(null)');
    return Array.isArray(nullDocument) && nullDocument.length === 0 ? true : 'null document did not return []';
  });

  check('metricRows() reproduces every served value verbatim', () => {
    sandbox.__doc = DOCUMENT;
    const rows = inContext('window.WB.metricRows(__doc)');
    if (!Array.isArray(rows) || rows.length !== DOCUMENT.metrics.length) {
      return `expected ${DOCUMENT.metrics.length} rows`;
    }
    for (let index = 0; index < rows.length; index += 1) {
      const served = DOCUMENT.metrics[index];
      if (rows[index].value !== served.value) {
        return `row ${index} value ${String(rows[index].value)} != ${String(served.value)}`;
      }
    }
    if (rows[0].value !== 432.75) return 'the distinctive metric value did not survive';
    if (rows[1].value !== null) return 'a null metric value must stay null, never 0';
    return true;
  });

  check('findingRows()/eventRows()/turnRows()/transcriptRows() keep their served order', () => {
    const pairs = [
      ['findingRows', 'findings'],
      ['eventRows', 'events'],
      ['turnRows', 'turns'],
      ['transcriptRows', 'transcript'],
    ];
    for (const [method, member] of pairs) {
      const rows = inContext(`window.WB.${method}(__doc)`);
      if (!Array.isArray(rows) || rows.length !== DOCUMENT[member].length) {
        return `${method} returned ${Array.isArray(rows) ? rows.length : 'a non-array'}`;
      }
    }
    const events = inContext('window.WB.eventRows(__doc)');
    return events[0].start_ms === 1234500.0 ? true : 'event start_ms was not passed through';
  });

  check('load() records an explicit error state for a missing document', () => {
    let threw = false;
    try {
      inContext('window.WB.load("RUN-synthetic", null)');
    } catch (error) {
      threw = true;
    }
    if (!threw) return 'load(null) must throw instead of inventing a document';
    const state = inContext('window.WB.state()');
    if (!state || state.loaded !== false) return 'state() must report loaded: false';
    if (!state.error) return 'state() must carry an explicit error message';
    return inContext('window.WB.regionsFromDocument(__doc).length') === DOCUMENT.regions.length
      ? true
      : 'a rejected load must not disturb the caller document';
  });

  check('load() stores the served document and reports its own numbers', () => {
    sandbox.__doc = DOCUMENT;
    const state = inContext('window.WB.load("RUN-synthetic", __doc)');
    if (!state || state.loaded !== true) return 'load did not report a loaded state';
    if (state.documentId !== 'WORKBENCH-synthetic') return `documentId ${state.documentId}`;
    if (state.regionCount !== DOCUMENT.regions.length) return `regionCount ${state.regionCount}`;
    if (state.runId !== 'RUN-synthetic') return `runId ${state.runId}`;
    return true;
  });

  check('selectEvidence() returns null for an unknown region id', () =>
    inContext('window.WB.selectEvidence("does-not-exist")') === null ? true
      : 'an unknown id must not produce a synthesised region');

  check('selectEvidence() returns the served region, never a copy', () => {
    const row = inContext('window.WB.selectEvidence("event:EVT-1")');
    if (!row) return 'the served region was not resolved';
    if (row.region_id !== 'event:EVT-1') return `region_id ${row.region_id}`;
    if (row.start !== 1234.5 || row.end !== 7890.25) {
      return `coordinates ${row.start}..${row.end} were re-derived`;
    }
    return true;
  });

  // ---- integration: execute the renderer's own mount path --------------------
  // The pure projections above cannot catch a broken render function, and a real
  // browser run is not available to every contributor. This section therefore runs
  // `mount()` for real - DOM stub, stand-in wavesurfer, lazy same-origin script loader
  // - and proves that a Finding / Metric / Event row navigates to the *backend's*
  // interval and synchronizes the evidence panel from the served fields.

  const provisionalDocument = Object.assign({}, DOCUMENT, {
    document_id: 'WORKBENCH-provisional',
    gate: {
      status: 'awaiting_role_review',
      reason: '合成的等待状态',
      role_dependent_available: false,
      view_kind: 'provisional',
    },
    tracks: [{ track_id: 'speaker', label: '说话人聚类', evidence_class: 'deterministic' }],
    // A provisional document omits the role-dependent turn/metric/finding regions.
    regions: [REGION_B],
    metrics: [],
    findings: [],
    events: [],
    turns: [],
    transcript: [],
    unavailable: [{ stage: 'turn', status: 'insufficient_evidence', reason: '人工角色确认未完成：合成等待' }],
    abstentions: { metrics_gap: { status: 'insufficient_evidence', reasons: [{ code: 'unmatched', count: 3, detail: '合成原因' }] } },
  });

  /** The regions the parent asked for: one per navigable evidence kind. */
  const NAVIGABLE = [
    { id: 'finding:FND-1', start: 12.5, kind: 'finding' },
    { id: 'metric:MET-1', start: 20.75, kind: 'metric' },
    { id: 'event:EVT-1', start: 1234.5, kind: 'event' },
    { id: 'event:EVT-2', start: 30.5, kind: 'event' },
  ];

  sandbox.__hostDoc = { run_id: DOCUMENT.run_id, workbench: DOCUMENT };
  sandbox.__hostProvisional = { run_id: provisionalDocument.run_id, workbench: provisionalDocument };
  let mountError = '';
  const confirmed = { gate: '', metrics: '', status: '', transcript: '', regions: [], panels: {} };
  const provisional = { regions: [] };
  try {
    await vm.runInContext('window.WB.mount(__hostDoc)', sandbox);
    confirmed.regions = seen.regions.slice();
    confirmed.gate = elements.get('wb-gate').innerHTML;
    confirmed.metrics = elements.get('wb-metrics').innerHTML;
    confirmed.status = elements.get('wb-status').innerHTML;
    confirmed.transcript = elements.get('wb-transcript').innerHTML;
    // Navigate to every navigable kind and capture what the panels show.
    for (const target of NAVIGABLE) {
      sandbox.__target = target.id;
      const before = seen.seeks.length;
      confirmed.panels[target.id] = {
        row: await vm.runInContext('window.WB.selectEvidence(__target)', sandbox),
        seeks: seen.seeks.slice(before),
        evidence: elements.get('wb-evidence').innerHTML,
      };
    }
    sandbox.__missing = 'region-that-does-not-exist';
    const seeksBeforeMissing = seen.seeks.length;
    const missing = await vm.runInContext('window.WB.selectEvidence(__missing)', sandbox);
    const seeksAfterMissing = seen.seeks.length;
    confirmed.missing = { row: missing, leaked: seeksAfterMissing - seeksBeforeMissing };
    // The role-bearing region is captured while the confirmed document is loaded.
    sandbox.__turn = 'turn:TURN-1';
    confirmed.turn = {
      row: await vm.runInContext('window.WB.selectEvidence(__turn)', sandbox),
      evidence: elements.get('wb-evidence').innerHTML,
    };
    await vm.runInContext('window.WB.mount(__hostProvisional)', sandbox);
    provisional.regions = seen.regions.slice(confirmed.regions.length);
  } catch (error) {
    mountError = error && error.message ? error.message : String(error);
  }

  check('mount() renders a served document without throwing', () =>
    mountError ? `mount() threw ${mountError}` : true);

  check('mount() creates the player from the served audio and the vendored library', () => {
    if (!seen.creates.length) return 'wavesurfer.create() was never called';
    const created = seen.creates[0];
    if (created.url !== DOCUMENT.audio.url) return `player url ${created.url}`;
    if (created.normalize !== true) return 'normalize: true must be requested';
    return true;
  });

  check('mount() creates exactly one Region per served region, in document order', () => {
    if (confirmed.regions.length !== DOCUMENT.regions.length) {
      return `expected ${DOCUMENT.regions.length} regions, saw ${confirmed.regions.length}`;
    }
    for (let index = 0; index < DOCUMENT.regions.length; index += 1) {
      const served = DOCUMENT.regions[index];
      const region = confirmed.regions[index];
      if (region.id !== served.region_id) return `region ${index} id ${region.id}`;
      if (region.start !== served.start_sec) {
        return `region ${index} start ${region.start} != served ${served.start_sec}`;
      }
      if (region.end !== served.end_sec) {
        return `region ${index} end ${region.end} != served ${served.end_sec}`;
      }
      if (region.drag !== false || region.resize !== false) {
        return 'regions must not be draggable or resizable (no local evidence editing)';
      }
    }
    return true;
  });

  check('every navigable kind seeks to its own persisted start_sec, unconverted', () => {
    for (const target of NAVIGABLE) {
      const panel = confirmed.panels[target.id];
      if (!panel) return `no navigation result recorded for ${target.id}`;
      if (!panel.row) return `${target.id} did not resolve to a region`;
      if (panel.row.region_id !== target.id) return `${target.id} resolved to ${panel.row.region_id}`;
      if (panel.row.start !== target.start) {
        return `${target.id} reported start ${panel.row.start} instead of ${target.start}`;
      }
      if (panel.seeks.length !== 1) {
        return `${target.id} produced ${panel.seeks.length} seek calls, expected exactly 1`;
      }
      if (panel.seeks[0] !== target.start) {
        return `${target.id} seeked to ${panel.seeks[0]} instead of ${target.start} seconds`;
      }
    }
    return true;
  });

  check('a transcript row links only through the served region_id, never a name match', () => {
    const html = confirmed.transcript;
    if (!html) return 'the transcript panel was not rendered';
    // The linked row: produced by the backend-resolved `region_id`.
    const linked = html.match(/<button[^>]*data-wb-region="([^"]*)"[^>]*>ASR-0001<\/button>/);
    if (!linked) return 'a transcript row with a served region_id was not made navigable';
    if (linked[1] !== 'event:EVT-1') {
      return `the transcript row linked to ${linked[1]} instead of its served region`;
    }
    // The unlinked row: its `segment_id` equals a served region's `detail.segment_id`,
    // which must not be enough to build a link.
    if (/<button[^>]*>\s*SEG-1\s*<\/button>/.test(html)) {
      return 'a transcript row was linked by matching detail.segment_id instead of region_id';
    }
    if (!html.includes('SEG-1')) return 'the unmatched transcript row is missing from the table';
    return true;
  });

  check('a transcript link reaches the synchronized evidence panel', () => {
    const panel = confirmed.panels['event:EVT-1'];
    if (!panel) return 'the event panel probe did not run';
    if (!panel.evidence.includes('ASR-0001')) {
      return 'the transcript row linked to the selected region is missing from the panel';
    }
    // The unlinked row must not appear: it has no served region to synchronize with.
    if (panel.evidence.includes('无对应区间')) {
      return 'a transcript row without a served region_id was synchronized anyway';
    }
    return true;
  });

  check('the finding/event/metric panels show the served transcript, provenance and role fields', () => {
    const finding = confirmed.panels['finding:FND-1'];
    const metric = confirmed.panels['metric:MET-1'];
    const event = confirmed.panels['event:EVT-1'];
    if (!finding || !metric || !event) return 'a navigation panel was not recorded';
    // Served transcript text reaches the panel through the transcript row's
    // backend-resolved `region_id`; it is copied, never re-transcribed or re-timed.
    if (!event.evidence.includes('合成文本')) {
      return 'the served transcript text is missing from the synchronized panel';
    }
    if (!event.evidence.includes('timeline.json')) return 'the served source document is missing';
    if (!event.evidence.includes('synthetic-processor')) return 'the served processor is missing';
    if (!event.evidence.includes('ART-timeline')) return 'the served artifact reference is missing';
    if (!event.evidence.includes('1234.5')) return 'the served start_sec is missing';
    if (!finding.evidence.includes('findings.json')) return 'the finding source document is missing';
    if (!finding.evidence.includes('synth-finding-processor')) {
      return 'the finding processor is missing';
    }
    if (!finding.evidence.includes('needs_verification')) {
      return 'the served finding status is missing';
    }
    if (!finding.evidence.includes('0.25')) return 'the served finding confidence is missing';
    if (!metric.evidence.includes('metrics.json')) return 'the metric source document is missing';
    if (!metric.evidence.includes('20.75')) return 'the served metric region start_sec is missing';
    if (!metric.evidence.includes('432.75')) return 'the served metric value is missing';
    return true;
  });

  check('a role-confirmed region shows the served role, role_basis, confidence and status', () => {
    const panel = confirmed.turn;
    if (!panel) return 'the role-confirmed region probe did not run';
    if (!panel.row || panel.row.role !== 'device') return 'the served role was not resolved';
    const html = panel.evidence;
    if (!html.includes('AI 设备')) return 'the served role label is missing';
    if (!html.includes('user_review')) return 'the served role_basis is missing';
    if (!html.includes('0.625')) return 'the served confidence is missing';
    if (!html.includes('observed')) return 'the served status is missing';
    if (!html.includes('turns.json')) return 'the served source document is missing';
    if (!html.includes('synth-turn-processor')) return 'the served processor is missing';
    return true;
  });

  check('an unknown region id returns null and does not move the player', () => {
    if (!confirmed.missing) return 'the unknown-id probe did not run';
    if (confirmed.missing.row !== null) return 'an unknown id must not produce a synthesised region';
    if (confirmed.missing.leaked !== 0) {
      return `an unknown id produced ${confirmed.missing.leaked} seek calls`;
    }
    return true;
  });

  check('the metrics table shows the served value and N/A for an abstention', () => {
    const html = confirmed.metrics;
    if (!html.includes('432.75')) return 'the served metric value is missing from the table';
    const row = html.split('<tr>').find(part => part.includes('overlap_ratio'));
    if (!row) return 'the abstained metric row is missing';
    if (!row.includes('N/A')) return 'a null metric value must render as N/A';
    if (row.includes('>0<')) return 'an abstained metric must never be shown as 0';
    return true;
  });

  check('a confirmed gate renders the role-confirmed view', () => {
    if (!confirmed.gate.includes('role_confirmed')) {
      return 'the served role_confirmed view_kind must be shown';
    }
    return true;
  });

  check('a provisional document renders the banner and the unavailable list verbatim', () => {
    const gate = elements.get('wb-gate').innerHTML;
    if (!gate.includes('provisional')) return 'the provisional banner is missing';
    if (!gate.includes('view_kind')) return 'the gate fields must be shown from the backend document';
    const unavailable = elements.get('wb-unavailable').innerHTML;
    if (!unavailable.includes('人工角色确认未完成：合成等待')) {
      return 'the backend unavailable reason must be rendered verbatim';
    }
    const metrics = elements.get('wb-metrics').innerHTML;
    if (!metrics.includes('后端未提供这类证据')) {
      return 'a role-dependent section must be reported as unavailable, not filled in';
    }
    const turns = elements.get('wb-turns').innerHTML;
    if (!turns.includes('后端未提供这类证据')) {
      return 'no turn may be fabricated while the role gate is open';
    }
    if (confirmed.status.includes('缺失')) {
      return `the confirmed document reported a loading failure: ${confirmed.status}`;
    }
    return true;
  });

  check('a provisional mount creates no region for an omitted role-dependent track', () => {
    const served = provisionalDocument.regions;
    if (provisional.regions.length !== served.length) {
      return `expected ${served.length} regions for the provisional document, saw ${provisional.regions.length}`;
    }
    const roleDependent = new Set(['turn', 'metric', 'finding']);
    for (const region of provisional.regions) {
      const provider = served.find(candidate => candidate.region_id === region.id);
      if (!provider) return `an unserved region ${region.id} was created`;
      if (roleDependent.has(provider.track_id)) {
        return `a ${provider.track_id} region was created while the role gate is open`;
      }
      if (region.start !== provider.start_sec || region.end !== provider.end_sec) {
        return `region ${region.id} coordinates were not the served start_sec/end_sec`;
      }
    }
    return true;
  });

  check('unmount() clears the loaded document', () => {
    inContext('window.WB.unmount()');
    const state = inContext('window.WB.state()');
    if (state !== null) return 'state() must be null again after unmount()';
    return inContext('window.WB.selectEvidence("event:EVT-1")') === null
      ? true
      : 'selectEvidence() must not resolve a region after unmount()';
  });
}

const ok = errors.length === 0;
const log = [];
for (const entry of checks) log.push(`${entry.ok ? 'ok  ' : 'FAIL'} ${entry.name}${entry.detail ? ` — ${entry.detail}` : ''}`);
for (const error of errors) if (!log.some(line => line.includes(error))) log.push(`ERROR ${error}`);
if (ok) {
  log.push(servedDocumentPath
    ? `A real build_workbench() projection reproduces its served coordinates and every `
      + `record with a region_id navigates to it exactly once (${checks.length} checks, `
      + `${servedRegionCount} served regions).`
    : 'Evidence Workbench projections reproduce the backend document verbatim and every '
      + `Finding/Metric/Event region navigates to the served interval (${checks.length} checks, `
      + `${DOCUMENT.regions.length} synthetic regions).`);
}

const payload = {
  ok,
  reason: ok ? 'ok' : 'workbench_render_verification_failed',
  artifact: 'aivoicebench/static/workbench.js',
  mode: servedDocumentPath ? 'served_document' : 'synthetic_document',
  checks,
  errors,
  log,
};

if (asJson) process.stdout.write(JSON.stringify(payload, null, 2) + '\n');
else for (const line of log) process.stdout.write(line + '\n');
if (!ok) process.exitCode = 1;
