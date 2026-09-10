# Expanded Latency Metrics

Issue #25. Extends the deterministic metric formulas and adds a
`compute_timeline_metrics()` function that extracts metric values from an
auto-generated EventTimeline (#24).

## Metric taxonomy (user directive section #5)

| Metric | Status | Source |
| --- | --- | --- |
| First Speech Latency | ✅ observed | `latency_ms(tester_end, device_start)` |
| Turn Gap | ✅ observed | `turn_gap_ms(device_end, next_tester_start)` |
| Barge-in Stop Latency | ✅ observed | `barge_in_stop_latency_ms(interrupt_start, device_end)` |
| Overlap Duration | ✅ observed | `overlap_duration_ms(overlap_start, overlap_end)` |
| Overlap Ratio | ✅ observed | `overlap_ratio(overlap, device_duration)` |
| False Endpoint | ✅ observed | `false_endpoint_detected(events)` |
| Barge-in Success | ✅ formula | `barge_success(stop, new_response)` (existing) |
| Feedback Latency | ⚠️ insufficient_evidence | Needs acoustic pattern / LLM |
| Meaningful Response Latency | ⚠️ insufficient_evidence | Needs ASR + LLM semantic analysis |

## Evidence-first

Every observed metric carries `evidence_ids` and `event_ids` linking to the
timeline events that produced it. LLM-dependent metrics return
`insufficient_evidence` with a reason — never a guessed time.

## Command

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench metrics artifacts/fusion/timeline.json
```

## Formulas added to `formulas.py`

- `turn_gap_ms(device_end, next_tester_start)` — gap between turns
- `barge_in_stop_latency_ms(interrupt_start, device_end)` — barge-in stop
- `overlap_duration_ms(start, end)` — single overlap duration
- `overlap_ratio(overlap, device_duration)` — fraction overlapping
- `false_endpoint_detected(events)` — boolean from event list
- `feedback_latency_status()` — insufficient_evidence (needs LLM)
- `meaningful_response_latency_status()` — insufficient_evidence (needs LLM)

## Next stages

- #10: LLM Harness to compute feedback and meaningful response latency
- Integration: `compute_timeline_metrics` into the import pipeline's
  `metrics` stage after fusion generates the timeline
