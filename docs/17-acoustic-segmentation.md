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

现有 `AcousticSegmenter` Protocol 保留为可替换接口，当前实现状态：

```text
AcousticSegmenter
├─ EnergyVadSegmenter        # implemented: stdlib legacy / fixture / explicit fallback
├─ SileroVadSegmenter        # implemented: server baseline (Issue #23)
└─ TenVadSegmenter           # planned: browser/control or replay adapter
```

下游只消费统一 AcousticSegments contract，不绑定某一个 VAD 项目。`SileroVadSegmenter` 位于 `aivoicebench/silero_vad.py`，实现同一 Protocol；它不改变 `EnergyVadSegmenter` 的任何行为。

Provider 选择是显式的：

```text
AIVOICEBENCH_ACOUSTIC_PROVIDER = energy | silero   （默认 energy）
--vad energy | silero                              （CLI，覆盖环境变量）
AIVOICEBENCH_SILERO_POLICY = 1.0.0                 （Silero 策略版本）
AIVOICEBENCH_SILERO_MODEL = <path>                 （可选，指向部署自带的 ONNX 权重）
```

**禁止静默回退。** 当选择 `silero` 而运行时或权重不可用时，Recording Analysis 的 acoustic stage 显式 `failed`（`SileroUnavailableError`），Cli exit 1；系统**不会**改用 energy VAD 冒充 Silero，也不会在文档中把 method 写成 `silero_vad`。要使用 fallback 必须由调用方显式选择 `energy`，因此文档与 Run manifest 中的 `method` 始终说明真实产生边界的算法。

### 2.1 EnergyVadSegmenter

当前实现读取 canonical WAV（PCM16LE / 16 kHz / mono），按 frame 计算 RMS，使用完整文件的 10th-percentile noise floor 与 global peak 设置自适应阈值，再 merge/filter 成 segment。

该实现是 **batch algorithm**：依赖完整文件统计，不 causal，不允许直接冒充 Active Measurement streaming processor。

### 2.2 SileroVadSegmenter（Issue #23，已实现）

实现路径与边界：

- **Runtime**：直接调用 `onnxruntime` 执行官方 Silero ONNX 导出图，`intra_op_num_threads = 1`、`inter_op_num_threads = 1`、CPU Execution Provider。Adapter **不导入 torch**：torch/torchaudio 不是推理依赖，只作为 `silero-vad` 包的声明依赖被一同安装。
- **权重来源（确定性、可审计）**：`silero-vad` PyPI wheel `6.2.2` 内的 `silero_vad/data/silero_vad.onnx`，文件大小 `2327524` bytes、sha256 `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`。Adapter 在加载前校验大小与 sha256，加载后把实际使用的 digest 写入 `processor.model.sha256`；校验失败即 `SileroUnavailableError`。**模型二进制不进入本仓库。** 部署可用 `AIVOICEBENCH_SILERO_MODEL` 指向自有权重，但同样必须通过该校验。
- **帧化**：模型自身的固定输入窗口 512 samples @16 kHz（32 ms hop）+ 64 samples left context。这是导出图的属性，不是策略选择，因此不放入 policy。最后一个不足 512 samples 的窗口以零填充，`processor.model.tail_padded` 记录该事实；短于一个完整窗口的音频不做模型调用，直接 `insufficient_evidence`。
- **输出**：model method 使用统一的 `audio_relative_ms` 坐标，`uncertainty_ms` = 一个窗口（32 ms）；每个 segment 的 `frame_stats` 记录该区间的 `peak/mean/min_speech_probability`、所用 `threshold`、`frames_above_threshold`、`frame_count`；`processor.parameters` 另记逐帧概率的确定性 10-bin histogram 与阈值上下计数。
- **证据与角色**：不产生 speaker 角色、不产生 Turn/Event、不消费 ASR 输出。Adapter 的入口只有音频路径，因此 ASR provider timestamp 无法进入声学结果。

Silero 输出仍必须经过 AIVoiceBench Adapter（已实现）：

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

当前 `EnergyVadSegmenter 1.0.0` 继续使用 `noise_floor_plus_active_range_fraction` policy。Silero 的首个 policy version 已在实现时建立，见 §3.2。

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
- sensitivity profile **只属于 energy provider**：`AIVOICEBENCH_ACOUSTIC_PROFILE` 与模型 provider 同时设置时 acoustic stage 显式失败，避免把一个 energy 参数静默套用到 Silero。

