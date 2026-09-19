# Recording Analysis 产品需求

本文承载 PRD-F001–F019。Requirement ID 覆盖：PRD-F001、PRD-F002、PRD-F003、PRD-F004、PRD-F005、PRD-F006、PRD-F007、PRD-F008、PRD-F009、PRD-F010、PRD-F011、PRD-F012、PRD-F013、PRD-F014、PRD-F015、PRD-F016、PRD-F017、PRD-F018、PRD-F019。Recording Analysis 是独立正式 Measurement Pipeline：External Recording → Import → Normalize/QA → Acoustic/ASR/Speaker Separation → Attribution/Fusion → Turn/EventTimeline → Canonical Metrics → Judge/Findings/Review/Report。

| ID | 需求与验收摘要 | 当前状态 |
| --- | --- | --- |
| F001 | 导入 WAV/MP3/M4A 与可选设备资料；不要求 TestCase；损坏输入也保留 Import Run 与失败原因 | ✅ implemented；真实 5–20 分钟录音验收待完成 |
| F002 | 原始/标准化/派生资产不可变，记录 Hash、元数据、父引用、转换器与参数 | ✅ implemented（限定导入资产） |
| F003 | 统一标准化与 Audio QA；格式、时长、空音频、解码失败等须明确诊断 | ✅ implemented；QA 不等于准确率保证 |
| F004 | 可恢复分阶段编排，阶段输入输出、失败、重试与审计可追溯 | 🟡 partial；Judge/Findings 已接入 Recording Analysis 主链（#10，受配置与角色 Gate 约束），真实 provider 调用与发布验收待完成 |
| F005 | File ASR 的完整录音识别、原生响应审计与时间戳；默认豆包 Seed ASR 2.0 `volc.seedasr.auc` 异步 submit/query，极速版为兼容模式；部分时间戳缺陷不得删除其余有效证据 | 🟡 partial；#87 已实现 transport，#93 跟踪 Seed recovery/partial evidence；真实质量待验收 |
| F006 | Speaker/source attribution：消费火山 File ASR 匿名 speaker labels；tester/device/unknown 只由用户人工确认，不使用 LLM 角色判断，不按先后猜角色 | ✅ implemented（软件）；匿名聚类、人工确认 Gate、不可变 revision 与 diff 已实现（#95）；真实录音人工标注验收待完成 |
| F007 | Turn/Response 关联必须有可解释角色和时序证据；歧义可弃权 | 🟡 partial；#94 提供确定性 acoustic↔speaker-span 对齐与覆盖诊断，#24 使 Turn/Response 关联在已确认角色下确定且可复现（连续同角色、设备先发言、编号唯一连续），未确认/冲突/未匹配一律弃权且不谎报 complete；真实混音打断判定待验收 |
| F008 | 自动 EventTimeline 由 Canonical Event 组成，并可追溯音频/转写/归属 Evidence | 🟡 partial；#94 对齐证据已可追溯，#24 保证事件证据覆盖其区间、声学时间证据只发布声学置信度、非 complete 时间线必须写 gaps；真实场景验收待完成 |
| F009 | 唯一确定性 Metric Engine 产出 MetricResult；不并行计算同名公式 | 🟡 partial；指标不可用时的原因与计数已在 API/报告/Web 显式输出（#94）；PRD-M003/M006 的受约束语义证据现已由 #10 Judge 供给，未供给或不合格时显式弃权 |
| F010 | Structured LLM Harness/Judge 受 schema、Evidence、版本与失败状态约束 | ✅ implemented（软件）：ImportRun 在配置了 Judge 且角色已确认时执行完整 Judge，结果通过 schema + Evidence/Event/Turn 引用校验后才成为语义证据（#10）；**真实 provider 调用与真实验收未完成**，仍由 #85 承载 |
| F011 | Findings 必须关联指标、证据、置信度、影响和复核状态 | ✅ implemented（软件）：Finding 2.1.0 显式记录 Turn/Event/Metric/Evidence 链接，缺可解析证据引用只记录弃权、不生成 Finding（#10）；真实录音复核待验收 |
| F012 | 人工修订为新 revision，不覆盖机器原件；可重算并显示差异 | ✅ implemented（软件）：角色 mapping 每次保存生成不可变 `role-review/role-mapping-REV-NNNN.json` 与新 AnalysisRevision，旧 artifact 字节不变并显示 diff（#95） |
| F013 | 生成 Markdown/JSON 报告，保留结论至证据的回溯路径 | 🟡 partial；指标可用性原因已进入报告（#94），正式报告仍需人工角色确认后才产出（#95） |
| F014 | Web/CLI 统一分析工作台；Web Evidence Workbench 使用 wavesurfer.js 展示 waveform、Regions/Timeline 与点击证据定位，不自研 waveform renderer | 🟡 partial；人工角色确认面板与区间试听已实现（#95），wavesurfer.js 集成 planned |
| F015 | 后端托管的 Provider 配置、路由、快照与调用审计；目标由服务器侧外置 `providers.yaml` / `storage.yaml` 提供非敏感配置，密钥仅以 env/secret reference 解析 | 🟡 partial；#87 已实现外置 YAML loader/validator + SQLite migration/conflict + Docker read-only mount，software_verified；真实部署验收待完成 |
| F016 | File ASR、Streaming ASR 与 TTS 按生命周期分家族；File ASR 支持 `inline | object_storage | auto`，Streaming ASR 不经过对象存储；浏览器不持有长期凭据 | 🟡 partial；#87 已实现 transport selection + TOS adapter，software_verified；真实云/设备待验收 |
| F017 | 同一原件可产生新 AnalysisRevision；旧产物、配置和差异可追溯 | ✅ implemented（软件）：角色确认与 ASR retry 都生成新 AnalysisRevision，旧产物保留并在 `role_review.diff` 显示变化（#95） |
| F018 | Compare 仅在 case/audio/policy/model/环境可比时报告差异，条件不兼容须拒绝比较 | ⬜ planned |
| F019 | Frozen Golden Voice：冻结刺激资产及 Hash/参数/精确静音；不等于设备真值 | ⬜ planned |

