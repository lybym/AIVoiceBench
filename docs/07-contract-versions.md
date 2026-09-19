# Contract versions and TestCase execution semantics

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

## TestCase 2.0.0 (Issue #1)

Breaking change from the seed 1.0.0: strict mode-specific stimulus objects replace open-ended fields. Case ID remains stable; migrated example content uses Case version 2.0.0. Each contract evolves independently; current imported Transcript is 1.1.0 and MetricResult is 3.0.0. The sections below also document legacy versions.

Migration:

1. Set TestCase `schema_version` to `2.0.0` and increment `case_version` when changing content.
2. Use primary category `level1` L1-L5 and explicit `capability_refs`; keep metric IDs separate.
3. Replace URI-only asset maps with versioned manifests (path, hash, format, provider/model/voice/config/generator version). Config must contain generation parameters only, never credentials.
4. Convert silence milliseconds to integer `sample_count` at 16,000 Hz; 800 ms is 12,800 samples. Reject durations not exactly representable in samples at build time.
5. Replace ad-hoc expected fields with identified assertions: deterministic metric/operator/value, or semantic/manual metric/rubric/human-review flag.
6. Set `case_timeout_ms`; optional `repeat` defaults to one at execution, not through validator mutation. Set explicit Golden Set ID/version when executing regression.

### Segments and modes

`audio_asset` references frozen WAV. `tts` additionally records intended text but also requires a frozen asset reference: no live synthesis during a standard regression. `silence` inserts zero samples with exact count. All playback groups must contain audio. Relative asset paths are rooted in the configured asset directory; runtime must resolve symlinks and verify hashes.

- `fixed_audio`: ordered `segments` and `assets`, no trigger/agent/turns.
- `interactive`: separate `prompt` and `interruption` segment lists, manifests and `trigger`.
- `multi_turn`: at least two ordered turns with unique IDs. Play a turn, associate a fresh device response start/end pair with that turn, then release the next turn. `wait_for` times out from tester playback end; a speech-end event from an earlier response cannot advance the state. The last turn also waits for the response. Case deadline is global; turn timeout cannot extend it.
- `exploratory_agent`: provider/model/local config-profile ID, instructions, turn count and duration limits. No Golden Set association. The config profile resolves local credentials without embedding them in cases. Providers/models are verified before use.

### Interactive state machine

`ready -> play_prompt -> await_device_start -> await_offset -> play_interruption -> observe -> complete`.

Arm detection at prompt playback end. Use the first fresh device speech start associated with the prompt; preserve its source and timestamp. `timeout_ms` is the overall trigger deadline from arming, including the offset. Interruption target time = detected device start + offset. If speech ends before that target, if the target exceeds the trigger deadline, or if no start arrives, mark blocked with the reason; do not play late and claim barge-in. If target equals deadline, deadline wins. Global case timeout always wins. Controller timestamps the actual playback onset; scheduling intent is not observed onset. Interrupted old response and new response need separate associations in the Timeline.

On Windows, scheduling/recording jitter is measured and reported; declarative sample precision is not a claim of measured physical timing. Fixed stimulus execution should eventually record BUILD_INFO and audio QA independently.

### Expected behavior

Assertions have unique IDs and must reference the case's `metrics`. `eq` supports primitive exact comparison; ordered operators require numeric values. Numeric units come from metric definitions. Expectations in example fixtures are illustrative, not approved release gates. A semantic assertion never authorizes a model to invent numeric timing: barge-in success combines independently measured stop behavior with the new-intent semantic result in the future metric engine.

### Offline validation

`python -m aivoicebench validate <case.json> [case.yaml ...]` checks JSON Schema Draft 2020-12 plus reference/timing/uniqueness invariants. Invalid input exits 1 with file and object path. Duplicate YAML/JSON keys and unsafe YAML tags are rejected. Validation is local, requires no credentials and does not execute audio. It does not certify that an asset exists or that a case passed on hardware.

## Event / EventTimeline 2.0.0 and Evidence 1.0.0 (Issue #2)

The seed Timeline wrapper was not governed by a schema. `event-timeline.schema.json` now governs run/case/attempt identity, version snapshots, tracks, artifacts, evidence catalog, ordered events and gaps. `event.schema.json` governs one event. Evidence becomes its own reusable contract ahead of Finding integration in #4. Seed inline artifact snippets migrate to `evidence` catalog entries plus `evidence_ids`. Never silently treat old seed examples as a measured capture.

