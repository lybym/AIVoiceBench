# AIVoiceBench

A hardware-in-the-loop benchmark, regression, and exploratory testing platform for AI voice terminals.

## Mission

AIVoiceBench turns AI voice-device evaluation from ad-hoc conversations into a reproducible engineering workflow:

**External Recording -> Import -> Normalize -> ASR/Diarization -> Automatic Events/Turns -> Metrics/Semantic Evaluation -> Findings/Evidence -> Human Verification -> Report/Regression**

The platform is designed for AI toys, companion devices, speakers, cameras, and other conversational voice terminals.

## Core principles

1. **Evidence first** - every metric and defect must be traceable to audio, transcript, timeline events, or device logs.
2. **Deterministic where possible** - latency, overlap, CER, barge-in timing, and rates are computed by code rather than an LLM judge.
3. **LLM only where necessary** - intent, context, persona, emotion, hallucination, and safety are evaluated by structured judges.
4. **Reproducibility** - Golden Sets, environment profiles, device versions, prompts, models, and audio assets are versioned.
5. **Black-box vs white-box separation** - observable end-to-end metrics must not be mislabeled as internal VAD/ASR/LLM/TTS latency without logs.
6. **Exploration becomes regression** - confirmed bugs found by an exploratory voice agent should become minimal reproducible Golden Cases.

## Initial scope

Phase 0 defines the four data contracts that all later components depend on:

- `TestCase`
- `EventTimeline`
- `MetricResult`
- `Finding`

The highest-priority MVP imports an existing 5–20 minute WAV/MP3/M4A conversation recording and automatically produces a trustworthy, evidence-linked report. Existing TestCase, Timeline, Evidence, metrics, findings, Runner, ASR/Vosk and deterministic engine are retained. Audio Station/HIL becomes a later automation extension. See [migration and repository audit](docs/13-import-first-migration.md).

See `docs/` for architecture and roadmap, and `schemas/` for the canonical contracts.

## Docker and browser delivery

The final deliverable is a Docker-deployed backend and frontend, accessed through a Web UI from Windows browsers. A Windows executable or installer is not required. Supply Docker configuration, persistent storage, startup and browser usage instructions. Online ASR/TTS/LLM providers may be configured; local execution does not promise offline operation. A cloud service cluster is not required. The current CLI validates contracts and prepares local Run/audio artifacts with explicit measurement blockers; it is not yet a hardware runner or packaged application. See [local runner instructions](docs/09-local-runner.md).

The import-first milestone now also provides `python -m aivoicebench import recording.wav` (WAV/MP3/M4A), source preservation, canonical conversion/QA, optional Vosk ASR and recoverable stage-status reports. Automatic speaker/turn/event/semantic analysis is still pending. See [recording import instructions](docs/14-recording-import.md) and the [primary-workflow migration PR](https://github.com/lybym/AIVoiceBench/pull/28).

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for offline validation commands, [project context](docs/05-project-context.md) for the complete delivery agreement, [contract versions](docs/07-contract-versions.md) for migration and trigger semantics, and [work log](docs/06-work-log.md) for actual progress and untested dependencies.


### Web release v0.1.2

The browser workspace supports WAV, MP3 and M4A imports, persistent original and normalized evidence, consistent history/detail status, audio playback and Markdown report download. Docker includes FFmpeg. Health and OpenAPI read the same application version. Release publication checks the tag against that version and smoke-tests the built container with synthetic recordings in all three formats. Speaker attribution and semantic conclusions still abstain when evidence is unavailable; this is not real-device MVP acceptance.

### Model management v0.1.3

Use the browser Model Management page to register provider/model profiles and select defaults for result analysis or future speech capabilities. Existing compatible Chat Completions result analysis is wired; speech adapters are explicitly pending integration. Keys are write-only, configuration revisions prevent stale writes and each Web Run keeps a non-secret snapshot. See [model management](docs/16-model-management.md) for persistence, credential handling and deployment scope.
