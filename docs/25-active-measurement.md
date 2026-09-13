# Active Measurement — Measurement Audio 与正式结果基础

PRD refs: PRD-F023/F025/F026、PRD-M001–M010、PRD-N002/N003/N007。本文描述目标架构与分阶段实现边界；未标为 implemented 的能力不得按完成报告。

## 1. Pipeline boundary

```text
Browser Microphone
       ↓ one physical PCM source
 ┌─────┴───────────────────────────────┐
 │ Control Plane                       │ Measurement Plane
 │ RMS VAD / Streaming ASR / Agent     │ durable Measurement Audio
 │ playback / timeout / UI             │ sample clock / integrity
 │ execution-record.json               │ EventTimeline / MetricResult
 └─────────────────────────────────────┘
```

Active Measurement 与 Recording Analysis 都是正式 Measurement Pipeline。前者的声学原件固定标识为 `ART-live-measurement-audio-*`，后者为 `ART-external-recording-*`；不得把同一音频绕行 Recording Analysis 后称为 Active Result，也不得把 execution record 改名为 Timeline。

## 2. Measurement Audio contract

每次 Active Run 在第一句播放前开始持续采集，到完成、用户停止、失败或连接中断为止。基础 Artifact 使用 PCM16LE / 16 kHz / mono WAV，并记录：artifact/run/session identity、source、path、SHA-256、sample rate/channels/encoding/bit depth、sample count、captured sample count、gap-filled sample count、capture start/end、frame size/count、dropped/duplicate/gap/stale frames、浏览器可提供的设备设置、结束原因和 `measurement_policy_version`。

传输帧使用固定长度 `[4B big-endian sequence][PCM16LE]`。序号必须按“浏览器产生的帧”递增，而不是只按成功发送的帧递增，使背压丢帧在下一已收帧处表现为 gap。服务端拒绝奇数字节、非固定 frame size 和错误格式；duplicate/stale frame 不重复写入。缺口用显式零样本填充以维持 sample clock，并同时记录 gap frame/sample 数；含 gap 的 Artifact 可保存审计，但是否可用于某个指标由 Measurement Policy 决定。

正常完成写 `status=complete`；用户取消、断连或控制失败写 `partial` 并保留已接收样本；无法形成合法 WAV/metadata 写 `invalid`。停止后 Artifact 只读，SHA 和 sample count 必须能从 WAV 复核。

## 3. Time bases

| Clock | 用途 | 能否直接成为正式声学边界 |
| --- | --- | --- |
| audio sample clock | `sample_index / sample_rate`，Event/Evidence 的 `audio_relative_ms` | 是，首选 |
| client monotonic | 浏览器调度、挂起/恢复诊断 | 否 |
| server monotonic | 会话、处理和 transport latency 诊断 | 否 |
| wall clock | 审计、跨 Artifact 粗关联 | 否 |
| WebSocket receive time | transport/jitter 诊断 | 否 |

## 4. Stimulus Reference interface

每个实际播放资产保存 immutable reference：artifact ID、turn/playback identity、相对路径、SHA-256、sample rate、channels、encoding、sample count。`playback_started/ended` 只提供 alignment 搜索窗口先验。

后续 `StimulusAligner` 接口接收 Measurement Audio + Stimulus Reference + search window，输出 match interval、correlation/confidence、uncertainty、processor/policy version 和 evidence refs。弱匹配、多重匹配、截断或 capture gap 覆盖关键区域时弃权；绝不把 callback 时间直接输出成 `tester_speech_start/end`。

## 5. AcousticBoundaryPolicy

策略与执行方式解耦。`AcousticBoundaryPolicy` 至少版本化 frame/hop、rolling noise estimator、start/end threshold、hysteresis、min speech、min silence、merge gap 和 boundary uncertainty。`StreamingAcousticSegmenter` 必须 causal；Batch replay 可驱动同一 state machine 验证。

当前 `EnergyVadSegmenter 1.0.0` 使用完整文件的 10th percentile noise floor 和 global peak，属于 Recording Analysis Batch Algorithm，不能作为严格 Streaming 实现。阶段性保留是兼容选择，不宣称与未来 Streaming policy 完全等价。

## 6. Canonical events and metrics

Online Event Producer 与 Offline Event Producer 都输出现有 Canonical Event 类型。Active 使用已对齐 stimulus 形成 tester evidence，剩余 speech 仅作 device candidate；unknown 不强制分配。每个正式事件引用 Measurement Audio Evidence，并记录 `audio_relative_ms`、confidence、uncertainty、producer 和 policy version。

两个 Pipeline 都调用 `compute_timeline_metrics(...)`；同一 canonical fixture 必须得到相同指标数值。`execution_kind` 继续表达 hardware/imported/synthetic 等执行性质；后续 contract bump 用独立 `measurement_pipeline=active_measurement|imported_recording` 与 `finalization_state` 表达来源和生命周期，避免重载旧字段或破坏 2.0/3.0 历史读取。

现有 Canonical Event source 继续使用 `audio_signal`；Active/Recording 的区别由 owning artifact、measurement pipeline、producer 和 policy provenance 表达，不为了来源不同发明 `live_*` 事件类型或平行 taxonomy。

## 7. Barge-in boundary

普通 turn-taking 是第一闭环。单麦克风混音下，Energy VAD 无法可靠确定 tester 与旧 device response 重叠时的独立 stop boundary。没有 stimulus reference cancellation/AEC/loopback/source-aware evidence时，PRD-M005/M006/M007/M009 保持 `insufficient_evidence`。已有 Control VAD 可触发打断，但不能证明正式 Barge-in metric。

## 8. Implementation status at design convergence

| Capability | Status |
| --- | --- |
| Active Measurement Audio | planned；本轮仅完成 docs，列为下一实现阶段 |
| Stimulus Reference storage/interface | planned；第一阶段建立基础引用，不做伪 alignment |
| Stimulus Alignment | planned |
| Streaming Acoustic Measurement | planned |
| Canonical Live EventTimeline | planned |
| Unified Metric Engine | Recording Analysis implemented/partial；Active wiring planned |
| Measurement Equivalence | validation_pending |
| Advanced Barge-in | planned |
