"""Real LLM providers for OpenAI-compatible APIs.

Supports:
- Volcengine Doubao (火山引擎豆包): base_url=https://ark.cn-beijing.volces.com/api/v3
- OpenAI: base_url=https://api.openai.com/v1

Both use the same OpenAI chat completions format.
API keys are read from environment variables (never committed to git).
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .llm import LLMInvocation, PROMPT_VERSIONS, SYSTEM_PROMPTS


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class OpenAICompatibleProvider:
    """LLM provider for any OpenAI-compatible API (OpenAI, Volcengine Doubao).

    Requires the `requests` package. API key is read from environment variables
    or passed explicitly — never stored in code.

    Config:
        api_key_env: environment variable name (e.g., "ARK_API_KEY")
        base_url: API endpoint base URL
        model: model/endpoint ID
        temperature: sampling temperature (default 0.3)
        max_tokens: max response tokens (default 4096)
    """

    def __init__(self, provider_name, base_url, model,
                 api_key_env="ARK_API_KEY", api_key=None,
                 temperature=0.3, max_tokens=4096, timeout_seconds=30,
                 prompt_version="openai-compatible-v1.0.0"):
        self.provider = provider_name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.prompt_version = prompt_version

    def _check_key(self):
        if not self.api_key:
            raise ValueError(
                f"API key not configured. Set the {self.api_key_env} environment variable "
                f"or provide it in config/aivoicebench.yaml. "
                f"See config/aivoicebench.example.yaml for setup instructions.")

    def complete(self, system_prompt, user_prompt, dimension, context):
        """Call the LLM API and return (structured_result, invocation_record).

        API or output-validation failures remain insufficient evidence; no mock
        fallback. The raw provider text is preserved either way, so a rejected
        judgment is auditable instead of invisible.
        """
        inv = LLMInvocation(
            invocation_id="CALL-" + uuid.uuid4().hex,
            provider=self.provider, model=self.model,
            prompt_version=PROMPT_VERSIONS.get(dimension, "unknown-1.0.0"),
            started_at=_utc_now(), endpoint=self.base_url,
        )
        start_time = time.monotonic()
        raw_text = None

        try:
            self._check_key()
            raw_text = self._call_api(
                system_prompt, user_prompt,
                response_format=self._judge_response_format(dimension),
            )
            result = self._parse_response(raw_text, dimension, context)
        except Exception as error:
            result = {'decision': 'unknown', 'score': None, 'confidence': 0.0,
                      'status': 'insufficient_evidence',
                      'reason': 'LLM call or structured output validation failed'}
            inv.status = 'failed'
            inv.failure_code = ('unavailable' if isinstance(error, ValueError)
                                and 'API key not configured' in str(error)
                                else 'invalid_output' if raw_text is not None
                                else 'provider_error')

        inv.finished_at = _utc_now()
        inv.latency_ms = round((time.monotonic() - start_time) * 1000, 3)
        if inv.status != 'failed':
            inv.status = 'success'
        inv.input_chars = len(user_prompt)
        inv.raw_response = raw_text
        inv.output_chars = len(json.dumps(result, ensure_ascii=False))
        return result, inv

    def complete_raw(self, system_prompt, user_prompt):
        """Call the LLM API and return the raw response text, without JudgeResult validation.

        For use cases that need a different output schema (e.g. the voice test
        agent), this returns the raw text so the caller can parse its own format.
        """
        inv = LLMInvocation(
            invocation_id="CALL-" + uuid.uuid4().hex,
            provider=self.provider, model=self.model,
            prompt_version="voice-test-agent-1.0.0",
            started_at=_utc_now(),
        )
        start_time = time.monotonic()
        try:
            self._check_key()
            raw_text = self._call_api(system_prompt, user_prompt)
            inv.status = 'success'
        except Exception as error:
            inv.status = 'failed'
            inv.finished_at = _utc_now()
            inv.latency_ms = round((time.monotonic() - start_time) * 1000, 3)
            raise
        inv.finished_at = _utc_now()
        inv.latency_ms = round((time.monotonic() - start_time) * 1000, 3)
        inv.input_chars = len(user_prompt)
        inv.output_chars = len(raw_text)
        return raw_text, inv
        if inv.status != 'failed':
            inv.status = 'success'
        inv.input_chars = len(user_prompt)
        inv.output_chars = len(json.dumps(result, ensure_ascii=False))
        return result, inv

    def _judge_response_format(self, dimension):
        """Use a strict schema only for the verified Bailian Qwen model family.

        Other OpenAI-compatible endpoints keep JSON Object mode. A request or
        output failure is still reported by ``complete``; there is no retry or
        silent downgrade after a schema request fails.
        """
        endpoint = urlsplit(self.base_url)
        model = self.model.lower()
        supported_model = model == 'qwen3.8-flash' or model.startswith('qwen3.8-flash-')
        bailian_endpoint = ((endpoint.hostname or '').endswith('.aliyuncs.com')
                            and endpoint.path.rstrip('/').endswith('/compatible-mode/v1'))
        if not (supported_model and bailian_endpoint and dimension in SYSTEM_PROMPTS):
            return {'type': 'json_object'}

        properties = {
            'decision': {'type': 'string'},
            'reason': {'type': 'string'},
            'status': {'type': 'string', 'enum': [
                'observed', 'low_confidence', 'insufficient_evidence']},
            'confidence': {'type': 'number'},
            'evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
            'event_refs': {'type': 'array', 'items': {'type': 'string'}},
        }
        required = list(properties)
        if dimension in ('meaningful_response', 'feedback_detection'):
            roles = (['meaningful_start'] if dimension == 'meaningful_response'
                     else ['feedback_start', 'feedback_end'])
            properties['anchor_refs'] = {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'role': {'type': 'string', 'enum': roles},
                        'anchor_id': {'type': 'string'},
                    },
                    'required': ['role', 'anchor_id'],
                    'additionalProperties': False,
                },
            }
            required.append('anchor_refs')
        if dimension == 'feedback_detection':
            properties['feedback_type'] = {
                'type': ['string', 'null'],
                'enum': ['filler', 'ack', 'thinking_cue', 'non_speech_tone', 'other', None],
            }
            required.append('feedback_type')
        elif dimension == 'intent':
            properties['intent_label'] = {'type': ['string', 'null']}
            required.append('intent_label')
        elif dimension == 'conversation_quality':
            properties['score'] = {'type': ['number', 'null']}
            required.append('score')
        elif dimension == 'finding_candidate':
            properties.update({
                'finding_severity': {
                    'type': ['string', 'null'],
                    'enum': ['info', 'low', 'medium', 'high', 'critical', None],
                },
                'suspected_layer': {
                    'type': ['string', 'null'],
                    'enum': ['vad', 'endpoint', 'asr', 'aec', 'network', 'llm',
                             'prompt', 'context', 'memory', 'agent', 'tool', 'tts',
                             'safety', 'persona', 'unknown', None],
                },
                'attribution_confidence': {'type': ['number', 'null']},
                'requires_log_verification': {'type': 'boolean'},
            })
            required.extend(('finding_severity', 'suspected_layer',
                             'attribution_confidence', 'requires_log_verification'))
        elif dimension in ('semantic_response', 'barge_in_compliance'):
            # Omit the verdict when abstaining. The parser still requires a
            # boolean for observed status and rejects an explicit null on
            # low_confidence responses.
            properties['semantic_decision'] = {'type': 'boolean'}

        return {
            'type': 'json_schema',
            'json_schema': {
                'name': f'judge_{dimension}',
                'strict': True,
                'schema': {
                    'type': 'object',
                    'properties': properties,
                    'required': required,
                    'additionalProperties': False,
                },
            },
        }

    def _call_api(self, system_prompt, user_prompt, *, response_format=None):
        """Make the actual HTTP call to the OpenAI-compatible API."""
        import requests

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout_seconds,
            allow_redirects=False,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _parse_response(self, text, dimension, context):
        """Parse LLM response text into structured JudgeResult fields.

        Malformed decisions, unreferenced claims, and invented timing are rejected
        so the caller can preserve insufficient evidence. The model may *select* a
        measured boundary; it may never author a millisecond.
        """
        import math
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError('Structured decision must be an object')
        if any(not isinstance(parsed.get(k), str) or not parsed[k].strip()
               for k in ('decision', 'reason', 'status')):
            raise ValueError('Missing decision fields')
        confidence = parsed.get('confidence')
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError('Invalid confidence')
        if parsed['status'] not in ('observed', 'low_confidence', 'insufficient_evidence'):
            raise ValueError('Invalid decision status')
        if parsed.get('score') is not None and (type(parsed['score']) not in (int, float)
                or not math.isfinite(parsed['score']) or not 0 <= parsed['score'] <= 1):
            raise ValueError('Invalid score')
        for field in ('feedback_type', 'intent_label'):
            value = parsed.get(field)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f'Invalid {field}')
        if parsed.get('feedback_type') not in (
                None, 'filler', 'ack', 'thinking_cue', 'non_speech_tone', 'other'):
            raise ValueError('Invalid feedback type')
        if parsed.get('finding_severity') not in (None, 'info', 'low', 'medium',
                                                   'high', 'critical'):
            raise ValueError('Invalid finding severity')
        if parsed.get('suspected_layer') not in (None, 'vad', 'endpoint', 'asr',
                                                  'aec', 'network', 'llm', 'prompt',
                                                  'context', 'memory', 'agent', 'tool',
                                                  'tts', 'safety', 'persona', 'unknown'):
            raise ValueError('Invalid suspected layer')
        attribution_confidence = parsed.get('attribution_confidence')
        if attribution_confidence is not None and (
                type(attribution_confidence) not in (int, float)
                or not math.isfinite(attribution_confidence)
                or not 0 <= attribution_confidence <= 1):
            raise ValueError('Invalid attribution confidence')
        if ('requires_log_verification' in parsed
                and type(parsed['requires_log_verification']) is not bool):
            raise ValueError('Invalid log verification flag')
        for field in ('evidence_refs', 'event_refs'):
            refs = parsed.get(field)
            if refs is not None and (not isinstance(refs, list)
                                     or any(not isinstance(ref, str) or not ref.strip()
                                            for ref in refs)
                                     or len(refs) != len(set(refs))):
                raise ValueError(f'Invalid {field}')
        # The model selects an anchor id from the supplied measured boundaries.
        # Model-authored times cannot become observable audio measurements.
        if any(parsed.get(k) is not None for k in ('meaningful_response_start_ms',
                'feedback_start_ms', 'feedback_end_ms')):
            raise ValueError('Semantic timing requires verified anchor selection')
        allowed_anchors = {anchor.get('anchor_id') for anchor in context.get('anchors') or ()
                           if isinstance(anchor, dict)}
        if (dimension not in ('meaningful_response', 'feedback_detection')
                and 'anchor_refs' in parsed):
            raise ValueError('Anchor selections are not valid for this dimension')
        selections = parsed.get('anchor_refs')
        if selections is not None and not isinstance(selections, list):
            raise ValueError('Invalid anchor selections')
        for selection in selections or ():
            if not isinstance(selection, dict) or selection.get('anchor_id') not in allowed_anchors:
                raise ValueError('Anchor selection names an unmeasured boundary')
            if set(selection) != {'role', 'anchor_id'}:
                raise ValueError('Anchor selection must contain only role and anchor_id')
        if parsed['status'] == 'observed':
            if dimension == 'conversation_quality' and parsed.get('score') is None:
                raise ValueError('Observed conversation quality needs a score')
            if dimension == 'meaningful_response' and (
                    len(selections or ()) != 1
                    or (selections or ())[0].get('role') != 'meaningful_start'):
                raise ValueError('Observed meaningful response needs one measured anchor')
            if dimension == 'feedback_detection' and (
                    len(selections or ()) != 2
                    or {selection.get('role') for selection in selections}
                    != {'feedback_start', 'feedback_end'}
                    or len({selection['anchor_id'] for selection in selections}) != 2):
                raise ValueError('Observed feedback needs two distinct measured anchors')
            if dimension in ('semantic_response', 'barge_in_compliance') and (
                    type(parsed.get('semantic_decision')) is not bool):
                raise ValueError('Observed semantic verdict needs a boolean decision')
            if dimension == 'intent' and not parsed.get('intent_label'):
                raise ValueError('Observed intent needs an intent label')
            if dimension == 'feedback_detection' and not parsed.get('feedback_type'):
                raise ValueError('Observed feedback needs a feedback type')
            if dimension == 'finding_candidate' and (
                    parsed.get('finding_severity') is None
                    or parsed.get('suspected_layer') is None):
                raise ValueError('Observed finding needs severity and suspected layer')
            if parsed.get('suspected_layer') is not None:
                if attribution_confidence is None:
                    raise ValueError('Observed suspected layer needs attribution confidence')
                if parsed.get('requires_log_verification') is not True:
                    raise ValueError('Observed suspected layer needs log verification')
        if parsed['status'] != 'insufficient_evidence':
            allowed_evidence = context.get('evidence_refs', [])
            allowed_events = context.get('event_refs', [])
            if 'semantic_decision' in parsed and type(parsed['semantic_decision']) is not bool:
                raise ValueError('A semantic decision must be a boolean')
            refs = parsed.get('evidence_refs')
            event_refs = parsed.get('event_refs')
            cited = [item for item in (refs or []) if isinstance(item, str)]
            cited_events = [item for item in (event_refs or []) if isinstance(item, str)]
            if any(item not in allowed_evidence for item in cited):
                raise ValueError('Decision cites unreferenced evidence')
            if any(item not in allowed_events for item in cited_events):
                raise ValueError('Decision cites an unreferenced event')
            if not cited and not cited_events and not parsed.get('anchor_refs'):
                raise ValueError('Decision lacks verified evidence references')
        return parsed


class VolcengineLLMProvider(OpenAICompatibleProvider):
    """火山引擎豆包 LLM provider.

    Setup:
    1. Register at https://console.volcengine.com/ark
    2. Complete real-name verification
    3. Create an inference endpoint at:
       https://console.volcengine.com/ark/region:ark+cn-beijing/model
    4. Set environment variable: ARK_API_KEY=your_key
    5. Set model to your endpoint ID (e.g., "ep-xxxxxxxx")
    """

    def __init__(self, model=None, api_key=None, **kwargs):
        super().__init__(
            provider_name="volcengine",
            base_url=kwargs.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
            model=model or kwargs.get("model", ""),
            api_key_env="ARK_API_KEY",
            api_key=api_key,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 4096),
            prompt_version=kwargs.get("prompt_version", "volcengine-v1.0.0"),
        )


class OpenAILLMProvider(OpenAICompatibleProvider):
    """OpenAI LLM provider.

    Set environment variable: OPENAI_API_KEY=your_key
    """

    def __init__(self, model="gpt-4o", api_key=None, **kwargs):
        super().__init__(
            provider_name="openai",
            base_url=kwargs.get("base_url", "https://api.openai.com/v1"),
            model=model,
            api_key_env="OPENAI_API_KEY",
            api_key=api_key,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 4096),
            prompt_version=kwargs.get("prompt_version", "openai-v1.0.0"),
        )


def create_provider_from_config(config):
    """Create an LLM provider from a config dict.

    Supports configured cloud providers or unavailable mode. No mock fallback.
    """
    from .llm import UnavailableLLMProvider

    provider_name = config.get("llm_provider", "none")

    if provider_name == "volcengine":
        vc = config.get("volcengine", {})
        return VolcengineLLMProvider(
            model=vc.get("model", ""),
            api_key=vc.get("api_key"),
            base_url=vc.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
            temperature=vc.get("temperature", 0.3),
            max_tokens=vc.get("max_tokens", 4096),
        )
    elif provider_name == "openai":
        oc = config.get("openai", {})
        return OpenAILLMProvider(
            model=oc.get("model", "gpt-4o"),
            api_key=oc.get("api_key"),
            base_url=oc.get("base_url", "https://api.openai.com/v1"),
            temperature=oc.get("temperature", 0.3),
            max_tokens=oc.get("max_tokens", 4096),
        )
    else:
        return UnavailableLLMProvider()
