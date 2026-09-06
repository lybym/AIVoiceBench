# Development Roadmap

Final acceptance: Windows executable or installer, verified local startup and complete test/analysis workflow with configuration and usage instructions. Local execution may call configured online providers; no cloud cluster is mandatory. Continuously push source, tests, schemas, build configuration and docs through Issue PRs without automatic merge. CLI MVP is an intermediate milestone. Detailed scope: `05-project-context.md`; actual progress: `06-work-log.md`.

## Phase 0 - Contracts and methodology

Goal: stabilize the vocabulary and data contracts before building UI or infrastructure.

Deliverables:

- TestCase schema
- Event schema
- MetricResult schema
- Finding schema
- initial metric definitions
- example objects
- project charter and architecture

Exit criteria:

A minimal VAD, latency, barge-in, ASR, and context test can be represented end-to-end without ad-hoc fields.

## Phase 1 - Local Runner MVP

Goal: one workstation can execute tests against one physical AI voice device.

Planned capabilities:

1. Load and validate TestCase definitions.
2. Play fixed WAV stimuli.
3. Record device/room audio.
4. Integrate timestamped ASR.
5. Detect speech events and build Event Timeline.
6. Compute deterministic metrics.
7. Invoke structured LLM judgement for semantic dimensions.
8. Produce JSON + Markdown reports with evidence references.
9. Support a small Golden Set covering VAD, ASR, latency, barge-in, and context.

Exit criteria:

A small versioned set covering VAD, ASR, latency, barge-in and context runs through the chain. Record actual hardware coverage and missing provider/hardware inputs; synthetic dry-run is separate. The historical 20-30 count is a planning reference, not an approved minimum. Two-version comparisons are verified when both versions are available.

## Phase 2 - Local application and Control Plane

Goal: persist and manage tests through a complete locally usable Windows program. Provide Case/run management, local configuration, reports/evidence browsing and documented startup. Choose and verify a Windows packaging approach, dependencies and external tools; produce an executable or installer and test local startup/full workflow. Keep central/Edge deployment optional.

Suggested stack:

- FastAPI
- PostgreSQL
- React/Next.js
- local filesystem initially, S3/MinIO later

These are expansion choices, not mandatory local services. Introduce dependencies only when the current stage requires them; a lightweight local persistence option should support the packaged product.

Core entities:

- Project
- DeviceModel / DeviceInstance
- FirmwareVersion / AIConfigVersion
- TestCase / TestCaseVersion
- TestSuite
- TestRun / TestRunCase
- Artifact / Transcript / Event
- MetricResult / JudgeResult
- Finding / Evidence
- Baseline / ReleaseGate

## Phase 3 - Remote Test Station

Goal: separate the physical lab executor from the central platform.

Capabilities:

- station registration and health
- WebSocket command channel
- asset download/upload
- audio playback and multi-track recording
- calibration
- interactive triggers such as barge-in
- environment metadata capture

## Phase 4 - Automated Regression

Goal: turn Golden Sets into release qualification.

Capabilities:

- Golden Set versioning
- baseline comparison
- P50/P90/P95/P99 trends
- supplier comparison
- release gates
- defect-to-regression-case workflow

## Phase 5 - Exploratory Voice Agent

Goal: use a realtime voice model as a fuzz/exploratory tester rather than as the benchmark itself.

Principles:

- external controller owns coverage, topic rotation, and state
- realtime voice model executes natural speech behavior
- confirmed failures are minimized into deterministic regression cases
- exploratory results never silently overwrite Golden Set results
