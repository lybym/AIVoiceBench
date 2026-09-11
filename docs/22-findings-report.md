# Finding Generation + Report Rendering

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

2026-09-11 baseline: main c612d36 / alpha.2. Issue #11.

Web/CLI import calls write_import_report(): report_kind=import_stage_status, empty conclusions, insufficient device-performance evidence. It does not execute Judge/Findings. The modules and commands below are the separate pipeline/findings/report CLI; they are not the default Web path. Full integration and human confirmation remain incomplete.

## Pipeline

```
LLM Judge Results (#10)
  + EventTimeline (#24)
  + Metrics (#25)
  ↓
generate_findings() → Finding 2.0.0 documents
  ↓
render_report() → report.md + report.json
```

## Finding generation

`aivoicebench/findings.py`:

- Takes judge results with `dimension: finding_candidate` and `status: observed`
- Converts to Finding 2.0.0 with severity mapping:
  - critical → P0 defect
  - high → P1 defect
  - medium → P2 defect
  - low → P3 defect
  - info → observation (no severity)
- Every finding has:
  - `evidence_ids` linking to timeline audio time ranges
  - `event_ids` linking to detected events
  - `metric_ids` linking to computed metrics
  - `suspected_layers` with `requires_log_verification: true`
  - `status: needs_verification` (not confirmed until human review)
  - `human_review: required` for defects

## Report rendering

`aivoicebench/report.py`:

Produces `report.md` (human-readable) and `report.json` (structured):

1. **运行摘要** — run status, counts
2. **设备信息** — device/hardware/firmware/model/prompt/supplier
3. **音频分段** — fused segments with speaker role, timing, text
4. **对话轮次** — turn timeline with interruption/overlap flags
5. **事件时间线** — all events with type, time range, source, confidence
6. **指标** — metrics with values, status (observed/insufficient)
7. **LLM 语义评估** — judge results: intent, meaningful response, feedback, quality
8. **Findings** — severity, confidence, suspected layer, evidence refs
9. **证据** — evidence with audio time ranges and source
10. **溯源** — processor versions, model info, SHA256, invocation records

## Commands

```powershell
# Generate findings
& ./.venv/Scripts/python.exe -m aivoicebench findings --judge artifacts/judge/judge-results.json --timeline artifacts/fusion/timeline.json --metrics artifacts/fusion/metrics.json --output artifacts/findings/findings.json

# Render report
& ./.venv/Scripts/python.exe -m aivoicebench report --output artifacts/report --profile artifacts/profile.json --fused artifacts/fusion/fused-segments.json --turns artifacts/fusion/turns.json --timeline artifacts/fusion/timeline.json --metrics artifacts/fusion/metrics.json --judge artifacts/judge/judge-results.json --findings artifacts/findings/findings.json
```

## Full pipeline (end-to-end)

```
acoustic → fusion → metrics → judge → findings → report
```

Historical synthetic fixture example, not a current Web result or real measurement:
- 2 segments (tester: "今天天气怎么样？" / device: "嗯……好的，让我看看。南京今天天气晴朗。")
- 1 turn, 7 events, 3 metrics, 5 judge results, 1 finding
- Finding: [P2] high_latency (3000ms exceeds 2000ms threshold)
- Suspected layer: llm, requires log verification
- Report includes all audio time ranges for traceability

## Evidence-first

Every finding, metric, and event links back to:
- Evidence → audio time range → artifact → SHA256
- Event → evidence_ids → audio snippet
- Finding → evidence_ids + event_ids + metric_ids
- LLM result → invocation_id → provider/model/prompt_version

No conclusion is made without evidence. Insufficient evidence yields
`insufficient_evidence`, not a guessed result.

## Current limitations

- Metrics module (#25) doesn't yet feed back LLM judge results to resolve
  `insufficient_evidence` metrics (feedback_latency, meaningful_response_latency).
  Both the metrics (insufficient) and judge results (observed) are shown in
  the report — honest and transparent.
- Findings are candidates (`needs_verification`), not confirmed. Human review
  is required for all defects.
- No Finding lifecycle (candidate → confirmed → regression case) — that
  needs #26 human revision contract.
