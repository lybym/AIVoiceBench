/* AIVoiceBench — deterministic Browser Station build verification.
 *
 * Usage:
 *   node scripts/verify-web-build.mjs          # human readable
 *   node scripts/verify-web-build.mjs --json   # machine readable (used by tests)
 *
 * The Browser Station sources are TypeScript (`web/src/*.ts`) and the browser
 * JavaScript under `aivoicebench/static/` is their build output. Two things must
 * hold, and both are release blockers rather than style preferences:
 *
 * 1. **Typecheck.** `tsconfig.json` must compile with `strict` and emit no error.
 *    A non-zero exit here is what makes "typecheck is a gate" true.
 * 2. **Freshness.** The emitted JavaScript must be byte-identical to the file
 *    that is committed and shipped inside the Docker image. Without this check a
 *    source change that was never rebuilt would silently ship the old browser
 *    logic, and the TypeScript migration would decay back into two sources of
 *    truth.
 *
 * The check is deterministic: it uses the TypeScript compiler API with the
 * repository's own tsconfig, so it cannot drift from what `npm run build` does.
 * It fails loudly if TypeScript is not installed, because "the compiler is
 * missing" must never be reported as a pass.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CONFIG = join(ROOT, 'tsconfig.json');
const asJson = process.argv.includes('--json');

function emit(payload) {
  if (asJson) process.stdout.write(JSON.stringify(payload, null, 2) + '\n');
  else {
    for (const line of payload.log) process.stdout.write(line + '\n');
    if (!payload.ok) process.exitCode = 1;
  }
}

const require = createRequire(join(ROOT, 'package.json'));
let ts;
try {
  ts = require('typescript');
} catch (error) {
  emit({
    ok: false,
    reason: 'typescript_not_installed',
    log: [`TypeScript is not installed (${error.message}). Run \`npm ci\` (or \`npm install\`) first.`],
  });
  process.exit(1);
}

const configFile = ts.readConfigFile(CONFIG, path => readFileSync(path, 'utf8'));
if (configFile.error) {
  emit({
    ok: false,
    reason: 'tsconfig_unreadable',
    log: [ts.flattenDiagnosticMessageText(configFile.error.messageText, '\n')],
  });
  process.exit(1);
}

const parsed = ts.parseJsonConfigFileContent(configFile.config, ts.sys, ROOT, undefined, CONFIG);
const options = parsed.options;
const rootNames = parsed.fileNames;

const outDir = options.outDir ? resolve(ROOT, options.outDir) : null;
const rootDir = options.rootDir ? resolve(ROOT, options.rootDir) : null;
if (!outDir || !rootDir) {
  emit({
    ok: false,
    reason: 'tsconfig_missing_paths',
    log: ['tsconfig.json must set both "rootDir" and "outDir".'],
  });
  process.exit(1);
}

// A scratch output tree keeps the check non-destructive: the committed artifacts
// are only ever read, and the temporary emission is compared against them.
const scratch = join(ROOT, '.test-tmp', 'web-build-verify');
rmSync(scratch, { recursive: true, force: true });
mkdirSync(scratch, { recursive: true });

const program = ts.createProgram({ rootNames, options: { ...options, outDir: scratch, noEmitOnError: false } });
const emitResult = program.emit();
const diagnostics = ts.getPreEmitDiagnostics(program).concat(emitResult.diagnostics);

const formatDiagnostic = diagnostic => {
  const text = ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n');
  if (!diagnostic.file || diagnostic.start === undefined) return `error TS${diagnostic.code}: ${text}`;
  const { line, character } = ts.getLineAndCharacterOfPosition(diagnostic.file, diagnostic.start);
  return `${relative(ROOT, diagnostic.file.fileName)}:${line + 1}:${character + 1} - error TS${diagnostic.code}: ${text}`;
};

const typeErrors = diagnostics
  .filter(diagnostic => diagnostic.category === ts.DiagnosticCategory.Error)
  .map(formatDiagnostic);
const warnings = diagnostics
  .filter(diagnostic => diagnostic.category !== ts.DiagnosticCategory.Error)
  .map(formatDiagnostic);

/** Every emitted `.js` file, keyed by its path relative to the output root. */
function collectEmitted(directory) {
  const files = new Map();
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const absolute = join(directory, entry.name);
    if (entry.isDirectory()) {
      for (const [name, text] of collectEmitted(absolute)) files.set(name, text);
    } else if (entry.isFile() && entry.name.endsWith('.js')) {
      files.set(relative(scratch, absolute).split('\\').join('/'), readFileSync(absolute, 'utf8'));
    }
  }
  return files;
}

const emitted = collectEmitted(scratch);
const stale = [];
const missing = [];
for (const [name, text] of emitted) {
  const shippedPath = join(outDir, name);
  if (!existsSync(shippedPath)) {
    missing.push(`${relative(ROOT, shippedPath).split('\\').join('/')} (never built)`);
    continue;
  }
  const shipped = readFileSync(shippedPath, 'utf8').split('\r\n').join('\n');
  if (shipped !== text) stale.push(relative(ROOT, shippedPath).split('\\').join('/'));
}

// An artifact with no source would be an orphan: it would keep being served by
// FastAPI/Docker while nothing in the repository produces it any more.
const expectedNames = new Set(emitted.keys());
const orphans = [];
for (const entry of readdirSync(outDir, { withFileTypes: true })) {
  if (!entry.isFile() || !entry.name.endsWith('.js')) continue;
  if (!expectedNames.has(entry.name)) orphans.push(entry.name);
}

const staleSources = [];
const sourceDir = join(ROOT, 'web', 'src');
const expectedSources = new Set(rootNames.map(name => relative(sourceDir, name).split('\\').join('/')));
for (const entry of readdirSync(sourceDir, { withFileTypes: true })) {
  if (!entry.isFile() || !entry.name.endsWith('.ts')) continue;
  // Declaration files carry types only and must never emit JavaScript.
  if (entry.name.endsWith('.d.ts')) continue;
  if (!expectedSources.has(entry.name)) staleSources.push(entry.name);
}

const log = [];
for (const line of typeErrors) log.push(line);
for (const line of warnings) log.push(line);
if (stale.length) log.push(`Browser Station build output is out of date: ${stale.join(', ')}. Run \`npm run build\` and commit the result.`);
if (missing.length) log.push(`Browser Station build output is missing: ${missing.join(', ')}. Run \`npm run build\` and commit the result.`);
if (orphans.length) log.push(`Compiled JavaScript has no TypeScript source any more: ${orphans.join(', ')}. Remove the file or restore its source.`);
if (staleSources.length) log.push(`TypeScript source is not part of the build: ${staleSources.join(', ')}. Add it to tsconfig.json "include".`);

const ok = !typeErrors.length && !stale.length && !missing.length && !orphans.length && !staleSources.length;
if (ok) {
  log.push(`Browser Station typecheck passed (${rootNames.length} TypeScript files, strict mode).`);
  log.push(`Browser Station build output is up to date (${emitted.size} compiled files).`);
}

rmSync(scratch, { recursive: true, force: true });

emit({
  ok,
  reason: ok ? 'ok' : 'build_verification_failed',
  typecheck_errors: typeErrors,
  warnings,
  stale,
  missing,
  orphans,
  stale_sources: staleSources,
  compiled_files: [...emitted.keys()].sort(),
  log,
});
if (!ok) process.exitCode = 1;