/* AIVoiceBench — Browser Station script-scope smoke check.
 *
 * Usage: node scripts/smoke-web-station.mjs [--json]
 *
 * The TypeScript `.ts` sources are compiled to `.js` files that the page loads
 * with plain `<script src>` tags. That means they are *classic* scripts: they
 * share one global script scope and must keep doing so after compilation. A
 * module wrapper, a duplicated top-level name or a reference that was dropped
 * during the migration would not fail the typecheck (each file still compiles on
 * its own) but would break the page at runtime.
 *
 * This check evaluates the compiled artifacts in a single V8 context with a
 * minimal DOM stub standing in for `index.html`, then asserts the page's expected
 * globals exist and that the published control layer is a live object. It is a
 * structural smoke check, not behaviour evidence — the real page is driven by
 * `tests/test_voice_browser.py`.
 */

import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STATIC = join(ROOT, 'aivoicebench', 'static');
const asJson = process.argv.includes('--json');

/** Every id `index.html` provides; the scripts look these up at load time. */
const ELEMENT_IDS = [
  'notice', 'breadcrumb', 'nav-home', 'nav-import', 'nav-voice-test', 'nav-models',
  'version', 'home', 'import', 'voice-test', 'models', 'analysis',
  'runs', 'count',
  'file', 'file-label', 'file-size', 'drop', 'fields', 'upload-form', 'submit', 'progress',
  'analysis-title', 'run-id', 'status', 'audio', 'summary', 'detail',
  'vt-fixed', 'vt-free', 'vt-notice', 'vt-phrases', 'vt-device', 'vt-preview',
  'vt-status', 'vt-start-fixed', 'vt-stop-fixed', 'vt-start-free', 'vt-stop-free',
  'vt-capture-status', 'vt-device-transcript', 'vt-free-log', 'vt-capability',
  'vt-free-goal', 'vt-free-constraints', 'vt-free-max-turns', 'vt-free-capture-mode',
  'settings-revision', 'model-hint', 'routes', 'model-list',
  'model-editor', 'editor-title', 'model-form', 'model-protocol', 'model-capabilities',
  'model-params', 'secret-state', 'clear-secret',
  'workbench', 'wb-status', 'wb-refresh', 'wb-gate', 'wb-player', 'wb-waveform',
  'wb-timeline', 'wb-minimap', 'wb-tracks', 'wb-evidence', 'wb-findings', 'wb-metrics',
  'wb-events', 'wb-turns', 'wb-transcript', 'wb-revision', 'wb-provenance',
  'wb-abstentions', 'wb-unavailable',
];

function makeElement(id) {
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
    appendChild(child) { element.children.push(child); return child; },
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    scrollIntoView() {},
    reset() {},
    play: () => Promise.resolve(),
    pause() {},
    load() {},
    currentTime: 0,
    paused: true,
  };
  return element;
}

