# 验收与状态

本文承载 PRD-N001–N007、M1 验收门槛、状态语言和正式里程碑门槛。状态是审计事实，不会降低任何产品需求。

## 状态语言

| 代码状态 | 含义 |
| --- | --- |
| ✅ `implemented` | 有限范围的实现及针对性验证存在；不等于真实验收完成 |
| 🟡 `partial` | 已有基础或局部能力，必要闭环仍缺失 |
| ⬜ `planned` | 无可交付实现；设计、配置或关闭 Issue 不算实现 |
| ⏸ `deferred` | 不在当前 MVP 排程，保留已有基础 |

验证状态独立：`software_verified`、`container_verified`、`browser_verified`、`real_recording_pending`、`real_device_pending`、`validation_pending`。除非有直接证据，不能声明 `real_recording_verified` 或 `measurement_equivalence_verified`。状态语言由 `AcceptanceRecord 1.0.0` 逐 gate 落盘（见下）。

## 非功能要求

| ID | 要求 |
| --- | --- |
| PRD-N001 | Evidence First：结论回溯 Run → Turn → Event → 音频区间 → Evidence → 版本；不足时弃权 |
| PRD-N002 | 原件、机器输出、人工修订分层且不可静默覆盖；Hash 和 provenance 可复核 |
| PRD-N003 | 时间、公式、统计与验证由确定性代码执行；语义节点受约束且引用证据 |
| PRD-N004 | 凭据只由后端持有；浏览器、Git、快照、报告不出现长期密钥 |
| PRD-N005 | 设备内部根因未获设备日志时不得伪称已测得；外部 ASR 不等于设备内部 ASR |
| PRD-N006 | 需求、Issue、分支、PR、测试和工作日志可追溯；合并须有明确授权 |
| PRD-N007 | 正式测量记录独立 Artifact、sample-index timebase、policy/processor/version、完整性、confidence/uncertainty 与最终化状态；跨 Pipeline 比较声明可比性 |

## M1 录音分析验收门槛

M1 尚未通过。最终验收需要授权的真实 5–20 分钟录音、有效云服务调用、人工标注/复核与 Windows 浏览器完整路径。

- 导入、标准化、QA、File ASR、聚类/归属、Timeline、MetricResult、语义、Findings、人工修订和报告的实际链路必须分别呈现成功、失败、缺失和弃权。
- 软件 fixture、预览版或 CI 只能证明有限契约，不能代替真实录音质量、真实云服务、实体设备或人工标注。
- 验收记录必须保留 Artifact/Hash、版本、环境、样本选择、分母、失败样本和不确定性。

### 验收证据契约（不改变门槛）

M1 的五个 gate（`software_verified`、`container_verified`、`browser_verified`、`real_cloud_verified`、`real_recording_verified`）由 `AcceptanceRecord 1.0.0`（`schemas/acceptance-evidence.schema.json`）记录，并由确定性检查器 `aivoicebench/acceptance_evidence.py`（CLI `python -m aivoicebench acceptance init|check`）校验。该契约把本节门槛变成可执行检查，**不降低任何门槛**：

- `real_recording_verified` 只在存在授权真实样本（5–20 分钟区间）、人工复核证据与机器原件分离、且曝光扫描干净时才成立；synthetic/fixture 样本、mock、CI、机器生成却被记为人工的标注会被判为未授权声明并使记录 `invalid`。
- 每个 stage 的分母必须显式计入 complete/partial/failed/unknown/abstained/not_applicable 且与 `expected_total` 对账，**禁止只报成功的分母**。
- 每条结论必须可回溯 Run → Turn/Event → 音频区间 → Evidence → processor/model/policy version；缺口作为显式 open item 报出。
- 角色修改必须生成新 AnalysisRevision、保留旧结果字节并给出 recompute/diff 证据，且不得重跑识别/聚类。
- 未达到的 gate 必须写明缺口；**没有真实验收记录时不得把任何 gate 写成已验证**。

检查器本身只是工具，其通过不等于 M1 通过；M1 仍以真实证据记录为准。

## 正式里程碑

| 里程碑 | 目标 | 通过门槛 |
| --- | --- | --- |
| M1 | Recording Analysis MVP | 上述真实录音与人工复核门槛通过 |
| M2 | Fixed Active Voice Test | 冻结 Case/刺激、可靠控制、Live Measurement Audio 与正式 Active Result 最小闭环 |
| M3 | Free Test Agent | 受控 Agent、实时 Control、预算/Coverage、完整 Trace 与真实设备验收 |
| M4 | 独立关联与 Equivalence | 可复核配对、时钟/不确定性记录与已批准的偏差/一致性分析 |
| M5 | Compare/Regression | 经复核问题可最小化为固定 Case，并输出可比差异证据 |

M2–M5 不会降低 M1 的真实录音门槛；控制成功、播放完成或 Agent 运行完成均不自动代表正式 Measurement Result 通过。
