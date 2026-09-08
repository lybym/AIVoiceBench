# Segment Fusion, Turn Building, and Event Detection

Issue #24. This module bridges acoustic segments (#23) and optional ASR
transcripts (#7/#22) to produce fused segments with speaker roles,
conversational turns, detected events, and an auto-generated EventTimeline.

## Pipeline

```
AcousticSegments (#23)  ─┐
                         ├─→ fuse() ─→ FusedSegments ─→ build_turns() ─→ Turns
ASR Transcript (#7/#22) ─┘                                                    │
                                                                             ├─→ detect_events() ─→ Events + Evidence
                                                                             │
                                                                             └─→ generate_timeline() ─→ EventTimeline 2.0.0
```

## What this stage does

1. **Fuse**: Merges acoustic segment timing with optional ASR text. Applies
   the alternating heuristic for speaker attribution (tester/device/unknown)
   on single-channel mixed recordings. Each segment records `timing_source`
   (acoustic / fused), `speaker_source` (heuristic), and `speaker_confidence`.

2. **Build Turns**: Groups fused segments into conversational turns. Each
   turn pairs a tester speech segment with the following device response.
   Detects interruptions (tester speaks during device speech) and overlaps.

3. **Detect Events**: Generates Event 2.0.0 events from fused segments and
   turns:
   - `tester_speech_start` / `tester_speech_end`
   - `device_speech_start` / `device_speech_end`
   - `silence` (gaps between segments, duration event)
   - `timeout` (silence exceeding configurable threshold)
   - `overlap_start` / `overlap_end` (tester + device simultaneous)
   - `interrupt_start` (tester speaks during device speech)
   - `response_start` / `response_end` (device response boundaries)
   - `possible_false_endpoint` (short tester segment + immediate device)

4. **Generate Timeline**: Builds an EventTimeline 2.0.0 with tracks,
   artifacts, evidence, and events. Evidence references link every event
   to its source audio time range.

## What this stage does NOT do

- Claim high-confidence speaker attribution. The alternating heuristic is
  confidence 0.5 and explicitly marked as requiring human or diarization
  verification.
- Infer semantic boundaries (meaningful response start). That is LLM work (#10).
- Compute latency metrics. That is #25.
- Overwrite original ASR or acoustic data. All outputs are derived documents.

## Command

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench fusion artifacts/acoustic/ACOUSTIC-xxx.json --output artifacts/fusion
```

Optional ASR transcript and tuning:

```
--transcript artifacts/asr/ASR-xxx/transcript.json
--timeout-ms 5000      # silence above this becomes timeout
--false-endpoint-ms 300  # tester segment shorter than this + immediate device = false endpoint
```

CLI exit code 0 = complete timeline, 2 = partial/insufficient, 1 = error.

## Schemas

- `FusedSegments 1.0.0` (`schemas/fused-segments.schema.json`): unified
  segments with speaker_role, speaker_confidence, speaker_source,
  timing_source, text, acoustic_segment_id, asr_segment_id.
- `Turns 1.0.0` (`schemas/turns.schema.json`): turn_id, tester/device
  segment_ids, response_id, speech timing, has_interruption, has_overlap.
- `Event 2.0.0` (extended): added `silence`, `response_start`, `response_end`,
  `possible_false_endpoint` event types.
- `EventTimeline 2.0.0` (unchanged schema, auto-generated content).

Validation: `python -m aivoicebench validate doc.json --kind fused-segments`
or `--kind turns` or `--kind timeline`.

## Timing taxonomy

Every event carries `source` and `confidence`:

| Event type | Source | Confidence | Notes |
| --- | --- | --- | --- |
| tester/device speech boundaries | audio_signal | 0.7 | Acoustic VAD timing |
| silence, timeout | derived | 0.8-0.9 | Computed from segment gaps |
| overlap boundaries | derived | 0.8 | Computed from segment intersection |
| response boundaries | derived | 0.6 | Links device start/end to turn |
| possible_false_endpoint | derived | 0.5 | Heuristic, needs review |

Speaker attribution confidence is 0.5 (alternating heuristic). This is
distinct from event timing confidence.

## Evidence-first

Every event has at least one `evidence_id` linking to a time range in the
source audio. Events can be traced: Event → Evidence → Audio artifact →
SHA256. No event is created without acoustic evidence.

Insufficient evidence (no acoustic segments) yields `insufficient_evidence`
status and a partial timeline with gaps, not invented events.

## Dependency

This branch builds on `issue-23-acoustic-segmentation` (which has the
acoustic module and schema). PR #24 depends on PR #23. After #23 is merged,
this branch should be rebased to the new base.

## Next stages

- #25: expanded latency metrics consuming the fused timeline (feedback
  latency, meaningful response latency, barge-in success, overlap ratio).
- #10: LLM Harness for semantic decisions (intent, meaningful response
  boundary, conversation quality).
- Speaker diarization provider to replace the alternating heuristic with
  higher-confidence attribution.
