# Test methodology and capability mapping

> 2026-09-13 基线：main 0362b22。Recording Analysis 当前主链与验证限制见 [Recording Backbone](23-recording-backbone.md)；Active Measurement 目标与阶段状态见 [Active Measurement](25-active-measurement.md)。真实设备与 Measurement Equivalence 验证仍待完成。

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

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

## Independent measurement pipelines

正式测量有两个独立入口：

- **Active Measurement**：Live Measurement Audio → online/incremental acoustic processing → Canonical EventTimeline → canonical metrics。
- **Recording Analysis**：External Recording → offline acoustic/semantic processing → Canonical EventTimeline → canonical metrics。

两边不得复用同一声学 Artifact，也不得让 Recording Analysis 给 Active control trace “转正”。相同 Canonical Event 值必须产生相同指标值；pipeline、producer、policy 和 uncertainty 通过 provenance 区分。Control Plane 的 Browser RMS VAD、Streaming ASR、playback log 与 Agent observation 继续用于低延迟控制，但只有满足 Measurement Policy/Evidence Contract 的 Live Measurement Audio 处理结果才能进入正式 Timeline。

## Evidence and execution

Recording Analysis 的输入是已有 5–20 分钟对话录音，通常是手机产生的单轨混音。WAV/MP3/M4A imports preserve source, normalized derivative, hashes, processor/config and derivation records. Active Measurement 的输入是浏览器麦克风持续 PCM，必须保存 Measurement Audio、sample count、采集完整性、设备元数据、Measurement Policy 和每个 stimulus reference。Snapshot device, hardware, firmware, model, Prompt, supplier, environment and notes; unknown values remain null. Scripted TestCase and ground truth are optional. Freeze stimuli for later Golden regression. Separate physical tracks and calibration remain supported when supplied but are not import prerequisites.

Canonical samples map to audio-relative milliseconds by `sample_index * 1000 / sample_rate`. Preserve original presentation/capture timing and conversion/gap uncertainty. wall clock is audit metadata; client/server monotonic clocks are scheduling/transport diagnostics; WebSocket receive time is transport evidence; none substitutes for the audio sample clock. Automatic boundaries distinguish acoustic, ASR-estimated, diarization, semantic-selected and manual evidence, each with source/method/confidence/uncertainty. Mixed audio remains one physical track. In Active Measurement, aligned known stimulus provides tester evidence and remaining speech is only a device candidate; in Recording Analysis, speaker clusters map to tester/device/unknown using evidence, never first-speaker order. Ambiguity is an explicit output.

External ASR annotates recordings; it does not reveal device-internal recognition. Measure device ASR CER only from an appropriate device transcript paired with the case's ground truth. Black-box latency is observable turn timing only. AEC behavior is not ERLE. Do not infer an internal component root cause from one black-box symptom.

Timing and numeric metrics are deterministic. Judge rubrics require structured decisions and timestamped evidence; missing evidence yields an undecidable result. Human sampling remains required for subjective experience and human review for severe safety findings. Exploration is labeled separately from regression; a confirmed exploratory failure needs a minimized, frozen reproducer before inclusion in a Golden Set.

## Contract vs execution validation

Schema tests check shapes/types and modes. Application-level contract checks enforce unique IDs, references, bounded trigger timing and local asset paths. Runtime readiness checks must additionally verify file existence, resolved path containment (including symlinks), SHA-256, audio QA, provider capability and hardware availability. Fixture paths and zero hashes are intentional synthetic examples, not playable assets.

An 800 ms VAD pause and illustrative latency limits are case examples. Release gates require a separately configured policy/version and sufficient eligible observations. Publish Gate -> KPIs -> scoring only if a scoring policy exists; always report denominator/sample counts and insufficient evidence separately.

## Import metric distinctions

Feedback latency ends at associated perceptible feedback and records its type. First speech latency ends at speech onset. Meaningful response latency ends at an existing timestamped information-bearing answer position selected with semantic evidence/confidence. Turn gap declares the effective next-turn definition; overlap may produce a signed gap and must not be silently clamped into an ordinary latency. Barge-in stop observes old response stopping; new-intent latency requires a new-request association. Composite success additionally requires accepted input, new-intent answer and no return to the old response. Missing semantics remains insufficient even when speech stopped.

Unscripted false endpoint requires evidence of intended continuation; possible_false_endpoint stays a candidate. Overlap duration uses interval union and ratio declares its denominator. Backchannels, accidental overlap and intentional interruption need contextual evidence. Timeout requires a configured observation policy and a complete healthy window; file EOF is not timeout.

## Human revision and validation

Keep original model text/segments/events/findings. Append text, role, boundary, association and finding corrections with author/reason/base revision/evidence targets. Produce a new analysis revision, never overwrite raw output. Evaluate automatic processing against labeled recordings, measuring timing error, uncertain roles and abstentions alongside coverage. Synthetic fixtures validate logic; real mixed tester/device recordings establish MVP performance.

## Measurement Equivalence

Measurement Equivalence validates whether independent Active Measurement and Recording Analysis results agree within an approved engineering tolerance; it does not require the same recording, sample alignment or identical values. A paired experiment uses the same physical interaction with independent microphones/artifacts and records time mapping, policy/processor versions and uncertainty.

Report Mean Bias, Median Absolute Error, P95 Absolute Error, Bland-Altman limits of agreement, speech-start/end detection agreement, timeout classification agreement and barge-in classification agreement. An initial `first_speech_latency_ms` target of Median |Δ| ≤ 30 ms, P95 |Δ| ≤ 80 ms and |systematic bias| ≤ 20 ms is **provisional engineering target**, not an industry standard or verified result. Until real paired device experiments pass an approved policy, status remains `measurement_equivalence_validation_pending`.

Ordinary turn-taking is the first Active Measurement acceptance slice. Single-microphone tester/device overlap cannot reliably recover the old device-response stop boundary from Energy VAD alone; missing reference cancellation/AEC/loopback/source-aware evidence yields `insufficient_evidence` for advanced Barge-in metrics.

## Docker / browser delivery acceptance — 2026-09-09

The user replaced Windows executable/installer delivery with Docker backend and
frontend services accessed through a Windows browser. Native Windows packaging is
not an acceptance requirement. Validate image build/startup, mounted persistent
recordings and Run artifacts, restart recovery, and browser upload/analysis/history/
evidence playback/human revision flows. Use actual WAV/MP3/M4A and a labeled real
5–20 minute conversation for product acceptance. Local unit tests and a container
health check alone do not certify analysis accuracy or complete Web UI delivery.
