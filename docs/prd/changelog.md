# PRD 变更历史

| PRD 版本 | 日期 | 变更摘要 |
| --- | --- | --- |
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
