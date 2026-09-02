# Metric Definition v0.1

This document defines the first black-box metrics for the local runner MVP. Internal component timings must only be reported when device logs provide white-box evidence.

## 1. End-to-End First Audio Latency

**Definition**

`device_speech_start - tester_speech_end`

**Scope**: black-box

**Unit**: ms

**Recommended aggregation**: P50, P90, P95, P99, max

## 2. False Endpoint

A false endpoint occurs when device speech starts during a planned intra-utterance pause and before the tester resumes the intended utterance.

**Value**: boolean per case, rate across repetitions

**Scope**: black-box

## 3. Barge-in Success

A valid interruption succeeds when the device stops the old TTS within the configured limit and proceeds to the new user intent instead of continuing the old answer.

**Value**: boolean

## 4. Barge-in Stop Latency

`device_old_speech_end - interrupt_start`

Only valid when the interrupt event is confirmed and the device speech end belongs to the old response.

**Unit**: ms

**Scope**: black-box

## 5. Overlap Duration

Duration during which tester speech and device speech are simultaneously active.

Useful for turn-taking and barge-in analysis, but overlap alone is not automatically a defect.

## 6. ASR Character Error Rate

For test cases with a known textual ground truth:

`CER = (substitutions + deletions + insertions) / ground_truth_characters`

Report both aggregate CER and scenario-specific accuracy for numbers, dates, model names, English abbreviations, and self-correction cases.

## 7. Context Success

Evaluated against explicit case expectations. Prefer deterministic comparison where the expected fact is exact; otherwise use a structured LLM judge.

## 8. Instruction Following Success

Pass only when all mandatory constraints in the case are satisfied. For strict-format cases, deterministic validation takes precedence over LLM judgement.

## 9. Timeout Rate

`timeout_cases / eligible_cases`

A timeout threshold must be defined by the suite or case, not guessed after execution.

## Evidence rule

Every MetricResult must reference evidence artifacts and, where available, event IDs. If evidence is insufficient, status must be `insufficient_evidence` rather than fabricating a value.
