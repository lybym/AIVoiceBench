# Metric definitions 2.0.0 (MetricResult schema 3.0.0)

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Definitions are versioned independently from schemas. No thresholds in this document are approved release criteria. Synthetic reference calculations validate arithmetic, not device performance.

## Time base

External Recording measurement uses **`audio_relative_ms`** — position inside the imported recording, origin at the first decoded sample. This is the correct basis for imported-recording latency, overlap, barge-in and event boundaries.

Active Measurement uses the same **`audio_relative_ms`** semantics, derived from the first sample of its own `ART-live-measurement-audio`. Its wall clock, client/server monotonic clocks, playback callbacks and WebSocket receive time are diagnostics or alignment priors, not alternate formula inputs.

`run_monotonic_ms` records Control Evidence: actual program/controller execution timing during an Active Voice Test (playback command time, invocation latency, controller state). It must never describe the acoustic position of a Measurement Event.

Do not subtract a playback log clock from a phone recording timestamp. Cross-device clocks require an explicit mapping with recorded uncertainty (PRD-F024).

## Shared rules

**Metric definitions are pipeline-invariant.** Active Measurement and Recording Analysis both feed Canonical EventTimeline into `compute_timeline_metrics(...)` and emit the same canonical names/PRD refs. Do not create `live_*` and `offline_*` variants or duplicate formulas. Pipeline, event producer, measurement policy, artifact IDs, confidence/uncertainty and finalization state travel as provenance. Given the same canonical event values and policy, metric values must be identical regardless of pipeline.

All measurement times use the owning audio artifact's `audio_relative_ms` basis with explicit event/turn/response association. Compare clocks only when their mapping is established and sufficiently accurate for the selected policy. Preserve uncertainty; a threshold decision inside its uncertainty band must remain insufficient evidence/manual review at runtime. No substitution of scheduling intent for acoustic onset. No internal component inference from black-box E2E audio.

Active results may be displayed as `provisional` while required event boundaries can still change. A formal stored MetricResult is emitted only after the boundary policy finalizes its inputs; missing evidence maps to `insufficient_evidence`, and violated capture/contract integrity maps to an invalid result/artifact rather than a plausible number. Recording Analysis is not a finalization service for Active Measurement.

Every observed/pass/fail MetricResult references timestamped Evidence and supporting event IDs when boundaries exist. Exact inputs and formula/definition version must remain recoverable. Same-case repeated attempts and versions must not be mixed silently. Dry-run/synthetic outputs retain execution_kind and cannot qualify a release. A valid contract does not certify the content of the evidence.

Statuses: observed = eligible value without an applied threshold; pass/fail = value compared to a configured threshold; insufficient_evidence = missing/unusable observation; not_applicable = known ineligible condition. The latter two require a reason and null value, with sample_count zero; never convert missing data to zero or false. Finite numbers only. Pass/fail threshold contains operator, value, policy ID/version and case/release_gate origin. Boolean values permit eq only. Ordered comparisons are numeric; units must match the metric. No default confidence and no automatic pass from absent threshold.

**Confidence dimensions are separate and must not be conflated:**

| Dimension | Meaning | Source |
| --- | --- | --- |
| acoustic boundary confidence | how certain the speech onset/offset is | acoustic segmentation (`confidence`, `uncertainty_ms`) |
| speaker cluster confidence | how certain the segment belongs to one speaker cluster | diarization provider |
| role attribution confidence | how certain a speaker cluster is tester/device | source attribution evidence |
| semantic event confidence | how certain a semantic judgment applies | LLM Harness |

`confidence` is nullable. Unknown confidence is `null`, never `0`. `confidence_source` records which dimension a number belongs to.

## PRD metric names and legacy aliases

MetricResult 3.0.0 carries `prd_ref`, `policy` and `policy_version` on every metric.

| PRD | Canonical name | Legacy 2.0.0 name | Mapping |
| --- | --- | --- | --- |
| PRD-M001 | feedback_latency_ms | — | new |
| PRD-M002 | first_speech_latency_ms | e2e_first_audio_latency_ms | same formula, new name and policy ref |
| PRD-M003 | meaningful_response_latency_ms | semantic_response_latency_ms | same intent, semantic anchor required |
| PRD-M004 | turn_gap_ms | — | new, signed, direction corrected |
| PRD-M005 | barge_in_stop_latency_ms | barge_in_stop_latency_ms | unchanged name, response_id binding added |
| PRD-M006 | barge_in_new_intent_latency_ms | — | new, semantic dependency |
| PRD-M007 | barge_in_success | barge_in_success | unchanged name, full 4-component evidence required |
| PRD-M008 | false_endpoint_candidate | false_endpoint | candidate only; confirmation needs semantic evidence |
| PRD-M009 | overlap_duration_ms / overlap_ratio | overlap_duration_ms | ratio added |
| PRD-M010 | timeout / asr_cer / asr_wer / statistics | timeout_rate, asr_cer | unchanged |

