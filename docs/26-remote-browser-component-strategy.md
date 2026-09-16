# Remote Browser Station 与开源组件策略

> Owner decision / architecture decision，2026-09-16。本文是技术专题，不创建新的产品 Requirement ID；产品边界仍以 [PRD](PRD.md) 为准。

## 1. 决策摘要

AIVoiceBench 的正式主架构确定为：

```text
Remote Chrome Browser Station
├─ local audio playback / microphone capture
├─ AudioWorklet / local sample counter
├─ provisional control VAD
└─ UI / evidence interaction

        HTTPS / WebSocket
                ↓

Linux Server + Docker
├─ FastAPI / orchestration
├─ ASR / speaker separation
├─ durable audio / artifacts
├─ acoustic analysis / fusion
├─ metrics / Judge / findings
└─ report / persistence
```

**不把“直接运行在 Windows”作为正式交付方向。** Windows Native 只在未来专业 HIL 需要 WASAPI、loopback、专用声卡、USB/串口等低层能力时，作为独立 Station Agent 扩展，而不是迁移整个后端。

核心原则：

> **计算可以远，音频时间轴必须在现场生成。**

网络 RTT、WebSocket 排队、服务器调度和 ASR/LLM 返回延迟可以影响控制体验，但不得污染正式声学指标。正式时间基优先来自 Browser Station 本地音频 sample index：

```text
audio_relative_ms = sample_index * 1000 / sample_rate
```

`server_receive_time`、wall clock、client/server monotonic clock 只用于传输、调度和审计诊断。

## 2. Browser Station 责任

Browser Station 是远端测试现场的轻量执行端，而不是业务后端。近期目标浏览器仍以 Chrome 为主。

Browser Station 负责：

- `getUserMedia` 麦克风采集；
- 浏览器扬声器播放测试 stimulus；
- `AudioWorklet` 连续 PCM；
- 本地 sample counter 与 frame sequence；
- 本地控制 VAD；
- capture integrity / dropped-gap-duplicate frame 观察；
- 实际 `MediaTrackSettings`、requested constraints、浏览器/OS/音频设备快照；
- 断网期间的有限 buffer 与失败状态；
- Waveform / Timeline / Evidence 的交互展示。

Browser Station 不保存长期 Provider Credential，不负责 Judge、Metric 公式或最终报告。

远端访问必须满足浏览器安全上下文要求；正式部署应使用 HTTPS/WSS。AEC、Noise Suppression、AGC 等浏览器音频增强的 requested/actual setting 必须进入 Run provenance，不能默认为“关闭成功”。

## 3. Linux Server 责任

Linux Server + Docker 是 AIVoiceBench 的正式计算与持久化环境：

- Run / Session / TestCase 编排；
- File/Streaming ASR Provider；
- 录音与 Live Measurement Audio 持久化；
- Acoustic Boundary、Attribution、Fusion；
- Canonical EventTimeline 与唯一 Metric Engine；
- LLM Judge / Findings；
- Artifact、Evidence、Revision、Report；
- 多 Browser Station 集中管理。

系统不以低对话 RTT 为主要优化目标，因此 ASR、LLM、最终化声学处理可以运行在远端 Linux Server；只要正式事件继续使用现场音频时间轴，网络延迟不进入 PRD-M 指标。

## 4. VAD 组件选择

VAD 在 AIVoiceBench 中是 **Acoustic Boundary Evidence Producer**，不是所有 ASR 的前置步骤。ASR、VAD、speaker separation 应作为并行 Evidence Producer，而不是强制串成 `VAD → ASR → diarization`。

近期组件分工固定为：

| 场景 | 首选 | 角色 |
| --- | --- | --- |
| Browser Station / Active Control | **TEN VAD** | 低延迟 `speech_suspected_start/end`、轮次推进、timeout 辅助；事件使用本地 sample index |
| Recording Analysis / server-side finalized boundary | **Silero VAD** | 对持久音频生成可重放、可版本化的 speech boundary evidence |
| Active Measurement finalized replay | **Silero VAD** 为首个基线；TEN replay 可作为后续对照 | 在 Linux Server 对 durable Measurement Audio 重算正式候选边界 |
| Synthetic fixture / emergency fallback | 现有 RMS / `EnergyVadSegmenter` | 测试、debug、兼容，不再作为长期正式默认算法 |

TEN VAD 当前公开 Web/WASM 路径，适合 Browser Station；Silero VAD 支持 ONNX，适合 Linux Server 的可重复离线/准实时处理。阈值和 post-processing 必须由 AIVoiceBench 的版本化 Measurement Policy 管理，不能照搬项目默认值后直接宣称测量准确。

