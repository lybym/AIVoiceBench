# Active Measurement 产品需求

本文承载 PRD-F020–F026。Active Voice Test 与 Recording Analysis 都是一级工作流；两条正式 Measurement Pipeline 不共享声学原件、也不互为结果“转正”前置条件。

## PRD-F020 — Active Voice Test Controller / Fixed Case Runner

**状态：🟡 partial；优先级：P1 / M2。** 固定 Case 冻结刺激、停顿、触发条件与超时策略，由确定性 Controller 执行，不依赖 LLM 决策。

- 创建可追溯 Execution Run，记录 Case/策略/设备/配置/音频 Hash、计划和实际动作、观察、状态迁移与关闭原因。
- 支持多轮播放、响应观察、条件触发、取消与失败处理；无响应、低置信度、音频故障和中断必须有明确结果，不能无限等待或伪造成功。
- 相同 Case 与相同 Observation 序列重放时，控制决策应一致；控制层“回答结束”不等同正式声学时延或 Barge-in 判定。
- Fixed Mode 的基础推进可仅依赖 VAD；只有需要语义观察的 Case 才显式要求 ASR。

已有受控的软件/容器/浏览器验证覆盖停止、轮次校验、超时状态和最小 `execution-record`；真实扬声器、麦克风和实体设备多轮验收仍未完成。

## PRD-F021 — Free / Exploratory Voice Test Agent

**状态：🟡 partial；优先级：P1 / M3。** 用户提供 Goal、Strategy、Coverage、Budget、Stop Condition 与禁止行为；受 Harness 约束的 Agent 基于 Observation 决定下一步动作。

- 正式控制路径是 Browser Mic → VAD/Streaming ASR → Observation → Agent Decision → TTS/Playback，不以整轮录音后 File ASR 作为主路径。
- partial 只用于展示和留痕；Agent 的下一轮决策只使用 final。设备文本是 Control Evidence，provider timestamp 不能成为正式声学边界。
- 保存版本化目标、策略、允许工具、预算、停止条件、Observation、Decision、实际动作和失败理由；超预算、无 final、服务失败或用户停止须明确处置。
- File ASR 整轮上传只能作为操作者显式选择且显式标注的 fallback；缺少 Streaming ASR 时 `auto` 不得静默降级。

当前最小实时链路、后端能力预检、显式降级、时域 RMS VAD 判停和失败状态已有有限验证；真实云调用、真实设备、Coverage 与预算闭环仍待验收。

## PRD-F022 — 专业 HIL / Station

**状态：⏸ deferred；优先级：P3。** 保留专业声卡播放/录制、同步、loopback、SPL 校准、physical HIL 与 Remote Station 扩展。它不阻塞普通电脑的 F023；软件 fixture 不能冒充硬件验收。

## PRD-F023 — 基础本地播放与麦克风采集

**状态：🟡 partial；优先级：P1 / M2。** 普通 Windows 电脑的扬声器播放和浏览器麦克风持续采集同时服务 Control Plane 与 Measurement Plane。

- Control Plane 可使用连续 PCM、RMS VAD 和 Streaming ASR 进行播放、观察和会话控制。
- Measurement Plane 必须另行满足 F025 的持久化 Artifact、sample clock、完整性和 Evidence Contract，不能把控制输入自动升级为正式 Measurement Evidence。
- 设备选择、权限/中断/断开状态、实际格式、丢帧/重复/缺口以及播放/采集时间基准需要可见且可审计。
- 边播放边监听、真实声场泄漏与设备归属不确定时必须弃权或停止；外部录音可用于独立等价性验证，但非 Active Result 的前置资格。

## PRD-F024 — Execution Run 与独立录音 Analysis Run 关联

**状态：⬜ planned；优先级：P1 / M2–M4。** Active Execution Run 与 External Recording Analysis Run 可手动或自动关联，用于复测、审计与等价性验证。

- 关联保存双方身份、原件 Hash、Case/Turn 引用、来源、时间映射、偏移/漂移、不确定性与置信度。
- 关联歧义、录音缺段或无可靠映射时转人工确认；修订保留历史，禁止静默绑定或跨用声学证据。
- Active Measurement 不依赖关联即可形成正式结果；独立导入也不要求存在 Execution Run。

## PRD-F025 — Active Measurement Pipeline

**状态：⬜ planned；优先级：P1 / M2。** Active Voice Test 必须能独立从 Live Measurement Audio 形成可信、可审计的 Measurement Result。

- 每次 Active Run 在第一句播放前持续采集至完成、停止或失败，保存不可变 Measurement Audio Artifact、SHA-256、格式、sample count、帧完整性、设备信息和 `measurement_policy_version`。
- 正式声学时间使用 `sample_index / sample_rate` 的 `audio_relative_ms`；wall/monotonic/receive time 只作审计、控制或诊断。
- 保存 Stimulus Reference，并通过 reference-assisted alignment 产生 tester 边界；播放 callback 只作对齐先验。
- streaming-compatible AcousticBoundaryPolicy 生成带 confidence/uncertainty 的 Canonical Events；未知角色或不足证据保持 unknown / insufficient_evidence。
- Active Timeline 与 Recording Timeline 使用同一 Canonical Event 语义、唯一 Canonical Metric Engine 与 PRD-M001–M010，不创建实时/离线平行公式。
- 结果区分 provisional、finalized 与 abstained/invalid；不得重载既有 MetricResult `status`，须通过兼容演进表达 pipeline、policy 与最终化状态。

详细实现设计见 [Active Measurement 设计](../25-active-measurement.md)。本文不将该设计写成已实现状态。

## PRD-F026 — Measurement Equivalence Validation

**状态：⬜ planned；优先级：P1 / M4。** 对同一次可关联执行，比较 Active Measurement 与独立 External Recording 的相同定义指标。

- 预先冻结可比性、配对、有效/无效样本、缺失、阈值、统计方法和版本策略；禁止事后挑样本或只报均值。
- 报告 paired coverage、匹配拒绝原因、signed bias、absolute error、median、P95、agreement/correlation、方向性差异及不确定性。
- 首次 first-speech 工程目标为 median |Δ| ≤ 30 ms、P95 ≤ 80 ms、|bias| ≤ 20 ms；仅为未验证工程目标，非行业标准或完成声明。
- 配对不足、对齐不可靠、时间基不兼容或声场差异过大时输出 validation_pending / insufficient_evidence，不强行比较。

## Barge-in 边界

单麦克风混音下，未具备 stimulus cancellation、AEC/loopback 或 source-aware evidence 时，普通 turn-taking 先闭环；PRD-M005/M006/M007/M009 的高级重叠、停止与语义判断必须弃权，不得以 Control VAD 或播放日志替代正式测量。
