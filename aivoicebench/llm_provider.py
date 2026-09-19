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
            raw_text = self._call_api(system_prompt, user_prompt)
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

    def _call_api(self, system_prompt, user_prompt):
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
                or not math.isfinite(parsed['score'])):
            raise ValueError('Invalid score')
        # The model selects an anchor id from the supplied measured boundaries.
        # Model-authored times cannot become observable audio measurements.
        if any(parsed.get(k) is not None for k in ('meaningful_response_start_ms',
                'feedback_start_ms', 'feedback_end_ms')):
            raise ValueError('Semantic timing requires verified anchor selection')
        allowed_anchors = {anchor.get('anchor_id') for anchor in context.get('anchors') or ()
                           if isinstance(anchor, dict)}
        for selection in parsed.get('anchor_refs') or ():
            if not isinstance(selection, dict) or selection.get('anchor_id') not in allowed_anchors:
                raise ValueError('Anchor selection names an unmeasured boundary')
            if any(key in selection for key in ('start_ms', 'end_ms')):
                raise ValueError('Anchor selection must not carry model-authored timing')
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