依赖许可证需单独审查：Silero VAD 为 MIT；TEN VAD 虽基于 Apache 2.0，但仓库 LICENSE 还包含额外部署限制。引入 TEN VAD 到正式商业/分发路径前必须完成许可证适配确认；未确认前可以保留 Adapter/Spike，不得把依赖选择视为完成验收。

- Silero VAD: https://github.com/snakers4/silero-vad
- TEN VAD: https://github.com/TEN-framework/ten-vad

## 5. Speaker separation 选择

当前阶段 **不接 3D-Speaker**。

Recording Analysis 优先把现有火山 ASR 的自动说话人分离能力用完整：

```text
External Recording
       ↓
Volcengine File ASR
       ├─ transcript / timestamps
       └─ speaker labels / clusters
                    ↓
AIVoiceBench Attribution
speaker_0 / speaker_1 / ...
                    ↓
explicit evidence / semantic proposal / human review
                    ↓
tester / device / unknown
```

火山官方当前产品能力说明包含自动说话人分离；但 **provider speaker ID 只表示匿名 speaker cluster，不等于 tester/device**。必须继续经过 Attribution 层。缺少 label、低质量、冲突或角色证据不足时输出 `unknown` / `needs_review`，不能按“第一个说话人就是 tester”猜角色。

只有在以下条件之一出现后，再评估 3D-Speaker / pyannote 等独立 diarization Provider：

- 火山 speaker separation 在目标 AI 玩具录音上的 coverage/accuracy 不足；
- 需要完全离线/供应商无关 diarization；
- 需要独立 overlap/source-aware 能力；
- Benchmark 需要第二个 diarization reference。

## 6. Waveform / Evidence UI

Web Evidence Workbench 采用 **wavesurfer.js**，不自行实现 waveform renderer。

首期使用：

- Waveform core；
- Regions：渲染 tester/device/unknown、event、finding evidence 区间；
- Timeline：统一毫秒/秒时间轴；
- Minimap：长录音快速导航，可按实际需要启用。

目标交互：

```text
Finding / Metric / Event
          ↓ click
wavesurfer seek + zoom
          ↓
highlight Evidence Region
          ↓
同步 transcript / speaker / provenance
```

wavesurfer.js 只负责显示和交互，不产生 Measurement Event，也不能成为正式时间真值。Region 必须来自 AIVoiceBench Artifact/Evidence 的 `audio_relative_ms`。

- wavesurfer.js: https://github.com/katspaugh/wavesurfer.js

## 7. 推荐数据流

### Recording Analysis

```text
External Recording
   ├─ Silero VAD ───────────────→ acoustic boundaries
   ├─ Volcengine File ASR ──────→ text / timestamp / speaker labels
   └─ optional semantic evidence
                     ↓
             Attribution + Fusion
                     ↓
           Canonical EventTimeline
                     ↓
             Canonical Metrics
                     ↓
      wavesurfer.js Evidence Workbench
```

### Active Measurement

```text
Browser Station
Mic → AudioWorklet → sample-indexed PCM
       ├─ TEN VAD → provisional control events
       └─ WebSocket → Linux Server → durable Measurement Audio
                                      ↓
                              Silero finalized replay
                                      ↓
                              Canonical EventTimeline
                                      ↓
                               Canonical Metrics
```

这种分工允许控制链保持及时，同时使最终指标可以脱离网络 RTT 重放和审计。

## 8. 当前非目标

当前迭代不做：

- 把整个 AIVoiceBench 迁到 Windows Native；
- 3D-Speaker 集成；
- 自研 waveform renderer；
- 让 ASR endpoint 或 server receive time 直接成为正式 acoustic boundary；
- 为了追求低对话时延引入 RTC 作为唯一底座；
- 在没有真实目标录音标注集前宣称 Silero/TEN 谁是“测量真值”。

## 9. 验收顺序

1. Browser Station sample clock / frame integrity 稳定；
2. TEN VAD Browser Adapter 与 RMS fallback 并存并可观测；
3. Silero VAD Server Adapter 接入 Recording Analysis，并能对 Measurement Audio replay；
4. 火山 speaker separation 请求/响应契约完成真实服务验证；
5. wavesurfer.js Evidence UI 可由 Finding/Event/Metric 定位音频；
6. 使用目标 AI 玩具真实录音建立人工标注集，比较 boundary error、speaker coverage/abstention 和最终指标稳定性；
7. 达不到门槛时再引入额外 diarization/source-separation 组件。
