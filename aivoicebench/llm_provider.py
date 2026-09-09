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

        Falls back to a simple heuristic if the API call fails — the result
        is marked low_confidence and the error is recorded.
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
        except Exception as exc:
            # Fall back to heuristic on API failure
            from .llm import MockLLMProvider
            mock = MockLLMProvider()
            mock_result, _ = mock.complete(system_prompt, user_prompt, dimension, context)
            result = mock_result
            result["status"] = "low_confidence" if mock_result.get("status") == "observed" else mock_result.get("status")
            result["reason"] = (result.get("reason", "") +
                                f" [LLM API fallback: {type(exc).__name__}: {str(exc)[:100]}]")

        inv.finished_at = _utc_now()
        inv.latency_ms = round((time.monotonic() - start_time) * 1000, 3)
        inv.status = "success" if self.api_key else "no_api_key_fallback"
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

        The LLM is prompted to return JSON. If parsing fails, fall back
        to heuristic analysis of the text.
        """
        # Try to parse as JSON first
        try:
            # Strip markdown code fences if present
            clean = text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1] if "\n" in clean else clean[3:]
                if clean.endswith("```"):
                    clean = clean[:-3]
                clean = clean.strip()
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                # Ensure required fields
                parsed.setdefault("decision", "llm_response")
                parsed.setdefault("confidence", 0.8)
                parsed.setdefault("reason", text[:200])
                parsed.setdefault("status", "observed")
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Fall back: treat the text as the reason
        return {
            "decision": "llm_response",
            "confidence": 0.7,
            "reason": text[:500],
            "status": "observed",
        }


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

    Supports 'mock', 'volcengine', 'openai'.
    Returns MockLLMProvider as fallback.
    """
    from .llm import MockLLMProvider

    provider_name = config.get("llm_provider", "mock")

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
        return MockLLMProvider()