### 3.2 Silero boundary policy（Issue #23）

`SileroVadPolicy` 版本 `1.0.0`（文档中记为 `silero_boundary_policy/1.0.0`）是 AIVoiceBench 的 measurement input，不是上游默认值，也不是产品门槛：

| 参数 | 1.0.0 值 | 作用 |
| --- | --- | --- |
| `threshold` | 0.5 | speech onset：窗口语音概率 ≥ 该值即进入 speech |
| `negative_threshold` | 0.35 | hysteresis 释放阈值：低于该值才累计静音 |
| `min_speech_ms` | 250 | 短于该时长的候选段被丢弃（保持 unclaimed，不重分类为静音） |
| `min_silence_ms` | 100 | 静音持续不足该时长不结束 speech |
| `merge_gap_ms` | 0 | 间隔不超过该值的 speech 区间合并 |
| `pre_roll_ms` / `post_roll_ms` | 30 / 30 | 显式边界 roll；上游参考实现对每侧 padding 半个值，这里改为一侧一个显式 policy 成员 |

约束与如实声明：

- 这些值是 **initial baseline**，取自上游 Silero 参考后处理参数以便 provider 确定、可审计；它们**未**在 AIVoiceBench 目标录音上校准，文档 `processor.sensitivity.note` 与 `SILERO_POLICY_NOTES` 明确写明 “NOT calibrated / no accuracy claim”，且 `is_canonical_measurement_policy` 为 `false`。
- 修改任一值必须产生新的 `policy_version`，不得原地改 `1.0.0`。策略版本与有效参数都写入文档。
- 未实现 `max_speech_duration` 强制切分：本 provider 不做“为凑时长而切断”的处理，长 speech 保持整段并由 uncertainty 表达。
- 未知 `AIVOICEBENCH_SILERO_POLICY` / `--policy` 版本显式失败，不使用“最接近的版本”。

## 4. What this stage does NOT do

- Assign speaker roles（tester / device / unknown）；这是 speaker separation + attribution。
- Produce final events or timeline；这是 fusion / turn / event detection。
- Treat ASR timestamps as acoustic boundaries。
- Infer semantic boundaries（meaningful response start）；这是 Judge/semantic evidence。
- Claim sample-exact truth；模型边界仍有 uncertainty。
- Decide that Silero/TEN 的输出天然比人工标注或其他 Provider 更“真实”。

## 5. Current commands and parameters

### 5.1 Energy VAD（legacy，默认）

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench acoustic 'C:/recordings/normalized.wav' --output artifacts/acoustic
```

现有参数（只描述 Energy VAD legacy algorithm，**不会**被复用成 Silero 的隐式默认配置）：

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

### 5.2 Silero VAD（Issue #23，已实现）

```powershell
# 需要可选运行时：pip install -r requirements-vad.txt
& ./.venv/Scripts/python.exe -m aivoicebench acoustic 'C:/recordings/normalized.wav' `
    --vad silero --policy 1.0.0 --output artifacts/acoustic
```

`--vad`/`--policy` 同样适用于 `aivoicebench pipeline`；`import`（Recording Analysis）通过 `AIVOICEBENCH_ACOUSTIC_PROVIDER` / `AIVOICEBENCH_SILERO_POLICY` 选择。未选择模型 provider 时一切保持原状。

### 5.3 真实录音评测脚手架（AC3）

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench vad-eval `
    --annotation annotations/rec-001.json `
    --acoustic artifacts/acoustic/ACOUSTIC-xxxx.json `
    --output artifacts/vad-eval/rec-001.json
```

省略 `--annotation` 时命令明确输出 `NO_ANNOTATED_SAMPLE_SET` 并以 exit 2 结束，表示**评测未执行**，而不是零误差。标注文件格式见 `schemas/vad-annotation.schema.json`。

CLI exit code 0 = complete with segments / evaluated，2 = insufficient evidence / no annotated sample set，1 = error。

## 6. Output schema

`AcousticSegments 1.0.0` (`schemas/acoustic-segments.schema.json`) 的**版本号未变**，method-specific 成员是可选的加法扩展：

