# Continuous work log

## 2026-09-06 — intake and Issue #1

- Verified user-pulled checkout at `12 CODE/AIVoiceBench`: clean `main`, commit `47a1865`, remote `git@github.com:lybym/AIVoiceBench.git`. GitHub connector confirms private repository and push/admin permission. Issue #1 is open and has no prior PR implementation. No CONTRIBUTING or test-methodology document existed; added both.
- Persisted complete handoff scope plus Windows executable/installer delivery and continuous GitHub synchronization requirements in `05-project-context.md`; aligned charter/architecture/roadmap.
- Branch: `issue-1-testcase-contract`. TestCase 2.0.0 introduces strict four-mode definitions, reusable frozen-audio/TTS/sample-silence segments, bounded interactive triggers, manifest provenance, reusable assertions and stable identity/version rules. Added contract validator/CLI, six synthetic fixtures and Windows/Linux CI.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -v` — 26 tests passed. `.venv/Scripts/python.exe -m aivoicebench validate examples/test-case.example.yaml examples/test-cases/asr.json examples/test-cases/latency.json examples/test-cases/barge-in.json examples/test-cases/context.json examples/test-cases/exploratory.json` — all six contract-valid. Python 3.12, jsonschema 4.26.0, PyYAML 6.0.3 in project `.venv`.
- Initial Python app alias was unusable; used bundled Python to create project venv. Package download required approved network access. Initial clone network failed; user completed checkout with SSH origin.
- Hardware/provider scope: no device playback/recording, microphone calibration, ASR/TTS/Judge call, executable packaging or real HIL run performed. Fixture manifests contain declared placeholders, not audio-ready assets.
- Next: commit/push/create PR for #1, then dependent #2 Timeline, #3 Metrics, #4 Finding/Evidence before runner #5. Record synchronization outcome below.

### Issue #1 synchronization

- Commit `07e19be9050fc6c5391cacad7ad80c0bb3f9151d` pushed successfully to `origin/issue-1-testcase-contract` after approved SSH network access.
- PR https://github.com/lybym/AIVoiceBench/pull/12 — open, not merged. GitHub Contract validation workflow run `34027283318` completed successfully.

## 2026-09-06 — Issue #2 Event Timeline

- Branch `issue-2-event-timeline`, based on Issue #1 branch; planned PR base `issue-1-testcase-contract`. No PR merged.
- Added EventTimeline 2.0.0 wrapper and first-class Evidence 1.0.0; upgraded Event to 2.0.0. Preserved run snapshots, track role/clock/sync uncertainty, source/confidence, old/new response association, explicit partial/blocked gaps and artifact references. Extended offline CLI with `--kind timeline` and cross-object validation.
- Updated the VAD synthetic fixture to include both sides of the exact 800 ms pause, added a full old/new-response barge-in fixture and empty blocked capture fixture. These are synthetic illustrations; artifact hashes/paths are placeholders.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -v` — 48 tests passed. `.venv/Scripts/python.exe -m aivoicebench validate --kind timeline examples/timeline.example.json examples/timelines/barge-in.json examples/timelines/blocked.json` — all three passed contract validation.
- Hardware/provider limitations unchanged. Next: synchronize Issue #2 PR, then #3 formulas/MetricResult and #4 Finding integration.

### Issue #2 synchronization

- Commit `2476a0479f96487d92f4a3bdeed3108ca748ee37` pushed to `origin/issue-2-event-timeline` after approved SSH access.
- PR https://github.com/lybym/AIVoiceBench/pull/13 — open, not merged, base Issue #1 branch / PR #12.

## 2026-09-06 — Issue #3 Metrics and formulas

- Branch `issue-3-metric-contract` based on Issue #2. MetricResult 2.0.0 adds status/value/threshold consistency, execution provenance, reference links, count accounting and scope/method constraints. Device ASR CER and internal timings require device-log Evidence; uncalibrated cross-clock timings cannot be treated as observed.
- Formalized Phase 1 metrics, semantic Judge requirements, white-box reservations, R7 percentiles, eligible rates, micro CER and missing/ineligible handling in `03-metric-definition.md`. Added deterministic reference formulas for latency, overlap unions, CER, rates and barge-in components. These arithmetic functions do not perform hardware measurement or replace #8 event selection.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -q` and metric CLI on the observed synthetic/blocked examples; final test count and sync result appended after completion.
- Hardware/online adapters/packaging untested. Next: finalize #3 PR, then #4 Finding/Evidence lifecycle and regression candidates.
- Final validation: 69 tests passed; both synthetic observed and blocked MetricResult examples validate with their respective Timelines. A trailing blank-line warning from `git diff --check` was corrected before commit.

