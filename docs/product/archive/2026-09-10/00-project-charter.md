# Project Charter

## Problem

AI voice terminals are commonly evaluated through ad-hoc manual conversations. This makes supplier comparison, release acceptance, regression detection, and root-cause discussion subjective and difficult to reproduce.

## Goal

Build a hardware-in-the-loop testing platform that can execute repeatable voice tests against real devices, collect synchronized evidence, compute deterministic technical metrics, use structured LLM judging for semantic dimensions, and compare results across devices and versions.

Final delivery is a locally runnable Windows executable or installer covering the complete test/analysis workflow, local configuration and usage instructions. ASR/TTS/LLM can use configured online providers; do not equate local with offline. Verify startup and full workflow on this workstation, record real hardware coverage and untested items. Source-only or cloud-only delivery does not meet acceptance. Keep code/build configuration/tests/docs synchronized to private GitHub Issue branches and PRs without automatically merging. See `05-project-context.md` for the complete agreement.

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
