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
