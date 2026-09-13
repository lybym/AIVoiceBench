# 产品指标要求

本文承载 PRD-M001–M010 的产品含义和验收边界。具体字段、公式、MetricResult 兼容性和处理器实现见 [指标定义](../03-metric-definition.md)；该技术文档不能自行新增产品指标。

| ID | 指标 | 产品要求 |
| --- | --- | --- |
| PRD-M001 | First Speech Latency | tester 有效结束到 device 首次有效语音开始；时间基和 evidence 可追溯 |
| PRD-M002 | Endpoint / Response End | device 回答结束候选与不确定性；不能把 ASR final 直接当声学边界 |
| PRD-M003 | Semantic Response | 回答是否覆盖问题/策略目标；需要受约束语义证据 |
| PRD-M004 | Turn Gap | tester 结束到 device 开始的带符号 gap；负值是 overlap，不截零 |
| PRD-M005 | Barge-in Stop Latency | interrupt 发生到被打断旧回答停止；无法识别旧回答时弃权 |
| PRD-M006 | Barge-in Semantic Compliance | 打断后的新回答是否遵从新请求；需要可追溯语义证据 |
| PRD-M007 | False Endpoint | 候选与确认分开；不能由单一启发式宣布确认 |
| PRD-M008 | ASR / Transcript Quality | 只在存在有资格参考时计算；外部 ASR 不代表设备内部 ASR |
| PRD-M009 | Overlap | tester/device 有效语音重叠；混音或角色不明时弃权 |
| PRD-M010 | Coverage / Outcome | 区分计划、尝试、观察、正式测量与弃权，不用控制成功替代覆盖 |

## 通用判定规则

- 两条正式 Pipeline 使用相同的指标名、定义、Canonical Event 语义和 MetricResult 契约；声学 Artifact、Event Producer 和不确定性可不同。
- 任何指标必须能回溯到 Artifact、Event、Evidence、policy、processor/model version、confidence 与 uncertainty。
- `not_applicable` 表示结构上无意义，`insufficient_evidence` 表示证据不足，`invalid` 表示输入/完整性不合法；三者都不能被默认为 0 或成功。
- Active Measurement 的正式声学边界来自 sample-index timebase；Recording Analysis 的边界来自其独立 Artifact。ASR 时间戳、播放 callback、网络接收时间只能作为语义、控制或诊断信息。
- 统计应报告分母、缺失与弃权，不能只展示成功样本的平均值。