function buildSandbox() {
  const elements = new Map(ELEMENT_IDS.map(id => [id, makeElement(id)]));
  const document = {
    getElementById: id => elements.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: tag => makeElement(`created-${tag}`),
    body: makeElement('body'),
  };
  const sandbox = {
    document,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
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
    URL,
    URLSearchParams,
    Uint8Array,
    Int16Array,
    Float32Array,
    Blob: class Blob { constructor(parts) { this.parts = parts; this.size = 0; } },
    FormData: class FormData {
      constructor() { this.entries = new Map(); }
      set(key, value) { this.entries.set(key, value); }
      get(key) { return this.entries.get(key) ?? null; }
    },
    fetch: () => Promise.resolve({ ok: false, status: 503, json: async () => ({}), text: async () => '' }),
    navigator: { mediaDevices: { getUserMedia: async () => { throw new Error('no device in smoke check'); } } },
    location: { protocol: 'http:', host: '127.0.0.1:8000' },
    Audio: class Audio { constructor(url) { this.src = url; } play() { return Promise.resolve(); } pause() {} load() {} removeAttribute() {} },
    WebSocket: class WebSocket {
      static OPEN = 1;
      constructor(url) { this.url = url; this.readyState = 0; this.bufferedAmount = 0; }
      send() {}
      close() {}
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  return sandbox;
}

const errors = [];
const sandbox = vm.createContext(buildSandbox());
const loaded = [];

// The load order is the one `index.html` documents: the shared view helpers come
// first, then the model-settings view, then the control layer, then the Evidence
// Workbench renderer. Reading it from the page keeps the check honest if the markup
// ever changes.
const page = readFileSync(join(STATIC, 'index.html'), 'utf8');
const scriptOrder = [...page.matchAll(/<script src="\/static\/([^"]+)"><\/script>/g)].map(match => match[1]);
const expectedOrder = ['app.js', 'models.js', 'voice_test.js', 'workbench.js'];
if (JSON.stringify(scriptOrder) !== JSON.stringify(expectedOrder)) {
  errors.push(`index.html loads ${scriptOrder.join(', ')} instead of ${expectedOrder.join(', ')}`);
}

for (const name of scriptOrder.length ? scriptOrder : expectedOrder) {
  const source = readFileSync(join(STATIC, name), 'utf8');
  try {
    vm.runInContext(source, sandbox, { filename: name });
    loaded.push(name);
  } catch (error) {
    errors.push(`${name} failed to evaluate as a classic script: ${error.message}`);
  }
}

// The page's inline `onclick` handlers and the test hooks resolve these by name.
const expectedGlobals = ['$', 'esc', 'badge', 'notify', 'request', 'view', 'home',
                         'importView', 'voiceTestView', 'vtTab', 'openRun', 'render', 'tab',
                         'modelsView', 'editModel', 'saveRoutes', 'VT', 'WB'];
for (const name of expectedGlobals) {
  let present = false;
  try {
    present = vm.runInContext(`typeof ${name} !== 'undefined' && ${name} !== null`, sandbox);
  } catch (error) {
    errors.push(`global ${name} is not reachable: ${error.message}`);
    continue;
  }
  if (!present) errors.push(`global ${name} is missing after loading the compiled scripts`);
}

// `window.VT` is the published control layer; the browser tests call into it.
try {
  const surface = vm.runInContext('Object.keys(window.VT || {}).sort().join(",")', sandbox);
  const expected = ['checkCapability', 'controlState', 'createFixedSession', 'integrityState',
                    'startFixedTest', 'startFreeTest', 'stopFixedTest', 'stopFreeTest', 'stopMic'];
  const missing = expected.filter(key => !surface.split(',').includes(key));
  if (missing.length) errors.push(`window.VT is missing: ${missing.join(', ')}`);
  // A run that never started must report "no active run", not a fabricated state.
  const idle = vm.runInContext('window.VT.controlState()', sandbox);
  if (idle !== null) errors.push('controlState() must be null before a run starts');
} catch (error) {
  errors.push(`window.VT is not usable: ${error.message}`);
}

// `window.WB` is the published Evidence Workbench surface. It must exist as a live
// object and must report "nothing loaded" before a Run is mounted, so a missing
// backend document can never be rendered as a fabricated workbench.
try {
  const surface = vm.runInContext('Object.keys(window.WB || {}).sort().join(",")', sandbox);
  const expected = ['eventRows', 'findingRows', 'load', 'metricRows', 'mount', 'playerState',
                    'refresh', 'regionsFromDocument', 'selectEvidence', 'state',
                    'transcriptRows', 'turnRows', 'unmount'];
  const missing = expected.filter(key => !surface.split(',').includes(key));
  if (missing.length) errors.push(`window.WB is missing: ${missing.join(', ')}`);
  const idle = vm.runInContext('window.WB.state()', sandbox);
  if (idle !== null) errors.push('WB.state() must be null before a workbench document is loaded');
  const unknown = vm.runInContext('window.WB.selectEvidence("does-not-exist") === null', sandbox);
  if (unknown !== true) errors.push('WB.selectEvidence() must return null for an unknown region id');
} catch (error) {
  errors.push(`window.WB is not usable: ${error.message}`);
}

const ok = errors.length === 0;
if (asJson) {
  process.stdout.write(JSON.stringify({ ok, loaded, errors }, null, 2) + '\n');
} else {
  for (const line of loaded) process.stdout.write(`evaluated ${line}\n`);
  for (const line of errors) process.stdout.write(`ERROR ${line}\n`);
  process.stdout.write(ok
    ? 'Browser Station scripts share one global scope and publish the expected surface.\n'
    : 'Browser Station script scope check FAILED.\n');
}
if (!ok) process.exitCode = 1;