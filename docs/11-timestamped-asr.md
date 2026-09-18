# Timestamped external ASR

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Provider boundary 独立于 hardware/controller。File ASR Provider 返回原生响应、provider/model/config fingerprint，并规范化到 Transcript contract。Local operation 不要求 TTS/ASR/Judge 全部离线。

2026-09-16 路线决定：Recording Analysis 当前优先把 **火山 File ASR 的自动说话人分离**用完整，暂不把 3D-Speaker 作为必要依赖。ASR-native speaker label 是匿名 cluster evidence，不是 tester/device role truth。\n\n2026-09-18 配置/transport 决策：P0 File ASR 默认使用火山录音文件识别极速版 HTTP。目标由外置 `providers.yaml` 定义 endpoint/model/resource 与 `audio_transport`；`auto` 模式下小文件使用 Base64 `audio.data`，大文件才通过外置 `storage.yaml` 选择私有 TOS + 短期 Presigned GET 的 `audio.url`。对象存储是 transport adapter，不是所有 File ASR 的强制前置。

## Run locally — explicit offline fallback

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-asr.txt
.\.venv\Scripts\python.exe -m aivoicebench asr path/to/audio.wav --provider vosk --model-dir path/to/model --model-version 0.22 --source-role room_mix
```

Model path/version 显式提供；adapter 不静默下载或替换模型。Model files hash 进入 stable fingerprint。Vosk 继续作为显式 offline fallback，不在火山失败时自动替代。

Input 为 PCM16/16 kHz，mono 或 stereo with explicit channel selection。Adapter 不猜 speaker，也不静默 downmix stereo。可选 Run/Case 关联不证明 clock synchronization。

## Audit and observable limits

每次 invocation 保留 source snapshot、选定输入、raw provider response、normalized transcript 与 hashes；provider error 也保留可用 audit data。

Words/segments 保留 provider-estimated audio-relative timing。Lexical confidence 不等于 timestamp confidence。ASR timestamp 不自动升级为 acoustic speech event。

Text without timing becomes partial transcript with a gap. Empty recognition is an empty completed provider result, not proof that the DUT remained silent. Normalization rejects invalid/out-of-range/nonfinite timings.

External transcript 不代表 DUT 内部 ASR；不能用外部 ASR Transcript 冒充设备内部 transcript 计算 device ASR CER。

## Speaker separation contract

目标 File ASR normalization 允许 Provider 提供匿名 speaker labels：

```text
provider response
  utterance / word timing
  text
  speaker label
        ↓
Transcript / SpeakerSegments
        ↓
speaker_0 / speaker_1 / ...
        ↓
Attribution
        ↓
tester / device / unknown
```

约束：

- Provider speaker ID 只表示 ASR 服务认为属于同一说话人的 cluster；
- 不按 `speaker_0 = tester`、`speaker_1 = device` 进行顺序映射；
- label 缺失、冲突、短片段不可靠时保留 unknown / insufficient_evidence；
- role attribution 继续由显式证据、语义角色提议、上下文和人工修订共同完成；
- 原始 speaker label、provider/model/config/invocation 必须可追溯；
- speaker boundary 与 acoustic VAD boundary 是不同 Evidence Source，不静默覆盖。

当前阶段不接 3D-Speaker。只有真实目标录音验证证明火山 speaker separation 的 coverage/quality 不够，或者出现完全离线/供应商无关 diarization 等明确需求后，再增加独立 Diarization Provider。

## Current cloud/import path

Main `v0.4.0` 已有 audited Volcengine File ASR Web/CLI ImportRun 基础。Recording Backbone 见 [23-recording-backbone.md](23-recording-backbone.md)。现有代码可以消费 ASR-native labels，但当前音频 publication 仍依赖固定 Signed URL 配置；#87 将其改为极速版原生 `audio.data` / `audio.url` 双路径并外置 Provider/Storage 配置。**speaker-separation 请求参数和真实返回语义仍必须基于当前官方契约核对并完成真实服务验证**。

不得仅凭旧版 API 文档中的 `with_speaker_info` 等字段修改当前新接口请求；实际 adapter 必须以当前启用 endpoint/resource 的官方契约和真实响应为依据。

火山语音识别当前产品能力说明包含自动说话人分离（中英文）：<https://www.volcengine.com/docs/6561/1354871?lang=zh>。这说明能力存在，不等于当前 AIVoiceBench adapter 已完成参数接入、权限验证或真实录音准确率验收。

## Relationship to acoustic boundary

推荐 Recording Analysis 数据流不是 `VAD → ASR` 强制串行，而是：

```text
External Recording
   ├─ Silero VAD target ─→ acoustic speech boundaries
   └─ Volcengine ASR ────→ text / ASR timestamps / speaker labels
                         ↓
                 Attribution + Fusion
```

Silero/其他 VAD 解决“音频上什么时候有 speech”；ASR 解决“说了什么”并可补充 provider speaker labels。二者发生边界差异时作为独立 Evidence 保留。

## Actual historical workstation verification

2026-09-07 的 Vosk workstation smoke 使用生成 WAV 验证了 local WAV-to-transcript integration；它不证明真实设备 ASR accuracy、火山 speaker separation、角色归属或 HIL pass。

历史 model hash/fixture 信息继续保留在 Git history/work log；新的正式验收应聚焦真实 AI 终端录音与当前 cloud contract。

## Acceptance for speaker separation route

至少需要：

1. 当前火山 File ASR endpoint/resource 的 speaker separation 参数经过官方契约核对；
2. 真实调用 raw response 中出现可解释的 speaker label；
3. normalization 不丢失 label/timing/provenance；
4. 真实 5–20 分钟 tester/device 混合录音人工复核；
5. 报告 speaker coverage、错误/冲突、unknown/abstention 和 role-review workload；
6. 达不到项目批准门槛后，才开启额外 diarization Provider 选型。
