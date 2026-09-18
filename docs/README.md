# 文档导航

2026-09-16 已在 `v0.4.0` 实现基线之上补充一轮架构决策：**Linux Server + Docker 后端、远端 Chrome Browser Station、现场生成音频 sample timebase** 成为正式主架构；VAD 近期采用 TEN VAD（浏览器控制）+ Silero VAD（服务器离线/最终化分析），Recording Analysis 优先使用火山 ASR 自带 speaker separation，当前不接 3D-Speaker，Evidence UI 采用 wavesurfer.js。代码实现和真实设备验收状态不因本轮文档决策自动升级。

2026-09-17 进一步收敛 Browser Station 的实现语言：**现有 HTML/CSS/Vanilla JS 是当前实现基线，目标实现语言改为 TypeScript**。迁移只用于强化 AudioWorklet、PCM/sample timebase、WebSocket、Control VAD、capture integrity 和 Run state 的静态类型约束；不要求 React/Vue 等框架重写，也不改变 Linux Server + Docker、FastAPI 静态交付、正式 Measurement semantics 或浏览器目标。实现由 [Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 跟踪，完成前保持 planned。

2026-09-18 配置与 File ASR transport 收敛：目标把 LLM/ASR/TTS Provider 与对象存储非敏感参数分别外置为服务器侧 `providers.yaml` / `storage.yaml`，长期 secret 只通过 env/secret reference 解析。Recording Analysis P0 继续使用火山 File ASR 极速版 HTTP：小文件 inline Base64，大文件私有 TOS + 短期 Presigned GET；对象存储不是 Streaming ASR 或所有 File ASR 的强制依赖。实现由 [Issue #87](https://github.com/lybym/AIVoiceBench/issues/87) 跟踪。

- **[产品需求 PRD](PRD.md)**：唯一产品入口，维护边界、全局原则、需求目录和正式里程碑；详细功能/指标/验收/追踪/历史见 [PRD 分册导航](prd/README.md)。
- **[Remote Browser Station 与开源组件策略](26-remote-browser-component-strategy.md)**：2026-09-16/17 的部署、Browser Station TypeScript 目标、VAD、speaker separation、wavesurfer.js 与 Windows Native 边界决策。
- [产品文档中心](product/README.md)：旧来源归集映射、冲突处置、集中历史快照。
- [系统架构](01-system-architecture.md)：技术边界与目标设计，包括 Browser Station 当前 Vanilla JS / 目标 TypeScript 的实现边界。
- [测试方法](02-test-methodology.md)、[指标定义](03-metric-definition.md)、[契约版本](07-contract-versions.md)：验证和数据约束。
- [Roadmap](04-development-roadmap.md)：PRD 编号到 Issue 的执行映射；Browser Station TypeScript 等价迁移由 #84 跟踪。
- [Context](05-project-context.md)：决策来源与工作区上下文；[Work log](06-work-log.md)：实际执行记录。
- [Model management](16-model-management.md)：Provider/Storage 外置配置、secret boundary 与 File ASR transport 目标；[Recording import](14-recording-import.md)、[Docker/API](20-docker-api.md)：运行方式与接口说明。
- [声学分段](17-acoustic-segmentation.md)：Acoustic Boundary Provider、Silero/TEN/RMS 的职责边界。
- [Streaming ASR 边界](24-streaming-asr.md)：Active Voice Test 的实时识别边界、事件模型、二进制音频通道与火山契约；Recording Analysis 的 File ASR 见 [时间戳 ASR](11-timestamped-asr.md)。
- [Active Measurement](25-active-measurement.md)：持续 Measurement Audio、sample clock、Stimulus Reference、在线声学处理、Canonical Timeline 和统一指标边界，以及 Browser Station TypeScript 迁移的非行为变更约束。
- [Recording Backbone](23-recording-backbone.md)：火山 File ASR、ASR-native speaker labels、Attribution/Fusion 主链与审计边界。

根目录 [AGENTS.md](../AGENTS.md) 规定开发者如何读取与同步 PRD。技术专题文件不是额外 PRD，不以旧例子或工作日志替代用户需求。