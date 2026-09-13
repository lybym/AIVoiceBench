# Expanded Metrics — 当前实现与剩余范围

2026-09-11 基线 main c612d36 / alpha.2。用户含义在 [PRD-M001～M010](PRD.md)，技术公式在 [指标定义](03-metric-definition.md)。

[compute_timeline_metrics()](../aivoicebench/metrics.py) 直接输出规范 MetricResult **3.0.0**，旧 [engine.py](../aivoicebench/engine.py) 保留显式 Case/Timeline 计算。2.0.0 历史结果仍可读，验证器按版本检查，不把新字段强加给旧数据。

该函数及其后续版本是 Active Measurement 与 Recording Analysis 的唯一 Canonical Metric Producer。Event Producer 和 Audio Evidence 可以不同；相同 canonical events/policy 必须得到相同数值。不得新增 `live_first_speech_latency` / `offline_first_speech_latency` 或复制公式。Active Timeline 接线仍 planned，不能因本文声明统一方向就写成已实现。

| 范围 | 已实现 | 尚缺 / 限制 |
| --- | --- | --- |
| M002 First Speech | 同轮 tester end→device onset；提前发声为 not_applicable | 自动角色及真实边界质量 |
| M004 Turn Gap | tester end→device start，保留负值 | 真实对照和语义有效起点 |
| M005 Barge-in Stop | interrupted_response_id 查找旧回答结束 | 自动语义关联，不替代完整打断成功 |
| M008 False Endpoint | false_endpoint_candidate / candidate_only | observed 只指候选存在，不是确认缺陷 |
| M009 Overlap | 事件对计算 duration/ratio，保留引用和分母策略 | 多区间去重/合并、复杂响应和真实声源识别仍待验证 |
| M001/M003/M006/M007 | 显式 insufficient_evidence，不猜语义时间 | 反馈/有效回答/新 Intent 锚点与完整观察窗口 |
| M010 timeout/CER/WER/统计 | 旧 engine/formulas 有基础 | 当前导入不自动产出 M010，缺合法参考/健康窗口不能算 |

无事件时返回不足证据与空指标，不强造各维度。单项记录 Run/Analysis、定义/策略版本、适用状态、引用和计数。ImportRun 未执行 Judge，配置路由不等于语义指标已补齐。

证据：[契约测试](../tests/test_metrics_contract.py)、[版本兼容](../tests/test_metric_compatibility.py)、[扩展指标](../tests/test_metrics_expanded.py)、[显式角色端到端](../tests/test_explicit_attribution_e2e.py)。PR #52 的代码已在 main 集成历史中，不应根据 Closed 标签重复修复。真实录音对照仍待完成。
