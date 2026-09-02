# System Architecture

## Target architecture

```text
Web Control Plane
  Projects / Devices / Versions / Cases / Suites / Runs / Reports
                |
                v
          Test Orchestrator
                |
      +---------+----------+
      |         |          |
 Fixed Audio  Multi-turn  Exploratory Agent
      |         |          |
      +---------+----------+
                v
        Edge Test Station Agent
      Playback / Recording / Triggers
                |
                v
          Physical AI Device
                |
                v
          Multi-track Evidence
                |
                v
       Event Timeline Engine
                |
      +---------+----------+
      |                    |
Deterministic Metrics    ASR / Logs
      |                    |
      +---------+----------+
                v
         Evaluation Engine
      Rule Engine + LLM Judge
                |
                v
      Metrics / Findings / Evidence
                |
                v
 Dashboard / Regression / Release Gate
```

## Control Plane

Later phases will expose project, device, version, suite, run, metrics, findings, and report management through FastAPI + PostgreSQL with a React/Next.js UI.

## Edge Test Station

Runs next to the physical device. Responsibilities:

- download test definition/assets
- play deterministic stimulus audio
- record device response
- trigger interactive events such as barge-in relative to detected device speech
- capture timestamps and calibration metadata
- upload artifacts/results

## Evidence model

The platform should support at least:

- stimulus/source audio track
- device/room recording track
- optional room reference track
- timestamped ASR
- device logs when available

Single mixed recordings are supported as an MVP input but should be marked as lower-confidence evidence for attribution.

## Evaluation split

### Code-computed

Latency, overlap, stop latency, speech duration, CER/WER where ground truth exists, success/failure counts, timeout rate, percentiles.

### LLM-judged

Intent understanding, context, memory, reasoning quality, instruction following, persona, emotion, hallucination, and safety.

LLM results must be structured and evidence-linked; they are not allowed to fabricate precise technical timing.

## Core contracts

All major components exchange four canonical objects:

1. TestCase
2. Event
3. MetricResult
4. Finding

Schema evolution must be versioned and backward-compatible where practical.
