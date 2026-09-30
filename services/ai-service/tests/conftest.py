"""Shared fixtures: fake env for Settings, clear lru_cache between tests."""

from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Minimal env so Settings validates without a real .env."""
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("JWT_SECRET","test-jwt-secret-at-least-32-characters-long",)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-internal-token-at-least-16-characters-long")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def sample_summary_request():
    from app.schemas.summary import SummaryRequest

    return SummaryRequest(
        incident_id=UUID("c9d8e7f6-a5b4-4c3d-8e1f-0a9b8c7d6e5f"),
        tenant_id=UUID("b7e1f290-12ab-4cd3-8ef5-6789abcd0123"),
        service_name="payment-service",
        environment="production",
        severity="HIGH",
        occurrence_count=10,
        first_seen_at=datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
        last_seen_at=datetime(2026, 9, 28, 10, 5, tzinfo=UTC),
        top_messages=["timeout calling gateway"],
    )


@pytest.fixture
def summary_request_json(sample_summary_request) -> dict:
    return sample_summary_request.model_dump(mode="json", by_alias=True)
