# Metric definitions 4.0.0 (MetricResult schema 3.0.0)

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Definitions are versioned independently from schemas. No thresholds in this document are approved release criteria. Synthetic reference calculations validate arithmetic, not device performance.

## Definition versions and the PRD-M001–M010 conflict

`docs/prd/metric-requirements.md` (PRD 1.5.0) decomposes `PRD-M001–M010` as First
Speech Latency / Endpoint Response End / Semantic Response / Turn Gap / Barge-in Stop
Latency / Barge-in Semantic Compliance / False Endpoint / ASR-Transcript Quality /
Overlap / Coverage-Outcome. The pre-1.5.0 PRD — and therefore this document and
`aivoicebench/metrics.py` before Issue #25 — used a different set (Feedback Latency /
First Speech / Meaningful Response / Turn Gap / Barge-in Stop / Barge-in New Intent /
Barge-in Success / False Endpoint / Overlap / Timeout-CER-WER).

The issue is that a PRD id is only meaningful together with the definition version
that assigns it: `PRD-M002` means First Speech Latency under `definition_version`
3.0.0 and Endpoint/Response End under 4.0.0. Issue #25 instructs implementation
"according to current PRD definitions", so **the current PRD wins** and the canonical
engine emits `definition_version` **4.0.0**. The historical table is retained in
`aivoicebench.metrics.PRD_REFS_BY_DEFINITION` together with `prd_ref_for`,
`migrate_prd_ref` and `migrate_metric_document`, so a stored 3.0.0 document keeps its
original meaning and any conversion is explicit; `metric_errors` rejects a document
whose `definition_version` maps its name to a different PRD id.

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
`definition_version` 4.0.0 assigns the current PRD decomposition:

| PRD | Canonical name | Unit | Path |
| --- | --- | --- | --- |
| PRD-M001 | first_speech_latency_ms | ms | deterministic, acoustic boundaries |
| PRD-M002 | response_end_candidate_ms | ms | deterministic candidate + `uncertainty_ms` |
| PRD-M003 | semantic_response | boolean | constrained semantic evidence |
| PRD-M004 | turn_gap_ms | ms | deterministic, signed |
| PRD-M005 | barge_in_stop_latency_ms | ms | deterministic, interrupted `response_id` |
| PRD-M006 | barge_in_semantic_compliance | boolean | constrained semantic evidence |
| PRD-M007 | false_endpoint_candidate / false_endpoint_confirmed | boolean | candidate heuristic; confirmation is composite |
| PRD-M008 | asr_cer / asr_wer | ratio | eligible reference + device-internal transcript |
| PRD-M009 | overlap_duration_ms / overlap_ratio | ms / ratio | deterministic interval intersection |
| PRD-M010 | coverage | ratio | deterministic planned/attempted/measured/abstained counts |

Names the current decomposition no longer defines (`feedback_latency_ms`,
`meaningful_response_latency_ms`, `barge_in_new_intent_latency_ms`,
`barge_in_success`) are still emitted as **legacy continuity records**: `prd_ref` is
`null`, the status is `insufficient_evidence` and the reason says the current PRD no
longer defines the requirement. They are never computed into a new value, and
`metric_errors` requires that reason. Legacy 2.0.0 documents and 3.0.0 documents remain
readable; new output never reuses an old PRD id for a name the current definition maps
elsewhere.

## Phase 1 formulas and contracts

