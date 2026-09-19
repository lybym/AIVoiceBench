# Remote Browser Station 与开源组件策略

> Owner decision / architecture decision，2026-09-16；2026-09-17 补充 Browser Station TypeScript 实现语言决策。本文是技术专题，不创建新的产品 Requirement ID；产品边界仍以 [PRD](PRD.md) 为准。

## 1. 决策摘要

AIVoiceBench 的正式主架构确定为：

```text
Remote Chrome Browser Station
├─ TypeScript source / compiled browser JS
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

### 2.1 Browser Station 实现语言：TypeScript

2026-09-17 决定将 Browser Station 的实现语言收敛为 **TypeScript**，2026-09-19 由 [Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 完成等价迁移：手写源码为 `web/src/{app,models,voice_test,workbench,pcm_capture_worklet}.ts`（AudioWorklet 全局作用域声明见 `audioworklet-globals.d.ts`；`workbench.ts` 由 #11 加入，是 Evidence Workbench 渲染层），编译产物 `aivoicebench/static/*.js` 仍由现有 FastAPI/Docker 静态链交付，`index.html`/`app.css` 仍是手写资产，第三方浏览器库见 `aivoicebench/static/vendor/`。

这项迁移的目的不是更换 UI 框架，而是让 Browser Station 作为 Measurement Agent 时的高风险状态具有静态类型约束，重点包括：

- audio frame：`sequence`、`sampleIndex`、`sampleRate`、`channelCount` 与 payload；
- AudioWorklet 主线程/处理线程消息；
- VAD/control observation 的 type、source 与 sample index；
- capture integrity 的 gap、duplicate、stale、dropped 计数与状态；
- requested media constraints 与实际 `MediaTrackSettings`；
- WebSocket/control lifecycle；
- Fixed/Free Voice Test 的 Run/Turn 与停止、超时、断连状态。

迁移约束：

1. **行为等价优先。** TypeScript 迁移不得顺带改变 HTTP/WebSocket contract、PCM framing、Run/Turn identity、Measurement semantics 或 `sample_index / sample_rate` 时间基。
2. **仍交付普通浏览器 JavaScript。** TypeScript 只作为源码与构建/类型检查层，编译产物继续由现有 FastAPI/Docker 静态交付链服务，目标浏览器仍是 Chrome。
3. **不要求框架重写。** 本决策不引入 React、Vue、Svelte 等 UI 框架要求；可以保持当前 DOM/CSS 结构并完成渐进式迁移。
4. **避免双源。** 迁移完成后不得长期同时维护同一业务逻辑的手写 `.js` 与 `.ts` 两套源码。
5. **类型检查进入验证门禁。** Browser Station 后续变更至少应通过 deterministic build/typecheck，并继续通过既有 browser/container 行为测试。

因此 TypeScript 是 Browser Station 的工程实现约束，不是新的 Measurement truth，也不改变 PRD requirement 语义。

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
| Recording Analysis / server-side finalized boundary | **Silero VAD** | 对持久音频生成可重放、可版本化的 speech boundary evidence；server-side ONNX Adapter 已实现（#23，`aivoicebench/silero_vad.py`） |
| Active Measurement finalized replay | **Silero VAD** 为首个基线；TEN replay 可作为后续对照 | 在 Linux Server 对 durable Measurement Audio 重算正式候选边界；Adapter/Policy 已具备，durable Measurement Audio 接线仍待实现 |
| Synthetic fixture / emergency fallback | 现有 RMS / `EnergyVadSegmenter` | 测试、debug、兼容，不再作为长期正式默认算法；**只能被显式选择**，不会被模型 provider 静默回退 |

TEN VAD 当前公开 Web/WASM 路径，适合 Browser Station；Silero VAD 支持 ONNX，适合 Linux Server 的可重复离线/准实时处理。阈值和 post-processing 必须由 AIVoiceBench 的版本化 Measurement Policy 管理，不能照搬项目默认值后直接宣称测量准确。Silero 的首个策略 `silero_boundary_policy/1.0.0` 与权重 sha256 记录见 [声学分段](17-acoustic-segmentation.md) §2.2/§3.2。

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

**当前实现状态（Issue #11）。** vendored 集成已经落地：固定 wavesurfer.js **7.12.12**（BSD-3-Clause，npm tarball sha1 `f402d88f56091d09e045c98f48075859b37719dd`），逐字节复制到 `aivoicebench/static/vendor/`，由 `vendor/VENDOR.json` 记录每个文件的 sha256；浏览器只从同源 `/static/vendor/...` 惰性加载 core → regions → timeline（≥5 分钟录音再启用 minimap），不使用 CDN，也不在运行时请求任何第三方库。渲染层是 `web/src/workbench.ts` 编译出的 `workbench.js`（`window.WB`），Region 直接使用后端 `regions[].start_sec`/`end_sec` 且禁用 drag/resize，`unknown`/`uncertain`/`provisional` 区间使用独立配色，指标值原样显示（`null` → `N/A`），角色依赖证据不可用时只渲染 provisional 横幅与后端 `unavailable` 列表。确定性门禁见 `scripts/verify-workbench-render.mjs`（已接入 `npm run verify`，随 CI 的 `contracts` job 执行；并支持 `--document <workbench.json>` 用真实 `build_workbench()` 输出复跑渲染不变量）、`scripts/smoke-web-station.mjs` 与 `tests/test_web_workbench.py`。转写行只在后端发布了指向已发布 region 的 `region_id` 时可导航：该关联由后端从 fused segment 的 `asr_segment_id`→`acoustic_segment_id` 交叉引用解析（ASR utterance id 与声学片段 id 是独立命名空间），前端不按 `detail.segment_id` 同名匹配。

**仍未验证。** Docker 镜像内真实浏览器中的波形渲染、seek/zoom、Regions/Timeline 交互与 Minimap 启用尚未验收（由 #85 承载）。因此本组件状态是“vendor 与投影集成已实现（software/artifact 级证据）”，不是完成的浏览器验收，也不构成 `real_recording_verified`。

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
Browser Station (TypeScript source)
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
- React/Vue/Svelte 等前端框架重写；
- 3D-Speaker 集成；
- 自研 waveform renderer；
- 让 ASR endpoint 或 server receive time 直接成为正式 acoustic boundary；
- 为了追求低对话时延引入 RTC 作为唯一底座；
- 在没有真实目标录音标注集前宣称 Silero/TEN 谁是“测量真值”。

TypeScript 迁移本身不是 UI 框架升级，也不扩大 Browser Station 的产品职责。

## 9. 验收顺序

1. ✅ 完成 Browser Station Vanilla JS → TypeScript 等价迁移：typecheck/build 可重复（`scripts/verify-web-build.mjs` 校验类型与产物新鲜度），Docker 静态交付路径不变，AudioWorklet/WebSocket/Fixed-Free 行为回归由既有 browser/container 测试承担；
2. Browser Station sample clock / frame integrity 稳定；
3. TEN VAD Browser Adapter 与 RMS fallback 并存并可观测；
4. Silero VAD Server Adapter 接入 Recording Analysis，并能对 Measurement Audio replay；
5. 火山 speaker separation 请求/响应契约完成真实服务验证；
6. 🟡 wavesurfer.js Evidence UI 集成已落地（vendor 7.12.12、同源 `/static/vendor/`、`web/src/workbench.ts` → `workbench.js`，Finding/Metric/Event/转写 → Region seek + highlight + 证据面板同步）；**真实浏览器 Docker 验收未完成**（#85），尚不能判定“可由 Finding/Event/Metric 定位音频”已验收；
7. 使用目标 AI 玩具真实录音建立人工标注集，比较 boundary error、speaker coverage/abstention 和最终指标稳定性；
8. 达不到门槛时再引入额外 diarization/source-separation 组件。
