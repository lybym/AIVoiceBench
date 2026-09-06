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
