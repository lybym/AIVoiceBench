# AIVoiceBench contributor / agent rules

## Read before implementation

1. Read [docs/PRD.md](docs/PRD.md), especially the applicable Requirement IDs, status legend and product acceptance.
2. Read relevant architecture, metric definitions and schemas; consult the current Issue and actual source/ref.
3. Use the work log for execution history only. docs/product/archive is historical evidence, never active requirements.

## Authority and changes

- The user's explicit instructions take priority. Synchronize newly authorized product decisions into the PRD in the same PR; do not ask again for authorization already given.
- PRD defines product behavior and scope; technical docs define implementation constraints; schemas define data contracts; Issues bound work units. If they disagree, state and resolve the conflict instead of silently choosing an old document or changing the product to fit code.
- Every product behavior change references PRD-F/M/N IDs in its Issue and PR. Update affected acceptance conditions and implementation status with code/test/ref evidence. Documentation-only and maintenance changes may say no behavior change and identify relevant requirements.
- Code implementation, main integration, Release publication and real-recording acceptance are different states. Never claim completion from module existence, an Issue closure, a fixture pass or a published image alone.
- Requirement IDs are stable. Do not duplicate product requirements into context, roadmap or work log; link to the PRD. Keep architecture/methodology/metric details in their technical documents.

## Engineering and governance

- Extend existing TestCase, Timeline, Evidence, MetricResult, Finding, Runner, ASR/Vosk, validators and deterministic engine. Preserve Audio Station as the P3 extension.
- Evidence first: no invented timing, speaker roles, semantic proof, internal latency or verified root cause. Separate synthetic, imported and physical-device evidence.
- One logical Issue/branch/PR; minimize dependency chains. Do not merge without explicit user authorization. Preserve unrelated worktrees/rebases and user changes.
- Never commit credentials, real private recordings or generated personal reports. Verify current official provider APIs before implementation; no hard-coded historical model/voice assumptions.
- For asynchronous paid providers, persist the provider request/job ID before polling. Resume an unfinished invocation by querying that ID before any resubmission; never hide a second billable submit behind retry/restart behavior.
- Poll-window exhaustion for an asynchronous job is a recoverable pending state, not a terminal failure. Keep the job queryable across resume/restart until the provider returns a terminal status or an explicit user action authorizes a new submit.
- A `partial` ASR result may still contain valid utterance, timestamp and anonymous speaker-label evidence. Preserve usable evidence plus explicit gaps; do not discard the whole transcript or block downstream diarization solely because the stage is not `complete`.
- Recording Analysis must not use an LLM to assign tester/device roles. Preserve anonymous speaker clusters, wait for an explicit user mapping, store it as a new revision, and run role-dependent metrics/final reporting only after that manual decision.
- Acoustic boundaries and ASR speaker spans remain independent evidence. Alignment must record overlap/coverage and retain unmatched, conflict and unknown states; never copy the nearest role merely to populate metrics.
- Run checks appropriate to the change, document results/limitations in docs/06-work-log.md, commit/push and open a PR. Do not claim an unrun test or deployment. Documentation-only changes need link/requirement-ID/evidence-reference checks, not redundant audio tests or a Docker release.
- Deliver Docker backend/frontend for Windows-browser use, not a Windows executable. Product acceptance remains the PRD gate.
