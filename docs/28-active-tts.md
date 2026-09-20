# Active TTS — 火山 V3 WebSocket 传输契约与刺激资产

PRD refs: PRD-F015 / F016 / F020 / F021；相关 M2、M3、N003。技术契约文档，不是额外需求。

本文定义 **Active Voice Test** 使用的 TTS 传输与资产边界。Free 模式的实时识别边界见
[Streaming ASR](24-streaming-asr.md)；Active Measurement 的整体平面划分见
[Active Measurement](25-active-measurement.md)。

## 1. 为什么拆成两条 route

一次 Run 的 TTS 需求有两种互不相同的生命周期，把它们合并在一个
`volcengine_tts` profile 里会迫使运行时去猜协议：

| | `tts`（Fixed 资产合成） | `streaming_tts`（Free 流式会话） |
| --- | --- | --- |
| 面向 | Fixed Case Runner | Free / Exploratory Test Agent |
| 输入 | 完整固定话术，一次提交 | LLM 流式输出的有序可朗读 chunk，随时追加 |
| 输出 | 完整 MP3，落盘冻结 | MP3 chunk 流，边到边播 |
| 生命周期 | `StartSession → TaskRequest → FinishSession → 收齐音频 → 关闭` | `StartSession → TaskRequest* → (audio chunks) → FinishSession / CancelSession` |
| 协议 | V3 **单向** WebSocket | V3 **双向** WebSocket |
| 结算单位 | 冻结的 Stimulus Artifact（SHA-256 + sample metadata） | 与一个 Run/Turn 绑定的 session |

`providers.yaml` 因此暴露两条独立 route：`tts` 与 `streaming_tts`。缺失
`streaming_tts` route 时 Free 的流式合成能力报告为未配置，**不会**回退复用资产合成
provider。

## 2. 媒体格式固定为 MP3

Active TTS 的媒体格式固定为 **MP3**，不是配置项（Issue #98 产品决定，覆盖此前 WAV 目标表述）：

- `providers.yaml` **不提供** `format` / `encoding` 键；出现 `format:` 会以显式迁移错误拒绝，
  而不是被静默忽略（避免操作者误以为 WAV/PCM 设置仍然生效）。
- Fixed 直接校验 Provider 返回的 MP3 并冻结为 MP3 Stimulus Artifact，**不产生 PCM/WAV
  封装或转码副本**。
- Free 的 TTS chunk 同样是 MP3。
- 本决定只针对 **Active TTS 输出/刺激**。Recording Analysis 的 canonical audio 与 Active
  Measurement 的 durable Measurement Audio 仍可使用 PCM/WAV；它们不是 TTS 播放资产。

## 3. Fixed：冻结刺激资产

```text
complete fixed text
→ backend `tts` route
→ V3 unidirectional WebSocket
→ MP3 audio chunks
→ MP3 frame 校验 + sample metadata
→ immutable MP3 Stimulus Artifact
→ formal Browser playback
```

- 每个 synthesis request 使用唯一 request identity；正式 Run 引用已冻结资产的
  SHA-256、sample rate/channels/bitrate/frame count 与 non-secret provider/config snapshot。
- 重新执行同一 Case **不得**因为启动 Run 而静默重新合成或替换该资产。
- 音频不是"看起来像 MP3"就被接受：解析器逐帧扫描，拒绝非 MP3、中途采样率/声道变化、
  空帧，并在至少一个有效帧之后于首个非帧字节处停止计数。校验失败即失败，不写入资产。
- **冻结产物的命名与交付 Content-Type 必须与容器一致**。`voice_test.py` 依据 provider 返回的
  `format` 决定扩展名（MP3 → `.mp3`，遗留 SSE 迁移期可仍为 `.wav`），
  `GET /api/voice-test/sessions/{id}/audio/{i}` 依据实际扩展名返回 `audio/mpeg` / `audio/wav`。
  之前固定写 `.wav` 并以 `audio/wav` 交付 MP3 字节会让 artifact 的 provenance 与真实容器矛盾，
  也会让按 Content-Type 选择解码器的浏览器拿到错误格式。
- Provider timing（延迟、chunk 到达时间）是诊断，不是正式 acoustic truth。

## 4. Free：双向流式会话

```text
Streaming ASR final Observation
→ Streaming LLM
→ ordered speakable text chunks
→ backend `streaming_tts` route
→ V3 bidirectional WebSocket
→ MP3 audio chunks
→ (target) Browser streaming playback
```

