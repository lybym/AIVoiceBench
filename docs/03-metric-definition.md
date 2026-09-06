# Metric definitions 1.0.0 (MetricResult schema 2.0.0)

Definitions are versioned independently from schemas. No thresholds in this document are approved release criteria. Synthetic reference calculations validate arithmetic, not device performance.

## Shared rules

All times use the Timeline run monotonic millisecond basis with explicit event/turn/response association. Compare clocks only when their mapping is established and sufficiently accurate for the selected policy. Preserve uncertainty; a threshold decision inside its uncertainty band must remain insufficient evidence/manual review at runtime. No substitution of scheduling intent for acoustic onset. No internal component inference from black-box E2E audio.

Every observed/pass/fail MetricResult references timestamped Evidence and supporting event IDs when boundaries exist. Exact inputs and formula/definition version must remain recoverable. Same-case repeated attempts and versions must not be mixed silently. Dry-run/synthetic outputs retain execution_kind and cannot qualify a release. A valid contract does not certify the content of the evidence.

Statuses: observed = eligible value without an applied threshold; pass/fail = value compared to a configured threshold; insufficient_evidence = missing/unusable observation; not_applicable = known ineligible condition. The latter two require a reason and null value, with sample_count zero; never convert missing data to zero or false. Finite numbers only. Pass/fail threshold contains operator, value, policy ID/version and case/release_gate origin. Boolean values permit eq only. Ordered comparisons are numeric; units must match the metric. No default confidence and no automatic pass from absent threshold.

## Phase 1 formulas and contracts

| Metric | Formula / decision | Applicability, evidence and missing handling | Unit |
| --- | --- | --- | --- |
| e2e_first_audio_latency_ms | first device speech start after completed user utterance minus its final tester speech end | Same turn, first associated response. VAD intra-utterance pauses are not final utterance boundaries. Require both acoustic boundaries and synchronized tracks. Device onset before utterance completion is overlap/early endpoint, not a negative latency. Missing onset with a valid timeout is still missing latency, with a separate timeout observation. | ms |
| semantic_response_latency_ms | annotated first meaningful response onset minus final tester speech end | Meaningfulness is anchored by a reviewed transcript/audio interval; acknowledgments/fillers do not count. Manual/semantic identification chooses evidence; arithmetic is deterministic. Missing meaningful boundary is insufficient evidence even if first audio exists. | ms |
| false_endpoint | true iff device speech starts strictly inside a declared intra-utterance pause, before tester resumes | Requires planned pause bounds, actual tester continuation and full observable device channel across the window. At exact pause end it is not inside. Silence inferred from missing capture cannot establish false. No planned pause = not applicable. | boolean |
| barge_in_stop_latency_ms | old response speech end minus actual interruption onset | Old response must be active at interruption; compare its response_id, not the new response. Missing old end = insufficient evidence. End before interrupt = not applicable (natural completion). | ms |
| barge_in_stop_success | stop latency <= configured stop limit | Requires valid stop metric and explicit versioned limit. Missing limit/observation = insufficient evidence. | boolean |
| barge_in_new_response_success | new user intent is answered by the new associated response | Structured Judge contract below, or exact deterministic rule when possible. Old-answer continuation is false. Missing transcript/new-response identity = insufficient evidence. | boolean |
| barge_in_success | stop success AND new-response success | Composite combines separately evidenced components; either missing => insufficient evidence, even when the other is false. Preserve both components. | boolean |
| overlap_duration_ms | length of intersection of tester-speech union and device-speech union | Merge intervals on each channel before intersection; half-open intervals, touching bounds contribute zero. Require complete observation window; missing detector/capture does not mean empty speech. | ms |
| asr_cer | Unicode edit distance / reference character count | Device-internal transcript required, paired with case reference text. NFC normalization only, case/whitespace/punctuation preserved (nfc-v1). Empty reference = not applicable. Insertions can produce CER > 1. External ASR may annotate recordings but cannot populate this device metric. | ratio |
| timeout_rate | confirmed timeouts / eligible completed deadline observations | Requires a configured deadline and controller evidence that observation remained healthy through it. Capture/network test-station failure excluded as insufficient evidence. Device nonresponse with intact capture is an eligible timeout. Denominator zero => insufficient evidence. | ratio |
| context_success | response satisfies explicit facts/constraints from identified prior turns | Prefer exact fact matching when valid; otherwise structured Judge with both context and response evidence. Missing context is insufficient evidence. | boolean |
| instruction_success | all mandatory explicit constraints satisfied | Deterministic checks take precedence for strict formats. Semantic constraints use structured Judge; any unassessable mandatory item makes the combined result insufficient evidence. | boolean |

