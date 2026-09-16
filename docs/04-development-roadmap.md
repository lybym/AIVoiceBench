# Development Roadmap — 双正式 Measurement Pipeline

> 产品范围、优先级和验收条件的唯一来源是 [PRD](PRD.md)；总体技术边界见 [架构文档](01-system-architecture.md)。本文只说明实施顺序、依赖和阶段出口，不把目标设计写成已实现。

## 1. 路线选择

AIVoiceBench 同时推进两条一级正式测量链：

- **Recording Analysis 收口线**：继续完成 M1 的真实火山 File ASR、ASR-native speaker separation、Attribution/Fusion、Judge、Findings、人工修订、完整报告和真实录音验收。
- **Active Measurement 演进线**：采用 Linux Server + Docker 后端与远端 Chrome Browser Station；现场产生 sample-indexed Measurement Audio，服务器完成最终化声学分析、Canonical Timeline 与正式 MetricResult。

两条线不共享 Audio Evidence；它们共享 Canonical Event 语义、指标定义、MetricResult 契约和尽可能一致的 Measurement Policy。Event Producer 可不同，Metric Producer 必须保持唯一。External Recording 可做独立复测和 Measurement Equivalence，不是 Active Result 转正前置。

近期组件决策见 [Remote Browser Station 与开源组件策略](26-remote-browser-component-strategy.md)：

- Browser Control VAD：TEN VAD（WASM）为目标实现；现有 RMS 保留 fallback/debug；
- Server Acoustic Boundary：Silero VAD 为首个正式候选 baseline；
- Speaker separation：优先使用火山 ASR 原生 speaker labels，当前不接 3D-Speaker；
- Evidence UI：wavesurfer.js；
- Windows Native：不作为正式交付方向。

## 2. 当前基线

代码实现基线仍是 `v0.4.0@9632844`；2026-09-16 的组件/部署决策只更新路线，不自动升级实现状态。

| 范围 | 当前事实 | 明确缺口 |
| --- | --- | --- |
| Recording Analysis | 导入、标准化、云 File ASR、聚类/归属、融合，以及有角色证据时的 Turn/Event/Metric 主链已有 | 真实火山 speaker separation 请求/响应验收、Judge/Findings 主链、人工修订、wavesurfer Evidence UI、完整报告和真实录音验收未闭环 |
| Fixed Voice Test | 浏览器播放、麦克风、Control RMS VAD、WebSocket 控制、Turn ID、超时、停止和迟到事件防护已有 | TEN VAD Browser Adapter、Frozen Golden Voice、Measurement Audio、条件 Barge-in、设备设置快照和实体设备验收未闭环 |
| Free Voice Test | AudioWorklet 在设备回答阶段把 PCM 送入 Streaming ASR；partial/final、Agent 下一轮和显式 File ASR fallback 已有 | 不是跨整次 Run 的 durable Measurement Audio；真实云/设备、预算、Coverage、Barge-in 与 Streaming TTS 未完成 |
| Canonical metrics | `compute_timeline_metrics(...)` 输出 MetricResult 3.0.0 | 只由 Recording Timeline 调用；Active Measurement 尚无 Canonical Timeline，不得另建平行公式 |
| Measurement Equivalence | 产品/方法概念已定义 | 没有真实配对实验；保持 validation_pending |

## 3. 近期执行顺序

### R1 — Recording Analysis：先把已有云能力用完整

1. 核对并实现当前火山 File ASR 自动说话人分离参数/响应映射；
2. 保存匿名 speaker labels、provider/model/config provenance；
3. Attribution 继续把 `speaker_N` 映射到 tester/device/unknown，禁止按顺序猜角色；
4. 真实 5–20 分钟 AI 玩具录音验证 speaker coverage、冲突、abstention 和角色复核工作量；
5. 未达到要求再建立独立 diarization Provider 选型任务；当前不引入 3D-Speaker。

阶段出口：至少一批真实录音可以从 File ASR speaker labels 进入 Attribution/Fusion，且 unknown/conflict 行为可审计。

### R2 — Acoustic Boundary Provider

1. 保留 `EnergyVadSegmenter` 作为 fixture/fallback；
2. 接入 Silero VAD server-side Adapter；
3. Recording Analysis 默认可配置切换 Energy/Silero，不改变下游 AcousticSegments contract；
4. 建立人工标注样本，报告 speech start/end error、miss/false alarm 与 coverage；
5. 不把 Silero 默认阈值当产品门槛，阈值进入版本化 Measurement Policy。

阶段出口：Silero 能对同一 Artifact deterministic replay，输出带 processor/policy/confidence/uncertainty 的 Acoustic Evidence。

### R3 — Browser Station 与 TEN VAD

1. 明确 Browser Station metadata contract：browser/OS、input/output device、requested/actual media settings、sample rate；
2. `AudioWorklet` sample counter 与 sequence 成为现场时间轴；
3. 接入 TEN VAD WASM 作为 Browser Control VAD；
4. RMS VAD 保留显式 fallback，所有 event 记录 source；
5. 断网、暂停、后台、权限变化、设备切换和 gap/duplicate/stale frame 都产生明确状态。