```json
{
  "schema_version": "1.0.0",
  "document_id": "ACOUSTIC-<uuid>",
  "source": { "path", "sha256", "duration_ms", "sample_rate_hz", "channels", "encoding" },
  "processor": {
    "method": "energy_vad | silero_vad",
    "processor_version": "1.0.0",
    "parameters": { "…": "method 变体（见下）" },
    "sensitivity": { "profile": "canonical | silero_boundary_policy/1.0.0", "overrides": { }, "is_canonical_measurement_policy": true, "note": "…" },
    "model": { "name", "version", "sha256", "source", "runtime", "runtime_version", "sample_rate_hz", "window_samples" }
  },
  "status": "complete | partial | insufficient_evidence",
  "reason": null,
  "segments": [
    {
      "segment_id": "SEG-0000",
      "start_ms": 500.0,
      "end_ms": 1000.0,
      "confidence": 0.82,
      "source": "acoustic",
      "method": "energy_vad | silero_vad",
      "uncertainty_ms": 10.0,
      "frame_stats": { "…": "method 变体（见下）" }
    }
  ]
}
```

三个 method-specific 成员（都**可选**、都 additive）：

1. **`processor.parameters`** 是 `oneOf` 两个闭集变体。energy/RMS 变体沿用原字段（`frame_ms`/`hop_ms`/`energy_metric`/`threshold_factor`/`threshold_mode`/…），且不允许出现 `threshold`；模型变体要求 `frame_samples`/`hop_samples`/**`threshold`**（绝对概率阈值）/`min_speech_ms`/`min_silence_ms`，并可带 `negative_threshold`、`frames_above_threshold`、`frames_below_negative_threshold`、`speech_probability_histogram`。这样模型 VAD 不需要伪造一个“能量相对阈值”。
2. **`processor.model`** 记录权重 identity（`name`/`version`/`sha256`/`source`）、runtime 与 `execution_provider`、以及固定分析窗口。缺失该块的模型文档会被 `acoustic_errors()` 拒绝，因为它的边界不可 replay/审计；反之 energy 文档出现该块也被拒绝。
3. **`segments[].frame_stats`** 同样是 `oneOf`：energy 变体为 `peak_rms`/`mean_rms`/`threshold_rms`/`frame_count`；模型变体为 `peak_speech_probability`/`mean_speech_probability`/`min_speech_probability`/`threshold`/`frames_above_threshold`/`frame_count`。

`processor.sensitivity` 是 #94 加入的**可选**字段（见 §3.1）：未写入该字段的历史文档仍然合法。Silero 复用它承载 policy id（`profile = silero_boundary_policy/1.0.0`）与 “非 canonical measurement policy” 标记。

### 6.1 下游（#24）契约边界

下游只允许依赖公共字段：`schema_version`、`source.sha256/duration_ms`、`processor.method/processor_version`、`status`、`reason`，以及每个 segment 的 `segment_id`/`start_ms`/`end_ms`/`confidence`/`source`/`method`/`uncertainty_ms`。**不得**读取 `processor.model`、`peak_speech_probability` 等 provider 专有字段来做判定。`AcousticSegments 1.0.0` 的 required 字段与语义均未改变，因此 #24 无需迁移。

版本决定、迁移说明与正/负测试覆盖见 [契约版本](07-contract-versions.md)。

## 7. Timing taxonomy

| Source | Example method | This stage? |
| --- | --- | --- |
| Acoustic timing | Energy / Silero / TEN VAD | Yes |
| ASR estimated timing | Provider timestamps | No |
| Speaker separation timing | ASR-native / future diarization model | No |
| LLM inferred semantic | Structured decisions | No |
| Manual corrected | Human annotation | No |

Active Browser Control event 必须能追溯到本地 sample index。Server receive time 只用于 transport/jitter diagnosis。

**ASR 隔离是可执行的约束，不只是约定**：`SileroVadSegmenter.segment(path)` 只接受音频路径，没有任何 ASR/transcript 入参；`acoustic_errors()` 会拒绝 `processor.method` 或 `segments[].method` 中出现 `asr` / `diarization` / `speaker` / `llm` / `provider_timestamp` 之类非信号来源，因此把 ASR final 时间戳改标成 acoustic boundary 的文档无法通过校验。

## 8. Evidence and validation

### 8.1 已完成的软件验证（fixture 级）

- **确定性 replay**：同一 Artifact + 同一 processor/policy 重复分割得到相同的 normalized Acoustic Evidence。比较时排除的字段只有 identity/environment 两项：`document_id`（每次新 UUID）与 `source.path`（调用方放置位置）。其余全部逐字段比较，且 `source.sha256` 参与比较，因此“同一 Artifact”是被字节绑定的，而不是靠文件名。
- **合成 fixture**：speech-like / silence / **显式 white noise** / 纯 tone 四类；边界顺序与坐标；短段（低于 `min_speech_ms` 保持 unclaimed）；空音频；短于一个分析窗口的音频；非 canonical 格式拒绝。
- **policy 敏感性**：`threshold`、`negative_threshold`、`min_speech_ms`、`min_silence_ms`、`merge_gap_ms`、pre/post roll 各自改变结果并被记录。
- **provenance/uncertainty**：每个 boundary 带 method/version/policy/evidence/uncertainty；模型 digest 与 runtime 版本进入文档。
- **模型 provider 不产生 RMS。** `SileroVadSegmenter` 的 `frame_stats` 是概率证据，没有 `mean_rms`。`alignment.py` 的 low-energy 诊断据此改为：只有真正测到 `mean_rms` 的 segment 才进入分布与阈值，缺失能量的 segment 记 `low_energy=false` 并在可选 `diagnostics.low_energy.energy_evidence` 中以 `mean_rms: null` 显式暴露；否则每个模型 boundary 都会被静默误判为“低音量设备回应”。见 [契约版本](07-contract-versions.md)。
- 测试文件：`tests/test_silero_vad.py`、`tests/test_silero_runtime.py`、`tests/test_vad_evaluation.py`、`tests/test_alignment.py`（能量证据缺失场景）。全部离线、无凭据、无网络。

### 8.2 尚未完成的真实验收（AC3）

Issue #23 的第三条验收要求“人工标注的真实录音样本集”报告 speech-start/end error、miss/false-alarm 与 evaluated denominator/coverage。本环境**没有**该数据集，因此：

- 已实现可复用的评测脚手架 `aivoicebench/vad_evaluation.py` + `schemas/vad-annotation.schema.json` + `aivoicebench vad-eval`；
- 无标注数据时脚手架显式输出 `no_annotated_sample_set`（`has_annotated_sample_set: false`，所有指标为 `null`），**不会**用合成数据冒充真实标注；
- **AC3 未满足**，真实录音人工标注验收仍然待办。

真实验收必须使用目标 AI 玩具录音人工标注集，至少报告：

- speech-start absolute error；
- speech-end absolute error；
- miss / false alarm；
- short speech 与长静音表现；
- background audio / music / room noise 场景；
- coverage 与 `insufficient_evidence`；
- 不同 policy version 的回归变化。

在这些真实数据出现前，不宣称 Silero 或 TEN 是 AIVoiceBench 的 acoustic ground truth。

## 9. Next stages

- Recording Analysis：Silero Adapter ✅ → ImportRun acoustic stage 可选切换 ✅ → **paired labeled validation（待真实标注数据）**；
- Evaluation：把 `vad-eval` 接到 ImportRun 产物，形成每次 Run 的 AC3 报告（仍受真实标注数据阻塞）；
- Active Control：TEN VAD WASM → Browser Station sample-indexed provisional event；
- Active Finalization：durable Measurement Audio → Silero replay → Canonical EventTimeline；
- Speaker separation：先验证火山 ASR-native speaker labels，不在当前阶段接 3D-Speaker；
- UI：wavesurfer.js 显示 acoustic/speaker/event/finding Evidence Region。

依赖说明：`requirements-vad.txt` 同时安装 `silero-vad` 与 `onnxruntime`。`silero-vad` 声明 torch/torchaudio，因此该文件会一并拉取它们；Adapter 自身只使用 onnxruntime，torch 仅为取得经过校验的权重文件与打包成本。若未来需要更小的部署面，可改为从官方发布获取同一 ONNX 文件并用同一 sha256 校验，`AIVOICEBENCH_SILERO_MODEL` 已为该路径预留。