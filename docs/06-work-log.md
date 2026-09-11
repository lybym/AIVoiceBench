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

## 2026-09-10 — v0.1.3 release verification

- Resumed after a workspace-credit approval interruption; no release failure was inferred from that interruption.
- Confirmed both Contract validation runs 34363937460/34363920358 succeeded for e3c2821a417a1aeea90a7c029290b6f814bf747b. Docker registry workflow 34363996503 and Release workflow 34363991525 also succeeded.
- Container smoke passed version 0.1.3, settings write/redaction, WAV/MP3/M4A synthetic imports, history/detail status and audio responses. This remains synthetic software validation, not real device accuracy acceptance.
- Release v0.1.3 pins that exact source commit. Asset aivoicebench-v0.1.3.tar.gz: 320066940 bytes; SHA256 8107c67b7c1261e09640edbfe909a6f11cb552c42e4ad692be098fabd2c95c78.
- Updated published Release notes with the refreshed UI, model management, deployment instructions and explicit pending speech-adapter scope. PR #43 and #45 remain reviewable, without automatic merge.


## 2026-09-10 — M1 recording backbone (#22 / #30)

- User limited this milestone to PRD-F004/F005/F016: unified ImportRun, actual configured cloud ASR, native invocation evidence, timestamped Transcript and minimal Web visibility. No diarization/Turn/Event/Metric expansion, HIL, Compare or new dashboard work.
- Audited main 3f75d5a (PR #43/#45/#47 merged). Original checkout still contains rebase metadata, so created independent issue-22-recording-backbone worktree. Selectively ported #31 invocation primitives/tests and #32 signed-upload/hash-readback design; did not merge old branches or their obsolete orchestration.
- Verified the requested current Volcano product updates and navigated to current recording flash HTTP docs (2608628, updated 2026-09-09). Selected documented synchronous URL-based ASR with explicit private publication configuration; no historical base64 assumption. No actual user audio upload or credential-based API request occurred.
- Web now invokes one ImportRun ledger. ASR native output, audit and Transcript are cataloged; ModelSettings captures capability providers once, with diarization/TTS still unavailable. Failed ASR preserves the Run; explicit retries preserve previous AnalysisRevision and invocation attempts. Web displays transcript and reuses playback; legacy web-analysis is read-only compatibility.
- Transcript 1.1 adds honest cloud unknown model hash/word confidence and overlap support; legacy 1.0 validation remains strict. Existing deterministic/semantic modules are retained and not auto-run without their evidence. Reports and model snapshots are revision-local.
- Validation: full suite 302 tests passed at the initial M1 integration check; targeted recovery test added afterward (final result recorded below). JavaScript syntax, Python compilation and diff whitespace checks passed. Tests are synthetic transports/media, not real device/cloud acceptance.
- Local Docker engine is available but clean build failed downloading the Python base layer from Docker Hub (network EOF). Added an isolated GitHub container workflow for codecs, synthetic ASR and actual Docker restart/manifest hash checks; result pending PR execution. Target source version 0.2.0-alpha.1; not a release declaration. No automatic merge.

- Follow-up verification: 7 targeted M1 tests passed, including report interruption recovery without repeating ASR. Actual Windows browser showed the synthetic transcript at 0.10–0.60 s, unknown role and partial Run; clicking its timestamp changed the audio control to playing. GitHub built the image and passed three-codec API smoke; its first test stage lacked the TestClient-only httpx dependency. Added that dependency to the isolated test container (same as existing contract CI), not the release image; rerunning container/restart validation.

## 2026-09-10 — M1 final software verification (PR #48)

- Validated source commit 379f4b4896cd233db85a1dd5e76f21612eda186e. [Windows and Linux contract CI](https://github.com/lybym/AIVoiceBench/actions/runs/34445056898) passed all 303 tests on each platform.
- [Container backbone check](https://github.com/lybym/AIVoiceBench/actions/runs/34445056890) passed image build, actual HTTP WAV/MP3/M4A imports, seven synthetic ASR/recovery tests, and an actual Docker restart followed by history, Transcript, model settings, audio and artifact hash checks. The earlier missing test dependency is resolved. The local Docker Hub download failure remains a local environment limitation; successful container evidence comes from GitHub CI.
- Windows browser inspection also confirmed timestamped Transcript rendering and audio seek/playback for a synthetic Run. All media and ASR transport responses used here are synthetic; these checks do not establish live cloud recognition or real terminal accuracy.
- PRD-F004/F005/F016 implementation is available in PR #48, unmerged and unreleased. Issues #22/#30 remain open. Real M1 acceptance still requires an authorized 5–20 minute recording, configured Volcengine credentials and private signed audio publication. No private recording, generated personal report or secret was committed.
- This final update changes verification documentation only; it does not alter the tested code or expand M1 scope.

## 2026-09-11 — M1 metrics canonical contract closeout (PR #52 / Issue #25)

- User confirmed M1 critical path order: PR #52 metrics closeout → Real Diarization → Semantic Attribution → LLM Judge/Findings. This round closed the metric/event contract before real-record analysis, so no second temporary data structure enters the real-recording stage.
- **Root cause of the failed metrics stage**: `compute_timeline_metrics` returned `status='observed'`, but the `analysis-output` envelope schema only accepts `['pending','running','complete','partial','insufficient_evidence','failed']`. Envelope validation rejected it with `ValueError`. Fixed by mapping `observed → complete` and `insufficient_evidence-with-metrics → partial` so the metric documents are preserved.
- **Canonical MetricResult unified to 3.0.0** (`schemas/metric.schema.json`). `metrics.py` no longer emits a lightweight parallel dict; it produces canonical MetricResult directly. Added `prd_ref`, `policy`, `policy_version`, `turn_id`, `response_id`, `analysis_id`, `confidence_source`, `uncertainty_ms`; `case_id` and `confidence` are now nullable (imported runs have no Case; unknown confidence is null, never 0). `schema_version` accepts both 2.0.0 and 3.0.0 so legacy artifacts remain readable. New metric names added for PRD-M001..M010 while legacy names (`e2e_first_audio_latency_ms`, `semantic_response_latency_ms`, `false_endpoint`) are kept with an explicit alias mapping documented in `03-metric-definition.md`.
- **PRD-M004 Turn Gap corrected**: direction changed from `device_end → next_tester_start` to `tester_end → device_start` (same turn), and it is now **signed** — a negative value expresses overlap/barge-in and stays `observed`; it is never clamped or raised. The old formula is retained as `turn_gap_ms_legacy()`, which still rejects negative values. `policy='device_speech_start'` with `policy_version` is recorded on each metric so a future semantic-anchor policy cannot be silently aggregated together.
- **PRD-M005 Barge-in bound to old response identity**: the turn model gained `interrupted_response_id` and `interrupting_segment_ids`. `detect_events` now timestamps `interrupt_start` at the interrupting tester segment and binds it to the interrupted old `response_id`. The metric selector requires a `device_speech_end` carrying that same response_id; a later different response never satisfies it. If the old response already ended before the interruption, the result is `not_applicable`, not a negative stop latency. `formulas.barge_in_stop_latency_ms` again raises on `end < start`, so the selector cannot bypass the formula's validation.
- **PRD-M008 False Endpoint demoted to candidate**: renamed `false_endpoint_detected` → `false_endpoint_candidate` with `policy='candidate_only'`; confirmation requires tester-continuation + in-pause device response + the full observation window + semantic/manual verification. The confirmed form is never emitted by the deterministic layer.
- **PRD-M006/M007 keep abstaining**: `barge_in_new_intent_latency_ms` and `barge_in_success` (composite, all four components) remain `insufficient_evidence`; no time-sequence heuristic substitutes for semantic evidence.
- **Status distinction added**: `not_applicable` (structurally meaningless, e.g. orphan device turn with no tester utterance) is now distinct from `insufficient_evidence` (should be measurable but evidence is missing). Every metric is emitted rather than silently omitted, so denominators stay honest. `counts` now reports total/observed/pass/fail/not_applicable/insufficient_evidence instead of collapsing everything into one bucket.
- **Confidence dimensions separated**: `FusedSegment` and `Event` now distinguish `acoustic_boundary_confidence` (from acoustic segmentation), `speaker_cluster_confidence` (diarization), `role_attribution_confidence` (source attribution) and `semantic_event_confidence`. The previous event-level `confidence is None → 0.0` coercion was removed; Event and MetricResult `confidence` are nullable with an explicit `confidence_source`. Acoustic timing confidence is no longer overwritten by role/diarization confidence.
- **Docs**: `03-metric-definition.md` updated to definitions 2.0.0 / MetricResult 3.0.0, correcting the time base (External Recording uses `audio_relative_ms`; `run_monotonic_ms` is reserved for execution/Control Evidence), the Turn Gap signed policy, the False Endpoint candidate/confirmed split, and the confidence-dimension table.
- Validation: 206 targeted tests pass with zero failures (25 metrics contract + 7 explicit-attribution E2E + 15 diarization + 7 provenance + existing metric/engine/timeline/testcase/findings/fusion suites), including updated legacy tests for the intentional semantic changes. End-to-end `explicit attribution → fusion → turns → timeline → metrics` now reaches `metrics: complete`; the fixture yields `first_speech_latency_ms=860 ms` and `turn_gap_ms=860 ms` observed, with feedback/meaningful-response/new-intent/composite-barge-in correctly abstaining. Every emitted metric validates against the canonical schema; provenance traces `metrics → timeline → fused-segments → acoustic-segments → normalized_audio → original_recording`, with diarization and attribution as additional fusion parents.
- Real Diarization remains the next critical path. No real cloud diarization, real recording, or human-verified acceptance was performed; no automatic merge.

## 2026-09-11 — Real diarization slice: ASR-native speaker clustering (PRD-F006 / PRD-F016, Issues #24/#22)

- User fixed this round's scope: implement real diarization by reusing the configured ASR's native speaker information, delivering the product slice "real service adapter → ImportRun → reviewable speaker segments". Real Diarization comes before Semantic Attribution; the ordering was not reopened.
- **One recognition submission, two outputs.** `ASRNativeDiarizationProvider.diarize_from_transcript()` derives speaker clusters from the already-completed ASR native response. It performs no network I/O (`processor.config.cloud_call_performed=false`, `derivation=asr_native_speaker_labels`), so one paid recognition call yields both the Transcript and the speaker segments. `import_pipeline` now loads the transcript and its invocation evidence *before* diarization and passes them in; the diarization stage's parents are `[transcript, acoustic_segments, normalized_audio]`.
- **Evidence chain for clusters.** Each `SpeakerSegment` keeps `native_speaker_id` (the raw provider label), `timestamp_source='provider_utterance_estimate'`, `raw_message_index`, `raw_utterance_index`, and the ASR `invocation_id` + `native_response_sha256` in `scope`. Local speaker IDs are namespaced as `{recording_sha256[:12]}:speaker_N`, so `speaker_0` in one recording can never be merged with `speaker_0` in another. Cluster `confidence` stays `null`: recognition and timestamp confidence are never reused as clustering confidence.
- **No inference, no invented labels.** Missing labels produce `insufficient_evidence` with zero segments, not filled-in clusters. Partially labeled responses produce `partial`, with unlabeled utterances left as `{scope}:unknown`. Non-alternating and single/three-speaker orders are preserved exactly as reported; there is no sequential fill, no alternating heuristic, and no assumption of exactly two speakers.
- **Why not a request parameter.** The existing `utterances[].additions.speaker` read path was kept and no unverified speaker parameter was sent. `VolcengineASRProvider` still requests only `show_utterances: True` (library_version bumped to `1.1.0`). The official parameter table could not be extracted because the vendor documentation pages are JS-rendered, so whether a request flag is required to enable speaker output remains **unverified** and is recorded as `live_api_pending` rather than asserted.
- **`ModelSettings` routing.** `adapter_available` now treats a `volcengine_asr` profile as a diarization adapter, and `RunProviders.diarization` is populated with an ASR-native factory that reuses the configured ASR profile — no second endpoint and no second billing event. `readiness.diarization` therefore reports `configured` instead of `not_integrated`; the corresponding backbone assertion was updated for this intended product change.
- **Resume/cache path.** `_run_evidence_chain_resume` previously dropped `providers`, so a resumed Run could not rebuild clusters. It now forwards them, and speaker segments are rebuilt from the preserved native response with no re-upload and no second recognition. Verified by resuming a completed Run under a transport patched to raise on any request.
- **Fusion adaptation instead of blanket assignment.** `_fusion_with_speakers` no longer picks the largest overlap for the whole acoustic segment. New `fusion.apply_speakers()` records `speaker_candidates` with overlap durations and either (a) assigns the single overlapping cluster (`single_overlap`), (b) splits the acoustic segment at cluster boundaries into per-speaker sub-segments (`split_multiple_speakers`) while keeping the parent `acoustic_segment_id`/`asr_segment_id` and placing the ASR text on the longest sub-segment only so it cannot be counted twice, or (c) abstains when differently labeled clusters claim the same instant (`ambiguous_overlap`, `speaker_source='ambiguous'`, `speaker_id=null`) because that is a clustering contradiction, not a close call. A known cluster never implies a known role: without role evidence `speaker_role` stays `unknown`.
- **Stage states are no longer silently absent (split out into PR #52).** Three failures inherited from the M1 stack were found while validating this slice: stages that never ran stayed `pending` with no envelope, so an import could omit a stage entirely and a reader could not tell "did not run" from "not part of this Run". Because the defect is in the stage ledger rather than in the diarization feature, it was committed separately on `m1-metrics-contract` (PR #52) instead of being folded into this slice: every stage in `STAGE_KINDS` now publishes an envelope, stages without canonical audio are `insufficient_evidence`, stages gated on upstream speaker/acoustic evidence (`EVIDENCE_GATED_STAGES`) report `insufficient_evidence` rather than `pending` because by the end of an import they can no longer become available, `pending` is reserved for processors this milestone does not run (ASR without a provider, judge, findings), and unrun stages always carry `data=null`.
- **Schema changes are limited to this slice.** `fused-segments` gained `speaker_evidence`, `speaker_candidates`, `segment_origin` and `ambiguous` as a `speaker_source`; `speaker-segments` gained `scope` and per-segment native/raw references. A separate, non-blocking question was found and deliberately **not** changed here: `schemas/evidence.schema.json` requires a numeric `confidence`, while Event 2.0.0 and MetricResult 3.0.0 allow `null` plus an explicit confidence dimension. No current path emits a null evidence confidence (role-dependent event detection abstains before building evidence), so relaxing that schema is registered as follow-up work rather than folded into this slice.
- **User-facing entry.** The Analysis API response now exposes `speaker_segments`, `diarization_scope` and `attribution`; the web Analysis view shows a cluster count, the diarization stage state, the service-native labels and the invocation basis, and labels each transcript segment with its local cluster plus native label while keeping roles at "待确认"; the CLI `import` prints cluster/label/invocation summary lines and the existing per-stage status list.
- **Independent import needs no role input.** Explicit mapping is an optional human verification/correction path and Semantic Attribution is an optional automatic judgement, so neither is a precondition for importing. Asserted end to end: an upload with no profile, no mapping and no speaker list still produces Transcript + speaker segments + a report, keeps every stage out of `failed`, leaves all roles `unknown`, and keeps `turns`/`timeline`/`metrics` at `insufficient_evidence` rather than inventing a role.
- Validation: the diarization slice passes together with the two PR #52 commits — full suite **416 tests, zero failures** (`unittest discover`, FFmpeg/FFprobe codec tests enabled) on branch `m1-real-diarization`. New suites: `test_asr_diarization.py` (12), `test_fusion_speakers.py` (18), plus additions to `test_recording_backbone.py`. `test_metric_compatibility.py` (25 cases) rides with PR #52. Backbone tests prove `PUT/GET/POST` with exactly one POST while both Transcript and speaker segments are produced, that attribution still returns `unknown`, that no explicit mapping is required, and that resume rebuilds clusters with zero further requests.
- Verification level for this slice: **synthetic ✅ / software ✅ / live cloud API ❌ not attempted / real recording ❌ not attempted / human verified ❌ not attempted**. Success is bounded to "speaker clustering available"; the system still never claims tester/device roles were verified. Semantic Attribution remains the next step (M1.2 remainder).

## 2026-09-11 — PRD 1.1.1 baseline recalibration

- Owner instruction: recalibrate the PRD on the actual development branch before implementing, and stop mechanically executing an earlier prompt's technical plan. The governing baseline was re-read from this branch's full `docs/PRD.md` (1.1.0 at the time of reading), not from `main`, PR descriptions or chat summaries. Branch `m1-metrics-contract`, HEAD `b4b7263` at the start of this round; PRD 1.1.0.
- **Milestone-name collision removed.** Section 7's engineering split (`Backbone → Speaker Attribution → Turn/Event/Metrics → LLM/Findings → 人工修订/Web`) reused the M2～M5 numbers that section 8 assigns to product milestones (M2 Fixed Voice Test Runner, M3 Free Voice Test Agent, M4 execution↔analysis linkage, M5 Compare/Regression). Those engineering stages are now explicitly labelled **M1.1～M1.5, internal sub-stages of product M1**, with a table mapping each sub-stage to its PRD refs. Section 8 remains the only product schedule. The change keeps all historical content, does not lower any M1 acceptance condition, and does not re-defer Active Voice Test: the text now states that basic active voice testing (M2/M3, including F023 local playback and microphone observation) is **not** postponed by the P3 professional-HIL deferral.
- **Requirement vs implementation suggestion vs implementation status.** Section 1 gained an explicit rule: the PRD defines what the product needs and how it is accepted; architecture, schemas, interfaces and provider-reuse choices define how it is implemented, and concrete function names, field names, split order or call counts are implementation suggestions that cannot become product gates the PRD never set. A worked example is recorded (reusing one ASR native response satisfies the same requirement as a second dedicated service call; a specific field name is not an acceptance condition).
- **Stale status refreshed with branch evidence.** F006/F009/F016 rows, the M004/M008 metric rows, and the Issue #3/#25 rows had described pre-fix gaps as current. Each was updated to keep the original gap statement for the published v0.1.3 baseline and add a clearly labelled "本分支进展（未合并）" note with its code/test evidence. No status was upgraded to merged, released or real-recording-verified.
- PRD version raised to **1.1.1** with a changelog row recording the change and its source. This recalibration changes documentation only; it does not alter tested code or expand scope.

## 2026-09-11 — v0.2.0-alpha.1 预览发布（M1 现有能力收敛）

所有者本轮目标：暂停新增功能，把已完成能力交付为可下载、可安装、可实际操作的预览版。授权范围仅限本次发布（合并必要 PR、创建 Tag、发布 Pre-release、上传附件），不改变"以后所有 PR 可自动合并"的规则。

### 基线确认与 PR 集成

- 开工确认：分支 `m1-semantic-attribution`、HEAD `82fbf0c`、PRD **1.1.2**（`main` 上为 1.1.1）、`VERSION=0.2.0-alpha.1`、Tag 仅到 `v0.1.3`（`v0.2.0-alpha.1` 未被占用）。
- **按祖先关系而非 PR 编号集成。** `git merge-base --is-ancestor` 证明 `m1-real-diarization`（#53）已包含 `main` + #48 + #49 + #50 + #51 + #52 的全部提交；`git rev-list --count origin/main..<分支>` 对 #51/#52 均为 **0**，三点差异为空。因此把 #53 的 base 从 `m1-metrics-contract` 调整为 `main` 后合并（merge commit `36a98a1`），一次带入整条已验证链路，未重复 cherry-pick、未引入旧实现、未回退 PRD。
- #48/#49/#50 由 GitHub 自动标记 MERGED；#51/#52 因 base 指向中间分支而未被自动标记，已附"祖先关系 + 提交计数 + 三点差异"证据后关闭。#54（Semantic Attribution）**有意保留未合并**：本轮范围是录音分析预览，且在没有人工复核的情况下不应把语义角色推断当作正式测量展示。
- 在独立工作树 `_avb_release_verify`（新检出 `03f5583`，不使用主工作区）运行全量测试：**428 tests, 0 failures**。

### 发布范围冻结（只把真正接到入口的能力算作可用）

可用（无需云端）：三格式导入、原件/标准化资产与 Hash/provenance、阶段账本、Audio QA、能量 VAD、历史、详情四标签页、音频回放与片段跳转、报告（Markdown+JSON）、失败信息与显式重试、容器重启后持久化、模型配置管理。
需配置：云 ASR 真实调用与时间戳转写（API Key **加三个签名 URL 环境变量**）、说话人标签展示（依赖服务是否返回标签）。
实验性/未真实验证：云 ASR 与标签（接口约定待确认）、确定性指标（未与人工标注对照）、说话人聚类（≠角色）。
未实现：TTS/Golden Voice、Fixed Runner、Free Agent、Compare、专业 HIL、F023 本地播放与麦克风、F024 关联、Waveform、人工修订工作台、语义角色归属。

### 发布阻塞修复（集中在 `release/0.2.0-alpha.1` 一个工作单元）

- **`release.yml` 重复 `prerelease` 键**：原先同一 `with` 内先写动态表达式、末尾又写 `prerelease: false`，YAML 重复键使后者覆盖前者，任何版本都会被发成正式版。现只保留一处、由 tag 推导（`-` 即 pre-release），并显式 `make_latest: false`，保证预览版不夺走稳定版的 Latest 定位。
- **发布说明与真实范围不符**：旧 body 宣称"LLM 语义评估/Finding 生成/人工修正契约"等笼统能力。现改为 `body_path` 指向随版本发布的 `docs/releases/<version>.md`，内容为本次真实范围、配置条件、已知限制与验证状态。
- **版本/Tag/源码一致性校验前移**：工作流在构建前校验 `tag == 'v' + VERSION`、发布说明文件存在且标注为预览版；tag 与 `target_commitish` 绑定到显式 `ref` 解析出的 SHA。
- **附件可用性而非"构建成功"**：新增"保存镜像后删除再从 tar.gz 重新 load 并跑 smoke"步骤；容器 smoke 覆盖 `/health` 版本、三格式导入、历史/详情/音频、密钥不回显，并做重启后历史与哈希校验。
### 预览启动脚本的两个真实缺陷（公开发布前发现并修复，改用 0.2.0-alpha.2）

第一次构建（`v0.2.0-alpha.1`，tag 保留未移动）产出的 Draft Release 附件中，PowerShell 启动脚本在本机默认 shell 下不可用。两个缺陷都是在本机按"用户实际用法"运行**下载到的附件**时暴露的，而不是靠阅读代码：

1. **UTF-8 无 BOM 导致 Windows PowerShell 5.1 解析失败。** 脚本含中文，而 `powershell.exe`（5.1，Windows 默认）在无 BOM 时按 ANSI 解码，报 `Missing closing '}'`——3 个解析错误，脚本根本无法执行。PowerShell 7 能正确解析，因此只测 pwsh 会漏掉。
2. **`$ErrorActionPreference = 'Stop'` 与原生命令 stderr 冲突。** 即使解析通过，`docker info *> $null` 会在 5.1 下把 docker 的 stderr 警告升级为终止性 `NativeCommandError`，脚本在任何实际动作前就退出。此外用 `ValueFromRemainingArguments` 包装 docker 调用会与 `docker ps -a` 这类单横线标志冲突。

修复：脚本以 **UTF-8 BOM + CRLF** 写入；不再使用 `Stop` 偏好，改为对每次原生调用显式检查 `$LASTEXITCODE`；去掉包装函数。已验证：`powershell.exe` 5.1 解析 0 错误，且缺镜像包、缺镜像、`-SkipLoad` 三条失败路径都给出明确中文提示。

**版本处理：** `v0.2.0-alpha.1` 的 tag 与其 Draft Release 已存在，按"不覆盖已有 Release、不移动已有 Tag"的约束不复用该版本号；改用下一个未占用版本 **`v0.2.0-alpha.2`**，并同步版本文件（`aivoicebench/version.py`、Dockerfile label、compose、启动脚本、发布说明）。alpha.1 的 Draft Release 未公开发布，已删除；tag 保留不动。

以上两个缺陷已加入 `tests/test_release_packaging.py` 作为回归防护（BOM/CRLF 断言、5.1 解析断言、禁止 `Stop` 与 `ValueFromRemainingArguments` 断言）。

- **预览部署资产**：`docs/releases/docker-compose.preview.yml`（独立容器名 `aivoicebench-preview`、独立卷、仅绑定 `127.0.0.1`、预留三个签名 URL 变量）与 `docs/releases/start-aivoicebench-preview.ps1`（从发布镜像 `docker load`、版本一致性校验、端口/同名容器冲突明确提示、**不删除任何已有容器或数据卷**）。
- **Dockerfile 元数据不再过度声明**：`description` 与新增 `version` label 对齐真实范围。
- 新增 `scripts/check_release_workflow.py` 与 `tests/test_release_packaging.py`（13 项），后者已证明能捕获原始的重复 `prerelease` 缺陷。

### 发布前验收（在候选提交上执行）

| 项目 | 结果 | 位置 |
| --- | --- | --- |
| 项目全量测试 | ✅ 441 tests, 0 failures（含 FFmpeg 三格式） | 本地 + CI（Linux/Windows contracts） |
| PR 集成候选全量测试 | ✅ 428 tests, 0 failures | 独立工作树 `_avb_release_verify` @ `03f5583` |
| 镜像构建 | ✅ | GitHub Actions（本地 Docker Hub 拉取 `python:3.12-slim` 持续 EOF，见下"环境限制"） |
| 从镜像启动 + `/health` | ✅ 版本 `0.2.0-alpha.1` | 发布工作流容器 smoke |
| WAV/MP3/M4A 导入 | ✅ 25/25 检查 | 本地 API 实例 + 隔离输出根目录 |
| 历史 / 详情 / 音频读取 / 报告 | ✅ | 同上 |
| 缺凭据时阶段状态明确、原件与 Run 不丢失 | ✅ ASR 阶段 `pending` 并给出原因 | 同上 |
| 云服务失败 | ✅ 10/10 检查：ASR 阶段 `failed`、原因保留、不伪造转写、原件保留、报告仍生成、失败 Run 仍是有效 Run、密钥不泄露 | 本地 API 实例（配置无效的合成发布地址） |
| 容器/服务重启后历史、配置、录音、证据 | ✅ 11/11 检查；确认是**新进程**（PID 变更）后重新查询，且注册资产 Hash 仍有效 | 本地 API 实例 |
| 预览环境不污染已有数据 | ✅ 验收使用独立输出根目录与独立命名空间；期间未创建或删除任何 Docker 容器/数据卷 | 同上 |

第一次执行"重启后"检查时，被验证的进程实际上仍是旧进程（新进程因端口占用未能绑定），该次结果**作废并重做**；上表结果来自确认 PID 变更后的重跑。

### 环境限制（不隐瞒）

本机 Docker 无法从 Docker Hub 拉取 `python:3.12-slim` 基础层（`production.cloudfront.docker.com` 持续 EOF），因此**镜像构建与容器内 smoke 在 GitHub Actions 上执行**；镜像产出后下载回本地，`docker load` 与容器启动/基础 smoke 再在本地复核。这不改变结论，但记录构建发生的位置。

### 验证边界

真实云调用、真实录音与人工标注对照**均未进行**；预览发布门槛与 PRD 第 7 节完整 M1 验收分别记录，本版不主张任何真实验收通过。

## 2026-09-11 — Issue #60 / v0.3.1 Volcengine TTS V3 SSE hotfix

- **问题与范围：** v0.3.0 的 `volcengine_tts` 仍向旧 v1 JSON 接口发送
  `Authorization: Bearer;…`，本机真实 API Key 验证失败。该修复只处理
  Active Voice Test 的云端 TTS 调用，不改变录音导入主链路、ASR 的签名 URL
  前置条件、Timeline/Metric/Evidence 基础设施或 HIL 排程（PRD-F016、F019、
  F020、F023、N004；Issue #60）。
- **实现：** TTS 改为 V3 单向 SSE，使用 `X-Api-Key`、
  `X-Api-Resource-Id`、`X-Api-Request-Id`；模型配置要求 resource ID、voice
  和 `wav`，不包含账户专属默认值。SSE 音频帧必须拼接为非空、可由 `wave`
  验证的 WAV 后才能作为播放资产；失败调用不写音频，并保存不含密钥或服务端
  原始错误的审计记录。审计保留 API version、公开配置、请求 ID、延迟、状态、
  输出 hash 与 WAV 元数据。
- **软件验证：** `tests.test_voice_test`、`tests.test_model_settings` 与
  `tests.test_release_packaging` 覆盖 V3 请求体/头、多帧 SSE、WAV 校验、失败
  脱敏审计、配置传递、版本化发布资产；实际结果在发布前复跑记录。
- **候选容器云端冒烟：** 本地独立镜像
  `aivoicebench:v0.3.1-candidate`、独立卷、`127.0.0.1:10431`。在用户明确
  授权的 API Key 下，仅合成一条短句；`POST .../synthesize` 返回 `ready`，
  短语状态 `ready`，音频端点 HTTP 200 / `audio/wav`，153,220 bytes。该结果
  验证 V3 请求、SSE 合帧和浏览器音频服务，不构成真实扬声器、麦克风或 AI
  设备测试。
- **密钥清理证据：** 冒烟后先后执行 API 密钥删除与容器内 SQLite 计数检查；
  模型描述显示 `credential_configured=false`，`secrets` 表记录数为 `0`。没有
  把密钥、真实录音或生成音频提交到仓库。
- **待完成：** 发布包下载后的重复冒烟、GitHub Release CI、真实设备声学
  对话、Frozen Golden Asset/正式 Execution Evidence 仍分别待验收；不能由
  此次短句云调用替代。

## 2026-09-11 — Issue #62 / fixed voice generation progress

- **问题与范围：** 用户点击固定用例的“生成语音”后没有持续进度提示，且旧的
  合成响应不含 `session_id`，可能让后续试听 URL 缺少会话标识。本修复只覆盖
  主动测试 TTS 资产生成的可见状态（PRD-F014、F016、F020、N004）；不改变
  导入录音主链路、Evidence/Timeline/Metric 契约或真实设备验收范围。
- **实现：** 合成开始将会话标为 `generating`，同一会话拒绝重复合成；成功时返回
  完整会话快照，失败时标记 `failed`。浏览器立即禁用提交和输入，建立 aria-live
  状态区域，按会话接口轮询已 `ready` 的短句数量及实际等待时间；完成、失败和
  网络异常都会恢复操作。预览继续使用完整会话 ID，且生成后显示预览面板。
- **软件验证：** 一次性 Docker 测试容器安装明确的 `httpx` 开发依赖后，
  `python -m unittest tests.test_voice_test tests.test_web_release -v`：**22 tests,
  0 failures**（含 WAV/MP3/M4A 合成导入 fixture）；随后完整 `unittest discover`
  通过。发布资产契约 **17 tests, 0 failures（1 个仅 Linux 环境跳过）**。未使用云端
  凭据、未产生真实录音或设备测试结论。
- **发布与附件级复测（HTTP/容器面，非浏览器面）：** PR #63 已合并至 `8d01ef2`。
  全量单元测试由 **Contract validation workflow `34584755849`** 执行：Ubuntu 与
  Windows 各 **463 tests 通过**。Release workflow `34584800872` **不执行单元测试**，
  只完成构建、合成容器冒烟、镜像保存/重载及附件发布。公开 `v0.3.2` Docker 包
  SHA256 为
  `dd3cf9d4cf86c1ce609f3bf5111d884d96fd3367270a57a13a6279c8eca2a447`；本次整理
  另行核对 Release digest、`SHA256SUMS.txt` 与下载附件三者一致。
- **10434 隔离容器记录（历史，本次未复跑）：** 从下载附件加载的隔离容器在
  `127.0.0.1:10434` 验证 `/health` 版本 `0.3.2`、进度静态资源可用；未配置 TTS 时
  合成返回 502，查询会话为 `failed`，没有遗留“生成中”状态。该次执行是本机一次性
  记录；本次整理时本机 Docker daemon 未运行，**未复跑该容器**，故不声称已独立复核
  该次结果，也不把环境不可用写成测试通过或测试失败。本次改在源码/制品层另行核对：
  镜像归档 `aivoicebench:v0.3.2` 的 `version` label 与镜像内 `aivoicebench/version.py`
  均为 `0.3.2`，`/health` 返回版本 `0.3.2`，`/static/voice_test.js` 可取得且包含
  进度逻辑，未配置 TTS 时合成返回 502、会话为 `failed`。以上仍属 HTTP/容器面检查。
- **待完成：** 发布镜像的**浏览器操作冒烟**——在浏览器中实际执行生成语音、播放、
  开始测试与停止——仍未完成；容器 `/health`、静态资源可取与 502 检查**不能替代**
  浏览器操作验证。未使用云端凭据或真实录音；真实 TTS/设备验证仍不由本次 UI 进度
  修复替代。
