# Docker / API — 当前使用说明与远端部署边界

代码实现基线仍为 `v0.4.0`。产品要求见 [PRD](PRD.md)，主链配置见 [Recording Backbone](23-recording-backbone.md)。2026-09-16 已确定正式目标部署为 **Linux Server + Docker Backend + Remote Chrome Browser Station**；当前发布包的 localhost 用法仍是已有实现，不代表长期架构必须把 Docker 跑在 Windows 本机。

## 当前版本与既有启动方式

[v0.4.0](https://github.com/lybym/AIVoiceBench/releases/tag/v0.4.0) 是当前正式发布。既有 Windows 辅助启动脚本可以在本机启动 Docker 并访问 `http://127.0.0.1:8000`；这是当前发布的便利入口，不是 Windows Native 产品方向。

Docker 自带 FFmpeg/FFprobe，无需从 Windows 挂载可执行文件；不交付 EXE。

## 正式目标部署

```text
Remote Chrome Browser Station
        │ HTTPS / WSS
        ▼
Linux Server
└─ Docker: AIVoiceBench Backend/Web
```

Browser Station：

- 获取 microphone permission；
- 播放 stimulus；
- `getUserMedia` + `AudioWorklet`；
- local sample counter / frame sequence；
- TEN VAD target / RMS fallback；
- Waveform/Evidence UI。

Linux Server：

- API / WebSocket；
- Run orchestration；
- File/Streaming ASR、LLM、TTS；
- durable Artifact/Evidence；
- Silero finalized acoustic analysis；
- Attribution/Fusion/Timeline/Metrics/Report。

正式远端访问需要 TLS/secure context，并应通过反向代理或等效机制提供认证、HTTPS/WSS、连接超时和上传限制。当前 v0.4.0 的 localhost 单用户安全假设不能直接扩展成公网部署安全结论。

## 外置 Provider / Storage 配置

2026-09-18 的目标部署把所有模型/语音 Provider 与对象存储的非敏感参数移出镜像和应用代码：

```text
host
├─ /etc/aivoicebench/providers.yaml
└─ /etc/aivoicebench/storage.yaml
          │
          └─ read-only mount
                 ↓
          Linux Docker Backend
```

仓库只提供 `config/providers.example.yaml` 和 `config/storage.example.yaml`。运行时可用
`AIVOICEBENCH_PROVIDERS_CONFIG` / `AIVOICEBENCH_STORAGE_CONFIG` 指定文件位置，但这两个
环境变量只保存**路径**；endpoint/model/resource/voice/route、TOS endpoint/region/bucket/prefix/TTL
等实际配置应写在外置文件。长期 API Key / AK / SK 仍通过文件中的 env reference 在后端解析，
不得写进 YAML。

当前 `main` 的 SQLite model-settings 与 `AIVOICEBENCH_AUDIO_PUT_URL/GET_URL/HOST` 仍是
代码事实；[Issue #87](https://github.com/lybym/AIVoiceBench/issues/87) 负责迁移。文档中的外置
配置不能在 #87 合并前被宣称为已实现。

## 接口与边界

| 方法 | 路径 | 当前用途 |
| --- | --- | --- |
| GET | `/health`、`/openapi.json`、`/` | 版本/接口信息与 Web |
| POST | `/api/analyze` | multipart 三格式导入，与 CLI 共用 ImportRun；设备资料可选 |
| GET | `/api/runs`、`/api/runs/{run_id}` | 历史/详情与分析产物 |
| GET | `/api/runs/{run_id}/audio` | 标准化音频回放 |
| POST | `/api/runs/{run_id}/resume` | 显式 retry ASR；不是通用重分析接口 |
| GET | `/api/runs/{run_id}/role-review` | 人工说话人角色复核面：匿名聚类、代表性区间、转写片段、试听范围、已保存 revision 与 diff |
| GET | `/api/runs/{run_id}/evidence-workbench` | Evidence Workbench 投影：region/track、Gate 状态、指标与 Findings、不可用/弃权阶段、`evidence_integrity`、provenance；坐标全部来自持久化证据（同 `AnalysisResponse.workbench`）。**三态契约**：Run 目录缺 manifest、缺 `analysis_id` 或没有任何可读证据 → **404**（“该记录没有可复核的证据工作台”）；证据存在但投影失败（例如投影层自身出错）→ **500**（“证据工作台投影失败：该 Run 的证据存在但无法投影”），绝不折叠成 404；投影成功 → **200**，并把该 revision 的全部缺口放在 `unavailable`（`not_run`/`incomplete`/`failed`/`unreadable`/`invalid`）与 `evidence_integrity`（`unreadable_documents`、`skipped_records`）中 |
| POST | `/api/runs/{run_id}/role-review` | 保存 `{mapping, reviewer, reason}`：要求每个聚类都有明确决定（`unknown` 有效），创建不可变 revision 并重跑 Attribution 及下游；不调用 Provider |
| GET / POST | `/api/models` | 脱敏模型配置；长期凭据留在 server |
| GET | `/api/voice-test/capabilities/{mode}` | 能力预检；不默认发起付费探测 |
| POST | `/api/voice-test/sessions/{id}/start` | 启动会话；能力不足时明确拒绝 |
| POST | `/api/voice-test/sessions/{id}/device-audio` | 显式 turn-file fallback |
| WebSocket（existing control path） | session control/audio paths | Fixed/Free Control Plane；具体路径以当前 OpenAPI/代码为准 |
| WebSocket（planned） | `/api/voice-test/sessions/{id}/measurement-audio` | 跨整次 Run 的持续 PCM Measurement Capture |
| GET（planned） | `/api/voice-test/sessions/{id}/measurement-audio` / metadata | Measurement Audio Artifact 与 contract metadata |

Active Measurement API 在当前实现基线仍为 planned。现有 Free Streaming ASR 音频 WebSocket 不能被误报为跨整次 Run 的 F025 Measurement Audio。

## Remote Browser Station transport rule

浏览器发送的 Measurement PCM frame 必须携带现场 sequence/sample-time 信息。服务器接收时间只用于 transport diagnostics：

```text
Browser sample index  → formal audio_relative_ms
WebSocket receive time → network/jitter diagnostics only
```

因此即使 Browser 与 Linux Server 跨 LAN/WAN，正式指标也不能直接使用“server 收到上一包/下一包的时间差”。网络断连、gap、duplicate、stale frame 必须被显式保存到 capture-integrity metadata。

## Browser media settings

Remote Station 必须记录 requested 与 actual media settings，包括：

```text
echoCancellation
noiseSuppression
autoGainControl
sampleRate
channelCount
device metadata
browser / OS
```

对 AEC/NS/AGC 的 request 不代表浏览器实际遵守；最终应读取可获得的 `MediaTrackSettings` 并进入 Run provenance。

## Cloud provider, File ASR transport and speaker separation

云 Provider 需要外置服务路由与后端凭据引用。Recording Analysis 当前优先验证火山 File ASR 原生 speaker separation；ASR-native labels 仍是匿名 speaker clusters，必须进入 Attribution 才能得到 tester/device/unknown。

File ASR 默认使用豆包 Seed ASR 2.0 `volc.seedasr.auc` 异步 submit/query；极速版 HTTP 仅为显式兼容模式。音频 transport 支持：

```text
auto
├─ small canonical WAV → Base64 audio.data
└─ large canonical WAV → private TOS → short-lived Presigned GET → audio.url
```

因此对象存储是大文件 URL transport 的可选基础设施，不是所有 File ASR 的启动前置，也不参与 Streaming ASR。Backend 已持有文件时直接使用 Storage Adapter 上传，不再要求人为预先生成固定 PUT URL。私有对象完成识别后应删除，并用 bucket lifecycle 作为兜底清理。

Seed standard 是异步 submit/query：服务端必须在首次 submit 后持久化 request ID、query audit 与 stage 状态。HTTP 客户端断开或容器/worker 恢复时，先 query 既有 job；不得把重连实现成未提示的第二次计费 submit。当前修复验收见 [#93](https://github.com/lybym/AIVoiceBench/issues/93)。

当前阶段不要求 3D-Speaker。详见 [Recording Backbone](23-recording-backbone.md)、[模型/配置管理](16-model-management.md) 与 [组件策略](26-remote-browser-component-strategy.md)。

## Evidence UI

Web Evidence Workbench 目标使用 wavesurfer.js。音频 Artifact 仍由 Server API 提供，浏览器 Region 坐标来自 Evidence/EventTimeline 的 `audio_relative_ms`，不在前端重新生成 Measurement truth。

## Security and deployment caveats

- 浏览器不持有长期 ASR/LLM/TTS secret；
- 远端部署使用 HTTPS/WSS；
- 不把当前 localhost 明文模型配置假设直接搬到公网；
- Provider key、对象存储 AK/SK、完整 Presigned URL 不写入 Issues、报告、Run snapshot、普通日志、URL query 或前端 bundle；
- reverse proxy、authentication、rate/size limits 与 server hardening 属于远端正式部署的验收项。

## Validation boundary

软件/容器验证与远端部署验收必须分开记录。正式 Remote Browser acceptance 至少验证：

- Linux Server Docker build/start/restart/persistent volume；
- Chrome secure-context microphone permission；
- local sample clock/sequence 连续性；
- WAN/LAN jitter、断连与 gap 行为；
- actual media settings capture；
- upload/history/evidence playback；
- 真实 Recording Analysis 与实体 AI Device 的端到端运行。

当前代码/发布事实不自动证明上述远端场景已通过。

## Recording Analysis 读取面与容器重启验证（#27）

Recording Analysis 的 Run/revision/status 语义只有一个来源：`aivoicebench/run_view.py` 的持久化投影。API 与 CLI 都是它的外壳，因此同一份 Run 目录在任何读取面都得到同一份结论：

| 读取面 | 用途 |
| --- | --- |
| `GET /api/runs` | Run 列表（`run_id` / `status` / 当前 `analysis_id` / device） |
| `GET /api/runs/{run_id}` | 当前 revision 的完整视图：阶段账本、role Gate、证据链接（`workbench`）、指标/Findings、`report_md` |
| `python -m aivoicebench runs [RUN_ID] [--output DIR] [--json]` | 同一文档的 CLI 读取面；`--json` 输出与 API 响应体逐字段相同，便于脚本比对两个面 |

CLI 退出码与 `import` 一致（`1` = Run 不可读或存在 `failed` 阶段，`2` = 可读但仍有未完成阶段，`0` = 全部阶段 `complete`）。CLI 与 API 都只读持久化文档，不重新计算指标、事件或角色。

容器验证由 `.github/workflows/backbone-smoke.yml` 承担：镜像内先跑 `scripts/docker_smoke.py`（三格式导入、历史/详情、音频回放），再 `--save-history`（通过真实 Web API 保存一组人工角色决定，生成第二个 AnalysisRevision），随后 `docker restart` 并由 `--verify-history` 断言重启后重建的**当前 AnalysisRevision、完整阶段账本、role Gate（`complete_review` 及其 revision 身份）、证据链接（workbench 与 invocation artifact 可达性）与报告**与重启前逐字段一致，且每个 artifact 重新哈希通过。该 job 只证明容器/持久化行为，不构成真实 provider 调用或真实录音验收（[#85](https://github.com/lybym/AIVoiceBench/issues/85)）。
