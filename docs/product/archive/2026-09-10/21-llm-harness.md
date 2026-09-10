# LLM Harness — Semantic Evaluation

Issue #10. Implements the LLM evaluation layer that handles semantic
understanding, complementing the Deterministic Engine (#8/#25).

## Architecture (user directive section #6)

```
Orchestrator → Context → Model → Structured Decision → Result
                              ↓
                    Schema-constrained output
```

The LLM handles: intent, meaningful response, feedback detection,
conversation quality, finding candidates. It does NOT handle timing,
file I/O, or arithmetic — those stay in the Deterministic Engine.

## Provider abstraction

```python
class LLMProvider(Protocol):
    def complete(self, system_prompt, user_prompt, dimension, context) -> (dict, LLMInvocation)
```

- **MockLLMProvider**: deterministic heuristics for testing (no API keys)
- Future: OpenAIProvider, VolcengineLLMProvider (same interface)

Every call records an immutable `LLMInvocation` with:
`invocation_id`, `provider`, `model`, `prompt_version`, `started_at`,
`finished_at`, `latency_ms`, `status`, `input_chars`, `output_chars`.

## Evaluation dimensions

| Dimension | What it does | Needs |
|---|---|---|
| `intent` | Classify user intent (weather_query, time_query, etc.) | Tester transcript |
| `meaningful_response` | Locate first information-bearing point | Device transcript + timestamps |
| `feedback_detection` | Detect filler/ack/thinking cue | Device transcript + timestamps |
| `conversation_quality` | Overall quality score (0-1) | Metrics + events |
| `finding_candidate` | Generate finding if issues detected | Metrics + events |

## Meaningful Response detection

The key capability that resolves `insufficient_evidence` from #25:

```
Device: "嗯……好的，让我看看。南京今天天气晴朗。"
                ↑ filler          ↑ meaningful content starts
```

The LLM classifies text segments as filler vs meaningful and returns
`meaningful_response_start_ms`. This enables:

```
meaningful_response_latency_ms = meaningful_response_start_ms - tester_speech_end_ms
```

In the functional test: tester ended at 1000ms, meaningful content starts
at 2346ms → **latency = 1346ms** [observed].

## Finding candidate generation

When metrics show issues, the judge generates finding candidates with:
- `finding_severity`: info/low/medium/high/critical
- `suspected_layer`: vad/endpoint/asr/aec/network/llm/prompt/...
- `attribution_confidence`: how confident the LLM is about the cause
- `requires_log_verification`: **always True** for suspected causes

LLM cannot claim a confirmed root cause from black-box audio. Suspected
causes are hypotheses that need device log verification.

## Command

```powershell
& ./.venv/Scripts/python.exe -m aivoicebench judge \
  artifacts/fusion/fused-segments.json \
  --turns artifacts/fusion/turns.json \
  --metrics artifacts/fusion/metrics.json \
  --output artifacts/judge
```

## Schema

`JudgeResult 1.0.0` (`schemas/judge-result.schema.json`):
- Dimension-specific required fields (allOf constraints)
- `suspected_layer` → must have `attribution_confidence` + `requires_log_verification`
- `meaningful_response` → must have `meaningful_response_start_ms`
- `feedback_detection` → must have `feedback_type`, `feedback_start_ms`, `feedback_end_ms`
- `intent` → must have `intent_label`
- `finding_candidate` → must have `finding_severity`, `suspected_layer`

Validation: `python -m aivoicebench validate doc.json --kind judge-result`

## Current limitations

1. **MockLLMProvider only** — real LLM providers (OpenAI, Volcengine) need
   API keys and implementation. The mock uses simple heuristics (filler
   word matching, keyword-based intent classification).
2. **No real LLM calls** — all results are deterministic mock outputs.
3. **No prompt engineering** — system prompts are templates, not
   optimized for a specific model.
4. **No streaming** — all calls are synchronous.
5. **No Finding confirmation** — candidates are generated but not linked
   to the Finding 2.0.0 lifecycle (that needs #11 report integration).

## Next steps

- Implement real LLM provider (OpenAI/Volcengine) with schema-constrained output
- Integrate `meaningful_response_start_ms` back into the metrics pipeline to
  resolve the `insufficient_evidence` latency
- Add Finding lifecycle integration (candidate → human review → confirmed)
- Add context/memory/instruction_following dimensions
