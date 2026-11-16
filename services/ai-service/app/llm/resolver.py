"""Resolve (purpose → model spec, provider → API key)."""

from typing import Protocol

from pydantic import SecretStr

from app.config import Settings
from app.llm.model_spec import ModelSpec
from app.llm.providers import get_provider
from app.llm.purpose import Purpose


class LlmResolver(Protocol):
    """Anything that can answer: which model for this purpose, which key for this provider."""
    
    def model_for(self, purpose: Purpose) -> ModelSpec: ...

    def api_key_for(self, provider: str) -> SecretStr: ...


class PlatformLlmResolver:
    """Env-based defaults: per-purpose ``LLM_<PURPOSE>=provider:model`` + platform API keys."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        
    def model_for(self, purpose: Purpose) -> ModelSpec:
        return self.settings.llm_models[purpose]
    
    def api_key_for(self, provider: str) -> SecretStr:
        spec = get_provider(provider)
        key: SecretStr | None = getattr(self.settings, spec.api_key_setting)
        if key is None:
            raise ValueError(
                f"{spec.api_key_setting.upper()} is not set but {provider!r} was requested"
            )
        return key


class TenantLlmResolver:
    """A workspace's own choices over the platform defaults.

    ``specs`` holds the model the workspace picked per purpose and ``keys`` the API keys it stored, by provider (if
    two purposes of one provider hold different keys, the first of investigation, triage, summary, evaluation wins).
    A purpose with no saved model uses the platform model; a provider with no saved key uses the platform key.
    """

    def __init__(self, platform: PlatformLlmResolver, specs: dict[Purpose, ModelSpec], keys: dict[str, SecretStr]):
        self._platform = platform
        self._specs = specs
        self._keys = keys

    def model_for(self, purpose: Purpose) -> ModelSpec:
        return self._specs.get(purpose) or self._platform.model_for(purpose)

    def api_key_for(self, provider: str) -> SecretStr:
        return self._keys.get(provider) or self._platform.api_key_for(provider)

    def is_platform_model(self, purpose: Purpose) -> bool:
        return purpose not in self._specs

