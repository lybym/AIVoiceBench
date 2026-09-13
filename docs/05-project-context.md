# Project context — 决策与工作区上下文

本文件不再承担 PRD。产品需求、状态与验收请读 [PRD](PRD.md)；执行顺序见 [roadmap](04-development-roadmap.md)；技术约束见 [architecture](01-system-architecture.md)；开发规则见 [AGENTS](../AGENTS.md)。

## 已确认决策来源

- 2026-09-07：用户将已有录音导入分析提升为最高优先级；HIL/Audio Station 后置，保留既有基础设施。
- 2026-09-09：用户明确改为 Docker 后端+前端、Windows 浏览器，取消 Windows EXE/安装包要求。
- 2026-09-09：用户要求简洁新版 Web 与三项发布修复，并追加参考 DeepSeek Harness 的模型配置管理。
- 2026-09-10：用户要求同库中心 PRD、归集旧需求、明确代码实现标识。整合结果在 PRD，不在此再次扩写需求。

- 2026-09-10：用户明确主动测试/录音分析双主流程，普通本地音频与专业 HIL 拆分。
- 2026-09-11：用户要求按最新代码更新 docs。
- 2026-09-13：用户明确把 Active Voice Test 提升为可独立产生正式 Measurement Result 的 Active Measurement Pipeline；Recording Analysis 保持另一条独立正式 Pipeline。两者不共享 Audio Evidence，共享 Canonical Measurement Semantics；External Recording 不再是 Active Result 转正前置。随后用户把本任务范围收紧为**仅迭代 docs 内容**，禁止本轮修改代码、schema、测试或前端。

## 工作区与审计边界

授权仓库：lybym/AIVoiceBench。主检出位于本机 WORK/12 CODE/AIVoiceBench；现有分支、未提交内容及其他工作树不应被文档审计改动；工作区状态需每次核对。隔离工作目录是执行安排，不是产品要求。

2026-09-13 本任务重新 fetch 并核对 `origin/main` = `0362b221ec06d2eba8134851469c34d4c1215952`。Recording Analysis 已有部分正式 Event/Metric 主链；Active Voice 已有 Control Plane 的浏览器播放、RMS VAD、Streaming ASR 与 Agent 基础，但 main 没有跨整次 Run 的 durable Measurement Audio、sample-clock Evidence、Stimulus Alignment 或 Canonical Live Timeline。本轮只收敛文档，不升级这些实现状态。

硬件信息只有“自带麦克风/扬声器的语音终端”，型号未定；尚无用户真实录音验收。合成语音、静音和软件夹具不是设备性能证据。

原始交接来源为“优化语音测试提示词”会话与后续明确用户决定。旧文件完整保存在 [历史上下文](product/archive/2026-09-10/05-project-context.md)；个人本机路径/旧工具建议仅用于追溯，不能成为新增依赖。实际执行进度只追加到 [work log](06-work-log.md)。
