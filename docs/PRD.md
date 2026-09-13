---
prd_id: AIVB-PRD
prd_version: 1.5.1
status: modularized_for_owner_review
updated: 2026-09-13
implementation_baseline: v0.4.0@9632844da6ddcef757fd7df20a6bb12e46853cdd
main_baseline: 9632844da6ddcef757fd7df20a6bb12e46853cdd
---

# AIVoiceBench 产品需求文档（PRD）

**产品唯一需求入口。** 本文定义产品边界、稳定 Requirement ID、全局原则、需求目录与正式里程碑。详细功能、指标、验收、追踪和版本历史按职责收敛到 [PRD 分册](prd/README.md)。分册不是平行 PRD；任何新增或变更的产品要求均须先更新本文并保留既有 ID。

## 1. 审阅规则与状态语言

每项功能有稳定 `PRD-Fxxx` 编号，指标有 `PRD-Mxxx`，非功能要求有 `PRD-Nxxx`。编号不因重排而变化，也不复用。**需求是目标；代码与验证状态是截至审计基线的事实。**

| 代码状态 | 含义 |
| --- | --- |
| ✅ `implemented` | 已有有限范围实现及针对性验证，不等于真实验收完成 |
| 🟡 `partial` | 有基础或局部能力，必要闭环仍缺失 |
| ⬜ `planned` | 无可交付实现；设计、配置或关闭 Issue 不算实现 |
| ⏸ `deferred` | 非当前排程，保留已有基础 |

验证状态独立：`software_verified`、`container_verified`、`browser_verified`、`real_recording_pending`、`real_device_pending` 和 `validation_pending` 不可互相替代。没有真实证据时，不得声明 `real_recording_verified` 或 `measurement_equivalence_verified`。

需求、实现建议与实现状态必须分开：只有项目所有者能改变产品范围；架构、schema、Issue、PR 和工作日志只能说明实现方式或事实，不能将推断升级为需求或完成声明。

## 2. 产品定义与不可违反边界

AIVoiceBench 是面向 AI 玩具、陪伴终端、音箱等具备麦克风和扬声器设备的可复现、可追溯、可比较、可回归评测 Harness。测试工程师、产品负责人、算法/软件工程师及采购/供应商评估人员基于同一证据链审阅结果。

### 两条一级工作流与双正式 Measurement Pipeline

```text
Active Measurement                         Recording Analysis
Live Measurement Audio                     External Recording
Online Event Producer                      Offline Event Producer
                         \                 /
                          Canonical EventTimeline
                                   ↓
                         Canonical Metric Engine
                                   ↓
                              MetricResult
```

- **Active Voice Test** 包含 Fixed Case Runner 与 Free Test Agent：平台播放刺激、观察设备、控制下一步并形成 Execution Run。
- **Active Measurement** 从独立 Live Measurement Audio 形成正式结果；它不依赖 External Recording 才能“转正”。
- **Recording Analysis** 导入独立 External Recording，进行离线声学、语义、审计和复核；它同样是正式 Pipeline。
- 两条 Pipeline 不共享同一声学原件；它们共享 Canonical Event 语义、PRD-M001–M010 指标定义、MetricResult 契约和版本化 Measurement Policy。

### 证据与时间边界

1. Evidence First：每个结论可回溯到 Run、Turn、Event、Artifact 区间、Evidence 与处理器/模型版本；证据不足时弃权，不猜测。
2. Control Evidence、Active Measurement Evidence 与 External Recording Evidence 分层保存；控制成功、播放完成或 ASR final 不等于正式测量通过。
3. Active 正式声学时间以 `sample_index / sample_rate` 的 `audio_relative_ms` 为准。播放 callback、wall/monotonic clock、接收时间和 provider timestamp 只可作控制、诊断或对齐先验。
4. 外部 ASR 不等于设备内部 ASR；混音不伪装成独立声道；无法可靠归属或识别边界时输出 unknown、needs_review、insufficient_evidence 或 invalid。
5. 原件、机器输出和人工修订分层保存；不以人工更正覆盖原始 Artifact。长期凭据只由后端持有。

### 交付与路线边界

交付形态是 Docker 后端与 Web UI，经 Windows 浏览器使用。普通电脑扬声器与麦克风是 M2 的基础能力；专业 HIL、同步校准、loopback 和 Remote Station 是 P3 扩展。Recording Analysis 使用 File ASR；Active 控制使用 Streaming ASR。二者的 provider timestamp 都不直接充当正式声学边界。

## 3. 需求目录

详细验收条件和依据位于相应分册；本表是稳定 ID、范围、优先级和当前事实的总览。

