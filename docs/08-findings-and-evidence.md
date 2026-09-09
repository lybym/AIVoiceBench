# Findings, evidence and regression workflow

Finding schema 2.0.0 links the first-class Evidence 1.0.0 catalog in a valid Timeline. A finding is either a defect or an observation. A normal observation has null severity and cannot become a regression defect candidate. The seed passing VAD finding was migrated to an explicitly synthetic observation, rather than counting a pass as a P3 defect.

## Severity guidance

| Level | Impact guidance |
| --- | --- |
| P0 | Critical safety/privacy harm or complete loss of core operation with no workable recovery. Immediate human assessment before confirmation/qualification. |
| P1 | Major safety or core-function failure with substantial impact; limited workaround. Severe safety needs human review. |
| P2 | Reproducible functional/interaction regression with bounded impact; normal prioritization. |
| P3 | Minor quality/usability defect with low impact. Normal observations do not receive P3. |

Severity describes observed impact, not confidence or internal cause. This guidance is not an automatic release gate; product-specific gate policy remains configured/versioned. Safety findings classified P0/P1 require approved human review before confirmed/fixed status. Review identity and timezone-qualified timestamp are explicit; the program does not fabricate a reviewer.

## Observation confidence vs cause confidence

`confidence` concerns the observed phenomenon; `attribution_confidence` concerns its hypothesized cause. Neither defaults to certainty. `attribution_status` is independent of finding `status`:

- unknown: suspected_layers exactly [unknown], attribution_confidence = 0. Evidence may confirm a symptom without identifying a cause.
- suspected: named layers are hypotheses, and requires_log_verification must be true. A black-box early response can suggest Endpoint but cannot prove an internal endpoint algorithm bug.
- verified: requires_log_verification false, supporting device-log evidence and approved human review. Logs must actually substantiate the cause; structural validation cannot assess their truth.

Closed taxonomy: wake_word, vad, endpoint, asr, aec, network, llm, prompt, context, memory, agent, tool, tts, safety, persona, unknown. A verified root cause cannot be unknown. Do not promote hypotheses merely because the symptom is confirmed or repeatable.

## Evidence and relationships

Every finding, including an observation, references at least one Evidence object with an artifact, track (nullable for nonaudio), run-relative start/end, source and confidence. Confirmed/fixed defects additionally require a nonzero-duration snippet. Evidence IDs, event IDs, metric IDs and run/case/execution_kind must resolve consistently. A finding's linked event evidence must be included among its Evidence IDs. Linked MetricResults are validated with the same Timeline. Missing recording/event belongs in Timeline gaps or an insufficient-evidence metric, not an invented confirmed defect.

Snippet intervals locate original artifacts; optional transcript_excerpt supplements them. A snippet is not a replacement for the original artifact or its hash. The runtime must verify existence/hash, path containment and access controls before opening evidence. Synthetic examples retain execution_kind=synthetic and placeholder assets; they are not physical recordings or accepted regressions.

## Regression candidate lifecycle

Exploration -> reproduce/confirm observed defect -> mark candidate -> minimize -> freeze/version Case and Golden Set -> run real regression. Only confirmed/fixed defects can have regression_case_candidate=true. The regression object records state, minimal reproduction and linked Case/Golden identities. Candidate/minimized objects may leave not-yet-created identities null; frozen requires all identities/versions and a valid linked non-exploratory TestCase whose Golden Set agrees. Runtime additionally verifies the frozen WAV manifest/QA and real reproduction; a schema-valid frozen fixture is not proof that assets exist.

Do not change the stable Case ID when editing a reproducer; increment Case version and independently version the Golden Set. Use separate prompt/interruption assets and stateful triggers for Barge-in/AEC. A confirmed exploratory failure never silently overwrites the current Golden Set. Human review for severe safety remains required throughout promotion.

## Validation examples

```powershell
python -m aivoicebench validate examples/finding.example.json --kind finding --timeline examples/timeline.example.json --metrics examples/metrics.example.json
python -m aivoicebench validate examples/findings/exploratory-defect.json --kind finding --timeline examples/findings/defect-timeline.json
```

Use `--regression-case <case.json>` for a frozen candidate. Pass/fail here means contract validation only. Malformed citations, unsupported attribution, missing review metadata and unconfirmed regression promotion are rejected.
