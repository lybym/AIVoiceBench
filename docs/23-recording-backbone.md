# M1 — Real Recording Backbone

PRD refs: PRD-F004–F009/F016/F017; Issues #22/#24/#25/#27/#30.
Audited 2026-09-11 at main c612d36 (published as v0.2.0-alpha.2), then re-checked against main 8d01ef2 (= v0.3.2). Complete M1 and real-recording acceptance remain pending.

## One Run

Web upload and CLI `import` use `import_recording()` and `ImportRun.execute()`.
Ingestion → normalization → QA → configured ASR → versioned status report share
one manifest, stage ledger and artifact catalog. Acoustic → ASR-native diarization → attribution → fusion execute; turns/timeline/metrics execute only with role evidence. Without roles they abstain. Judge/findings are not executed by ImportRun; their state envelopes remain explicit. Existing modules and the
legacy explicit `pipeline` CLI remain available, but are not another Web path.
Historical `web-analysis` records remain readable and are never rewritten.

`ModelSettings.capture()` returns `(snapshot, RunProviders)` with ASR factory,
diarization, Judge and TTS slots. ASR and ASR-native diarization are wired into
ImportRun. Judge configuration can be captured but is not executed by this path;
TTS stays outside the ImportRun analysis path, where it is used only by the
Active Voice Test (API-Key V3 SSE, Issue #60). Credential values and signed URLs stay in memory, not snapshots.
Saving configuration does not make a service call. ASR errors occur inside its
stage after the original recording has been preserved.

## Official contract verified 2026-09-10

Started at the requested [product updates](https://docs.volcengine.com/docs/6561/2668039?lang=zh),
then followed API navigation to the current
[recording flash HTTP contract](https://docs.volcengine.com/docs/6561/2608628?lang=zh)
(page updated 2026-09-09). Browser verification was necessary because the web fetch
redirect failed. The current page specifies URL input. A historical page documents
base64, but this adapter does not assume it in the current contract.

The implemented synchronous POST uses the configured full endpoint
`https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash`,
model `bigmodel`, resource `volc.bigasr.auc_turbo`, new-console `X-Api-Key`,
UUID request ID and sequence `-1`. It requests utterances, disables ITN and
semantic smoothing, and retains punctuation. The provider limit is 100 MB / two
hours; the existing application limit remains 30 minutes with canonical WAV.
No unverified speaker-separation request flag is sent. Returned labels can be read, but the enabling flag, label semantics and real service availability remain unverified. No automatic tester/device assignment is claimed.

These values are a dated adapter contract, not permanent product requirements.
Unsupported model/endpoint/resource combinations fail explicitly rather than
silently sending another model. Service model weights/revision are not exposed.

## Configure locally

1. In Web model management add protocol `volcengine_asr`, model `bigmodel`, full
   endpoint above, purpose ASR and a new-console API key (write-only), or reference
   a credential environment variable available inside the container. Select it
   for the ASR route. Timeout can be configured up to 300 seconds.
   For clusters also bind the diarization route to the same enabled ASR profile.
   It consumes the existing ASR response without another upload/recognition;
   absent labels yield insufficient_evidence.
2. Supply `AIVOICEBENCH_AUDIO_PUT_URL`, `AIVOICEBENCH_AUDIO_GET_URL`, and
   `AIVOICEBENCH_AUDIO_HOST` in a local, ignored `.env`. Compose forwards these.
   PUT and GET must be expiring HTTPS URLs for the same private object on that
   host; the operator supplies them using their storage service. Do not put URLs
   or keys in Issues, reports, source files or command histories.
3. Import the authorized WAV/MP3/M4A. The publisher PUTs canonical WAV, reads it
   back and checks SHA256 before giving the URL to ASR. Redirects are rejected.
   Publication and recognition are serialized to prevent concurrent imports from
   overwriting the configured object while it is being consumed. Storage URL
   provisioning, expiry/cleanup and rotation are operator responsibilities; this
   milestone does not create a bucket, change an ACL or expose localhost audio.

The signed-URL publisher is replaceable. This configuration is required for cloud
calls, not for local import or Vosk. No audio is uploaded when ASR is unconfigured.
Local retry may be necessary after refreshing expired publication credentials.

CLI uses the same path with explicit cloud opt-in:

```text
python -m aivoicebench import conversation.m4a --model-settings artifacts/.model-settings
```

Existing `--asr-provider vosk --model-dir ... --model-version ...` remains an
explicit offline fallback; it is not silently substituted for a failed cloud call.

## Evidence, contracts and retry

- `original/` retains input bytes. `analysis/ANALYSIS-*/` contains normalization,
  native ASR, Transcript, acoustic/speaker/attribution/fusion/turn/timeline/metric envelopes, model snapshot, status outputs and reports.
  `manifest.json` selects the current revision. Reports are now revision-local;
  consumers should resolve their paths through the manifest.
- `provider-calls/CALL-*/start.json` is written before the operation; immutable
  `result.json` records duration, status, safe failure code and hashed output
  references. Attempts share an ASR operation ID for the Run. Transport failures
  are not retried automatically. Raw responses precede normalization, including
  service rejection. Responses echoing credentials are withheld with digest and
  reason rather than persisting secrets. HTTP code is not recognition success.
- Invocation foundation and signed-publication checks are selectively ported
  from #31/#32. The obsolete async job orchestration is not merged into main.
- Transcript **1.1.0** extends the existing contract for nullable remote model
  hash, absent word detail/confidence and overlapping utterances in start order.
  **1.0.0 remains strict** for legacy Vosk fixtures. Timing is provider-estimated,
  confidence/clock mapping unknown; numeric speaker IDs are not tester/device roles.
  Missing utterance times produce gaps; out-of-range times fail normalization.
- Explicit `POST /api/runs/{id}/resume` with `{"retry_asr": true}` preserves the
  previous manifest and outputs and creates another AnalysisRevision in that Run.
  It requires a verified canonical artifact; corrupt originals must be reimported.
  A completed ASR/report returns unchanged without another cloud request. Retry
  uses a new configuration snapshot. It is not generic human reanalysis (M1.5 engineering slice).
  Locks prevent simultaneous import/retry writes and release after process exit.
  A timed-out cloud attempt may already have been billed; Web labels retry accordingly.

## Verification boundaries

Targeted tests use synthetic audio and injected transport responses. They check
real codecs, source preservation, route capture, native responses, time bounds,
secret-safe failures, retry identity, immutable revisions, cross-process lock
semantics and history reopening. They do not measure Chinese ASR accuracy or
prove live cloud permission. Docker verification is separate from TestClient
reopening. Real M1 acceptance still requires authorized 5–20 minute terminal audio,
usable ASR/storage credentials, a real invocation and Windows-browser inspection.

## Stage states and evidence limits

When canonical audio exists, unconfigured ASR and unexecuted Judge/Findings may remain pending. Missing upstream evidence yields insufficient_evidence; operation errors remain failed. Unrun envelopes carry reasons and null data. A file existing on disk does not prove its processor ran.

Default Web/CLI has no role-editing input. Internal explicit mapping is tested with synthetic fixtures; no known roles means no role-dependent timings. Provider-estimated cluster boundaries retain their source; ambiguous overlap does not force an assignment.

write_import_report() creates an import-stage status report with empty conclusions and insufficient device-performance evidence. It does not run render_report()/Judge/Findings. Full semantic reports and human revisions remain product M1 work.
