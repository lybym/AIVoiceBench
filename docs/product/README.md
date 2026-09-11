# 产品文档中心

**唯一权威需求：[../PRD.md](../PRD.md)**。本目录存放归集清单、历史快照和追溯说明，不生成第二份需求台账。审阅者直接修改 PRD 中对应编号；代码状态和验收状态必须分开更新。

## 文档职责与归集映射

| 原分散来源 | 归入 PRD 的内容 | 现在职责 |
| --- | --- | --- |
| README | 定位、交付、功能进展 | 运行入口与简短介绍；链接 PRD |
| 00-project-charter | 产品目标、用例、MVP 范围 | 使命/背景，已移除 EXE/HIL 当前门槛 |
| 01-system-architecture | Primary Workflow、处理职责、数据/语义边界 | 技术目标架构，不宣称所有组件已实现 |
| 02-test-methodology | 能力范围、证据原则、真实验收 | 测试方法/观测限制；产品验收链接 PRD |
| 03-metric-definition | 用户体感指标含义和适用范围 | 技术公式、单位、契约与统计；实现状态看 PRD-Mxxx |
| 04-development-roadmap | P0/P1/P2/P3、依赖 | 仅 PRD ID → Issue → 排程 |
| 05-project-context | 交付约定、产品需求、决策混合内容 | 仅上下文、决策来源、工作区与审计边界 |
| 13-import-first-migration | 历史迁移/Issue 处置 | 原文集中归档，旧路径保留入口 |
| 16-model-management（v0.1.3） | 模型配置范围与未接入能力 | PRD-F015/F016；当前 main 已有配置/路由与云 ASR/聚类说明，见 [模型管理](../16-model-management.md) |
| 17～22 技术专题 | acoustic/fusion/metric/API/Judge/report 的功能描述 | 实现/接口说明，产品要求与完成状态回到 PRD；纠正已发现的旧实现叙述 |
| 06-work-log | 不归集为需求 | 历史执行事实，原样保留并追加本次记录 |

## 原文快照

[archive/2026-09-10/](archive/2026-09-10/) 保存本次编辑前 main 19d3a07 的 README、CONTRIBUTING、00～05、13、18～21 等来源。具体文件见 [快照说明](archive/2026-09-10/README.md)。保留旧口径是为了追溯，不能当成活动需求；相对路径按原文件位置解释。

## 已消除的冲突

- Windows EXE/installer 交付 → 用户明确选择 Docker/Web，PRD-N001。
- HIL 作为首要入口 → 主动测试/录音分析双主流程；M1 录音分析优先，普通音频为 F023/M2，专业 HIL 为 F022/P3。
- Project Context 作为完整产品协议 → PRD 是唯一入口，Context 不再扩写功能/验收。
- 交替分配说话人、Mock 默认成功 → 现有代码已弃权；PRD-F006/F010 不允许把旧实现当需求。
- “发布/文件存在/测试通过”等于完成 → 分别记录实现、集成位置与真实验收状态。

新增需求不应在该目录另建竞争性 PRD。可增加用户批准的设计稿/调研材料，但必须声明对应 PRD ID、用途及其不改变产品范围。
