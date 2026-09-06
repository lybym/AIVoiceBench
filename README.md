# AIVoiceBench

A hardware-in-the-loop benchmark, regression, and exploratory testing platform for AI voice terminals.

## Mission

AIVoiceBench turns AI voice-device evaluation from ad-hoc conversations into a reproducible engineering workflow:

**TestCase -> Stimulus -> Recording -> Event Timeline -> Metrics/Judgement -> Evidence -> Finding -> Regression**

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

Phase 1 will build a local Python runner capable of executing a small Golden Set, recording audio, producing timestamped ASR, extracting events, computing deterministic metrics, invoking an LLM judge, and generating an evidence-linked report.

See `docs/` for architecture and roadmap, and `schemas/` for the canonical contracts.

## Local Windows delivery

The final deliverable is a Windows executable or installer for the full local testing and analysis workflow, with local setup and usage instructions. Online ASR/TTS/LLM providers may be configured; local execution does not promise offline operation. A cloud service cluster is not required. The current CLI validates contracts and prepares local Run/audio artifacts with explicit measurement blockers; it is not yet a hardware runner or packaged application. See [local runner instructions](docs/09-local-runner.md).

Start with [CONTRIBUTING.md](CONTRIBUTING.md) for offline validation commands, [project context](docs/05-project-context.md) for the complete delivery agreement, [contract versions](docs/07-contract-versions.md) for migration and trigger semantics, and [work log](docs/06-work-log.md) for actual progress and untested dependencies.
