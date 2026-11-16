"""The V3 tables against real Postgres: the rules the design relies on are enforced by the database, not only by code.

One remediation per pull request and environment, one active remediation per repository and environment, one deploy and
one rollback per remediation, rows that follow their investigation when it is deleted, and an audit nobody can change.
Needs the migrations applied (``alembic upgrade head``).
"""

import os
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import (
    approval,
    deploy_audit,
    deploy_target,
    deployment,
    pull_request,
    release_ledger,
    remediation,
    verification,
)
from app.schemas.remediation import ACTIVE_STATUSES, STATUSES, TERMINAL_STATUSES
from tests.test_github_app_tables import _investigation, _pull, _repository

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

TENANT_A = UUID("33000000-0000-4000-8000-00000000000a")
TENANT_B = UUID("33000000-0000-4000-8000-00000000000b")
USER = UUID("33000000-0000-4000-8000-0000000000c1")
TABLES = (deploy_target, remediation, deployment, verification, approval, release_ledger, deploy_audit)
SHA = "b" * 40


async def _wipe(session: AsyncSession) -> None:
    for tenant in (TENANT_A, TENANT_B):
        for name in ("investigation", "repository_connection"):  # everything else follows by cascade
            await session.execute(text(f"DELETE FROM ai.{name} WHERE tenant_id = :t"), {"t": tenant})
    await session.commit()


@pytest_asyncio.fixture
async def session(db_session: AsyncSession) -> AsyncGenerator[AsyncSession]:
    await _wipe(db_session)
    yield db_session
    await db_session.rollback()
    await _wipe(db_session)


async def _pull_request(session: AsyncSession, repository_id: UUID, tenant: UUID = TENANT_A) -> tuple[UUID, UUID]:
    """A pull request of a new investigation: ``(pull request id, investigation id)``."""
    investigation_id = await _investigation(session, tenant)
    row = await session.execute(insert(pull_request).values(**_pull(tenant, investigation_id, repository_id)).returning(pull_request.c.id))
    return row.scalar_one(), investigation_id


async def _remediation(session: AsyncSession, pull_id: UUID, investigation_id: UUID, repository_id: UUID, **overrides: Any) -> UUID:
    values = {
        "tenant_id": TENANT_A,
        "pull_request_id": pull_id,
        "investigation_id": investigation_id,
        "repository_id": repository_id,
        "environment": "production",
        "merge_sha": SHA,
    } | overrides
    return (await session.execute(insert(remediation).values(**values).returning(remediation.c.id))).scalar_one()


async def _setup(session: AsyncSession) -> tuple[UUID, UUID, UUID, UUID]:
    """``(repository id, pull request id, investigation id, remediation id)`` for a fresh offer."""
    repository_id = await _repository(session)
    pull_id, investigation_id = await _pull_request(session, repository_id)
    remediation_id = await _remediation(session, pull_id, investigation_id, repository_id)
    return repository_id, pull_id, investigation_id, remediation_id


async def test_the_core_definitions_match_the_real_columns(session: AsyncSession) -> None:
    for table in TABLES:
        rows = await session.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'ai' AND table_name = :t"),
            {"t": table.name},
        )
        assert {r[0] for r in rows} == {c.name for c in table.columns}, table.name


async def test_every_table_has_a_required_tenant_column(session: AsyncSession) -> None:
    for table in TABLES:
        row = (
            await session.execute(
                text("SELECT is_nullable FROM information_schema.columns WHERE table_schema = 'ai' AND table_name = :t AND column_name = 'tenant_id'"),
                {"t": table.name},
            )
        ).one()
        assert row[0] == "NO", table.name


async def test_every_status_of_the_vocabulary_is_accepted_and_an_unknown_one_is_not(session: AsyncSession) -> None:
    repository_id = await _repository(session)
    pull_id, investigation_id = await _pull_request(session, repository_id)

    for status in STATUSES:  # a different environment each time, so the active-environment rule stays out of the way
        await _remediation(session, pull_id, investigation_id, repository_id, environment=status.lower(), status=status)
    with pytest.raises(IntegrityError):
        await _remediation(session, pull_id, investigation_id, repository_id, environment="odd", status="PAUSED")
    await session.rollback()


