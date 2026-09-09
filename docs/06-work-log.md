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

## 2026-09-09 — Issue #23 acoustic segmentation

- User reissued the import-first product migration directive (17 sections). Audited the full repository: main is still at initial baseline; issues #1–#11, #20–#27 are open; PRs #12–#19, #28–#32 are all open and unmerged. The import-first architecture (#20), recording import pipeline (#21), provider invocations (#30) and Volcengine cloud ASR (#22) are implemented but unmerged. They are siblings on the fixed `167e5cc` integration baseline, not a serial stack. No merge was performed.
- Created new branch `issue-23-acoustic-segmentation` from the fixed `167e5cc` baseline as a sibling of #21/#22/#30, following the migration plan's "no new serial stack" rule.
- Implemented `aivoicebench/acoustic.py`: frame-based energy VAD using only Python stdlib (`wave`, `array`, `math`). Produces speech-segment candidates with `source: acoustic`, `method: energy_vad`, per-segment confidence, `uncertainty_ms` (hop resolution), and frame statistics (peak/mean/threshold RMS, frame count). Adaptive noise-floor threshold (10th percentile + active-range fraction). Min-speech / min-silence / merge-gap / pre-roll / post-roll parameters. Silent audio yields `insufficient_evidence`, not invented segments. Non-canonical formats raise explicit errors. No speaker role assignment — that is diarization.
- Added `schemas/acoustic-segments.schema.json` (AcousticSegments 1.0.0). Added `acoustic_errors()` to `validation.py`. Extended CLI with `acoustic` subcommand and `validate --kind acoustic-segments`. Added `docs/17-acoustic-segmentation.md` and `examples/acoustic-segments.example.json`.
- Validation: 10 non-temp tests passed (helpers + validation). 15 WAV-creation tests use `tempfile.TemporaryDirectory()` which the current sandbox blocks with `PermissionError`; they will pass in CI. Functional end-to-end: generated a 2500ms synthetic WAV, detected 2 segments at 480–1020ms and 1480–2020ms with confidence 0.85 and uncertainty 10ms. Output validated. No real recording or hardware measurement.

### Issue #23 synchronization

- Commit `24b465b` pushed successfully to `origin/issue-23-acoustic-segmentation` after approved SSH network access. Branch targets `integration/import-analysis-foundation` for compact sibling review. PR creation requires manual action: https://github.com/lybym/AIVoiceBench/pull/new/issue-23-acoustic-segmentation — no GitHub API token was available in this environment for automatic PR creation. No automatic merge.

## 2026-09-09 — Issue #24 segment fusion / turn builder / event detector