阶段出口：控制链可以在不依赖 server receive timestamp 的情况下产生 sample-indexed provisional speech events。

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
| A — Remote Browser Measurement Foundation | ⬜ 下一实现阶段 | continuous browser PCM、local sample counter、durable `ART-live-measurement-audio`、capture integrity、Browser Station metadata、Execution Run 引用 | F023/F025、N002/N003/N007 | WAV/hash/sample count 一致；网络 RTT/receive time 不进入 acoustic metric；连续/重复/缺口/stale/stop/cancel/断连有测试 |
| B — Browser Control VAD | ⬜ planned | TEN VAD WASM Adapter、RMS fallback、sample-indexed provisional boundaries | F020/F021/F023 | Control VAD source 可追溯；断网不改变已生成的本地音频时间 |
| C — Server Finalized Acoustic Measurement | ⬜ planned | Silero VAD Adapter、Measurement Policy、durable audio replay、boundary uncertainty | F025、N007 | 同一 Measurement Audio 可 deterministic replay；Silero/TEN/RMS 不静默覆盖彼此 |
| D — Stimulus Measurement | ⬜ planned | 保存 stimulus reference/identity/sample info；Measurement Audio alignment；tester speech start/end | F019/F020/F025、M002/M004 | tester boundary 来自声学 alignment 而非 playback callback；弱/多重匹配弃权 |
| E — Canonical Live Timeline | ⬜ planned | Active Measurement → EventTimeline；Evidence refs；Turn/Response identity；unknown/abstain | F008/F025 | Canonical event taxonomy 与 Recording 一致；`execution-record.json` 仍独立保留 |
| F — Unified Metrics | ⬜ planned | Active Timeline → `compute_timeline_metrics(...)`；provisional display；finalized MetricResult | F009/F025、M001–M010 | 相同 canonical fixture 不论 pipeline 均得相同数值；无 `live_*`/`offline_*` 指标 |
| G — Measurement Equivalence | ⬜ validation_pending | 同一物理交互的独立 External Recording；paired bias/error/agreement；批准阈值 | F024/F026、N007 | Mean Bias、Median AE、P95 AE、Bland-Altman、边界/timeout/barge-in agreement 有真实实验 |
| H — Advanced Overlap / Barge-in | ⬜ planned | stimulus reference/loopback、reference cancellation/AEC/source-aware processing、overlap identity | F020/F025、M005/M006/M007/M009 | 单麦克风 VAD 不再被误写为已解决；证据不足继续 insufficient_evidence |

## 5. 与既有产品里程碑的映射

- **M1 Recording Analysis**：R1、R2、R4 优先收口真实录音证据链。
- **M2 Fixed Voice Test**：A、B、C、D 完成普通 turn-taking 最小闭环。
- **M3 Free Voice Test Agent**：复用相同 Browser Station/Measurement Plane，完成 E、F 的实时展示/最终化。
- **M4 关联与验证**：完成 G；外部录音关联服务于复测/方法验证，不承担 Active Result 转正。
- **M5 Compare / Regression**：只比较兼容 case/asset/metric/policy 版本；H 在证据与硬件条件成熟后进入。

## 6. 阶段门禁

每个阶段分别记录：

1. **Contract verified**：schema、身份、sample timebase、完整性、错误与兼容行为有针对性测试。
2. **Software verified**：受控 PCM/fixture 下的状态机、处理器、失败隔离和审计通过。
3. **Browser verified**：远端 Chrome 的权限、持续音频生命周期、MediaTrackSettings、停止/断连和多轮操作通过。
4. **Container/server verified**：Linux Docker 运行、持久卷、重启读取、API 下载和远端 WebSocket 通过。
5. **Physical-device verified**：真实扬声器、麦克风和实体设备完成执行；记录环境和失败。
6. **Measurement-equivalence verified**：独立双录音配对、误差/偏差/一致性达到批准策略。

前一层通过不自动升级后一层。发布镜像、合成音频、Provider mock、浏览器播放成功或 Execution Run 完成都不能替代真实设备或 Measurement Equivalence。

## 7. 兼容性与非目标

- Recording Analysis、File ASR/Streaming ASR 生命周期、Fixed 无 ASR 基础控制、Free Mic→Streaming ASR→Agent、stop 行为和 provider call accounting 必须回归。
- `execution-record.json` 保留 Control Trace 价值；新 Measurement artifacts 通过引用并存，历史 artifacts/schema 尽量可读。
- VAD 是并行 Evidence Producer，不是 File/Streaming ASR 的强制前置。
- 第一阶段可以只保存 Stimulus Reference/interface，不输出伪造 tester acoustic boundary。
- RTC、S2S、Hybrid、React 重写、数据库/集群、Windows Native 全量迁移、3D-Speaker、专业 SPL/loopback Station 不进入近期基础切片关键路径。
- TEN VAD 正式商业/分发接入前需要完成其附加许可证条件审查；许可证未确认不能视为依赖验收完成。