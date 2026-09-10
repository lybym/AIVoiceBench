# Timestamped external ASR

> Technical reference / 技术参考。产品范围、验收与当前代码实现标识统一见 [PRD](PRD.md)。设计目标或示例不表示功能已实现；历史执行状态不替代当前 ref 审计。

The provider boundary is independent of the hardware/controller. Providers return native response strings plus the actual provider/model/config fingerprint and implement normalization into Transcript 1.0.0. Vosk is the first optional local adapter; online providers can implement the same interface. Local operation does not require that TTS/ASR/Judge all run offline.

## Run locally

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-asr.txt
.\.venv\Scripts\python.exe -m aivoicebench asr path/to/audio.wav --provider vosk --model-dir path/to/model --model-version 0.22 --source-role room_mix
```

Model path/version are explicit; the adapter does not silently download or substitute a model. Model files are hashed into a stable fingerprint. Example version 0.22 refers to the small Chinese model tested on this workstation, not a permanent current-model default. Model availability/license checked against [official Vosk models](https://alphacephei.com/vosk/models); the selected small Chinese model is listed as Apache 2.0. API behavior checked against the [official Python WAV example](https://github.com/alphacep/vosk-api/blob/master/python/example/test_simple.py).

Input: PCM16, 16 kHz, mono or stereo with explicit `--channel 1/2`. The adapter never guesses speakers or downmixes stereo. Optional paired `--run-id`/`--case-id` associates the transcript with an existing Run but does not itself prove run identity or synchronize its clock. The initial bounded implementation supports up to 10 minutes.

## Audit and observable limits

Each invocation retains a local source snapshot, selected mono input, original provider response strings in raw-provider.json, normalized transcript.json, and hashes for source/input/raw/model files. Provider errors retain available audit data and error.json. These outputs are local and ignored by Git.

Words and segments preserve provider-estimated audio-relative timing. Timestamp confidence is unknown/null when the provider only supplies recognition confidence; lexical confidence is not timestamp confidence. Speaker is null unless a future adapter actually supplies attribution. Source role is explicitly configured, default unknown. Transcript-to-Run mapping remains unmapped until alignment is established; no ASR boundary is silently promoted to a stronger acoustic speech event. Device ASR CER cannot be computed from this external transcript as though it were a device log.

Text without timing becomes a partial transcript with a gap. Empty recognition is an empty completed provider result, not evidence that a device remained silent. Normalization rejects invalid/out-of-range/nonfinite timings. A shared validator checks normalized bounds/order across adapters.

## Actual workstation verification

2026-09-07: generated a local test WAV (not microphone/terminal capture) using Microsoft Huihui Desktop, then actually called Vosk 0.3.45 with vosk-model-small-cn-0.22. Input duration was 7130 ms; one timestamped segment recognized the intended phrase about voice testing and the blue sky. Original TTS punctuation was not retained by ASR. This proves the local WAV-to-transcript integration, not ASR accuracy on real devices or a HIL pass.

Model archive SHA-256: `3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba`. Model-file fingerprint: `2e71fb6b30fd4f73945f19995a506118164751524c3325dc61a721b9e50d25c4`. The archive/model stay in ignored .cache/models. Target capture, run-clock alignment and other providers remain untested.

The user explicitly permits Volcano/火山 API speech synthesis. Golden Set generation will support that configurable provider; local Windows synthesis was only a convenient ASR smoke input. Do not infer that the final application must synthesize locally or operate entirely offline.