### Time and tracks

All Event/Evidence/gap times are finite, nonnegative milliseconds from the Timeline's documented time base. Measurement timelines prefer `audio_relative_ms` from sample index; wall-clock time is not used for duration arithmetic. Boundaries are point events (`end_ms == start_ms`); ASR/custom intervals may span time. Intervals use `[start, end)` for duration arithmetic, while point evidence at a boundary is allowed.

Each audio artifact references one logical recording track, with an explicit clock ID/sample rate and sync metadata. `run_ms = sample_index * 1000 / sample_rate_hz + offset_ms`; drift and uncertainty bound cross-track calculations. `duration_ms` is artifact-local elapsed duration; evidence offsets are always run-relative. Multichannel files may be exposed as separate logical track artifacts. `uncalibrated` uses null offsets/uncertainty/drift; consumers must not subtract unrelated clocks. `synthetic` calibration and artifacts cannot be relabeled a hardware run. Calibration measurement/verification belongs to #6; schema validation is not calibration.

Hardware runs require stimulus/device-output track declarations and non-null device/hardware/firmware/model/Prompt/environment snapshots. Runtime additionally verifies actual recorded channels, version values and hash manifests. Blocked/dry-run/imported/synthetic runs can record unknown metadata as null, rather than inventing versions.

### Ordering, association and missing evidence

Events are in nondecreasing `start_ms` order. For equal times the serialized array order is the stable tie-break; no implicit event priority is inferred. Start/end pairs use type + turn ID + response ID. Device speech requires both turn and response; interrupted old and new responses use distinct response IDs. Tester pauses can close/reopen speech within one turn. Complete timelines cannot have unmatched pairs. Partial timelines may retain observed unmatched boundaries with explicit gaps; never generate a missing start/end to make the structure look complete. A blocked timeline can have zero events/evidence and a reason plus required evidence.

`planned_pause_*` represents declared stimulus timing, not an observed device-internal VAD state. `controller` captures scheduling/execution records; measured acoustic onsets should cite audio. `asr_segment` requires text and producer profile identifying the external ASR configuration. It must not be labeled internal device ASR unless supported by an explicit device-log source. `custom` requires a namespaced custom name. New core event types require versioned taxonomy changes instead of arbitrary payload fields.

Active Measurement reuses the same canonical event types. `playback_started/ended`, Browser VAD and Streaming ASR provider timestamps remain control/alignment evidence; formal `tester_speech_start/end` must cite the Live Measurement Audio and Stimulus Alignment producer. `execution-record.json` remains a separate control artifact and is not silently upgraded into EventTimeline 2.0.0.

### Source, scope and confidence

Allowed sources: audio_signal, asr, device_log, manual_annotation, controller, derived. White-box events require device-log sources and device-log artifacts. External audio/ASR/manual/controller/derived observations remain black-box in this version; derived internal-log analysis needs a separately designed provenance contract. Do not use black-box events to name internal VAD/ASR/LLM/TTS stages.

Confidence is required in [0,1] with no default. It expresses producer confidence in annotation/source association, not a statistically calibrated probability or measurement precision. Preserve timestamp uncertainty in sync metadata separately. Evidence must resolve to an artifact/track, have a valid interval, and cover the event it supports. Missing confidence is invalid; uncertain observations may carry low confidence, while absent observations belong in gaps.

Run `python -m aivoicebench validate --kind timeline <timeline.json>`. It checks shape, finite values, identities, ordering, pair association, references and mapped artifact bounds. Physical files/hash accuracy, calibration and observational truth are runtime checks, not guaranteed by this validator.

## MetricResult 2.0.0 (Issue #3)

Migrates inline evidence to Evidence IDs with explicit supporting event IDs. Adds definition_version, execution_kind, structured aggregation/counts/input metric references and traceable threshold policy. Replaces seed string/null values and ambiguous aggregation strings. `observed` represents measurements without a threshold; pass/fail requires a consistent configured comparison. Missing/ineligible results require reason plus null value. White-box metrics require device-log Evidence when linked to a Timeline. No precise technical timing from an LLM.

