# PRD 分册导航

`../PRD.md` 是 AIVoiceBench 的唯一产品需求入口，维护产品边界、全局原则、稳定编号目录和正式里程碑。本目录将高频变化或篇幅较长的详细内容按职责拆分；任何产品变更仍须先定位 `PRD-Fxxx`、`PRD-Mxxx` 或 `PRD-Nxxx` 并同步中心入口。

| 分册 | 权威内容 | 不包含 |
| --- | --- | --- |
| [Recording Analysis](recording-analysis.md) | PRD-F001–F019 的需求、验收与实现边界 | Active Measurement 的实现设计 |
| [Active Measurement](active-measurement.md) | PRD-F020–F026 的需求、验收与阶段状态 | 具体协议/API 设计 |
| [Metric requirements](metric-requirements.md) | PRD-M001–M010 的产品指标、判定与弃权边界 | 公式实现细节 |
| [Acceptance status](acceptance-status.md) | 非功能要求、M1 验收、验证状态与里程碑门槛 | 具体 Issue 映射 |
| [Traceability](traceability.md) | Requirement ↔ Issue ↔ PR 追踪与审计结论 | 产品需求正文 |
| [Changelog](changelog.md) | PRD 版本历史与用户决策来源 | 当前需求正文 |

技术设计、schemas、Issue、工作日志只描述如何实现或实际发生了什么，不能自行改变 PRD 的产品范围。详细技术边界见 [系统架构](../01-system-architecture.md)、[指标定义](../03-metric-definition.md)、[Roadmap](../04-development-roadmap.md) 和 [Active Measurement 设计](../25-active-measurement.md)。
