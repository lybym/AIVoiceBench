# Test methodology and capability mapping

> 2026-09-16 技术路线：正式主架构采用 Linux Server + Docker Backend 与 Remote Chrome Browser Station；现场音频 sample clock 是 Active Measurement 的正式时间基。近期 VAD 分工为 TEN VAD（Browser Control）+ Silero VAD（Server finalized replay），Recording Analysis 优先使用火山 File ASR 的 speaker separation。代码实现与真实验收状态仍以 [PRD](PRD.md) 为准。

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

- **Active Measurement**：Remote Browser Station 的 Live Measurement Audio → server-side finalized acoustic processing → Canonical EventTimeline → canonical metrics。
- **Recording Analysis**：External Recording → offline acoustic/semantic processing → Canonical EventTimeline → canonical metrics。

两边不得复用同一声学 Artifact，也不得让 Recording Analysis 给 Active control trace “转正”。相同 Canonical Event 值必须产生相同指标值；pipeline、producer、policy 和 uncertainty 通过 provenance 区分。

Control Plane 的 TEN VAD（目标）/RMS fallback、Streaming ASR、playback log 与 Agent observation 用于实时控制；只有满足 Measurement Policy/Evidence Contract 的 Live Measurement Audio 处理结果才能进入正式 Timeline。

## Remote Browser Station measurement principle

Active Measurement 的现场链路：

```text
Device / room audio
        ↓
Chrome Browser Station
        ↓ getUserMedia / AudioWorklet
local sample counter + frame sequence
        ├─ TEN VAD target → provisional control observations
        └─ PCM transport → Linux Server
                           ↓
                 durable Measurement Audio
                           ↓
                 Silero finalized replay
```

正式声学时间：

```text
audio_relative_ms = sample_index * 1000 / sample_rate
```

网络 RTT、WebSocket receive time、server monotonic、ASR/LLM response time 只用于 transport/control diagnosis。它们不得被相减后冒充 first-speech、turn-gap、barge-in stop 等正式指标。

Browser Station 还应记录 requested/actual media settings，尤其 `echoCancellation`、`noiseSuppression`、`autoGainControl`、sample rate、channel count 和 device metadata。正式远端访问使用 HTTPS/WSS。

## Evidence and execution

Recording Analysis 的输入是已有 5–20 分钟对话录音，通常是手机或独立录音设备产生的单轨混音。WAV/MP3/M4A imports preserve source, normalized derivative, hashes, processor/config and derivation records.

Active Measurement 的输入是 Browser Station 麦克风持续 PCM，必须保存 Measurement Audio、sample count、frame sequence、采集完整性、Browser Station metadata、Measurement Policy 和每个 stimulus reference。Snapshot device, hardware, firmware, model, Prompt, supplier, environment and notes; unknown values remain null. Scripted TestCase and ground truth are optional. Freeze stimuli for later Golden regression.

Canonical samples map to audio-relative milliseconds by `sample_index * 1000 / sample_rate`. Preserve original presentation/capture timing and conversion/gap uncertainty. wall clock is audit metadata; client/server monotonic clocks are scheduling/transport diagnostics; WebSocket receive time is transport evidence; none substitutes for the audio sample clock.

Automatic boundaries distinguish acoustic、ASR-estimated、speaker-separation、semantic-selected 和 manual evidence，每类都保留 source/method/confidence/uncertainty。

### Recording Analysis speaker separation

近期优先使用火山 File ASR 原生自动说话人分离：

```text
Volcengine speaker label
        ↓
speaker_0 / speaker_1 / ...
        ↓
Attribution
        ↓
tester / device / unknown
```

provider speaker ID 只是匿名 cluster evidence，不是角色真值。不能按 first-speaker order 猜 tester/device。角色歧义保持 unknown/needs_review。当前阶段不把 3D-Speaker 放入关键路径；只有真实目标录音验证显示火山方案 coverage/quality 不足，或出现供应商无关/离线 diarization 需求时再评估。

### VAD methodology

VAD 与 ASR 并行，不是强制 `VAD → ASR` 串联：

- TEN VAD：Browser Control Plane 的低延迟 provisional start/end；
- Silero VAD：Linux Server 对 External Recording / durable Measurement Audio 的 acoustic boundary baseline；
- RMS/Energy：fixture、debug、fallback；
- ASR：文字、语义、provider timing/speaker evidence。

任何第三方 VAD 默认 threshold 都不是 AIVoiceBench 产品门槛。threshold、hysteresis、min speech/silence、merge/padding 与 uncertainty 必须进入版本化 Measurement Policy，并使用人工标注目标录音验证。