| Metric (`prd_ref`) | Formula / decision | Applicability, evidence and missing handling | Unit |
| --- | --- | --- | --- |
| first_speech_latency_ms (PRD-M001) | first device speech start minus that turn's final tester speech end | Same turn, first associated response. Require both **acoustic** boundaries (`source` audio_signal/manual_annotation). Device onset before utterance completion is overlap → **not_applicable**, not a negative latency. Tester utterance present but no device onset evidence → insufficient_evidence. No tester utterance → not_applicable. An ASR/control-sourced boundary abstains instead of becoming a value. | ms |
| response_end_candidate_ms (PRD-M002) | absolute `audio_relative_ms` position of the turn's acoustic response end, with its own `uncertainty_ms` and boundary confidence | Candidate, never a confirmed end. The request must have **completed**: the turn needs a `tester_speech_end`, and a turn with only `tester_speech_start` abstains because the request never closed. An acoustic `device_speech_end` or acoustic `response_end` may supply the candidate; a `response_end` that is only `derived`, and any `asr`-sourced end or ASR final timestamp, cannot — the metric then abstains naming that reason. In the real fusion pipeline the event is `derived`, so the candidate always comes from the acoustic `device_speech_end`. No device response at all → not_applicable. | ms |
| semantic_response (PRD-M003) | boolean coverage decision from a constrained semantic record | Requires a record with `kind=semantic_response`, a boolean `decision`, `criterion_id`/`criterion_version`, a `judge_profile` and evidence/event references. Anything less abstains. No device response → not_applicable. A performed judgment carries `method=llm_judge`. | boolean |
| turn_gap_ms (PRD-M004) | that turn's device speech start minus tester speech end | **Signed.** Negative values express overlap/barge-in and remain `observed`; never clamp to zero or raise. Policy `device_speech_start` (deterministic) is current; a future policy may select a semantic effective-response start. Different policies must not be aggregated together. Legacy direction (device_end → next_tester_start) is retained only as `turn_gap_ms_legacy`, which still rejects negative values. | ms |
| barge_in_stop_latency_ms (PRD-M005) | interrupted old response's speech end minus actual interruption onset | The `interrupt_start` event must carry the interrupted **old `response_id`**; the paired `device_speech_end` must carry the same response_id. A later different response never satisfies it. Old end missing → insufficient_evidence. Old response already ended before the interruption → **not_applicable** (natural completion); never a negative stop latency. No interruption in the turn → not_applicable. | ms |
| barge_in_semantic_compliance (PRD-M006) | boolean compliance decision for the response that follows an interruption | Requires `kind=barge_in_compliance` constrained semantic evidence with the same structural requirements as PRD-M003. No interruption in the turn → not_applicable. Barge-in stop latency never implies compliance. | boolean |
| false_endpoint_candidate / false_endpoint_confirmed (PRD-M007) | candidate: true iff a `possible_false_endpoint` event was observed. confirmed: true only with tester continuation, an in-pause device response end, a covered observation window, a complete timeline and an explicit semantic/manual confirmation record | **Candidate and confirmation are separate results.** A candidate alone is always emitted and never rendered as a confirmed defect; a missing component makes the confirmed metric `insufficient_evidence` and names every missing component. Confirmation is `method=composite`, never a bare heuristic. | boolean |
| asr_cer / asr_wer (PRD-M008) | edit distance / reference unit count over NFC-normalized text | Requires an eligible nonempty reference **and** a device-internal transcript carrying an evidence reference. An external ASR transcript annotates the recording and can never populate the device metric — it abstains naming that reason. Empty reference → not_applicable. Word/character normalization is `nfc-v1`; case/whitespace/punctuation preserved. | ratio |
| overlap_duration_ms / overlap_ratio (PRD-M009) | length of the intersection of tester-speech union and device-speech union; ratio over the declared denominator | Explicit `overlap_start`/`overlap_end` pairs are used when present; otherwise the interval unions of both roles are intersected. Merge intervals per channel before intersecting; touching bounds contribute zero. When tester/device identity cannot be established from the recording the metric **abstains** and names the timeline gap — a mixed track without roles is not a zero-overlap measurement. A turn whose role identity and intervals exist but whose intersection is not formable (a role set with no closed interval) also abstains explicitly instead of producing no document, so PRD-M010 can never read an unmeasurable turn as evaluated. | ms / ratio |
| coverage (PRD-M010) | measured turns / attempted turns | Attempted units are the timeline's turns; measured units are turns with at least one **formal measurement**, meaning an observed PRD-M001 `first_speech_latency_ms`, PRD-M004 `turn_gap_ms` or PRD-M005 `barge_in_stop_latency_ms` whose interval actually closed. An observed PRD-M002 endpoint **candidate** is deliberately excluded: it is a position, observable on a turn that produced no measurement, and counting it made a run with no formal measurement report an observed 1.0. `aggregation` is `kind=rate` and reports `sample_count`/`total_count`/`excluded_count` plus `invalid_count`, `abstained_count`, one linked `input_metric_ids` entry per measured turn and, when a plan exists, `planned_count`. No plan → planned coverage is not reported and a control/transport success is never substituted. No attempted unit → not_applicable. | ratio |

Aggregation for the whole engine is `aggregate_metrics(...)` (percentile over
compatible single samples with `algorithm=R7`, otherwise a rate with
`algorithm=eligible_ratio`) and `latency_percentiles(...)` for the reporting view. Both
keep the denominator and the invalid/abstained counts; neither zero-fills.

Every observed metric must reference timestamped Evidence; the engine never emits an
observed value without an evidence reference, and `invalid` (artifact integrity
failure) is a distinct status from `not_applicable` and `insufficient_evidence`.
An invalid-integrity run emits an `invalid` document for **every** name the current
decomposition defines, plus the legacy continuity records in the same status: an
omitted requirement must not be readable as an evaluated one.

