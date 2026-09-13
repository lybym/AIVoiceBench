# Requirement 追踪与审计结论

Issue 定义工作单元，PR 定义一次可审阅变更，工作日志记录事实；三者都不改变 PRD 的产品范围。Issue 保持 Open 不等于无实现，Closed 也不等于需求完成或真实验收通过。

## 当前重点 Issue 映射

| Issue | PRD 编号 | 追踪重点 |
| --- | --- | --- |
| #21 | F001–F003 | 真实录音导入与资产链路验收 |
| #22、#30 | F005、F016 | Provider 调用审计、真实云契约与质量 |
| #23 | F008 | 声学分段与边界不确定性 |
| #24 | F006–F008 | 角色、Turn 与 Event 自动关联 |
| #25 | F009、M001–M010 | 指标、语义证据与真实对照 |
| #26 | F012、F017 | 人工修订、重算与历史保留 |
| #27 | F004、F013、F014 | Judge/Findings/报告与 Web 集成 |
| #5、#6、#9 | F020、F022/F023、F019 | Active Runner、HIL、冻结刺激 |
| [#76](https://github.com/lybym/AIVoiceBench/issues/76) | F023–F026、N007 | 双正式 Measurement Pipeline 文档收敛；实现与等价性实验仍待后续工作单元 |

## 审计结论

当前 M1 的主要缺口是：真实聚类/角色/语义关联、完善的事件/指标证据、Judge/Findings/报告主链、人工审核与通用重分析，以及授权真实录音、有效服务调用和人工标注。当前 Active Control 已有局部基础，但 Active Measurement Audio、Stimulus Alignment、在线 Canonical Timeline、正式 Active MetricResult、Measurement Equivalence 和高级 Barge-in 仍按 `planned` 或 `validation_pending` 管理。

历史已关闭 Issue 的需求已归入稳定 Requirement ID；旧 Issue、PR、发布标签和归档记录仅作可追溯证据，不覆盖当前需求。
