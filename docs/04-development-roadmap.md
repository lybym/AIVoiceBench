# Development Roadmap — 双正式 Measurement Pipeline

> 产品范围、优先级和验收条件的唯一来源是 [PRD](PRD.md)；总体技术边界见 [架构文档](01-system-architecture.md)。本文只说明实施顺序、依赖和阶段出口，不把目标设计写成已实现。

## 1. 路线选择

AIVoiceBench 同时推进两条一级正式测量链：

- **Recording Analysis 收口线**：继续完成 M1 的真实火山 File ASR、ASR-native speaker separation、Attribution/Fusion、Judge、Findings、人工修订、完整报告和真实录音验收。
- **Active Measurement 演进线**：采用 Linux Server + Docker 后端与远端 Chrome Browser Station；现场产生 sample-indexed Measurement Audio，服务器完成最终化声学分析、Canonical Timeline 与正式 MetricResult。

两条线不共享 Audio Evidence；它们共享 Canonical Event 语义、指标定义、MetricResult 契约和尽可能一致的 Measurement Policy。Event Producer 可不同，Metric Producer 必须保持唯一。External Recording 可做独立复测和 Measurement Equivalence，不是 Active Result 转正前置。

近期组件决策见 [Remote Browser Station 与开源组件策略](26-remote-browser-component-strategy.md)：

