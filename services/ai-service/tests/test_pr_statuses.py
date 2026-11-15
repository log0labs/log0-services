"""The V2 statuses and state keys: what blocks a new run, what is recovered, what survives a checkpoint."""

import os
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import investigation
from app.graph.state import checkpoint_serde, new_state
from app.schemas.domain import BranchRef, PrDecision, PullRequestRef
from app.schemas.investigation import (
    ACTIVE_STATUSES,
    PAUSED_STATUSES,
    InvestigationScope,
    InvestigationStatus,
)
from app.store import investigations as store

TENANT = UUID("33000000-0000-4000-8000-00000000000a")
USER = UUID("33000000-0000-4000-8000-0000000000c1")

needs_postgres = pytest.mark.skipif(
    os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
    reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
)


def test_the_pull_request_gate_pauses_the_run_and_publishing_does_not() -> None:
    assert InvestigationStatus.AWAITING_PR_APPROVAL in PAUSED_STATUSES
    assert InvestigationStatus.PUBLISHING not in PAUSED_STATUSES


def test_every_status_that_is_not_finished_blocks_a_new_investigation() -> None:
    finished = {InvestigationStatus.COMPLETED, InvestigationStatus.FAILED, InvestigationStatus.CANCELLED}

    assert ACTIVE_STATUSES == set(InvestigationStatus) - finished


def test_a_new_state_has_no_pull_request_yet() -> None:
    state = new_state(investigation_id="i", incident_id="c", scope=InvestigationScope())

    assert (state["pr_decision"], state["branch"], state["commit_sha"], state["pull_request"]) == (None, None, None, None)


def test_the_pull_request_types_survive_a_checkpoint_round_trip() -> None:
    serde = checkpoint_serde()
    values = [
        PrDecision(action="approve", decided_by=str(USER)),
        BranchRef(name="log0/fix-a-b", base_sha="a" * 40),
        PullRequestRef(number=3, url="https://github.com/acme/shop/pull/3", branch="log0/fix-a-b", commit_sha="b" * 40),
    ]

    for value in values:
        assert serde.loads_typed(serde.dumps_typed(value)) == value


@pytest_asyncio.fixture
async def session(db_session: AsyncSession) -> AsyncGenerator[AsyncSession]:
    await db_session.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": TENANT})
    await db_session.commit()
    yield db_session
    await db_session.rollback()
    await db_session.execute(text("DELETE FROM ai.investigation WHERE tenant_id = :t"), {"t": TENANT})
    await db_session.commit()


async def _add(session: AsyncSession, incident: UUID, status: InvestigationStatus) -> UUID:
    row = await session.execute(
        insert(investigation)
        .values(tenant_id=TENANT, incident_id=incident, requested_by=USER, status=status.value)
        .returning(investigation.c.id)
    )
    return row.scalar_one()


@needs_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [InvestigationStatus.AWAITING_PR_APPROVAL, InvestigationStatus.PUBLISHING])
async def test_a_run_parked_at_the_pull_request_gate_or_publishing_blocks_another_for_the_incident(
    session: AsyncSession, status: InvestigationStatus
) -> None:
    incident = uuid4()
    await _add(session, incident, status)

    with pytest.raises(IntegrityError):
        await _add(session, incident, InvestigationStatus.QUEUED)
    await session.rollback()


@needs_postgres
@pytest.mark.asyncio
async def test_a_finished_run_does_not_block_a_new_one(session: AsyncSession) -> None:
    incident = uuid4()
    await _add(session, incident, InvestigationStatus.COMPLETED)

    await _add(session, incident, InvestigationStatus.QUEUED)


@needs_postgres
@pytest.mark.asyncio
async def test_startup_recovery_picks_up_publishing_but_not_the_parked_gates(session: AsyncSession) -> None:
    publishing = await _add(session, uuid4(), InvestigationStatus.PUBLISHING)
    parked = await _add(session, uuid4(), InvestigationStatus.AWAITING_PR_APPROVAL)
    await session.commit()

    pending = {investigation_id for investigation_id, _ in await store.list_pending_for_runner(session)}

    assert publishing in pending and parked not in pending
