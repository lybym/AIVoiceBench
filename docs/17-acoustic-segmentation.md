# Acoustic Segmentation — signal-based speech detection

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Issue #23. 本模块从原始音频生成 speech-segment / acoustic-boundary **候选证据**。Acoustic timing 与 ASR estimated timing、speaker separation timing、LLM semantic timing、manual corrected timing 分离；segment 本身不带 tester/device 角色。

2026-09-16 组件决策：

- **Silero VAD**：作为 Linux Server 上 Recording Analysis 与 Active Measurement finalized replay 的首个模型型 Acoustic Boundary Provider；
- **TEN VAD**：作为 Remote Chrome Browser Station 的目标 Control VAD，优先用于低延迟 provisional start/end；
- **Energy/RMS VAD**：保留 synthetic fixture、debug、兼容和显式 fallback，不再作为长期正式默认算法。

详细分工见 [Remote Browser Station 与开源组件策略](26-remote-browser-component-strategy.md)。

## 1. VAD 在 AIVoiceBench 中的定位

VAD 不是 ASR 的强制前置。推荐链路是并行 Evidence Producer：

```text
Audio
 ├─ Acoustic Boundary Provider ─→ speech boundaries
 ├─ ASR Provider ───────────────→ text / provider timestamps
 └─ Speaker Separation ─────────→ anonymous speaker clusters
                         ↓
                  Attribution / Fusion
                         ↓
               Canonical EventTimeline
```

因此 Recording Analysis 即使由 File ASR 自己完成内部切句，也仍可使用独立 VAD 来得到与 ASR 解码逻辑解耦的 speech onset/offset evidence；反过来，如果某个任务只需要转写或语义评估，也不要求先跑独立 VAD。

## 2. Provider 边界

现有 `AcousticSegmenter` Protocol 保留为可替换接口，目标实现为：

```text
AcousticSegmenter
├─ EnergyVadSegmenter        # implemented legacy/fallback
├─ SileroVadSegmenter        # planned server baseline
└─ TenVadSegmenter           # planned browser/control or replay adapter
```

下游只消费统一 AcousticSegments contract，不绑定某一个 VAD 项目。

### 2.1 EnergyVadSegmenter

当前实现读取 canonical WAV（PCM16LE / 16 kHz / mono），按 frame 计算 RMS，使用完整文件的 10th-percentile noise floor 与 global peak 设置自适应阈值，再 merge/filter 成 segment。

该实现是 **batch algorithm**：依赖完整文件统计，不 causal，不允许直接冒充 Active Measurement streaming processor。

### 2.2 SileroVadSegmenter

目标用途：

- Recording Analysis 的默认模型型 boundary candidate；
- Active Measurement 对 durable Measurement Audio 的 server-side finalized replay；
- 和现有 Energy VAD / TEN VAD 做 paired validation。

Silero 输出仍必须经过 AIVoiceBench Adapter：

- sample index / milliseconds 统一换算；
- processor/model/version 固定记录；
- threshold、min speech/silence、padding/merge 等进入版本化 Measurement Policy；
- 原始概率或足够的诊断信息应尽可能保留；
- 不把项目默认 threshold 直接写成产品门槛。

Silero VAD 官方项目提供 PyTorch/ONNX 使用路径，当前仓库许可证为 MIT：<https://github.com/snakers4/silero-vad>。

### 2.3 TenVadSegmenter / Browser TEN VAD

目标用途：

- Remote Chrome Browser Station 的 Control Plane；
- `speech_suspected_start/end`、轮次推进、timeout 辅助；
- 事件直接记录现场 `sample_index`，而不是服务器接收时刻；
- 后续可对持久音频 replay，作为 Silero finalized result 的对照。

TEN VAD 官方仓库提供 Web/WASM 路径：<https://github.com/TEN-framework/ten-vad>。其 LICENSE 在 Apache 2.0 之外包含额外部署限制，因此正式商业/分发接入前必须完成许可证审查。许可证未确认时只允许做 Adapter/Spike，不得把依赖状态写成 production accepted。

## 3. AcousticBoundaryPolicy

PRD-F025 要求 boundary definition 与执行方式解耦。版本化 `AcousticBoundaryPolicy` 至少描述：

```text
provider / model / version
sample_rate
frame / hop
start threshold
end threshold
hysteresis
min speech
min silence
merge gap
pre/post padding
boundary uncertainty
post-processing version
```

TEN Browser provisional、Silero Server finalized、Energy legacy 可以使用不同 processor，但必须保留 policy provenance；它们发生冲突时保留双方证据，不静默覆盖。

当前 `EnergyVadSegmenter 1.0.0` 继续使用 `noise_floor_plus_active_range_fraction` policy。Silero/TEN 的首个 policy version 需要在实现时建立，不在本文预先伪造具体阈值。

### 3.1 Acoustic sensitivity profile（#94）

低音量设备回应可能整段落在 canonical 阈值之下，从而既没有 acoustic segment 也没有 speaker 对齐证据。为**评估**（不是替换）这一缺口，`EnergyVadSegmenter.from_profile()` 支持命名 sensitivity profile：

