# M1 — Real Recording Backbone

PRD refs: PRD-F004–F009/F014/F016/F017；Issues #22/#24/#25/#27/#30。
实现基线仍为 `v0.4.0@9632844`。2026-09-16 只更新 Recording Analysis 的技术路线：Silero VAD 作为 server-side acoustic boundary 候选、火山 ASR 原生 speaker separation 作为当前 speaker clustering 主路径、wavesurfer.js 作为 Evidence UI；不因文档更新声明真实录音验收完成。

Recording Analysis 是独立正式 Measurement Pipeline over `ART-external-recording`。它与 Active Measurement 共享 Canonical Event semantics 和 metric engine，但绝不消费 Active pipeline 的 Live Measurement Audio 作为资格步骤，也不“提升” execution control trace。External recordings 用于独立复测、深度离线分析、审计、人工复核、回归和 Measurement Equivalence。

## One Run

Web upload 和 CLI `import` 使用 `import_recording()` / `ImportRun.execute()`。

目标主链：

```text
Ingestion
→ normalization / QA
→ Acoustic Boundary (Energy legacy / Silero target)
→ Volcengine File ASR
     ├─ transcript / timestamps
     └─ anonymous speaker labels
→ Attribution
→ Fusion
→ Turn / EventTimeline
→ Canonical Metrics
→ Judge / Findings / Review / Report
```

已有代码能执行 acoustic → ASR-native diarization → attribution → fusion；只有存在角色证据时才继续 turns/timeline/metrics，否则弃权。Judge/Findings 尚未完整接入 ImportRun。历史 `web-analysis` 记录保持可读且不重写。

`ModelSettings.capture()` 是当前实现中的 model/provider snapshot 入口；ASR 与 ASR-native diarization 已可进入 ImportRun，Judge 配置可捕获但主链未完整执行。2026-09-18 目标架构改为从外置 `providers.yaml` / `storage.yaml` 解析有效配置，并继续生成 secret-free Run snapshot。Credential values 与完整 Presigned URL 始终保持运行时内存态，不写快照。#87 合并前，SQLite model settings 仍是当前代码事实。

## 2026-09-18 File ASR 与对象存储决策

Recording Analysis 的 P0 File ASR 继续采用火山**录音文件识别极速版 HTTP**，不切换到 Streaming ASR。

三种火山录音文件识别形态的产品定位：

- **极速版 HTTP**：当前 AIVoiceBench 默认；单次请求返回，适合交互式 Recording Analysis。
- **标准版**：保留为未来高质量/长录音 Provider mode；异步 submit/query 生命周期，不进入 #87 当前实现。
- **闲时版**：保留为未来大规模历史回归/批处理 Provider mode；不适合当前“上传后立即看报告”的主流程。

极速版目标 transport：

```text
canonical WAV
    |
    +-- <= inline_max_bytes --> Base64 audio.data
    |
    +-- >  inline_max_bytes --> private TOS object
                               --> short-lived Presigned GET URL
                               --> audio.url
```

初始 `inline_max_bytes` 为 **15 MiB**，作为可配置工程默认值而不是硬编码产品常量。对象存储只在 URL transport 需要时参与；小文件不应因为缺少 TOS 而被阻断。

TOS 是第一对象存储 adapter，原因是与当前火山服务栈部署一致、支持私有对象和预签名 URL。它不是 File ASR 协议的强制绑定。Storage Adapter 必须保持可替换，且对象存储只承担临时 transport，不成为 Evidence source。

目标运行配置分离：

```text
/etc/aivoicebench/providers.yaml
  └─ File ASR / Streaming ASR / TTS / Judge profiles + routes

/etc/aivoicebench/storage.yaml
  └─ TOS endpoint/region/bucket/prefix + credential refs + TTL/cleanup
```

长期 secret 不进入 YAML；只保存环境变量/secret reference。仓库样例见 `config/providers.example.yaml` 与 `config/storage.example.yaml`。

官方契约核对入口（实现时仍须再次在线验证）：

- 录音文件识别极速版 HTTP：火山文档 ID `6561/1631584`；
- 录音文件识别标准版：`6561/1354868`；
- 录音文件识别闲时版：`6561/1840838`；
- TOS Presigned URL：对象存储预签名机制文档。