Validate with `python -m aivoicebench validate --kind metric --timeline examples/timeline.example.json examples/metrics.example.json`. Aggregate input IDs resolve in the future report container; this command checks one Timeline's evidence. Definition/formula details and calculation examples are in `03-metric-definition.md`. Reference arithmetic is implemented/tested; capture/event selection remains #8.

MetricResult 3.0.0 is currently emitted by the Recording Analysis canonical engine and remains the required result family for Active Measurement. Pipeline provenance and provisional/finalized lifecycle will be added through a versioned, backward-readable contract evolution when Active Timeline wiring lands; do not create a realtime-only metric schema. Until then, `execution_kind` retains its existing hardware/imported/synthetic meaning and must not be silently reinterpreted as the measurement pipeline.

## Planned Active Measurement Audio 1.0.0 (PRD-F025)

The next Active Measurement foundation is designed to add a dedicated `active-measurement-audio.schema.json`; that schema does not exist on the docs-only branch yet. It will bind one Active Execution Run to one durable PCM WAV plus capture-integrity and Stimulus Reference metadata. Required semantics include sample-clock origin, SHA-256, exact sample counts, received/gap-filled frames, duplicate/stale/gap accounting, browser device settings, capture start/end, terminal reason and `measurement_policy_version`.

This is separate from `audio-capture.schema.json` 1.0.0, whose contract is the existing PortAudio/HIL duplex capture with `capture.wav` and `stimulus-reference.wav`. Reusing that station-specific shape for browser streaming would make legacy required fields (numeric input/output devices, driver latency, two fixed artifacts) lie. Both contracts remain readable; a later convergence may introduce a common artifact envelope without rewriting historical station documents.

The Measurement Audio contract does not itself assert speech boundaries, source attribution, a complete Timeline or a passing metric. Capture gaps can be preserved with zero samples to maintain the sample clock, but policy eligibility remains explicit. Stimulus references establish immutable inputs and an alignment interface; playback callbacks never satisfy tester acoustic events by themselves.

## Finding 2.0.0 / Evidence 1.0.0 integration (Issue #4)

Migrates inline evidence to the Timeline Evidence catalog; separates observation/defect and observation confidence/cause confidence. Adds explicit unknown/suspected/verified attribution, required review metadata for serious safety confirmation, and candidate/minimized/frozen regression links. Frozen candidate validation requires the linked versioned TestCase/Golden Set. See `08-findings-and-evidence.md` for severity guidance, provenance, lifecycle and CLI examples. Review timestamps require a full timezone-qualified ISO timestamp (no leap seconds); built-in validation does not rely on an optional date-format dependency.

## Import contracts (audited 2026-09-11 at main c612d36; re-checked at main 8d01ef2 / v0.3.2)

- RecordingRun / AnalysisOutput bind Run/Analysis identity, stage status and artifact refs; they do not replace preparation RunManifest.
- Transcript 1.1.0 supports nullable remote model hash, absent word detail/confidence and overlapping utterances in start order; 1.0.0 remains strict for legacy inputs. See [ASR](11-timestamped-asr.md) and [Backbone](23-recording-backbone.md).
- SpeakerAssignments / Attribution keep clustering and tester/device/unknown roles separate. Provider-estimated timing is not acoustic truth; exact versions/fields are in their schemas.
- Imported metrics emit MetricResult 3.0.0 directly; 2.0.0 remains readable through version-aware validation. Fields include analysis_id, prd_ref, turn/response identity, policy/version, reason, confidence source and uncertainty. Nullable fields preserve unknown evidence; schema compliance is not accuracy validation.

Code/tests: [validation.py](../aivoicebench/validation.py), [metrics.py](../aivoicebench/metrics.py), [version compatibility](../tests/test_metric_compatibility.py), [metric contracts](../tests/test_metrics_contract.py). Real-device acceptance remains separate.

## AcousticSegments 1.0.0 additive `processor.sensitivity` + SpeakerAlignment 1.0.0 (Issue #94)

Two contract decisions, both additive:

1. **`AcousticSegments 1.0.0` keeps its version.** `processor.sensitivity` is a new **optional** member (`profile`, `overrides`, `is_canonical_measurement_policy`, `note`). No existing required property changed meaning, and a document written before the field existed still validates, so bumping the version would force every stored Run, example and downstream reader to migrate for a purely descriptive addition. The field exists because a non-canonical acoustic sensitivity must be *visible*: a quiet-device comparison run may never be presented as the canonical measurement policy.
   - Migration: none required. Readers that ignore unknown members keep working; a reader that needs the sensitivity reads `processor.sensitivity` and must treat a missing field as "canonical, unrecorded" rather than "non-canonical".
   - Positive/negative coverage: [test_acoustic.py](../tests/test_acoustic.py) records the parameters; [test_alignment.py](../tests/test_alignment.py) covers canonical default, `quiet_device` marking, canonical-with-override losing canonical status, and rejection of unknown profiles/parameters/non-finite values.
2. **`SpeakerAlignment 1.0.0`** (`schemas/speaker-alignment.schema.json`) is a new document contract for the deterministic acoustic-segment ↔ ASR-speaker-span alignment. It is deliberately separate from FusedSegments rather than extra fields on fused segments: `speaker-alignment` is closed (`additionalProperties: false`) and adding alignment members there would have been a breaking change to a contract that many readers already consume. The alignment document records both source intervals, signed boundary offsets, both overlap ratios, per-cluster coverage with its denominator, the low-energy distribution and the boundary-drift statistics. It contains no `speaker_role` member: an anonymous cluster is never a tester/device role.
   - It is registered as artifact kind `speaker-alignment` inside the recording chain (`alignment.json`) and validated before registration; the `fusion` stage records its document id, status, policy version and processor version.
   - Migration: none; no prior artifact had this shape.

## AcousticSegments 1.0.0 additive model-provider members + VadAnnotation 1.0.0 (Issue #23)
Two contract decisions, both additive. No previously stored Run, example or
downstream reader is invalidated by either.