| profile | 定位 | 是否 measurement policy |
| --- | --- | --- |
| `canonical` | 现行默认（`threshold_factor 0.15`、`min_speech 100 ms`、`min_silence 200 ms`、`merge_gap 80 ms`、无 roll） | 是 |
| `quiet_device` | 诊断用：更低 active-range 阈值（`0.05`）、`min_speech 60 ms`、更长 pre/post roll | 否，显式标记 |

约束：

- profile 与显式 override 都进入 `processor.sensitivity`，并带 `is_canonical_measurement_policy`；非 canonical 时 note 明确写明“不得作为 canonical measurement 报告”。
- `resolve_sensitivity()` 拒绝未知 profile/参数与非有限值；canonical profile 加任何 override 也不再是 canonical。
- Recording Analysis 通过 `AIVOICEBENCH_ACOUSTIC_PROFILE` 选择 profile，默认 canonical；选择结果随 Run 证据保存，Metric Engine 公式与门槛不受影响，因此不会静默改变测量口径。
- 覆盖率差异由 alignment 诊断的 `low_energy` 分布与未匹配时长量化，见 [融合/Turn/Event](18-fusion-turns-events.md)。

## 4. What this stage does NOT do

- Assign speaker roles（tester / device / unknown）；这是 speaker separation + attribution。
- Produce final events or timeline；这是 fusion / turn / event detection。
- Treat ASR timestamps as acoustic boundaries。
- Infer semantic boundaries（meaningful response start）；这是 Judge/semantic evidence。
- Claim sample-exact truth；模型边界仍有 uncertainty。
- Decide that Silero/TEN 的输出天然比人工标注或其他 Provider 更“真实”。

## 5. Current command and legacy parameters

现有 Energy VAD CLI 继续保留：

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench acoustic 'C:/recordings/normalized.wav' --output artifacts/acoustic
```

现有参数：

```text
--frame-ms 30
--hop-ms 10
--threshold-factor 0.15
--min-speech-ms 100
--min-silence-ms 200
--merge-gap-ms 80
--pre-roll-ms 0
--post-roll-ms 0
```

这些参数只描述 Energy VAD legacy algorithm，不应被复用成 Silero/TEN 的隐式默认配置。

CLI exit code 0 = complete with segments，2 = insufficient evidence，1 = error。

## 6. Output schema

`AcousticSegments 1.0.0` (`schemas/acoustic-segments.schema.json`) 当前核心形态保持不变：

```json
{
  "schema_version": "1.0.0",
  "document_id": "ACOUSTIC-<uuid>",
  "source": { "path", "sha256", "duration_ms", "sample_rate_hz", "channels", "encoding" },
  "processor": { "method": "energy_vad", "processor_version": "1.0.0", "parameters": { }, "sensitivity": { "profile": "canonical", "overrides": { }, "is_canonical_measurement_policy": true, "note": "…" } },
  "status": "complete | partial | insufficient_evidence",
  "reason": null,
  "segments": [
    {
      "segment_id": "SEG-0000",
      "start_ms": 500.0,
      "end_ms": 1000.0,
      "confidence": 0.82,
      "source": "acoustic",
      "method": "energy_vad",
      "uncertainty_ms": 10.0,
      "frame_stats": { "peak_rms", "mean_rms", "threshold_rms", "frame_count" }
    }
  ]
}
```

Silero/TEN 接入时优先通过兼容 contract 扩展 `processor.method` / parameters/provenance，不为每个 Provider 创建平行 downstream schema。

`processor.sensitivity` 是 #94 加入的**可选**字段（见 §3.1）：未写入该字段的历史文档仍然合法，写入时只描述“这些边界由哪个 sensitivity 产生、是否为 canonical measurement policy”，不改变任何既有 required 字段语义。版本决定与迁移说明见 [契约版本](07-contract-versions.md)。

## 7. Timing taxonomy

| Source | Example method | This stage? |
| --- | --- | --- |
| Acoustic timing | Energy / Silero / TEN VAD | Yes |
| ASR estimated timing | Provider timestamps | No |
| Speaker separation timing | ASR-native / future diarization model | No |
| LLM inferred semantic | Structured decisions | No |
| Manual corrected | Human annotation | No |

Active Browser Control event 必须能追溯到本地 sample index。Server receive time 只用于 transport/jitter diagnosis。

## 8. Evidence and validation

现有 synthetic PCM fixture 继续验证逻辑与 contract；新增 Silero/TEN 后，真实验收必须使用目标 AI 玩具录音人工标注集，至少报告：

- speech-start absolute error；
- speech-end absolute error；
- miss / false alarm；
- short speech 与长静音表现；
- background audio / music / room noise 场景；
- coverage 与 `insufficient_evidence`；
- 不同 policy version 的回归变化。

在这些真实数据出现前，不宣称 Silero 或 TEN 是 AIVoiceBench 的 acoustic ground truth。

## 9. Next stages

- Recording Analysis：Silero Adapter → ImportRun acoustic stage → paired labeled validation；
- Active Control：TEN VAD WASM → Browser Station sample-indexed provisional event；
- Active Finalization：durable Measurement Audio → Silero replay → Canonical EventTimeline；
- Speaker separation：先验证火山 ASR-native speaker labels，不在当前阶段接 3D-Speaker；
- UI：wavesurfer.js 显示 acoustic/speaker/event/finding Evidence Region。