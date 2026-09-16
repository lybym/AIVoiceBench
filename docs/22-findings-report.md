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

`aivoicebench/findings.py`：

- 接收 `finding_candidate` / `observed` Judge result；
- 映射 severity 到 defect/observation；
- 每个 Finding 保存 `evidence_ids`、`event_ids`、`metric_ids`、suspected layer 与 review 状态；
- 缺少证据时不生成确定性结论；
- defect 默认需要 human review。

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

- ImportRun 尚未执行完整 Judge/Findings 主链；
- Findings 仍是 candidate / needs_verification；
- Human revision lifecycle 尚未完整闭环；
- wavesurfer.js Evidence Workbench 为 planned；
- Active Measurement 的 durable audio/timeline 尚未完成，因此 Active waveform 只能在相应 Artifact 建立后接入。