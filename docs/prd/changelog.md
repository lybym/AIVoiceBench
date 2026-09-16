# PRD 变更历史

| PRD 版本 | 日期 | 变更摘要 |
| --- | --- | --- |
| 1.5.2 | 2026-09-16 | 部署与组件路线收敛：正式主架构明确为 Linux Server + Docker 后端与远端 Chrome Browser Station；正式声学时间必须在现场以 sample clock 生成。近期 VAD 采用 TEN VAD（浏览器控制）+ Silero VAD（服务器离线/最终化分析）；Recording Analysis 优先使用火山 ASR speaker separation，当前不接 3D-Speaker；Evidence UI 采用 wavesurfer.js。实现基线与真实验收状态不升级。 |
| 1.5.1 | 2026-09-13 | 正式版事实收敛：把 alpha.6 自由对话修复与可选语义角色提议纳入 `v0.4.0` 发布基线；更新 Docker/Web 交付说明。产品边界、Requirement ID 与真实验收门槛不变。 |
| 1.5.0 | 2026-09-13 | 结构化拆分：`PRD.md` 保持唯一入口；详细功能、指标、验收、追踪和历史分别移入 `docs/prd/`。Requirement ID、产品边界、验收门槛与实现状态不因拆分而升级或降低。 |
| 1.4.0 | 2026-09-13 | 确立 Active Measurement 与 Recording Analysis 两条独立正式 Measurement Pipeline；新增 F025、F026、N007，正式声学时间以 sample clock 为准。 |
| 1.3.3 | 2026-09-13 | 发布事实校准至 v0.4.0-alpha.4；不改变产品需求或真实验收状态。 |
| 1.3.2–1.3.0 | 2026-09-11 至 2026-09-12 | Streaming ASR 路线分叉、预检/判停/降级行为收敛，保持真实云与实体设备验收 pending。 |
| 1.2.3–1.2.0 | 2026-09-11 | 控制可靠性、TTS 与主动测试排程收敛；不降低 M1 验收。 |
| 1.1.2–1.0.0 | 2026-09-10 至 2026-09-11 | 导入优先、中央 PRD、双主流程、证据链、里程碑和 Issue 追踪基础建立。 |

完整的逐项历史理由保留在 Git 提交历史、发布说明和 [工作日志](../06-work-log.md)。PRD 变更必须在同一 PR 记录 Requirement ID、范围、验收与用户决定。