### Issue #3 synchronization

- Commit `8a716fff0cedd8b093074ad8bb5b304eb884b766` pushed successfully to `origin/issue-3-metric-contract`.
- PR https://github.com/lybym/AIVoiceBench/pull/14 — open, not merged, stacked on PR #13.

## 2026-09-06 — Issue #4 Finding and Evidence

- Branch `issue-4-finding-evidence` based on Issue #3. Finding 2.0.0 distinguishes normal observations from defects; separate observed/attribution confidence, unknown/suspected/verified causes, log verification, human safety review and regression candidate lifecycle. Evidence references resolve to timestamped snippets/artifacts/events; supplied metric references and frozen Case/Golden versions are checked.
- Added synthetic exploratory defect/Timeline and migrated the seed passing observation. No physical device defect is claimed. Added severity and regression guidance in `08-findings-and-evidence.md`.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -q` — 86 tests passed. Both finding CLI examples in `08-findings-and-evidence.md` validated. A negative test exposed optional date-time validation being skipped by jsonschema without an optional dependency; fixed with an explicit timezone/date parser and reran all tests.
- All P0 Issue #1–#4 implementations now have local schema/fixture/reference validation. PR review/merge is still outstanding; no PR was merged. Hardware, audio asset QA, provider calls, Windows packaging and real HIL remain untested.
- Next: sync #4 PR, inspect actual Issue #5 acceptance, build bounded local runner/dry-run without claiming hardware measurement.

### Issue #4 synchronization

- Commit `d2a9a3666aea1595efbff1e849116405478dba16` pushed successfully to `origin/issue-4-finding-evidence`.
- PR https://github.com/lybym/AIVoiceBench/pull/15 — open, not merged, stacked on PR #14. GitHub Contract validation run `34043917782` completed successfully.

## 2026-09-07 — Issue #5 local preparation runner

- Branch `issue-5-local-runner` based on Issue #4. Implemented `python -m aivoicebench run <case-or-suite>`, per-attempt Run directories, fixed-audio hash/format/QA checks and sample-precise composition, version/profile snapshots, BUILD_INFO and artifact manifests. Added RunManifest/TestSuite schemas and local usage instructions.
- Outputs use canonical dry-run Timeline and insufficient-evidence metrics, with no invented findings or captured events. Missing files, hash mismatch, unsupported modes and missing hardware station remain explicit blockers. Bounded in-memory preparation supports cases up to 10 minutes; streaming endurance work remains later.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -q` — 101 tests passed. Temporary synthetic PCM validates 12,800 exact silence samples, preserved sources, hashes, CLI exit 0/2, repeated-run isolation and error paths.
- Actual CLI: `.venv/Scripts/python.exe -m aivoicebench run examples/suite.example.json --dry-run --output artifacts/runs` produced five blocked Runs with normalized artifacts. Run IDs: `RUN-5ae92c01ffde4420abdb266016504086`, `RUN-f1abd8e1835e46999aa76c9910d650c7`, `RUN-b365bdb146fe486db221f65b75c20de0`, `RUN-6f8898e85b50458d868ea150eebc6a67`, `RUN-ee872901828b4337980be763a64dc3f7`. These are blocker demonstrations, not measured HIL results. Artifacts remain local/ignored.
- Blockers: example frozen WAVs are not built; #6 audio station and calibration not integrated; user device/audio routing details requested asynchronously. ASR/TTS/Judge providers and credentials not configured or invoked. Windows executable/installer not built.
- Issue #5 is a partial implementation pending physical fixed-audio execution through #6; do not close it or claim final end-to-end acceptance. Next: synchronize draft PR, implement #6 with software checks while awaiting hardware details, then integrate real capture when configured.

### Issue #5 synchronization

