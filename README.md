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
