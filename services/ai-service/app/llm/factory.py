"""Build LangChain chat models from a resolver + purpose."""

from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from app.llm.providers import get_provider
from app.llm.purpose import Purpose
from app.llm.resolver import LlmResolver

MAX_RETRIES = 2


@dataclass(frozen=True)
class GenerationConfig:
    temperature: float
    max_tokens: int
    timeout_seconds: int


def build_chat_model(
    *,
    provider: str,
    model: str,
    api_key: str,
    generation: GenerationConfig,    
) -> BaseChatModel:
    spec = get_provider(provider)
    kwargs: dict[str, Any] = {
        "api_key": api_key,
        "temperature": generation.temperature,
        "max_tokens": generation.max_tokens,
        "timeout": generation.timeout_seconds,
        "max_retries": MAX_RETRIES,
    }
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    return init_chat_model(model, model_provider=spec.langchain_provider, **kwargs)


def chat_model_for_purpose(
    resolver: LlmResolver,
    purpose: Purpose,
    generation: GenerationConfig,
) -> BaseChatModel:
    """Single entry point for all LLM calls: summary, triage, investigation, eval."""
    spec = resolver.model_for(purpose)
    key = resolver.api_key_for(spec.provider)
    return build_chat_model(
        provider=spec.provider,
        model=spec.model,
        api_key=key.get_secret_value(),
        generation=generation,
    )
