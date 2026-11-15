"""The V2 tables (installation, pull request, test run, write audit) against real Postgres.

They pin what the design relies on: one installation belongs to one tenant, one pull request per
investigation, a tenant column everywhere, writes switched off by default, and rows that follow
their investigation when it is deleted. Needs the migrations applied (``alembic upgrade head``).
"""

import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    github_installation,
    investigation,
    pull_request,
    repository_connection,
    test_run,
    write_audit,
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT_A = UUID("32000000-0000-4000-8000-00000000000a")
TENANT_B = UUID("32000000-0000-4000-8000-00000000000b")
USER = UUID("32000000-0000-4000-8000-0000000000c1")
TABLES = (github_installation, pull_request, test_run, write_audit)


async def _wipe(session: AsyncSession) -> None:
    for tenant in (TENANT_A, TENANT_B):
        for name in ("write_audit", "test_run", "pull_request", "investigation", "repository_connection", "github_installation"):
            await session.execute(text(f"DELETE FROM ai.{name} WHERE tenant_id = :t"), {"t": tenant})
    await session.commit()


@pytest_asyncio.fixture
async def session(db_session: AsyncSession) -> AsyncGenerator[AsyncSession]:
    await _wipe(db_session)
    yield db_session
    await db_session.rollback()
    await _wipe(db_session)


async def _installation(session: AsyncSession, tenant: UUID = TENANT_A, installation_id: int = 4242) -> None:
    await session.execute(
        insert(github_installation).values(
            tenant_id=tenant, installation_id=installation_id, account_login="acme", account_type="Organization", created_by=USER
        )
    )


async def _repository(session: AsyncSession, tenant: UUID = TENANT_A, **overrides: Any) -> UUID:
    values = {
        "tenant_id": tenant,
        "owner": "acme",
        "name": "shop",
        "token_ciphertext": "gAAAAA-not-a-real-token",
        "key_id": "k1",
        "created_by": USER,
    } | overrides
    return (await session.execute(insert(repository_connection).values(**values).returning(repository_connection.c.id))).scalar_one()


async def _investigation(session: AsyncSession, tenant: UUID = TENANT_A) -> UUID:
    row = await session.execute(
        insert(investigation)
        .values(tenant_id=tenant, incident_id=uuid4(), requested_by=USER, status="COMPLETED")
        .returning(investigation.c.id)
    )
    return row.scalar_one()


def _pull(tenant: UUID, investigation_id: UUID, repository_id: UUID) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "investigation_id": investigation_id,
        "repository_id": repository_id,
        "branch": "log0/fix-abc-def",
        "base_sha": "a" * 40,
        "approved_by": USER,
    }


async def test_the_core_definitions_match_the_real_columns(session: AsyncSession) -> None:
    for table in (*TABLES, repository_connection):
        rows = await session.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'ai' AND table_name = :t"),
            {"t": table.name},
        )
        assert {r[0] for r in rows} == {c.name for c in table.columns}, table.name


async def test_every_new_table_has_a_required_tenant_column(session: AsyncSession) -> None:
    for table in TABLES:
        row = (
            await session.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'ai' AND table_name = :t AND column_name = 'tenant_id'"
                ),
                {"t": table.name},
            )
        ).one()
        assert row[0] == "NO", table.name


async def test_one_installation_belongs_to_one_tenant(session: AsyncSession) -> None:
    await _installation(session, TENANT_A, 4242)

    with pytest.raises(IntegrityError):
        await _installation(session, TENANT_B, 4242)
    await session.rollback()


async def test_pr_writes_are_off_by_default_for_a_repository(session: AsyncSession) -> None:
    repository_id = await _repository(session)

    enabled = (await session.execute(select(repository_connection.c.pr_writes_enabled).where(repository_connection.c.id == repository_id))).scalar_one()

    assert enabled is False


async def test_removing_an_installation_unlinks_its_repositories_but_keeps_them(session: AsyncSession) -> None:
    await _installation(session)
    repository_id = await _repository(session, installation_id=4242, github_repository_id=7)

    await session.execute(text("DELETE FROM ai.github_installation WHERE installation_id = 4242"))

    linked = (await session.execute(select(repository_connection.c.installation_id).where(repository_connection.c.id == repository_id))).scalar_one()
    assert linked is None


async def test_a_repository_cannot_point_at_an_unknown_installation(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await _repository(session, installation_id=999)
    await session.rollback()


async def test_only_one_pull_request_per_investigation(session: AsyncSession) -> None:
    repository_id = await _repository(session)
    investigation_id = await _investigation(session)
    await session.execute(insert(pull_request).values(**_pull(TENANT_A, investigation_id, repository_id)))

    with pytest.raises(IntegrityError):
        await session.execute(insert(pull_request).values(**_pull(TENANT_A, investigation_id, repository_id)))
    await session.rollback()


async def test_a_new_pull_request_starts_at_branch_created(session: AsyncSession) -> None:
    repository_id = await _repository(session)
    investigation_id = await _investigation(session)

    state = (
        await session.execute(insert(pull_request).values(**_pull(TENANT_A, investigation_id, repository_id)).returning(pull_request.c.state))
    ).scalar_one()

    assert state == "branch_created"


async def test_deleting_an_investigation_removes_its_pull_request_and_test_runs(session: AsyncSession) -> None:
    repository_id = await _repository(session)
    investigation_id = await _investigation(session)
    await session.execute(insert(pull_request).values(**_pull(TENANT_A, investigation_id, repository_id)))
    await session.execute(insert(test_run).values(tenant_id=TENANT_A, investigation_id=investigation_id, commit_sha="b" * 40, outcome="passed"))

    await session.execute(text("DELETE FROM ai.investigation WHERE id = :i"), {"i": investigation_id})

    assert (await session.execute(select(pull_request.c.id))).first() is None
    assert (await session.execute(select(test_run.c.id).where(test_run.c.tenant_id == TENANT_A))).first() is None


async def test_the_audit_log_keeps_rows_after_the_investigation_is_gone(session: AsyncSession) -> None:
    investigation_id = await _investigation(session)
    await session.execute(
        insert(write_audit).values(
            tenant_id=TENANT_A, investigation_id=investigation_id, actor_id=USER, action="branch.create", repository="acme/shop", result="ok"
        )
    )

    await session.execute(text("DELETE FROM ai.investigation WHERE id = :i"), {"i": investigation_id})

    rows = (await session.execute(select(write_audit.c.action).where(write_audit.c.tenant_id == TENANT_A))).all()
    assert [r[0] for r in rows] == ["branch.create"]