Legacy 2.0.0 documents remain readable by validation. New output uses the canonical names above. A legacy name never silently changes meaning.

## Phase 1 formulas and contracts

| Metric (`prd_ref`) | Formula / decision | Applicability, evidence and missing handling | Unit |
| --- | --- | --- | --- |
| first_speech_latency_ms (PRD-M002) | first device speech start minus that turn's final tester speech end | Same turn, first associated response. Require both acoustic boundaries. Device onset before utterance completion is overlap → **not_applicable**, not a negative latency. Tester utterance present but no device onset evidence → insufficient_evidence. No tester utterance → not_applicable. Legacy alias: e2e_first_audio_latency_ms. | ms |
| turn_gap_ms (PRD-M004) | that turn's device speech start minus tester speech end | **Signed.** Negative values express overlap/barge-in and remain `observed`; never clamp to zero or raise. Policy `device_speech_start` (deterministic) is current; a future policy may select a semantic effective-response start. Different policies must not be aggregated together. Legacy direction (device_end → next_tester_start) is retained only as `turn_gap_ms_legacy`, which still rejects negative values. | ms |
| feedback_latency_ms (PRD-M001) | final tester speech end → first perceivable feedback onset | Needs feedback_type (filler / ack / thinking cue / non-speech tone). Pure signal timing cannot separate feedback from noise → insufficient_evidence until acoustic pattern or LLM evidence exists. | ms |
| meaningful_response_latency_ms (PRD-M003) | annotated first meaningful response onset minus final tester speech end | Meaningfulness is anchored by a reviewed transcript/audio interval; acknowledgments/fillers ("嗯", "好的，让我看看") do not count. Manual/semantic identification chooses evidence; arithmetic is deterministic. Missing meaningful boundary → insufficient_evidence even if first audio exists. Legacy alias: semantic_response_latency_ms. | ms |
| false_endpoint_candidate (PRD-M008) | true iff a `possible_false_endpoint` event was observed in the window | **Candidate only.** Confirmation additionally requires tester-continuation evidence, device response inside the intra-utterance pause, the full observation window and semantic/manual verification. A candidate must never be rendered as a confirmed defect. Legacy alias: false_endpoint (meaning unchanged but reserved for the confirmed form). | boolean |
| barge_in_stop_latency_ms (PRD-M005) | interrupted old response's speech end minus actual interruption onset | The `interrupt_start` event must carry the interrupted **old `response_id`**; the paired `device_speech_end` must carry the same response_id. A later different response never satisfies it. Old end missing → insufficient_evidence. Old response already ended before the interruption → **not_applicable** (natural completion); never a negative stop latency. | ms |
| barge_in_new_intent_latency_ms (PRD-M006) | new interrupting utterance end → response to that new intent | Requires semantic intent association. An arbitrary later device onset is not the new-intent response → insufficient_evidence until semantic evidence exists. | ms |
| barge_in_success (PRD-M007) | old answer stopped AND new input accepted AND new intent answered AND no return to the old answer within the window | Composite. Any missing necessary component → insufficient_evidence, even when another component is false. Stop latency and stop success are reported separately from the composite. | boolean |
| overlap_duration_ms / overlap_ratio (PRD-M009) | length of intersection of tester-speech union and device-speech union; ratio over declared denominator | Merge intervals per channel before intersection; half-open, touching bounds contribute zero. Ratio denominator is the same-turn device speech duration when used. Unknown-source speech must not be counted as tester/device overlap. Missing detector/capture is not empty speech. | ms / ratio |
| timeout (PRD-M010) | confirmed observation deadline exceeded | Requires a configured deadline and evidence that observation stayed healthy through it. Capture/network failure is excluded as insufficient_evidence. EOF does not by itself prove a timeout. | boolean |
| asr_cer / asr_wer (PRD-M010) | edit distance / reference unit count | Device-internal transcript required, paired with case reference text. NFC normalization only, case/whitespace/punctuation preserved (nfc-v1). Empty reference = not_applicable. External ASR annotates recordings but cannot populate the device metric. | ratio |
| timeout_rate / `*_success_rate` (PRD-M010) | true eligible observations / eligible observations | Rate aggregation `eligible_ratio`. Fractions in [0,1], not percentages. Report sample_count, total_count and excluded_count; never zero-fill. | ratio |
| context_success / instruction_success | response satisfies explicit facts/constraints from identified prior turns | Prefer exact fact matching when valid; otherwise structured Judge with both context and response evidence. Missing context → insufficient_evidence. | boolean |

