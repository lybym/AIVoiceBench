# Recording Analysis 产品需求

本文承载 PRD-F001–F019。Requirement ID 覆盖：PRD-F001、PRD-F002、PRD-F003、PRD-F004、PRD-F005、PRD-F006、PRD-F007、PRD-F008、PRD-F009、PRD-F010、PRD-F011、PRD-F012、PRD-F013、PRD-F014、PRD-F015、PRD-F016、PRD-F017、PRD-F018、PRD-F019。Recording Analysis 是独立正式 Measurement Pipeline：External Recording → Import → Normalize/QA → Acoustic/ASR/Speaker Separation → Attribution/Fusion → Turn/EventTimeline → Canonical Metrics → Judge/Findings/Review/Report。

| ID | 需求与验收摘要 | 当前状态 |
| --- | --- | --- |
| F001 | 导入 WAV/MP3/M4A 与可选设备资料；不要求 TestCase；损坏输入也保留 Import Run 与失败原因 | ✅ implemented；真实 5–20 分钟录音验收待完成 |
| F002 | 原始/标准化/派生资产不可变，记录 Hash、元数据、父引用、转换器与参数 | ✅ implemented（限定导入资产） |
| F003 | 统一标准化与 Audio QA；格式、时长、空音频、解码失败等须明确诊断 | ✅ implemented；QA 不等于准确率保证 |
| F004 | 可恢复分阶段编排，阶段输入输出、失败、重试与审计可追溯 | 🟡 partial；Judge/Findings 尚未接入主链 |
| F005 | File ASR 的完整录音识别、原生响应审计与时间戳；P0 默认火山极速版 HTTP，按配置选择 inline Base64 或私有对象存储 + 短期 Presigned GET URL，Storage 只作为 transport | 🟡 partial；当前代码仍依赖固定 Signed URL publication，#87 负责重构，真实服务与质量待验收 |
| F006 | Speaker/source attribution：近期优先消费火山 File ASR 自动说话人分离的匿名 speaker labels；角色证据不足时保持 unknown；可选语义处理器只提出待复核角色，不按先后猜 tester/device | 🟡 partial；软件验证已覆盖提议、弃权、冲突和 provenance，真实 speaker separation / 角色识别待验收 |
| F007 | Turn/Response 关联必须有可解释角色和时序证据；歧义可弃权 | 🟡 partial |
| F008 | 自动 EventTimeline 由 Canonical Event 组成，并可追溯音频/转写/归属 Evidence | 🟡 partial |
| F009 | 唯一确定性 Metric Engine 产出 MetricResult；不并行计算同名公式 | 🟡 partial |
| F010 | Structured LLM Harness/Judge 受 schema、Evidence、版本与失败状态约束 | 🟡 partial；ImportRun 未执行完整 Judge |
| F011 | Findings 必须关联指标、证据、置信度、影响和复核状态 | 🟡 partial |
| F012 | 人工修订为新 revision，不覆盖机器原件；可重算并显示差异 | 🟡 partial |
| F013 | 生成 Markdown/JSON 报告，保留结论至证据的回溯路径 | 🟡 partial |
| F014 | Web/CLI 统一分析工作台；Web Evidence Workbench 使用 wavesurfer.js 展示 waveform、Regions/Timeline 与点击证据定位，不自研 waveform renderer | 🟡 partial；wavesurfer.js 集成 planned |
| F015 | 后端托管的 Provider 配置、路由、快照与调用审计；目标由服务器侧外置 `providers.yaml` / `storage.yaml` 提供非敏感配置，密钥仅以 env/secret reference 解析 | 🟡 partial；现有 SQLite model settings 已实现，#87 负责外置文件迁移 |
| F016 | File ASR、Streaming ASR 与 TTS 按生命周期分家族；File ASR 支持 `inline | object_storage | auto`，Streaming ASR 不经过对象存储；浏览器不持有长期凭据 | 🟡 partial；#87 + 真实云/设备待验收 |
| F017 | 同一原件可产生新 AnalysisRevision；旧产物、配置和差异可追溯 | 🟡 partial |
| F018 | Compare 仅在 case/audio/policy/model/环境可比时报告差异，条件不兼容须拒绝比较 | ⬜ planned |
| F019 | Frozen Golden Voice：冻结刺激资产及 Hash/参数/精确静音；不等于设备真值 | ⬜ planned |

## F005 / F015 / F016 配置与 File ASR transport 决策

2026-09-18 授权的目标设计：

- Provider 与 Storage **分文件外置**：运行时分别加载 `/etc/aivoicebench/providers.yaml` 与 `/etc/aivoicebench/storage.yaml`（路径可由仅表示“文件位置”的环境变量覆盖）；仓库只保留 example。
- `providers.yaml` 统一声明 Judge/LLM、File ASR、Streaming ASR、TTS、diarization route 的 endpoint、model/resource、voice、timeout 与协议参数；`storage.yaml` 只声明对象存储 adapter、endpoint/region/bucket/prefix、凭据引用、Presigned URL TTL 与清理策略。
- 配置文件不得包含长期 secret 值；只保存 `credential_env` / AK/SK env reference。浏览器、Run snapshot、报告、Issue 和日志都不得回显 secret 或完整签名 URL。
- Recording Analysis 的 P0 File ASR 默认使用火山**录音文件识别极速版 HTTP**。标准版和闲时版保留为未来 Provider mode，不进入 #87 当前实现范围。
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

火山 speaker label / speaker ID 只表示 Provider 给出的匿名 cluster，不自动等于 tester/device。系统继续通过显式映射、语义角色提议、上下文证据和人工复核完成 role attribution；未知和冲突必须保留。

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
- 语义角色提议保留 provider/model/prompt/invocation/evidence refs，模型自报置信度不作为校准准确率；显式或人工证据优先，冲突不覆盖。
- 真实录音、真实云服务、人工标注和实体设备验收必须与软件/容器/浏览器验证分开记录。

详细技术边界见 [录音导入](../14-recording-import.md)、[Recording backbone](../23-recording-backbone.md)、[声学分段](../17-acoustic-segmentation.md)、[融合/Turn/Event](../18-fusion-turns-events.md) 与 [确定性引擎](../12-deterministic-engine.md)。