| ID | 能力 | 优先级 | 当前状态 | 分册 |
| --- | --- | --- | --- | --- |
| F001–F003 | 录音导入、Artifact provenance、标准化与 QA | P0 | ✅ implemented（真实录音待验收） | [Recording Analysis](prd/recording-analysis.md) |
| F004–F009 | 编排、File ASR、归属、Turn/EventTimeline、确定性指标 | P0 | 🟡 partial | [Recording Analysis](prd/recording-analysis.md) |
| F010–F017 | Judge、Findings、修订、报告、Web、模型配置与重分析 | P0/P1 | ✅/🟡，逐项见分册 | [Recording Analysis](prd/recording-analysis.md) |
| F018–F019 | Compare 与 Frozen Golden Voice | P2/P1 | ⬜ planned | [Recording Analysis](prd/recording-analysis.md) |
| F020–F021 | Fixed Runner 与 Free Test Agent | P1 / M2–M3 | 🟡 partial | [Active Measurement](prd/active-measurement.md) |
| F022 | 专业 HIL / Station | P3 | ⏸ deferred | [Active Measurement](prd/active-measurement.md) |
| F023 | 基础播放与持续麦克风采集 | P1 / M2 | 🟡 partial | [Active Measurement](prd/active-measurement.md) |
| F024 | Execution/Analysis Run 独立关联 | P1 / M2–M4 | ⬜ planned | [Active Measurement](prd/active-measurement.md) |
| F025 | Active Measurement Pipeline | P1 / M2 | ⬜ planned | [Active Measurement](prd/active-measurement.md) |
| F026 | Measurement Equivalence Validation | P1 / M4 | ⬜ planned | [Active Measurement](prd/active-measurement.md) |
| M001–M010 | 体验/时延/打断/覆盖指标 | 跨里程碑 | 逐项 evidence-first | [Metric requirements](prd/metric-requirements.md) |
| N001–N007 | Evidence、不可变性、确定性、安全与测量 provenance | 跨里程碑 | 逐项见分册 | [Acceptance status](prd/acceptance-status.md) |

### MVP 与正式里程碑

P0/P1 表示开发先后，不表示产品可选性。M1 是 Recording Analysis MVP；F016 的 File ASR 部分属于 M1，TTS/F019/F020/F023/F025 属于 M2，Streaming ASR/F021 属于 M3，F024 自动关联与 F026 属于 M4。状态与验证门槛见 [验收与状态](prd/acceptance-status.md)。

| 里程碑 | 目标 |
| --- | --- |
| M1 | 独立 External Recording 的可信导入、分析、证据和人工验收闭环 |
| M2 | Fixed Active Test 与独立 Live Measurement Audio 的最小正式结果闭环 |
| M3 | 受控 Free Test Agent、实时控制、预算/Coverage 与真实设备证据 |
| M4 | 可复核的独立关联与 Measurement Equivalence |
| M5 | Compare、Regression 与由经复核问题最小化而来的固定 Case |

## 4. 分册职责与变更流程

| 内容 | 权威位置 |
| --- | --- |
| 产品定义、全局原则、需求目录、正式里程碑 | 本文 |
| Recording 与 Active 的详细功能验收 | [功能分册](prd/README.md) |
| M001–M010 产品指标 | [指标需求](prd/metric-requirements.md) |
| 非功能要求、验收门槛、状态语言 | [验收与状态](prd/acceptance-status.md) |
| Requirement ↔ Issue ↔ PR 映射 | [追踪](prd/traceability.md) |
| PRD 版本历史 | [变更历史](prd/changelog.md) |

1. 变更产品行为时先定位 Requirement ID，并在同一 PR 更新本入口和唯一的详细正文。
2. Issue 写明 `PRD refs`、范围、验收和依赖；不按状态机械重复建 Issue。
3. `planned → partial → implemented` 需要代码入口、针对性测试与 commit/PR/tag 证据；真实验收需要独立真实证据。
4. 拆分后的模块不可复制或分叉同一需求正文；新文件必须在 [PRD 分册导航](prd/README.md) 和本文目录登记。
5. 合并仅在项目所有者明确授权时执行。

## 5. 当前审计与历史入口

当前实现基线是 `v0.4.0@9632844da6ddcef757fd7df20a6bb12e46853cdd`，也是发布时的 main 提交。该基线收敛 alpha.6 自由对话修复、语义角色提议与模块化 PRD；历史提交、预发布、Issue/PR 和工作日志仍作为审计证据。当前追踪和主要缺口见 [Requirement 追踪与审计结论](prd/traceability.md)，版本演进见 [PRD 变更历史](prd/changelog.md)。

技术专题文件不构成平行 PRD：例如 [Active Measurement 设计](25-active-measurement.md) 说明实现边界，[指标定义](03-metric-definition.md) 说明契约与公式，[Roadmap](04-development-roadmap.md) 说明实施顺序。
