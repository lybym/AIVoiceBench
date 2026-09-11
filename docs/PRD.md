---
prd_id: AIVB-PRD
prd_version: 1.1.3
status: consolidated_for_owner_review
updated: 2026-09-11
implementation_baseline: v0.2.0-alpha.2@c612d36a61a5cbc90f629677b28d64228316f1d0
main_baseline: c612d36a61a5cbc90f629677b28d64228316f1d0
---

# AIVoiceBench 产品需求文档（PRD）

**产品唯一需求入口。** 本文归集已明确的用户要求，不因当前代码缺少功能而降低验收要求。需求由项目所有者审阅修改；架构、Issue、工作日志不能自行改变本文的产品范围。直接的新用户指令优先，开发者应在同一 PR 中同步本文并说明差异，不能把自己的推断写成用户批准。

## 1. 如何审阅与识别实现状态

每项功能有稳定 `PRD-Fxxx` 编号，指标有 `PRD-Mxxx`，非功能要求有 `PRD-Nxxx`。编号不随排序变化，也不复用已废弃编号。每项记录需求、可核对的验收条件、代码状态、验证状态和实现依据。**需求内容是目标；状态是截至本次审计的事实。**

| 标识 | 代码状态含义 |
| --- | --- |
| ✅ `implemented` 已实现 | 所标明的有限功能范围已有代码与针对性验证；不等于整个产品完成真实录音验收 |
| 🟡 `partial` 部分实现 | 有基础或局部能力，但该需求尚有未闭环的必要条件 |
| ⬜ `planned` 待实现 | 审计基线未发现可交付实现；设计、配置占位和关闭 Issue 均不算实现 |
| ⏸ `deferred` 暂缓 | 非当前 MVP 优先项；保留现有代码，需后续排期 |

验证标识独立记录：`software_verified` 软件/合成数据验证、`container_verified` 镜像验证、`browser_verified` 浏览器操作验证、`real_recording_pending` 真实录音待验收。未给定真实录音与有效服务凭据，**本 PRD 不含任何 real_recording_verified 项**。测试数量不能替代功能覆盖率，不发布“完成百分比”。

### 需求、实现建议与实现状态

三者分开记录，不得互相升级：

- **需求**（本文第 3～6 节）：产品需要什么、用户可操作能力是什么、如何验收。只有项目所有者能修改。
- **实现建议**（第 9 节索引的架构/技术文档、schema、Issue）：如何实现。具体函数名、字段名、拆分方式、调用次数、Provider 复用方式都属于实现选择，可以由开发者在技术文档中演进，**不能自动成为 PRD 没有提出的产品门槛**。典型误用：把“必须调用两次服务”当成产品要求（复用一次 ASR 原生输出即可满足同一需求）；把某个具体字段名当成验收条件。
- **实现状态**：截至某次审计或某个分支的事实，必须给出代码入口、测试和 commit/PR/tag 依据。

本文中的代码状态以 **main `c612d36` / `v0.2.0-alpha.2`** 为审计基线（2026-09-11）。未合并分支单独标注，不能写成 main 已实现；发布和软件验证也不能替代真实验收。下文代码/测试引用固定到该提交，便于复核。

### 代码位于哪里

