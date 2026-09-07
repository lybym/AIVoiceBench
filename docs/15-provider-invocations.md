# Provider invocation evidence

Recording Import → Automatic Analysis remains the Primary Workflow. The full
architecture migration is PR #28; import/normalization is PR #29. This sibling
implementation addresses #30, an independent foundation extracted from #22.
It does not depend on unmerged import code or extend the old serial PR stack.

## Implemented contract

`InvocationAudit` writes `provider-calls/CALL-*/start.json` before a provider call.
The start is never replaced. `result.json` is created exactly once after completion.
An interrupted process leaves a pending start, not a fabricated successful result.
Separate retries have distinct invocation IDs and retain a shared operation ID plus
attempt number. The orchestrator chooses retry policy; this layer never resubmits
a billable request automatically.

Records retain provider/model/processor version, credential-free HTTPS endpoint,
API and prompt versions (nullable when inapplicable), explicit config and its closed
JSON Schema, UTC start/end and monotonic elapsed time. Input/output paths are local
to the evidence root and carry SHA256 and byte size. Completion requires output
evidence. `invocation_errors` checks the schema and referenced files when consuming
a record. `read_invocation` also checks that the terminal result matches its
immutable start. Changed inputs cannot produce a successful result. A failed attempt may
retain outputs; a pending attempt is not a measurement or a failed device test.

`RecordedASRProvider` wraps the existing `ASRProvider` protocol. It preserves native
response strings and the selected model profile as separate hashed artifacts,
checks that the returned profile matches the selected configuration, and delegates
normalization. Existing Vosk/Transcript contracts and timing semantics are unchanged.
Provider completion and transcript normalization are separate outcomes: if the
latter fails, the completed call and native output survive. Exceptions passed into
the legacy ASR error report are fixed safe messages, not SDK exception text.

Usage through the existing API (the root must contain the ASR output directory):

```python
from aivoicebench.asr import VoskProvider, transcribe_file
from aivoicebench.asr_audit import RecordedASRProvider
from aivoicebench.providers import ProviderIdentity

provider = VoskProvider(model_dir, model_version)
config_schema = {
    "type": "object", "additionalProperties": False,
    "required": ["words", "sample_rate_hz", "chunk_frames"],
    "properties": {
        "words": {"const": True},
        "sample_rate_hz": {"const": 16000},
        "chunk_frames": {"const": 4000},
    },
}
recorded = RecordedASRProvider(
    provider, evidence_root,
    ProviderIdentity("vosk", provider.profile["model_id"], "asr-audit-1.0.0"),
    provider.profile, config_schema,
)
transcribe_file(source_wav, recorded, evidence_root / "asr", source_role="room_mix")
```

The config schema is trusted adapter code, never a model-generated schema. Adapters
must constrain permissible values and keep credentials outside config; the key guard
is an additional check, not a universal secret detector. Do not pass authorization
headers, signed URLs, SDK request objects or environment dumps into this layer.
Future URL publication stores a non-secret object identity and artifact linkage;
temporary signed URLs stay in memory. Native recognition content remains private
run evidence and is not committed to Git.

LLM/TTS/Diarization protocols mark future provider boundaries. They do not implement
model decisions. The existing ASR protocol and PR #29 AudioProcessingProvider remain
their owning interfaces. Structured LLM decisions/evidence validation remain #10.

## Current Volcano verification (2026-09-07)

Read the live official documentation in the browser, starting with the previously
verified [product updates](https://docs.volcengine.com/docs/6561/2668039?lang=zh).
No historical endpoint assumption was used for an implementation.

- [Standard submit](https://docs.volcengine.com/docs/6561/2606791?lang=zh), updated
  2026-09-04: `/api/v3/auc/bigmodel/submit` uses `X-Api-Key`, a request UUID and
  resource `volc.seedasr.auc` (2.0) or `volc.bigasr.auc` (1.0). Body requires
  `audio.url`; `model_name` is `bigmodel`. Limits: 512 MB / five hours. Chinese
  diarization requires `enable_speaker_info` and `show_utterances`; `ssd_version`
  `200` with `ssd_mode` 1 is documented for longer non-meeting recordings. This
  is a candidate configuration, not verified accuracy for terminal conversations.
- [Standard query](https://docs.volcengine.com/docs/6561/2606792?lang=zh), updated
  2026-08-18: `/api/v3/auc/bigmodel/query` queries using the returned task ID.
  Async state/retry classification must be verified against the error-code contract
  before adapter implementation, not inferred from HTTP 200 alone.
- [Flash](https://docs.volcengine.com/docs/6561/2608628?lang=zh), updated
  2026-09-04: `/api/v3/auc/bigmodel/recognize/flash`, resource
  `volc.bigasr.auc_turbo`, synchronous result, 100 MB / two hours. Its current body
  also specifies `audio.url`; local `audio.data`/base64 support was not established.
  `show_utterances` exposes segments/words; the example includes
  `additions.speaker`. Preserve that ID without assigning a tester/device role.
  Keep `enable_ddc=false` so semantic smoothing does not remove fillers.

## Remaining #22 work and integration

1. Configured private audio publication / expiring URL transport with original-to-
   uploaded artifact hash linkage; never silently upload to an arbitrary host or
   substitute an unrelated audio URL for a local file.
2. Standard asynchronous submit/query adapter, bounded polling, resumable task ID,
   explicit retry rules and attempt records. Secrets supplied separately through
   environment/local credentials; no credentials are currently configured here.
3. Extend cloud transcript representation for unavailable model hashes/word
   confidence and overlapping diarized segments, preserving the old local schema.
   Unknown confidence must remain null, not 0/1. ASR/diarization timestamps are
   estimates with unknown uncertainty; they are not acoustic ground truth.
4. Diarization output/provider abstraction with source/model/evidence, unknown role,
   plus append-only human revisions (#26) and fusion (#24).
5. Wire providers into imported Run stage/artifact registration after dependency
   integration is explicitly authorized; #21 remains usable in local-only mode.
6. Contract tests plus a separately labeled actual cloud call and real 5–20 minute
   recording acceptance. This milestone performs no upload, cloud recognition,
   physical HIL, or real-device performance measurement.

Issues/PRs stay open for review. No main changes or merges are performed.
