# Expanded Metrics — 当前实现与剩余范围

2026-09-19 基线 main `21b36d8`（Issue #25）。产品含义在 [PRD-M001～M010](prd/metric-requirements.md)，技术公式在 [指标定义](03-metric-definition.md)。

[compute_timeline_metrics()](../aivoicebench/metrics.py) 是该 Pipeline 唯一的 Canonical Metric Producer，输出规范 MetricResult **3.0.0** / `definition_version` **4.0.0**。旧 [engine.py](../aivoicebench/engine.py) 保留显式 Case/Timeline 计算。2.0.0 与 3.0.0 历史结果仍可读。

**PRD-M001–M010 的编号冲突与结论。** PRD 1.5.0 的 `docs/prd/metric-requirements.md` 把 `PRD-M001–M010` 重新分解为 First Speech / Endpoint Response End / Semantic Response / Turn Gap / Barge-in Stop / Barge-in Semantic Compliance / False Endpoint / ASR-Transcript Quality / Overlap / Coverage-Outcome；1.5.0 之前的 PRD 与 `03-metric-definition.md`、`metrics.py` 使用的是另一组（Feedback / First Speech / Meaningful Response / Turn Gap / Barge-in Stop / Barge-in New Intent / Barge-in Success / False Endpoint / Overlap / Timeout-CER-WER）。Issue #25 明确要求按**当前** PRD 定义实现，因此本次以当前 PRD 为准，引擎输出 `definition_version` 4.0.0；历史 3.0.0 映射保留在 `metrics.PRD_REFS_BY_DEFINITION`，并提供 `prd_ref_for` / `migrate_prd_ref` / `migrate_metric_document` 显式迁移，`metric_errors` 拒绝 `definition_version` 与 `prd_ref` 不一致的文档。

Active Measurement 与 Recording Analysis 共用同一引擎；Event Producer 和 Audio Evidence 可以不同，相同 canonical events/policy 必须得到相同数值。不得新增 `live_*` / `offline_*` 或复制公式。Active Timeline 接线仍 planned。

| PRD | 已实现 | 尚缺 / 限制 |
| --- | --- | --- |
| M001 First Speech | 同轮 tester end→device onset；仅接受声学边界；提前发声为 not_applicable | 自动角色与真实边界质量 |
| M002 Endpoint / Response End | 声学 `device_speech_end` 提供候选点与 `uncertainty_ms`；请求必须已结束（需 `tester_speech_end`，只有 `tester_speech_start` 时弃权）；ASR final 只作为转写估计并显式弃权 | 真实回答结束的语义确认与多候选选择 |
| M003 Semantic Response | 受约束语义证据资格路径（decision/criterion/judge_profile/evidence 齐备才取值） | #10 Judge 未接入主链，导入路径恒为 insufficient_evidence |
| M004 Turn Gap | tester end→device start，保留负值 | 真实对照和语义有效起点 |
| M005 Barge-in Stop | 按 interrupted_response_id 查找旧回答结束 | 自动关联质量与真实打断场景 |
| M006 Barge-in Semantic Compliance | 受约束语义证据资格路径 | 同上，#10 未接入 |
| M007 False Endpoint | `false_endpoint_candidate` 只报候选；`false_endpoint_confirmed` 需 tester 续说 + 停顿内设备回应 + 完整观察窗 + 语义/人工确认，缺项逐条列出 | 完整观察窗的真实覆盖证据 |
| M008 ASR / Transcript Quality | 需有资格参考 + device_internal 转写 + evidence 引用；外部 ASR 显式弃权 | 真实设备内部转写来源 |
| M009 Overlap | 优先用 overlap 事件对，否则用双方语音区间并集交集；角色身份无法建立、或区间存在但交集不可形成时都显式弃权（不再静默无文档） | 复杂响应与真实声源识别的分母一致性 |
| M010 Coverage / Outcome | `coverage` 区分 attempted/measured/abstained/invalid，`planned_count` 仅在给出计划时报告；measured 只计真正闭合区间的正式测量（M001/M004/M005），M002 候选点不构成一次正式测量 | 真实计划的计划单位来源 |

无事件时返回不足证据与空指标，不强造各维度。`invalid`（Artifact 完整性失败）与 `not_applicable` / `insufficient_evidence` 一样必须带 reason、`value=null`、`sample_count=0`；invalid 运行会为当前分解的**每个**名字与 legacy 记录各出一份 `invalid` 文档，缺失的名字不得被读成"已评估"。聚合由 `aggregate_metrics()` / `latency_percentiles()` 提供，保留分母与 excluded/invalid/abstained 计数；`rate`/`micro` 聚合必须为每个样本链接一个合格 `input_metric_ids`，因此一个 turn 可产出多样本的名字（每个 overlap 对、每次打断）带逐样本标识。整体 `status` 由唯一规则 `metrics.run_status` 推导，所有调用面一致。

证据：[契约测试](../tests/test_metrics_contract.py)、[PRD 一致性](../tests/test_metrics_prd_conformance.py)、[版本兼容](../tests/test_metric_compatibility.py)、[扩展指标](../tests/test_metrics_expanded.py)、[证据守卫](../tests/test_evidence_guards.py)。真实录音对照仍待完成。
