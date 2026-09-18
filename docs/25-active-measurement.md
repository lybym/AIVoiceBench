# Active Measurement — Measurement Audio 与正式结果基础

PRD refs: PRD-F023/F025/F026、PRD-M001–M010、PRD-N002/N003/N007。本文描述目标架构与分阶段实现边界；未标为 implemented 的能力不得按完成报告。

2026-09-16 决策：正式主架构采用 **Linux Server + Docker Backend + Remote Chrome Browser Station**。Browser Station 现场生成音频 sample timebase；服务器可以远端计算，网络 RTT 不进入正式声学指标。VAD 近期分工为 TEN VAD（Browser Control）+ Silero VAD（Server finalized replay）。详见 [组件策略](26-remote-browser-component-strategy.md)。

2026-09-17 补充实现决策：Browser Station 当前仍是 HTML/CSS/Vanilla JS，目标实现语言改为 **TypeScript**，迁移由 [Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 跟踪。该迁移只强化浏览器侧音频帧、sample timebase、WebSocket、Control VAD、capture integrity 与状态机的类型约束，不改变本文件定义的 Measurement semantics，也不要求 React/Vue 等框架重写。

2026-09-18 补充 Active TTS 决策：Fixed 使用火山 V3 WebSocket **单向流式**完成完整话术的 asset synthesis，并在正式 Run 前冻结 Stimulus；Free 使用火山 V3 WebSocket **双向流式**承接 Streaming LLM text 并向 Browser 流式输出音频。实现由 [Issue #98](https://github.com/lybym/AIVoiceBench/issues/98) 跟踪；当前 V3 HTTP SSE adapter 仍是实现事实，文档目标不得被误写成已完成。

## 1. Pipeline boundary

```text
Remote Chrome Browser Station
(TypeScript target / current Vanilla JS)
Browser Microphone
       ↓ one physical PCM source + local sample counter
 ┌─────┴────────────────────────────────────┐
 │ Control Plane                            │ Measurement Plane
 │ TEN VAD target / RMS fallback            │ durable Measurement Audio
 │ Streaming ASR / playback / timeout / UI  │ frame sequence / integrity
 │ execution-record.json                    │ local sample timebase
 └──────────────────────────────────────────┘
                     │ HTTPS / WebSocket
                     ▼
             Linux Server + Docker
                     │
      ┌──────────────┴──────────────┐
      │ orchestration / providers   │
      │ Silero finalized replay     │
      │ EventTimeline / MetricResult│
      └─────────────────────────────┘
```

Active Measurement 与 Recording Analysis 都是正式 Measurement Pipeline。前者的声学原件固定标识为 `ART-live-measurement-audio-*`，后者为 `ART-external-recording-*`；不得把同一音频绕行 Recording Analysis 后称为 Active Result，也不得把 execution record 改名为 Timeline。

## 1.1 Active TTS transport contract

TTS 是 Active Control/Stimulus Plane 的 Provider，不是正式声学时间源。

### Fixed / asset synthesis

```text
complete fixed text
→ backend `tts` route
→ Volcengine V3 unidirectional WebSocket
→ MP3 audio chunks
→ validate
→ immutable MP3 Stimulus Artifact
→ formal Browser playback
```

固定用例的 WebSocket 合成发生在准备阶段。正式 Run 必须引用已经冻结的 stimulus identity、SHA-256、sample rate/channels/encoding/sample count 和 non-secret provider/config snapshot。重新执行同一 Case 不得因为启动 Run 而静默重新合成或替换该资产。

Active TTS 媒体格式统一固定为 **MP3**。Fixed 直接校验并冻结 Provider 返回的 MP3，不再引入 PCM/WAV 封装或转码层；`providers.yaml` 也不提供 TTS format/encoding 配置项。

### Free / streaming synthesis

```text
Streaming ASR final Observation
→ Streaming LLM
→ ordered speakable text chunks
→ backend `streaming_tts` route
→ Volcengine V3 bidirectional WebSocket
→ MP3 audio chunks
→ Browser streaming playback
```

双向 TTS session 绑定一个 Run/Turn；text/audio chunk 顺序、finish/cancel/close 与 stale ownership 必须明确。用户 Stop、Turn 改变、provider failure 或未来 Barge-in cancel 之后，迟到音频只能作为诊断被丢弃，不能继续播放或进入下一 Turn。

### Configuration and evidence boundary

两类 TTS 都从 server-owned `providers.yaml` 获取 non-secret 参数，但**format/encoding 固定为 MP3，不允许配置**。仅保留 speaker/voice、sample rate、speech rate，以及所选协议/音色官方实际支持的 loudness/pitch 等必要调整项。unsupported 组合显式失败，不静默忽略。2026-09-18 的官方单向 V3 文档仍标注音高调节暂不支持，因此 Fixed 单向 profile 不得默认宣称 pitch capability；若官方后续改变，validator、capability matrix 与本文必须同步更新。Credential 只在 Backend 解析，Browser 不直连 Provider。

协议依据：[V3 单向 WebSocket](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-websocket?lang=zh)；[V3 双向 WebSocket](https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket?lang=zh)。

LLM chunk 时间、TTS provider event、audio chunk arrival、Browser playback callback 都可记录为 Control/Provider diagnostics；它们不能替代本文件定义的 Browser sample clock、Stimulus Alignment 或正式 Measurement Audio boundary。

## 2. Remote Browser Station contract

Browser Station 是现场 audio I/O endpoint，不是业务后端。当前浏览器源码位于 `aivoicebench/static/`，手写 JS 入口包括 `app.js`、`models.js`、`voice_test.js` 和 `pcm_capture_worklet.js`；这些入口迁移到 TypeScript 后，编译产物仍须由现有 FastAPI/Docker 静态交付链服务，浏览器运行时仍执行普通 JavaScript。

TypeScript 迁移必须保持当前 HTTP/WebSocket contract、PCM framing、Run/Turn identity、控制语义和 `sample_index / sample_rate` 时间基兼容。迁移完成前当前 Vanilla JS 仍是实现事实；类型语言决策不能自动升级任何 Active Measurement capability 状态。

每次 Session/Run 至少记录：

```text
station_id
browser / browser_version
os
input device metadata
output device metadata
requested media constraints
actual MediaTrackSettings
sample_rate / channels / encoding
AudioContext state
frame size / sequence
sample counter
capture start/end reason
```

`echoCancellation`、`noiseSuppression`、`autoGainControl` 等必须同时记录 requested 与 actual；不能假定浏览器一定按请求生效。

远端正式部署使用 HTTPS/WSS 以满足浏览器 secure-context 与麦克风权限要求。Provider secret 只存在于 Linux Server。

浏览器侧高风险对象应在 TypeScript 中具有显式类型，包括 audio frame/sequence/sample index、AudioWorklet message、VAD/control observation、capture integrity、MediaTrackSettings、WebSocket lifecycle 与 Fixed/Free Run state；类型约束不得成为新的事实来源，正式 Evidence 与时间语义仍由既有契约决定。

## 3. Measurement Audio contract

每次 Active Run 在第一句播放前开始持续采集，到完成、用户停止、失败或连接中断为止。基础 Artifact 使用 PCM16LE / 16 kHz / mono WAV，并记录 artifact/run/session/station identity、source、path、SHA-256、sample rate/channels/encoding/bit depth、sample count、captured sample count、gap-filled sample count、capture start/end、frame size/count、dropped/duplicate/gap/stale frames、浏览器实际设备设置、结束原因和 `measurement_policy_version`。

传输帧使用固定长度 `[4B big-endian sequence][PCM16LE]`。序号按“浏览器产生的帧”递增，而不是只按成功发送的帧递增，使背压/断网丢帧在下一已收帧处表现为 gap。服务端拒绝奇数字节、非固定 frame size 和错误格式；duplicate/stale frame 不重复写入。缺口用显式零样本填充以维持 sample clock，并同时记录 gap frame/sample 数；含 gap 的 Artifact 可保存审计，但是否可用于某个指标由 Measurement Policy 决定。

网络排队不会改变 `sample_index`。即使服务器晚到数百毫秒收到 frame，正式声学边界仍按 Browser Station 原始 sample timeline 解释。

## 4. Time bases

| Clock | 用途 | 能否直接成为正式声学边界 |
| --- | --- | --- |
| browser audio sample clock | `sample_index / sample_rate`，Event/Evidence 的 `audio_relative_ms` | **是，首选** |
| client monotonic | 浏览器调度、挂起/恢复诊断 | 否 |
| server monotonic | 会话、处理和 transport latency 诊断 | 否 |
| wall clock | 审计、跨 Artifact 粗关联 | 否 |
| WebSocket receive time | transport/jitter 诊断 | 否 |
| ASR provider timestamp | 文字/语义与 provider timing evidence | 否，不自动成为 acoustic truth |

核心原则：**计算可以远，音频时间轴必须在现场生成。**

## 5. VAD / Acoustic Boundary 分工

### 5.1 Browser Control — TEN VAD

目标实现使用 TEN VAD Web/WASM 在 Browser Station 产生低延迟 provisional/control observation：

```text
speech_suspected_start
speech_suspected_end
```

每个 observation 必须保存 local sample index、source=`ten_vad`、policy/model version 与必要诊断。现有 RMS VAD 继续显式保留为 fallback/debug；事件 source 不得混淆。

TEN VAD 的正式引入还受其许可证附加条件审查约束；文档选型不等于 production dependency accepted。

### 5.2 Server Finalization — Silero VAD

Linux Server 对 durable Measurement Audio 做 finalized replay，首个模型型 baseline 使用 Silero VAD。它输出正式候选 acoustic boundaries，继续携带 confidence/uncertainty/processor/policy provenance。

TEN provisional 与 Silero finalized 不一致时保留冲突，不静默覆盖。未来可以做 TEN replay 与 Silero 对照，但不创建两套 Metric 公式。

### 5.3 VAD 不是 ASR 前置

VAD 与 Streaming ASR 并行：VAD 提供 acoustic/control boundary evidence，ASR 提供 text/semantic/provider endpoint evidence。ASR 可以处理连续流，不要求先由 VAD 切成文件段。

## 6. Stimulus Reference interface

每个实际播放资产保存 immutable reference：artifact ID、turn/playback identity、相对路径、SHA-256、sample rate、channels、encoding、sample count。`playback_started/ended` 只提供 alignment 搜索窗口先验。

后续 `StimulusAligner` 接口接收 Measurement Audio + Stimulus Reference + search window，输出 match interval、correlation/confidence、uncertainty、processor/policy version 和 evidence refs。弱匹配、多重匹配、截断或 capture gap 覆盖关键区域时弃权；绝不把 callback 时间直接输出成 `tester_speech_start/end`。

## 7. Canonical events and metrics

Online Event Producer 与 Offline Event Producer 都输出现有 Canonical Event 类型。Active 使用已对齐 stimulus 形成 tester evidence，剩余 speech 仅作 device candidate；unknown 不强制分配。每个正式事件引用 Measurement Audio Evidence，并记录 `audio_relative_ms`、confidence、uncertainty、producer 和 policy version。

两个 Pipeline 都调用 `compute_timeline_metrics(...)`；同一 canonical fixture 必须得到相同指标数值。`execution_kind` 继续表达 hardware/imported/synthetic 等执行性质；后续 contract bump 用独立 `measurement_pipeline=active_measurement|imported_recording` 与 `finalization_state` 表达来源和生命周期。

## 8. Barge-in boundary

普通 turn-taking 是第一闭环。单麦克风混音下，普通 VAD 无法可靠确定 tester 与旧 device response 重叠时的独立 stop boundary。没有 stimulus reference cancellation/AEC/loopback/source-aware evidence 时，PRD-M005/M006/M007/M009 保持 `insufficient_evidence`。已有 Control VAD 可触发打断，但不能证明正式 Barge-in metric。

## 9. Windows Native / Audio Station 边界

当前没有必要把 AIVoiceBench 直接运行在 Windows。原生 Windows/WASAPI 的价值仅在未来出现专业 HIL 需求时：loopback、专用声卡、多通道、低层 driver timestamp、USB/串口等。

未来形态应是：

```text
Linux AIVoiceBench Server
        ↕
Browser Station 或可选 Windows Audio Station Agent
```

而不是迁移整个 Server。

## 10. Implementation status at design convergence

| Capability | Status |
| --- | --- |
| Remote Browser Station architecture | decided；实现/远端真实部署验收 pending |
| Browser Station TypeScript migration | planned；当前 Vanilla JS；Issue #84 |
| Fixed V3 unidirectional WS TTS + frozen stimulus | planned；当前 V3 HTTP SSE；Issue #98 |
| Free Streaming LLM → V3 bidirectional WS TTS → streaming playback | planned；Issue #98 |
| TEN VAD Browser Adapter | planned；RMS fallback 已有 |
| Active Measurement Audio | planned |
| Stimulus Reference storage/interface | planned |
| Stimulus Alignment | planned |
| Silero finalized Acoustic Provider | planned |
| Canonical Live EventTimeline | planned |
| Unified Metric Engine | Recording Analysis implemented/partial；Active wiring planned |
| Measurement Equivalence | validation_pending |
| Advanced Barge-in | planned |