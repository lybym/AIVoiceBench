# LLM Harness — 实现说明

产品语义能力、结构输出、证据边界和验收统一见 [PRD-F010](PRD.md)。本文描述 main 19d3a07 / v0.1.3 的代码事实。

## 当前调用路径

`LLMJudge` 调用 `LLMProvider.complete(system_prompt, user_prompt, dimension, context)`，返回结构结果与 `LLMInvocation`。默认 `UnavailableLLMProvider`，`MockLLMProvider` 仅为显式软件 fixture/CLI 测试；不能把 Mock 结果称为真实模型评估。

`llm_provider.py` 已有兼容 Chat Completions 的 OpenAI/Volcengine Provider，失败、非法 JSON、无有效引用和模型编造时间会被拒绝。它不是完整的 schema-constrained tool loop：上下文/语义锚点、统一原生调用审计和更多产品维度仍需接通。

v0.1.3 的模型管理可为下一 Web Run 选择 Judge profile，保留脱敏配置快照；main 未合入该功能。语音 Provider 适配器仍为后续集成，配置本身不证明连通。

## 时间与 Findings

`pipeline._integrate_llm_metrics()` 当前保留反馈和有效回答时延为 insufficient_evidence。未来应由模型选择已有边界 ID，再由确定性公式计算；不能恢复旧文档中由模型直接返回毫秒并视为 observed 的做法。

JudgeResult schema、已有样例和运行时验证仍需统一；schema 接受字段不代表该字段具有有效声学 Evidence。Finding 候选、疑似层和日志验证要求的产品规则见 PRD-F011。

代码：`aivoicebench/llm.py`、`llm_provider.py`、`pipeline.py`；测试：`test_llm.py`、`test_evidence_guards.py`、`test_revision_pipeline.py`；Issue #10/#30。旧示例原文集中在 [历史快照](product/archive/2026-09-10/21-llm-harness.md)。
