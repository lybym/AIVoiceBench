# Streaming ASR — Active Voice Test 实时识别边界

PRD refs: PRD-F016 / F020 / F021 / F023；相关 N003、N004。技术契约文档，不是额外需求。

本文定义 **Active Voice Test** 使用的 Streaming ASR 边界。Recording Analysis 的 File ASR
契约见 [时间戳 ASR](11-timestamped-asr.md) 与 [录音主链](23-recording-backbone.md)。

## 1. 为什么分成两个 Provider 家族

| | File ASR | Streaming ASR |
| --- | --- | --- |
| 面向 | Recording Analysis | Active Voice Test |
| 输入 | 已完成的完整录音文件 | 浏览器麦克风的持续音频流 |
| 生命周期 | 一次提交、一次识别；可离线重跑 | `start_session → push_audio → events → finish_input → close / cancel` |
| 输出 | 完整 Transcript（utterances / timestamps / 说话人标签） | partial / final transcript + speech / endpoint 事件 |
| 主要角色 | Recording Analysis 的文字/语义证据；provider timestamp 非声学真值 | Active Control Plane 的实时文字/语义观察；可被正式结果引用为语义证据，但不提供正式声学边界 |
| 凭据/文件 transport | 后端持有；极速版可 inline Base64，只有大文件 URL 模式才使用对象存储 + 短期 Presigned GET | 后端持有；**不使用对象存储文件发布，浏览器不接触任何长期凭据** |

两者共享“provider / model / resource / 版本化调用审计 / 长期凭据不入配置文件且不入快照”的原则；Provider 非敏感参数由目标外置 `providers.yaml` 统一声明，
但生命周期不同，**不合并成一个只接受文件的接口**。`asr.py` 的 `ASRProvider`
（`FileASRProvider` 别名）继续是 File ASR 契约，语义不变。

## 2. 统一事件模型

`streaming_asr.py` 定义的供应商无关事件（`StreamingASREvent`）：

| kind | 含义 | 必需字段 |
| --- | --- | --- |
| `asr_session_started` | 会话已建立 | source, at |
| `speech_started` | 检测到开始说话 | source, at, basis |
| `partial_transcript` | 中间识别结果（会变化） | text, source, at, sequence |
| `final_transcript` | 该段确认结果 | text, source, at, sequence |
| `speech_ended` | 判停 / 端点 | source, at, basis |
| `asr_error` | 失败（一等状态） | error, at |
| `asr_session_closed` | 会话结束 | at, basis |

来源必须显式标注：`browser_vad`（浏览器 RMS VAD）、`volcengine_streaming_asr`（供应商事件）、
`combined`（两者一致或经合并判定）。供应商给出的时间戳与本地接收时间分别保存，**不互相覆盖**；
供应商估计的时间不是声学真值，也不升级为正式 speech boundary。Active Measurement 的正式边界优先来自独立持续保存的 Live Measurement Audio processor；ASR 文字可作为语义 Evidence 被引用。

冲突处理：VAD 与供应商判停不一致时保留双方事件并标记冲突，不做静默覆盖。

## 3. 音频与传输

```text
Browser Mic
  → getUserMedia
  → AudioContext（目标 16 kHz）+ AudioWorklet
  → mono / PCM16LE / 20 ms 帧
  → 独立 audio WebSocket（二进制帧）
  → Backend 会话缓冲
  → StreamingASRProvider（聚合为约 200 ms 包）
  → 火山流式接口
```

- **浏览器不发送音频给云服务**：云请求由后端发起。
- 采样率/位深/声道必须实际取得并在 trace 中记录；**拿不到目标格式时明确失败**，
  不静默送入错误格式的音频（`audio_capture_failed` / `invalid_audio`）。
- 传输帧：`[4B big-endian 序号][PCM16LE 字节]`，序号用于发现乱序、重复与迟到包。
- 控制面与音频面分开：JSON 控制事件继续走既有 `/ws`，二进制 PCM 走
  `/api/voice-test/sessions/{id}/audio`。混在同一连接会让 JSON 解析与二进制帧互相干扰。

上面的 `/audio` 是 Free 模式每轮 Streaming ASR transport，不是跨整次 Run 的正式 Measurement Audio Artifact。F025 另设 Measurement Plane：同一浏览器麦克风 PCM 可被并行送往持续 Measurement Capture，但持久化、sequence/sample clock、完整性、policy 和 Evidence 契约独立；共享输入不等于 ASR control event 自动升级。

## 4. 与 VAD / Measurement Acoustic Processor 的关系

Browser VAD 与 Streaming ASR **并行存在**，职责不同：

