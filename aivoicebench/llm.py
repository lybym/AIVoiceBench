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
import hashlib
import json
import re
import time
import uuid
from typing import Protocol

from .runner import write_json
from .semantic_evidence import (
    CRITERIA_VERSION, SEMANTIC_CRITERIA, anchor_for, build_judge_profile,
    criterion_for, resolve_anchors, turn_anchors, turn_evidence,
)

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
    """Immutable record of one LLM call (simplified from #30 provider audit).

    ``raw_response`` preserves the provider's own output next to the validated
    result, so a rejected or downgraded judgment stays auditable. Credentials are
    never part of this record.
    """
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
    endpoint: str | None = None
    failure_code: str | None = None
    raw_response: str | None = None

    def to_dict(self):
        return {
            'invocation_id': self.invocation_id, 'provider': self.provider,
            'model': self.model, 'prompt_version': self.prompt_version,
            'endpoint': self.endpoint,
            'started_at': self.started_at, 'finished_at': self.finished_at,
            'latency_ms': self.latency_ms, 'status': self.status,
            'failure_code': self.failure_code,
            'input_chars': self.input_chars, 'output_chars': self.output_chars,
            'raw_response': self.raw_response,
            'raw_response_sha256': (hashlib.sha256(self.raw_response.encode('utf-8')).hexdigest()
                                    if self.raw_response is not None else None),
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


class UnavailableLLMProvider:
    """Production default when no semantic provider is configured."""

    def __init__(self):
        self.provider = 'unavailable'
        self.model = 'none'

    def complete(self, system_prompt, user_prompt, dimension, context):
        now = _utc_now()
        invocation = LLMInvocation('CALL-' + uuid.uuid4().hex, 'unavailable', 'none',
                                   PROMPT_VERSIONS.get(dimension, 'unknown-1.0.0'), now,
                                   finished_at=now, latency_ms=0, status='not_configured')
        return {'decision': 'unknown', 'score': None, 'confidence': 0.0,
                'status': 'insufficient_evidence', 'reason': 'No semantic provider configured'}, invocation


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
        elif dimension == "semantic_response":
            result = self._semantic_verdict(context)
        elif dimension == "barge_in_compliance":
            result = self._semantic_verdict(context)
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
        # The fixture's own structured output is preserved exactly like a real
        # provider's raw text, so the same audit path covers both.
        inv.raw_response = json.dumps(result, ensure_ascii=False)
        inv.output_chars = len(inv.raw_response)
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
        """Select an existing measured boundary for the first meaningful content.

        The mock estimates *which* supplied anchor the transition falls on; it
        never authors a millisecond. With no usable interior boundary the honest
        answer is insufficient evidence, not an interpolated number.
        """
        text = context.get('text', '')
        anchors = context.get('anchors') or []
        if not text or not text.strip():
            return {
                'decision': 'no_text', 'score': None, 'confidence': 0.3,
                'reason': 'No ASR text available for the device response; '
                          'cannot locate meaningful content without transcript',
                'status': 'insufficient_evidence',
            }

        filler, meaningful = self._strip_filler(text)
        if not meaningful:
            return {
                'decision': 'all_filler', 'score': None, 'confidence': 0.6,
                'reason': 'Entire response appears to be filler/feedback; '
                          'no meaningful content detected',
                'status': 'insufficient_evidence',
            }

        if not anchors:
            return {
                'decision': 'no_measured_boundary', 'score': None, 'confidence': 0.3,
                'reason': 'No measured boundary is available inside this turn; a semantic '
                          'estimate cannot become an acoustic timestamp',
                'status': 'insufficient_evidence',
            }

        seg_start = context.get('device_speech_start_ms')
        if seg_start is None:
            seg_start = anchors[0]['start_ms']
        seg_end = context.get('device_speech_end_ms')
        if seg_end is None:
            seg_end = anchors[-1]['end_ms']
        span = seg_end - seg_start
        filler_ratio = len(filler) / len(text) if text else 0
        transition = seg_start + filler_ratio * span
        selected = next((anchor for anchor in anchors if anchor['start_ms'] >= transition), None)
        if selected is None:
            return {
                'decision': 'no_measured_boundary_after_filler', 'score': None, 'confidence': 0.4,
                'reason': 'Filler precedes meaningful content, but no measured boundary exists at '
                          'or after that transition; the response start cannot be timestamped',
                'status': 'insufficient_evidence',
            }

        return {
            'decision': 'meaningful_content_located',
            'score': None,
            'confidence': 0.6,
            'reason': f'Filler "{filler.strip()}" precedes meaningful content '
                      f'"{meaningful[:20]}..."; selected measured boundary {selected["anchor_id"]}',
            'anchor_refs': [{'role': 'meaningful_start', 'anchor_id': selected['anchor_id']}],
            'event_refs': [selected['anchor_id']],
            'status': 'observed',
        }

    def _feedback_detection(self, context):
        """Detect non-speech feedback at the start of a device response.

        A feedback interval needs two measured boundaries. If the turn has no
        second usable boundary the interval cannot be measured, and the mock
        abstains rather than reporting a filler-length estimate as a time range.
        """
        text = context.get('text', '')
        device_start = context.get('device_speech_start_ms')
        anchors = [anchor for anchor in (context.get('anchors') or [])
                   if anchor.get('start_ms') is not None
                   and (device_start is None or anchor['start_ms'] >= device_start)]
        filler, _meaningful = self._strip_filler(text)
        if not filler:
            return {
                'decision': 'no_feedback', 'score': None, 'confidence': 0.5,
                'reason': 'No filler/feedback detected at response start',
                'status': 'insufficient_evidence',
            }
        if len(anchors) < 2:
            return {
                'decision': 'no_measured_interval', 'score': None, 'confidence': 0.4,
                'reason': 'A feedback interval needs two measured boundaries in this turn; '
                          'a filler-length estimate is not a time range',
                'status': 'insufficient_evidence',
            }

        start_anchor, end_anchor = anchors[0], anchors[1]
        if end_anchor['start_ms'] <= start_anchor['start_ms']:
            return {
                'decision': 'degenerate_interval', 'score': None, 'confidence': 0.4,
                'reason': 'The two measured boundaries in this turn are not a nonempty interval',
                'status': 'insufficient_evidence',
            }
        ftype = 'ack'
        if re.match(r'^[嗯啊呃]+', filler):
            ftype = 'filler'
        elif '让我' in filler or '看看' in filler:
            ftype = 'thinking_cue'

        return {
            'decision': 'feedback_detected',
            'score': None,
            'confidence': 0.6,
            'reason': f'Detected {ftype}: "{filler.strip()}" between measured boundaries '
                      f'{start_anchor["anchor_id"]} and {end_anchor["anchor_id"]}',
            'feedback_type': ftype,
            'anchor_refs': [{'role': 'feedback_start', 'anchor_id': start_anchor['anchor_id']},
                            {'role': 'feedback_end', 'anchor_id': end_anchor['anchor_id']}],
            'event_refs': [start_anchor['anchor_id'], end_anchor['anchor_id']],
            'status': 'observed',
        }

    def _semantic_verdict(self, context):
        """Return a boolean semantic decision for a constrained criterion.

        Software fixture only: the overlap heuristic below stands in for a model,
        so every decision is low confidence and is still gated on the caller
        supplying citable evidence. Without a citable reference the fixture
        abstains exactly as a real provider must.
        """
        tester_text = (context.get('tester_text') or '').strip()
        device_text = (context.get('device_text') or '').strip()
        if not context.get('evidence_refs') and not context.get('event_refs'):
            return {
                'decision': 'no_citable_evidence', 'score': None, 'confidence': 0.3,
                'reason': 'No citable evidence/event reference exists for this turn; a semantic '
                          'verdict without a citation is not evidence',
                'status': 'insufficient_evidence',
            }
        if not tester_text or not device_text:
            return {
                'decision': 'no_text', 'score': None, 'confidence': 0.3,
                'reason': 'Both the tester request and the device response need transcript text '
                          'before a semantic verdict is possible',
                'status': 'insufficient_evidence',
            }
        _filler, meaningful_device = self._strip_filler(device_text)
        shared = {character for character in tester_text if character.strip()} & \
                 {character for character in meaningful_device if character.strip()}
        decision = len(shared) >= 2
        return {
            'decision': 'semantic_decision', 'score': None, 'confidence': 0.35,
            'semantic_decision': decision,
            'reason': ('Fixture overlap heuristic: tester and device text share '
                       f'{len(shared)} content characters; this is not a calibrated model judgment'),
            'evidence_refs': list(context.get('evidence_refs') or []),
            'event_refs': list(context.get('event_refs') or []),
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
            'evidence_refs': list(context.get('evidence_refs') or []),
            'event_refs': list(context.get('event_refs') or []),
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
            'evidence_refs': list(context.get('evidence_refs') or []),
            'event_refs': list(context.get('event_refs') or []),
            'status': 'observed',
        }

    def _finding_candidate(self, context):
        """Generate a finding candidate from metrics and events.

        The candidate cites the events/evidence of the metric or event it relied
        on. A candidate that cannot name what it rested on stays insufficient
        evidence, because a Finding without a resolvable citation is not one.
        """
        metrics = context.get('metrics', [])
        events = context.get('events', [])

        def citation(source):
            return {'evidence_refs': list(source.get('evidence_ids') or []),
                    'event_refs': list(source.get('event_ids') or [])}

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
                **citation(m),
            }

        # Check for false endpoints
        feps = [e for e in events if e.get('type') == 'possible_false_endpoint']
        if feps:
            evidence_ids, event_ids = [], []
            for event in feps:
                evidence_ids.extend(event.get('evidence_ids') or [])
                if event.get('event_id'):
                    event_ids.append(event['event_id'])
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
                'evidence_refs': list(dict.fromkeys(evidence_ids)),
                'event_refs': list(dict.fromkeys(event_ids)),
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
                **citation(overlaps[0]),
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
    'meaningful_response': 'meaningful-v1.1.0',
    'feedback_detection': 'feedback-v1.1.0',
    'intent': 'intent-v1.0.0',
    'conversation_quality': 'quality-v1.0.0',
    'finding_candidate': 'finding-v1.0.0',
    'semantic_response': 'semantic-response-v1.0.0',
    'barge_in_compliance': 'barge-in-compliance-v1.0.0',
}

