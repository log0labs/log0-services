"""Rolling back: from a bad deploy to the previous commit back (real graph, real Postgres, a test clock)."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.integrations.clickhouse import ClickHouseError
from app.store import remediations as remediation_store
from tests.remediation_support import (
    PREVIOUS_SHA,
    Harness,
    build_harness,
    forget,
    session_factory,  # noqa: F401  (fixture)
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]

GRACE = 120
SOAK = 600


@pytest_asyncio.fixture
async def h(session_factory) -> AsyncGenerator[Harness]:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        yield harness
    finally:
        await forget(session_factory, harness.seed.tenant)


async def _look(h: Harness, *, after: float) -> None:
    h.clock.advance(seconds=after)
    await h.run("staging")


async def _bad_deploy(h: Harness) -> None:
    """A deploy that made things worse: the run is parked at the decision gate."""
    await h.deployed()
    h.logs.errors_per_minute = 10
    await _look(h, after=GRACE + 6 * 60)
    assert (await h.row("staging")).status == "AWAITING_DECISION"


async def _rolled_back(h: Harness) -> None:
    """The person chose to roll back and the pipeline put the previous commit back: the run is watching the recovery."""
    await _bad_deploy(h)
    h.logs.errors_per_minute = 4  # the old error, at its old rate
    await h.decide("rollback")
    assert (await h.row("staging")).status == "ROLLING_BACK"


async def _verification_kinds(h: Harness) -> list[tuple[str, str]]:
    async with h.factory() as session:
        rows = await session.execute(text("SELECT kind, verdict FROM ai.verification WHERE remediation_id = :r ORDER BY created_at, id"), {"r": h.seed.offers["staging"]})
        return [(r[0], r[1]) for r in rows]


async def test_a_person_who_rolls_back_sends_the_previous_commit_through_the_same_pipeline(h: Harness) -> None:
    await _rolled_back(h)

    assert [d.sha for d in h.provider.dispatches] == ["c" * 40, PREVIOUS_SHA]
    assert {d.workflow_file for d in h.provider.dispatches} == {"deploy.yml"}
    row = await h.row("staging")
    assert row.status == "ROLLING_BACK" and row.wake_at is not None  # the rollback is in its grace period, a timer is set
    actions = [a for a, _ in await h.audit()]
    assert actions.index("deploy.degraded") < actions.index("rollback.dispatching") < actions.index("rollback.succeeded")


async def test_a_rollback_that_recovers_ends_as_rolled_back_and_the_old_commit_is_the_release(h: Harness) -> None:
    await _rolled_back(h)

    await _look(h, after=SOAK)

    row = await h.row("staging")
    assert row.status == "ROLLED_BACK" and row.finished_at is not None and row.wake_at is None
    assert row.outcome is not None and row.outcome["recovered"] is True and row.outcome["confirmed"] is True
    assert await _verification_kinds(h) == [("deploy", "degraded"), ("rollback", "healthy")]
    async with h.factory() as session:
        latest = (await session.execute(text("SELECT sha FROM ai.release_ledger WHERE repository_id = :r AND environment = 'staging' ORDER BY recorded_at DESC, id DESC LIMIT 1"), {"r": h.seed.repository_id})).scalar_one()
    assert latest == PREVIOUS_SHA  # the next rollback target is what runs now
    assert ("rollback_recovered", "ok") in await h.audit()
    assert "remediation.rollback_recovered" in await h.events()


async def test_a_rollback_that_does_not_recover_stops_loudly_and_never_tries_again(h: Harness) -> None:
    await _bad_deploy(h)
    h.logs.errors_per_minute = 12  # still on fire after going back
    await h.decide("rollback")

    await _look(h, after=SOAK)

    row = await h.row("staging")
    assert row.status == "FAILED" and row.outcome is not None and row.outcome["recovered"] is False
    assert await _verification_kinds(h) == [("deploy", "degraded"), ("rollback", "degraded")]
    assert "remediation.rollback_not_recovered" in await h.events()
    assert len(h.provider.dispatches) == 2  # one deploy and one rollback, never a second rollback
    await h.run("staging")
    assert len(h.provider.dispatches) == 2


async def test_a_rollback_with_too_little_traffic_is_marked_unconfirmed(h: Harness) -> None:
    await _bad_deploy(h)
    h.logs.traffic = 3
    await h.decide("rollback")

    await _look(h, after=SOAK)

    row = await h.row("staging")
    assert row.status == "ROLLED_BACK" and row.outcome is not None and row.outcome["confirmed"] is False and row.outcome["recovered"] is False
    assert "remediation.rollback_unconfirmed" in await h.events()


async def test_a_rollback_pipeline_that_fails_fails_the_remediation(h: Harness) -> None:
    await _bad_deploy(h)
    h.provider.status = "failed"

    await h.decide("rollback")

    row = await h.row("staging")
    assert row.status == "FAILED" and row.outcome == {"failed": "the pipeline run failed", "kind": "rollback"}
    assert ("rollback.failed", "failed") in await h.audit() and "rollback.failed" in await h.events()
    assert await _verification_kinds(h) == [("deploy", "degraded")]


async def test_a_rollback_that_never_appears_fails_instead_of_waiting_forever(h: Harness) -> None:
    await _bad_deploy(h)
    h.provider.hide_run = True
    await h.decide("rollback")
    assert (await h.row("staging")).status == "ROLLING_BACK"

    await _look(h, after=121)

    assert (await h.row("staging")).status == "FAILED"


async def test_switching_deploys_off_stops_a_rollback_before_anything_is_dispatched(h: Harness) -> None:
    await _bad_deploy(h)
    h.settings.deploys_enabled = False

    await h.decide("rollback")

    row = await h.row("staging")
    assert row.status == "FAILED" and row.outcome is not None and "switched off" in row.outcome["failed"]
    assert len(h.provider.dispatches) == 1  # only the original deploy
    assert ("rollback.refused", "refused") in await h.audit()


async def test_a_second_rollback_cannot_be_started(h: Harness) -> None:
    await _rolled_back(h)
    await _look(h, after=SOAK)
    row = await h.row("staging")
    assert row.status == "ROLLED_BACK"

    async with h.factory() as session:
        again = await remediation_store.transition(session, tenant_id=h.seed.tenant, remediation_id=row.id, from_status="AWAITING_DECISION", to="ROLLING_BACK")

    assert not again
    async with h.factory() as session:
        count = (await session.execute(text("SELECT count(*) FROM ai.deployment WHERE remediation_id = :r AND kind = 'rollback'"), {"r": row.id})).scalar_one()
    assert count == 1


async def test_the_question_says_whether_a_rollback_is_possible(h: Harness) -> None:
    await _bad_deploy(h)

    async with h.factory() as session:
        payload = (await session.execute(text("SELECT payload FROM ai.investigation_event WHERE type = 'remediation.decision_requested' AND investigation_id = :i"), {"i": h.seed.investigation_id})).scalar_one()

    assert payload["canRollback"] is True and payload["rollbackTarget"] == PREVIOUS_SHA


async def test_without_a_known_previous_release_a_rollback_is_not_offered(session_factory) -> None:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        harness.deploys.sha = None
        await _bad_deploy(harness)
        async with harness.factory() as session:
            payload = (await session.execute(text("SELECT payload FROM ai.investigation_event WHERE type = 'remediation.decision_requested' AND investigation_id = :i"), {"i": harness.seed.investigation_id})).scalar_one()
        assert payload["canRollback"] is False and payload["rollbackTarget"] is None
    finally:
        await forget(session_factory, harness.seed.tenant)


async def test_a_log_store_that_is_down_at_the_end_leaves_the_rollback_unconfirmed(h: Harness) -> None:
    await _rolled_back(h)
    h.logs.error = ClickHouseError("down")

    await _look(h, after=SOAK)

    row = await h.row("staging")
    assert row.status == "ROLLED_BACK" and row.outcome is not None and row.outcome["confirmed"] is False