- Browser VAD：快速、低成本的 `speech suspected start / end / timeout`，用于轮次控制；
- Streaming ASR：设备说了什么（partial / final）、供应商判停（`definite`）。

**轮次开始不得依赖 final transcript**：VAD 先发现讲话，ASR 随后给文字。Fixed Mode 的推进
只依赖 VAD；Streaming ASR 是增强 Observation。Partial 只记录/展示/留痕，不触发 LLM。

两者都属于低延迟 Control Plane，不承担正式 acoustic onset/offset。Measurement Plane 使用版本化、streaming-compatible `AcousticBoundaryPolicy`，基于 sample index 形成 confidence/uncertainty 可追溯的 boundaries。现有 Browser RMS 阈值和 `EnergyVadSegmenter` 的 global noise/peak 算法均不能未经验证直接冒充该处理器。

### 4.1 判据是时域 RMS，阈值由噪声底抬高

VAD 使用 `AnalyserNode.getFloatTimeDomainData` 计算**线性 PCM RMS**（−1..1）。此前的实现
读取频域 bin 并除以 255：那不是振幅，绝对阈值因此没有意义——非零环境噪声可以让一轮永远
无法结束，安静房间也可能看起来像讲话。

- 每轮开始有 1200 ms 校准窗口，收集安静样本（RMS < 0.05），取 80 分位作为噪声底，
  下限为绝对底 `0.004`；起点阈值 = `max(0.012, 底 × 3)`，结束阈值 = `max(0.0072, 底 × 1.8)`。
- **校准不暂停检测**：设备可能立刻回答，静默窗口会丢掉讲话开始。
- 安静样本不足 8 个时**不虚构**噪声底，保留绝对底，并在 `vad_diagnostics` 中标注
  `baseline_basis: absolute_floor`。这些阈值是**控制判据，不是声学测量**。
- 一旦检测到疑似讲话，无回答超时不再适用；改为**轮次上限**（默认 90 s，可由会话
  `round_observation_max_ms` 收紧）。达到上限时上报
  `observation_timeout`（`reason: cannot_confirm_response_end`），关闭原因为
  `observation_end_unconfirmed`，**不记为回答完成、不伪造回答结束**。
- 停止、迟到事件与取消清理行为不变：本地停止即时生效，迟到 `ended` 不恢复监听或推进。

### 4.2 观察方式在启动前由后端强制判定

- `GET /api/voice-test/capabilities/{mode}` 返回该模式所需项、缺少项与浏览器需自行检查的
  能力；**默认不发起付费探测**（`connectivity: not_probed`），响应与日志不含凭据、服务地址
  或签名 URL。
- 同一检查在 `POST /api/voice-test/sessions/{id}/start` 与控制 socket 的 `start` 消息上
  **强制生效**：缺少项时拒绝启动、指名缺少项、不发起任何 LLM/TTS/ASR 调用。
- 自由模式的 `capture_mode=auto` **只解析为 Streaming ASR**；缺少 Streaming ASR 时明确拒绝，
  不静默切换。`turn_file`（整轮录音 + File ASR）只有操作者显式选择时使用，并在 UI、play
  消息与执行记录中标注为降级。Streaming ASR **不需要** File ASR 的对象存储或 Signed URL 音频发布；File ASR fallback 自身按 #87 的 `inline | object_storage | auto` policy 处理。
- 降级上传帧：浏览器上传实际录到的容器，后端先校验签名/大小，再用 FFmpeg 解码为
  canonical 16 kHz mono PCM16 WAV 并做 canonical QA，然后才调用 File ASR；文本取自归一化
  `segments`。非法媒体、识别失败、空结果分别记录为 `invalid_audio` / `asr_failed`，不包装成
  “成功的空 transcript”，也不推进下一轮。转写只能来自服务端捕获记录。

## 5. 火山引擎契约（依据官方文档核对）

以下为截至 2026-09-11 从火山引擎文档中心在线页面核对的结论（页面 ID 见下）。

### 接口与资源

| 项 | 值 |
| --- | --- |
| 双向流式端点 | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` |
| 双向流式（优化版） | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` |
| `model_name` | `bigmodel`（文档「目前只有 bigmodel」） |
| Resource ID | `volc.bigasr.sauc.duration` / `volc.bigasr.sauc.concurrent`（流式 1.0，小时版/并发版） |

小时版与并发版是**计费模式**差异，不是功能差异。文档标注 2.0 代 `volc.seedasr.sauc.*`
为推荐；本适配器不硬编码代际，resource ID 由配置提供。

### 鉴权

| 控制台 | 请求头 |
| --- | --- |
| 新版（本适配器采用） | `X-Api-Key`、`X-Api-Resource-Id`、`X-Api-Request-Id`、`X-Api-Sequence: -1` |
| 旧版 | `X-Api-App-Key`（APP ID）、`X-Api-Access-Key`（Access Token），另两项相同 |