#: Dimensions whose timing output is a *selection* from measured anchors. These
#: never accept a model-authored millisecond.
ANCHOR_SELECTION_DIMENSIONS = ('meaningful_response', 'feedback_detection')

SYSTEM_PROMPTS = {
    'meaningful_response': (
        'You are an AI voice evaluation judge. Given a device response transcript '
        'and the measured boundaries that exist inside this turn, select the anchor '
        'whose boundary is the first point where meaningful content begins. Filler '
        'words (嗯, 啊, 好的, 让我看看) are NOT meaningful. Return "anchor_refs" with '
        'role "meaningful_start" and the chosen anchor_id. Never return milliseconds '
        'and never name a boundary that was not supplied.'
    ),
    'feedback_detection': (
        'You are an AI voice evaluation judge. Detect non-speech feedback at the '
        'start of a device response. Classify as: filler, ack, thinking_cue, '
        'non_speech_tone, or other. Select two supplied anchors with roles '
        '"feedback_start" and "feedback_end". Never return milliseconds.'
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
        'not confirmed root causes. Cite the evidence/event ids you relied on.'
    ),
    'semantic_response': (
        'You are an AI voice evaluation judge applying criterion CRIT-SEMANTIC-RESPONSE. '
        'Decide whether the device response is semantically relevant and meaningful for '
        'the tester request. Return a boolean "semantic_decision" and cite the supplied '
        'evidence/event ids you relied on. If the supplied evidence is not enough to '
        'decide, return status "insufficient_evidence" and say why. Never invent timing.'
    ),
    'barge_in_compliance': (
        'You are an AI voice evaluation judge applying criterion CRIT-BARGE-IN-COMPLIANCE. '
        'Decide whether the response that follows an interruption honours the new tester '
        'intent. Return a boolean "semantic_decision" and cite the supplied evidence/event '
        'ids you relied on. Interruption timing alone never establishes compliance. If the '
        'supplied evidence is not enough to decide, abstain with a reason.'
    ),
}


