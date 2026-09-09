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
                 temperature=0.3, max_tokens=4096,
                 prompt_version="openai-compatible-v1.0.0"):
        self.provider = provider_name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.prompt_version = prompt_version

    def _check_key(self):
        if not self.api_key:
            raise ValueError(
                f"API key not configured. Set the {self.api_key_env} environment variable "
                f"or provide it in config/aivoicebench.yaml. "
                f"See config/aivoicebench.example.yaml for setup instructions.")

    def complete(self, system_prompt, user_prompt, dimension, context):
        """Call the LLM API and return (structured_result, invocation_record).

        API or output-validation failures remain insufficient evidence; no mock fallback.
        """
        inv = LLMInvocation(
            invocation_id="CALL-" + uuid.uuid4().hex,
            provider=self.provider, model=self.model,
            prompt_version=PROMPT_VERSIONS.get(dimension, "unknown-1.0.0"),
            started_at=_utc_now(),
        )
        start_time = time.monotonic()

        try:
            self._check_key()
            raw_text = self._call_api(system_prompt, user_prompt)
            result = self._parse_response(raw_text, dimension, context)
        except Exception:
            result = {'decision': 'unknown', 'score': None, 'confidence': 0.0,
                      'status': 'insufficient_evidence',
                      'reason': 'LLM call or structured output validation failed'}
            inv.status = 'failed'

        inv.finished_at = _utc_now()
        inv.latency_ms = round((time.monotonic() - start_time) * 1000, 3)
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
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _parse_response(self, text, dimension, context):
        """Parse LLM response text into structured JudgeResult fields.

        Malformed decisions, unreferenced claims, and invented timing
        are rejected so the caller can preserve insufficient evidence.
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
        # The existing context does not supply validated timing anchor IDs yet.
        # Model-authored times cannot become observable audio measurements.
        if any(parsed.get(k) is not None for k in ('meaningful_response_start_ms',
                'feedback_start_ms', 'feedback_end_ms')):
            raise ValueError('Semantic timing requires verified anchor selection')
        if parsed['status'] != 'insufficient_evidence':
            refs = parsed.get('evidence_refs')
            allowed = context.get('evidence_refs', [])
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in allowed for r in refs):
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