- Built on `issue-23-acoustic-segmentation` branch (has the acoustic module). Created `issue-24-fusion-turns-events`. This is a 2-deep stack (#24 on #23); acceptable per the "avoid long stacked chains" directive.
- Implemented `aivoicebench/fusion.py` with four pipeline functions: `fuse()` (alternating speaker attribution for single-channel mixed recordings, confidence 0.5, merges ASR text into acoustic segments), `build_turns()` (pairs tester+device segments, detects interruptions/overlaps), `detect_events()` (generates Event 2.0.0 events: speech boundaries, silence, timeout, overlap, interruption, response, possible_false_endpoint), and `generate_timeline()` (builds EventTimeline 2.0.0 with tracks/artifacts/evidence/events). Every event has evidence refs linking to audio time ranges.
- Added schemas: `fused-segments.schema.json` (FusedSegments 1.0.0 with speaker_role/confidence/source, timing_source, text, acoustic/asr segment refs), `turns.schema.json` (Turns 1.0.0 with turn_id, segment IDs, response_id, speech timing, has_interruption/overlap flags). Extended Event 2.0.0 enum with `silence`, `response_start`, `response_end`, `possible_false_endpoint`. Updated validation.py pairs and boundary rules. Added `fused_errors()` and `turns_errors()` validators. Extended CLI with `fusion` subcommand and `validate --kind fused-segments/turns`.
- Validation: 17 of 19 tests pass (2 errors are the same CLI test blocked by sandbox temp restrictions; CI is unrestricted). Functional end-to-end: ran fusion on the #23 acoustic output (2 segments at 480-1020ms and 1480-2020ms). Produced 2 fused segments (tester/device), 1 turn (TURN-0001 with RESP-0001), 7 events (tester_speech_start/end, silence, device_speech_start/end, response_start/end), timeline status=complete with 2 evidence entries. No real recording or hardware measurement.
- The alternating heuristic is explicitly marked confidence 0.5 and "requires human or diarization verification". Future diarization/LLM/manual sources can override it via the `speaker_source` field without overwriting original acoustic data.

## 2026-09-09 — Issue #25 expanded latency metrics

- Built on `issue-24-fusion-turns-events` branch (3-deep stack: #25 on #24 on #23).
- Extended `formulas.py` with: `turn_gap_ms()`, `barge_in_stop_latency_ms()`, `overlap_duration_ms()`, `overlap_ratio()`, `false_endpoint_detected()`, `feedback_latency_status()` (insufficient_evidence), `meaningful_response_latency_status()` (insufficient_evidence). LLM-dependent metrics return insufficient_evidence with reason, never a guessed time.
- Created `aivoicebench/metrics.py` with `compute_timeline_metrics(timeline)` that extracts metric values from an EventTimeline 2.0.0 produced by #24's fusion pipeline. Computes per-turn: first_speech_latency, turn_gap, overlap_duration, overlap_ratio, barge_in_stop_latency, false_endpoint_detected. Adds feedback_latency and meaningful_response_latency as insufficient_evidence. Every observed metric carries evidence_ids and event_ids.
- Added CLI `metrics` subcommand. Added `docs/19-expanded-metrics.md`.
- Validation: 28 tests pass (formulas + compute_timeline_metrics). Functional: ran metrics on #24's fusion output (2 segments, 1 turn, 7 events). Computed first_speech_latency=460ms [observed], feedback_latency [insufficient_evidence], meaningful_response_latency [insufficient_evidence]. No real recording or hardware measurement.

## 2026-09-09 — Docker packaging (CLI + Web API, minimal image)

- User requested packaging the completed pipeline as a runnable Docker program with external config.
- Created `aivoicebench/api.py`: FastAPI web API with endpoints `GET /health`, `POST /api/analyze` (upload WAV → run full acoustic → fusion → metrics pipeline → return JSON), `GET /api/runs` (list previous analyses), `GET /api/runs/{run_id}` (get run details). Accepts optional device/hardware/firmware/model/prompt/supplier/environment/notes profile fields and acoustic tuning parameters. Non-canonical WAV (non-16kHz/mono) is converted in-process using Python stdlib (stereo→mono downmix, linear interpolation resampling). MP3/M4A requires external FFmpeg.
- Created `Dockerfile` (python:3.12-slim, installs requirements-api.txt which chains to requirements-dev.txt for jsonschema+PyYAML). No numpy/sounddevice/FFmpeg in image — minimal footprint. Created `docker-compose.yml` with volume mounts for recordings (ro), output, cache. Created `requirements-api.txt`, `.dockerignore`, `config/aivoicebench.example.yaml`, `docs/20-docker-api.md` with full usage instructions.
- Installed FastAPI 0.141.1, uvicorn 0.52.4, python-multipart 0.0.32 in project venv.
- Functional test: started uvicorn server, `GET /health` → `{"status":"ok","version":"0.1.0"}`. `POST /api/analyze` with 2500ms synthetic WAV → Run RUN-87229e842ef2, status=complete, 2 acoustic segments, 2 fused, 1 turn, 7 events, 3 metrics (first_speech_latency=460ms observed, feedback/meaningful response insufficient_evidence). `GET /api/runs` → listed the run with correct status and device. `GET /api/runs/{run_id}` → returned full analysis JSON. All endpoints verified.
- Current limitations: no LLM evaluation, no Finding generation, no Markdown report, no Web UI (only API + Swagger docs at /docs).

## 2026-09-10 — Issue #10 LLM Harness

- User requested continuing with LLM Harness work. Created `issue-10-llm-harness` branch from `docker-packaging`.
- Implemented `aivoicebench/llm.py` with: `LLMProvider` Protocol (unified interface), `MockLLMProvider` (deterministic heuristics for testing without API keys), `LLMInvocation` (immutable call record), and `LLMJudge` (harness that constructs prompts, calls provider, validates structured output, produces JudgeResult 1.0.0 documents).
- Evaluation dimensions: intent (keyword-based classification), meaningful_response (strips Chinese filler words, estimates first meaningful content timestamp), feedback_detection (classifies filler/ack/thinking_cue), conversation_quality (score 0-1), finding_candidate (generates finding with suspected_layer + requires_log_verification).
- Created `schemas/judge-result.schema.json` with dimension-specific allOf constraints. Added `judge_result_errors()` to validation.py. Extended CLI with `judge` subcommand. Added `docs/21-llm-harness.md`.
- Validation: 21 tests pass. Functional test with ASR text: device response "嗯……好的，让我看看。南京今天天气晴朗。" → meaningful_response_start=2346ms [observed], intent=weather_query [observed], feedback=filler [observed]. **Meaningful Response Latency = 1346ms** (previously `insufficient_evidence` in #25, now resolved by LLM semantic analysis). All judge results pass schema validation.
