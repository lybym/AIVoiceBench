# Test methodology and capability mapping

Capability IDs classify what the device should do; metric IDs define measurements; Case IDs select reproducible stimuli and expectations. These catalogs evolve independently. `capability_refs` links cases to the capability catalog below; `metrics` and assertion `metric_id` link cases to metric definitions. An engineering category is the primary ownership layer, not an inferred root cause. Multiple capabilities may be attached to one case.

| Business view | Capability IDs | Engineering layers |
| --- | --- | --- |
| Technical performance | wake_word, vad_endpoint, asr, aec, tts | L1 |
| Technical performance | barge_in, tts_cancel, response_latency | L2 |
| Technical performance | network, endurance, recovery | L5 |
| Interaction experience | turn_taking, pace, interruption, continuous_dialogue | L2 |
| Interaction experience | listening_quality | L1, L4 |
| Interaction experience | far_field | L1 |
| AI product capability | intent, context, memory, instruction_following, knowledge_reasoning, hallucination | L3 |
| AI product capability | persona, emotion, proactivity, safety | L4 |

## Evidence and execution

Freeze stimuli and versions before regression. Record separate stimulus/device tracks and the optional room/reference track. Snapshot device, firmware, model, Prompt, environment, audio routing, asset hashes, calibration and clock synchronization for every run. Mark unsynchronized/mixed tracks and uncertainty explicitly; do not assume independent audio clocks are synchronized. Store measured sample positions and their mapping to run-relative monotonic milliseconds.

External ASR annotates recordings; it does not reveal device-internal recognition. Measure device ASR CER only from an appropriate device transcript paired with the case's ground truth. Black-box latency is observable turn timing only. AEC behavior is not ERLE. Do not infer an internal component root cause from one black-box symptom.

Timing and numeric metrics are deterministic. Judge rubrics require structured decisions and timestamped evidence; missing evidence yields an undecidable result. Human sampling remains required for subjective experience and human review for severe safety findings. Exploration is labeled separately from regression; a confirmed exploratory failure needs a minimized, frozen reproducer before inclusion in a Golden Set.

## Contract vs execution validation

Schema tests check shapes/types and modes. Application-level contract checks enforce unique IDs, references, bounded trigger timing and local asset paths. Runtime readiness checks must additionally verify file existence, resolved path containment (including symlinks), SHA-256, audio QA, provider capability and hardware availability. Fixture paths and zero hashes are intentional synthetic examples, not playable assets.

An 800 ms VAD pause and illustrative latency limits are case examples. Release gates require a separately configured policy/version and sufficient eligible observations. Publish Gate -> KPIs -> scoring only if a scoring policy exists; always report denominator/sample counts and insufficient evidence separately.