Response identity and turn completion are prerequisites supplied by the fusion/timeline engine, not guessed by the formulas. Reference functions in `aivoicebench/formulas.py` are arithmetic demonstrations; event selection lives in `aivoicebench/metrics.py` and produces canonical MetricResult 3.0.0 directly.

Chinese ASR suites may classify numbers, dates, proper nouns, Chinese/English mixing, model IDs, corrections, far-field and noise through capability/tags and versioned reference assets. Additional normalization profiles must be named/versioned; never silently change text normalization to improve CER. Micro CER = sum edit counts / sum eligible reference characters; do not average case CERs unless a distinct macro metric is explicitly defined.

## Structured semantic Judge contract

For context, instruction and new-response intent, return a boolean decision or insufficient_evidence, criterion/rubric ID and version, explicit evidence IDs/intervals, rationale and confidence. Log provider/model/Prompt/config version separately through judge_profile and run snapshot. Evaluate only supplied observations. A Judge cannot return technical timings or invent internal cause. Preserve human sampling for subjective judgments; serious safety remains human-reviewed. The concrete Judge output schema and adapter are Issue #10.

## Aggregation

Single: total_count = 1, sample_count is 0 or 1. Aggregate containers preserve sample_count, total_count and excluded_count with sample_count + excluded_count = total_count. Exclude null/ineligible observations and report their reasons; never zero-fill. Link eligible input MetricResult IDs; cross-run aggregate reference resolution is performed by the report/regression container, not by pretending one Timeline contains other runs.

Latency percentiles use R7: sort n eligible samples ascending; h = (n - 1) * p, lower = floor(h), upper = ceil(h); result = x[lower] + (x[upper] - x[lower]) * (h - lower), with p = .50/.90/.95/.99 and zero-based indices. n = 0 => null/insufficient_evidence; n = 1 => that sample. Retain precision until display. Example [100,200,300,400] ms yields P50=250, P90=370, P95=385, P99=397; n=4.

Boolean rates use named false_endpoint_rate, barge_in_success_rate, context_success_rate, instruction_success_rate. Rate = true eligible observations / eligible observations; aggregation kind rate and algorithm eligible_ratio. These are fractions [0,1], not percentages; convert only for display. For failure rates true means failure; for success rates true means success. Timeout rate follows the same denominator rule. Micro CER aggregation kind micro/algorithm micro_cer records the number of eligible cases as sample_count and links their metrics; numerator/character denominator remain in evidence calculation records.

## White-box reservations

internal_vad_latency_ms, internal_asr_latency_ms, internal_llm_latency_ms and internal_tts_latency_ms are reserved for instrumented device stage start/end logs on documented clock mappings. aec_erle_db requires calibrated echo/reference/residual signal evidence and signal/window definitions. These are not Phase 1 black-box metrics: without the appropriate logs/signals return insufficient_evidence. Black-box AEC behavior must not be presented as ERLE. Schema/source validation is necessary but does not establish that a log contains the right semantic boundaries; runtime adapter validation remains required.

## Measurement Equivalence validation metrics

Equivalence is a validation layer over paired, independent pipeline results, not a new device-experience metric family. For compatible `metric_id`/definition/policy pairs, report signed differences (Active − Recording), Mean Bias, Median Absolute Error, P95 Absolute Error and Bland-Altman limits of agreement. For event/classification outputs, report speech-start/end detection agreement, timeout classification agreement and barge-in classification agreement with eligible/excluded counts.

The initial `first_speech_latency_ms` targets—Median |Δ| ≤ 30 ms, P95 |Δ| ≤ 80 ms and |systematic bias| ≤ 20 ms—are provisional engineering targets pending real paired experiments. They are not industry standards, default release gates or evidence of current accuracy.

## Reporting

Release Gate -> KPI Dashboard -> composite score, only with explicit scoring policy. Gate configuration/version, units, eligible population, sample counts, evidence completeness and execution_kind travel with the decision. JSON and Markdown retain Evidence links. Supplier/version comparisons use compatible case/asset/metric-definition versions and calibration profiles. No synthetic value is a release acceptance result.