## F005 / F015 / F016 配置与 File ASR transport 决策

2026-09-18 授权的目标设计：

- Provider 与 Storage **分文件外置**：运行时分别加载 `/etc/aivoicebench/providers.yaml` 与 `/etc/aivoicebench/storage.yaml`（路径可由仅表示“文件位置”的环境变量覆盖）；仓库只保留 example。
- `providers.yaml` 统一声明 Judge/LLM、File ASR、Streaming ASR、TTS、diarization route 的 endpoint、model/resource、voice、timeout 与协议参数；`storage.yaml` 只声明对象存储 adapter、endpoint/region/bucket/prefix、凭据引用、Presigned URL TTL 与清理策略。
- 配置文件不得包含长期 secret 值；只保存 `credential_env` / AK/SK env reference。浏览器、Run snapshot、报告、Issue 和日志都不得回显 secret 或完整签名 URL。
- Recording Analysis 默认配置豆包 **Seed ASR 2.0 标准版**：`file_mode=seed_standard`、resource `volc.seedasr.auc`，使用异步 submit/query。必须在 query 前持久化 request ID；进程或客户端中断后优先恢复既有 job，不得用“重试”隐式重复提交。极速版只保留显式兼容模式，闲时版仍是未来 Provider mode。
- 极速版 transport 默认 `auto`：canonical WAV 小于等于可配置 `inline_max_bytes` 时走 `audio.data` Base64；超过阈值时才把对象上传到私有 TOS，并生成短期 Presigned GET URL 作为 `audio.url`。
- 初始工程默认 `inline_max_bytes = 15 MiB`，只是保守配置值，不是产品永久常量；必须可配置、可快照、可审计。
- 对象存储只解决“大文件让 Provider 可读取”的 transport 问题，不是 Evidence source，也不是所有 File ASR / Streaming ASR 的前置依赖。