Response identity and turn completion are prerequisites supplied by the controller/timeline engine, not guessed by the formulas. Reference functions in aivoicebench/formulas.py are arithmetic demonstrations; the event selection and audio engine are Issue #8.

Chinese ASR suites may classify numbers, dates, proper nouns, Chinese/English mixing, model IDs, corrections, far-field and noise through capability/tags and versioned reference assets. Additional normalization profiles must be named/versioned; never silently change text normalization to improve CER. Micro CER = sum edit counts / sum eligible reference characters; do not average case CERs unless a distinct macro metric is explicitly defined.

## Structured semantic Judge contract

For context, instruction and new-response intent, return a boolean decision or insufficient_evidence, criterion/rubric ID and version, explicit evidence IDs/intervals, rationale and confidence. Log provider/model/Prompt/config version separately through judge_profile and run snapshot. Evaluate only supplied observations. A Judge cannot return technical timings or invent internal cause. Preserve human sampling for subjective judgments; serious safety remains human-reviewed. The concrete Judge output schema and adapter are Issue #10.

## Aggregation

Single: total_count = 1, sample_count is 0 or 1. Aggregate containers preserve sample_count, total_count and excluded_count with sample_count + excluded_count = total_count. Exclude null/ineligible observations and report their reasons; never zero-fill. Link eligible input MetricResult IDs; cross-run aggregate reference resolution is performed by the report/regression container, not by pretending one Timeline contains other runs.

Latency percentiles use R7: sort n eligible samples ascending; h = (n - 1) * p, lower = floor(h), upper = ceil(h); result = x[lower] + (x[upper] - x[lower]) * (h - lower), with p = .50/.90/.95/.99 and zero-based indices. n = 0 => null/insufficient_evidence; n = 1 => that sample. Retain precision until display. Example [100,200,300,400] ms yields P50=250, P90=370, P95=385, P99=397; n=4.

Boolean rates use named false_endpoint_rate, barge_in_success_rate, context_success_rate, instruction_success_rate. Rate = true eligible observations / eligible observations; aggregation kind rate and algorithm eligible_ratio. These are fractions [0,1], not percentages; convert only for display. For failure rates true means failure; for success rates true means success. Timeout rate follows the same denominator rule. Micro CER aggregation kind micro/algorithm micro_cer records the number of eligible cases as sample_count and links their metrics; numerator/character denominator remain in evidence calculation records.

## White-box reservations

internal_vad_latency_ms, internal_asr_latency_ms, internal_llm_latency_ms and internal_tts_latency_ms are reserved for instrumented device stage start/end logs on documented clock mappings. aec_erle_db requires calibrated echo/reference/residual signal evidence and signal/window definitions. These are not Phase 1 black-box metrics: without the appropriate logs/signals return insufficient_evidence. Black-box AEC behavior must not be presented as ERLE. Schema/source validation is necessary but does not establish that a log contains the right semantic boundaries; runtime adapter validation remains required.

## Reporting

Release Gate -> KPI Dashboard -> composite score, only with explicit scoring policy. Gate configuration/version, units, eligible population, sample counts, evidence completeness and execution_kind travel with the decision. JSON and Markdown retain Evidence links. Supplier/version comparisons use compatible case/asset/metric-definition versions and calibration profiles. No synthetic value is a release acceptance result.
