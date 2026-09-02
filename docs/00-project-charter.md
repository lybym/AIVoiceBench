# Project Charter

## Problem

AI voice terminals are commonly evaluated through ad-hoc manual conversations. This makes supplier comparison, release acceptance, regression detection, and root-cause discussion subjective and difficult to reproduce.

## Goal

Build a hardware-in-the-loop testing platform that can execute repeatable voice tests against real devices, collect synchronized evidence, compute deterministic technical metrics, use structured LLM judging for semantic dimensions, and compare results across devices and versions.

## Primary use cases

- Supplier benchmark comparison
- Firmware/model/prompt regression testing
- Release qualification
- Long-running stability testing
- Exploratory/red-team voice testing
- Defect reproduction and evidence sharing

## Testing layers

1. Acoustic and speech chain: wake word, VAD/endpoint, ASR, AEC, TTS, far-field/noise robustness.
2. Realtime interaction: turn-taking, barge-in, TTS cancel, response latency, overlap, conversation rhythm.
3. AI cognition: intent, context, memory, reasoning, knowledge, instruction following, hallucination, tool use.
4. Persona/UX/Safety: persona, naturalness, proactivity, emotion, empathy, child safety, privacy, medical and content safety.
5. Reliability: network recovery, reconnect, long-running stability, state recovery, performance drift.

## Non-goals for Phase 0/1

- Building a polished web UI before data contracts and runner behavior stabilize.
- Claiming internal component latency from black-box audio alone.
- Replacing deterministic metrics with LLM judgement.
- Fully automating physical SPL/distance calibration in the first MVP.

## Success criteria for MVP

A single workstation can run a small suite against one physical AI terminal and produce:

- source test case
- stimulus asset references
- synchronized recording
- timestamped transcript
- normalized event timeline
- deterministic metrics
- structured semantic judgement
- evidence-linked findings
- Markdown/JSON report artifacts