文档原文："（旧版控制台使用，新版控制台只需要 X-Api-Key 即可）"。本适配器**只实现新版单
Key 方案**；旧版双凭据需要第二个密钥槽位，未实现，也不猜测其组合方式。
`Authorization: Bearer; …` 与 HMAC256 属于旧版 `/api/v2/asr`，**与本接口无关**。
`X-Api-Connect-Id` 出现在官方请求头示例中（用于追踪），但不在此前的头部表格中列为必需，
因此作为可追踪标识发送，并记录为"推荐而非已确认必需"。

### 二进制信封

4 字节 header（协议版本 / header 长度 / 消息类型 / 消息标志 / 序列化 / 压缩 / 保留），
整数**大端**：

```text
full client request  : [Header 4B][Payload size 4B][Payload]
audio only request   : [Header 4B][Payload size 4B][Payload]
full server response : [Header 4B][Sequence 4B][Payload size 4B][Payload]
error from server    : [Header 4B][Error code 4B][Error size 4B][Error UTF-8]
```

消息类型：`0b0001` full client request、`0b0010` audio only、`0b1001` full server response、
`0b1111` error。标志：`0b0000` 无序号、`0b0001` 正序号、`0b0010` 最后一包、`0b0011` 负序号。
序列化：`0b0000` 无 / `0b0001` JSON。压缩：`0b0000` 无 / `0b0001` gzip。

### 本适配器发送的字段（白名单）

`audio`：`format=pcm`、`rate=16000`、`bits=16`、`channel=1`。
`request`：`model_name=bigmodel`、`enable_itn=false`、`enable_punc=true`、
`enable_ddc=false`、`show_utterances=true`、`end_window_size`（强制判停）、
`force_to_speech_time`（起始保护）。

只发送上表字段；`codec` 依赖文档默认值 `raw`（即 PCM），不额外发送。任何未核对字段都不得
加入，测试会断言实际发出的字节。

### 判停

`end_window_size`：静音超过该值直接判停并输出 `definite`；配置后语义分句失效。
文档对新旧页面给出的取值范围不一致（旧页"最小 200"，新页 `[300,5000]`，推荐 `[800,1000]`），
本适配器采用 `[300,5000]` 并在配置校验中强制执行。

### 结果解析

`code == 0` 为成功；`payload_msg.result.text` 为整体文本；`utterances[].definite == true`
表示该分句确定；`payload_msg.result.utterances[].words[]` 为词级时间。
仅当 `show_utterances=true` 时才有 utterances。文档不同页面存在 `payload_msg.result` 与顶层
`result` 两种嵌套，解析器**同时接受**并在事件 detail 中记录实际形状。
协议中**没有 `is_final` 字段**；结束信号是二进制标志 `0b0011` 与 JSON `is_last_package: true`。

### 错误码

`20000000` 成功、`45000001` 请求参数无效、`45000002` 空音频、`45000081` 等包超时、
`45000151` 音频格式不正确、`550xxxxx` 服务内部错误、`55000031` 服务器繁忙。
鉴权/权限问题的具体映射文档**未单列**（落在 `45xxxxx` 带内的判断属推断）。

## 6. 尚未验证（不得声称已通过）

- **真实云调用**：本仓库无凭据，未做任何真实 `/api/v3/sauc/*` 调用；未计费。
- **真实云参数**：`end_window_size` / `force_to_speech_time` 的实际判停行为未在真实服务上观察。
- 文档**未发布**流式最大会话时长、WebSocket 关闭码表、RTF 硬要求。
- **实体设备 / 真实扬声器 / 真实麦克风**验收未进行。
- **Active Measurement Audio / acoustic events**：本文件记录的 Streaming ASR transport 不等于这些能力已实现；以 [Active Measurement](25-active-measurement.md) 状态为准。
- **Measurement Equivalence**：没有独立双录音真实实验，保持 validation_pending。
- 这些结论只覆盖软件与受控输入验证（`software_verified` / `browser_verified`）。

## 7. 参考页面（火山引擎文档中心，2026-09-11 在线核对）

- 大模型流式语音识别 API：`6561/1354869`
- 双向 / 单向流式页面（当前版）：`6561/2630027`、`6561/2628951`
- 资源 ID 与配额：`6561/1476626`；计费/并发：`6561/1359370`
- 录音文件识别（标准版）：`6561/1354868`、`6561/2606791`、`6561/2606792`
- 旧版鉴权方法（`/api/v2/asr`，与本接口无关）：`6561/107789`
