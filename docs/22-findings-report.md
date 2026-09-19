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
- `metric_ids` / `turn_ids` 均对照本次传入的 Timeline 与 metrics 解析；`turn_ids` 只能由该 Finding **自己引用的 events** 回溯得到（Timeline evidence 本身不携带 Turn 绑定，因此 linked metric 不能替它声明 Turn），否则不生成；
- **没有"就近借用证据"回退。** 候选若无法引用可解析证据（或 Timeline 本身不合法），该候选进入 `abstentions`（state + 原因），不产生 Finding；
- 每个生成的 Finding 在写入前都用 `finding_errors` 校验；不合约的 Finding 进入 `rejected` 并中止写出，绝不发布；
- defect 与 suspected 归因默认需要 human review；具名 `suspected_layers` 一律 `requires_log_verification=true`，`attribution_confidence` 上限 0.99；层名不在闭合 taxonomy 内则退化为显式 `unknown`（`attribution_confidence = 0`）。

`generate_findings_document()` 返回 `{schema_version, run_id, analysis_id, findings, abstentions, rejected}`；`generate_findings()` 保留为只返回列表的兼容入口。

### Finding 2.0.0 → 2.1.0

2.1.0 增加显式 `turn_ids` 与可空 `analysis_id`；`case_id` 在两个版本都保持必填非空：Finding 对照持久化 Timeline 解析，而 EventTimeline 始终带 Case 身份（未脚本化导入带 #24 建立的 `CASE-auto` 占位），因此可空 `case_id` 不可达、不予提供。`migrate_finding_document()` 可显式重发为 2.1.0，**必须传入 Timeline**：`turn_ids` 由该 Timeline 与 Finding 自己的 events 解析；缺 Timeline 时直接报错而不是产出空 `turn_ids`（空值在真实 Timeline 上会被 `finding_errors` 拒绝，即产出本引擎自己不会接受的文档）。测量层（MetricResult 3.0.0）对未脚本化导入保留了它自己可空的 `case_id`；这与 Finding 层的约定不同，已在 `docs/07-contract-versions.md` 记录，不做静默统一。

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

### 当前实现状态（Issue #11，浏览器侧）

wavesurfer.js 的 vendored 集成已经落地，但**尚未完成真实浏览器验收**：

- **版本与来源。** 固定使用 wavesurfer.js **7.12.12**（BSD-3-Clause），从 npm tarball `wavesurfer.js-7.12.12.tgz`（sha1 `f402d88f56091d09e045c98f48075859b37719dd`）逐字节复制到 `aivoicebench/static/vendor/`，`vendor/VENDOR.json` 记录每个文件的 sha256 与来源；文件本身不被改写或重新打包。
- **同源交付。** 浏览器只从同源 `/static/vendor/wavesurfer.min.js`、`regions.min.js`、`timeline.min.js`（以及长录音用 `minimap.min.js`）加载，由既有 FastAPI/Docker 静态链服务；**不使用 CDN，不在运行时请求任何第三方库**，不引入 bundler、ES module 包装或前端框架。
- **源码与产物。** 渲染层源码为 `web/src/workbench.ts`，编译产物 `aivoicebench/static/workbench.js` 由页面作为普通脚本加载，并发布 `window.WB`（`regionsFromDocument`、`metricRows`、`findingRows`、`eventRows`、`turnRows`、`transcriptRows`、`state`、`selectEvidence`、`mount`、`unmount`、`playerState`、`refresh`）。
- **只渲染后端证据。** Region 坐标直接取后端 `regions[].start_sec` / `end_sec`（唯一换算是后端已完成的 ms→s），`drag: false`、`resize: false`；`role: unknown` / `uncertain` / `provisional` 区间使用独立配色与标注，`metrics[].value` 原样显示且 `null` 显示为 `N/A`；`gate.role_dependent_available === false` 时只渲染 provisional 横幅与后端 `unavailable` 列表，不由前端推断角色。点击 Finding/Metric/Event/转写行只调用 `WB.selectEvidence(region_id)`，未知 id 返回 `null`，不会合成区间。转写行只在后端发布了指向**已发布 region** 的 `region_id` 时可点击；该关联由后端从 fused segment 的 `asr_segment_id`→`acoustic_segment_id` 交叉引用解析（ASR id 与声学片段 id 是独立命名空间），前端不按 `detail.segment_id` 同名匹配。该交叉引用只保证“同一段音频”而非“区间相等”，所以转写行同时带 `region_basis` 与 `region_span_matches`：当声学区间只是**包含**该话语（一个声学片段覆盖多句，或父片段按说话人切开）时 `region_basis` 为 `fused_acoustic_segment_container` 且 `region_span_matches` 为 `false`，证据面板与转写表如实标注为「容器区间（该话语为其子区间，非等值）」，绝不呈现为等值匹配。
- **门禁。** `npm run verify` 依次执行 `node scripts/verify-web-build.mjs`、`node scripts/smoke-web-station.mjs`、`node scripts/verify-workbench-render.mjs`（投影与坐标原样性，并在 DOM/wavesurfer stub 上真实执行 `mount()`：Finding/Metric/Event 行 → `selectEvidence` 必须 seek 到该区间自己的 `start_sec`、同步面板显示后端转写文本/角色/role_basis/confidence/status/来源文档/processor，未知 id 返回 `null` 且不移动播放位置，转写行只按后端 `region_id` 关联且容器区间必须被标注，角色 Gate 未完成时不创建 role-dependent Region）。同一脚本支持 `--document <workbench.json>`，用 python 侧真实 `build_workbench()` 输出复跑上述不变量，并由 `tests/test_web_workbench.py` 驱动，避免合成文档与后端真实形状各自演化。另由 `python -m unittest tests.test_web_workbench tests.test_web_station_build` 覆盖 vendor provenance、同源性、无凭据字面量、产物新鲜度。
- **仍未验证。** Docker 镜像内真实浏览器中的波形像素、seek/zoom、Regions/Timeline 交互与 5 分钟以上 Minimap 启用尚未验收（由 #85 承载）；本条**不构成** `real_recording_verified`，也不是完成的浏览器验收结论。

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
- wavesurfer.js Evidence Workbench 的投影、vendor 集成与渲染层已实现（#11，见上文“当前实现状态”）；**真实浏览器（Docker/远端 Chrome）中的波形与交互验收未完成**，由 #85 承载；
- Active Measurement 的 durable audio/timeline 尚未完成，因此 Active waveform 只能在相应 Artifact 建立后接入。