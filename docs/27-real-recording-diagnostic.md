# 2026-09-18 真实录音诊断与迭代清单

> 本文记录一次授权私有录音的诊断事实。录音、凭据和生成报告均保留在 Git 外；以下结果不等于 `real_recording_verified`，也不关闭 [#85](https://github.com/lybym/AIVoiceBench/issues/85)。

## 运行结果

在 `v0.5.0` 本地 Docker 部署上，使用服务器侧只读 `providers.yaml` 与本地 secret 文件执行 Recording Analysis。火山 Seed ASR 2.0 标准版完成异步识别，得到 67 个带时间的 utterance、5 个匿名 speaker clusters；其中 1 处 word timing 缺失被保留为显式 gap。诊断阶段曾让 LLM 提出 3 个 tester cluster 与 2 个 device cluster，全部为 `needs_review`，并形成 6 个候选 turns；根据随后确认的产品决定，这些机器角色与候选 turns 不进入正式结果。

指标没有生成不是 UI 丢字段：声学链产生的 84 个 segments 没有获得 speaker cluster / role evidence，Fusion、Timeline 与 Metric Engine 因证据不足正确弃权。该缺口由 [#94](https://github.com/lybym/AIVoiceBench/issues/94) 跟踪。

## 本次暴露的问题

| 类别 | 现象 | 处理/状态 |
| --- | --- | --- |
| 凭据 | 错误 API Key 返回 401 | 运维配置问题；不在代码中保存 key |
| 权限 | 正确 key 但 resource 未开通返回 403 | 控制台开通后恢复；错误必须保留在 invocation audit |
| Provider contract | adapter 只接受同步极速版，无法使用 `volc.seedasr.auc` | 修复候选支持封闭的 Seed 标准版 submit/query 契约；[#93](https://github.com/lybym/AIVoiceBench/issues/93) |
| 时间戳 | 少量 word offset 为 `-1` 时整份 Transcript 被拒绝 | 修复候选保留有效 utterance/speaker evidence，把坏 word 标为 gap/partial |
| 部分结果传播 | ASR 为 `partial` 时，导入链不把 transcript 交给 diarization | 修复候选允许有用的 partial evidence 下传 |
| 断线/恢复 | HTTP 客户端中断后云任务已完成，但本地可能重复提交 | 修复候选先恢复既有 request ID 并继续 query，禁止隐式重复计费提交 |
| 轮询超时 | job 仍在处理中但本地 poll window 耗尽，若写成 failed，后续可能误发新 submit | 保持 recoverable pending；resume 继续 query 原 request ID |
| 角色判断 | LLM role JSON 曾被 generic JudgeResult schema 拒绝，且机器提议无法作为角色真值 | 产品决定改为用户人工确认；Recording Analysis 不再调用 LLM 归因，见 #95 |
| 角色确认闭环 | 当前 Web/API 没有 cluster 播放、人工 tester/device/unknown mapping、revision 与重分析 Gate | 尚未实现；[#95](https://github.com/lybym/AIVoiceBench/issues/95) |
| 指标 | ASR 有 speaker labels，但声学 segments 无 speaker/role，指标为空 | 尚未修复；需确定性 acoustic↔ASR span alignment、coverage 诊断和低音量样本评估；[#94](https://github.com/lybym/AIVoiceBench/issues/94) |

## 已观察到的证据边界

- Seed standard 的 speaker label 是匿名 cluster，不是 tester/device 真值。
- 本次 LLM 角色输出只保留为诊断历史，不作为产品证据；正式角色只能来自用户保存的人工 mapping。
- ASR timestamp 只能作为 provider evidence/对齐输入，不能替代声学边界。
- 单个私有样本证明“真实云调用曾成功并暴露这些缺口”，不证明识别准确率、speaker coverage、低音量设备区分质量或 M1 验收。
- 正式验收仍需 #85 要求的 5–20 分钟授权样本、人工作业、分母、浏览器回放、修订重分析和完整指标证据。

## Issue 映射

- [#93](https://github.com/lybym/AIVoiceBench/issues/93)：Seed standard 异步任务恢复、partial transcript/speaker evidence 传播与测试。
- [#94](https://github.com/lybym/AIVoiceBench/issues/94)：低音量设备场景下 acoustic segments 与 ASR speaker spans 的证据安全对齐，以及“为何无指标”的可解释输出。
- [#95](https://github.com/lybym/AIVoiceBench/issues/95)：用户人工确认 speaker roles，保存新 revision 后重跑下游并生成正式测试报告；禁止 LLM 角色判断。
- 父任务保持 [#22](https://github.com/lybym/AIVoiceBench/issues/22)、[#24](https://github.com/lybym/AIVoiceBench/issues/24)、[#25](https://github.com/lybym/AIVoiceBench/issues/25)、[#27](https://github.com/lybym/AIVoiceBench/issues/27)；最终真实验收仍是 [#85](https://github.com/lybym/AIVoiceBench/issues/85)。
