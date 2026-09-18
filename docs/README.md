# 文档导航

2026-09-16 已在 `v0.4.0` 实现基线之上补充一轮架构决策：**Linux Server + Docker 后端、远端 Chrome Browser Station、现场生成音频 sample timebase** 成为正式主架构；VAD 近期采用 TEN VAD（浏览器控制）+ Silero VAD（服务器离线/最终化分析），Recording Analysis 优先使用火山 ASR 自带 speaker separation，当前不接 3D-Speaker，Evidence UI 采用 wavesurfer.js。代码实现和真实设备验收状态不因本轮文档决策自动升级。

2026-09-19 更新：[Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 的 Browser Station 等价迁移已完成到代码、测试与构建门禁：手写源码为 `web/src/*.ts`，`aivoicebench/static/*.js` 是编译产物，仍由 FastAPI/Docker 静态交付。类型约束只覆盖 AudioWorklet、PCM/sample timebase、WebSocket、Control VAD、capture integrity 和 Run state；不要求 React/Vue 等框架重写，也不改变 Linux Server + Docker、正式 Measurement semantics 或浏览器目标。远端 Chrome 与容器内的行为验证继续由既有 browser/container 测试与发布流程承担。

2026-09-18 配置与 File ASR transport 收敛：`v0.5.0` 已将 LLM/ASR/TTS Provider 与对象存储非敏感参数分别外置为服务器侧 `providers.yaml` / `storage.yaml`，长期 secret 只通过 env/secret reference 解析。File ASR 小文件可 inline Base64，大文件可走私有 TOS + 短期 Presigned GET；对象存储不是 Streaming ASR 或所有 File ASR 的强制依赖。该能力由 [Issue #87](https://github.com/lybym/AIVoiceBench/issues/87) / PR #90 实现并完成软件验证，真实服务质量仍需独立验收。

同日的授权诊断录音进一步验证了 Seed ASR 2.0 标准版的异步 submit/query 与匿名 speaker labels，并暴露部分时间戳、断线恢复和低音量设备对齐缺口。最新产品决定是不使用 LLM 判断角色，改由用户人工确认后再生成后续指标与正式报告；完整事实见 [真实录音诊断记录](27-real-recording-diagnostic.md)，任务由 [#93](https://github.com/lybym/AIVoiceBench/issues/93)、[#94](https://github.com/lybym/AIVoiceBench/issues/94) 和 [#95](https://github.com/lybym/AIVoiceBench/issues/95) 跟踪。

2026-09-18 Active TTS 路线进一步收敛：Fixed 目标使用火山 V3 WebSocket 单向流式接口生成并冻结 **MP3 Stimulus Artifact**，Free 目标使用 V3 WebSocket 双向流式接口承接 Streaming LLM 并向 Browser 流式播放 **MP3**；新增 `streaming_tts` 生命周期/route，与 Fixed 的 `tts` asset synthesis 分离。**TTS format/encoding 固定为 MP3，不作为可配置项**；音色、采样率、语速以及协议实际支持的音量/音调等必要参数按官方 V3 字段外置配置。该迁移由 [Issue #98](https://github.com/lybym/AIVoiceBench/issues/98) 跟踪，当前 HTTP SSE 实现状态不因此升级。

- **[产品需求 PRD](PRD.md)**：唯一产品入口，维护边界、全局原则、需求目录和正式里程碑；详细功能/指标/验收/追踪/历史见 [PRD 分册导航](prd/README.md)。
- **[Remote Browser Station 与开源组件策略](26-remote-browser-component-strategy.md)**：2026-09-16/17 的部署、Browser Station TypeScript 目标、VAD、speaker separation、wavesurfer.js 与 Windows Native 边界决策。
- [产品文档中心](product/README.md)：旧来源归集映射、冲突处置、集中历史快照。
- [系统架构](01-system-architecture.md)：技术边界与目标设计，包括 Browser Station 的 TypeScript 源码 / 编译产物交付边界。
- [测试方法](02-test-methodology.md)、[指标定义](03-metric-definition.md)、[契约版本](07-contract-versions.md)：验证和数据约束。
- [Roadmap](04-development-roadmap.md)：PRD 编号到 Issue 的执行映射；Browser Station TypeScript 等价迁移由 #84 跟踪。
- [Context](05-project-context.md)：决策来源与工作区上下文；[Work log](06-work-log.md)：实际执行记录。
- [Model management](16-model-management.md)：Provider/Storage 外置配置、secret boundary、File ASR transport，以及 `tts` / `streaming_tts` 的 V3 WebSocket 生命周期分离；[Recording import](14-recording-import.md)、[Docker/API](20-docker-api.md)：运行方式与接口说明。
- [声学分段](17-acoustic-segmentation.md)：Acoustic Boundary Provider、Silero/TEN/RMS 的职责边界。
- [Streaming ASR 边界](24-streaming-asr.md)：Active Voice Test 的实时识别边界、事件模型、二进制音频通道与火山契约；Recording Analysis 的 File ASR 见 [时间戳 ASR](11-timestamped-asr.md)。
- [Active Measurement](25-active-measurement.md)：持续 Measurement Audio、sample clock、Stimulus Reference、在线声学处理、Canonical Timeline 和统一指标边界，以及 Browser Station TypeScript 迁移的非行为变更约束。
- [Recording Backbone](23-recording-backbone.md)：火山 File ASR、ASR-native speaker labels、Attribution/Fusion 主链与审计边界。
- [真实录音诊断记录](27-real-recording-diagnostic.md)：2026-09-18 实测问题、已修复候选、空白指标根因、Issue 与验收边界。

根目录 [AGENTS.md](../AGENTS.md) 规定开发者如何读取与同步 PRD。技术专题文件不是额外 PRD，不以旧例子或工作日志替代用户需求。
