# LLM Harness — 实现说明

产品语义能力、结构输出、证据边界和验收统一见 [PRD-F010/PRD-F011](PRD.md)。本文描述 Issue #10 之后的代码事实。

## 当前调用路径

`LLMJudge.evaluate()` 在**一个 Timeline 上**逐 Turn 运行维度判定，产出一份 `JudgeResults 1.0.0` artifact（`aivoicebench/llm.py`）：

```text
EventTimeline + Turns + MetricResult（确定性，先算）
  ↓ 每个 Turn 的允许引用范围 = 该 Turn 自己的 events + 它们的 evidence
Provider.complete(system_prompt, context, dimension, context)
  ↓ 原始 provider 文本（原样保留）
Harness 校验：锚点只能"选择"不能"编造"、引用必须存在、失败 invocation 不得 observed
  ↓
JudgeResult 1.0.0（validated）+ Invocation（raw response + provider/model/prompt/version）
  ↓ 不合格 → abstention（含原因），不产生判定
```

- **时间只能被选择，不能被编造。** `meaningful_response` 与 `feedback_detection` 不再接受模型自报毫秒：`context['anchors']` 给出该 Turn 内**已有** measured 边界（事件 id + start/end + 证据），provider 只能返回 `anchor_refs` 的 `anchor_id`；harness 用该锚点自身的毫秒回填 `meaningful_response_start_ms` / `feedback_start_ms` / `feedback_end_ms`。未知锚点、缺失锚点、或自带毫秒的锚点一律被拒绝。`derived` 事件（如 fusion 的 `response_start`/`response_end`）不是 measured 边界，不可作为锚点。若 Turn 内不存在所需锚点，结果是 `insufficient_evidence` 并说明原因，而不是插值。
- **语义判定需要 criterion 与引用。** `semantic_response`（criterion `CRIT-SEMANTIC-RESPONSE`，PRD-M003）与 `barge_in_compliance`（`CRIT-BARGE-IN-COMPLIANCE`，PRD-M006）必须给出布尔 `semantic_decision`、criterion id/version、`judge_profile` 与至少一个可解析引用。缺引用、非布尔决定、criterion 不一致、invocation 未成功都会把结果降级为 `insufficient_evidence` + `abstention_reason`。
- **引用范围由 harness 拥有。** 逐 Turn 判定只能引用该 Turn 的 events 与它们的 evidence；run 级判定（`conversation_quality`、`finding_candidate`）可引用同一 Timeline 的任意对象。被选中的 event 会自动把该 event 自身的 evidence 并入 `evidence_refs`，因此"事件证据必须包含在 metric/finding evidence_ids 中"这条不变式由构造保证。跨 Turn 引用被丢弃，不会借用别的 Turn 的证据。
- **疑似根因永远是假设。** 具名 `suspected_layer` 一律强制 `requires_log_verification=true`，`attribution_confidence` 上限 0.99；没有设备日志时不得升为 verified。

## 受约束语义证据（PRD-M003 / PRD-M006）

`aivoicebench/semantic_evidence.py` 是 JudgeResult 变成**测量输入**的唯一通道，也是决定资格的唯一 place：

- 每条 record 需要 `kind`（`semantic_response` / `barge_in_compliance`）、布尔 `decision`、`criterion_id`/`criterion_version`、`judge_profile`、≥1 个可解析 evidence/event 引用；
- 引用必须属于被判定 Turn 且存在于同一 Timeline，否则弃权；
- 不合格结果进入 `abstentions`（带 state 与原因），**不会**变成值。缺一条 record 就是"没有语义证据"，`metrics` 会明确弃权并给出原因，不会默认成 `false`。

`compute_timeline_metrics(timeline, semantic_evidence=records)` 是唯一消费者；`pipeline.py` 与 `import_pipeline.py` 都按此调用。确定性指标先算，Judge 只补充确定性代码无法回答的语义问题。

## Recording Analysis 主链

`_run_role_dependent_chain` 顺序为 attribution → fusion → turns → timeline → **judge** → metrics（含 semantic evidence）→ **findings**：

- Judge 阶段在 metrics 之前运行，因为 metric 文档一旦发布即不可变：PRD-M003/M006 必须一次性带上语义记录。Judge 内部用内存中的确定性 metrics 作为 context，因此确定性问题的答案仍来自确定性代码。
- 未配置 Judge provider 时 `judge` 阶段以 `insufficient_evidence` 与准确原因发布，**不调用任何模型**，也不编造语义值。
- 角色未人工确认（匿名 cluster）时 Turn/Timeline/Metrics/Judge/Findings 全部以角色 Gate 原因弃权 —— Judge 永远不参与角色判定（见 [#95](https://github.com/lybym/AIVoiceBench/issues/95)）。
- **一次判定一个消费者。** `barge_in_compliance` 只在 Turn 真的带 `interrupt_start` 事件时请求（`semantic_evidence.has_interrupt_evidence`），这与 PRD-M006 判定适用性的谓词是同一条；不会出现"花了一次 provider 调用但没有任何 metric 消费该记录"的情况，也不会用 turn flag 与事件两种规则推导同一语义。
- 保存角色 revision（`apply_role_mapping`）会**重跑** Judge/Findings 并沿用同一 provider 配置；确认角色不会静默丢掉语义阶段或换掉模型。
- 引擎校验自身产物：Judge artifact 违反自身契约时该阶段 `failed`，并把被拒文档与逐条校验错误写成 `retained_diagnostic`（`judge-results-contract-violation.json`）便于诊断；不合格文档绝不发布为证据。

## 原始输出与 provenance

- 每次调用保留原始 provider 文本（`judge-raw.json`、invocations 的 `raw_response` 与 `raw_response_sha256`），与 validated result 分开保存；解析失败时原始文本同样保留，便于审计"模型究竟返回了什么"。
- provenance 为 provider / model / prompt_version / criteria_version / `profile_id`；`metric.judge_profile` 记录 `profile_id`。凭据永不进入 artifact：`credential_errors` 拒绝含凭据形状 key/value 的文档（PRD-N004）。
- 默认 provider 仍是 `UnavailableLLMProvider`；`MockLLMProvider` 只是软件 fixture（`--provider mock` 或测试注入），不能把 Mock 结果称为真实模型评估。

## 代码与测试

代码：`aivoicebench/llm.py`、`llm_provider.py`、`semantic_evidence.py`、`findings.py`、`pipeline.py`、`import_pipeline.py`；校验：`validation.judge_document_errors` / `judge_result_errors` / `semantic_evidence_errors` / `credential_errors`。

测试：`tests/test_judge_contract.py`（判定、锚点选择、引用完整性、弃权、凭据）、`tests/test_llm.py`（fixture 行为、artifact、单文档规则）、`tests/test_findings_generation.py`（Finding 生成/弃权/迁移）、`tests/test_judge_import_stage.py`（Recording Analysis 主链集成）。

## 仍然存在的限制

- **真实 provider 调用与真实录音验收未完成**：当前证据是 fixture 级（无音频、无网络）。"至少一次授权真实 Recording Analysis Run 执行真实 Judge 路径"由 [#85](https://github.com/lybym/AIVoiceBench/issues/85) 承载。
- `context_understanding` / `instruction_following` 等维度已在 schema 声明，但没有对应的 canonical metric 绑定；它们当前不是 PRD-M003/M006 的输入。
- context/instruction 类语义 metric（`context_success` / `instruction_success`）仍未产出。
- Judge/Findings 尚未接入 Web 的 Finding 复核工作台（[#11](https://github.com/lybym/AIVoiceBench/issues/11)）。