- Commit `689d8a6b09c81796ae13e10b680c40666fa2b7a3` pushed to `origin/issue-5-local-runner`.
- Draft PR https://github.com/lybym/AIVoiceBench/pull/16 — stacked on PR #15, not merged, explicitly partial physical-execution acceptance.

## 2026-09-07 — Issue #6 audio station

- User clarified: no target prepared yet; generic conversational terminal with built-in speaker/microphone. Persisted in project context. Continue independent software work without repeated hardware questions.
- Branch `issue-6-audio-station`, based on Issue #5. Optional audio dependencies, read-only enumeration, explicit mono/stereo duplex capture, bounded buffers, raw driver monotonic timestamps, hash/format metadata, partial-failure handling and loopback correlation implemented. No silent delay correction. Official API references and usage in `10-audio-station.md`.
- Installed NumPy 2.5.3, sounddevice 0.5.6 and cffi 2.1.1 in project venv only. `.venv/Scripts/python.exe -m unittest discover -s tests -q` — 110 tests passed, including software-only callback and known-delay/noise/polarity fixtures. No mock input is presented as a physical capture.
- Actual read-only enumeration returned 39 endpoint entries (duplicates across host APIs, not 39 physical devices), including Realtek microphone/speaker entries. Saved local output in ignored `artifacts/audio/devices.json`. Fixed redirected Windows CLI text/JSON to UTF-8.
- No playback, microphone stream, calibration probe, ASR/TTS/Judge call or target device test performed. #6 hardware acceptance and #5 Run/capture integration pending; keep draft. Digital stimulus reference is not acoustic capture.
- Next: sync #6 draft, implement timestamped ASR/provider boundaries and Run/capture/Timeline integration. Final Windows executable/installer remains outstanding.

### Issue #6 synchronization

- Commit `a4272e5fb3e63822e8379c3a9eaf3a9c9ddfcc05` pushed to `origin/issue-6-audio-station`.
- Draft PR https://github.com/lybym/AIVoiceBench/pull/17 — stacked on PR #16, not merged; explicitly records missing physical acceptance and Run integration.

## 2026-09-07 — Issue #7 timestamped ASR

- Branch `issue-7-timestamped-asr` based on Issue #6. Added provider Protocol/native-response audit boundary, optional Vosk adapter, Transcript 1.0.0, source/channel preservation, model/file fingerprints and shared timestamp/reference validation. External ASR remains distinct from device ASR truth and acoustic Timeline events.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -q` — 120 tests passed. Tests cover raw response retention, timestamp/recognition confidence separation, missing timings, invalid provider data, stereo selection, run association and failure audits.
- Downloaded official small Chinese model (about 42 MB) into ignored .cache/models; validated archive extraction containment. Installed Vosk 0.3.45 in project venv. License/source/hashes documented in `11-timestamped-asr.md`.
- Actual provider smoke: generated the fixed phrase “你好，这是语音测试。请告诉我，为什么天空是蓝色的。” to a 16 kHz PCM16 WAV through local Microsoft Huihui Desktop. Initial sandbox voice access failed; approved outside-sandbox file synthesis succeeded without playback/recording. Corrected CLI handling of empty/invalid WAV errors exposed by that attempt.
- Executed `.venv/Scripts/python.exe -m aivoicebench asr artifacts/asr-smoke/source-generated.wav --provider vosk --model-dir .cache/models/vosk-model-small-cn-0.22 --model-version 0.22 --source-role stimulus --output artifacts/asr-smoke/results` — COMPLETE. Output `ASR-698cedf924574e249a73025762abb35c`: 7130 ms source, one timestamped segment, expected words recognized; validated with `validate --kind transcript`. Source hash `8e66659b7f5a8558325827ad8a2d21880aec22d132ab921eb42858a7e7aee203`. This is generated-file ASR verification, not target hardware measurement.
- User permits Volcano API TTS; persisted preference. Read-only, allowlisted inspection of existing Golden project found V3 SSE configuration, resource seed-tts-2.0, voice zh_female_vv_uranus_bigtts and 16 kHz PCM target. These are reuse clues, not current API availability/acceptance proofs. No credential values were printed/copied. Current official API overview was checked; detailed V3 page redirected and requires follow-up before integration in #9.
- Remaining: target-recording/provider validation and Run-clock alignment; online adapters optional. Next: sync #7, deterministic event/metric engine #8, then current Volcano TTS Golden builder #9. Full Windows packaged application remains unfinished.

