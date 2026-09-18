# Model management

> 2026-09-18 target decision: provider/model and object-storage configuration is **server-side external configuration**, not application code and not browser-owned state. Current `main` still contains the SQLite-backed model-settings implementation; [Issue #87](https://github.com/lybym/AIVoiceBench/issues/87) performs the migration. This document distinguishes current fact from target behavior and does not claim #87 is implemented.

The target route model keeps lifecycle-distinct capabilities: **`asr`** (File ASR for Recording Analysis), **`streaming_asr`** (Active Voice Test real-time recognition), **`tts`** (complete-text / asset synthesis, used by Fixed), **`streaming_tts`** (streaming-text / streaming-audio session, used by Free), `diarization`, and result `judge`. Current `main` still exposes the legacy five-route model and V3 HTTP SSE TTS; the `streaming_tts` route and V3 WebSocket split are target behavior tracked by [Issue #98](https://github.com/lybym/AIVoiceBench/issues/98). File ASR and Streaming ASR remain separate lifecycle families; Provider timestamps are never formal acoustic boundaries.

## 1. Configuration ownership

### Current implementation

Current releases persist model profiles/routes under:

```text
AIVOICEBENCH_OUTPUT/.model-settings/credentials.sqlite3
```

The browser model manager can edit those settings. Credential values are backend-only and the API returns only redacted/configured state. This remains a compatibility fact until #87 lands.

### Target configuration

The target server runtime uses two external files:

```text
/etc/aivoicebench/providers.yaml
/etc/aivoicebench/storage.yaml
```

Repository examples:

```text
config/providers.example.yaml
config/storage.example.yaml
```

`config/aivoicebench.example.yaml` contains only core application settings plus paths to those external files. The runtime path may be overridden by:

```text
AIVOICEBENCH_PROVIDERS_CONFIG
AIVOICEBENCH_STORAGE_CONFIG
```

These environment variables identify **file locations only**. Endpoint/model/resource/voice/route/storage parameters must not be scattered across many process environment variables or hard-coded in application code.

For Docker/Linux Server deployment, the two runtime files should be mounted read-only. They are deployment input and must be independently reviewable/versionable outside the image.

## 2. Provider configuration

`providers.yaml` is the target source of truth for non-secret provider configuration:

- profile id/name/provider/protocol;
- endpoint/base URL;
- model or endpoint id;
- resource id;
- TTS voice/audio parameters;
- timeout and protocol-specific parameters;
- capability declarations and default routes;
- credential **reference** such as `credential_env`, never the credential value.

Expected route set:

```text
tts
asr
streaming_asr
diarization
judge
```

ASR-native diarization should normally point `diarization` to the same File ASR profile so one native recognition response can provide transcript/timestamps/speaker labels without a duplicate billable call.

### Active TTS transport split (#98)

Active Voice Test must not use one ambiguous TTS profile to infer transport at runtime:

- `tts`: complete text / asset synthesis. The target Volcengine adapter uses **V3 WebSocket unidirectional streaming** (`wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream`). Fixed cases submit the complete text once, consume the provider audio stream, validate/normalize it and freeze a Stimulus Artifact before the formal Run.
- `streaming_tts`: streaming text / streaming audio session. The target Volcengine adapter uses **V3 WebSocket bidirectional streaming** (`wss://openspeech.bytedance.com/api/v3/tts/bidirection`). Free mode feeds ordered LLM text chunks into one Turn-bound TTS session and streams returned audio to Browser playback.

Both profiles are server-owned. Browser code receives audio/control data but never Provider credentials and never connects directly to Volcengine. A failure on the bidirectional path does not silently select SSE or the unidirectional route.

Contract sources (implementation must re-check these current official pages before coding):

- [Volcengine V3 WebSocket unidirectional streaming TTS](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-websocket?lang=zh)
- [Volcengine V3 WebSocket bidirectional streaming TTS](https://docs.volcengine.com/docs/DoubaoVoice/bidirectional-streaming-text-to-speech-websocket?lang=zh)

TTS profile parameters must use the provider's **current official V3 field semantics**, not legacy aliases inferred from the existing SSE adapter. Common configurable parameters include speaker/voice, output encoding/format, sample rate and speech rate; loudness/volume and pitch are exposed only where the selected protocol/model/voice officially supports them. Unsupported combinations are configuration errors or explicit `unsupported` capability results, never silent no-ops. The Run snapshot records only non-secret resolved values and the actual transport.

For Fixed, provider stream encoding and final Stimulus Artifact encoding are separate concerns. The official streaming API does not imply that the wire stream is WAV; the Backend may wrap/normalize supported provider audio (for example PCM) into the project's frozen WAV asset after synthesis and before formal playback.

The OpenAI-compatible Judge path remains Chat Completions-compatible. File ASR, Streaming ASR and the two TTS lifecycle families use protocol-specific adapters. Provider configuration does not prove connectivity or paid-service readiness.

## 3. Object-storage configuration

`storage.yaml` is separate from provider configuration because object storage is an **audio transport adapter**, not an ASR model.

It owns only non-secret storage parameters such as:

```text
provider
endpoint
region
bucket
prefix
credential env references
presigned GET TTL
delete-after-use
lifecycle backstop
```

The first implementation target is Volcengine TOS. The bucket/object must remain private. The backend uploads directly through the storage adapter and only exposes a time-bounded Presigned GET URL to a File ASR provider when URL transport is needed.

Object storage is not required by Streaming ASR and is not required for every File ASR request.

## 4. File ASR transport policy

Recording Analysis keeps Volcengine **录音文件识别极速版 HTTP** as the P0 File ASR path.

Target modes:

```text
audio_transport = inline | object_storage | auto
```

Default `auto` behavior:

```text
canonical WAV
    |
    +-- <= inline_max_bytes --> Base64 --> audio.data --> File ASR Flash
    |
    +-- >  inline_max_bytes --> private object storage
                               --> short-lived Presigned GET
                               --> audio.url --> File ASR Flash
```

Initial engineering default:

```text
inline_max_bytes = 15728640   # 15 MiB
```

The threshold is configurable and must be captured in the Run's non-secret configuration provenance; it is not a permanent product constant.

Standard/Idle File ASR may be added later as additional provider modes. They are not required by #87 and do not replace the default Flash path.

The existing fixed publication variables:

```text
AIVOICEBENCH_AUDIO_PUT_URL
AIVOICEBENCH_AUDIO_GET_URL
AIVOICEBENCH_AUDIO_HOST
```

are current implementation details only. #87 removes them from the production dependency path. A backend that already owns the canonical WAV should not require a pre-created PUT URL merely to upload its own file.

## 5. Credentials and secret boundary

Long-lived credentials remain backend-only.

External YAML contains references such as:

```yaml
credential_env: VOLCENGINE_SPEECH_API_KEY
credentials:
  access_key_env: TOS_ACCESS_KEY
  secret_key_env: TOS_SECRET_KEY
```

It must not contain the resolved secret values.

Secrets and complete signed URLs must never enter:

- Git;
- browser payload/static bundle;
- Run snapshot/model-config artifact;
- report;
- Issue/PR text;
- ordinary logs.

A Run snapshot records effective **non-secret** provider/storage configuration, selected route, transport mode/threshold, model/resource identifiers, storage adapter id, and safe invocation provenance.

## 6. Revisions and migration

Configuration is resolved once at Run start and the resolved non-secret view is immutable for that AnalysisRevision. A later file edit affects only a later Run/reanalysis.

Migration from current SQLite settings must be explicit:

- do not silently merge SQLite and external-file values;
- startup/API must identify the active source;
- a conflict is an explicit configuration error or a documented compatibility mode;
- legacy settings remain readable only for the bounded migration period;
- browser configuration UI must not continue persisting a second authoritative copy after file-based ownership becomes active.

## 7. Verification

#87 must cover:

- YAML schema/version validation;
- missing/unreadable config files;
- duplicate/invalid profile or route references;
- missing credential environment variables without secret echo;
- URL/protocol/range validation;
- `inline | object_storage | auto` branch selection;
- no-storage behavior for eligible inline File ASR;
- required-storage failure for over-threshold File ASR;
- TOS upload / Presigned GET / cleanup audit using controlled fixtures;
- secret and signed-URL redaction;
- immutable per-Run non-secret configuration snapshots;
- Docker read-only config mounts and restart behavior.

These tests are software/container evidence only. Real Volcengine/TOS calls and authorized real recordings remain separate #22/#85 acceptance evidence.
