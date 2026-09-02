# Development Roadmap

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

At least 20-30 real cases can be run repeatedly and compared between two device/software versions.

## Phase 2 - Control Plane

Goal: persist and manage tests as a platform.

Suggested stack:

- FastAPI
- PostgreSQL
- React/Next.js
- local filesystem initially, S3/MinIO later

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
