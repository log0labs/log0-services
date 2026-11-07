"""Shared fixtures: fake env for Settings, clear lru_cache between tests."""

import asyncio
import os
import sys

# psycopg async requires SelectorEventLoop on Windows (not ProactorEventLoop).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory


def _postgres_reachable() -> bool:
    return os.getenv("SKIP_INTEGRATION_TESTS", "").lower() not in (
        "1",
        "true",
        "yes",
    )


requires_postgres = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()
    await engine.dispose()

@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Minimal env so Settings validates without a real .env."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://log0:log0@localhost:5433/log0",
    )
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
