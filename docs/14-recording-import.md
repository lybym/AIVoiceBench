# Recording Import — main / alpha.2

> 2026-09-11 基线：main c612d36 / alpha.2。当前主链与验证限制见 [Recording Backbone](23-recording-backbone.md)；真实验收仍待完成。

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

Recording Analysis is the M1 implementation priority within the dual-workflow product. Current Web/CLI share ImportRun; no TestCase or live hardware is required. Existing Runner/Station/contracts are retained.

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

RecordingRun 1.0 is a new workflow-specific manifest; the preparation RunManifest 1.0 is unchanged. An AnalysisOutput envelope binds domain output to Run/Analysis and status. Pending/failed/insufficient outputs have `data: null`, not invented canonical events or zero metrics. `data_artifact_ref` identifies the stored canonical Transcript document; resolve its local relative paths against that document's folder. The envelope supplies unscripted Run identity without putting a fake case_id into Transcript 1.0. Cloud Transcript 1.1.0 and imported MetricResult 3.0.0 are implemented, with legacy version validation retained.

Artifacts have unique IDs, relative paths, size/hash, parent IDs and processor labels. A normalized artifact points to the immutable original. Metadata/audit files record converter version and executable hash, exact downmix/resample options, source container/codec/rate/channels/start-time fields and derived duration. `recording_run_errors(manifest, root)` verifies paths, all file hashes, parent ordering and stage references. Failed conversion may retain incomplete files only as diagnostics; they are not registered as normalized audio.

Each re-import creates a new Run and Analysis ID while the unchanged source hash ties attempts together. Same normalized bytes are checked for the same local configuration; cloud output equality is not assumed. Explicit ASR resume retains the Run and creates an AnalysisRevision; completed ASR/report does not rerun. Full in-Run reanalysis and manual Annotation application belong to #26; no raw output is overwritten here. The manifest is an atomic progress checkpoint, not an immutable analysis result. Disk/filesystem failure can prevent further checkpoints/reports; existing original artifacts are never deliberately removed.

## Timing and quality limits

Conversion uses an explicit stream, PCM16, 16kHz, equal-weight stereo downmix if needed, SWR resampling settings, no gain normalization, no denoising and no silence trimming. Format is read from the encoded stream rather than assigning requested sample rate to raw bytes. Network input protocols and MOV external data references are disabled. The original remains available for channel-aware attribution; a downmixed derivative is not source separation and can lose antiphase content.

Canonical sample time is exact relative to its own decoded waveform. Original codec padding/edit lists and presentation time can affect mapping to the source container. Those start fields and duration differences are retained; source offset and uncertainty remain unknown/needs_review rather than false millisecond precision. None of these sample mappings establishes a speech onset, speaker role or device latency. Peak/RMS/DC/clipping/silence are measurements, not an invented quality gate.

The existing ASR provider limit remains 10 minutes by default for compatibility; the import caller explicitly allows up to 30 minutes. Vosk results remain external ASR with provider-estimated timestamps and unknown timing confidence/role. Unscripted ASR does not become device ASR truth.

## Validation and remaining acceptance

Tests use synthetic tones to exercise actual WAV/MP3/AAC-in-M4A decoding, 44.1kHz stereo → 16kHz mono, canonical sample preservation, 20-minute conversion/ASR preparation, source tampering/truncation, profile/ref validation, missing tools, failed ASR, repeat import isolation and secret-safe failure logs. Generated Chinese speech also exercises actual Vosk transcription inside an imported Run. Neither is a real tester/device conversation acceptance.

Remaining: real ASR/speaker-label verification, automatic role/semantic association, Judge/Findings/full report integration, human revisions and full Windows-browser/real-recording acceptance. No Windows executable or professional HIL is a prerequisite.

Official media references: [FFmpeg stream selection/conversion](https://ffmpeg.org/ffmpeg.html), [FFprobe structured metadata](https://ffmpeg.org/ffprobe.html), [resampler options](https://ffmpeg.org/ffmpeg-resampler.html). Local CLI needs installed FFmpeg/FFprobe; Docker already includes them.