- [main 审计源码](https://github.com/lybym/AIVoiceBench/tree/c612d36a61a5cbc90f629677b28d64228316f1d0) 与 `v0.2.0-alpha.2` 指向同一提交。PR #48/#49/#50/#53/#55/#56 已合并；#51/#52 虽显示 Closed，其实现提交已由集成历史带入 main，不能根据 PR 标签判断代码缺失。
- [v0.2.0-alpha.2](https://github.com/lybym/AIVoiceBench/releases/tag/v0.2.0-alpha.2) 已公开为 Pre-release；GitHub Latest 稳定版仍为 v0.1.3。alpha.1 标签保留、未作为公开预览版发布；当前安装入口见 [预览版说明](releases/0.2.0-alpha.2.md)。
- [CI 34511317063](https://github.com/lybym/AIVoiceBench/actions/runs/34511317063) 在 Windows/Linux 各运行 445 项测试，Linux 跳过 1 项，工作流成功；[发布工作流 34511317171](https://github.com/lybym/AIVoiceBench/actions/runs/34511317171) 成功，含镜像启动、合成三格式导入、重启恢复、附件重新加载验证。本次文档审计复核这些记录，未重新运行硬件或真实云测试。
- [PR #54](https://github.com/lybym/AIVoiceBench/pull/54) 的可选语义角色归属尚未合并；main 的聚类标签不等于 tester/device 角色，正常无角色输入仍保持 unknown。
- 真实云凭据调用、5～20 分钟真实设备录音与人工标注质量验收仍待完成。完整 M1 未通过；预览版不是产品完成声明。

## 2. 产品定义与边界

AIVoiceBench 是可复现、可追溯、可比较、可回归的 **AI Voice Terminal Evaluation Harness**，服务于 AI 玩具、陪伴终端、音箱等具备麦克风与扬声器的语音设备。

主要使用者：测试工程师分析对话和缺陷；产品负责人审阅体验与验收证据；算法/软件工程师定位退化；采购或供应商评估人员在可比条件下对照版本与设备。供应商比较是后续扩展，不挤占导入分析 MVP。

### 两条一级主流程（Primary Workflows）

**Active Voice Test（主动语音测试）与 Recording Analysis（录音分析）均为一级核心能力。** Import First 表示开发顺序：先完成 M1 录音分析，再完成 M2 固定用例主动测试、M3 自由测试 Agent；不能把平台定位缩减为上传录音后的分析工具。

```mermaid
flowchart TD
  P[AIVoiceBench] --> A[Active Voice Test 主动语音测试]
  P --> R[Recording Analysis 录音分析]
  A --> F[Fixed Case Runner 固定用例]
  A --> G[Free Voice Test Agent 自由对话]
  F --> T[TTS / Frozen Audio]
  G --> T
  T --> S[本地扬声器播放]
  S --> D[实体 AI 设备]
  D --> O[Mic / VAD / ASR 实时观察]
  O --> C[Controller 下一轮 / 条件触发]
  C --> F
  C --> G
  D -. 另一台设备全程录音 .-> E[External Recording]
  E --> R
  R --> I[Import / Audio QA / 声学与语义分析]
  I --> J[Events / Metrics / LLM Judge]
  J --> Q[Findings / Evidence / Human Verification / Report]
  Q --> K[确认并最小化 / 冻结回归 Case]
  K --> F
```

**主动语音测试：** 用户选择固定 Case，或指定 Goal、Test Strategy、Coverage、Budget 和 Stop Condition；平台主动向实体设备发问，通过实时观察控制下一轮，生成 Execution Run。固定模式冻结用例、音频、停顿和触发策略，由确定性 Controller 执行；自由模式由受 Harness 约束的 LLM Test Agent 根据观察生成下一步动作。两种 Runner 共享播放、观察和执行审计基础。

**录音分析：** External Recording → Import → Normalize / Audio QA → Acoustic Segmentation + ASR / Diarization → Source Attribution / Segment Fusion → Turn / Response Association → Automatic Events / Event Timeline → Deterministic Metrics → Structured LLM Evaluation → Findings / Evidence → Human Verification → Report → Regression。

输入通常是手机、录音笔或另一台电脑录下的**单轨混音**，同时包含测试者（人或平台播放）与 AI 设备。独立导入仍只需录音及可选设备资料，不要求先创建 TestCase、Execution Run、Timeline 或指定全部说话人。

### 实时控制与正式测量分离

| 证据类别 | 来源和用途 | 产品边界 |
| --- | --- | --- |
| Control Evidence | 平台麦克风、实时 VAD/ASR、播放日志、Agent Observation/Action；判断设备是否说完、何时打断、回答大意及下一轮策略 | 允许误差，但保留来源、置信度、时间基准与失败状态；只作为执行决策依据 |
| Measurement Evidence | 另一台设备全程录音，导入后经高质量离线声学/语义分析及必要人工复核 | 正式响应时延、抢话、Barge-in、上下文和回答质量结论均回溯外部录音，沿用 Evidence First 与不确定性要求 |

播放命令时间不等于真实声学 onset，实时 ASR 不能替代外部录音的正式转写。执行完成不代表设备通过测试；未导入外部录音时显示正式测量待补充，不将 Control Evidence 自动升级为 Measurement Evidence。执行与分析通过身份引用和带不确定性的录音时间映射关联，不直接相减不同设备的时钟（PRD-F024）。

产品闭环：**指定测试策略 → 平台主动与设备对话 → 外部设备记录真实声学表现 → 自动分析 → 发现与复核问题 → 沉淀固定回归 Case → 下一版本重新自动测试。**

### 交付边界

交付形态为 **Docker 后端 + 前端、Windows 浏览器访问 Web UI**。M1 不以现场播放和监听为验收前提；M2 必须支持普通电脑扬声器播放与麦克风实时观察（PRD-F023），不能因专业 HIL 延期而整体排除真实本地音频能力。浏览器音频或本地适配器的具体方案由技术设计确定，不假设 Docker 自动获得 Windows 音频设备。

Windows EXE/安装包、集群、复杂云端 Control Plane 不属于当前交付前提；专业声卡同步、loopback、SPL 校准和 Remote Station 继续作为 P3 扩展（PRD-F022）。既有 Audio Station/HIL、Runner、Vosk、契约和确定性引擎保留并按需扩展。

### 能力分类（非全部已实现声明）

保持原有五层评测视角：L1 声学/语音（wake word、VAD/endpoint、ASR、AEC、TTS、远场/噪声）；L2 实时交互（turn-taking、节奏、打断、TTS cancel、连续对话）；L3 AI 认知（intent、context、memory、instructions、knowledge/reasoning、工具使用）；L4 Persona/UX/Safety（自然度、情感、主动性、儿童安全、隐私、医疗与内容安全）；L5 可靠性（网络、重连、恢复、耐久、性能漂移）。能力 ID、指标 ID 和 Case ID 独立，通过引用关联。分类不意味着每一项都有检测器或属于当前 MVP 的已实现功能；实现范围逐项以下文为准，技术映射见 [测试方法](02-test-methodology.md)。

### 不可违反的产品原则

1. Evidence First：结论可回溯到 Run → Turn → Event → 音频区间 → Transcript/Evidence → 处理器/模型版本。缺失证据输出 insufficient_evidence、low_confidence 或 needs_review，不猜时间、得分或内部根因。
2. 不把 ASR/diarization/LLM 估计边界写成精确声学真值；不按先后顺序强行分配 tester/device；混音不能伪装成独立声道。
3. 文件、Hash、音频元数据、信号时间、公式、CER/WER、统计和验证由确定性代码执行；语义决策由有约束、有证据的模型节点执行。
4. 原始文件、机器输出和人工修订分层保存；不得以“人工修正”为由覆盖机器原件。
5. 云 ASR/TTS/音频服务可优先选用，Vosk 保留离线 fallback。实现云接口前核对官方文档，不把历史 endpoint/resource/model/voice/API version 固化为产品要求。
6. Control Evidence 与 Measurement Evidence 分层保存；保留计划动作、实际动作和观测差异，不以执行成功替代正式测量通过。
7. 无设备日志不能宣称已测得内部 VAD/ASR/LLM/TTS 延迟；外部 ASR 不是设备内部 ASR；黑盒回声行为不是 ERLE。疑似层只能带置信度和 requires_log_verification。

## 3. 需求总览

| ID | 功能 | 优先级 | 代码状态 | 集成位置 / 当前主要缺口 |
| --- | --- | --- | --- | --- |
| PRD-F001 | WAV/MP3/M4A 导入与设备资料 | P0 | ✅ implemented | main + alpha.2；Web/CLI 均用统一导入路径 |
| PRD-F002 | 原始与派生 Evidence/provenance | P0 | ✅ implemented | main + release；此状态限定导入资产 |
| PRD-F003 | 标准化与 Audio QA | P0 | ✅ implemented | main + release；准确率不由 QA 保证 |
| PRD-F004 | 全链路可恢复编排 | P0 | 🟡 partial | ImportRun 已贯通 acoustic→聚类/归属→fusion→turns→timeline→metrics；Judge/Findings 仅状态占位 |
| PRD-F005 | 时间戳 ASR 与云服务 | P0 | 🟡 partial | 云 ASR/原生响应/时间戳与调用审计已接通；真实云及录音质量待验收 |
| PRD-F006 | 混音说话人/声源归属 | P0 | 🟡 partial | ASR-native 聚类与声源归属处理器已接入；无角色证据保持 unknown；语义角色 PR #54 未合并 |
| PRD-F007 | Turn / Response 关联 | P0 | 🟡 partial | 有角色证据时可生成 turns/responses；自动语义关联仍未闭环 |
| PRD-F008 | 自动事件与规范 Timeline | P0 | 🟡 partial | 已接入统一账本及身份引用；显式角色路径有契约测试，真实自动事件待验收 |
| PRD-F009 | 确定性指标集成 | P0 | 🟡 partial | 已产出 MetricResult 3.0.0，方向/候选语义修复已入 main；自动语义及真实对照未完成 |
| PRD-F010 | Structured LLM Harness / Judge | P1（MVP 必需） | 🟡 partial | 既有独立 pipeline/Provider 可用；ImportRun 尚不执行 Judge，完整工具循环未闭环 |
| PRD-F011 | Findings 与问题解释 | P1（MVP 必需） | 🟡 partial | 既有候选生成模块保留；ImportRun 尚不执行 Findings，确认工作流未完成 |
| PRD-F012 | 人工修正与有效视图 | P1（MVP 必需） | 🟡 partial | RevisionStore 已有；Web 修订/重算未接通 |
| PRD-F013 | Markdown + JSON 报告 | P1（MVP 必需） | 🟡 partial | 导入状态报告按 AnalysisRevision 保存；完整结论报告尚未接入主导入流程 |
| PRD-F014 | Web 测试与分析工作台 | P0/P1 | 🟡 partial | 转写、聚类、阶段状态、ASR 重试与回放已有；波形、完整 Timeline、人工审核仍缺 |
| PRD-F015 | 统一模型配置管理 | P1 | ✅ implemented | 配置/路由/快照已有，ASR 与 ASR-native 聚类可绑定；Judge 配置不等于导入流程已执行 |
| PRD-F016 | 语音生成/分析适配器实际调用 | P0 ASR（M1）/ P1 TTS（M2） | 🟡 partial | 云 ASR/审计与复用原生响应的聚类已入 main；真实服务标签契约待确认，TTS 未接入 |
| PRD-F017 | 同一录音分析修订与重现 | P1 | 🟡 partial | 同 Run 显式 ASR 重试会生成新 AnalysisRevision；通用重分析/人工修订重算尚缺 |
| PRD-F018 | 版本/设备/供应商 Compare | P2 | ⬜ planned | 无产品比较工作流 |
| PRD-F019 | Frozen Golden Voice 资产 | P1（M2 核心） | ⬜ planned | 暂停草稿不算可交付能力 |
| PRD-F020 | Active Voice Test Controller / Fixed Runner | P1（M2 核心） | 🟡 partial | Runner 基础已有，真实多轮控制未完成 |
| PRD-F021 | Free / Exploratory Voice Test Agent | P1（M3 核心） | ⬜ planned | 无完整探索→确认→回归链路 |
| PRD-F022 | 专业 HIL / 同步校准 / Remote Station | P3 | ⏸ deferred | Station 代码保留，非当前 MVP 门槛 |
| PRD-F023 | 基础本地播放与麦克风观察 | P1（M2 核心） | ⬜ planned | 从 F022 拆出；真实本地音频链路待交付 |
| PRD-F024 | 执行与外部录音分析 Run 关联 | P1（M2 手动 / M4 自动） | ⬜ planned | 双 Run 引用、录音时间映射及自动匹配待实现 |

### MVP 范围与排程的关系

P0/P1 表示开发先后，不表示可选与必选。当前 MVP 专指 M1 录音分析，包含 PRD-F001～F015、PRD-F016 的云 ASR/diarization 与调用审计部分、PRD-F017；各项以第 4 节限定范围和第 7 节验收为准。PRD-F016 的 TTS 部分及 PRD-F018～F024 不阻塞 M1；其中 F016 TTS/F019/F020/F023 与 F024 手动关联为 M2 必需，F021 为 M3 必需，F024 自动关联为 M4 必需。优先级与里程碑共同表达排程：主动测试是 P1 核心能力，但不插队 M1；专业 F022 继续 P3。PRD-F015 的配置管理已实现，不代表 PRD-F016 的语音调用已实现。

同一功能的“代码已实现”“已合入 main”“已发布”“真实录音验收通过”分别记录；本次仅按代码/测试/发布证据刷新实现状态，不升级真实验收状态。

## 4. 核心功能与验收条件

### PRD-F001 — 录音导入与设备资料

**代码：✅ implemented；验证：software_verified、container_verified、browser_verified；位置：main + alpha.2（Web/CLI 统一导入）。**

接收 WAV、MP3、M4A，不依赖 TestCase；支持用户提供的 5～20 分钟完整对话。Device、Hardware Version、Firmware、AI Model、Prompt Version、Supplier、Environment、Notes 可选，未知保持空值。当前实现额外设置 30 分钟 / 1 GiB 上限，这是版本限制，不把真实 20 分钟准确性验收改成短样本验收。

- [x] CLI 与发布版 Web 三种格式可进入导入流程，错误格式明确拒绝。
- [x] 设备资料与 Run 关联，损坏录音仍保留导入 Run。
- [ ] 5～20 分钟真实录音的全链路质量与可用性验收（见第 7 节，不能由本项代码完成替代）。

依据：[import_pipeline.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/import_pipeline.py)、[api.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/api.py)；[test_import.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_import.py)、[test_web_release.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_web_release.py)；Issues #21、#42。

### PRD-F002 — 导入资产与溯源

**代码：✅ implemented（限定导入资产）；验证：software_verified、container_verified；位置：main + alpha.2。**

原始文件和标准化文件分开保存，记录 SHA256、大小、元数据、父资产 ID、转换工具/版本、调用参数及审计输出，不覆盖原件。可沿派生关系找到原始录音；失败诊断与成功结果分开。全部分析结论的统一 Evidence 仍属于 PRD-F004/F008/F011。

- [x] 原始与派生文件存在、Hash 可复核、父引用可追踪。
- [x] 禁止修改已登记的不可变资产；保留转换失败信息。

依据：[import_artifacts.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/import_artifacts.py)、[test_import.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_import.py)；Issue #21。

### PRD-F003 — 标准化与音频 QA

**代码：✅ implemented；验证：software_verified、container_verified；位置：main + alpha.2。**

内部 Canonical Audio 使用 WAV / PCM16 / 16 kHz / mono，保存原始通道与格式。测量 duration、sample count、peak、RMS、DC、clipping 等，不凭空添加合格阈值。转换可能存在 codec delay 等残余不确定性，不宣称原始压缩音频与标准化边界等同于声学真值。

- [x] FFmpeg/FFprobe 版本和参数有记录；Docker 自带工具。
- [x] 三种真实编码格式的合成音频转换通过；空音频/无语音可以明确弃权。

依据：[audio_processing.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/audio_processing.py)、[Dockerfile](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/Dockerfile)、[docker_smoke.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/scripts/docker_smoke.py)；Issue #21。

### PRD-F004 — 分阶段编排与失败隔离

**代码：🟡 partial；验证：software_verified（局部）；位置：main + alpha.2。**

每层 input → versioned processor → output，Run 应包含 OriginalArtifact、NormalizedAudio、AudioMetadata、Transcript、Timeline、Metrics、JudgeResults、Findings、Report。每个阶段可 pending、partial、insufficient_evidence、failed；单阶段失败不能破坏 Run。依赖缺失时明确跳过原因，报告仍尝试输出。

- [x] 导入/标准化/可选 ASR 有阶段账本、输出封装与失败保留。
- [ ] Web 后续 acoustic/fusion/metric/Judge/report 全部接入同一阶段账本、规范资产登记和修订身份。
- [ ] Web/CLI 使用一致的完整分析编排，而非两个局部路径。

已合入范围：Web `/api/analyze` 与 CLI `import` 共用 `import_recording()`；声学、聚类、归属、融合及有合格角色时的轮次/事件/指标均进入账本和资产目录，ASR 重试沿用 Run 并新建修订。Judge/Findings 尚未执行，只发布未运行状态，因此上述完整验收项仍未完成。见 [主链测试](../tests/test_recording_backbone.py) 与 [显式角色端到端测试](../tests/test_explicit_attribution_e2e.py)。

依据：[import_pipeline.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/import_pipeline.py)、[pipeline.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/pipeline.py)；Issues #21、#27。

### PRD-F005 — ASR

**代码：🟡 partial；验证：software_verified；位置：main + alpha.2。**

使用可替换 Provider 输出原始响应和带时间戳 Transcript，标记 ASR estimated timing；保留中文、混合语言及识别缺口。Vosk 为可选离线方案，优先允许成熟云服务，不要求本地模型成为最终默认。

- [x] ASRProvider、Vosk、Transcript 与原生输出验证已有。
- [ ] Web 真实录音自动调用中文云 ASR，并把文本/时间证据接入后续语义和事件分析。
- [x] 云调用资源 ID、版本、原生输出与 invocation 审计已在 Web/CLI 导入路径集成（软件验证）；真实服务调用/质量仍待验收。旧 #31/#32 仅选择性复用，不合入过时异步方案。

云适配器实现见 [volcengine_asr.py](../aivoicebench/volcengine_asr.py)、[asr_audit.py](../aivoicebench/asr_audit.py)、[主链测试](../tests/test_recording_backbone.py)。需要 ASR 路由、凭据及签名音频发布配置；仅填写 Key 不足以调用。

依据：[asr.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/asr.py)、[test_asr.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_asr.py)；Issues #7、#22、#30。

### PRD-F006 — Speaker / Source Attribution

**代码：🟡 partial；验证：software_verified（弃权与结构）；位置：main + alpha.2。**

处理单轨混音的 speech segmentation → speaker clusters → tester/device/unknown。Assignment 含 confidence、source、provider、model、evidence；融合音色参考、diarization、可选语义判断和人工修正，保留冲突。不能只相信一个模型或按轮流出现强制角色。

- [x] 默认未知角色不会生成伪确定的角色时延。
- [ ] 可用 diarization/source Provider 与真实混音角色识别。
- [ ] 人工角色修订作为新 Evidence/Annotation 参与有效结果。

已合入进展：ASR-native 聚类复用同一次识别的原生响应，进入账本与 Web，但标签请求契约和真实可用性未验证。F006 仍为 partial；语义角色归属 PR #54 未合并。证据见 [diarization.py](../aivoicebench/diarization.py)、[ASR 聚类测试](../tests/test_asr_diarization.py)、[融合测试](../tests/test_fusion_speakers.py)。

依据：[fusion.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/fusion.py)、[test_evidence_guards.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_evidence_guards.py)；Issues #22、#24、#26。

### PRD-F007 — Turn / Response 关联

**代码：🟡 partial；验证：software_verified（显式归属 fixture）；位置：main + alpha.2。**

把 Speech Segment → Speaker/Source → Turn → Response → Event 关联。打断后能区分旧回答终止、新输入结束、新 Intent 回答，以及是否又回到旧回答。语义关系不确定时保留候选/待复核，不靠纯时间顺序确认 Intent。

- [x] 显式标注角色的输入可构建基础 turns/responses。
- [ ] 自动角色/语义关联、跨轮纠正和反例覆盖完成。

依据：[fusion.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/fusion.py)、[test_fusion.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_fusion.py)；Issue #24。

### PRD-F008 — Automatic Event Detection / Timeline

**代码：🟡 partial；验证：software_verified；位置：main + alpha.2。**

识别 tester_speech_start/end、device_speech_start/end、silence、overlap、interruption_start/end、response_start/end、timeout、possible_false_endpoint，并映射到版本化规范事件契约。产品事件概念与当前序列化别名可以不同，但映射必须明确，不能静默改义。

边界必须包含 source、confidence、method、evidence、uncertainty，区分 acoustic、ASR estimated、diarization、LLM semantic selection、manual corrected。声学分段不能只依赖 ASR timestamp。Timeout 需要完整观察窗口和策略；录音 EOF 不自动是 timeout；possible_false_endpoint 不是已确认缺陷。

- [x] 能量 VAD 声学片段、时序候选、未知角色弃权。
- [ ] 自动 Timeline 的 Run/Turn/Response/Evidence、音频区间和规范 schema 全部验证通过。
- [ ] 打断结束、timeout 与 false endpoint 语义及事件命名统一，不把候选直接判真。

已合入证据：`_timeline()`/`_metrics()` 绑定实际 Run 身份；[显式归属端到端测试](../tests/test_explicit_attribution_e2e.py) 验证 Timeline、指标及引用。无角色时不产生伪事件。复杂自动关联、真实边界及语义确认仍未通过整体验收。

依据：[acoustic.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/acoustic.py)、[fusion.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/fusion.py)、[validation.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/validation.py)；Issues #2、#23、#24。

### PRD-F009 — 确定性指标

**代码：🟡 partial；验证：software_verified；位置：main + alpha.2。**

复用 TestCase、EventTimeline、MetricResult、公式、验证和旧 deterministic engine。指标需声明事件选择、适用范围、单位、样本/分母、不确定性、缺值和版本化阈值；没有阈值不得自动判通过。具体用户含义见第 5 节，技术公式见 [指标定义](03-metric-definition.md)。

- [x] 旧引擎的证据验证、基本时延/overlap/CER/统计基础存在。
- [x] 导入 metrics.py 直接产出 MetricResult 3.0.0，保留 2.0.0 可读；Turn Gap 方向与负值、旧 response 引用及 False Endpoint 候选语义已修复并有软件验证。
- [ ] 自动角色/语义事件供给、多区间完整性及所有扩展指标真实有效性验收。
- [ ] 真实录音的指标边界与人工标注对照验证。

新增依据：[指标契约测试](../tests/test_metrics_contract.py)、[版本兼容测试](../tests/test_metric_compatibility.py)、[显式归属端到端测试](../tests/test_explicit_attribution_e2e.py)。不能从契约测试通过推导 M001～M010 均能自动测量。

依据：[engine.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/engine.py)、[formulas.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/formulas.py)、[metrics.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/metrics.py)、[test_engine.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_engine.py)；Issues #3、#8、#25。

### PRD-F010 — Structured LLM Harness / Judge

**代码：🟡 partial；验证：software_verified；位置：main + alpha.2。**

Orchestrator → Context → Model → Structured Decision → 允许的 Tool/Deterministic Function → Observation → Model → Final Result。关键调用使用 schema-constrained 输出与引用校验，保留原生响应和验证结果；不能用自由文本猜 JSON，不能发明时间戳、音频 Evidence 或内部原因。

语义维度至少覆盖 Intent、Turn 关联、Meaningful Response、Context、Memory、Instruction Following、Reasoning/Knowledge、Hallucination、Persona、Emotion、Proactivity、Safety、对话质量与结论解释。JudgeResult 包含 decision、score、confidence、reason、evidence_refs、turn_refs、model、prompt_version；Decision 包含 selected_action、confidence、rationale、required_tools、expected_evidence。疑似根因保留 attribution_confidence 与 requires_log_verification。

- [x] 可配置兼容服务、结构字段校验、失败/无证据弃权，不默认 Mock 成功。
- [ ] 有效 Transcript/Turn/时间锚点进入 Context；模型只选择已有证据。
- [ ] 完整 schema-constrained 调用、受限工具循环、统一调用审计与上述维度的真实覆盖。

主路径限制：`ImportRun` 未调用 Judge；`judge-results.json` 为状态封装，配置 judge 路由不会使导入自动执行语义评估。已有 `pipeline` CLI 是独立模块入口，不能作为 Web 主流程已闭环的证据。

依据：[llm.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/llm.py)、[llm_provider.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/llm_provider.py)、[test_llm.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_llm.py)；Issues #10、#30。

### PRD-F011 — Findings

**代码：🟡 partial；验证：software_verified；位置：main + alpha.2。**

Finding 显示 Severity、Confidence、Reason、Evidence、Audio Timestamp、Suspected Layer 与 Human Review。确定性异常与 LLM 候选都须通过证据验证，不从缺少发现推导“设备合格”。严重安全/主观问题需要人工复核。

- [x] Finding/Evidence 基础契约、候选生成和基本呈现存在。
- [ ] 每条结论的完整可解析引用与点击证据音频区间、确认/拒绝工作流。

主路径限制：`ImportRun` 未执行 `generate_findings()`，空列表或 pending 状态不代表没有缺陷。

依据：[findings.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/findings.py)、[test_findings.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_findings.py)；Issues #4、#11。

### PRD-F012 — 人工修订

**代码：🟡 partial；验证：software_verified；位置：main + alpha.2。**

支持 ASR 文本、speaker、事件边界、turn association、finding 确认/拒绝；记录 reviewer、reason、base revision、原值、新值、证据目标。机器原件保留，生成有效视图与新分析修订，形成 Machine → Human Review → Confirmed Evidence → Regression Case。

- [x] RevisionStore 与复制后应用修订的基础实现。
- [ ] 严格修订 schema/引用校验、并发/基线检查、Web 编辑、下游重算和确认闭环。

依据：[revision.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/revision.py)、[test_revision_pipeline.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_revision_pipeline.py)；Issue #26。

### PRD-F013 — 报告

**代码：🟡 partial；验证：software_verified、browser_verified；位置：main + alpha.2。**

生成 Markdown + JSON，呈现状态、缺口、指标、语义结果、Findings 和 Evidence。关键结论可定位音频区间及处理版本。只有明确版本化评分/门禁策略存在时才显示 Release Gate 或综合分，必须显示样本与分母。

- [x] 导入状态报告、分析报告、Web Markdown 下载；部分结果不是成功验收。
- [ ] 完整结论级证据闭环、有效修订报告、兼容规范输出与真实验收。

当前导入调用 `write_import_report()`，输出 `report_kind=import_stage_status`、空 conclusions 与设备表现 insufficient_evidence；Web 展示其他阶段产物不代表已生成完整结论报告。`render_report()` 保留于独立 CLI/pipeline，尚未纳入统一导入主链。

依据：[report.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/report.py)、[import_report.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/import_report.py)、[test_findings_report.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_findings_report.py)；Issues #11、#27。

### PRD-F014 — Web 测试与分析工作台

**代码：🟡 partial；验证：browser_verified、container_verified（局部）；位置：main + alpha.2。**

简洁界面，以 Home/Runs、Import、Analysis、Metrics、Findings、模型管理为当前导航；M2 增加与 Recording Analysis 并列的 Active Voice Test 入口，支持固定 Case、音频设备选择、启动/停止和执行轨迹；M3 增加目标、策略、预算和停止条件配置；M4 展示执行与分析关联；Compare 在 M5 增加。Analysis 应联动 Audio Waveform、Speaker Segments、Transcript、Turn Timeline、Events、Metrics、LLM Findings。不能要求用户读开发实现信息才能正常操作。

- [x] 浅色工作台、历史、导入表单、报告状态、音频控件、片段跳转、指标/发现切换。
- [ ] 波形、完整转写/轮次/事件联动、Finding 区间跳转、人工审核入口及真实长录音体验。

- [ ] M2/M3 主动测试入口可操作，清楚区分执行完成、测量待补充与正式结论；后续范围不计入现有 browser_verified。

依据：[static/](https://github.com/lybym/AIVoiceBench/tree/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/static)、[api.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/api.py)；Issues #27、#42。

### PRD-F015 — 模型配置管理

**代码：✅ implemented（配置管理与路由，不代表各处理器均执行）；验证：software_verified、container_verified、browser_verified；位置：main + alpha.2。**

参考 DeepSeek Harness 的 provider profile、credential reference、用途绑定与配置修订方式，实现本项目独立配置层。支持 provider/model/endpoint、语音及推理参数、启停、tts/asr/diarization/judge 默认模型、只写入密钥或环境变量引用。保存配置不得等同于连通验证，不自动发起付费探测。

- [x] 新增/编辑/移除、用途约束、乐观配置版本、持久化、密钥不回显；可配置兼容 Judge，但 ImportRun 尚未执行该服务。
- [x] 配置于下一 Run 生效；每个 Web Run 保存脱敏快照和 Hash。
- [x] 未接入 speech adapter 明示 not_integrated，不模拟运行；现有环境配置首次保存前保持兼容。

依据：[model_settings.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/model_settings.py)、[models.js](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/static/models.js)、[test_model_settings.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_model_settings.py)；Issue #44。

### PRD-F016 — 语音服务实际调用与主动测试 TTS

**代码：🟡 partial（云 ASR/原生响应聚类与审计已合入，TTS planned）；验证：software_verified、real_recording_pending。**

提供统一 ASRProvider、TTSProvider、AudioProcessingProvider、DiarizationProvider 接入点；核对火山等当前官方 API 后实现真实调用。每次调用记录 provider、model、endpoint/API version、config、prompt_version（适用时）、timestamp、输入/输出 refs、latency、status，不记录 Secret。配置管理不代表调用能力已交付。音频标准化和 Vosk 已有能力分别见 PRD-F003/F005，不在此重复标为未实现。

- [ ] 云 ASR/diarization 原生输出、重试/失败状态、调用审计与 Web 分析集成。

已合入进展：云 ASR、调用审计、Web/CLI 及 ASR-native DiarizationProvider 已具备，聚类复用原生响应而不追加识别。服务是否需显式开启分离、原生标签语义及真实可用性仍待验证；该整体验收项保持未勾选。见 [云适配器](../aivoicebench/volcengine_asr.py)、[聚类处理器](../aivoicebench/diarization.py)、[主链测试](../tests/test_recording_backbone.py)。
- [ ] TTS 真实调用服务于主动测试（P1/M2）：固定 Runner 执行前生成并冻结音频，运行时重用资产；M3 自由 Agent 可逐轮生成，保存每轮实际播放音频及引用。效果与成本按服务实际支持能力选择。
- [ ] TTS 失败、预算耗尽或音频无效时明确停止/失败，不标记为已播放；调用记录关联 Execution Run/Turn 和音频资产。

依据：[model_settings.py 的路由与适配器实现](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/model_settings.py)；Issues #22、#30、#9；旧 #31/#32 的调用审计/发布校验已选择性复用，未整体合入。

### PRD-F017 — 重复分析与修订

**代码：🟡 partial；验证：software_verified（Hash/模型快照）；位置：main + alpha.2。**

同一录音可生成独立 AnalysisRevision，保留 Run、原始 Hash、输入引用、处理器/模型/配置版本、调用记录和人工修订 ID。确定性环节应可重现；云模型差异应可解释而非承诺位级一致。

- [x] 文件 Hash、独立导入记录、模型配置快照等基础存在。
- [ ] 同 Run 多次分析入口、输出不覆盖、修订对比和可解释差异报告。

已合入：`POST /api/runs/{run_id}/resume` 显式重试 ASR，校验 canonical 资产、保留旧输出/快照、使用新配置创建 AnalysisRevision，并执行后续证据链。已完成 ASR/report 时不重复调用。此入口不支持对已完成结果任意重分析，也不等于人工修订后重算。见 [恢复实现](../aivoicebench/import_pipeline.py) 与 [恢复测试](../tests/test_recording_backbone.py)。

依据：[import_artifacts.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/import_artifacts.py)、[revision.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/revision.py)；Issues #26、#27。

### PRD-F018 — Compare

**代码：⬜ planned；优先级：P2。** 比较版本 A/B、设备 A/B、供应商 A/B；声明 case/audio/metric/model policy 的可比范围、样本、分母、环境与不兼容项。验收需可比群体选择、差异指标、证据回溯和不兼容拒绝，不能仅并排展示两个分数。依据：发布版 [api.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/api.py) 无 Compare 工作流；Issue #27 的后续范围。

### PRD-F019 — Frozen Golden Voice

**代码：⬜ planned；优先级：P1（M2 核心）。** 固定 Case 可复现执行的基础。TTS 生成后冻结，保存 text/provider/model/voice/speed/pitch/volume/sample_rate/format/生成时间/hash/响应元数据；回归重用 Frozen Audio，不实时重合成。Golden 表示冻结刺激资产，不代表设备回答的事实真值。

- [ ] 生成、QA、版本和缓存可追踪；Case 锁定音频 Hash/版本，参数变化生成新版本，不覆盖原件。
- [ ] 精确停顿采用 TTS(A) + sample-exact silence + TTS(B)，保存片段和静音 sample count；800 ms 是 Case 示例，非统一门槛。文件中精确停顿不代表真实声场没有误差。
- [ ] 同一 Case 重复执行重用相同资产；资产缺失、损坏或 Hash 不符时拒绝执行并保留原因。

依据：Issue #9 暂停草稿未纳入发布代码；既有 TestCase/音频资产契约可复用。本次升级优先级，不升级实现状态。

### PRD-F020 — Active Voice Test Controller / Fixed Case Runner

**代码：🟡 partial；优先级：P1（M2 核心）；验证：software_verified（仅 Runner 基础）。** Controller 是主动测试执行核心。Fixed Case Runner 冻结 TestCase、音频、停顿、触发条件和超时策略，由确定性状态机执行，不依赖 LLM 决策。复现指相同刺激和控制策略，设备回答与实际触发时间允许不同且必须留痕。

- [ ] 验证 Case/Frozen Audio，创建 Execution Run，保存 Case/策略版本、设备资料、配置快照和音频 Hash；提示资产与打断资产分离。
- [ ] 支持多轮播放、等待设备响应开始/结束、条件触发、明确 deadline、取消与失败处理；记录状态迁移、计划动作、实际播放开始/结束/取消、Observation、触发依据及 Run/Turn 引用。
- [ ] 句中停顿用例可播放“我想问一下”+ 800 ms silence +“南京明天天气怎么样”，等待回答结束后问“北京呢”；文本、停顿和等待策略均冻结。
- [ ] Barge-in 用例先请求长故事，检测设备持续发声后等待 Case 指定时长（如 2 秒）再播放“停，换个问题”；记录实际触发及旧/新回答候选，不能用预拼接时间代替实时触发。未观察到响应时按策略 timeout/停止，不伪造打断成功。
- [ ] 相同 Case 与相同观察序列重放时控制决策一致；真实设备测试核对播放/触发轨迹。设备回答结束的控制判断与正式 Barge-in/时延/语义判定分开。
- [ ] 监听中断、低置信度、自身播放误识别或设备断开时按冻结策略等待/停止并记录原因，不无限等待或无记录追加重试；用户可随时停止。

依据：[runner.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/runner.py)、[test_runner.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_runner.py)；Issue #5。现有 Run 准备不等于真实多轮执行已交付。

### PRD-F021 — Free / Exploratory Voice Test Agent

**代码：⬜ planned；优先级：P1（M3 核心）。** 面向测试目标的 Voice Test Harness / Test Agent。用户定义 Goal、Test Strategy、Coverage、Budget、Stop Condition 和禁止行为；LLM Planner 根据实时 Observation 决定下一步话术或动作，经 Harness 校验后调用 TTS/播放/观察工具。与 Fixed Runner 分离，复用 F020/F023 执行基础；不同于事后 F010 Judge。

- [ ] 保存版本化目标、策略、允许 Tool、轮次/时长/调用成本预算和停止条件；动作经过结构校验及预算检查，越界动作拒绝执行。预算可配置，15 轮是示例而非默认 SLA。
- [ ] 支持“建立临时事实 → 间隔若干轮 → 重问 → 切换话题 → 恢复原话题”等策略，遵守“不告知正在测试、不直接提示正确答案”等用户约束。
- [ ] Mic → VAD → 实时 ASR → Observation → LLM Decision → Action → TTS/Playback 循环保留 Trace：模型/提示版本、观察引用、决策理由、工具调用、实际音频、覆盖进展、消耗与停止原因。
- [ ] Coverage 区分计划、已尝试、已观察及待正式测量；ASR 不确定、模型/工具失败、预算耗尽或用户停止均有明确处置，不能把控制观察或 Agent 自评当作正式通过。
- [ ] 设备回复作为被测数据，不能改写 Harness 的目标、工具权限、预算和禁止行为。
- [ ] M3 保存候选问题与完整轨迹供离线分析/复核；M5 基于 Measurement Evidence 和人工确认最小化用例，冻结音频/策略后交给固定 Runner 复测。探索输出不能直接成为 Golden ground truth。

依据：既有路线图探索目标与本次用户补充；发布基线无完整实现。

### PRD-F022 — 专业 HIL / Station

**代码：⏸ deferred；优先级：P3；保留已有局部实现。** 保留专业声卡播放/录制、同步/校准、loopback、SPL 校准、physical HIL 与 Remote Station 扩展。普通电脑播放与麦克风观察已拆为 PRD-F023，不随本项延期。验收须明确真实硬件、同步不确定性与健康观察窗口，不以软件 fixture 冒充硬件测试。

依据：[station.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/station.py)、[test_station.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_station.py)；Issue #6。保留原编号及拆分追溯关系。

### PRD-F023 — 基础本地播放与麦克风观察

**代码：⬜ planned（可交付基础音频链路）；优先级：P1（M2 核心）。** 普通 Windows 电脑扬声器播放测试语音，麦克风/VAD 提供响应观察；M3 接入实时 ASR 供 Agent 理解回答。复用 Station 接口，不依赖专业 HIL/SPL 校准或 Remote Station 才可用。

- [ ] Web 可选择输入/输出设备、检查可用性、启动/停止；权限拒绝、无设备和音频中断明确显示，失败不生成成功播放记录。
- [ ] 支持边播放边监听以执行 Barge-in；记录自身播放泄漏、噪声与设备响应归属的不确定性，无法可靠区分时按策略弃权/停止。
- [ ] 保存实际播放资产、设备/采样配置、观测时间基准及丢帧/中断信息；平台麦克风输入不自动成为正式测量录音。
- [ ] 真实扬声器、麦克风、实体 AI 设备验证多轮等待和条件打断，由另一台设备全程录音核对；合成测试不替代真实本地链路验收。

依据：本次用户要求从 F022 拆出；既有 Station 软件基础不构成本项已交付证据。

### PRD-F024 — Execution Run 与外部录音 Analysis Run 关联

**代码：⬜ planned；优先级：P1（M2 手动关联，M4 自动关联）。** Execution Run 保存刺激、动作和控制观察；外部录音导入形成独立 Analysis Run，AnalysisRevision 不覆盖执行原件。两者引用关联；独立导入仍不需要执行记录。

- [ ] M2 支持手动将外部录音导入并关联 Execution Run，记录身份、原件 Hash、Case/Turn 引用及关联来源；未关联显示 measurement_pending。
- [ ] M4 自动提出匹配并建立可追溯关联：保存匹配依据、置信度、执行片段到录音区间映射、时钟偏移/漂移及不确定性。歧义、录音不完整或无可靠映射时转人工确认，不静默绑定。
- [ ] 更正关联生成新修订并保留历史；重复导入/重试不产生冲突关联；分段录音或一份录音包含多个执行时明确区间范围。
- [ ] 报告从正式 Finding/Metric 追溯外部音频区间，再关联到 Case/执行动作；指标在外部录音时间轴上计算，不把播放命令或跨设备时钟当声学真值。

依据：本次用户明确提出双证据链和 M4 自动关联；新增编号不表示已有实现。

## 5. 用户体感指标要求（PRD-F009 的子需求）

下表定义产品含义；技术单位、公式版本、事件别名和验证边界由 [metric-definition](03-metric-definition.md) 维护，不得反向修改本表以迎合当前错误实现。

| ID | 指标与验收含义 | 代码状态 / 明确差距 |
| --- | --- | --- |
| PRD-M001 | Feedback Latency：用户最终结束 → 首次可感知反馈；记录 feedback_type（嗯/好的/thinking cue/提示音等） | 🟡 partial：当前返回 insufficient_evidence；反馈起点及逐 turn 关联未接通，反馈自身时长不是反馈时延 |
| PRD-M002 | First Speech Latency：同一关联轮次用户最终结束 → 首个设备语音 onset；旧 E2E First Audio 名称的兼容语义见下文 | 🟡 partial：导入路径已按 tester end→同轮 device onset 计算；提前发声为 not_applicable；真实自动角色与边界待验证 |
| PRD-M003 | Meaningful Response Latency：用户结束 → 首个承载答案语义的信息点；ASR+LLM 选择既有锚点并记录证据/confidence | 🟡 partial：已有契约/占位，真实语义锚点未实现；不得将“嗯/让我看看”自动等同答案 |
| PRD-M004 | Turn Gap：用户说完 → 设备有效开始下一轮；明确有效起点和负 gap/overlap 处理 | 🟡 partial：main 已使用 tester_speech_end→device_speech_start 并保留负值；MetricResult 3.0.0 有软件验证，真实对照待完成 |
| PRD-M005 | Barge-in Stop Latency：测试者打断开始 → AI 旧 response 停止；必须关联旧 response_id | 🟡 partial：导入计算关联 interrupted_response_id 并查找旧回答结束；复杂自动关联和真实验证仍缺 |
| PRD-M006 | Barge-in New Intent Latency：新的打断语句结束 → 开始回答新 Intent | ⬜ planned：尚无有效自动语义关联链路，不能用任意下一次设备发声代替 |
| PRD-M007 | Barge-in Success：停止旧回答 + 接收新输入 + 回答新 Intent + 不再回到旧回答 | 🟡 partial：停止/新回答基础契约存在，完整组合语义未闭环；缺一项证据均不得自动通过 |
| PRD-M008 | False Endpoint：用户句中停顿时设备错误抢答；结合意图继续证据，possible_false_endpoint 保持候选 | 🟡 partial：main 仅输出 false_endpoint_candidate（policy=candidate_only）；observed 只表示候选存在，不是确认缺陷 |
| PRD-M009 | Overlap Duration / Ratio：区间并集交集及明确分母；后续可区分正常 backchannel/主动打断/意外重叠 | 🟡 partial：算术基础已有，混音双声源与多区间/分母一致性待闭环 |
| PRD-M010 | Timeout、CER/WER、统计：健康窗口+显式 deadline；正确参考文本来源；eligible 样本与分母、P50/P90/P95/P99 | 🟡 partial：旧 engine/formulas 保留 timeout/CER/WER/统计基础；compute_timeline_metrics 尚不自动产出 M010，缺参考/健康窗口不能计算 |

依据集中为 [formulas.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/formulas.py)、[engine.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/engine.py)、[metrics.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/metrics.py)、[test_metrics_expanded.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/tests/test_metrics_expanded.py)。主要任务为 #8、#25，语义依赖 #10，事件依赖 #24。这些问题在本次仅文档 PR 中登记，不偷改业务代码。

### 指标边界与判定策略

- **Feedback / First Speech / Meaningful Response 分别记录。** 提示音可以是反馈但不是语音；“嗯”可以同时是反馈和首次语音，但不自动成为有效答案。PRD-M002 保留旧 E2E First Audio 指标按设备语音 onset 计算的兼容语义，不静默改成“任意声音”。输出注明 metric ID、公式版本和所选事件，历史值不能混用新口径。
- **Turn Gap 明示有效起点。** PRD-M004 的方向固定为 tester end → device start；策略须说明选择语音起点还是经语义确认的有效回答起点，并引用相应事件。起点未明确时不计算；不同策略不直接聚合比较。可观察的负间隔保留为负值并关联 overlap，不能截成零掩盖抢话；旧 device end → tester start 结果不能沿用本指标名称。
- **打断成功有观察范围。** PRD-M005 的停止时长与 PRD-M007 的成功判定分开。成功需分别给出旧回答停止、新输入被接收、新 Intent 被回答、观察范围内未恢复旧回答的证据；停止期限和后续观察窗口属于版本化策略。外部录音只能证明可观察行为，不能声称已读取设备内部接收状态。窗口不完整或任一必要证据缺失时不得通过；EOF 不能证明“以后不再回到旧回答”。
- **区间与统计不可伪精确。** 每个时延保留两个边界的来源、不确定性和关联身份；阈值落在不确定区间内时需复核。Overlap 对重复区间先去重，明确分母是有效观察时长还是其他声明范围；unknown 声源不能直接计为 tester/device 重叠。汇总同时报告总样本、可计算样本、弃权/失败及排除原因，避免只展示成功样本。

上述约束是验收口径，尚未全部实现。具体停止期限、观察窗口、覆盖率和允许误差未由项目所有者确定，不在此虚构默认数值；评测设备体验的阈值与验证 AIVoiceBench 测量准确性的容差分别保存。

## 6. 非功能需求

| ID | 要求与验收 | 代码状态 / 验证 / 依据 |
| --- | --- | --- |
| PRD-N001 | Docker 后端+前端、Windows 浏览器；版本号与 Release 一致；有启动配置与持久卷；不要求 EXE | ✅ implemented（容器交付基础）；container_verified/browser_verified；[release.yml](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/.github/workflows/release.yml)、[Dockerfile](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/Dockerfile)。长期运行/恢复与真实全流程仍由第 7 节验收 |
| PRD-N002 | 所有结论的证据与不确定性可验证；规范 schema 和 runtime refs/hash 同时验证 | 🟡 partial；旧 contracts/validation/engine 已有，导入自动输出尚未全部遵从；[validation.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/validation.py) |
| PRD-N003 | 调用/配置/处理器版本可追踪，原件不覆盖，重复运行可解释 | 🟡 partial；import/模型快照已实现，ASR invocation 与重试 AnalysisRevision 已有，Judge 审计与通用重分析尚缺；PRD-F004/F016/F017 |
| PRD-N004 | Secret 不进 Git、API 响应、分析快照；私有录音/个人报告不提交；配置存储持久且限制访问 | ✅ implemented（现有单用户本地管理范围）；software_verified；[.gitignore](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/.gitignore)、[model_settings.py](https://github.com/lybym/AIVoiceBench/blob/c612d36a61a5cbc90f629677b28d64228316f1d0/aivoicebench/model_settings.py)。本地密钥数据库为明文，不宣称加密或多租户授权；远程暴露须认证代理 |
| PRD-N005 | 5～20 分钟录音可用、依赖错误可诊断、重启恢复不丢历史/配置/原件；资源限制明确 | 🟡 partial；上传上限和持久卷已有，长录音、恢复/中断全流程待验收；不捏造处理时长或准确率 SLA |
| PRD-N006 | 重要模块有针对性测试；合成/真实分开；每逻辑单元 Issue/branch/PR/work log；不得自动 merge | ✅ implemented（治理与测试基础）；[tests](https://github.com/lybym/AIVoiceBench/tree/c612d36a61a5cbc90f629677b28d64228316f1d0/tests)、[AGENTS.md](../AGENTS.md)、[work log](06-work-log.md)。AGENTS/PRD 规则已在 main；每次变更仍须执行，不因历史遵守而永久豁免 |

## 7. M1 录音分析 MVP 验收门槛（当前未通过）

使用项目所有者提供或明确授权的 **5～20 分钟真实测试者+设备混音录音**，保留本地证据，不提交录音/个人报告到 Git。以下全体通过后，才可将当前 MVP 标为完成：

- [ ] 三种格式可导入，原件/标准化/Hash/元数据/转换 provenance 可复核。
- [ ] 时间戳 ASR、tester/device/unknown 自动分段与角色证据可检查，不依赖手工造 Timeline。
- [ ] 自动 Turn/Response/Event Timeline 能表达自然对话及打断场景。
- [ ] 核心时延、抢话、重叠、打断按合格证据计算；缺证据正确弃权。
- [ ] 结构化 LLM 对 Intent/Context 等执行有效语义判断，结论不越过可观察边界。
- [ ] Findings、Markdown 与 JSON 的关键结论可点击/解析到对应音频区间和处理版本。
- [ ] 人工文本/角色/边界/关联/发现修订可保存，不覆盖机器原件，能生成新有效分析。
- [ ] 同一录音重复分析可按 Run/AnalysisRevision 追踪差异及模型/配置变化。
- [ ] Windows 浏览器中的上传→分析→历史→回放→报告→修订全流程可用；Docker 重启后历史/配置/证据仍可访问。
- [ ] 明确记录实际设备、录音、服务、人工标注、失败案例、版本与验收结论；不得用合成 fixture 或“有报告文件”代替。

### 验收执行与证据记录

先保存机器自动分析，再做人工标注/修订；分别评估自动结果与人工修订后的结果，不能用修订后准确性替代自动能力。三格式导入可用同一授权录音的派生编码验证，但应记录转换关系，不能计为三次独立真实对话。

| 验收范围 | 必须记录的证据与判定边界 |
| --- | --- |
| 场景覆盖 | 记录自然问答、反馈/填充语、打断换 Intent、重叠、句中停顿及未响应场景是否实际出现。录音没有出现的场景标为未覆盖；需要补充授权真实片段或继续待验收，不能从“未发现”推导检测通过 |
| 自动识别与时间 | 对照人工复核的文字、角色、事件边界和 Turn/Response 关系，记录识别错误、边界偏差、漏检/误检及人工标注自身不确定性；可计算覆盖率与准确性分开呈现 |
| 语义与 Findings | 对照音频/转写检查 Intent、Context 与有效回答证据，记录候选误报、漏报、弃权及人工确认/拒绝；未被对话触发的语义维度不虚构评分 |
| 弃权与失败 | 证据不足时弃权是正确行为，但全部 unknown、全部 insufficient_evidence、空 Findings 或仅报告生成成功都不能证明 MVP 完成。故障隔离测试通过也不能代替正常录音自动分析验收 |
| 重复分析与恢复 | 使用同一原件记录不同 AnalysisRevision，核对确定性结果与配置/模型变化；重启后检查历史、配置、原件和派生证据可访问，机器原始结果没有被修订覆盖 |
| 验收结论 | 保存需求 ID、Run/修订、音频区间、人工参考、处理器/模型/策略版本、实测值和通过/不通过/待验收理由；缺必要证据不得勾选通过 |

**定量验收策略待确定：** 在正式质量验收前，由项目所有者确认并版本化场景覆盖、最低可计算覆盖率、角色/事件识别质量、边界允许误差及语义复核准则。当前不填写未经确认的数值；缺少对应策略时可以完成实现和采集实测数据，但不能宣称整体质量验收通过。不能看过结果后静默降低门槛。这里验证的是所选真实录音及已覆盖场景，不据单次录音宣称所有设备/供应商普遍适用。

M1 未完成的主要阻塞是 PRD-F004～F013/F017 的自动证据闭环，不是 Windows 打包、硬件播放录制或 TTS 样式。P1 标记代表排程层次，不代表这些验收项可以从 MVP 删除。

### main 的 M1 实现状态（M1.1～M1.5 工程子阶段）

M1.1～M1.5 仅为产品 M1 内部工程拆分；正式产品里程碑仍按第 8 节 M1～M5。预览发布门槛与第 7 节完整质量验收分开，不能用“小范围可用”替代整体 M1 完成。

| 子阶段 | main / alpha.2 已有范围 | 尚缺 |
| --- | --- | --- |
| M1.1 Recording Backbone | Web/CLI 统一 ImportRun；三格式导入、ASR/原生响应/调用审计、修订与显式重试；合成容器恢复已验证 | 真实云服务、真实录音与质量验收 |
| M1.2 聚类与源归属 | ASR-native 聚类、独立 attribution/fusion；音频 Hash 隔离标签，跨聚类拆段与冲突弃权，原生时间/文本来源保留 | 真实标签契约/可用性；自动 tester/device 归属（PR #54 未合并） |
| M1.3 Turn / Event / Metrics | 有角色证据时进入轮次/事件/MetricResult 3.0.0，显式归属 fixture 验证通过 | 自然混音自动关联、语义锚点、完整扩展指标和真实对照 |
| M1.4 LLM Judge / Findings | 独立模块与旧 pipeline 保留；主导入流程发布未执行状态 | 接入 ImportRun、受约束工具循环、结论级证据闭环 |
| M1.5 修订 / 重分析 / Web 验收 | 转写/聚类/回放/历史/阶段状态/ASR 重试、修订内状态报告 | 人工 Web 修订、通用重分析、波形/完整 Timeline、真实全流程验收 |

独立导入不需要角色映射。当前普通 Web/CLI 没有角色编辑入口；内部显式映射测试只证明具备该证据时的下游路径，不代表用户已可自动得到角色。缺角色时 turns/timeline/metrics 明确弃权，原件/聚类与 Run 保留。即使配置 Judge，当前导入也不执行语义角色或事后 Judge。

实现与验证依据见 [Recording Backbone](23-recording-backbone.md)、[聚类与融合](18-fusion-turns-events.md)、[指标实现](19-expanded-metrics.md)、[CI](https://github.com/lybym/AIVoiceBench/actions/runs/34511317063)。发布版本为 [v0.2.0-alpha.2](releases/0.2.0-alpha.2.md)，不是待发布的 alpha.1；真实验收项仍全部保留。

## 8. 优先级与变更流程

### 正式产品里程碑

以下为目标排程，不是已交付声明。**先完成 M1，Fixed Voice Test Runner 是紧接 M1 的下一个主要里程碑。** M2/M3 都是核心交付，专业 HIL 保持 P3 独立扩展。

| 里程碑 | 范围 / PRD refs | 阶段完成门槛 |
| --- | --- | --- |
| M1 录音导入分析主链 | F001～F015、F016 ASR/diarization、F017 | 完成第 7 节真实录音验收；继续收尾，不因主动测试调整插入硬件前置依赖 |
| M2 Fixed Voice Test Runner | F016 TTS、F019、F020、F023、F014 主动测试入口、F024 手动关联 | 冻结 Case/音频；真实扬声器/麦克风/AI 设备完成多轮等待、句中停顿和条件 Barge-in；外部录音手动关联并进入 M1 分析，控制/测量分开；停止与故障轨迹完整 |
| M3 Free Voice Test Agent | F021、F016/F023 实时语音、F014 Agent 配置 | 真实设备上按目标执行自适应对话，覆盖/预算/约束/停止可验证，Observation/Action/音频 Trace 完整；外部录音独立分析，不以 Agent 自评通过 |
| M4 执行与分析自动关联 | F024 自动关联、F004/F017 修订溯源 | 自动匹配可复核，歧义可转人工；错配、缺段、时钟偏差有记录，正式结论可追溯执行且不混用时间轴 |
| M5 Compare / Regression / 自动探索缺陷 | F018、F021 回归转换、F019/F020 重放、F011/F012 复核 | 经外部录音分析和人工确认的问题可最小化成固定 Case；下一版本自动重测，同一冻结刺激下输出可比结果/差异证据，不兼容条件拒绝直接比较 |

M2/M3 的控制误差、播放时序偏差和触发可靠性须记录实测结果，并在正式验收前确认版本化容差，不虚构数值。各阶段分别记录软件验证、真实设备执行与正式测量验收，不互相替代。

M1 近期顺序（产品 M1 内部工程子阶段 M1.1～M1.5，非产品里程碑）：在已合并 #43/#45 的基线上，M1.1 统一 PRD-F004 编排/审计与 PRD-F005/F016 ASR → M1.2 PRD-F006 说话人聚类与源归属 → M1.3 PRD-F007/F008/F009 事件关联、Timeline 与 PRD-M001～M010 → M1.4 PRD-F010/F011 语义与发现 → M1.5 PRD-F012/F013/F014/F017 审核/修订/报告/Web → 第 7 节真实验收。相互独立的结构/基础工作可以并行，不能再次形成冗长串行 PR 链。完成 M1 后按第 8 节正式进入 M2 Fixed Voice Test Runner。

2026-09-07 Import First 调整继续约束 M1 执行顺序；将 Controller/TTS/普通本地音频整体长期后置的范围由本次用户明确要求与 M2/M3 排程取代。现有 #5/#9/#6 沿用；历史 Issue 标题/标签不覆盖新的优先级。本次按用户要求同步 docs 中的实现说明与路线图；根目录 AGENTS 的旧 Station/P3 表述未改，产品排程以本 PRD 的 F022/F023 拆分为准。

### 变更流程

1. 修改产品行为先定位 Requirement ID。Issue 写 `PRD refs`、本次范围、验收与依赖；现有 Issue 编号沿用，不按状态机械新建重复任务。
2. 产品范围、优先级、行为或验收变化在同一 PR 修改本文，并在下表记录原因和用户决定。明确新增用户要求后可执行，不因历史文档冲突再索取已给出的授权；未被授权的产品取舍不得静默猜测。
3. 实现状态从 planned → partial → implemented 必须提供代码入口、针对性测试和所在 commit/PR/tag；真实验收状态须另有实际证据。仅添加测试、文档或关闭 Issue 不足以升级状态。
4. 架构定义实现约束，schemas/技术指标定义契约，Issue 定义当前工作单元，work log 只记录事实。若它们与 PRD 冲突，指出并修正，不用旧日志覆盖当前需求。
5. 文档、代码和需求同库版本化；发布记录准确 commit/tag。PRD 变更保留 Requirement ID，合并仅在用户明确授权时执行。

| PRD 版本 | 日期 | 变更 | 来源 |
| --- | --- | --- | --- |
| 1.1.3 | 2026-09-11 | 按 main c612d36 / alpha.2 刷新实现、集成、发布和 CI 证据；修正 ASR/聚类/指标/重试与 Judge/Findings 边界，保留真实验收门槛 | 项目所有者要求按最新代码更新 docs |
| 1.1.2 | 2026-09-11 | 记录 v0.2.0-alpha.1 预览发布：M1 当前能力的可下载预览版，范围冻结为录音导入/资产与 provenance/阶段账本/配置管理/云 ASR 调用路径/时间戳转写/回放/说话人标签展示/历史/报告/失败与持久化；明确云 ASR 完整前置条件（API Key + 三个签名 URL）与已知限制（接口约定待确认、无角色证据时保持 unknown）；第 7 节完整 M1 验收仍未通过、未降级 | 项目所有者要求收敛现有功能并交付可实际操作的预览版 |
| 1.1.1 | 2026-09-11 | 消除第 7 节与第 8 节的里程碑编号冲突：第 7 节工程拆分改标为产品 M1 内部子阶段 M1.1～M1.5，正式排程仍以第 8 节 M1～M5 为准；新增第 1 节“需求 / 实现建议 / 实现状态”区分规则；按当前分支代码、测试与提交刷新 F006/F009/F016、M004/M008、Issue #3/#25 的状态描述并标注“本分支进展（未合并）” | 项目所有者要求先校准 PRD 基线、消除里程碑命名冲突、区分需求与实现建议与实现状态 |
| 1.1.0 | 2026-09-10 | 确立主动测试/录音分析双主流程与双证据链；F019/F020/F021 升 P1 核心，F022 拆出 F023，新增 F024；明确 M1→M5 与阶段验收，保留 M1 收尾顺序和实现状态 | 项目所有者要求按主动测试定位修订，且只修改 PRD |
| 1.0.2 | 2026-09-10 | 更新 main 基线至 3699587（PR #43/#45 已合并）；F014/F015 状态更新为 main + release；新增附录 A Issue→PRD 交叉引用 | 项目所有者要求检查未实现 Issue 并更新 PRD |
| 1.0.1 | 2026-09-10 | 审阅第 3/5/7 节：明确 MVP 与排程、指标边界、场景覆盖及弃权验收 | 项目所有者审阅要求；未代替所有者批准数值门槛 |
| 1.0.0 | 2026-09-10 | 从分散文档归集；导入优先、Docker/Web、模型管理、代码/发布/验收三者区分；未添加新的准确率/SLA 要求 | 项目所有者导入优先指令、2026-09-09 Docker/Web 与模型管理要求、2026-09-10 中央 PRD 要求 |

归集映射与旧快照见 [产品文档索引](product/README.md)。用户审阅优先看第 3 节范围、第 5 节指标、第 7 节验收，再按编号修改第 4 节细节。

## 附录 A — Issue → PRD 交叉引用

2026-09-11 核对仍有 20 个 Open Issue；以下更新代码缺口，Issue 保持打开不代表尚无实现。主动测试排程以第 8 节为准。Issue 编号沿用不变；不按状态机械新建重复任务。

### P0 Issues（当前 MVP 阻塞项）

| Issue | 标题 | PRD 编号 | 关键未实现缺口 |
| --- | --- | --- | --- |
| #1 | Validate TestCase schema | 基础契约 | 契约验证已实现；Issue 未关闭因等待真实录音验收 |
| #2 | Event Timeline schema | PRD-F008 | 显式角色路径有 Timeline/schema 验证；自然混音自动事件仍待验收 |
| #3 | MetricResult schema | PRD-F009 | main 已统一导入 MetricResult 3.0.0 并保留 2.0.0 兼容；不是尚待集成的分支 |
| #4 | Finding/Evidence schema | PRD-F011, F002 | 完整证据链/人工确认未闭环 |
| #7 | Timestamped ASR + cloud | PRD-F005 | Web/CLI 云 ASR 已接通；真实调用及准确性待验收 |
| #8 | Deterministic engine + event pipeline | PRD-F008, F009 | 事件/指标已进入 ImportRun；完整自动语义供给与真实验证仍缺 |
| #20 | Recording import as primary | PRD-S2 Primary Workflow | 方向已确定；集成基线已建立 |
| #21 | Import WAV/MP3/M4A | PRD-F001, F002, F003 | 真实 5-20 分钟录音验收未完成 |
| #22 | Provider invocation + cloud ASR | PRD-F005, F016 | 云 ASR 原生响应、聚类派生与调用审计已接入；真实服务契约/质量待验证 |
| #23 | Acoustic segmentation | PRD-F008 | 声学片段已进入统一账本；实际边界误差与场景覆盖待验收 |
| #24 | Fuse speaker + turns/events | PRD-F006, F007, F008 | 自动角色/语义关联、打断事件命名统一未完成 |
| #25 | Expand metrics | PRD-F009, M001-M010 | main 已修复方向/候选语义并采用 3.0.0；M010 自动产出与语义指标闭环仍缺 |
| #27 | Integration + Docker/Web UI | PRD-F004, F013, F014, S7 | 后续证据链已接入，Judge/Findings 尚未执行；完整报告/修订与真实验收未通过 |
| #30 | Provider invocation audit | PRD-F016 | ASR/音频发布调用审计已集成；Judge 等全流程审计仍待接通 |

### P1 Issues（MVP 必需，排程在后）

| Issue | 标题 | PRD 编号 | 关键未实现缺口 |
| --- | --- | --- | --- |
| #10 | LLM Harness / Judge | PRD-F010 | 完整 schema-constrained 调用、工具循环、语义维度真实覆盖未闭环 |
| #11 | Evidence-linked reports | PRD-F011, F013 | 完整结论级回溯、点击证据音频区间未完成 |
| #26 | Human annotations + reanalysis | PRD-F012, F017 | 严格修订 schema/引用校验、Web 编辑、重算闭环未完成 |

### 主动测试与专业扩展 Issues（非 M1 阻塞）

| Issue | 标题 | PRD 编号 | 状态 |
| --- | --- | --- | --- |
| #5 | Scripted Case runner | PRD-F020 | P1/M2 核心；partial：真实多轮控制未完成；历史 P2 标题待实现任务同步 |
| #6 | Audio Station / HIL | PRD-F022, F023 | 专业 F022 继续 P3 deferred；基础 F023 为 P1/M2 planned，分别排程 |
| #9 | Frozen TTS Golden asset | PRD-F016, F019 | P1/M2 核心；planned：旧暂停草稿不算已交付 |

F021（M3）与 F024（M2/M4）的实现 Issue 在对应阶段启动时建立有界工作单元；本次仅定义需求，不虚构已创建的任务或实现。

### 已关闭 Issue（需求已归入 PRD）

| Issue | 标题 | PRD 编号 | 说明 |
| --- | --- | --- | --- |
| #40 | Restore evidence-safe baseline | PRD-N001, S7 | Docker/browser 交付基线已恢复 |
| #42 | Fix Web release metadata + UI | PRD-F014 | Web 界面重构已合并入 main |
| #44 | Model management | PRD-F015 | 模型配置管理已合并入 main |
| #46 | Centralize PRD | PRD 全文 | PRD 已创建并持续维护 |

### 审计结论

**1.0.2 审计时的 20 个 Open Issue 已有编号映射；这不代表产品定位完整。1.1.0 补齐主动测试一级工作流、双证据链和正式里程碑。** Issue 中的细节要求（如 #25 的负 gap 不截零、#24 的不按先后强制角色、#26 的严格引用校验）已在 PRD 第 4/5 节对应条目的验收条件中体现。

当前 M1 的主要剩余工作：

1. F006/F007/F008：真实聚类契约验证、自动角色与语义关联；评审 PR #54 时不把分支进展提前算作 main。
2. F009/M001～M010：接入可靠语义/事件证据、补齐尚未自动产出的指标，进行真实标注对照；不要重复修复已合入的 Turn Gap 和 3.0.0 契约。
3. F004/F010/F011/F013：把 Judge、Findings 与完整结论报告接入统一导入账本，补齐语义引用及失败状态。
4. F012/F014/F017：人工审核/修订、通用重分析、可解释差异与波形/Timeline 联动。
5. 第 7 节：授权真实录音、有效服务调用、人工标注及 Windows 浏览器完整验收；预览版和合成 CI 不能替代。
