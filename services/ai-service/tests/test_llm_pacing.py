"""Pacing: log0 keeps its own model calls under the provider's per-minute limit instead of waiting for a 429."""

import pytest

from app.llm.factory import (
    GenerationConfig,
    build_chat_model,
    rate_limiter_for,
    requests_per_minute,
)

GENERATION = GenerationConfig(temperature=0.0, max_tokens=256, timeout_seconds=30)


def _google(key: str):
    return build_chat_model(provider="google_genai", model="gemini-test", api_key=key, generation=GENERATION)


def test_gemini_is_paced_below_the_free_tier_limit_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_REQUESTS_PER_MINUTE", raising=False)

    assert requests_per_minute("google_genai") == 12
    assert _google("k-one").rate_limiter is not None


def test_models_that_share_a_key_share_one_limiter_and_another_key_has_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_REQUESTS_PER_MINUTE", raising=False)

    assert _google("k-shared").rate_limiter is _google("k-shared").rate_limiter  # the provider counts per key, not per model
    assert _google("k-shared").rate_limiter is not _google("k-other").rate_limiter


def test_an_operator_can_change_or_switch_off_the_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_REQUESTS_PER_MINUTE", "60")
    assert requests_per_minute("google_genai") == 60 and requests_per_minute("groq") == 60

    monkeypatch.setenv("LLM_REQUESTS_PER_MINUTE", "0")
    assert rate_limiter_for("google_genai", "k") is None and _google("k-off").rate_limiter is None

    monkeypatch.setenv("LLM_REQUESTS_PER_MINUTE", "not a number")
    assert requests_per_minute("google_genai") == 12  # an unusable value falls back to the default


def test_providers_without_a_known_limit_are_not_paced_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_REQUESTS_PER_MINUTE", raising=False)

    assert requests_per_minute("groq") == 0 and rate_limiter_for("groq", "k") is None