实现任务见 [Issue #87](https://github.com/lybym/AIVoiceBench/issues/87)。

## 2026-09-16 speaker separation 决策

当前阶段 **不接 3D-Speaker**。先把火山 File ASR 的自动说话人分离能力验证并使用完整。

火山当前产品能力说明已列出“自动说话人分离（中英文）”。AIVoiceBench 的实现要求是：

1. 根据当前实际使用的 File ASR API 文档核对启用参数，不沿用旧接口字段猜测；
2. 保存原始 provider response；
3. 将 provider speaker label 规范化为匿名 `speaker_0 / speaker_1 / ...`；
4. speaker label 只作为 diarization evidence，不直接变成 tester/device；
5. Attribution 层继续结合显式 mapping、语义提议、时序上下文和人工复核；
6. labels 缺失、冲突或质量不足时保持 `unknown` / `needs_review` / `insufficient_evidence`；
7. 真实 AI 玩具录音上的 coverage/accuracy 达不到要求后，才进入独立 diarization Provider（例如 3D-Speaker/pyannote）评估。

参考：火山语音识别产品说明 <https://www.volcengine.com/docs/6561/1354871?lang=zh>。

> 旧实现说明中“读取返回 label 但不发送未核对 speaker-separation flag”的安全边界仍有效：在当前实际接口字段核对和测试完成前，不得凭旧 API 的 `with_speaker_info` 等字段直接修改新接口请求。

## Acoustic boundary 路线

现有 `EnergyVadSegmenter` 继续保留用于 fixture、debug 和兼容；Recording Analysis 的下一模型型 Provider 是 Silero VAD。

```text
External Recording
 ├─ Silero VAD ───────→ acoustic start/end candidates
 └─ Volcengine ASR ───→ text/timestamp/speaker labels
                    ↓
                  Fusion
```

VAD 不作为 ASR 的强制前置。两者并行产生 evidence；Silero threshold/post-processing 由版本化 Measurement Policy 管理，不能把第三方默认值直接当正式产品门槛。

## Current Volcengine File ASR contract boundary

当前代码实现过的极速 File ASR path 使用配置化 endpoint/model/resource 与后端凭据，但仍通过固定 URL/发布机制提交 canonical audio。旧审计曾核对 `volc.bigasr.auc_turbo` 极速接口以及 utterance/word timing；具体 endpoint/resource/请求字段属于 dated adapter contract，不是永久产品要求。

#87 的目标是保留同一极速版 Provider 能力，同时把输入 transport 改成 provider 原生的 `audio.data` / `audio.url` 双路径，并把 endpoint/model/resource/transport policy 全部移入外置 provider 配置。固定 `AIVOICEBENCH_AUDIO_PUT_URL` / `AIVOICEBENCH_AUDIO_GET_URL` / `AIVOICEBENCH_AUDIO_HOST` 不再是目标生产依赖。

2026-09-16 起，speaker separation 接入必须以**当前启用的火山接口文档和真实服务响应**为准，不能仅凭旧文档/旧接口兼容字段推断。服务模型版本、speaker label 语义和准确率都要进入真实验收记录。

## Configure locally / on Linux Server

目标部署：

1. 将 `config/providers.example.yaml` 复制为服务器外置 `/etc/aivoicebench/providers.yaml`，配置 File ASR endpoint/model/resource、`audio_transport`、`inline_max_bytes`、route 与 credential env reference。
2. 仅当 `object_storage` / `auto` 的当前文件需要 URL transport 时，加载 `/etc/aivoicebench/storage.yaml`。TOS bucket 保持 private，Backend 直接上传并生成短期 Presigned GET。
3. 长期 Provider key / TOS AK/SK 只存在于后端 secret/environment；配置文件、Issue、报告、Run snapshot 与日志不保存 secret 或完整签名 URL。
4. speaker separation 与 ASR 尽量复用同一次原生响应，不为同一录音做无必要的第二次云识别。
5. `diarization` route 指向 ASR-native labels 时，缺少 labels 应返回 `insufficient_evidence`，不静默造 cluster。
6. Vosk 保留显式 offline fallback，但不冒充火山调用失败后的自动替代。

在 #87 合并前，当前 Web model management / SQLite 与固定 Signed URL publisher 仍可能是实际运行入口；不得把上述目标配置方式误写成当前版本已实现。

CLI 仍走相同 ImportRun：

```text
python -m aivoicebench import conversation.m4a --model-settings artifacts/.model-settings
```

## Evidence, contracts and retry

- `original/` 保留输入 bytes；`analysis/ANALYSIS-*/` 保存 normalization、native ASR、Transcript、acoustic/speaker/attribution/fusion/turn/timeline/metric envelopes、model snapshot、status 和 reports。
- `provider-calls/CALL-*/start.json` 在外部调用前落盘；result 记录 duration/status/safe failure/hash references。Credential 回显必须被过滤。
- Transcript contract 保留 provider-estimated timing、nullable confidence/model hash、speaker labels 等信息；numeric speaker IDs 不是 tester/device roles。
- explicit retry 生成新的 AnalysisRevision，不覆盖旧 manifest/output；completed operation 不重复计费调用。
- 文件存在不能证明 processor 已执行；stage state 必须显式。

## Ingestion gate（Issue #21 范围）

Ingestion gate 只负责让每个被拒绝的输入都留下可复核状态，不改变下游语义：

- 支持的扩展名（WAV/MP3/M4A）之外的输入、缺失文件、超过 1 GiB 的输入：`ingestion` stage `failed`，Run 仍生成 manifest 与 report，`original_sha256` 保持 null，不注册 `original_recording`。
- 损坏、截断、空音频、无法映射的声道布局（例如多声道 WAV）：原始文件按字节保留，`normalization` stage `failed`，reason 与本地 diagnostic 一并落盘。
- 三种格式的真实 codec 执行测试仍以 `AIVOICEBENCH_REQUIRE_MEDIA_TESTS=1` 在 CI 强制运行。

Audio QA 只报告 canonical artifact 上的测量事实与有效性条件（`audio-qa` envelope，必要时另有 `audio-qa-conditions` 文档）：

- `decodable_canonical_audio`：decode 成功即 `met`；声明“decode 成功不代表含语音”。
- `nonempty_signal`：未配置能量门槛时为 `unassessed`；canonical 波形零能量时为 `insufficient`；非零但不可闻仍为 `unassessed`（零能量边界是有意为之，不发明门槛）。
- 出现 `insufficient` 时 envelope 为 `insufficient_evidence` 且 `data: null`，测量值改以独立注册文档保留（该文档同时进入 `stages.audio_qa.output_artifact_ids`）；不允许把沉默录音呈现为可用录音，也不允许发明 pass/fail 门槛。
- 完成态 envelope 的 `data_artifact_ref` 指向承载 `data` 的 `audio-metadata.json`，不是 WAV；`normalized_audio` 仅作为 stage 输入与父引用保留。
- QA 不声明识别质量、ASR 准确率或测量准确率。
- `GET /api/runs/{run_id}.audio_qa` 跨 AnalysisRevision 解析：canonical QA 每 Run 只测一次，`resume` 不重发 envelope，故视图先读当前 revision，缺失时回退 manifest 中最新注册的 `audio-qa`；旧 Run 无 `conditions` 时返回显式 `conditions_version: unassessed`。

## Evidence Workbench

Recording Analysis 的 Web 证据审阅采用 wavesurfer.js，而不是继续自行堆 waveform renderer。

首期要求：

```text
Waveform
+ Timeline
+ Regions(acoustic / speaker / event / finding)
+ transcript / role / provenance side panel
```

Finding、Metric、Event 点击后应定位并高亮相应 Evidence Region。wavesurfer.js 只显示 `audio_relative_ms` 坐标，不生成新的 Event 或 Metric。

## Verification boundaries

Targeted tests 可以使用 synthetic audio / injected provider responses 验证 codec、source preservation、native response normalization、speaker label mapping、time bounds、secret-safe failures、retry、revision 和 lock semantics；它们不能证明中文识别准确率或 speaker separation 真实质量。

M1 真实验收至少需要：

- 授权的 5–20 分钟 AI 终端真实录音；
- 真实火山 File ASR 调用；
- speaker separation 实际返回及人工核对；
- Silero/legacy acoustic boundary 与人工标注比较；
- Attribution unknown/conflict/人工修订闭环；
- wavesurfer Evidence UI 逐证据复核。

没有上述证据前，speaker clustering、角色归属、声学边界和完整 M1 均不能写成 real_recording_verified。

## Stage states and evidence limits

默认 Web/CLI 没有角色真值。Provider-estimated cluster boundaries 保留来源；ambiguous overlap 不强制分配。语义角色提议可以辅助人工审阅，但不能覆盖显式 evidence。`write_import_report()` 仍只是 import-stage 状态报告；完整 semantic report/findings/human revision 仍属于 M1 收口工作。