- 不等待完整 LLM response 才开始 TTS：`append_text()` 可随时追加。
- 文本 chunk 顺序**原样保持**：`split_speakable_chunks()` 只在句末标点或硬上限处切分，
  从不重排、去重或丢弃。
- TTS session 与 Active Turn 绑定（`context.session_id / turn_id / run_index`）。
- **Stale ownership 由传输层强制执行**：**调用方调用 `cancel()` 之后**（用户 Stop、turn 改变、
  run stop、provider failure、未来 Barge-in 各自触发一次 `cancel()`），所有后续音频帧被丢弃并计为
  `tts_stale_audio`，**永不写入该 turn 的音频字节**。这是阻止迟到音频串入下一轮的机制。
  需要明确的是：**传输层不自行检测 turn 是否过期**——「turn 改变 / run stop / barge-in 应当触发
  `cancel()`」的上层编排属于尚未实现的 Free 接线（§8），`stale_turn_reason()` 目前也还没有生产调用方。
- 取消后的 session 拒绝新的 `append_text`；session 一旦进入终态（finished/cancelled/failed），
  再次 `close()`/`cancel()` 不会改写已记录的证据，也不会复活音频。
- 被取消或失败的 session 音频落盘为 `{stream_id}.partial.mp3`，与完成态的 `{stream_id}.mp3`
  可区分；`_write_audio_file()` 写失败时结果降级为 `failed` 并在 events 中记录，不会把只存在于
  内存的字节上报为完成的合成。
- 流式链路失败**不得**静默切回旧 SSE 或单向 WS。Run snapshot 的 `streaming_tts_fallback_policy`
  与 session `summary()['fallback_policy']`（`forbidden_transports` 由适配器模块声明）真正把该决定
  落盘，而不是靠「没有配置旧 profile」隐含表达；若未来需要 fallback，必须由产品显式定义、
  UI 标注并写入 Run。
- **失败分类只有一份实现。** `streaming_tts.py` 拥有类别词表（`TTS_ERROR_CATEGORIES`）、
  `(category)` 标记格式（`failure_category_of`）与类别→审计码映射（`FAILURE_CODE_BY_CATEGORY` /
  `failure_code_for`）；适配器只消费它，不保留第二份映射。因此**供应商无关的边界模块不 import
  任何 Provider 模块**，新增 Provider 时也不需要反向修改它。
- LLM partial、TTS audio chunk、browser playback callback 均为 Control/Provider diagnostics，
  不构成正式 Measurement acoustic boundary。

## 5. 配置与能力校验

可配置项只保留当前官方协议支持且确有需要调整的：

- `resource_id`（必填）、`voice`（必填）；
- `sample_rate`；`speed`（→ `speech_rate`）；`volume`（→ `loudness_rate`）；
- `timeout_seconds`；`model`（审计标签）。

按 **protocol capability** 校验，不支持即显式失败：

- `pitch` 仅在协议真正支持时接受。2026-09-18 官方单向 V3 页面仍标注音高调节暂不支持，
  因此单向 profile 声明 `pitch` 直接拒绝；声明式接受但运行时忽略等于宣传协议没有的能力。
- endpoint 必须与所声明的 protocol 一致：单向 protocol 只能指向
  `/api/v3/tts/unidirectional/stream`，双向只能指向 `/api/v3/tts/bidirection`；
  旧 SSE endpoint 对任一 WS protocol 一律拒绝。
- capability 与 protocol 必须匹配（`volcengine_tts_ws` → `tts`；
  `volcengine_tts_ws_bidirectional` → `streaming_tts`）。

`CAPABILITY_MATRIX` 是这份能力的单一来源：

| transport | complete_text | streaming_text | streaming_audio | pitch | loudness |
| --- | --- | --- | --- | --- | --- |
| `volcengine_tts_ws_unidirectional` | ✓ | — | ✓ | — | ✓ |
| `volcengine_tts_ws_bidirectional` | ✓ | ✓ | ✓ | ✓ | ✓ |

若官方后续改变 pitch 支持状态，validator、capability matrix 与本文必须同步更新。

## 6. 鉴权与凭据边界

| 控制台 | 请求头 |
| --- | --- |
| 新版（本适配器采用） | `X-Api-Key`、`X-Api-Resource-Id`、`X-Api-Request-Id`、`X-Api-Connect-Id` |
| 旧版 | 需要 APP ID **与** Access Token 两个凭据槽位，本适配器**未实现也不猜测** |

