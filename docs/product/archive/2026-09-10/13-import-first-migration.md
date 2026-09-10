# Import-first migration and repository audit

Decision date: 2026-09-07. Primary workflow is imported recordings, not physical playback/capture. This is an adaptation of completed infrastructure, not a replacement implementation.

## Observed repository state

Remote refs refreshed. `main` and `origin/main` both point to `47a186501cc03e5baaeed18f317533441e51b9c8`. Issues #1–#11 remain open. PR metadata reports all #12–#19 open, mergeable, unmerged, with no requested reviewers; this is not a review approval. Each PR has one distinct commit, in the following ancestry:

| PR | Base | Head commit | Draft | PR-triggered CI run / outcome |
| --- | --- | --- | --- | --- |
| #12 | main | 07e19be | no | 34027283318 success |
| #13 | issue-1-testcase-contract | 2476a04 | no | 34028410587 success |
| #14 | issue-2-event-timeline | 8a716ff | no | 34028732323 success |
| #15 | issue-3-metric-contract | d2a9a36 | no | 34043917782 success |
| #16 | issue-4-finding-evidence | 689d8a6 | yes | 34044426996 success |
| #17 | issue-5-local-runner | a4272e5 | yes | 34071129967 success |
| #18 | issue-6-audio-station | 21dee2e | yes | 34071739425 success |
| #19 | issue-7-timestamped-asr | 167e5cc | yes | 34072446336 success |

Baseline validation on this Windows host: 132 tests passed. Tests demonstrate software contracts, synthetic arithmetic and adapters; they do not certify 5–20 minute real mixed-recording analysis. Vosk has transcribed a generated speech WAV. No target conversation has been analyzed and no physical microphone/loopback test has run.

The untested Issue #9 TTS draft is preserved in local stash `ad7f35350df8dba1afc0c3c858c40c90a4a6eac9` on `issue-9-golden-stimulus`; nothing from it enters the import branch. Do not apply it over unrelated work. It needs current API review and tests before future P2 use.

## Integration recommendation (not executed)

1. Owner review and explicitly authorize integrating #12→#15 in dependency order, retargeting each next PR to main after its parent lands. Preserve commit ancestry through merge commits to avoid duplicating the remainder of the stack; a squash policy instead needs planned rebases and new validation.
2. Review #16→#19 as bounded infrastructure. Separate unfinished physical acceptance into deferred #6; do not claim hardware completion merely to undraft PRs. Recheck diffs/tests and scope before integration.
3. New independent branches use the fixed `integration/import-analysis-foundation` snapshot at `167e5cc` and target it for compact review. No merge occurred when creating this reference. It is explicitly provisional, not a trusted replacement for main. Architecture and import PRs are siblings, not successive bases. After authorized foundation integration, retarget these PRs to main and resolve/test any documentation conflicts.
4. Do not make another serial stack. If a later code unit needs unmerged import changes, use the existing integration plan and request owner review rather than silently stacking indefinitely.

## Reuse and gaps

| Existing module | Direct reuse | Import adaptation |
| --- | --- | --- |
| TestCase / validators | optional scripted expectations, stable IDs | imports do not require a fabricated Case; optional evaluation profile |
| EventTimeline / Evidence | clocks, refs, scope, artifacts, uncertainty | recording-relative time base, artifact derivation, automatic segments/turns and new event taxonomy |
| MetricResult / formulas / engine | arithmetic, thresholds, count accounting, evidence checks | expanded latency; per-turn imported eligibility; preserve legacy definitions |
| Finding | suspected/verified distinction, confidence, review/regression | candidate generation and immutable human confirmation |
| Runner | digest, artifact files, isolated runs, JSON writing | import stage ledger and recoverable partial stages; old dry-run manifest remains valid |
| ASRProvider / Vosk | native/normalized outputs, external-ASR scope | 20-minute recordings, mature cloud API and shared invocation records |
| Audio Station | existing code and tests retained | no MVP development dependency; P3 |
| TTS draft | preserved separately | P2, reverify official contract before resuming |

## Issue migration and dependency order

| Issue | Decision | Current ownership |
| --- | --- | --- |
| #1–#4 | keep completed foundations / pending review | new schema versions extend, never discard, their behavior |
| #5 | keep runner, split new entry point, lower physical priority | #21 import orchestrator; scripted control P2/P3 |
| #6 | defer to P3 | preserve station/calibration; no new hardware work |
| #7 | retain and extend | #22 cloud ASR/diarization/provider contracts |
| #8 | retain deterministic consumer, split unfinished detection | #23 acoustic; #24 fusion/turns/events; #25 metrics |
| #9 | lower to P2 | frozen TTS assets after import path |
| #10 | retain, expand Harness/Judge | structured semantic decisions and finding candidates |
| #11 | retain report ownership | #21 minimal status report; #11 full evidence-linked report |

New Issues: #20 migration; #21 ingestion + canonical normalization + stage ledger; #22 uniform providers and cloud recognition; #23 acoustic candidates; #26 immutable revision contract; #24 speaker fusion/turns/events; #25 expanded metrics; #27 complete import/real-recording/Windows acceptance. Issues #21–#27 were created and #5–#11 annotated without erasing their original acceptance/history.

Execution order: #21 first; #22 and #23 can then proceed independently; define #26 before effective speaker/turn views in #24. #24 consumes #22/#23/#26. #25 consumes #24 and existing formulas. #10 consumes #22/#24; ambiguous semantic decisions may remain pending while chronological processing runs. #11 consumes the ledger/events/metrics/Judge. #27 is the integration gate, not a claim that an import placeholder report completes the MVP. P0 thin report is allowed to show pending P1 semantic work. Comparison/regression/agents/Golden are P2; station/remote HIL P3.

## Current official API verification

Visited the user-specified [product updates](https://docs.volcengine.com/docs/6561/2668039?lang=zh) in the browser (page updated 2026-08-31), then followed API navigation to [recording recognition flash HTTP](https://docs.volcengine.com/docs/6561/2608628?lang=zh), updated 2026-09-04. It documents the V3 flash recognition endpoint, API-key authentication, `volc.bigasr.auc_turbo`, `bigmodel`, file URL input, utterance output and millisecond timestamps. These are observed documentation values, not a permanently hard-coded provider choice or an actually tested call. The header resource/model and documented URL-input transport must be revalidated when #22 is implemented. Do not upload audio to an improvised public URL.

The page overview lists WAV/MP3/Ogg while its format field also lists M4A/AAC and other containers; use verified canonical WAV for the initial adapter rather than asserting all native containers work. Native diarization speaker labels are evidence for clustering, not tester/device ground truth. Keep semantic smoothing disabled so fillers/corrections are not discarded from evaluation input. No credential value was read, stored or committed; no cloud speech call occurred during this audit.

## Acceptance boundary

Real target: a user-supplied 5–20 minute recording → original/normalized evidence → timestamped ASR → tester/device/unknown segments → turns/events → eligible latency/interruption/overlap metrics → structured semantics/findings → JSON/Markdown with audio ranges → human revisions → repeatable versioned analysis. Format smoke tests use explicit synthetic signals. A missing provider, ambiguous mixed speaker or failed stage yields a durable partial Run, not fabricated precision or a passing device score.
