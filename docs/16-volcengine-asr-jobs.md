# Explicit cloud recording jobs — Issue #22 milestone

This adds configured object publication and Volcano standard asynchronous ASR job
operations. It reuses #30 invocation evidence. It does not yet normalize cloud
transcripts or generate acoustic events, role assignments, turns or findings.

## API verification

Official standard [submit](https://docs.volcengine.com/docs/6561/2606791?lang=zh)
and [query](https://docs.volcengine.com/docs/6561/2606792?lang=zh) contracts were
verified on September 7; their endpoint/config findings are in document 15.
On September 8, verified the current official
[status table](https://docs.volcengine.com/docs/6561/2611432?lang=zh), last updated
July 31: 20000000 means success, 20000001 processing, 20000002 queued, and
20000003 silent audio. The implementation maps processing/queued to partial and
silence to insufficient_evidence, never to device timeout or a passed evaluation.
HTTP 200 without a recognized service status is not accepted as success.

Selected standard resource: `volc.seedasr.auc`, model_name `bigmodel`, v3.
Chinese diarization uses show_utterances/enable_speaker_info and ssd_version 200,
ssd_mode 1 as a candidate long non-meeting configuration. ITN and semantic
smoothing are disabled to preserve spoken content. Service model accuracy/version
stability and terminal speaker separation remain unverified by a real API call.

## Explicit input publication

The caller provides expiring PUT/GET URLs on one explicitly configured HTTPS host,
addressing the same object path. This code does not create storage or permissions.
It uploads canonical WAV, reads back that object, checks byte SHA256, and retains a
publication proof linked to the local file before passing the GET URL to ASR.
No signed URL or credential is stored in the job/audit. Responses echoing known
authentication material are rejected rather than persisted as raw evidence.
Source limits: PCM16, 16 kHz, mono, nonempty, <=30 minutes and <=60 MB.
The local import pipeline in PR #29 remains responsible for WAV/MP3/M4A decoding.

Object expiry and cleanup are configured by the storage owner; this adapter does
not delete remote objects or change ACLs. Use private, time-limited access. Readback
proves the uploaded bytes at that time; it cannot prove a remote object was not
replaced later. Use unique immutable object names and sufficient URL lifetime for
the recognition job. No public bucket is required or created.

## Usage

Environment variables (values should stay in local credentials, not command history
or Git): `VOLCENGINE_API_KEY`, `AIVOICEBENCH_AUDIO_PUT_URL`,
`AIVOICEBENCH_AUDIO_GET_URL`, `AIVOICEBENCH_AUDIO_HOST`.
The submit command is an explicit cloud upload and billable API operation. Merely
importing an audio file or running tests does not invoke it.

```powershell
python -m aivoicebench.volcengine_asr submit artifacts/my-run/normalized.wav --root artifacts/my-run
python -m aivoicebench.volcengine_asr query artifacts/my-run/cloud-jobs/JOB-ID/job.json --root artifacts/my-run
```

Submit writes an immutable local job with request UUID before the HTTP submission.
After submission it retains a receipt, native response/hash and returned task ID.
Query uses that returned ID; when a submission outcome is unknown, its saved request
UUID permits a recovery query without automatically uploading or submitting again.
Rejected submissions require review before any new job. There is no automatic
billable retry. Query performs one bounded HTTP request and can be repeated by a
controller/user; pending results remain explicit. All requests have a 30-second
transport timeout and bounded response reads. Redirects are not followed.

Every call retains invocation metadata and native response bytes before interpreting
the service status. Resume verifies source, publication and native submission
hashes and rejects an unrelated task ID substituted into a receipt. Raw speaker
IDs and timestamps remain provider output; no tester/device assignment or acoustic
timing is claimed. CLI exit 0 means this provider operation completed, 2 means a
partial/insufficient result, and 1 means failure; none means MVP acceptance.

## Validation and remaining work

Twelve synthetic transport tests cover upload readback, submit/query, queue and
processing status, silence, unknown submission recovery without resubmit, returned
task ID, service failure, source/receipt tampering, destination validation, absent
credentials and credential echo protection. No external network is used by these
tests. Existing ASR contracts/providers and import code are retained.

Still required by #22: normalized cloud Transcript with nullable model hash and
confidence, diarization estimates/unknown-role evidence, Run-stage integration,
controller-driven bounded polling/cancellation and real cloud validation. Import
and this provider milestone remain separate pending dependency consolidation.

This branch has one explicit dependency on PR #31 to reuse its audit code rather
than duplicate it. Do not extend another serial chain from this branch. Following
owner-authorized integration, retarget/rebase on main and rerun integration tests.
No merge is authorized or performed by creating this branch.
