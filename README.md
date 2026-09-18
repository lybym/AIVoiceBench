# AIVoiceBench

面向 AI 语音终端、AI 玩具和语音智能体的可复现、可追溯、可比较、可回归评测 Harness。

## 两条独立正式测量链

```text
Active Measurement                    Recording Analysis
Live Measurement Audio                External Recording
Online Event Producer                 Offline Event Producer
                  \                   /
                   Canonical EventTimeline
                              ↓
                    Canonical Metric Engine
                              ↓
                         MetricResult
```

- **Active Measurement**：平台主动播放测试语音，通过本地 Measurement Capture 采集现场声学信号，并逐步形成正式事件和指标。
- **Recording Analysis**：导入手机、录音笔或另一台电脑产生的独立 External Recording，执行离线声学与语义分析。

两条 Pipeline 不共享同一份 Audio Evidence；它们共享 Canonical Event 语义、指标定义、MetricResult 契约和版本化 Measurement Policy。External Recording 可用于独立复测、深度分析和 Measurement Equivalence 验证，但不是 Active Measurement 结果“转正”的前置条件。

## 当前实现边界

`v0.5.0` 在 `v0.4.0` 稳定版之上合入 #86（Audio QA 条件）、#89（录音 Artifact 链隔离）与 #90（外置配置 + File ASR transport），均 software_verified；`v0.4.0` 正式版本身收敛基于 `v0.4.0-alpha.6` 候选、语义角色归属与 main 的双正式测量 Pipeline 文档：

- Recording Analysis 已具备录音导入、标准化、部分声学/ASR/归属/融合，以及有足够角色证据时的 Timeline 和 canonical metrics。最新产品决定要求 tester/device 角色全部由用户人工确认，不再由 LLM 判断；人工确认与重分析 UI 尚未闭环，见 [#95](https://github.com/lybym/AIVoiceBench/issues/95)。
- 2026-09-18 的授权诊断录音已验证火山 Seed ASR 2.0 标准版可返回时间戳和匿名 speaker clusters，并暴露出异步任务恢复、部分转写传播与低音量设备声学对齐问题。修复候选与证据见 [真实录音诊断记录](docs/27-real-recording-diagnostic.md)；当前指标仍因声学片段缺少可用 speaker/人工角色对齐而弃权，不得据此声明 M1 或真实录音验收完成。
- Active Voice Test 已具备浏览器播放、Control RMS VAD、Streaming ASR、Fixed/Free 控制与 execution record 基础；当前 TTS 实现仍是火山 V3 HTTP SSE one-shot adapter。2026-09-18 已确定目标 TTS 路线：**Fixed 使用 V3 单向 WebSocket，先流式合成并冻结为固定 Stimulus Artifact 后再执行测试；Free 使用 V3 双向 WebSocket，形成 LLM Streaming → TTS Streaming → 浏览器流式播放链路**。该迁移由 [Issue #98](https://github.com/lybym/AIVoiceBench/issues/98) 跟踪，在实现与验证完成前不得写成 implemented。跨整次 Active Run 的 durable Measurement Audio、Stimulus Alignment、Streaming Acoustic Measurement、Canonical Live Timeline 和正式 Active MetricResult 仍为 planned。
- Browser Station 当前仍由 `aivoicebench/static/` 下的 HTML/CSS/Vanilla JS 实现；2026-09-17 已确定后续浏览器侧目标实现语言为 **TypeScript**，以约束 AudioWorklet、PCM frame/sample counter、WebSocket、Control VAD、capture integrity 与 Run state。该迁移由 [Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 跟踪；在代码、测试、浏览器和容器验证完成前不得写成 implemented。此次决策不要求 React/Vue 等框架重写。
- 软件验证、浏览器验证、容器验证、真实设备验证和 Measurement Equivalence 验证是不同状态。当前没有 `measurement_equivalence_verified` 声明。

## 先读文档

- [中心 PRD：产品范围、验收与实现状态](docs/PRD.md)
- [Active Measurement 技术边界](docs/25-active-measurement.md)
- [总体架构](docs/01-system-architecture.md)
- [Remote Browser Station 与开源组件策略](docs/26-remote-browser-component-strategy.md)
- [测试方法](docs/02-test-methodology.md)
- [指标定义](docs/03-metric-definition.md)
- [Development Roadmap](docs/04-development-roadmap.md)
- [2026-09-18 真实录音诊断与遗留问题](docs/27-real-recording-diagnostic.md)
- [完整文档导航](docs/README.md)

根目录 [AGENTS.md](AGENTS.md) 规定开发和状态声明规则。旧产品文件位于 `docs/product/archive/`，只作历史证据，不是当前要求。

## 交付形态

当前正式交付为 Docker 后端与前端，通过 Windows 浏览器访问；不要求 Windows EXE/安装包。Docker 内包含媒体处理依赖，运行数据通过持久卷保存。Browser Station 的 TypeScript 目标仍编译为普通浏览器 JavaScript 静态产物，由现有 FastAPI/Docker 交付链服务，不改变 `Linux Server + Docker Backend + Remote Chrome Browser Station` 的正式拓扑。`v0.5.0` 稳定版的启动命令、附件和 SHA-256 以 [Docker/API 文档](docs/20-docker-api.md) 与 [发布说明](docs/releases/0.5.0.md) 为准；`v0.5.0-alpha.1` 预览版仍保留为历史候选记录。

默认部署面向可信单用户 localhost。模型和语音服务凭据只由后端持有，不进入浏览器、Git、运行快照或报告；保存配置不等于服务连通或真实效果已经验证。2026-09-18 的目标配置进一步把 Judge/LLM、File ASR、Streaming ASR、TTS 的非敏感参数外置到服务器 `providers.yaml`，把对象存储参数外置到独立 `storage.yaml`，长期 secret 只保留 env/secret reference；该迁移由 [Issue #87](https://github.com/lybym/AIVoiceBench/issues/87) 跟踪并已由 PR #90 在 `v0.5.0` 实现（software_verified）；真实 TOS 路径待后续真实验收，SQLite/固定 Signed URL 仅作 legacy 兼容层。Active TTS 在此基础上进一步区分 **`tts`（完整文本→冻结/资产型 TTS）** 与 **`streaming_tts`（流式文本→流式音频）** 两类能力：火山目标协议分别为 V3 单向 WS 与 V3 双向 WS；**两条 TTS 链的输出格式统一固定为 MP3，不提供 format/encoding 配置项**。speaker/voice、sample rate、speech rate 以及协议实际支持的 loudness/pitch 等参数按官方 V3 字段外置配置并进入非敏感 Run snapshot，不支持的组合不得静默忽略（#98）。

## 验证原则

合成 fixture 用于验证契约、状态机和确定性公式，不能证明实体设备表现。正式结果必须回溯到独立声学 Artifact、sample-indexed timebase、Measurement Policy、Evidence、confidence/uncertainty 和处理器版本；证据不足时输出 `insufficient_evidence`，不能猜测声学边界、说话人或设备内部根因。
