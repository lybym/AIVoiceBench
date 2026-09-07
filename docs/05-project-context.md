# Persistent project context and delivery agreement

Updated 2026-09-06. This document preserves the user-authorized scope for future sessions. Read alongside the charter, architecture, roadmap and work log. Historical examples are design references, never approved acceptance thresholds.

## Workspace, ownership and source

- Main ongoing task: build AIVoiceBench from zero for AI conversational terminals / AI toys using hardware-in-the-loop (HIL) tests.
- The user selected this Windows workstation, `C:\Users\lybym\OneDrive\WORK`, explicitly instead of cloud Work. Checkout: `12 CODE/AIVoiceBench`.
- Authorized private repository: https://github.com/lybym/AIVoiceBench. Implement, test, commit, push and create one PR per Issue. Do not merge PRs autonomously. Preserve unrelated and uncommitted user changes. Never add the whole WORK directory.
- Source conversation: “优化语音测试提示词”, `6a969f58-7dfc-83e8-a5f5-a5bd4d22f5a4`; handoff task `01a07551-298d-7242-8ac7-00ff4ab59b61`. Handoff scope plus the Windows delivery addendum are the current requirements; verify actual repository state rather than trusting historical completion reports.
- Potential reusable material: `12 CODE/AI_Toy_Golden_Set_Doubao`. Read-only inspection when needed; do not copy credentials or adopt its current audio spacing as acceptance criteria.

## Final product acceptance

Hardware clarification (2026-09-07): target hardware is not prepared yet. Its form is a voice-conversation terminal with built-in speaker and microphone. Continue generic/offline implementation; actual terminal testing and station routing/calibration remain pending. Do not assume a specific model or report device performance from computer endpoint enumeration.

Deliver a compiled/packaged Windows executable or installer that starts locally and completes Case management/execution, audio playback and recording, timestamped ASR, Timeline, deterministic metrics, structured Judge, Evidence, Findings, reports and regression. Supply local configuration, startup and user instructions. Code, documentation, isolated scripts or a cloud-only service are not final delivery.

Local execution does not mean fully offline. ASR, TTS and LLM providers may require configured online services and credentials; document those dependencies. Verify packaging dependencies, external binaries, Windows startup and an end-to-end run on the user's machine at the relevant phase. Record exactly which hardware and scenarios were actually tested, and which remain untested. Central Control Plane / Edge is the modular expansion architecture; cloud deployment, PostgreSQL/Redis clusters and separate servers must not be mandatory for local use.

Continuously synchronize source, schemas, tests, build/packaging configuration and usage docs to GitHub Issue branches/PRs. Keep secrets, private raw audio and generated reports out of Git. A local commit is not a successful push: log the commit, push result and PR URL separately.

## Complete evaluation loop

Test audio or agent -> conversation with physical toy -> full multitrack recording -> timestamped ASR -> Event Timeline -> deterministic metrics -> LLM Judge -> Evidence -> Finding -> report/regression. Support version qualification, supplier comparison and development regression. Every Run must snapshot device instance/model, hardware, firmware, model, Prompt, environment and test-asset versions.

The platform controls, captures, calculates and regresses. Models generate suitable exploratory stimuli and evaluate semantics. Rule Engine and Judge stay separate. Evidence is a first-class object with artifact, track, timestamp basis, source, confidence and resolvable references. Missing evidence yields insufficient evidence / blocked, never invented timing, failures, passes or internal causes. Subjective experience needs human sampling and serious safety issues need human review.

## Coverage and observable boundaries

Capability tree, metric tree and case tree are distinct and explicitly linked. Preserve the business/engineering mapping in `02-test-methodology.md`.

- Technical performance: Wake Word, VAD/Endpoint, ASR, AEC, Barge-in, TTS Cancel, response latency, TTS, network, endurance, recovery.
- Interaction: turn-taking, pace, interruptions, listening quality, far-field, continuous conversation.
- AI product: Intent, Context, Memory, Instruction Following, Knowledge/Reasoning, Hallucination, Persona, Emotion, Proactivity, Safety.
- Engineering: L1 acoustic/speech; L2 realtime interaction; L3 AI cognition; L4 Persona/UX/Safety; L5 Reliability.

