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


# M3: class TenantLlmResolver(PlatformLlmResolver):
#   """Override model_for / api_key_for from ai.provider_config (decrypted BYOK) with fallback to platform."""
