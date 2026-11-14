"""The two store functions the review gate needs: a compare-and-set status change and "newest event of a type"."""

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
from app.runner import SessionFactory
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.store import events as event_store
from app.store import investigations as store

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT = UUID("66666666-6666-4666-8666-666666666666")
OTHER = UUID("77777777-7777-4777-8777-777777777777")
USER = UUID("33333333-3333-4333-8333-333333333333")
WAITING, RUNNING = InvestigationStatus.AWAITING_REVIEW, InvestigationStatus.RUNNING


@pytest_asyncio.fixture
async def factory() -> AsyncGenerator[SessionFactory]:
    engine = create_async_engine(get_settings().database_url_async, pool_pre_ping=True)
    f = create_session_factory(engine)

    async def wipe() -> None:
        async with f() as session:
            for tenant in (TENANT, OTHER):
                await session.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": tenant})
            await session.commit()

    await wipe()
    try:
        yield f
    finally:
        await wipe()
        await engine.dispose()


async def make(f: SessionFactory, number: int, status: InvestigationStatus = WAITING, tenant: UUID = TENANT) -> UUID:
    async with f() as session:
        row = await store.create(
            session, tenant_id=tenant, incident_id=UUID(f"b0000060-0000-4000-8000-{number:012d}"), requested_by=USER, scope=InvestigationScope()
        )
        await store.transition_status(session, investigation_id=row.id, tenant_id=tenant, status=status)
    return row.id


async def status_of(f: SessionFactory, investigation_id: UUID, tenant: UUID = TENANT) -> InvestigationStatus:
    async with f() as session:
        row = await store.get_by_id(session, tenant_id=tenant, investigation_id=investigation_id)
    assert row is not None
    return row.status


# -------------------------------------------------------------------- transition_status_if


async def test_the_status_changes_only_when_it_is_the_expected_one(factory: SessionFactory) -> None:
    waiting, finished = await make(factory, 1), await make(factory, 2, InvestigationStatus.COMPLETED)
    async with factory() as session:
        assert await store.transition_status_if(session, investigation_id=waiting, tenant_id=TENANT, from_status=WAITING, to_status=RUNNING)
        assert not await store.transition_status_if(session, investigation_id=waiting, tenant_id=TENANT, from_status=WAITING, to_status=RUNNING)  # already moved
        assert not await store.transition_status_if(session, investigation_id=finished, tenant_id=TENANT, from_status=WAITING, to_status=RUNNING)
    assert await status_of(factory, waiting) == RUNNING and await status_of(factory, finished) == InvestigationStatus.COMPLETED


async def test_another_tenant_cannot_change_the_status(factory: SessionFactory) -> None:
    waiting = await make(factory, 3)
    async with factory() as session:
        assert not await store.transition_status_if(session, investigation_id=waiting, tenant_id=OTHER, from_status=WAITING, to_status=RUNNING)
    assert await status_of(factory, waiting) == WAITING


async def test_of_many_simultaneous_changes_exactly_one_wins(factory: SessionFactory) -> None:
    waiting = await make(factory, 4)

    async def attempt() -> bool:
        async with factory() as session:
            return await store.transition_status_if(session, investigation_id=waiting, tenant_id=TENANT, from_status=WAITING, to_status=RUNNING)

    assert (await asyncio.gather(*(attempt() for _ in range(8)))).count(True) == 1


async def test_a_waiting_investigation_is_not_picked_up_by_startup_recovery(factory: SessionFactory) -> None:
    waiting, running = await make(factory, 5), await make(factory, 6, RUNNING)
    async with factory() as session:
        pending = {p[0] for p in await store.list_pending_for_runner(session)}
    assert running in pending and waiting not in pending


# ------------------------------------------------------------------------- latest_of_type


async def test_the_newest_event_of_a_type_is_found_and_is_tenant_scoped(factory: SessionFactory) -> None:
    mine, theirs = await make(factory, 7), await make(factory, 8, tenant=OTHER)
    async with factory() as session:
        for number, kind in enumerate(["review.requested", "gap.recorded", "review.requested", "review.decided"], start=1):
            await event_store.append_event(session, investigation_id=mine, type=kind, payload={"n": number})
        await event_store.append_event(session, investigation_id=theirs, type="review.requested", payload={"n": 99})

        newest = await event_store.latest_of_type(session, tenant_id=TENANT, investigation_id=mine, type="review.requested")
        assert newest is not None and (newest.seq, newest.payload["n"]) == (3, 3)
        assert await event_store.latest_of_type(session, tenant_id=TENANT, investigation_id=mine, type="nothing.like.this") is None
        assert await event_store.latest_of_type(session, tenant_id=TENANT, investigation_id=theirs, type="review.requested") is None  # not mine
        assert (await event_store.latest_of_type(session, tenant_id=OTHER, investigation_id=theirs, type="review.requested")) is not None
