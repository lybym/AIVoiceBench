# Expanded Metrics — 当前实现与需求差距

指标的用户含义与验收在 [PRD-M001～M010](PRD.md)，规范契约与统计在 [metric-definition](03-metric-definition.md)。本文件只记录导入扩展实现的技术差距。

`compute_timeline_metrics()` 位于 `aivoicebench/metrics.py`，调用 `formulas.py`；旧的证据校验计算路径位于 `engine.py`。测试分别为 `test_metrics_expanded.py` 与 `test_engine.py`。二者需要集成，而非重写旧引擎。

| 当前路径 | 尚需修复/验证 |
| --- | --- |
| first_speech_latency_ms | 可靠 tester/device 角色、对应 turn 边界和 canonical MetricResult 验证 |
| turn_gap_ms(device_end, next_tester_start) | 方向与 PRD-M004 不符；不得将当前函数参数反向固化为需求 |
| barge_in_stop_latency_ms | 自动识别旧 response 身份，避免误取新回答结束 |
| overlap duration / ratio | 多区间、完整观察窗口、分母与规范 Evidence 一致性 |
| false_endpoint_detected | 不能将 possible_false_endpoint 候选直接升级为已确认异常 |
| feedback / meaningful response | 当前保持 insufficient_evidence；需要已有语义锚点，不接受模型编造时间 |

旧表格中的 observed 表示某段代码可输出该枚举，不代表产品需求已完成。所有完成状态移到 PRD。旧技术说明保留在 [原文快照](product/archive/2026-09-10/19-expanded-metrics.md)。相关 Issues #8/#25，依赖 #24/#10。
