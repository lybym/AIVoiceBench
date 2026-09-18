# Recording Import — main / alpha.2

> 2026-09-11 基线：main c612d36 / alpha.2。当前主链与验证限制见 [Recording Backbone](23-recording-backbone.md)；真实验收仍待完成。

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Recording Analysis is the M1 implementation priority and one of two independent formal Measurement Pipelines. Current Web/CLI share ImportRun; no TestCase, Execution Run or live hardware is required. Its `ART-external-recording` is independent from Active Measurement's Live Measurement Audio. Existing Runner/Station/contracts are retained; this pipeline is not an Active Result finalization service.

## What works

Three-format ingestion, immutable source/canonical PCM16/16kHz/mono assets, QA, optional configured Vosk or cloud ASR, native responses/audit and Transcript are integrated. Acoustic, ASR-native clustering, attribution and fusion share the ledger. Role-dependent turns/events/metrics execute only with evidence; default unknown roles abstain. Judge/Findings are not executed and reports are stage-status reports.

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
    fused-segments.json
    turns.json
    timeline.json
    metrics.json
    judge-results.json
    findings.json
    report.json
    report.md
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

Remaining: real ASR/speaker-label verification, automatic role/semantic association, Judge/Findings/full report integration, human revisions and full Windows-browser/real-recording acceptance. No Windows executable or professional HIL is a prerequisite.

Official media references: [FFmpeg stream selection/conversion](https://ffmpeg.org/ffmpeg.html), [FFprobe structured metadata](https://ffmpeg.org/ffprobe.html), [resampler options](https://ffmpeg.org/ffmpeg-resampler.html). Local CLI needs installed FFmpeg/FFprobe; Docker already includes them.
