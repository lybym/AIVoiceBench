# 文档导航

2026-09-13 已按 main 与 `v0.4.0-alpha.6` 候选基线核对。产品现定义 Active Measurement 与 Recording Analysis 两条独立正式 Measurement Pipeline；alpha.6 包含 Streaming ASR、完整 `max_turns` 轮次和迟到转写隔离修复。Active Measurement Audio 基础、完整 M1、真实物理设备声学测量、Recording Analysis 正式验收与 Measurement Equivalence 验证仍待完成。先看 [v0.4.0-alpha.6 发布说明](releases/0.4.0-alpha.6.md)、[Active Measurement](25-active-measurement.md)、[Streaming ASR 边界](24-streaming-asr.md)、[总体架构](01-system-architecture.md)、[Roadmap](04-development-roadmap.md) 和 PRD 的实现状态。

- **[产品需求 PRD](PRD.md)**：审阅/修改功能、优先级、验收和代码实现标识。
- [产品文档中心](product/README.md)：旧来源归集映射、冲突处置、集中历史快照。
- [系统架构](01-system-architecture.md)：技术边界与目标设计。
- [测试方法](02-test-methodology.md)、[指标定义](03-metric-definition.md)、[契约版本](07-contract-versions.md)：验证和数据约束。
- [Roadmap](04-development-roadmap.md)：PRD 编号到 Issue 的执行映射。
- [Context](05-project-context.md)：决策来源与工作区上下文；[Work log](06-work-log.md)：实际执行记录。
- [Recording import](14-recording-import.md)、[Docker/API](20-docker-api.md)：运行方式与接口说明，明确当前预览版与旧稳定版差异。
- [Streaming ASR 边界](24-streaming-asr.md)：Active Voice Test 的实时识别边界、事件模型、二进制音频通道与已核对的火山契约；Recording Analysis 的 File ASR 见 [时间戳 ASR](11-timestamped-asr.md)。
- [Active Measurement](25-active-measurement.md)：持续 Measurement Audio、sample clock、Stimulus Reference、在线声学处理、Canonical Timeline 和统一指标边界。

根目录 [AGENTS.md](../AGENTS.md) 规定开发者如何读取与同步 PRD。技术专题文件不是额外 PRD，不以旧例子或工作日志替代用户需求。
