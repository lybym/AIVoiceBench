"""LLM Harness for semantic evaluation.

Architecture (user directive section #6):
  Orchestrator → Context → Model → Structured Decision → Result

The LLM handles semantic understanding (intent, meaningful response,
conversation quality, finding candidates). It does NOT handle timing,
file I/O, or arithmetic — those stay in the Deterministic Engine.

Key principles:
- Schema-constrained structured output, not free text
- Every judgment is evidence-linked and confidence-scored
- LLM cannot invent timestamps, audio evidence, or device-internal latency
- Suspected causes remain hypotheses with attribution_confidence
  and requires_log_verification=True
- insufficient_evidence instead of guessing

Provider abstraction supports OpenAI, Doubao/Volcengine, or any
compatible service. MockLLMProvider enables testing without API keys.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import time
import uuid
from typing import Protocol

from .runner import write_json

# Common Chinese filler / feedback words that precede meaningful content
FILLER_PATTERNS = [
    r'^[嗯啊呃唉哦哈]+[，。…\s]*',
    r'^好的[，。…\s]*',
    r'^让我(看看|想想|查查)[，。…\s]*',
    r'^嗯[…，。]*好的[，。…\s]*',
    r'^(那个|这个|嗯)[，。…\s]*',
    r'^是的[，。…\s]*',
    r'^收到[，。…\s]*',
]


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LLMInvocation:
    """Immutable record of one LLM call (simplified from #30 provider audit)."""
    invocation_id: str
    provider: str
    model: str
    prompt_version: str
    started_at: str
    finished_at: str | None = None
    latency_ms: float | None = None
    status: str = "running"
    input_chars: int = 0
    output_chars: int = 0

    def to_dict(self):
        return {
            'invocation_id': self.invocation_id, 'provider': self.provider,
            'model': self.model, 'prompt_version': self.prompt_version,
            'started_at': self.started_at, 'finished_at': self.finished_at,
            'latency_ms': self.latency_ms, 'status': self.status,
            'input_chars': self.input_chars, 'output_chars': self.output_chars,
        }


class LLMProvider(Protocol):
    """Unified LLM provider interface.

    Implementations: MockLLMProvider (testing), OpenAIProvider (future),
    VolcengineLLMProvider (future). Every call must produce a JudgeResult-
    compatible dict and record an LLMInvocation.
    """

    def complete(self, system_prompt: str, user_prompt: str,
                 dimension: str, context: dict) -> tuple[dict, LLMInvocation]:
        """Call the model and return (structured_result, invocation_record)."""
        ...


class MockLLMProvider:
    """Deterministic provider for testing without API keys.

    Uses simple heuristics to simulate LLM semantic analysis.
    NOT a replacement for real LLM evaluation — results are low-confidence.
    """

    def __init__(self, model="mock-llm-v1", prompt_version="mock-1.0.0"):
        self.provider = "mock"
        self.model = model
        self.prompt_version = prompt_version

    def complete(self, system_prompt: str, user_prompt: str,
                 dimension: str, context: dict) -> tuple[dict, LLMInvocation]:
        inv = LLMInvocation(
            invocation_id="CALL-" + uuid.uuid4().hex,
            provider=self.provider, model=self.model,
            prompt_version=self.prompt_version, started_at=_utc_now(),
        )
        start = time.monotonic()

        if dimension == "meaningful_response":
            result = self._meaningful_response(context)
        elif dimension == "feedback_detection":
            result = self._feedback_detection(context)
        elif dimension == "intent":
            result = self._intent(context)
        elif dimension == "conversation_quality":
            result = self._conversation_quality(context)
        elif dimension == "finding_candidate":
            result = self._finding_candidate(context)
        else:
            result = {
                'decision': 'unknown', 'score': None, 'confidence': 0.3,
                'reason': f'MockLLMProvider does not support dimension: {dimension}',
                'status': 'insufficient_evidence',
            }

        inv.finished_at = _utc_now()
        inv.latency_ms = round((time.monotonic() - start) * 1000, 3)
        inv.status = "success"
        inv.input_chars = len(user_prompt)
        inv.output_chars = len(json.dumps(result, ensure_ascii=False))
        return result, inv

    def _strip_filler(self, text):
        """Return (filler_text, meaningful_text) by stripping common fillers."""
        if not text:
            return "", ""
        remaining = text
        total_stripped = ""
        changed = True
        while changed:
            changed = False
            for pattern in FILLER_PATTERNS:
                match = re.match(pattern, remaining)
                if match:
                    total_stripped += match.group()
                    remaining = remaining[match.end():]
                    changed = True
                    break
        return total_stripped, remaining

    def _meaningful_response(self, context):
        """Locate the first information-bearing point in a device response."""
        text = context.get('text', '')
        seg_start = context.get('device_speech_start_ms', 0)
        seg_end = context.get('device_speech_end_ms', 0)
        seg_duration = seg_end - seg_start

        if not text or not text.strip():
            return {
                'decision': 'no_text', 'score': None, 'confidence': 0.3,
                'reason': 'No ASR text available for the device response; '
                          'cannot locate meaningful content without transcript',
                'meaningful_response_start_ms': None,
                'status': 'insufficient_evidence',
            }

        filler, meaningful = self._strip_filler(text)
        if not meaningful:
            return {
                'decision': 'all_filler', 'score': None, 'confidence': 0.6,
                'reason': 'Entire response appears to be filler/feedback; '
                          'no meaningful content detected',
                'meaningful_response_start_ms': None,
                'status': 'insufficient_evidence',
            }

        # Estimate timestamp: proportion of filler in total text
        total_len = len(text)
        filler_ratio = len(filler) / total_len if total_len > 0 else 0
        meaningful_start_ms = seg_start + filler_ratio * seg_duration

        return {
            'decision': 'meaningful_content_located',
            'score': None,
            'confidence': 0.6,
            'reason': f'Filler "{filler.strip()}" precedes meaningful content '
                      f'"{meaningful[:20]}..." at estimated {filler_ratio:.0%} of response',
            'meaningful_response_start_ms': round(meaningful_start_ms, 3),
            'status': 'observed',
        }

    def _feedback_detection(self, context):
        """Detect non-speech feedback (filler, ack, thinking cue)."""
        text = context.get('text', '')
        seg_start = context.get('device_speech_start_ms', 0)
        seg_end = context.get('device_speech_end_ms', 0)

        filler, meaningful = self._strip_filler(text)
        if not filler:
            return {
                'decision': 'no_feedback', 'score': None, 'confidence': 0.5,
                'reason': 'No filler/feedback detected at response start',
                'feedback_type': None,
                'feedback_start_ms': None,
                'feedback_end_ms': None,
                'status': 'insufficient_evidence',
            }

        total_len = len(text)
        filler_ratio = len(filler) / total_len if total_len > 0 else 0
        feedback_end = seg_start + filler_ratio * (seg_end - seg_start)

        # Classify feedback type
        ftype = 'ack'
        if re.match(r'^[嗯啊呃]+', filler):
            ftype = 'filler'
        elif '让我' in filler or '看看' in filler:
            ftype = 'thinking_cue'

        return {
            'decision': 'feedback_detected',
            'score': None,
            'confidence': 0.6,
            'reason': f'Detected {ftype}: "{filler.strip()}"',
            'feedback_type': ftype,
            'feedback_start_ms': round(seg_start, 3),
            'feedback_end_ms': round(feedback_end, 3),
            'status': 'observed',
        }

    def _intent(self, context):
        """Classify the user's intent from transcript text."""
        text = context.get('tester_text', '')
        if not text:
            return {
                'decision': 'no_text', 'score': None, 'confidence': 0.3,
                'reason': 'No tester transcript available for intent classification',
                'intent_label': None,
                'status': 'insufficient_evidence',
            }

        text_lower = text.lower()
        if any(w in text for w in ['天气', '下雨', '温度']) or 'weather' in text_lower:
            label = 'weather_query'
        elif any(w in text for w in ['时间', '几点', '日期']) or 'time' in text_lower:
            label = 'time_query'
        elif any(w in text for w in ['播放', '音乐', '歌曲']) or 'play' in text_lower:
            label = 'media_playback'
        elif any(w in text for w in ['设置', '提醒', '闹钟']) or 'set' in text_lower:
            label = 'device_setting'
        elif any(w in text for w in ['为什么', '解释', '原因']) or 'why' in text_lower:
            label = 'reasoning_request'
        elif '?' in text or '？' in text:
            label = 'question'
        else:
            label = 'general_statement'

        return {
            'decision': label,
            'score': None,
            'confidence': 0.7,
            'reason': f'Classified intent as {label} from tester text',
            'intent_label': label,
            'status': 'observed',
        }

    def _conversation_quality(self, context):
        """Assess overall conversation quality from metrics."""
        metrics = context.get('metrics', [])
        events = context.get('events', [])

        observed = [m for m in metrics if m.get('status') == 'observed']
        if not observed:
            return {
                'decision': 'insufficient_data', 'score': None, 'confidence': 0.3,
                'reason': 'No observed metrics available for quality assessment',
                'status': 'insufficient_evidence',
            }

        # Simple heuristic: penalize high latency and overlaps
        penalty = 0
        for m in observed:
            name = m.get('name', '')
            val = m.get('value')
            if name == 'first_speech_latency_ms' and val and val > 2000:
                penalty += 0.2
            if name == 'overlap_ratio' and val and val > 0.3:
                penalty += 0.15

        false_endpoints = [e for e in events if e.get('type') == 'possible_false_endpoint']
        penalty += len(false_endpoints) * 0.1

        score = max(0.0, min(1.0, 0.85 - penalty))

        if score >= 0.7:
            decision = 'acceptable'
        elif score >= 0.5:
            decision = 'marginal'
        else:
            decision = 'poor'

        return {
            'decision': decision,
            'score': round(score, 3),
            'confidence': 0.5,
            'reason': f'Quality score {score:.2f} based on {len(observed)} metrics, '
                      f'{len(false_endpoints)} false endpoints',
            'status': 'observed',
        }

    def _finding_candidate(self, context):
        """Generate a finding candidate from metrics and events."""
        metrics = context.get('metrics', [])
        events = context.get('events', [])

        # Check for high latency
        latency_metrics = [m for m in metrics
                           if m.get('name') == 'first_speech_latency_ms'
                           and m.get('status') == 'observed'
                           and m.get('value') and m['value'] > 2000]
        if latency_metrics:
            m = latency_metrics[0]
            return {
                'decision': 'high_latency',
                'score': None,
                'confidence': 0.6,
                'reason': f'First speech latency {m["value"]}ms exceeds 2000ms threshold',
                'finding_severity': 'medium',
                'suspected_layer': 'llm',
                'attribution_confidence': 0.4,
                'requires_log_verification': True,
                'status': 'observed',
            }

        # Check for false endpoints
        feps = [e for e in events if e.get('type') == 'possible_false_endpoint']
        if feps:
            return {
                'decision': 'false_endpoint_detected',
                'score': None,
                'confidence': 0.6,
                'reason': f'{len(feps)} possible false endpoint(s) detected',
                'finding_severity': 'medium',
                'suspected_layer': 'endpoint',
                'attribution_confidence': 0.5,
                'requires_log_verification': True,
                'status': 'observed',
            }

        # Check for excessive overlap
        overlaps = [m for m in metrics
                    if m.get('name') == 'overlap_ratio'
                    and m.get('status') == 'observed'
                    and m.get('value') and m['value'] > 0.3]
        if overlaps:
            return {
                'decision': 'excessive_overlap',
                'score': None,
                'confidence': 0.6,
                'reason': f'Overlap ratio {overlaps[0]["value"]:.0%} exceeds 30%',
                'finding_severity': 'low',
                'suspected_layer': 'aec',
                'attribution_confidence': 0.3,
                'requires_log_verification': True,
                'status': 'observed',
            }

        return {
            'decision': 'no_finding', 'score': None, 'confidence': 0.5,
            'reason': 'No significant issues detected in metrics or events',
            'finding_severity': None,
            'suspected_layer': None,
            'attribution_confidence': None,
            'requires_log_verification': False,
            'status': 'insufficient_evidence',
        }


PROMPT_VERSIONS = {
    'meaningful_response': 'meaningful-v1.0.0',
    'feedback_detection': 'feedback-v1.0.0',
    'intent': 'intent-v1.0.0',
    'conversation_quality': 'quality-v1.0.0',
    'finding_candidate': 'finding-v1.0.0',
}

SYSTEM_PROMPTS = {
    'meaningful_response': (
        'You are an AI voice evaluation judge. Given a device response transcript '
        'with timestamps, identify the first point where meaningful content begins. '
        'Filler words (嗯, 啊, 好的, 让我看看) are NOT meaningful. Return the timestamp '
        'in milliseconds. Do not invent timestamps not present in the transcript.'
    ),
    'feedback_detection': (
        'You are an AI voice evaluation judge. Detect non-speech feedback at the '
        'start of a device response. Classify as: filler, ack, thinking_cue, '
        'non_speech_tone, or other. Return the feedback time range.'
    ),
    'intent': (
        'You are an AI voice evaluation judge. Classify the user intent from '
        'their speech transcript. Use a concise label (e.g., weather_query, '
        'time_query, media_playback, device_setting, reasoning_request).'
    ),
    'conversation_quality': (
        'You are an AI voice evaluation judge. Assess overall conversation '
        'quality from the provided metrics and events. Return a score 0-1 '
        'and a decision: acceptable, marginal, or poor.'
    ),
    'finding_candidate': (
        'You are an AI voice evaluation judge. Based on metrics and events, '
        'generate a finding candidate if any issues are detected. Include '
        'suspected_layer and severity. Suspected causes must be hypotheses, '
        'not confirmed root causes.'
    ),
}


class LLMJudge:
    """LLM Harness that evaluates semantic dimensions.

    Constructs prompts, calls the LLM provider, validates structured output,
    and produces JudgeResult 1.0.0 documents with evidence links.
    """

    def __init__(self, provider=None):
        self.provider = provider or MockLLMProvider()
        self.invocations = []

    def _build_judge_result(self, raw_result, inv, dimension, turn_id,
                            response_id, evidence_refs, event_refs, run_id=None):
        """Wrap provider output into a schema-compliant JudgeResult."""
        prompt_version = PROMPT_VERSIONS.get(dimension, 'unknown-1.0.0')
        return {
            'schema_version': '1.0.0',
            'judge_id': 'JUDGE-' + uuid.uuid4().hex,
            'run_id': run_id,
            'dimension': dimension,
            'turn_id': turn_id,
            'response_id': response_id,
            'decision': raw_result.get('decision', 'unknown'),
            'score': raw_result.get('score'),
            'confidence': raw_result.get('confidence', 0.5),
            'reason': raw_result.get('reason', ''),
            'meaningful_response_start_ms': raw_result.get('meaningful_response_start_ms'),
            'feedback_type': raw_result.get('feedback_type'),
            'feedback_start_ms': raw_result.get('feedback_start_ms'),
            'feedback_end_ms': raw_result.get('feedback_end_ms'),
            'intent_label': raw_result.get('intent_label'),
            'suspected_layer': raw_result.get('suspected_layer'),
            'attribution_confidence': raw_result.get('attribution_confidence'),
            'requires_log_verification': raw_result.get('requires_log_verification', False),
            'evidence_refs': evidence_refs,
            'event_refs': event_refs,
            'finding_severity': raw_result.get('finding_severity'),
            'model': inv.model,
            'prompt_version': prompt_version,
            'provider': inv.provider,
            'invocation_id': inv.invocation_id,
            'latency_ms': inv.latency_ms,
            'status': raw_result.get('status', 'observed'),
        }

    def judge_meaningful_response(self, turn, fused_segments, run_id=None):
        """Locate the first meaningful content in a device response."""
        device_segs = [s for s in fused_segments
                       if s['segment_id'] in turn.get('device_segment_ids', [])]
        if not device_segs:
            return self._insufficient('meaningful_response', turn,
                                      'No device response segments in this turn', run_id)

        seg = device_segs[0]
        context = {
            'text': seg.get('text'),
            'device_speech_start_ms': turn.get('device_speech_start_ms') or seg['start_ms'],
            'device_speech_end_ms': turn.get('device_speech_end_ms') or seg['end_ms'],
        }
        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
        raw, inv = self.provider.complete(
            SYSTEM_PROMPTS['meaningful_response'], user_prompt,
            'meaningful_response', context)
        self.invocations.append(inv)
        ev_refs = [seg.get('acoustic_segment_id', '')] if seg.get('acoustic_segment_id') else []
        return self._build_judge_result(
            raw, inv, 'meaningful_response',
            turn.get('turn_id'), turn.get('response_id'),
            ev_refs, [], run_id)

    def judge_feedback(self, turn, fused_segments, run_id=None):
        """Detect feedback at the start of a device response."""
        device_segs = [s for s in fused_segments
                       if s['segment_id'] in turn.get('device_segment_ids', [])]
        if not device_segs:
            return self._insufficient('feedback_detection', turn,
                                      'No device response segments', run_id)

        seg = device_segs[0]
        context = {
            'text': seg.get('text'),
            'device_speech_start_ms': seg['start_ms'],
            'device_speech_end_ms': seg['end_ms'],
        }
        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
        raw, inv = self.provider.complete(
            SYSTEM_PROMPTS['feedback_detection'], user_prompt,
            'feedback_detection', context)
        self.invocations.append(inv)
        return self._build_judge_result(
            raw, inv, 'feedback_detection',
            turn.get('turn_id'), turn.get('response_id'),
            [], [], run_id)

    def judge_intent(self, turn, fused_segments, run_id=None):
        """Classify the user's intent from their speech."""
        tester_segs = [s for s in fused_segments
                       if s['segment_id'] in turn.get('tester_segment_ids', [])]
        tester_text = ' '.join(s.get('text') or '' for s in tester_segs)
        if not tester_text.strip():
            return self._insufficient('intent', turn,
                                      'No tester transcript available', run_id)

        context = {'tester_text': tester_text}
        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
        raw, inv = self.provider.complete(
            SYSTEM_PROMPTS['intent'], user_prompt,
            'intent', context)
        self.invocations.append(inv)
        return self._build_judge_result(
            raw, inv, 'intent',
            turn.get('turn_id'), turn.get('response_id'),
            [], [], run_id)

    def assess_quality(self, metrics, events, run_id=None):
        """Assess overall conversation quality."""
        context = {'metrics': metrics, 'events': events}
        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
        raw, inv = self.provider.complete(
            SYSTEM_PROMPTS['conversation_quality'], user_prompt,
            'conversation_quality', context)
        self.invocations.append(inv)
        return self._build_judge_result(
            raw, inv, 'conversation_quality',
            None, None, [], [], run_id)

    def generate_finding_candidates(self, metrics, events, run_id=None):
        """Generate finding candidates from metrics and events."""
        context = {'metrics': metrics, 'events': events}
        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
        raw, inv = self.provider.complete(
            SYSTEM_PROMPTS['finding_candidate'], user_prompt,
            'finding_candidate', context)
        self.invocations.append(inv)
        return self._build_judge_result(
            raw, inv, 'finding_candidate',
            None, None, [], [e.get('event_id', '') for e in events
                             if e.get('event_id')], run_id)

    def _insufficient(self, dimension, turn, reason, run_id=None):
        return {
            'schema_version': '1.0.0',
            'judge_id': 'JUDGE-' + uuid.uuid4().hex,
            'run_id': run_id,
            'dimension': dimension,
            'turn_id': turn.get('turn_id') if turn else None,
            'response_id': turn.get('response_id') if turn else None,
            'decision': 'insufficient_evidence',
            'score': None, 'confidence': 0.3,
            'reason': reason,
            'requires_log_verification': False,
            'evidence_refs': [], 'event_refs': [],
            'model': self.provider.model,
            'prompt_version': PROMPT_VERSIONS.get(dimension, 'unknown'),
            'provider': self.provider.provider,
            'invocation_id': None, 'latency_ms': None,
            'status': 'insufficient_evidence',
            'meaningful_response_start_ms': None,
            'feedback_type': None, 'feedback_start_ms': None,
            'feedback_end_ms': None, 'intent_label': None,
            'suspected_layer': None, 'attribution_confidence': None,
            'finding_severity': None,
        }

    def evaluate_all(self, fused_doc, turns_doc, metrics_result, run_id=None):
        """Run all applicable judgments across all turns.

        Returns (judge_results, invocations).
        """
        results = []
        fused_segments = fused_doc.get('segments', [])
        turns = turns_doc.get('turns', [])
        metrics = metrics_result.get('metrics', [])
        events = []  # Would come from timeline; passed via context

        for turn in turns:
            # Intent
            results.append(self.judge_intent(turn, fused_segments, run_id))
            # Meaningful response
            results.append(self.judge_meaningful_response(turn, fused_segments, run_id))
            # Feedback
            results.append(self.judge_feedback(turn, fused_segments, run_id))

        # Conversation quality
        results.append(self.assess_quality(metrics, events, run_id))
        # Finding candidates
        results.append(self.generate_finding_candidates(metrics, events, run_id))

        return results, [inv.to_dict() for inv in self.invocations]


def judge_pipeline(fused_path, turns_path, metrics_path, output=None,
                   provider=None):
    """Run the LLM judge pipeline on fusion output.

    Reads fused-segments, turns, and metrics JSON files.
    Returns (judge_results, invocations).
    """
    from pathlib import Path
    fused_doc = json.loads(Path(fused_path).read_text(encoding='utf-8'))
    turns_doc = json.loads(Path(turns_path).read_text(encoding='utf-8'))
    metrics_result = json.loads(Path(metrics_path).read_text(encoding='utf-8'))

    judge = LLMJudge(provider)
    results, invocations = judge.evaluate_all(fused_doc, turns_doc, metrics_result)

    if output is not None:
        out = Path(output).resolve()
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / 'judge-results.json', {
            'schema_version': '1.0.0',
            'results': results,
            'invocations': invocations,
        })
        write_json(out / 'invocations.json', invocations)

    return results, invocations
