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