### Issue #7 synchronization

- Commit `21dee2ef0697d1a1448e440cab07af7e5f7f2672` pushed to `origin/issue-7-timestamped-asr`.
- Draft PR https://github.com/lybym/AIVoiceBench/pull/18 — stacked on #17, not merged. Generated-file provider path verified; actual target capture/clock integration pending.

## 2026-09-07 — Issue #8 deterministic event-consumer engine

- Branch `issue-8-metric-engine` based on #7. Implemented canonical event selection for E2E, false endpoint, old-response barge stop, overlap, eligible timeout and device-log CER. Added Case-version threshold policy, uncertainty handling, file/hash verification for real/imported evidence, compatible repeated-sample R7 aggregation and analysis CLI output snapshots.
- Validation: `.venv/Scripts/python.exe -m unittest discover -s tests -q` — 132 tests passed. Synthetic fixtures reproduce 1380 ms E2E and 200 ms barge stop; missing coverage, unknown synchronization, external ASR timing, duplicate aggregates and partial inputs do not create passed measurements.
- Actual CLI `python -m aivoicebench analyze examples/test-case.example.yaml --timeline examples/timeline.example.json` wrote ignored `artifacts/analysis/ANALYSIS-d891177f899143859f87bf72817cc73e` with execution_kind=synthetic. No new recording or real performance result.
- Raw-audio speech detector and live response/turn association not implemented yet; #8 remains partial. Existing ASR timing is deliberately not used as acoustic ground truth. Details in `12-deterministic-engine.md`.
- Next: sync #8 draft and implement #9 frozen Golden asset build with current Volcano API verification/configuration, preserving exact sample pauses and current scope constraints.

### Issue #8 synchronization

- Commit `167e5ccec51518f83f129949d37d428d29373ea9` pushed to `origin/issue-8-metric-engine`; draft PR https://github.com/lybym/AIVoiceBench/pull/19. No merge. CI run `34072446336` succeeded.

## 2026-09-07 — Import-first product migration (#20)

- User reprioritized the primary MVP to existing mixed recordings → automatic analysis/report. Retained all completed code and paused physical HIL/TTS work. The untested local TTS draft is preserved in stash `ad7f35350df8dba1afc0c3c858c40c90a4a6eac9` on its original branch; it is not part of the new implementation.
- Audited refreshed remote refs, all Issues #1–#11 and PR metadata/CI for #12–#19. All Issues open; all PRs unmerged and CI success. Main remains `47a1865`. Re-ran baseline: 132 tests passed. Audit/merge recommendation in `13-import-first-migration.md`.
- Created #20–#27 for migration, import, providers, acoustic segmentation, source/turn/event fusion, latency expansion, revisions and integrated acceptance. Updated #5–#11 titles and appended migration instructions while preserving original bodies. No Issue falsely closed as complete.
- Updated architecture, context, roadmap, methodology and README. Added fixed integration snapshot branch at `167e5cc`; new branches are siblings, not further serial stack members. No PR was merged or retargeted.
- Verified specified official Volcano product-update page through the browser and followed current recording-recognition API navigation (page updated 2026-09-04). Recorded transport/model/format caveats in the migration document. No provider call or recording upload.
- Confirmed local FFmpeg/FFprobe executables are available. Next: #21 recording ingestion, canonical conversion, provenance and isolated stage failure outputs. Real-recording/semantic MVP and Windows packaging remain future acceptance.


## 2026-09-09 — Evidence-safe baseline and Docker/browser delivery (#40)

