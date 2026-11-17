"""Build LangChain chat models from a resolver + purpose."""

import hashlib
import os
from dataclasses import dataclass
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter

from app.llm.providers import get_provider
from app.llm.purpose import Purpose
from app.llm.resolver import LlmResolver

# Providers answer 429 when a per-minute token limit is hit and say how long to wait; the SDK waits and retries.
# Free tiers hit this on the large investigation steps, so be patient (the per-call timeout still bounds it).
MAX_RETRIES = 4
# Gemini's free tier allows 15 requests a minute and its client backs off for only a few seconds, so a burst of
# calls (an investigation, or an evaluation run) needs many more attempts to ride out the minute.
MAX_RETRIES_BY_PROVIDER = {"google_genai": 10}

# log0 paces its own calls instead of waiting for a 429: a rejected request still counts against the limit, and the retries
# that follow make a burst worse. Gemini's free tier allows 15 requests a minute on its cheapest model and fewer on others,
# so 12 leaves room. ``LLM_REQUESTS_PER_MINUTE`` replaces it for every provider (0 switches pacing off); a paid tier can raise it.
DEFAULT_REQUESTS_PER_MINUTE = {"google_genai": 12}
_limiters: dict[str, InMemoryRateLimiter] = {}


def requests_per_minute(provider: str) -> int:
    raw = os.environ.get("LLM_REQUESTS_PER_MINUTE", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_REQUESTS_PER_MINUTE.get(provider, 0)


def rate_limiter_for(provider: str, api_key: str) -> InMemoryRateLimiter | None:
    """One limiter per provider key, shared by every model built with it: the provider counts requests per key, not per model."""
    per_minute = requests_per_minute(provider)
    if per_minute <= 0:
        return None
    identity = f"{provider}:{per_minute}:{hashlib.sha256(api_key.encode()).hexdigest()}"
    if identity not in _limiters:
        _limiters[identity] = InMemoryRateLimiter(requests_per_second=per_minute / 60, check_every_n_seconds=0.1, max_bucket_size=1)
    return _limiters[identity]


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
        "max_retries": MAX_RETRIES_BY_PROVIDER.get(provider, MAX_RETRIES),
    }
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    limiter = rate_limiter_for(provider, api_key)
    if limiter is not None:
        kwargs["rate_limiter"] = limiter
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
