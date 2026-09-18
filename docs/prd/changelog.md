# PRD 变更历史

| PRD 版本 | 日期 | 变更摘要 |
| --- | --- | --- |
| 1.5.8 | 2026-09-18 | 落实 F006/F012–F014/F017 的人工角色确认 Gate：匿名 cluster 出现时 Run 在 `awaiting_role_review` 暂停，role-dependent 阶段与正式报告不运行；Web/API 提供逐聚类复核面（代表性区间、转写片段、试听）、必须每个聚类明确决定（`unknown` 有效）、保存生成不可变 revision 与新 AnalysisRevision、显示 diff。重分析只从 Attribution 向下重跑并**恢复**识别/聚类证据，不重复云调用。Gate 状态区分 `awaiting_role_review`/`incomplete_review`/`complete_review`。新增 `role-review` 契约与 `speaker-role-review`/`speaker-role-mapping` artifact kind，既有 recording-run schema 不变。#95 软件验收完成；真实验收仍由 #85 承载。 |
| 1.5.7 | 2026-09-18 | 落实 F007–F009 与共同验收中的 acoustic↔ASR speaker span 对齐要求：对齐改为独立确定性模块并登记 `speaker-alignment` artifact，记录双方区间/有符号偏移/双向重叠比例/逐聚类 coverage/low-energy 分布/boundary drift；未匹配、冲突、未知显式保留，禁止 nearest-role 填充。指标为空必须在 API `metrics_gap`、报告与 Web 给出原因与计数。新增可选 acoustic sensitivity profile，非 canonical 必须显式标记。新增可选 `processor.sensitivity` 与 `SpeakerAlignment 1.0.0` 两个契约决定（保持 AcousticSegments 1.0.0）。#94 软件验收完成；真实验收仍由 #85 承载。 |
| 1.5.6 | 2026-09-18 | Recording Analysis 决策收敛（#93/#94/#95）：默认使用豆包 Seed ASR 2.0 标准版 `volc.seedasr.auc` 异步 submit/query，request ID 先持久化、恢复时 query 既有 job、禁止隐式重复 submit，极速版仅保留显式兼容模式；`partial` transcript 保留有效 utterance/timestamp/匿名 speaker evidence；acoustic boundary 与 ASR speaker span 的对齐必须记录 overlap/coverage 并保留未匹配/冲突；tester/device 角色只由用户人工确认（不使用 LLM），保存不可变新 revision 后才继续 role-dependent 分析。本行合并同一 PR 内先后记录的 1.5.4–1.5.6 三项 Recording Analysis 决定，避免与 #98 已占用的 1.5.4/1.5.5 版本号冲突。 |
| 1.5.5 | 2026-09-18 | Active TTS 配置进一步收敛：Fixed 与 Free 的 V3 WebSocket TTS 输出格式统一固定为 MP3，`format/encoding` 不再作为 `providers.yaml` 可配置项；Fixed 直接冻结 MP3 Stimulus，不再增加 WAV/PCM 转换层。其他 Recording Analysis / Active Measurement 证据音频格式不受影响。#98 同步更新验收。 |
| 1.5.4 | 2026-09-18 | Active TTS transport 收敛：Fixed Case Runner 目标使用火山 V3 WebSocket 单向流式完成完整话术合成并在正式 Run 前冻结 Stimulus Artifact；Free Test Agent 目标使用 V3 WebSocket 双向流式承接 Streaming LLM 并流式播放。Provider route 区分 `tts` 与 `streaming_tts`，常用音色/编码/采样率/语速以及协议实际支持的音量/音调等参数按官方 V3 字段外置配置。实现由 #98 跟踪；当前 HTTP SSE TTS 与既有代码/真实验收状态不自动升级。 |
| 1.5.3 | 2026-09-18 | 配置与 File ASR transport 收敛：LLM/ASR/TTS Provider 与对象存储非敏感参数改为服务器侧外置配置文件目标；长期密钥只通过 env/secret reference 解析。Recording Analysis P0 继续使用火山极速版 HTTP，小文件 inline Base64，大文件私有 TOS + 短期 Presigned GET URL；固定 PUT/GET/HOST 仅为待迁移现状。实现由 #87 跟踪，不升级代码/真实验收状态。 |
| 1.5.2 | 2026-09-16 | 部署与组件路线收敛：正式主架构明确为 Linux Server + Docker 后端与远端 Chrome Browser Station；正式声学时间必须在现场以 sample clock 生成。近期 VAD 采用 TEN VAD（浏览器控制）+ Silero VAD（服务器离线/最终化分析）；Recording Analysis 优先使用火山 ASR speaker separation，当前不接 3D-Speaker；Evidence UI 采用 wavesurfer.js。实现基线与真实验收状态不升级。 |
| 1.5.1 | 2026-09-13 | 正式版事实收敛：把 alpha.6 自由对话修复与可选语义角色提议纳入 `v0.4.0` 发布基线；更新 Docker/Web 交付说明。产品边界、Requirement ID 与真实验收门槛不变。 |
| 1.5.0 | 2026-09-13 | 结构化拆分：`PRD.md` 保持唯一入口；详细功能、指标、验收、追踪和历史分别移入 `docs/prd/`。Requirement ID、产品边界、验收门槛与实现状态不因拆分而升级或降低。 |
| 1.4.0 | 2026-09-13 | 确立 Active Measurement 与 Recording Analysis 两条独立正式 Measurement Pipeline；新增 F025、F026、N007，正式声学时间以 sample clock 为准。 |
| 1.3.3 | 2026-09-13 | 发布事实校准至 v0.4.0-alpha.4；不改变产品需求或真实验收状态。 |
| 1.3.2–1.3.0 | 2026-09-11 至 2026-09-12 | Streaming ASR 路线分叉、预检/判停/降级行为收敛，保持真实云与实体设备验收 pending。 |
| 1.2.3–1.2.0 | 2026-09-11 | 控制可靠性、TTS 与主动测试排程收敛；不降低 M1 验收。 |
| 1.1.2–1.0.0 | 2026-09-10 至 2026-09-11 | 导入优先、中央 PRD、双主流程、证据链、里程碑和 Issue 追踪基础建立。 |

完整的逐项历史理由保留在 Git 提交历史、发布说明和 [工作日志](../06-work-log.md)。PRD 变更必须在同一 PR 记录 Requirement ID、范围、验收与用户决定。
