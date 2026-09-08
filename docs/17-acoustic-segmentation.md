# Acoustic Segmentation — signal-based speech detection

Issue #23. This module produces speech-segment *candidates* from raw audio
using frame-based energy VAD. Acoustic timing is signal-processor timing,
distinct from ASR estimated timing, diarization timing, LLM-inferred
semantic timing, and manual corrected timing. Segments carry no speaker
role.

## What this stage does

Reads a canonical WAV (PCM16LE / 16 kHz / mono), splits it into analysis
frames, computes per-frame RMS energy, estimates a noise floor (10th
percentile of frame energies), sets an adaptive threshold above that floor,
marks frames as speech/silence, merges short gaps, filters segments below
a minimum duration, and outputs segment candidates with timing, confidence,
uncertainty, and frame statistics.

Uses only the Python standard library (`wave`, `array`, `math`). Cloud or
model-based VAD can implement the same `AcousticSegmenter` Protocol.

## What this stage does NOT do

- Assign speaker roles (tester / device / unknown). That is diarization.
- Produce events or a timeline. That is fusion / turns / event detection.
- Use ASR timestamps. ASR timing is a separate source.
- Infer semantic boundaries (meaningful response start). That is LLM work.
- Claim sample-exact speech onset. The uncertainty is the hop resolution.

## Command

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench acoustic 'C:/recordings/normalized.wav' --output artifacts/acoustic
```

Optional parameters:

```
--frame-ms 30          # analysis frame size
--hop-ms 10            # hop between frames (also the timing uncertainty)
--threshold-factor 0.15  # fraction of active energy range above noise floor
--min-speech-ms 100    # minimum segment duration
--min-silence-ms 200   # minimum gap to split segments
--merge-gap-ms 80      # merge gaps shorter than this
--pre-roll-ms 0        # extend segment start backward
--post-roll-ms 0       # extend segment end forward
```

CLI exit code 0 = complete with segments, 2 = insufficient evidence (silent
or no qualifying segment), 1 = error (missing file, non-canonical format).

## Output schema

`AcousticSegments 1.0.0` (`schemas/acoustic-segments.schema.json`):

```json
{
  "schema_version": "1.0.0",
  "document_id": "ACOUSTIC-<uuid>",
  "source": { "path", "sha256", "duration_ms", "sample_rate_hz", "channels", "encoding" },
  "processor": { "method": "energy_vad", "processor_version": "1.0.0", "parameters": { ... } },
  "status": "complete | partial | insufficient_evidence",
  "reason": null,
  "segments": [
    {
      "segment_id": "SEG-0000",
      "start_ms": 500.0,
      "end_ms": 1000.0,
      "confidence": 0.82,
      "source": "acoustic",
      "method": "energy_vad",
      "uncertainty_ms": 10.0,
      "frame_stats": { "peak_rms", "mean_rms", "threshold_rms", "frame_count" }
    }
  ]
}
```

Validation: `python -m aivoicebench validate doc.json --kind acoustic-segments`
checks schema validity, segment ordering, non-overlap, audio-bounds, source
consistency, and status/segments consistency.

## Timing taxonomy

The user's product direction requires explicit distinction between:

| Source | Method | This stage? |
| --- | --- | --- |
| Acoustic timing | Energy VAD | Yes |
| ASR estimated timing | Provider timestamps | No (#7/#22) |
| Diarization timing | Speaker model | No (future #24) |
| LLM inferred semantic | Structured decisions | No (#10) |
| Manual corrected | Human annotation | No (#26) |

Every segment boundary carries `source: "acoustic"` and `uncertainty_ms`
equal to the hop resolution. The true speech onset could be anywhere within
one hop of the detected boundary. This is not sample-exact ground truth.

## Evidence and validation

Tests use synthetic PCM16 sine tones with exact silence gaps. They exercise
actual signal detection, not mock outputs. A fully silent recording yields
`insufficient_evidence`, not an invented segment. Non-canonical formats
(stereo, wrong sample rate) raise an explicit error.

This stage is a sibling of #21 (recording import) and #22 (cloud ASR) on the
fixed `167e5cc` integration baseline. It is independently testable. Future
integration into the import pipeline calls `EnergyVadSegmenter.segment()`
from the `acoustic` stage of `import_pipeline.py` after normalization.

## Next stages

- #24: fuse acoustic + ASR + diarization segments into unified segments,
  build turns, detect events (interruption, overlap, response, timeout).
- #25: expand latency metrics (feedback, meaningful response, barge-in
  success, false endpoint) consuming the fused timeline.
- #10: LLM Harness for semantic decisions (intent, meaningful response
  boundary, conversation quality).
