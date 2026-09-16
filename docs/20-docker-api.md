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

## 接口与边界

| 方法 | 路径 | 当前用途 |
| --- | --- | --- |
| GET | `/health`、`/openapi.json`、`/` | 版本/接口信息与 Web |
| POST | `/api/analyze` | multipart 三格式导入，与 CLI 共用 ImportRun；设备资料可选 |
| GET | `/api/runs`、`/api/runs/{run_id}` | 历史/详情与分析产物 |
| GET | `/api/runs/{run_id}/audio` | 标准化音频回放 |
| POST | `/api/runs/{run_id}/resume` | 显式 retry ASR；不是通用重分析接口 |
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

## Cloud provider and speaker separation

云 ASR 需要服务路由、凭据和对应音频 transport 配置。Recording Analysis 当前优先验证火山 File ASR 原生 speaker separation；ASR-native labels 仍是匿名 speaker clusters，必须进入 Attribution 才能得到 tester/device/unknown。

当前阶段不要求 3D-Speaker。详见 [Recording Backbone](23-recording-backbone.md) 与 [组件策略](26-remote-browser-component-strategy.md)。

## Evidence UI

Web Evidence Workbench 目标使用 wavesurfer.js。音频 Artifact 仍由 Server API 提供，浏览器 Region 坐标来自 Evidence/EventTimeline 的 `audio_relative_ms`，不在前端重新生成 Measurement truth。

## Security and deployment caveats

- 浏览器不持有长期 ASR/LLM/TTS secret；
- 远端部署使用 HTTPS/WSS；
- 不把当前 localhost 明文模型配置假设直接搬到公网；
- 音频上传/发布 URL、对象存储凭据和 Provider key 不写入 Issues、报告、URL query 或前端 bundle；
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