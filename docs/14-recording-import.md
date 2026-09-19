# Recording Import — main / alpha.2

> 2026-09-11 基线：main c612d36 / alpha.2。当前主链与验证限制见 [Recording Backbone](23-recording-backbone.md)；真实验收仍待完成。

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Recording Analysis is the M1 implementation priority and one of two independent formal Measurement Pipelines. Current Web/CLI share ImportRun; no TestCase, Execution Run or live hardware is required. Its `ART-external-recording` is independent from Active Measurement's Live Measurement Audio. Existing Runner/Station/contracts are retained; this pipeline is not an Active Result finalization service.

## What works

Three-format ingestion, immutable source/canonical PCM16/16kHz/mono assets, QA, optional configured Vosk or cloud ASR, native responses/audit and Transcript are integrated. Acoustic and ASR-native clustering share the ledger. Role-dependent turns/events/metrics execute only after explicit user role evidence; default unknown roles abstain. LLM role attribution is disabled by product decision. Judge/Findings execute in the chain once a semantic provider is configured and roles are confirmed ([#10](https://github.com/lybym/AIVoiceBench/issues/10)); with no provider configured the stage abstains with its reason. Current reports remain provisional stage-status reports, not the post-review final test report.

Cloud ASR 的 `partial` 不等于无证据：只要 persisted Transcript 仍含合法 utterance/timestamp/speaker labels，diarization/attribution 必须消费这些有效部分并保留 gap 原因。对于 Seed standard 异步任务，resume 应查询已保存的 provider request ID，而不是重新 submit 同一录音。具体缺陷与验收见 [#93](https://github.com/lybym/AIVoiceBench/issues/93)。

## Windows commands

Use the project virtual environment with `requirements-dev.txt`. Install FFmpeg/FFprobe or provide paths to existing executables. The adapter is replaceable through `AudioProcessingProvider`; local conversion is the first deterministic implementation, not a requirement that ASR/diarization algorithms be local. No microphone or speaker setup is needed.

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench import 'C:/recordings/conversation.m4a' --profile examples/import-profile.example.json
```

Optional explicit offline ASR fallback:

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench import 'C:/recordings/conversation.wav' --asr-provider vosk --model-dir .cache/models/vosk-model-small-cn-0.22 --model-version 0.22
```

Vosk needs `requirements-asr.txt` and the selected local model. Missing model/configuration is recorded as a failed ASR stage after the source is preserved. It does not prevent a report. Imports support up to 30 minutes / 1 GiB, covering the required 5–20 minute inputs. Mono/stereo sources with one audio stream are supported; ambiguous multistream/multichannel media requires an explicit export/mapping. `--ffmpeg` and `--ffprobe` select executable paths. `--synthetic` marks generated test audio; ordinary external imports are labeled imported, which does not certify that their content is a real terminal test.

CLI exit code 2 means a valid partial Run with pending analysis, 1 means at least one failed stage or infrastructure error. The current import milestone does not return a fully analyzed success. Run `python -m unittest discover -s tests -q` for regression checks. Set `AIVOICEBENCH_REQUIRE_MEDIA_TESTS=1` to require actual FFmpeg conversion tests instead of skipping them when tools are absent; CI requires them on Windows and Linux.

## Stored outputs and reference resolution

```text
RUN-<uuid>/
  original/source.wav|mp3|m4a       # byte-identical snapshot, never overwritten
  manifest.json                    # atomic Run checkpoint / artifact catalog / stage ledger
  analysis/ANALYSIS-<uuid>/
    normalization/
      normalized.wav
      audio-metadata.json
      probe.stdout.txt             # native ffprobe JSON
      *.invocation.json            # exact argv, times, status, latency
      *.stdout.txt / *.stderr.txt  # local processor diagnostics
    asr-native/ASR-<uuid>/          # optional existing native + Transcript contract outputs
    audio-qa.json
    transcript.json
    acoustic-segments.json
    speaker-assignments.json
    attribution.json
    role-review.json
    alignment.json
    fused-segments.json
    turns.json
    timeline.json
    metrics.json
    judge-results.json
    findings.json
    report.json
    report.md
  role-review/
    role-mapping-REV-0001.json       # immutable human decision sets (append-only)
    role-mapping-REV-0002.json
```

RecordingRun 1.0 is a new workflow-specific manifest; the preparation RunManifest 1.0 is unchanged. An AnalysisOutput envelope binds domain output to Run/Analysis and status. Pending/failed/insufficient outputs have `data: null`, not invented canonical events or zero metrics. `data_artifact_ref` identifies the stored canonical document that carries the envelope's own `data` — the Transcript document for `transcript`, the canonical `audio-metadata.json` for a completed `audio-qa` — and its local relative paths resolve against that document's folder. It is never the binary audio artifact. The envelope supplies unscripted Run identity without putting a fake case_id into Transcript 1.0. Cloud Transcript 1.1.0 and imported MetricResult 3.0.0 are implemented, with legacy version validation retained.

Artifacts have unique IDs, relative paths, size/hash, parent IDs and processor labels. A normalized artifact points to the immutable original. Metadata/audit files record converter version and executable hash, exact downmix/resample options, source container/codec/rate/channels/start-time fields and derived duration. `recording_run_errors(manifest, root)` verifies paths, all file hashes, parent ordering, stage references and that every artifact kind belongs to the recording pipeline (`RECORDING_ARTIFACT_KINDS`): an Active Measurement artifact — live measurement audio, stimulus or control/execution evidence — can never be registered into a recording Run, so the chain stays isolated. Failed conversion may retain incomplete files only as diagnostics; they are not registered as normalized audio.

`GET /api/runs/{run_id}` returns the canonical audio QA as `audio_qa`: `{status, reason, measurements}`. `measurements` comes from the `audio-qa` envelope's own data when that stage completed, and otherwise from the registered document the envelope references (`audio-qa-conditions.json`, or the canonical `audio-metadata.json`), so the measured facts stay readable even when QA abstains. It is a read view of persisted documents, not a second QA computation, and `{}` means the Run has no registered `audio-qa` evidence at all.

Canonical audio QA is measured once per Run. `resume` deliberately preserves the `ingestion`/`normalization`/`audio_qa` stage state and creates a new AnalysisRevision without re-emitting the QA envelope, so the view resolves the current revision's envelope first and otherwise the newest registered `audio-qa` artifact in `manifest.json`, following that envelope's own refs. A revised Run therefore never reports "never produced QA" next to a QA ledger entry. A Run measured before conditions existed shows an explicit `conditions_version: unassessed` row rather than an empty list, so absent conditions can never be read as satisfied conditions.

Each re-import creates a new Run and Analysis ID while the unchanged source hash ties attempts together. Same normalized bytes are checked for the same local configuration; cloud output equality is not assumed. Explicit ASR resume retains the Run and creates an AnalysisRevision; completed ASR/report does not rerun. Full in-Run reanalysis and manual Annotation application belong to #26; no raw output is overwritten here. The manifest is an atomic progress checkpoint, not an immutable analysis result. Disk/filesystem failure can prevent further checkpoints/reports; existing original artifacts are never deliberately removed.

## Timing and quality limits

Conversion uses an explicit stream, PCM16, 16kHz, equal-weight stereo downmix if needed, SWR resampling settings, no gain normalization, no denoising and no silence trimming. Format is read from the encoded stream rather than assigning requested sample rate to raw bytes. Network input protocols and MOV external data references are disabled. The original remains available for channel-aware attribution; a downmixed derivative is not source separation and can lose antiphase content.

Canonical sample time is exact relative to its own decoded waveform. Original codec padding/edit lists and presentation time can affect mapping to the source container. Those start fields and duration differences are retained; source offset and uncertainty remain unknown/needs_review rather than false millisecond precision. None of these sample mappings establishes a speech onset, speaker role or device latency. Peak/RMS/DC/clipping/silence are measurements, not an invented quality gate.

Audio QA publishes those measurements together with explicit validity conditions in the `audio-qa` envelope, in `audio-metadata.json` (`normalized.conditions`) and, when QA abstains, in `audio-qa-conditions.json`. Each condition states its basis and its limitation: `decodable_canonical_audio` is `met` when FFmpeg produced canonical samples, and `nonempty_signal` is `unassessed` while no energy threshold is configured, or `insufficient` when the canonical waveform has exactly zero energy. A recording whose only unmet condition is `nonempty_signal` keeps `audio-qa` as `insufficient_evidence` with `data: null`, while the same measurements are published in the registered condition document, so abstention never deletes evidence. No condition is ever reported as pass/fail, and none of them claims recognition quality, ASR accuracy or measurement accuracy.

`audio-metadata.json` and `audio-qa-conditions.json` are Run-internal retention documents: no `schemas/*.schema.json` contract is registered for either, so `normalized.conditions` is additive by construction rather than by an `additionalProperties` declaration, and no migration is implied. A non-zero but inaudible signal stays `unassessed`: the exact-zero boundary is deliberate, and no energy threshold will be invented here.

The existing ASR provider limit remains 10 minutes by default for compatibility; the import caller explicitly allows up to 30 minutes. Vosk results remain external ASR with provider-estimated timestamps and unknown timing confidence/role. Unscripted ASR does not become device ASR truth.

## Validation and remaining acceptance

Tests use synthetic tones to exercise actual WAV/MP3/AAC-in-M4A decoding, 44.1kHz stereo → 16kHz mono, canonical sample preservation, 20-minute conversion/ASR preparation, source tampering/truncation, profile/ref validation, missing tools, failed ASR, repeat import isolation and secret-safe failure logs. A 5-minute (acceptance-band) synthetic recording is also imported through the full chain and re-read from disk alone — every registered artifact re-hashes and the manifest validates, which is the on-disk half of restart readability at acceptance-band duration. Derived-artifact provenance asserts every artifact carries a processor label and hash, the normalized audio reaches the immutable original, and the canonical metadata records the converter executable hash/version and exact normalization config; the recording-chain kind isolation test proves an Active Measurement artifact is rejected rather than retained. Generated Chinese speech also exercises actual Vosk transcription inside an imported Run. Neither is a real tester/device conversation acceptance.

Ingestion refusals are covered as durable states rather than silent gaps: a corrupt, truncated, empty or unmappable recording (for example a multichannel WAV or an unsupported `.txt` container) keeps its Run, records the refusal reason in `manifest.json`, leaves `original_sha256` null and registers no `original_recording`, and still writes a report. `test_import.py` also re-reads a completed Run from disk alone and re-hashes every registered artifact, proving that reading a Run needs no in-memory process state; the CI `Recording backbone container` workflow performs the equivalent check across a `docker restart`.

Remaining: real ASR/speaker-label verification, real-provider Judge/Findings acceptance and full Windows-browser/real-recording acceptance. No Windows executable or professional HIL is a prerequisite.

2026-09-18 的单份授权诊断录音已经观察到真实 Seed standard transcript 和 5 个匿名 speaker clusters，但 acoustic segments 未获得 speaker 对齐，且没有用户保存的角色 mapping，Timeline/metrics 因证据不足而弃权。诊断时曾执行的 LLM 角色提议现只作为历史故障证据，不进入产品结果。见 [诊断记录](27-real-recording-diagnostic.md)、[#94](https://github.com/lybym/AIVoiceBench/issues/94) 与人工确认 Gate [#95](https://github.com/lybym/AIVoiceBench/issues/95)。

## 人工角色确认 Gate（#95）

匿名 cluster 出现时，Run 在 `awaiting_role_review` 暂停：role-dependent Turns、Timeline、Metrics 与正式测试报告都不运行，只展示明确标注为导入/诊断的临时状态。该状态由每个 revision 自己发布的 `role-review.json`（kind `speaker-role-review`，`role-review` schema）记录，`GET /api/runs/{run_id}` 的 `role_review` 与 Web 片段页的人工确认面板读取同一份证据。

复核面按聚类给出：原生标签、段数与语音时长、可点击试听的代表性区间、对应 utterance 的转写片段与 `playback` 范围。`POST /api/runs/{run_id}/role-review` 接受 `{mapping, reviewer, reason}`，要求每个聚类都有明确决定；`unknown` 是有效决定，"未确认"与"确认后判为未知"是两种不同状态（`awaiting_role_review` / `incomplete_review` / `complete_review`）。

保存会写入一份不可变 `role-review/role-mapping-REV-NNNN.json`，并创建新的 AnalysisRevision。识别与聚类是原始机器证据，因此从当前 revision **恢复**（不重新调用 Provider、不重新聚类、不需要 Provider 配置），只从 Attribution 向下重跑；旧 revision 的 artifact 字节不变，修改 mapping 会再生成一份 revision 并在 `diff` 中显示变化。真实录音与人工标注验收仍属 [#85](https://github.com/lybym/AIVoiceBench/issues/85)。

Official media references: [FFmpeg stream selection/conversion](https://ffmpeg.org/ffmpeg.html), [FFprobe structured metadata](https://ffmpeg.org/ffprobe.html), [resampler options](https://ffmpeg.org/ffmpeg-resampler.html). Local CLI needs installed FFmpeg/FFprobe; Docker already includes them.

## 分阶段编排、失败状态与读取面一致性（#27）

Recording Analysis 只有一个持久化 Run/AnalysisRevision 模型和一个读取投影，Web、API 与 CLI 都渲染它：

```text
aivoicebench/import_pipeline.py   写入 stage ledger / envelopes / revisions
aivoicebench/run_view.py          唯一的 Run 读取投影（无 Web 依赖）
      ├─ GET /api/runs            列表
      ├─ GET /api/runs/{run_id}   详情（含 stages / role_review / workbench / report）
      └─ aivoicebench runs        同一文档；`--json` 输出与 API 逐字段相同
```

`GET /api/runs` 与 `GET /api/runs/{run_id}` 不再各自推导 `status`/revision：二者与 CLI 都调用同一个投影，因此“同一份持久化数据在不同读取面得到不同结论”在结构上不可达。`aivoicebench runs` 的退出码沿用 `import` 的约定：`1` 表示 Run 不可读或存在 `failed` 阶段，`2` 表示可读但仍有未完成阶段，`0` 表示全部阶段 `complete`。`--json` 打印的文档与 `GET /api/runs[/{run_id}]` 的响应体一致，可直接逐字段比对。

阶段账本（`manifest.stages`）为每个阶段记录 `status`（`pending`/`running`/`complete`/`partial`/`failed`/`insufficient_evidence`）、processor 名称与版本、输入/输出 artifact 引用、`reason` 与耗时。失败不会删除已产生的证据：失败阶段保留真实原因，未运行的下游阶段发布 `insufficient_evidence` envelope 并写明原因，报告阶段照常产出，Run 仍通过 `recording_run_errors` 完整性校验。

阻断 role-dependent 阶段的原因必须描述**该 revision 实际发生的事**，不能把不同状态混为一谈：

- 存在匿名聚类且没有人工决定 → `awaiting_role_review`；
- 只有部分聚类有决定 → `incomplete_review`；
- 全部聚类都被人工判为 `unknown` → 说明“没有任何聚类被判为 tester/device”；
- 人工 mapping 已确认 tester/device，但 fused segment 未取得角色（声学区间与 speaker span 无可归属重叠，`speaker-alignment` 记录该事实）→ 说明是**没有可归属重叠**，不得声称“用户没判 tester/device”；
- Attribution 阶段自身 `failed` → 直接引用该阶段的失败原因，不得用角色 Gate 掩盖处理器故障。

`tests/test_recording_orchestration.py` 覆盖上述区分、逐阶段故障注入（Run 仍有效、旧 revision artifact 字节不变、报告仍产出）、冷拷贝重建（当前 revision / 阶段状态 / 证据链接 / 报告只来自磁盘）、CLI 与 API 一致性以及 secret/签名 URL 不进入任何读取面。容器 `docker restart` 后的同一组断言由 CI 的 `Recording backbone container` job（`scripts/docker_smoke.py --save-history` / `--verify-history`）承担：重启后必须重建当前 AnalysisRevision、完整阶段账本、role Gate、证据链接与报告。以上均为软件/容器验证，不构成 `real_recording_verified`（[#85](https://github.com/lybym/AIVoiceBench/issues/85)）。