- Audited main `4ee8594`: four regression failures, unsupported alternating speaker roles, and LLM failure/mock behavior prevented trustworthy MVP claims. Existing import/contracts/ASR/metric foundations and Audio Station are preserved.
- User explicitly replaced native Windows packaging with Docker backend + frontend accessed from a Windows browser. Updated active architecture, context, roadmap, methodology and configuration; historical log entries remain historical.
- Original checkout has an unfinished rebase. Created isolated sibling worktree `AIVoiceBench-issue40`, branch `issue-40-regression-docker`, from main without changing that rebase.
- Fixed acoustic CLI file/directory output, empty canonical audio handling, and list-shaped human revisions. Default fusion preserves unknown roles and abstains from role-dependent events; attributed downstream fixtures remain explicitly synthetic.
- Default semantic provider is unavailable, not mock. Provider errors and invalid/unreferenced decisions fail closed without exposing exception contents; unverified model timestamps cannot populate latency metrics. Mock remains available only through explicit test/CLI selection.
- Validation: full unittest discovery — 264 tests passed (30.568 s); whitespace diff check passed. Tests include provider failure, malformed JSON, invented timestamps, unknown-role abstention and revision preservation. No real provider call, user recording, or physical device test occurred.
- Docker client is present but Docker engine is unavailable (docker_engine named pipe missing). Image startup, persistent-volume recovery and browser acceptance remain unverified. Existing Docker service serves API and static frontend together; no Windows installer is required.
- Remaining P0: shared Web/CLI import orchestration (including MP3/M4A), audited ASR/diarization integration, canonical timeline/metric validation and correct event/latency semantics. Structured semantic anchoring, complete evidence playback/revision flows and labeled real-recording acceptance remain open. This patch restores a conservative baseline; it does not complete the MVP. No automatic merge.


## 2026-09-09 — Web release corrections and minimal workspace (#42)

- Based on main `19d3a07` after authorized #41 merge/v0.1.1 publication. Branch `fix-web-release-v012`; original checkout/rebase preserved.
- Unified health/OpenAPI version as 0.1.2; release workflow checks requested tag against source version. History and detail share report-first status resolution, including legacy silent Runs without a timeline file.
- Web WAV/MP3/M4A now reuse the existing immutable recording import/normalization pipeline, including originals, hashes, conversion metadata and parent references. Derived Web analysis uses a separate directory and never replaces registered import outputs. Failed conversion/analysis retains the Run; deep per-stage analysis integration remains future work.
- Added FFmpeg to Docker; redesigned browser UI with restrained green/neutral styling, responsive sidebar, upload form, status/history, audio playback, segment seek controls, metrics/findings tabs and report download. User-controlled text is escaped. Removed misleading no-findings assertion of acceptable metrics.
- Validation: 269 tests passed locally, including real codec conversion of synthetic silence (WAV/MP3/M4A), corrupt upload retention, legacy status and version equality. Browser inspection confirmed import layout, version, history partial status, detail player and report tab. No real user recording/cloud call/device test. Release workflow now smoke-tests built Docker image for those three formats, version/status and audio responses before publishing.
- User requested Docker Release delivery; planned v0.1.2 via GitHub Actions after checks. No Windows installer. Real mixed-recording accuracy and semantic attribution remain unverified.


## 2026-09-09 — Model management follow-up (#44)

- v0.1.2 Docker Release workflow 34362745578 succeeded, including actual container smoke for version, WAV/MP3/M4A, history/detail and audio. PR #43 checks succeeded; release tag pins d8bf909. PR remains unmerged under existing governance.
- User added model configuration management inspired by DeepSeek Harness. Read local provider-profile/configuration and redacted settings contracts. Implemented original Python model registry, per-capability routes, transactional optimistic revisions, write-only local keys/env references, safe validation and next-Run configuration capture.
- Browser model manager supports add/edit/remove, enable/disable, default routing, provider/model/endpoint and purpose-specific parameters. Existing result Judge consumes selected OpenAI-compatible profile. Speech provider configurations remain explicitly not_integrated until adapter work; no cloud endpoints/models are guessed and no provider request is made on save.
- Every new Web Run captures/registers a SHA256-backed secret-free configuration snapshot. SQLite stores local credentials under persistent output; Compose defaults to localhost. Added redaction, concurrency, route/parameter validation, env precedence and Run-freeze tests. Browser verified model creation and default selection without secrets or network calls.
- Final validation: full 278 tests passed in 36.680 s, including 9 model settings tests. v0.1.3 Docker release verification follows. No real recording or cloud/device validation.
- Branch feature-model-management is one bounded follow-up to #43, not a growing feature stack. Publish via explicit source ref; keep PRs available for user-authorized integration.