### Aggregation identity and the overall status

`aggregation.sample_count`/`total_count`/`excluded_count` and `input_metric_ids` are one
contract: a `rate`/`micro` aggregate links exactly one eligible input `metric_id` per
sample, and `metric_errors` rejects a document that claims a sample without linking it.
`metric_id` therefore has to be unique per sample, which is why a name a single turn can
produce several samples of (one `overlap_duration_ms` per overlap pair, one
`barge_in_stop_latency_ms` per interruption) carries a per-sample discriminator.

The overall status of a metric document set has a **single** rule
(`metrics.run_status`): `observed` when any document is `observed`/`pass`/`fail`
*and* carries a non-null value, else `invalid` when any document is `invalid`, else
the timeline's own `invalid`/`blocked`, else `insufficient_evidence`. Every calling
surface derives it from that one helper so the same documents cannot produce two
different run statuses.

### Names outside the current PRD-M001–M010 decomposition

| Metric | Formula / decision | Applicability, evidence and missing handling | Unit |
| --- | --- | --- | --- |
| context_success / instruction_success | response satisfies explicit facts/constraints from identified prior turns | Prefer exact fact matching when valid; otherwise structured Judge with both context and response evidence. Missing context → insufficient_evidence. | boolean |
| timeout (legacy) | confirmed observation deadline exceeded | Requires a configured deadline and evidence that observation stayed healthy through it. Capture/network failure is excluded as insufficient_evidence. EOF does not by itself prove a timeout. The deterministic canonical engine emits PRD-M010 `coverage` instead; `timeout`/`timeout_rate` remain in the legacy Case/Timeline evaluator (`aivoicebench/engine.py`). | boolean |
| timeout_rate / `*_success_rate` (legacy) | true eligible observations / eligible observations | Rate aggregation `eligible_ratio`. Fractions in [0,1], not percentages. Report sample_count, total_count and excluded_count; never zero-fill. | ratio |

Response identity and turn completion are prerequisites supplied by the fusion/timeline engine, not guessed by the formulas. Reference functions in `aivoicebench/formulas.py` are arithmetic demonstrations; event selection lives in `aivoicebench/metrics.py` and produces canonical MetricResult 3.0.0 directly.

Chinese ASR suites may classify numbers, dates, proper nouns, Chinese/English mixing, model IDs, corrections, far-field and noise through capability/tags and versioned reference assets. Additional normalization profiles must be named/versioned; never silently change text normalization to improve CER. Micro CER = sum edit counts / sum eligible reference characters; do not average case CERs unless a distinct macro metric is explicitly defined.

## Structured semantic Judge contract

For context, instruction and new-response intent, return a boolean decision or insufficient_evidence, criterion/rubric ID and version, explicit evidence IDs/intervals, rationale and confidence. Log provider/model/Prompt/config version separately through judge_profile and run snapshot. Evaluate only supplied observations. A Judge cannot return technical timings or invent internal cause. Preserve human sampling for subjective judgments; serious safety remains human-reviewed.

The Judge output schema (`JudgeResult 1.0.0`) and its adapter are implemented by
Issue #10 (`aivoicebench/llm.py`, `aivoicebench/semantic_evidence.py`):

- **Timing is selected, not authored.** `meaningful_response` and
  `feedback_detection` report milliseconds that come from a measured anchor the
  provider selected by id from the boundaries that already exist inside the
  judged Turn. A model-authored millisecond, an unknown anchor id, or an anchor
  without evidence is rejected; without a usable anchor the result abstains.
  `derived` boundaries (fusion's `response_start`/`response_end`) are not
  measured anchors.
- **A boolean semantic verdict is eligible evidence only with its criterion.**
  `semantic_response` (PRD-M003) and `barge_in_compliance` (PRD-M006) require a
  boolean decision, `criterion_id`/`criterion_version`, a `judge_profile` and at
  least one reference that resolves inside the judged Turn. Anything less is an
  explicit abstention with a reason — never a `false` value.
- **Citations are scoped.** A per-turn judgment cites only that turn's events and
  their evidence; a run-level judgment may cite any object of the same Timeline.
  The adapter expands a selected event to that event's own evidence, so the
  metric invariant "event evidence must be included in metric evidence_ids" holds
  by construction.
- A suspected cause always carries `requires_log_verification=true` and a
  non-certain `attribution_confidence`; nothing here promotes it to a verified
  root cause.

Raw provider output, provider/model/prompt/criteria provenance and every
abstention are preserved on the Judge artifact, and credential-shaped material is
rejected before publication (PRD-N004).

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