凭据只在 Backend 的 WebSocket 握手使用，读自 server-owned secret/env。Browser 不直连
TTS Provider、不持有 TTS credential。凭据不进入 audit record、execution trace、API 响应或报告；
Provider 错误文本截断至 200 字符。

## 7. 二进制信封（V3 event envelope）

4 字节 header，整数**大端**：

```text
header               : [version<<4|header_size][msg_type<<4|flags][serialization<<4|compression][reserved]
client request       : [header][event i32][sid_len i32][sid][payload_len i32][payload]
server: conn/session : [header][event i32][sid_len i32][sid]（SessionFailed 可再带 [err_len][err]）
server: tts response : [header][event i32][sid_len i32][sid][payload_len i32][payload]
audio only response  : [header][event i32][sid_len i32][sid][payload_len i32][audio]
error from server    : [header][event i32][error_code i32][payload_len i32][error UTF-8]
```

- `message_type`：`0b0001` full client request、`0b1001` full server response、
  `0b1011` audio-only response、`0b1111` error。
- `flags`：`0b0100` with-event（V3 TTS 是事件驱动，不是序号驱动）。
- 序列化 `0b0001` JSON；压缩 `0b0000` none。
- 事件号：连接 `1/2/50/51/52`；会话 `100/101/102/150/151/152/153`；
  任务与音频 `200/350/351/352`。
- 请求体 `namespace` 固定为 `BidirectionalTTS`；只有
  `VERIFIED_REQUEST_FIELDS` 白名单字段会被发送，测试直接断言实际写出的字节。

## 8. 未验证（不得声称已通过）

- **真实云调用**：本仓库实现未做任何真实 `/api/v3/tts/*` 调用；未计费。
  `interface_contract.real_cloud_call` 记录为 `not_attempted`。
- **真实云参数**：`sample_rate` / `speech_rate` / `loudness_rate` 的实际音色支持矩阵未在真实
  服务上观察；服务端仍是最终权威。
- **单向请求时序假设**：`_UnidirectionalSession` 连续发送 `StartSession(100) → TaskRequest(200)
  → FinishSession(102)`，**不等待 `SessionStarted(150)`**。这是基于 V3 事件信封与仓库既有
  流式识别实践的工程推断，**未在真实服务上验证**；若官方要求等待 `150` 再发 `TaskRequest`，
  首次真实云验收会暴露该问题。因此该时序必须计入 Issue #98 AC #10 / #85 的真实云验收项。
- **audio-only 帧的"with-event + session 字段"假设**：解析器按
  `[header][event][sid_len][sid][payload_len][audio]` 处理，并由与实现无关的 golden hex fixture
  固定（`tests/test_tts_v3_websocket.py::GoldenFrameTests`）；该布局仍未经真实服务确认。
- **浏览器端流式播放**：Free 的 MP3 streaming playback、TTS audio channel/event contract 与
  Browser 侧 stale-turn 丢弃尚未实现（provider/session 层已实现并验证）。
- **Free 上层编排**：turn 改变 / run stop / barge-in 触发 `cancel()` 的调用方尚未接线，
  `stale_turn_reason()` 目前无生产调用方。
- **Fixed 审计 sidecar 的不可变性**：`{stem}-tts-audit.json` 由 `_write_audit` 覆盖写入，
  不经过 `schemas/provider-invocation.schema.json` 校验，也不具备 `InvocationAudit`
  的一次尝试一目录语义（该模式继承自旧 SSE 适配器）。它是**独立的 TTS sidecar 审计**，
  不是 `provider-invocation` 契约的一部分。
- **实体设备 / 真实扬声器 / 真实麦克风**验收未进行。
- 这些结论只覆盖软件与受控输入验证（`software_verified`）。真实录音、人工复核与浏览器回放由
  [#85](https://github.com/lybym/AIVoiceBench/issues/85) 承担。

## 9. 参考页面（火山引擎文档中心，2026-09-18 在线核对）

- [WebSocket 单向流式-V3](https://docs.volcengine.com/docs/DoubaoVoice/WebSocketUnidirectionalStreaming-V3?lang=zh)
- [WebSocket 双向流式-V3](https://docs.volcengine.com/docs/DoubaoVoice/WebSocketBidirectionalStreaming-V3?lang=zh)
- 稳定别名（Issue #98 引用形式）：
  [单向](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-websocket?lang=zh)、
  [双向](https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket?lang=zh)
- 错误码查询：`/docs/DoubaoVoice/error-code-query-1?lang=zh`
