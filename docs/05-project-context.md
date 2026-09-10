# Project context — 决策与工作区上下文

本文件不再承担 PRD。产品需求、状态与验收请读 [PRD](PRD.md)；执行顺序见 [roadmap](04-development-roadmap.md)；技术约束见 [architecture](01-system-architecture.md)；开发规则见 [AGENTS](../AGENTS.md)。

## 已确认决策来源

- 2026-09-07：用户将已有录音导入分析提升为最高优先级；HIL/Audio Station 后置，保留既有基础设施。
- 2026-09-09：用户明确改为 Docker 后端+前端、Windows 浏览器，取消 Windows EXE/安装包要求。
- 2026-09-09：用户要求简洁新版 Web 与三项发布修复，并追加参考 DeepSeek Harness 的模型配置管理。
- 2026-09-10：用户要求同库中心 PRD、归集旧需求、明确代码实现标识。整合结果在 PRD，不在此再次扩写需求。

## 工作区与审计边界

授权仓库：lybym/AIVoiceBench。主检出位于本机 WORK/12 CODE/AIVoiceBench；其未结束 rebase 不应被其他任务擅自继续/取消。隔离工作目录是执行安排，不是产品要求。

M1 分支从 main 3f75d5a 独立建立；#43/#45/#47 已合并。当前执行 #22/#30 的录音证据主链，后续阶段按 PRD 的 M1～M5 顺序；发布仍为 v0.1.3，分支目标 v0.2.0-alpha.1。后续任务必须重新核对 refs。

硬件信息只有“自带麦克风/扬声器的语音终端”，型号未定；尚无用户真实录音验收。合成语音、静音和软件夹具不是设备性能证据。

原始交接来源为“优化语音测试提示词”会话与后续明确用户决定。旧文件完整保存在 [历史上下文](product/archive/2026-09-10/05-project-context.md)；个人本机路径/旧工具建议仅用于追溯，不能成为新增依赖。实际执行进度只追加到 [work log](06-work-log.md)。
