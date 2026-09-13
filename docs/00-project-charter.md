# Project Charter — 项目使命

AIVoiceBench 将随意的 AI 语音设备对话体验转为可复现、可追溯的工程评估，帮助测试、产品和算法团队讨论缺陷、版本退化与后续供应商比较。

产品以 **Active Measurement** 和 **Recording Analysis** 两条独立正式测量链实现这一使命：前者从本地 Live Measurement Audio 产生结果，后者从独立 External Recording 产生结果。它们不共享声学原件，但共享 Canonical Event 语义、指标定义、MetricResult 契约和版本化 Measurement Policy；外部录音不是主动测量“转正”的前置条件。

本文件只解释为什么做。产品范围、导入主链路、Docker/Web 交付、优先级和验收统一由 [PRD](PRD.md) 定义；不再保存独立交付要求。

原先以 HIL 和 Windows 安装包为中心的章程已被用户后续决定替代，完整原文存于 [历史快照](product/archive/2026-09-10/00-project-charter.md)，不作为当前需求。