- Browser Station 实现语言：手写源码为 **TypeScript**（`web/src/`），编译产物为 `aivoicebench/static/*.js`；只做等价迁移和静态类型工程化，不要求 React/Vue 等框架重写；由 [Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 跟踪，代码与 build/typecheck 门禁已完成；
- Browser Control VAD：TEN VAD（WASM）为目标实现；现有 RMS 保留 fallback/debug；
- Server Acoustic Boundary：Silero VAD 为已实现的正式 baseline（server-side ONNX Adapter，#23）；energy/RMS 保留 fixture/fallback；
- Speaker separation：优先使用火山 ASR 原生 speaker labels，当前不接 3D-Speaker；
- Evidence UI：wavesurfer.js；
- Windows Native：不作为正式交付方向。
- Provider/Storage 配置：服务器侧外置 `providers.yaml` + `storage.yaml` 已由 #87/PR #90 实现；Recording File ASR 目标默认豆包 Seed ASR 2.0 `volc.seedasr.auc` 异步 submit/query，小文件 inline Base64，大文件私有 TOS + Presigned GET。

## 2. 当前基线

代码实现基线更新为 `v0.5.0@bc70b947`（稳定版）；`main` 已合入 #86/#89/#90（software_verified），真实验收状态不因稳定版本发布自动升级。

| 范围 | 当前事实 | 明确缺口 |
| --- | --- | --- |
| Recording Analysis | 导入、标准化、云 File ASR、聚类/归属、融合，以及有角色证据时的 Turn/Event/Metric 主链已有 | #87 外置配置 + inline/TOS transport 已由 #90 实现（software_verified，真实 TOS 路径待 main 线验证）；真实火山 speaker separation（#22）、Judge/Findings、人工修订、wavesurfer、完整报告和真实录音验收（#85）未闭环 |
| Browser Station implementation | 手写源码为 `web/src/{app,models,voice_test,pcm_capture_worklet}.ts` + `audioworklet-globals.d.ts`；`aivoicebench/static/*.js` 为编译产物，仍由 FastAPI/Docker 直接交付 | 等价迁移与 deterministic typecheck/build 已完成（#84）；远端 Chrome 与容器内行为回归继续由既有 browser/container 测试与发布流程证明 |
| Fixed Voice Test | 浏览器播放、麦克风、Control RMS VAD、WebSocket 控制、Turn ID、超时、停止和迟到事件防护已有；当前 TTS 仍为 V3 HTTP SSE | #98 的 V3 单向 WS asset synthesis、冻结 Stimulus、TEN VAD Browser Adapter、Frozen Golden Voice、Measurement Audio、条件 Barge-in、设备设置快照和实体设备验收未闭环 |
| Free Voice Test | AudioWorklet 在设备回答阶段把 PCM 送入 Streaming ASR；partial/final、Agent 下一轮和显式 File ASR fallback 已有；当前仍等待完整 LLM/TTS 音频资产 | #98 的 Streaming LLM → V3 双向 WS TTS → streaming playback 未完成；另有 durable Measurement Audio、真实云/设备、预算、Coverage、Barge-in 缺口 |
| Canonical metrics | `compute_timeline_metrics(...)` 输出 MetricResult 3.0.0 | 只由 Recording Timeline 调用；Active Measurement 尚无 Canonical Timeline，不得另建平行公式 |
| Measurement Equivalence | 产品/方法概念已定义 | 没有真实配对实验；保持 validation_pending |

## 3. 近期执行顺序

### R0 — Provider / Object Storage Configuration Foundation (#87)

1. 将 Judge/LLM、File ASR、Streaming ASR、TTS、diarization routes 的非敏感配置收敛到服务器侧外置 `providers.yaml`；
2. 将对象存储参数收敛到独立 `storage.yaml`，首个 adapter 为私有 TOS；长期 API Key / AK / SK 只通过 env/secret reference 解析；
3. Docker 只读挂载外置文件，Run start 时解析一次，snapshot 只保存 resolved non-secret provenance；
4. File ASR 极速版实现 `inline | object_storage | auto`：默认 `auto`，初始 inline 阈值 15 MiB 且可配置；
5. 小文件使用 `audio.data` Base64，不要求 Storage；大文件由 Backend 直接上传私有 TOS，生成短期 Presigned GET 作为 `audio.url`，完成后清理并保留 lifecycle backstop；
6. 移除生产路径对 `AIVOICEBENCH_AUDIO_PUT_URL/GET_URL/HOST` 的依赖；SQLite model settings 与外置配置不得形成两个静默 source of truth；
7. #87 本切片不实现标准版/闲时版；后续 Seed standard 异步恢复与 partial evidence 由 #93 独立跟踪，闲时版仍保留未来扩展点。

阶段出口：fresh Docker deployment 可仅凭外置 provider/storage 配置和 backend secret env 完成配置解析；小 File ASR 不配置 TOS 也可运行 inline，大文件明确选择 TOS URL transport；所有配置冲突、缺失与 secret redaction 可审计。

### R1 — Recording Analysis：先把已有云能力用完整

1. 在 #87 的外置 Provider/Storage 配置与 File ASR transport 基础上，核对并实现当前火山 File ASR 自动说话人分离参数/响应映射；
2. 保存匿名 speaker labels、provider/model/config provenance；
3. Attribution 继续把 `speaker_N` 映射到 tester/device/unknown，禁止按顺序猜角色；
4. 真实 5–20 分钟 AI 玩具录音验证 speaker coverage、冲突、abstention 和角色复核工作量；
5. 未达到要求再建立独立 diarization Provider 选型任务；当前不引入 3D-Speaker。
6. #93 完成 Seed standard submit/query、既有 job 恢复和 partial transcript/speaker evidence 传播；#94 完成 acoustic segment ↔ ASR speaker span 的可审计对齐、低音量设备 coverage 与无指标解释；#95 提供用户人工角色确认、revision、重分析和正式报告 Gate，禁止 LLM 角色判断。

阶段出口：至少一批真实录音可以从 File ASR speaker labels 进入 Attribution/Fusion，且 unknown/conflict 行为可审计。

### R2 — Acoustic Boundary Provider

1. ✅ 保留 `EnergyVadSegmenter` 作为 fixture/fallback；其行为、参数与文档均未改变；
2. ✅ 接入 Silero VAD server-side Adapter（[Issue #23](https://github.com/lybym/AIVoiceBench/issues/23)）：`aivoicebench/silero_vad.py` 经 `AcousticSegmenter` Protocol 实现，onnxruntime 直接执行官方 ONNX 导出图，权重来自 `silero-vad` wheel 6.2.2（sha256 校验并写入文档），不导入 torch；
3. ✅ Recording Analysis 可配置切换 Energy/Silero（`AIVOICEBENCH_ACOUSTIC_PROVIDER`、`--vad`），不改变下游 AcousticSegments contract；模型 provider 不可用时 acoustic stage 显式失败，不静默回退；
4. ⬜ 建立人工标注样本，报告 speech start/end error、miss/false alarm 与 coverage —— **未完成**。已实现可复用评测脚手架 `aivoicebench/vad_evaluation.py` + `schemas/vad-annotation.schema.json` + `aivoicebench vad-eval`；无标注数据时显式输出 `no_annotated_sample_set`。AC3 仍待人工标注真实录音验收；
5. ✅ 不把 Silero 默认阈值当产品门槛：阈值进入版本化 `silero_boundary_policy/1.0.0`，文档明确标记未在目标录音上校准且非 canonical measurement policy。

阶段出口：Silero 能对同一 Artifact deterministic replay，输出带 processor/policy/confidence/uncertainty 的 Acoustic Evidence —— ✅ 已由 `tests/test_silero_vad.py` 的 replay 用例证明（比较时仅排除 `document_id` 与 `source.path` 两个 identity/environment 字段）。真实录音质量与 AC3 人工标注评测仍未完成。

### R3 — Browser Station TypeScript Foundation 与 TEN VAD

1. ✅ 按 [Issue #84](https://github.com/lybym/AIVoiceBench/issues/84) 将 `app.js`、`models.js`、`voice_test.js`、`pcm_capture_worklet.js` 等价迁移到 TypeScript，建立 deterministic build/typecheck（`web/src/`、`tsconfig.json`、`scripts/verify-web-build.mjs`）；未引入 React/Vue，未改变现有协议/Measurement semantics；
2. ✅ 为 audio frame、AudioWorklet message、sample index/sequence、VAD/control observation、capture integrity、media settings、WebSocket lifecycle 与 Run state 建立显式 TypeScript 类型（`web/src/models.ts` 为共享契约层）；
3. ✅ 编译后 JavaScript 仍由现有 FastAPI/Docker 静态链交付（`aivoicebench/static/`，Dockerfile 与 `/static` 挂载未变）；Fixed/Free 行为未回退由既有 browser/container tests 与发布流程证明（本地不做真实浏览器声明）；
4. 明确 Browser Station metadata contract：browser/OS、input/output device、requested/actual media settings、sample rate；
5. `AudioWorklet` sample counter 与 sequence 成为现场时间轴；
6. 接入 TEN VAD WASM 作为 Browser Control VAD；
7. RMS VAD 保留显式 fallback，所有 event 记录 source；
8. 断网、暂停、后台、权限变化、设备切换和 gap/duplicate/stale frame 都产生明确状态。

阶段出口：Browser Station 以 TypeScript 作为主要手写源码语言，typecheck/build 与现有浏览器行为验证通过；控制链可以在不依赖 server receive timestamp 的情况下产生 sample-indexed provisional speech events。

### R3.5 — Active TTS V3 WebSocket split (#98)

1. 保留 `tts` 作为 Fixed/asset synthesis route，目标协议改为火山 V3 WebSocket 单向流式；新增 `streaming_tts` route 专用于 Free 的 V3 WebSocket 双向流式；不允许一个模糊 profile 静默猜 transport；
2. Fixed：完整 Case 文本经单向 WS **固定流式返回 MP3**，完成校验后直接冻结为 MP3 Stimulus Artifact，记录 SHA-256、sample metadata、speaker/voice、resource/model 与 non-secret config snapshot；正式 Run 仅播放冻结资产，不做 WAV 转换；
3. Free：LLM Provider/Agent 增加 streaming output，按安全可播边界顺序送入双向 TTS session；**MP3 audio chunks** 按 Turn identity 流式送到 Browser Station 播放；
4. Stop/cancel/stale Turn/断连必须终止 session 或丢弃迟到 chunk；不得把上一 Turn 音频串入下一 Turn，也不得在失败时静默退回旧 SSE/单向接口；
5. `providers.yaml` **不暴露 TTS format/encoding**；Active TTS 输出格式固定为 MP3。仅暴露官方 V3 文档实际支持且有必要调整的 speaker/voice、sample rate、speech rate，以及协议/音色支持时的 loudness/pitch 等参数；unsupported 参数组合显式失败；
6. Fixed 直接冻结 Provider 返回的 MP3 Stimulus，不再增加 WAV 封装/转码层，减少格式分支和配置面；
7. 单向/双向协议、鉴权、binary frames、session lifecycle 与错误处理必须基于当前官方文档建立 contract tests，并分别完成真实云 provider integration evidence。

阶段出口：Fixed 的正式播放资产可冻结、可 hash、可复现；Free 可在 LLM 完整 response 结束前开始收到并播放 TTS 音频；两者保持 credential/backend boundary 与 Control/Measurement Evidence 分层。

### R4 — wavesurfer.js Evidence Workbench

1. 引入 wavesurfer.js core + Regions + Timeline；
2. 先服务 Recording Analysis，再复用到 Active Measurement；
3. Event/Finding/Metric 点击定位对应 Evidence Region；
4. waveform、transcript、speaker role、event、provenance 同步；
5. 不在前端重新计算正式指标，不让 Region 成为新的事实来源。

阶段出口：从 Finding/Metric/Event 可以一键定位并回放其证据音频区间。

## 4. Active Measurement 连续里程碑

| 阶段 | 当前状态 | 关键交付 | PRD refs | 阶段出口 |
| --- | --- | --- | --- | --- |
| A — Remote Browser Measurement Foundation | ⬜ 下一实现阶段 | Browser Station TypeScript foundation（#84）、continuous browser PCM、local sample counter、durable `ART-live-measurement-audio`、capture integrity、Browser Station metadata、Execution Run 引用 | F023/F025、N002/N003/N007 | TypeScript typecheck/build + 既有 browser/container 回归通过；WAV/hash/sample count 一致；网络 RTT/receive time 不进入 acoustic metric；连续/重复/缺口/stale/stop/cancel/断连有测试 |
| B — Browser Control VAD | ⬜ planned | TEN VAD WASM Adapter、RMS fallback、sample-indexed provisional boundaries | F020/F021/F023 | Control VAD source 可追溯；断网不改变已生成的本地音频时间 |
| C — Server Finalized Acoustic Measurement | 🟡 partial | Silero VAD Adapter ✅（#23）、Measurement Policy ✅（`silero_boundary_policy/1.0.0`）、boundary uncertainty ✅；durable Measurement Audio replay 与 Active Run 接线未实现 | F025、N007 | 同一 Measurement Audio 可 deterministic replay；Silero/TEN/RMS 不静默覆盖彼此 |
| D — Stimulus Measurement | ⬜ planned | 保存 stimulus reference/identity/sample info；Measurement Audio alignment；tester speech start/end | F019/F020/F025、M002/M004 | tester boundary 来自声学 alignment 而非 playback callback；弱/多重匹配弃权 |
| E — Canonical Live Timeline | ⬜ planned | Active Measurement → EventTimeline；Evidence refs；Turn/Response identity；unknown/abstain | F008/F025 | Canonical event taxonomy 与 Recording 一致；`execution-record.json` 仍独立保留 |
| F — Unified Metrics | ⬜ planned | Active Timeline → `compute_timeline_metrics(...)`；provisional display；finalized MetricResult | F009/F025、M001–M010 | 相同 canonical fixture 不论 pipeline 均得相同数值；无 `live_*`/`offline_*` 指标 |
| G — Measurement Equivalence | ⬜ validation_pending | 同一物理交互的独立 External Recording；paired bias/error/agreement；批准阈值 | F024/F026、N007 | Mean Bias、Median AE、P95 AE、Bland-Altman、边界/timeout/barge-in agreement 有真实实验 |
| H — Advanced Overlap / Barge-in | ⬜ planned | stimulus reference/loopback、reference cancellation/AEC/source-aware processing、overlap identity | F020/F025、M005/M006/M007/M009 | 单麦克风 VAD 不再被误写为已解决；证据不足继续 insufficient_evidence |

## 5. 与既有产品里程碑的映射

- **M1 Recording Analysis**：R0、R1、R2、R4 优先收口配置/transport 与真实录音证据链。
- **M2 Fixed Voice Test**：A、B、C、D 完成普通 turn-taking 最小闭环；#98 完成 V3 单向 WS stimulus synthesis + frozen asset；Browser Station TypeScript 等价迁移属于 A 的工程基础，不新增产品能力。
- **M3 Free Voice Test Agent**：#98 完成 Streaming LLM → V3 双向 WS TTS → streaming playback，再复用相同 Browser Station/Measurement Plane 完成 E、F 的实时展示/最终化。
- **M4 关联与验证**：完成 G；外部录音关联服务于复测/方法验证，不承担 Active Result 转正。
- **M5 Compare / Regression**：只比较兼容 case/asset/metric/policy 版本；H 在证据与硬件条件成熟后进入。

## 6. 阶段门禁

每个阶段分别记录：

1. **Contract verified**：schema、身份、sample timebase、完整性、错误与兼容行为有针对性测试。
2. **Software verified**：受控 PCM/fixture 下的状态机、处理器、失败隔离和审计通过。
3. **Browser verified**：远端 Chrome 的权限、持续音频生命周期、MediaTrackSettings、停止/断连和多轮操作通过；Browser Station TypeScript 迁移后还必须通过 typecheck/build，且不能用编译成功替代行为验证。
4. **Container/server verified**：Linux Docker 运行、持久卷、重启读取、API 下载和远端 WebSocket 通过。
5. **Physical-device verified**：真实扬声器、麦克风和实体设备完成执行；记录环境和失败。
6. **Measurement-equivalence verified**：独立双录音配对、误差/偏差/一致性达到批准策略。

前一层通过不自动升级后一层。发布镜像、合成音频、Provider mock、浏览器播放成功或 Execution Run 完成都不能替代真实设备或 Measurement Equivalence。

## 7. 兼容性与非目标

- Recording Analysis、File ASR/Streaming ASR 生命周期、Fixed 无 ASR 基础控制、Free Mic→Streaming ASR→Agent、stop 行为和 provider call accounting 必须回归。
- `execution-record.json` 保留 Control Trace 价值；新 Measurement artifacts 通过引用并存，历史 artifacts/schema 尽量可读。
- Browser Station TypeScript 迁移是源码工程化，不改变正式 Measurement semantics；编译后的浏览器 JavaScript 继续由现有 Docker/FastAPI 交付。
- VAD 是并行 Evidence Producer，不是 File/Streaming ASR 的强制前置。
- 第一阶段可以只保存 Stimulus Reference/interface，不输出伪造 tester acoustic boundary。
- RTC、S2S、Hybrid、React/Vue/Svelte 框架重写、数据库/集群、Windows Native 全量迁移、3D-Speaker、专业 SPL/loopback Station 不进入近期基础切片关键路径。
- TEN VAD 正式商业/分发接入前需要完成其附加许可证条件审查；许可证未确认不能视为依赖验收完成。
