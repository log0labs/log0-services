"""Integration tests for app.runner.InvestigationRunner (requires Postgres + migrations)."""

import asyncio
import os
from collections.abc import AsyncGenerator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.session import create_session_factory
from app.runner import InvestigationRunner, SessionFactory
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.store import investigations as store

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT = UUID("44444444-4444-4444-8444-444444444444")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")


@pytest_asyncio.fixture
async def session_factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM ai.investigation WHERE tenant_id = :tid"),
                {"tid": TENANT},
            )
            await session.commit()
        await engine.dispose()


async def _create(factory: SessionFactory, incident_id: UUID):
    async with factory() as session:
        return await store.create(
            session,
            tenant_id=TENANT,
            incident_id=incident_id,
            requested_by=USER_ID,
            scope=InvestigationScope(),
        )


async def _wait_until(
    factory: SessionFactory,
    investigation_id: UUID,
    status: InvestigationStatus,
    timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    current = None
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            row = await store.get_by_id(
                session, tenant_id=TENANT, investigation_id=investigation_id
            )
        current = row.status if row else None
        if current == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {status}, last status {current}")


async def test_runner_completes_stub_investigation(session_factory: SessionFactory) -> None:
    row = await _create(session_factory, UUID("a0000020-0000-4000-8000-000000000020"))
    runner = InvestigationRunner(session_factory)
    runner.schedule(row.id, tenant_id=TENANT)
    await _wait_until(session_factory, row.id, InvestigationStatus.COMPLETED)
    await runner.shutdown()


async def test_runner_cancel_in_flight(session_factory: SessionFactory) -> None:
    row = await _create(session_factory, UUID("a0000021-0000-4000-8000-000000000021"))
    started = asyncio.Event()

    async def hang(_factory, _investigation_id, _tenant_id) -> None:
        started.set()
        await asyncio.Event().wait()

    runner = InvestigationRunner(session_factory, execute=hang)
    runner.schedule(row.id, tenant_id=TENANT)
    await asyncio.wait_for(started.wait(), timeout=5)
    await runner.cancel(row.id, tenant_id=TENANT)
    await _wait_until(session_factory, row.id, InvestigationStatus.CANCELLED)
    await runner.shutdown()


async def test_runner_recovers_queued_on_startup(session_factory: SessionFactory) -> None:
    row = await _create(session_factory, UUID("a0000022-0000-4000-8000-000000000022"))
    runner = InvestigationRunner(session_factory)
    await runner.recover_on_startup()
    await _wait_until(session_factory, row.id, InvestigationStatus.COMPLETED)
    await runner.shutdown()
