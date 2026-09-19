# Finding Generation + Report Rendering

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

实现基线仍为 `v0.4.0`。2026-09-16 增加 Web Evidence Workbench 的组件决策：采用 wavesurfer.js，不自研 waveform renderer；该决策不代表 UI 已实现。

## Pipeline

```text
LLM Judge Results
  + EventTimeline
  + Metrics
  ↓
generate_findings() → Finding documents
  ↓
render_report() → report.md + report.json
  ↓
Web Evidence Workbench → wavesurfer.js
```

## Finding generation

`aivoicebench/findings.py`（Issue #10 之后，Finding 2.1.0）：

- 接收 `finding_candidate` / `observed` Judge result；
- 映射 severity 到 defect/observation；
- 每个 Finding 保存 `evidence_ids`、`event_ids`、`metric_ids`、**`turn_ids`**、suspected layer 与 review 状态；
- `metric_ids` 使用 canonical `metric_id`（不是显示名），且只链接**自身已决定**（`observed`/`pass`/`fail`）且属于同一 Turn 的 MetricResult —— 弃权的 metric 不能支撑一个缺陷结论；
- `metric_ids` / `turn_ids` 均对照本次传入的 Timeline 与 metrics 解析；`turn_ids` 必须能由该 Finding 自己的事件/指标回溯到，否则不生成；
- **没有"就近借用证据"回退。** 候选若无法引用可解析证据（或 Timeline 本身不合法），该候选进入 `abstentions`（state + 原因），不产生 Finding；
- 每个生成的 Finding 在写入前都用 `finding_errors` 校验；不合约的 Finding 进入 `rejected` 并中止写出，绝不发布；
- defect 与 suspected 归因默认需要 human review；具名 `suspected_layers` 一律 `requires_log_verification=true`，`attribution_confidence` 上限 0.99；层名不在闭合 taxonomy 内则退化为显式 `unknown`（`attribution_confidence = 0`）。

`generate_findings_document()` 返回 `{schema_version, run_id, analysis_id, findings, abstentions, rejected}`；`generate_findings()` 保留为只返回列表的兼容入口。

### Finding 2.0.0 → 2.1.0

2.1.0 增加显式 `turn_ids` 与可空 `analysis_id`，并允许未脚本化导入的 `case_id` 为空（2.0.0 仍要求 case identity，且不得携带 2.1.0 字段）。历史文档继续通过其自身版本的规则；`migrate_finding_document()` 可显式重发为 2.1.0，`turn_ids` 从 Timeline 解析，**解析不到时留空而不是猜测**。

## Report rendering

`aivoicebench/report.py` 输出 `report.md` 和 `report.json`，覆盖：运行摘要、设备信息、音频分段、对话轮次、事件时间线、指标、LLM 语义评估、Findings、Evidence 与 provenance。

## Web Evidence Workbench — wavesurfer.js

Web 端音频证据审阅采用 [wavesurfer.js](https://github.com/katspaugh/wavesurfer.js)。首期使用：

- Waveform core：显示选定的 Original/Normalized/Measurement Audio；
- Regions：显示 acoustic segment、speaker cluster、turn、event、finding evidence；
- Timeline：显示统一时间轴；
- Minimap：长录音可选，用于快速导航。

目标交互：

```text
Finding / Metric / Event
        ↓ click
Evidence ID → audio_relative_ms range
        ↓
wavesurfer seek / zoom / highlight Region
        ↓
同步 transcript / speaker / confidence / uncertainty / provenance
```

### 时间与事实边界

wavesurfer.js **不是 Measurement Processor**：

- 不在浏览器重新推导正式 speech boundary；
- 不在前端计算 PRD-M 指标；
- Region 坐标来自 AIVoiceBench Artifact/Evidence/EventTimeline；
- 浏览器 seek/playback time 只用于 UI，不覆盖 `audio_relative_ms`；
- 人工拖动 Region 如未来允许，必须产生 Human Revision，而不是原地覆盖机器 Evidence。

这样可以把“可看见的波形编辑器”和“正式测量真值”严格分开。

## Commands

```powershell
# Generate findings
& ./.venv/Scripts/python.exe -m aivoicebench findings --judge artifacts/judge/judge-results.json --timeline artifacts/fusion/timeline.json --metrics artifacts/fusion/metrics.json --output artifacts/findings/findings.json

# Render report
& ./.venv/Scripts/python.exe -m aivoicebench report --output artifacts/report --profile artifacts/profile.json --fused artifacts/fusion/fused-segments.json --turns artifacts/fusion/turns.json --timeline artifacts/fusion/timeline.json --metrics artifacts/fusion/metrics.json --judge artifacts/judge/judge-results.json --findings artifacts/findings/findings.json
```

## Evidence-first

Every finding, metric, and event links back to：

- Evidence → audio time range → artifact → SHA256；
- Event → evidence_ids → audio snippet；
- Finding → evidence_ids + event_ids + metric_ids；
- LLM result → invocation_id → provider/model/prompt_version。

No conclusion is made without evidence. Insufficient evidence yields `insufficient_evidence`, not a guessed result.

## Current limitations

- Judge/Findings 已接入 Recording Analysis 主链（Issue #10），但**真实 provider 调用与真实录音验收未完成**（fixture 级证据，由 #85 承载）；
- Findings 仍是 candidate / needs_verification；
- Human revision lifecycle 尚未完整闭环；
- wavesurfer.js Evidence Workbench 为 planned（#11）；
- Active Measurement 的 durable audio/timeline 尚未完成，因此 Active waveform 只能在相应 Artifact 建立后接入。