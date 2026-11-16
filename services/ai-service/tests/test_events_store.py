"""Integration tests for app.store.events (requires Postgres + migrations)."""

import os
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investigation import InvestigationScope
from app.store import events as event_store
from app.store import investigations as store

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT_A = UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = UUID("22222222-2222-4222-8222-222222222222")
USER_ID = UUID("33333333-3333-4333-8333-333333333333")
INCIDENT = UUID("a0000030-0000-4000-8000-000000000030")


async def _cleanup(session: AsyncSession) -> None:
    await session.execute(
        text("DELETE FROM ai.investigation WHERE tenant_id = :tid"),
        {"tid": TENANT_A},
    )
    await session.commit()


async def _make(session: AsyncSession):
    return await store.create(
        session,
        tenant_id=TENANT_A,
        incident_id=INCIDENT,
        requested_by=USER_ID,
        scope=InvestigationScope(),
    )


async def test_append_assigns_increasing_seq(db_session: AsyncSession) -> None:
    try:
        inv = await _make(db_session)
        e1 = await event_store.append_event(db_session, investigation_id=inv.id, type="a")
        e2 = await event_store.append_event(
            db_session, investigation_id=inv.id, type="b", payload={"k": 1}
        )
        assert (e1.seq, e2.seq) == (1, 2)
        assert e2.payload == {"k": 1}
    finally:
        await _cleanup(db_session)


async def test_list_after_filters_and_orders(db_session: AsyncSession) -> None:
    try:
        inv = await _make(db_session)
        for t in ("a", "b", "c"):
            await event_store.append_event(db_session, investigation_id=inv.id, type=t)
        rows = await event_store.list_after(
            db_session, tenant_id=TENANT_A, investigation_id=inv.id, after_seq=1
        )
        assert [r.type for r in rows] == ["b", "c"]
    finally:
        await _cleanup(db_session)


async def test_list_after_other_tenant_is_empty(db_session: AsyncSession) -> None:
    try:
        inv = await _make(db_session)
        await event_store.append_event(db_session, investigation_id=inv.id, type="a")
        rows = await event_store.list_after(
            db_session, tenant_id=TENANT_B, investigation_id=inv.id
        )
        assert rows == []
    finally:
        await _cleanup(db_session)