class LLMJudge:
    """LLM Harness that evaluates semantic dimensions.

    Constructs prompts, calls the LLM provider, validates structured output, and
    produces JudgeResult 1.0.0 documents with evidence links.

    Two invariants hold for every result this class emits:

    * timing comes from a measured anchor the provider *selected*, never from
      model-authored milliseconds;
    * an observed result cites evidence that exists in the judged Timeline. When
      either invariant cannot be met the result is downgraded to an explicit
      abstention with a reason instead of being reported as a verdict.
    """

    def __init__(self, provider=None, config_version=None):
        self.provider = provider or UnavailableLLMProvider()
        self.config_version = config_version
        self.invocations = []
        self.abstentions = []

    def judge_profile(self, dimension):
        """Provider/model/prompt/criteria provenance for one dimension."""
        return build_judge_profile(
            self.provider.provider, self.provider.model,
            PROMPT_VERSIONS.get(dimension, 'unknown-1.0.0'),
            CRITERIA_VERSION,
            endpoint=getattr(self.provider, 'base_url', None),
            config_version=self.config_version)

    def _turn_context(self, turn, timeline, role=None):
        """Allowed evidence, events and anchors for one judgment.

        A judge may only cite what this returns. For a per-turn judgment the scope
        is that turn's own events (optionally restricted to one speaker role); a
        run-level judgment (quality, finding candidate) may cite any object of the
        same Timeline. Everything else is out of scope by construction, so an
        unreferenced claim cannot be assembled later.
        """
        if not isinstance(timeline, dict):
            return [], [], []
        if turn is None:
            evidence_ids = [item.get('evidence_id') for item in timeline.get('evidence') or ()
                            if item.get('evidence_id')]
            event_ids = [item.get('event_id') for item in timeline.get('events') or ()
                         if item.get('event_id')]
            anchors = sorted((anchor for anchor in
                              (self._event_anchor(event) for event in timeline.get('events') or ())
                              if anchor and anchor['usable']),
                             key=lambda item: (item['start_ms'], item['anchor_id']))
            return evidence_ids, event_ids, anchors
        evidence_ids, event_ids = turn_evidence(turn, timeline, role)
        anchors = [anchor for anchor in turn_anchors(turn, timeline, role) if anchor['usable']]
        return evidence_ids, event_ids, anchors

    @staticmethod
    def _event_anchor(event):
        from .semantic_evidence import NON_ANCHOR_EVENT_SOURCES
        if not isinstance(event, dict) or not event.get('event_id'):
            return None
        return {
            'anchor_id': event['event_id'],
            'event_type': event.get('type'),
            'start_ms': event.get('start_ms'),
            'end_ms': event.get('end_ms'),
            'source': event.get('confidence_source') or event.get('source') or 'event',
            'usable': bool(event.get('evidence_ids'))
                    and event.get('source') not in NON_ANCHOR_EVENT_SOURCES,
        }

    def _abstain(self, dimension, turn, response_id, reason, invocation_id=None,
                 state='insufficient_evidence'):
        self.abstentions.append({
            'dimension': dimension,
            'turn_id': turn.get('turn_id') if isinstance(turn, dict) else None,
            'response_id': response_id,
            'state': state,
            'reason': reason,
            'invocation_id': invocation_id,
        })

    def _build_judge_result(self, raw_result, inv, dimension, turn_id,
                            response_id, evidence_refs, event_refs, run_id=None,
                            profile=None, anchor_refs=None, abstention_reason=None):
        """Wrap provider output into a schema-compliant JudgeResult.

        `abstention_reason` is the harness's own reason when it downgraded an
        otherwise-`observed` provider answer. The provider's rationale stays in
        `reason`, so a reader can see both what the model claimed and why the
        engine refused to accept it.
        """
        prompt_version = PROMPT_VERSIONS.get(dimension, 'unknown-1.0.0')
        status = raw_result.get('status', 'observed')
        result = {
            'schema_version': '1.0.0',
            'judge_id': 'JUDGE-' + uuid.uuid4().hex,
            'run_id': run_id,
            'dimension': dimension,
            'turn_id': turn_id,
            'response_id': response_id,
            'decision': raw_result.get('decision') or 'unknown',
            'score': raw_result.get('score'),
            'confidence': raw_result.get('confidence', 0.5),
            'reason': raw_result.get('reason') or 'No reason supplied',
            'meaningful_response_start_ms': None,
            'feedback_type': raw_result.get('feedback_type'),
            'feedback_start_ms': None,
            'feedback_end_ms': None,
            'intent_label': raw_result.get('intent_label'),
            'suspected_layer': raw_result.get('suspected_layer'),
            'attribution_confidence': raw_result.get('attribution_confidence'),
            'requires_log_verification': raw_result.get('requires_log_verification', False),
            'evidence_refs': list(evidence_refs),
            'event_refs': list(event_refs),
            'finding_severity': raw_result.get('finding_severity'),
            'model': inv.model,
            'prompt_version': prompt_version,
            'provider': inv.provider,
            'invocation_id': inv.invocation_id,
            'latency_ms': inv.latency_ms,
            'status': status,
            'criterion_id': None,
            'criterion_version': None,
            'judge_profile': profile,
            'semantic_decision': None,
            'anchor_refs': [],
            'abstention_reason': None,
        }
        if isinstance(raw_result.get('anchor_refs'), list):
            result['anchor_refs'] = [resolved for resolved in anchor_refs or []]
        criterion = criterion_for(dimension)
        if criterion is not None:
            result['criterion_id'] = criterion['criterion_id']
            result['criterion_version'] = criterion['criterion_version']
            result['semantic_decision'] = raw_result.get('semantic_decision')
        if status != 'observed':
            result['abstention_reason'] = (abstention_reason or raw_result.get('reason')
                                           or 'The judge abstained')
            result['semantic_decision'] = None
            result['anchor_refs'] = []
            result['meaningful_response_start_ms'] = None
            result['feedback_start_ms'] = None
            result['feedback_end_ms'] = None
            result['feedback_type'] = None
        self._apply_attribution(result)
        return result

    def _apply_attribution(self, result):
        """A suspected layer is always a hypothesis that needs device logs.

        The harness never accepts `requires_log_verification=False` for a named
        layer, and never lets a missing attribution confidence look certain.
        """
        layer = result.get('suspected_layer')
        if layer is None:
            return
        confidence = result.get('attribution_confidence')
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            confidence = 0.0
        # A hypothesis is never certain: a self-reported 1.0 is capped, and device
        # logs stay required until a separate verification path clears them.
        result['attribution_confidence'] = min(float(confidence), 0.99)
        result['requires_log_verification'] = True

    def _judge(self, dimension, turn, response_id, context, run_id=None,
               timeline=None, role=None):
        """Run one dimension and return a validated JudgeResult.

        Every failure path produces an abstention: a downgraded result that says
        what was missing, never a silent `unknown` that reads like a verdict.
        """
        profile = self.judge_profile(dimension)
        evidence_ids, event_ids, anchors = self._turn_context(turn, timeline, role)
        context = dict(context)
        context['evidence_refs'] = evidence_ids
        context['event_refs'] = event_ids
        context['anchors'] = anchors
        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)
        raw, inv = self.provider.complete(
            SYSTEM_PROMPTS.get(dimension, ''), user_prompt, dimension, context)
        self.invocations.append(inv)

        selected, problems = resolve_anchors(raw.get('anchor_refs'), anchors)
        # The harness owns the citation set: it accepts only references that exist
        # in the judged scope, and expands a selected event to that event's own
        # evidence so metric/finding linkage stays consistent.
        allowed_evidence = set(evidence_ids)
        allowed_events = set(event_ids)
        claimed_evidence = [ref for ref in (raw.get('evidence_refs') or [])
                            if ref in allowed_evidence]
        claimed_events = [ref for ref in (raw.get('event_refs') or [])
                          if ref in allowed_events]
        for anchor in selected:
            if anchor['anchor_id'] not in claimed_events:
                claimed_events.append(anchor['anchor_id'])
        for event_id in claimed_events:
            event = next((item for item in (timeline or {}).get('events') or ()
                          if item.get('event_id') == event_id), None)
            for evidence_id in (event or {}).get('evidence_ids') or ():
                if evidence_id in allowed_evidence and evidence_id not in claimed_evidence:
                    claimed_evidence.append(evidence_id)

        status = raw.get('status', 'observed')
        provider_status = status
        if status == 'observed' and inv.status != 'success':
            status = 'insufficient_evidence'
            problems.append('the provider invocation did not succeed')
        if status == 'observed':
            if dimension in ANCHOR_SELECTION_DIMENSIONS:
                needed = (('feedback_start', 'feedback_end') if dimension == 'feedback_detection'
                          else ('meaningful_start',))
                if any(anchor_for(selected, anchor_role) is None for anchor_role in needed):
                    status = 'insufficient_evidence'
                    problems.append('no measured anchor was selected for ' + ', '.join(needed))
            if dimension in SEMANTIC_CRITERIA and type(raw.get('semantic_decision')) is not bool:
                status = 'insufficient_evidence'
                problems.append('a semantic verdict requires a boolean decision')
            if not claimed_evidence and not claimed_events:
                status = 'insufficient_evidence'
                problems.append('no citable evidence or event reference exists in the judged scope')

        # A harness downgrade is the actionable cause and becomes the abstention
        # reason; a provider that abstained on its own keeps its own rationale.
        downgrade = ('; '.join(problems) if problems and status != 'observed'
                     and provider_status == 'observed' else None)
        result = self._build_judge_result(
            dict(raw, status=status), inv, dimension,
            turn.get('turn_id') if isinstance(turn, dict) else None,
            response_id, claimed_evidence, claimed_events, run_id, profile, selected,
            abstention_reason=downgrade)
        if status == 'observed':
            if dimension == 'meaningful_response':
                anchor = anchor_for(selected, 'meaningful_start')
                if anchor is not None:
                    result['meaningful_response_start_ms'] = anchor['start_ms']
            if dimension == 'feedback_detection':
                start = anchor_for(selected, 'feedback_start')
                end = anchor_for(selected, 'feedback_end')
                if start is not None and end is not None:
                    result['feedback_start_ms'] = start['start_ms']
                    result['feedback_end_ms'] = end['end_ms']
                    if result['feedback_end_ms'] <= result['feedback_start_ms']:
                        result['status'] = 'insufficient_evidence'
                        result['abstention_reason'] = ('the selected anchors are not a nonempty '
                                                       'feedback interval')
                        result['feedback_start_ms'] = None
                        result['feedback_end_ms'] = None
                        problems.append(result['abstention_reason'])
        if result['status'] != 'observed':
            reason = result.get('abstention_reason') or '; '.join(problems) or 'abstained'
            result['abstention_reason'] = reason
            self._abstain(dimension, turn, response_id, reason, inv.invocation_id)
        return result

    def evaluate_turn(self, turn, fused_segments, timeline=None, run_id=None):
        """Judge one turn across every per-turn dimension."""
        device_segs = [segment for segment in fused_segments
                       if segment.get('segment_id') in turn.get('device_segment_ids', [])]
        tester_segs = [segment for segment in fused_segments
                       if segment.get('segment_id') in turn.get('tester_segment_ids', [])]
        device_text = ' '.join(segment.get('text') or '' for segment in device_segs)
        tester_text = ' '.join(segment.get('text') or '' for segment in tester_segs)
        response_id = turn.get('response_id')
        common = {
            'tester_text': tester_text,
            'device_text': device_text,
            'device_speech_start_ms': turn.get('device_speech_start_ms'),
            'device_speech_end_ms': turn.get('device_speech_end_ms'),
        }
        anchored = dict(common, text=device_text)
        return [
            self._judge('intent', turn, response_id, {'tester_text': tester_text},
                        run_id, timeline, role='tester'),
            self._judge('meaningful_response', turn, response_id, anchored, run_id, timeline,
                        role='device'),
            self._judge('semantic_response', turn, response_id, common, run_id, timeline),
            self._judge('feedback_detection', turn, response_id, anchored, run_id, timeline,
                        role='device'),
        ]

    def evaluate(self, fused_doc, turns_doc, metrics_result, timeline=None, run_id=None,
                 analysis_id=None):
        """Run the full Judge and return one JudgeResults 1.0.0 artifact."""
        fused_segments = fused_doc.get('segments', [])
        turns = turns_doc.get('turns', [])
        metrics = metrics_result.get('metrics', [])
        events = (timeline or {}).get('events', [])
        results = []
        for turn in turns:
            results.extend(self.evaluate_turn(turn, fused_segments, timeline, run_id))
            if turn.get('has_interruption'):
                results.append(self._judge('barge_in_compliance', turn,
                                           turn.get('response_id'),
                                           {'tester_text': ' '.join(
                                               segment.get('text') or ''
                                               for segment in fused_segments
                                               if segment.get('segment_id')
                                               in turn.get('interrupting_segment_ids', [])),
                                            'device_text': ' '.join(
                                               segment.get('text') or ''
                                               for segment in fused_segments
                                               if segment.get('segment_id')
                                               in turn.get('device_segment_ids', []))},
                                           run_id, timeline))
        if results:
            results.append(self._judge('conversation_quality', None, None,
                                       {'metrics': metrics, 'events': events},
                                       run_id, timeline))
        results.append(self._judge('finding_candidate', None, None,
                                   {'metrics': metrics, 'events': events}, run_id, timeline))
        return self.judge_document(results, run_id, analysis_id)

    def judge_document(self, results, run_id=None, analysis_id=None):
        """Assemble the artifact that carries results, raw output and abstentions."""
        invocations = [invocation.to_dict() for invocation in self.invocations]
        profile = self.judge_profile('semantic_response')
        return {
            'schema_version': '1.0.0',
            'run_id': run_id,
            'analysis_id': analysis_id,
            'criteria_version': CRITERIA_VERSION,
            'judge_profile': {
                'provider': profile['provider'],
                'model': profile['model'],
                'criteria_version': CRITERIA_VERSION,
                'endpoint': getattr(self.provider, 'base_url', None),
                'config_version': self.config_version,
                'prompt_versions': dict(PROMPT_VERSIONS),
            },
            'results': results,
            'invocations': invocations,
            'abstentions': list(self.abstentions),
        }

    def evaluate_all(self, fused_doc, turns_doc, metrics_result, run_id=None, timeline=None):
        """Backward-compatible entry point returning (results, invocations)."""
        document = self.evaluate(fused_doc, turns_doc, metrics_result, timeline, run_id)
        return document['results'], document['invocations']


def judge_pipeline(fused_path, turns_path, metrics_path, output=None, provider=None,
                   timeline_path=None, run_id=None, analysis_id=None):
    """Run the LLM judge pipeline on fusion output.

    A Timeline is optional only in the sense that a caller without one gets
    abstentions; it is never optional for an observed semantic verdict.
    """
    from pathlib import Path
    fused_doc = json.loads(Path(fused_path).read_text(encoding='utf-8'))
    turns_doc = json.loads(Path(turns_path).read_text(encoding='utf-8'))
    metrics_result = json.loads(Path(metrics_path).read_text(encoding='utf-8'))
    timeline = None
    if timeline_path is not None:
        timeline = json.loads(Path(timeline_path).read_text(encoding='utf-8'))

    judge = LLMJudge(provider)
    document = judge.evaluate(fused_doc, turns_doc, metrics_result, timeline, run_id,
                              analysis_id)

    if output is not None:
        out = Path(output).resolve()
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / 'judge-results.json', document)
        # The raw responses stay in their own artifact so a reader can audit a
        # rejected judgment without parsing the validated result list.
        write_json(out / 'judge-raw.json', {
            'schema_version': '1.0.0',
            'run_id': run_id,
            'invocations': document['invocations'],
        })

    return document
