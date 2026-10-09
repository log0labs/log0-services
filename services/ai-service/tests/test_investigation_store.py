"""Integration tests for app.store.investigations (requires Postgres + migrations)."""

import os
from uuid import UUID

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import investigation
from app.schemas.investigation import InvestigationScope, InvestigationStatus
from app.store import investigations as store
from app.store.exceptions import ActiveInvestigationError

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


async def _delete_tenant_rows(session: AsyncSession, tenant_id: UUID) -> None:
    await session.execute(
        text("DELETE FROM ai.investigation WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    )
    await session.commit()


async def test_create_and_get_by_id(db_session: AsyncSession) -> None:
    incident_id = UUID("a0000001-0000-4000-8000-000000000001")
    scope = InvestigationScope(
        logs=True,
        history=False,
        code=True,
        runbooks=False,
        docs=True,
    )
    try:
        created = await store.create(
            db_session,
            tenant_id=TENANT_A,
            incident_id=incident_id,
            requested_by=USER_ID,
            scope=scope,
            question="What broke?",
        )
        assert created.status == InvestigationStatus.QUEUED
        assert created.question == "What broke?"
        assert created.scope.logs is True
        assert created.scope.history is False

        loaded = await store.get_by_id(
            db_session,
            tenant_id=TENANT_A,
            investigation_id=created.id,
        )
        assert loaded is not None
        assert loaded.id == created.id
        assert loaded.scope.history is False
    finally:
        await _delete_tenant_rows(db_session, TENANT_A)


async def test_get_by_id_wrong_tenant_returns_none(db_session: AsyncSession) -> None:
    incident_id = UUID("a0000002-0000-4000-8000-000000000002")
    try:
        created = await store.create(
            db_session,
            tenant_id=TENANT_A,
            incident_id=incident_id,
            requested_by=USER_ID,
            scope=InvestigationScope(),
        )
        assert (
            await store.get_by_id(
                db_session,
                tenant_id=TENANT_B,
                investigation_id=created.id,
            )
            is None
        )
    finally:
        await _delete_tenant_rows(db_session, TENANT_A)


async def test_list_by_incident_order(db_session: AsyncSession) -> None:
    incident_id = UUID("a0000003-0000-4000-8000-000000000003")
    scope_json = InvestigationScope().model_dump(mode="json", by_alias=True)
    try:
        await db_session.execute(
            insert(investigation).values(
                tenant_id=TENANT_A,
                incident_id=incident_id,
                requested_by=USER_ID,
                status=InvestigationStatus.COMPLETED.value,
                scope=scope_json,
            )
        )
        await db_session.commit()

        second = await store.create(
            db_session,
            tenant_id=TENANT_A,
            incident_id=incident_id,
            requested_by=USER_ID,
            scope=InvestigationScope(),
        )

        rows = await store.list_by_incident(
            db_session,
            tenant_id=TENANT_A,
            incident_id=incident_id,
        )
        assert len(rows) == 2
        assert rows[0].id == second.id
        assert rows[0].status == InvestigationStatus.QUEUED
        assert rows[1].status == InvestigationStatus.COMPLETED
    finally:
        await _delete_tenant_rows(db_session, TENANT_A)


async def test_create_second_active_raises(db_session: AsyncSession) -> None:
    incident_id = UUID("a0000004-0000-4000-8000-000000000004")
    try:
        await store.create(
            db_session,
            tenant_id=TENANT_A,
            incident_id=incident_id,
            requested_by=USER_ID,
            scope=InvestigationScope(),
        )
        with pytest.raises(ActiveInvestigationError):
            await store.create(
                db_session,
                tenant_id=TENANT_A,
                incident_id=incident_id,
                requested_by=USER_ID,
                scope=InvestigationScope(),
            )
    finally:
        await _delete_tenant_rows(db_session, TENANT_A)
