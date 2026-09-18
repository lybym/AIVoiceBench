# Requirement 追踪与审计结论

Issue 定义工作单元，PR 定义一次可审阅变更，工作日志记录事实；三者都不改变 PRD 的产品范围。Issue 保持 Open 不等于无实现，Closed 也不等于需求完成或真实验收通过。

## 当前 M1 / P0 Issue 映射

当前 P0 主线收敛到 Recording Analysis / M1。实现、集成与真实验收分开追踪，避免把软件/容器验证误写成真实录音验收完成。

| Issue | PRD 编号 | 追踪重点 |
| --- | --- | --- |
| [#21](https://github.com/lybym/AIVoiceBench/issues/21) | F001–F003、N001–N002 | Recording import、不可变 Artifact、标准化与 Audio QA；真实 5–20 分钟录音导入验收 |
| [#87](https://github.com/lybym/AIVoiceBench/issues/87) | F005、F015–F016、N004/N006 | Provider/Object Storage 外置配置；File ASR 极速版 `inline | object_storage | auto` transport；移除固定 PUT/GET/HOST 生产依赖 |
| [#22](https://github.com/lybym/AIVoiceBench/issues/22) | F005–F006、F016、N001/N004/N005 | Volcengine File ASR、原生 speaker separation、Provider provenance 与真实云调用证据；当前不接 3D-Speaker |
| [#23](https://github.com/lybym/AIVoiceBench/issues/23) | F003、F008–F009、N001/N003/N007 | Silero VAD server acoustic-boundary baseline、Measurement Policy、边界不确定性与真实标注样本评估 |
| [#24](https://github.com/lybym/AIVoiceBench/issues/24) | F006–F008、N001/N003/N005 | speaker evidence → tester/device/unknown Attribution → Turn/Response → Canonical EventTimeline；冲突与未知必须保留 |
| [#25](https://github.com/lybym/AIVoiceBench/issues/25) | F009、M001–M010、N001/N003/N007 | 唯一 Canonical Metric Engine、MetricResult、证据资格与 denominator/abstention 语义 |
| [#10](https://github.com/lybym/AIVoiceBench/issues/10) | F010–F011、M003/M006、N001/N003/N004/N005 | Structured Judge、语义证据与 evidence-linked Findings；不得发明时间、角色或已验证根因 |
| [#26](https://github.com/lybym/AIVoiceBench/issues/26) | F012、F017、N001/N002/N006 | append-only 人工修订、AnalysisRevision、重算与 revision diff；不覆盖机器原件 |
| [#11](https://github.com/lybym/AIVoiceBench/issues/11) | F013–F014、N001/N002/N006 | wavesurfer.js Evidence Workbench、Finding/Metric/Event 音频定位与 evidence-linked JSON/Markdown 报告 |
| [#27](https://github.com/lybym/AIVoiceBench/issues/27) | F004、F013–F017、N001–N006 | Linux Server + Docker + Remote Chrome 下的 M1 分阶段编排、持久化、失败恢复与 Web/API/CLI 一致性 |
| [#85](https://github.com/lybym/AIVoiceBench/issues/85) | F001–F017、M001–M010、N001–N007 | M1 最终真实证据 Gate：授权真实录音、真实云调用、人工标注/复核、修订重分析、浏览器证据回放与验收状态记录 |

### M1 依赖与验收关系

```text
#21 Import / Artifact / QA
 ├─→ #87 external config + File ASR transport ─→ #22 File ASR + speaker separation ─┐
 └─→ #23 Silero acoustic boundary ───────────────────────────────────────────────────┤
                                       ↓
                              #24 Attribution / Turn / Event
                                       ↓
                              #25 Canonical Metrics
                                       ↓
                              #10 Judge / Findings

#26 Human Revision / Reanalysis ───────→ #24 / #25 / #10 / #11 / #27
#11 Evidence Workbench / Report ───────→ #27 Integration

#21–#27 + #10 + #11
          ↓
#85 Authorized real-recording M1 acceptance
```

`#87` 负责配置 ownership 与 File ASR transport，不能用其软件测试替代 #22 的真实云识别/speaker separation 或 #85 的真实录音验收。`#27` 证明集成链路、持久化、失败恢复和 Docker/Web 行为；它的关闭本身不等于 `real_recording_verified`。`#85` 独立承担 M1 的授权真实录音、真实云服务与人工复核门槛，避免 CI、mock、synthetic fixture、容器 smoke 或浏览器演示被误认为正式验收。

## 审计结论

当前 M1 的主要缺口按上述工作单元收敛为：Provider/Object Storage 外置配置与 File ASR transport 重构、真实火山 File ASR speaker separation 与调用证据、Silero 声学边界真实标注评估、角色/Turn/Event 的可审计归属、完整 PRD-M001–M010 证据资格、Structured Judge/Findings、不可变人工修订与重分析、wavesurfer Evidence Workbench/完整报告，以及最终授权真实录音与人工复核验收。

Recording Analysis 的实现完成、软件验证、容器验证、浏览器验证、真实云调用和 `real_recording_verified` 必须分别记录，不得相互替代。M1 最终是否通过以 #85 的真实证据 Gate 为准。

Active Measurement 继续按 M2–M4 独立推进；Browser Station TypeScript、Frozen Golden Voice、TEN VAD、Live Measurement Audio、Stimulus Alignment、Active Canonical Timeline、Active MetricResult、Measurement Equivalence 与专业 HIL 不进入本表的 M1 / P0 主链。它们仍由各自 Requirement ID、Roadmap 与对应 Issue 跟踪。

历史已关闭或被吸收的 Issue（包括早期 schema/runner/provider 元任务）保留为审计证据，不再作为当前 P0 的平行需求入口；旧 Issue、PR、发布标签和归档记录均不能覆盖当前 PRD Requirement 与验收状态。