1. **`AcousticSegments 1.0.0` keeps its version.** Adding an optional
   model-based provider (Silero VAD) required the document to be able to describe
   a boundary produced by *model speech probabilities* instead of frame energies.
   Three members carry that, all optional:
   - **`processor.parameters` becomes a `oneOf` of two closed variants.** The
     energy/RMS variant is unchanged (same fields, same `required`, and it now
     explicitly forbids `threshold`); the model variant requires
     `frame_samples`/`hop_samples`/`threshold`/`min_speech_ms`/`min_silence_ms`
     and may add `negative_threshold`, threshold-crossing counts and a 10-bin
     `speech_probability_histogram`. A model VAD therefore never has to report a
     fabricated energy-relative `threshold_factor`, and an energy document can
     never be mistaken for one that used an absolute probability threshold.
   - **`processor.model` is new and optional**: weight identity
     (`name`/`version`/`sha256`/`source`), runtime + version,
     `execution_provider`, and the fixed analysis window
     (`sample_rate_hz`/`window_samples`/`context_samples`). It exists because a
     model boundary without the weights digest and runtime is not replayable or
     auditable.
   - **`segments[].frame_stats` becomes a `oneOf`**: the energy variant is
     unchanged; the model variant reports `peak/mean/min_speech_probability`,
     the applied `threshold`, `frames_above_threshold` and `frame_count`.
   - Migration: **none required.** The `required` lists, the `schema_version`
     constant and every existing member's meaning are untouched. A reader that
     knows only the common acoustic contract keeps working; a reader that ignores
     `processor.model` keeps working, because no common member moved into it.
     `docs/17-acoustic-segmentation.md` §6.1 states exactly which fields a
     downstream consumer (#24) may depend on.
   - Runtime invariants that the JSON Schema cannot express are enforced by
     `aivoicebench.validation.acoustic_errors`: a model document without the
     `processor.model` block is rejected; an energy-method document that claims a
     model block is rejected; and a `method` naming a non-signal producer
     (`asr`, `diarization`, `speaker`, `llm`, `provider_timestamp`, …) is
     rejected, so an ASR timestamp can never be relabelled as an acoustic
     boundary.
   - Positive/negative coverage: [test_silero_vad.py](../tests/test_silero_vad.py)
     covers the model document shape, the missing-threshold rejection, the
     energy-claims-model rejection, the ASR-method rejection, replay equality and
     the legacy energy document still validating unchanged.
2. **`VadAnnotation 1.0.0`** (`schemas/vad-annotation.schema.json`) is a new
   document contract: one human annotation of speech boundaries in one real
   recording, bound to the recording by sha256, with optional
   `annotated_regions` (the ranges a human actually listened to). It exists so
   Issue #23's real-recording evaluation has an explicit input rather than
   hand-computed numbers, and so "no annotated sample set" is a representable
   state. It carries no accuracy result and no product threshold.
   - It is **not** a measurement policy and is not registered in the recording
     chain; it is consumed by `aivoicebench vad-eval`
     ([vad_evaluation.py](../aivoicebench/vad_evaluation.py)).
   - Migration: none; no prior artifact had this shape.
   - Positive/negative coverage: [test_vad_evaluation.py](../tests/test_vad_evaluation.py)
     covers schema rejection (missing digest, reversed/overlapping/out-of-bounds
     intervals, empty annotator), the `no_annotated_sample_set` /
     `annotated_sample_set_has_no_intervals` states, one-to-one matching,
     signed/absolute start-end error, miss and false-alarm accounting, the
     denominator/coverage figures and cross-document scoring.
3. **`SpeakerAlignment 1.0.0` keeps its version and gains one optional member.**
   `diagnostics.low_energy.energy_evidence` is a new **optional** array: one
   `{acoustic_segment_id, mean_rms}` entry per acoustic segment, with `mean_rms:
   null` when that segment carried no energy measurement. It exists because
   `alignment.py` previously read a missing `frame_stats.mean_rms` as `0.0`,
   which marked every boundary from a model-based provider (Silero reports
   per-frame speech probabilities, not RMS) as `low_energy` — a silent
   "quiet device" diagnosis manufactured from absent evidence. `low_energy` on a
   segment is now `true` only for a *measured* reading at or below the computed
   threshold, and both the threshold and the distribution are computed from the
   measured segments only.
   - Migration: none required. The member is optional and absent-means-unrecorded
     for readers; the `low_energy` rule only became stricter (never `true`
     without a measurement), which cannot turn a previously reported finding into
     a false one.
   - Positive/negative coverage: [test_alignment.py](../tests/test_alignment.py)
     covers a model segment excluded from the distribution while its `null` is
     recorded, an all-model document reporting `threshold_mean_rms: null` with
     zero low-energy segments, a genuinely measured `0.0` still being a finding,
     and the older document shape (field absent) still validating.

## SpeakerRoleReview 1.0.0 (Issue #95)

A third new document contract, `schemas/role-review.schema.json`, carries the manual speaker-role gate: the anonymous clusters, the evidence needed to decide each one, the saved human revision, the revision history and the diff.

- `RecordingRun` and `AnalysisOutput` are **unchanged**. The gate is published as a registered document (artifact kinds `speaker-role-review` for the per-revision surface and `speaker-role-mapping` for each immutable decision set) rather than as a new stage. Adding a stage would have been a breaking change to `recording-run.schema.json` (its `stages` object is closed and its required list is fixed), which is not warranted for a decision record layered on top of the existing chain.
- Decisions are stored one file per save (`role-review/role-mapping-REV-NNNN.json`) with an increasing index and a `previous_revision_ref`; an earlier decision is never overwritten. `unknown` is a stored decision and is distinguishable from "not yet reviewed", because the review document reports `awaiting_decision_for` separately from `unknown_clusters`.
- Migration: none; the contract is new and no existing Run needs rewriting. A Run analysed before this contract exists has no `role-review.json`, and the API builds an equivalent surface from its own preserved clusters instead of reporting the gate as satisfied.
- Positive/negative coverage: [test_role_review.py](../tests/test_role_review.py) covers the review surface and schema, incomplete/invented/wrong-role rejection, append-only indexing, diffing, the `awaiting_role_review` → `complete_review` transition, `unknown`-only mappings staying insufficient, and persistence across restart; [test_role_review_browser.py](../tests/test_role_review_browser.py) drives the real page through the same gate.

## FusedSegments 1.0.0 / Turns 1.0.0 already-required members and Turn semantics (Issue #24)

Issue #24 does **not** add a member to `FusedSegments`, `Turns` or
`Event`/`EventTimeline`. It records the members that #94/#95 introduced under the
unchanged `1.0.0` label, and fixes three behaviours those members made observable.
No stored document changes shape, so **no migration is required**.

1. **`FusedSegments 1.0.0` required list (unchanged version, members introduced by
   #94/#95).** `segments[]` now lists
   `speaker_evidence`, `speaker_candidates`, `segment_origin`, `text_attribution`,
   `start_boundary_source`, `end_boundary_source` and `role_attribution` as
   **required**. They are required rather than optional because each one is the
   *reason* a consumer must not read a fused segment as a decided fact: without
   `speaker_evidence` an abstention is indistinguishable from an assignment, and
   without `start/end_boundary_source` a provider-estimated edge is
   indistinguishable from a measured acoustic one.
   - Migration: a FusedSegments 1.0.0 document written before those members existed
     is no longer schema-valid. It was never a published contract: fused segments
     are an internal analysis artifact of one Run's `analysis/<id>/` directory, they
     are not hand-authored, and every stored Run is re-derivable from the preserved
     acoustic, transcript and diarization evidence. A reader that encounters such a
     document should re-run the fusion stage rather than treat it as authoritative.
   - Positive/negative coverage: [test_fusion_speakers.py](../tests/test_fusion_speakers.py)
     asserts each evidence state and the split/abstain distinction;
     [test_turns_events_attribution.py](../tests/test_turns_events_attribution.py)
     asserts the downstream consequences.
2. **A split segment must not reuse its parent's `segment_id`.** `apply_speakers`
   cuts one acoustic segment at speaker boundaries, and the pieces previously kept
   the parent's `segment_id`. Turn association and the timeline's `ev_map` key on
   that id, so the duplicate made one piece's evidence stand in for another's and a
   canonical event could cite an interval that did not cover it (`evidence must
   cover the event interval`). Every emitted segment now receives a unique
   `FSEG-NNNN`, and the shared `acoustic_segment_id` still identifies the acoustic
   segment every piece came from. `fused_errors` already rejected duplicate ids;
   the bug was that a split document was never validated before this Issue.
   - Migration: none. Only the *value* of `segment_id` for split pieces changed,
     and those ids were ambiguous before, so no correct consumer depended on them.
3. **Turn/Response numbering is positional.** `turn_id` and `response_id` are
   derived from the turns already built rather than from a counter bumped inside
   each branch. Previously a recording that began with device speech produced
   `TURN-0002` as its first turn (a gap, and a numbering that disagreed with array
   order); two turns could also collide on one id. `turn_id` is now always
   `TURN-0001..N` in array order, and `response_id` is `RESP-0001..N` over the turns
   that own device speech.
   - Migration: none. The previous numbering was not contiguous and therefore not a
     contract any consumer could rely on. Metrics bind to `turn_id`/`response_id`
     as *identifiers*, not as ordinals, and are computed from one timeline at a time.
4. **Incomplete evidence is never reported as `complete`.** Three status
   corrections, all making a previously over-claimed `complete` honest:
   - `build_turns` inherits the fused document's incompleteness. If roles are
     unresolved, conflicting or unmatched, the turn document is `partial` (or
     `insufficient_evidence` when no turn can be claimed) and its `reason` names the
     cause. A turn set built from only the attributed subset is not a complete turn
     set.
   - `detect_events` returns `partial` rather than `complete` when some segments
     carry no confirmed role. `generate_timeline` records a `gaps` entry for every
     non-`complete` timeline, so a `partial` timeline always explains itself.
   - An acoustic-timing evidence snippet publishes only the acoustic boundary
     confidence. It previously used the segment's role-attribution or
     speaker-cluster confidence when no acoustic number existed, which presented a
     human role decision (or a provider's clustering number) as the quality of an
     acoustic measurement. Absent stays `null` / `confidence_source: "none"`, and a
     split edge whose boundary came from a provider estimate publishes no acoustic
     confidence at all.
   - Migration: none. Every change moves a status from optimistic to explicit, so a
     consumer that already refused to act on `partial`/`insufficient_evidence` is
     unaffected, and a consumer that trusted `complete` was previously being told
     something untrue.
   - Positive/negative coverage: [test_turns_events_attribution.py](../tests/test_turns_events_attribution.py)
     covers consecutive same-role segments, a device-first recording, contiguous
     unique turn/response ids, a mixed mapping (one cluster deliberately `unknown`),
     an all-`unknown` mapping, an unmatched segment, a missing utterance label,
     conflicting clusters, split-edge confidence, evidence coverage, run identity and
     reproducibility of the association and the timeline.