仓库配置样例见 `config/providers.example.yaml` 与 `config/storage.example.yaml`；实现由 [Issue #87](https://github.com/lybym/AIVoiceBench/issues/87) 跟踪。

## F006 当前技术路线

当前阶段不引入 3D-Speaker 作为必要依赖。优先完成：

```text
External Recording
  ├─ Silero VAD → acoustic boundary evidence
  └─ Volcengine File ASR
       ├─ text / timestamp
       └─ anonymous speaker labels
                    ↓
             Attribution / Fusion
                    ↓
          tester / device / unknown
```

火山 speaker label / speaker ID 只表示 Provider 给出的匿名 cluster，不自动等于 tester/device。用户必须结合音频区间与 transcript，把每个 cluster 人工标记为 tester、device 或 unknown；系统保存不可变 revision 后才继续 role-dependent 分析。LLM 不参与角色归因，未知和冲突必须保留。

只有在真实 AI 玩具录音验证表明火山 speaker separation 的 coverage/quality 不足，或出现离线/供应商无关 diarization、独立 overlap detection 等明确需求时，再评估 3D-Speaker、pyannote 等额外 Provider。技术分工见 [Remote Browser Station 与开源组件策略](../26-remote-browser-component-strategy.md)。

## F014 Evidence Workbench

wavesurfer.js 负责 Web 端波形与区间交互：

- Waveform：原始/标准化或选定 Evidence Audio；
- Regions：speaker、turn、event、finding evidence 区间；
- Timeline：与 `audio_relative_ms` 对齐的可视时间轴；
- Finding/Metric/Event 点击后 seek/zoom/highlight；
- 同步展示 transcript、speaker role、confidence、uncertainty、processor/model provenance。

wavesurfer.js 不产生 Event，不改变 Artifact，也不是声学时间真值；所有 Region 坐标必须来自 AIVoiceBench Evidence/Timeline。

## 共同验收边界

- External Recording 是独立声学原件，不得被改写为 Active Measurement Evidence。
- 声学、角色和语义证据各自标记来源、置信度和不确定性；缺失时输出 `unknown`、`needs_review` 或 `insufficient_evidence`。
- File ASR 的 timestamp 与 transcript 是文字/语义证据，不自动成为声学真值。
- ASR-native speaker labels 是 diarization evidence，不是 tester/device role truth。
- `partial` ASR 若仍含有效 utterance/speaker evidence，必须把有效部分与显式 gap 一并传给 diarization/attribution；状态不是 `complete` 不能成为整份证据被丢弃的理由。
- acoustic boundary 与 ASR speaker span 的对齐必须记录 overlap/coverage、未匹配和冲突；不得为填充指标而就近复制 speaker/role。低音量设备对齐缺口由 #94 跟踪，已实现确定性对齐（`unmatched | single_cluster | multi_cluster | conflict`、双方区间与有符号偏移、两个方向重叠比例、逐聚类 coverage、low-energy 分布、boundary drift）与 `metrics_gap` 原因计数；诊断用 acoustic sensitivity profile 必须显式标记为非 canonical measurement policy。
- 指标为空时必须能从证据解释：API `metrics_gap`、报告“指标可用性”与 Web 面板给出原因代码与涉及片段数，不用 0 或成功占位符填充。
- 未保存人工角色 mapping 前，role-dependent Turns、Timeline、Metrics 与正式测试报告不运行；可展示的仅是明确标注为 provisional 的导入/诊断状态。人工 mapping 变更必须创建新 AnalysisRevision，不能覆盖机器原件或旧结论。Gate 状态必须可区分 `awaiting_role_review`、`incomplete_review`、`complete_review` 与人工判定的 `unknown`；保存后重分析只从 Attribution 向下重跑，识别与聚类作为原始机器证据被恢复而不是重算，因此不产生第二次云调用。
- 真实录音、真实云服务、人工标注和实体设备验收必须与软件/容器/浏览器验证分开记录。

详细技术边界见 [录音导入](../14-recording-import.md)、[Recording backbone](../23-recording-backbone.md)、[声学分段](../17-acoustic-segmentation.md)、[融合/Turn/Event](../18-fusion-turns-events.md)、[契约版本](../07-contract-versions.md) 与 [确定性引擎](../12-deterministic-engine.md)。