Black-box E2E timings cannot be attributed to internal VAD/ASR/LLM/TTS without device logs. External ASR is not device ASR ground truth. Black-box AEC behavior is distinct from ERLE requiring appropriate reference/internal signals. Suspected causes must remain hypotheses, with confidence and a log-verification requirement.

Attribution taxonomy: Wake Word, VAD, Endpoint, ASR, AEC, Network, LLM, Prompt, Context, Memory, Agent, Tool, TTS, Safety, Persona, Unknown (serialized lower snake case).

## Golden and exploratory assets

- Regression uses fixed, versioned WAV / frozen TTS Golden Sets. ChatGPT / Doubao S2S are for exploration. Confirm exploratory bugs, minimize the reproducer, then add a versioned Golden Case.
- Recommended/initial supported audio: WAV, PCM signed 16-bit little-endian, 16 kHz, mono. Insert VAD pauses at sample precision, not through natural TTS pauses.
- Interactive Barge-in/AEC uses separate prompt and interruption assets, a controller state machine, explicit timeouts and timestamps, and an offset after device speech start. No pre-concatenated timing approximation.
- Doubao exploration preference is S2S-O; Golden material uses large-model TTS/fixed WAV. These are planning preferences only: verify official APIs and actually available models before calls; never permanently hard-code historical model names.
- Manifest: provider/model/voice/config/version/hash. Builder: cache, dry-run, single-case build, BUILD_INFO. Audio QA: format, duration, clipping, peak, RMS, DC offset, pause precision.
- Stable Case IDs; content edits increment Case version. Golden Set versioning is independent. No requirement to create hundreds of cases for the first release.

## Architecture and reporting

Orchestrator supports `fixed_audio`, `interactive`, `multi_turn`, `exploratory_agent`. From the first contracts support stimulus track, device-output track, optional room reference/mix, synchronization and calibration. Software dry-run is never a hardware measurement.

Modules: Event Timeline Engine, Rule Engine, LLM Judge, Result DB, Dashboard/Regression/Release Gate. Preferred later stack: Python/FastAPI, React/Next.js, PostgreSQL, local files then S3/MinIO, Redis + RQ/Celery as needed, Python/WebSocket Edge. Introduce dependencies only when required; choose a light local configuration for the packaged product.

Metric definitions include formula, units, start/end events, applicability, missing-value handling and aggregation. Latency P50/P90/P95/P99 must show sample count. Separate first audio from meaningful/semantic response latency. Barge-in separates stop latency, stop success and new-intent response. Chinese ASR CER can break down numbers, dates, proper nouns, mixed Chinese/English, models, corrections, far-field and noise; label observable scope.

Reports order: Release Gate -> KPI Dashboard -> composite score. Gate rules must be configurable, versioned and traceable. Do not invent an approved threshold or a composite score when no scoring policy exists. Report/Metric/Finding all link timestamped Evidence. Produce JSON and Markdown, with later baseline/version comparison.

## Sequencing and continuation

First read actual Issue acceptance criteria and verify existing work. P0 Issues #1 TestCase, #2 Timeline, #3 Metric/formulas, #4 Finding/Evidence precede #5 local runner, #6 playback/recording/calibration, #7 timestamped ASR, #8 timeline/metric engine, #9 Doubao TTS Golden Stimulus, #10 structured Judge, #11 evidence-linked reports. Separate Issue branches and PRs; dependent PRs may be stacked with explicit base/dependencies, without merging.

Keep full context here and append execution evidence to `06-work-log.md`: phase/Issue, artifacts, commits/PRs, actual commands/results, blockers, next action. When hardware, microphone, provider credentials or GitHub access is unavailable, record the exact dependency and continue independent contract/fixture/offline/dry-run work. Never call mock evidence real HIL. Continue toward the packaged local product after CLI MVP.
