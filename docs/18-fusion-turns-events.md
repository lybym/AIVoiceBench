# Fusion / Turns / Events — 实现边界

产品需求和状态见 [PRD-F006～F008](PRD.md)。本文解释 main 19d3a07 / v0.1.3 的实际模块，不另定义产品需求。

## 处理入口

`fuse(acoustic_doc, transcript_doc=None)` 保留声学片段，可附加相交 ASR 文本。默认 speaker_role=unknown，不再交替猜 tester/device。存在 unknown 时角色依赖的事件检测输出 insufficient_evidence。

`build_turns()`、`detect_events()` 处理已明确角色的片段，构建 turn/response 和事件候选。`generate_timeline()` 序列化结果；输出存在不代表它已经通过全部规范 Timeline/Evidence 的语义与引用校验。

## 已知差距

- 实际混音 diarization/source Provider 与语义关联未接通；fixture 的人工角色不是真实设备识别。
- 事件别名、打断结束、完整观察窗口 timeout、possible_false_endpoint 确认以及音频 Evidence/Run 身份需要统一。
- 旧文档的交替角色 confidence=0.5 方案已被移除，不能恢复为当前默认行为。

CLI 仍可使用 `python -m aivoicebench fusion acoustic.json --output artifacts/fusion`；输入/输出字段以当前代码和 schemas 为准。对应代码 `aivoicebench/fusion.py`，测试 `tests/test_fusion.py`、`tests/test_evidence_guards.py`，Issue #24。

旧算法叙述和示例完整保存在 [原文](product/archive/2026-09-10/18-fusion-turns-events.md)。它们不是当前产品或准确性验收依据。
