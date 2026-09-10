"""Opt-in invocation audit around the existing ASRProvider protocol."""

import json
from pathlib import Path

from .asr import ProviderOutput
from .providers import InvocationAudit, ProviderFailure, immutable_json, _credential_keys


class RecordedASRProvider:
    """Use an evidence root containing the provider input (e.g. an imported Run).

    Expected profile is explicit trusted adapter configuration, not inferred from
    a response. Normalization remains the original provider's responsibility.
    A completed invocation is not a claim that transcript validation succeeded.
    """

    def __init__(self, provider, evidence_root, identity, profile, config_schema):
        self.provider = provider
        self.root = Path(evidence_root)
        self.identity = identity
        self.profile = json.loads(json.dumps(profile, allow_nan=False))
        _credential_keys(self.profile)
        if profile.get('provider') != identity.provider or profile.get('model_id') != identity.model:
            raise ValueError('ASR identity must match selected profile')
        self.config_schema = config_schema

    def transcribe(self, mono_wav):
        audit = InvocationAudit(self.root, self.identity, self.profile['config'],
                                self.config_schema, [mono_wav])
        try:
            result = self.provider.transcribe(mono_wav)
            if result.profile != self.profile:
                raise ValueError('Provider returned a different profile')
            if not isinstance(result.raw_messages, list) or any(not isinstance(raw, str) for raw in result.raw_messages):
                raise ValueError('Provider did not return native strings')
            raw_path = audit.directory / 'native-response.json'
            immutable_json(raw_path, {'messages': result.raw_messages})
            profile_path = audit.directory / 'provider-profile.json'
            immutable_json(profile_path, self.profile)
            audit.finish('complete', [raw_path, profile_path])
            return ProviderOutput(self.profile, list(result.raw_messages))
        except Exception:
            # Never stringify exceptions: cloud SDK errors may include auth or URLs.
            if not audit.finished:
                try:
                    audit.finish('failed', failure_code='provider_error')
                except Exception:
                    # A changed input or write failure must not produce false valid
                    # evidence; the immutable start remains visibly pending.
                    pass
            raise ProviderFailure('ASR provider invocation failed; inspect its local audit status') from None

    def normalize(self, messages, duration_ms):
        try:
            return self.provider.normalize(messages, duration_ms)
        except Exception:
            raise ProviderFailure('ASR normalization failed; native invocation evidence is retained') from None