## Black-box measurement limits

External ASR annotates recordings; it does not reveal device-internal recognition. Measure device ASR CER only from an appropriate device transcript paired with the case's ground truth. Black-box latency is observable turn timing only. AEC behavior is not ERLE. Do not infer an internal component root cause from one black-box symptom.

Single-microphone mixed audio remains one physical track. In Active Measurement, aligned known stimulus provides tester evidence and remaining speech is only a device candidate. Advanced overlap/barge-in requires stronger source-aware evidence; ordinary VAD cannot isolate overlapping tester/device stop boundaries reliably.

## Deterministic metrics and semantic Judge

Timing and numeric metrics are deterministic. Judge rubrics require structured decisions and timestamped evidence; missing evidence yields an undecidable result. Human sampling remains required for subjective experience and human review for severe safety findings. Exploration is labeled separately from regression; a confirmed exploratory failure needs a minimized, frozen reproducer before inclusion in a Golden Set.

## Contract vs execution validation

Schema tests check shapes/types and modes. Application-level contract checks enforce unique IDs, references, bounded trigger timing and local asset paths. Runtime readiness checks must additionally verify file existence, resolved path containment, SHA-256, audio QA, provider capability and hardware/browser availability. Fixture paths and zero hashes are intentional synthetic examples, not playable assets.

An 800 ms VAD pause and illustrative latency limits are case examples. Release gates require a separately configured policy/version and sufficient eligible observations. Publish Gate → KPIs → scoring only if a scoring policy exists; always report denominator/sample counts and insufficient evidence separately.

## Import metric distinctions

Feedback latency ends at associated perceptible feedback and records its type. First speech latency ends at speech onset. Meaningful response latency ends at an existing timestamped information-bearing answer position selected with semantic evidence/confidence. Turn gap declares the effective next-turn definition; overlap may produce a signed gap and must not be silently clamped into an ordinary latency. Barge-in stop observes old response stopping; new-intent latency requires a new-request association. Composite success additionally requires accepted input, new-intent answer and no return to the old response. Missing semantics remains insufficient even when speech stopped.

Unscripted false endpoint requires evidence of intended continuation; `possible_false_endpoint` stays a candidate. Overlap duration uses interval union and ratio declares its denominator. Backchannels, accidental overlap and intentional interruption need contextual evidence. Timeout requires a configured observation policy and a complete healthy window; file EOF is not timeout.

## Human revision and Evidence Workbench

Keep original model text/segments/events/findings. Append text, role, boundary, association and finding corrections with author/reason/base revision/evidence targets. Produce a new analysis revision, never overwrite raw output.

Web Evidence Workbench 使用 wavesurfer.js 显示 waveform、Regions、Timeline 和 Evidence seek/zoom/highlight。wavesurfer.js 只负责展示与交互，不重新计算正式 boundary/metric；人工拖动区间如未来开放，必须产生 Human Revision。

Evaluate automatic processing against labeled recordings, measuring timing error, uncertain roles and abstentions alongside coverage. Synthetic fixtures validate logic; real mixed tester/device recordings establish MVP performance.

## Measurement Equivalence

Measurement Equivalence validates whether independent Active Measurement and Recording Analysis results agree within an approved engineering tolerance; it does not require the same recording, sample alignment or identical values. A paired experiment uses the same physical interaction with independent microphones/artifacts and records time mapping, policy/processor versions and uncertainty.

Report Mean Bias, Median Absolute Error, P95 Absolute Error, Bland-Altman limits of agreement, speech-start/end detection agreement, timeout classification agreement and barge-in classification agreement. An initial `first_speech_latency_ms` target of Median |Δ| ≤ 30 ms, P95 |Δ| ≤ 80 ms and |systematic bias| ≤ 20 ms is **provisional engineering target**, not an industry standard or verified result. Until real paired device experiments pass an approved policy, status remains `measurement_equivalence_validation_pending`.

## Docker / remote browser delivery acceptance

Native Windows executable/installer is not an acceptance requirement. Validate：

- Linux Docker image build/startup；
- mounted persistent recordings and Run artifacts；
- restart recovery；
- remote Chrome upload/analysis/history/evidence playback/human revision；
- HTTPS/WSS microphone permission path；
- Browser Station local sample clock / sequence / media setting snapshot；
- disconnect/reconnect/gap handling；
- real WAV/MP3/M4A and labeled 5–20 minute conversation acceptance。

Local unit tests and container health checks alone do not certify analysis accuracy, remote Browser Station timing, or complete Web UI delivery。