async def test_the_active_index_is_made_of_the_same_statuses_as_the_code(session: AsyncSession) -> None:
    definition = (await session.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'ai' AND indexname = 'uq_remediation_active_environment'"))).scalar_one()

    assert all(f"'{status}'" in definition for status in ACTIVE_STATUSES)
    assert not any(f"'{status}'" in definition for status in TERMINAL_STATUSES | {"OFFERED"})


async def test_one_remediation_per_pull_request_and_environment(session: AsyncSession) -> None:
    repository_id, pull_id, investigation_id, _ = await _setup(session)

    await _remediation(session, pull_id, investigation_id, repository_id, environment="staging")  # another environment is fine
    with pytest.raises(IntegrityError):
        await _remediation(session, pull_id, investigation_id, repository_id, environment="production")
    await session.rollback()


async def test_only_one_remediation_per_repository_and_environment_can_be_active(session: AsyncSession) -> None:
    repository_id, _, _, first = await _setup(session)
    second_pull, second_investigation = await _pull_request(session, repository_id)
    await session.execute(text("UPDATE ai.remediation SET status = 'DEPLOYING' WHERE id = :i"), {"i": first})

    await _remediation(session, second_pull, second_investigation, repository_id)  # an offer does not hold the environment
    with pytest.raises(IntegrityError):
        await session.execute(text("UPDATE ai.remediation SET status = 'PREFLIGHT' WHERE pull_request_id = :p"), {"p": second_pull})
    await session.rollback()


async def test_the_environment_is_free_again_once_the_remediation_ends(session: AsyncSession) -> None:
    repository_id, _, _, first = await _setup(session)
    second_pull, second_investigation = await _pull_request(session, repository_id)
    second = await _remediation(session, second_pull, second_investigation, repository_id)
    await session.execute(text("UPDATE ai.remediation SET status = 'VERIFYING' WHERE id = :i"), {"i": first})

    await session.execute(text("UPDATE ai.remediation SET status = 'VERIFIED' WHERE id = :i"), {"i": first})
    await session.execute(text("UPDATE ai.remediation SET status = 'PREFLIGHT' WHERE id = :i"), {"i": second})

    assert (await session.execute(select(remediation.c.status).where(remediation.c.id == second))).scalar_one() == "PREFLIGHT"


async def test_an_active_remediation_does_not_block_another_environment_or_repository(session: AsyncSession) -> None:
    repository_id, pull_id, investigation_id, first = await _setup(session)
    await session.execute(text("UPDATE ai.remediation SET status = 'DEPLOYING' WHERE id = :i"), {"i": first})

    staging = await _remediation(session, pull_id, investigation_id, repository_id, environment="staging", status="DEPLOYING")
    other_repository = await _repository(session, name="other")
    other_pull, other_investigation = await _pull_request(session, other_repository)
    elsewhere = await _remediation(session, other_pull, other_investigation, other_repository, status="DEPLOYING")

    assert staging != elsewhere


async def test_a_remediation_has_at_most_one_deploy_and_one_rollback(session: AsyncSession) -> None:
    _, _, _, remediation_id = await _setup(session)
    values = {"tenant_id": TENANT_A, "remediation_id": remediation_id, "environment": "production", "sha": SHA}

    await session.execute(insert(deployment).values(kind="deploy", correlation_id=uuid4(), **values))
    await session.execute(insert(deployment).values(kind="rollback", correlation_id=uuid4(), **values))
    with pytest.raises(IntegrityError):
        await session.execute(insert(deployment).values(kind="rollback", correlation_id=uuid4(), **values))
    await session.rollback()


async def test_a_correlation_id_identifies_one_deployment(session: AsyncSession) -> None:
    _, _, _, remediation_id = await _setup(session)
    correlation = uuid4()
    values = {"tenant_id": TENANT_A, "remediation_id": remediation_id, "environment": "production", "sha": SHA, "correlation_id": correlation}

    await session.execute(insert(deployment).values(kind="deploy", **values))
    with pytest.raises(IntegrityError):
        await session.execute(insert(deployment).values(kind="rollback", **values))
    await session.rollback()


@pytest.mark.parametrize(
    ("table", "column", "bad"),
    [(deployment, "status", "lost"), (deployment, "kind", "redeploy"), (verification, "verdict", "fine"), (approval, "gate", "merge"), (release_ledger, "source", "guess")],
)
async def test_closed_vocabularies_are_checked_by_the_database(session: AsyncSession, table: Any, column: str, bad: str) -> None:
    repository_id, _, _, remediation_id = await _setup(session)
    base: dict[str, dict[str, Any]] = {
        "deployment": {"remediation_id": remediation_id, "kind": "deploy", "environment": "production", "sha": SHA, "correlation_id": uuid4()},
        "verification": {"remediation_id": remediation_id, "kind": "deploy", "verdict": "healthy", "window_start": text("now()"), "window_end": text("now()")},
        "approval": {"remediation_id": remediation_id, "gate": "deploy", "decision": "approve", "user_id": USER, "role": "ADMIN", "environment": "production", "sha": SHA},
        "release_ledger": {"repository_id": repository_id, "environment": "production", "sha": SHA, "source": "log0"},
    }

    with pytest.raises(IntegrityError):
        await session.execute(insert(table).values(tenant_id=TENANT_A, **(base[table.name] | {column: bad})))
    await session.rollback()


async def test_deleting_an_investigation_removes_everything_that_followed_from_it(session: AsyncSession) -> None:
    _, _, investigation_id, remediation_id = await _setup(session)
    await session.execute(insert(deployment).values(tenant_id=TENANT_A, remediation_id=remediation_id, kind="deploy", environment="production", sha=SHA, correlation_id=uuid4()))
    await session.execute(insert(approval).values(tenant_id=TENANT_A, remediation_id=remediation_id, gate="deploy", decision="approve", user_id=USER, role="ADMIN", environment="production", sha=SHA))
    await session.execute(insert(verification).values(tenant_id=TENANT_A, remediation_id=remediation_id, kind="deploy", verdict="healthy", window_start=text("now()"), window_end=text("now()")))

    await session.execute(text("DELETE FROM ai.investigation WHERE id = :i"), {"i": investigation_id})

    for table in (remediation, deployment, approval, verification):
        rows = (await session.execute(select(table).where(table.c.tenant_id == TENANT_A))).all()
        assert rows == [], table.name


async def test_the_release_ledger_outlives_the_remediation_that_wrote_it(session: AsyncSession) -> None:
    repository_id, _, investigation_id, remediation_id = await _setup(session)
    await session.execute(insert(release_ledger).values(tenant_id=TENANT_A, repository_id=repository_id, environment="production", sha=SHA, source="log0", remediation_id=remediation_id))

    await session.execute(text("DELETE FROM ai.investigation WHERE id = :i"), {"i": investigation_id})

    row = (await session.execute(select(release_ledger).where(release_ledger.c.repository_id == repository_id))).one()
    assert row.remediation_id is None and row.sha == SHA


async def test_a_repository_has_one_deploy_target(session: AsyncSession) -> None:
    repository_id = await _repository(session)
    values = {"tenant_id": TENANT_A, "repository_id": repository_id, "workflow_file": "deploy.yml", "updated_by": USER}

    await session.execute(insert(deploy_target).values(**values))
    with pytest.raises(IntegrityError):
        await session.execute(insert(deploy_target).values(**values))
    await session.rollback()


@pytest.mark.parametrize("statement", ["UPDATE ai.deploy_audit SET result = 'x' WHERE tenant_id = :t", "DELETE FROM ai.deploy_audit WHERE tenant_id = :t", "TRUNCATE ai.deploy_audit"])
async def test_the_audit_cannot_be_changed_or_emptied(session: AsyncSession, statement: str) -> None:
    tenant = uuid4()  # an audit row can never be deleted, so each run writes under a tenant of its own
    await session.execute(insert(deploy_audit).values(tenant_id=tenant, action="deploy.approved", result="ok"))
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(text(statement), {"t": tenant} if ":t" in statement else {})
    await session.rollback()

    row = (await session.execute(select(deploy_audit).where(deploy_audit.c.tenant_id == tenant))).one()
    assert row.result == "ok"


async def test_the_audit_accepts_new_rows(session: AsyncSession) -> None:
    tenant = uuid4()

    await session.execute(insert(deploy_audit).values(tenant_id=tenant, action="deploy.dispatched", result="ok", environment="production", sha=SHA))
    await session.commit()

    assert len((await session.execute(select(deploy_audit).where(deploy_audit.c.tenant_id == tenant))).all()) == 1
