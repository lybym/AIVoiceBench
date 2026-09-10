# Local Runner 0.1.0 — preparation milestone

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Current behavior: validate Case/suite, verify frozen assets, prepare fixed audio, create isolated Run outputs and report missing measurement evidence. It does not yet play/record audio or call ASR/Judge providers. This is an intermediate CLI milestone toward the complete Windows executable/installer, not the final product.

## Windows startup

From the repository root in PowerShell, use the project Python environment (see CONTRIBUTING for initial setup):

```powershell
.\.venv\Scripts\python.exe -m aivoicebench --help
.\.venv\Scripts\python.exe -m aivoicebench run examples/test-case.example.yaml --dry-run
.\.venv\Scripts\python.exe -m aivoicebench run examples/suite.example.json --dry-run --output artifacts/runs
```

The shipped contract examples intentionally reference nonexistent synthetic asset manifests. They return BLOCKED with missing WAV paths. To prepare real frozen assets, supply a Case with the actual relative WAV paths/SHA-256/provenance and use `--asset-root <directory>` if those paths are not relative to the Case file. Do not edit placeholder hashes to look valid without building/verifying the corresponding assets.

Optional `--device-profile <json>` accepts only device, hardware, firmware, model, prompt and environment version labels (or null for unknown). `examples/device-profile.example.json` lists the keys. No credentials belong in this profile. Real hardware execution will require complete version identification and audio configuration.

Suite 1.0.0 has suite_id, suite_version and an ordered unique list of relative Case paths rooted at the suite directory. TestCase repeat creates separate numbered attempts. Each attempt gets a new UUID Run directory; the runner does not overwrite earlier runs. Invalid schema/reference structure is rejected before execution. Sources/symlinks cannot escape the configured asset root.

Exit codes: 0 = preparation succeeded with no requested hardware measurement; 1 = input/validation error; 2 = execution/preparation blocked. Omitting `--dry-run` currently requests hardware but returns BLOCKED because #6 is pending. No playback or microphone capture happens in this milestone. Output execution_kind remains dry_run.

## Run artifacts

- case.json: exact normalized Case snapshot, with hash recorded in Timeline and BUILD_INFO.
- stimulus.wav: fixed-audio segments plus sample-exact silence, only when all source assets are verified and composition is bounded.
- stimulus-plan.json: planned sample intervals; explicitly not observed acoustic events.
- audio-qa.json: actual format/duration/peak/RMS/DC/clipping/sample count and source hashes, or precise missing/mismatched asset reasons. Levels are measurements, not approved acceptance limits.
- timeline.json: canonical dry-run Timeline with empty observed events/Evidence and an explicit capture gap. Prepared audio is not treated as captured device evidence.
- metrics.json: canonical MetricResult array, values null and status insufficient_evidence.
- findings.json: empty, because no device behavior was observed.
- BUILD_INFO.json: runner/Python version, case hash and asset checks, playback_performed=false, recording_performed=false.
- manifest.json: canonical Run manifest with requested mode, actual execution_kind, status/blockers, device profile and hashes of every other output. It excludes its own recursive hash.

Output defaults to ignored artifacts/runs. Do not commit personal recordings/generated output. Preparation is bounded to 10-minute cases/combined source-audio memory; long-run reliability needs a streaming adapter. Fixed audio is implemented; interactive, multi_turn and exploratory_agent are explicitly blocked scaffolds until their controllers exist.

## Validation and remaining acceptance

The tests create temporary synthetic PCM files to verify the exact 12,800-sample pause, source preservation, hash checks, truncated/unsupported WAV handling, interval bounds, repeat isolation and canonical outputs. Test signals are not real speech or hardware measurement. Running the shipped five-case suite validates blocker handling and writes normalized outputs; it does not produce five hardware results.

Issue #5 remains incomplete for physical fixed-audio execution until #6 playback/capture is integrated. Next: device selection, common-clock recording and loopback calibration; then timestamped ASR (#7), event selection/metric engine (#8), Golden asset build (#9), Judge (#10), report/regression (#11), and Windows application packaging.
