# Contract versions and TestCase execution semantics

## TestCase 2.0.0 (Issue #1)

Breaking change from the seed 1.0.0: strict mode-specific stimulus objects replace open-ended fields. Case ID remains stable; migrated example content uses Case version 2.0.0. Other seed contracts remain at 1.0.0 until their respective Issues; do not assume all contracts share one version.

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

All Event/Evidence/gap times are finite, nonnegative milliseconds from a documented run monotonic origin. Wall-clock time is not used for duration arithmetic. Boundaries are point events (`end_ms == start_ms`); ASR/custom intervals may span time. Intervals use `[start, end)` for duration arithmetic, while point evidence at a boundary is allowed.

Each audio artifact references one logical recording track, with an explicit clock ID/sample rate and sync metadata. `run_ms = sample_index * 1000 / sample_rate_hz + offset_ms`; drift and uncertainty bound cross-track calculations. `duration_ms` is artifact-local elapsed duration; evidence offsets are always run-relative. Multichannel files may be exposed as separate logical track artifacts. `uncalibrated` uses null offsets/uncertainty/drift; consumers must not subtract unrelated clocks. `synthetic` calibration and artifacts cannot be relabeled a hardware run. Calibration measurement/verification belongs to #6; schema validation is not calibration.

Hardware runs require stimulus/device-output track declarations and non-null device/hardware/firmware/model/Prompt/environment snapshots. Runtime additionally verifies actual recorded channels, version values and hash manifests. Blocked/dry-run/imported/synthetic runs can record unknown metadata as null, rather than inventing versions.

### Ordering, association and missing evidence

Events are in nondecreasing `start_ms` order. For equal times the serialized array order is the stable tie-break; no implicit event priority is inferred. Start/end pairs use type + turn ID + response ID. Device speech requires both turn and response; interrupted old and new responses use distinct response IDs. Tester pauses can close/reopen speech within one turn. Complete timelines cannot have unmatched pairs. Partial timelines may retain observed unmatched boundaries with explicit gaps; never generate a missing start/end to make the structure look complete. A blocked timeline can have zero events/evidence and a reason plus required evidence.

`planned_pause_*` represents declared stimulus timing, not an observed device-internal VAD state. `controller` captures scheduling/execution records; measured acoustic onsets should cite audio. `asr_segment` requires text and producer profile identifying the external ASR configuration. It must not be labeled internal device ASR unless supported by an explicit device-log source. `custom` requires a namespaced custom name. New core event types require versioned taxonomy changes instead of arbitrary payload fields.

### Source, scope and confidence

Allowed sources: audio_signal, asr, device_log, manual_annotation, controller, derived. White-box events require device-log sources and device-log artifacts. External audio/ASR/manual/controller/derived observations remain black-box in this version; derived internal-log analysis needs a separately designed provenance contract. Do not use black-box events to name internal VAD/ASR/LLM/TTS stages.

Confidence is required in [0,1] with no default. It expresses producer confidence in annotation/source association, not a statistically calibrated probability or measurement precision. Preserve timestamp uncertainty in sync metadata separately. Evidence must resolve to an artifact/track, have a valid interval, and cover the event it supports. Missing confidence is invalid; uncertain observations may carry low confidence, while absent observations belong in gaps.

Run `python -m aivoicebench validate --kind timeline <timeline.json>`. It checks shape, finite values, identities, ordering, pair association, references and mapped artifact bounds. Physical files/hash accuracy, calibration and observational truth are runtime checks, not guaranteed by this validator.

## MetricResult 2.0.0 (Issue #3)

Migrates inline evidence to Evidence IDs with explicit supporting event IDs. Adds definition_version, execution_kind, structured aggregation/counts/input metric references and traceable threshold policy. Replaces seed string/null values and ambiguous aggregation strings. `observed` represents measurements without a threshold; pass/fail requires a consistent configured comparison. Missing/ineligible results require reason plus null value. White-box metrics require device-log Evidence when linked to a Timeline. No precise technical timing from an LLM.

Validate with `python -m aivoicebench validate --kind metric --timeline examples/timeline.example.json examples/metrics.example.json`. Aggregate input IDs resolve in the future report container; this command checks one Timeline's evidence. Definition/formula details and calculation examples are in `03-metric-definition.md`. Reference arithmetic is implemented/tested; capture/event selection remains #8.

## Finding 2.0.0 / Evidence 1.0.0 integration (Issue #4)

Migrates inline evidence to the Timeline Evidence catalog; separates observation/defect and observation confidence/cause confidence. Adds explicit unknown/suspected/verified attribution, required review metadata for serious safety confirmation, and candidate/minimized/frozen regression links. Frozen candidate validation requires the linked versioned TestCase/Golden Set. See `08-findings-and-evidence.md` for severity guidance, provenance, lifecycle and CLI examples. Review timestamps require a full timezone-qualified ISO timestamp (no leap seconds); built-in validation does not rely on an optional date-format dependency.
