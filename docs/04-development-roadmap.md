# Development Roadmap — Import-first MVP

Updated 2026-09-07 by explicit product direction. Primary workflow: External Recording → Import → Normalize/QA → Acoustic + ASR/Diarization → Attribution/Fusion → Turns/Events/Timeline → Metrics → LLM Harness → Findings/Evidence → Human Verification → Report/Regression. Preserve the existing contracts, Runner, ASR/Vosk, deterministic engine and Audio Station. See `13-import-first-migration.md` for audited refs and Issue migration.

## P0 — establish the import path

1. #20 architecture, repository/PR audit and migration governance.
2. #21 immutable WAV/MP3/M4A import, canonical normalization, provenance, versioned stage ledger and recoverable partial Run; basic JSON/Markdown status report.
3. #22 unified invocation/provider layer plus current cloud ASR/diarization, preserving Vosk fallback. #23 independent acoustic segmentation with explicit uncertainty.
4. Define #26 annotation/revision contract early; #24 consumes it with ASR/acoustic evidence to create role assignments, turns/responses and events automatically.
5. #25 expand metrics without changing legacy meanings: feedback, first speech, meaningful response, turn gap, barge-in stop/new intent/composite success, false endpoint and overlap duration/ratio.
6. #27 integrate the complete pipeline and verify real-recording acceptance. #10 and #11 below are dependencies for full semantic/report acceptance; unavailable stages may be partial during intermediate milestones.

## P1 — semantic judgement and review

- #10 structured, provider-independent Harness/Judge: intent/turn relations, meaningful answer position, context/memory/instructions, reasoning/hallucination, persona/emotion/proactivity/safety, candidate findings. No invented timing/evidence/internal causes.
- #26 append-only text/speaker/boundary/association/finding corrections, effective views and reanalysis. Preserve original machine outputs and reviewer provenance.
- #11 complete evidence-linked JSON/Markdown report, clear machine/semantic/human status and audio ranges. No invented scores/release gates. The thin #21 report is a checkpoint, not full report acceptance.

## P2 — test generation and comparison

- #9 mature API TTS → frozen Golden assets with source/model/voice/config/hash and sample-exact inserted pauses. Currently paused draft retained.
- Scripted multi-turn controller, exploratory voice agent, minimized regression Cases.
- Version/device/supplier comparison, known-policy release gates and population/denominator-aware statistics.

## P3 — physical automation

- #6 integrated Audio Station, device playback/capture and loopback calibration; existing code/tests retained.
- Automatic HIL and remote Station. These do not block imported-recording analysis.

## Real MVP acceptance and Windows application

Give the program one real 5–20 minute tester + terminal recording. It must preserve/hash the source; standardize audio; transcribe with timestamps; automatically identify tester/device/unknown speech; build turns/events; compute eligible latency, interruption and overlap measurements; evaluate semantics; emit findings and JSON/Markdown; resolve every important conclusion to Run/Turn/Event/audio interval/Transcript/Evidence/processor or model version; allow human corrections; reproduce or explain differences between analysis revisions.

Confidence/uncertainty must remain honest. Unknown speaker is not forced into a role, ASR timestamps are not acoustic ground truth, silence at EOF is not automatically timeout, and unavailable semantic checks do not pass barge-in success. Synthetic codec/fixture tests and generated speech smoke tests are labeled separately from real acceptance. No actual user recording is available yet.

Ship a Windows executable/installer with Runs, Import (device/hardware/firmware/model/prompt/supplier/environment/notes), Analyze, waveform/transcript/turns/events/metrics/findings, evidence navigation and human review. Verify local launch and dependencies. Code, standalone commands and a placeholder report do not satisfy packaged MVP acceptance. Cloud providers may be configured; no server cluster is mandatory.

## Integration policy

PRs #12–#19 remain unmerged; CI succeeds but main is still the initial repository. Recommend owner-authorized review/integration of #12–#15 first, then scoped #16–#19. Never merge automatically. New architecture/import branches share fixed `integration/import-analysis-foundation` at `167e5cc` rather than extending the serial PR stack. After approved foundation integration, retarget to main and rerun relevant validation. Keep every logical work unit in its own Issue/branch/PR and record actual test, commit, push and PR evidence in the work log.
