"""After a deploy: the soak, the verdict, and what a person decides when it is not healthy (real graph, real Postgres, a test clock)."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.deploy.executor import thread_config
from app.deploy.nodes import MAX_EXTENSIONS
from app.integrations.clickhouse import ClickHouseError
from app.store import approvals, verifications
from tests.remediation_support import (
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

GRACE = 120  # the default grace period of a target
SOAK = 600  # the soak of the test environments


@pytest_asyncio.fixture
async def h(session_factory) -> AsyncGenerator[Harness]:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        yield harness
    finally:
        await forget(session_factory, harness.seed.tenant)


async def _latest(h: Harness):
    async with h.factory() as session:
        return await verifications.latest(session, tenant_id=h.seed.tenant, remediation_id=h.seed.offers["staging"])


async def _look(h: Harness, *, after: float) -> None:
    """Let ``after`` seconds pass and wake the run."""
    h.clock.advance(seconds=after)
    await h.run("staging")


# ----------------------------------------------------------------------- the soak


async def test_nothing_is_judged_during_the_grace_period(h: Harness) -> None:
    await h.deployed()
    before = h.logs.queries

    await _look(h, after=GRACE - 40)  # the pipeline finished 31 seconds ago and a minute more has passed

    assert (await h.row("staging")).status == "VERIFYING" and h.logs.queries == before  # not even a query: restarts are not judged
    assert await _latest(h) is None


async def test_a_deploy_that_fixed_the_error_is_verified_when_the_soak_ends(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 1  # it was 4 per minute before the deploy

    await _look(h, after=SOAK)

    row = await h.row("staging")
    assert row.status == "VERIFIED" and row.finished_at is not None and row.wake_at is None
    assert row.outcome is not None and row.outcome["verdict"] == "healthy" and row.outcome["deployed"] is True
    verdict = await _latest(h)
    assert verdict is not None and verdict.verdict == "healthy" and verdict.kind == "deploy"
    assert verdict.numbers["baselineErrorsPerMinute"] == 4.0 and verdict.numbers["errorsPerMinute"] < 2 and verdict.numbers["reasons"]
    assert ("deploy.healthy", "ok") in await h.audit()
    assert "remediation.healthy" in await h.events()


async def test_the_environment_is_free_again_once_the_deploy_is_verified(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 0
    await _look(h, after=SOAK)

    async with h.factory() as session:
        free = await session.execute(text("SELECT count(*) FROM ai.remediation WHERE repository_id = :r AND environment = 'staging' AND status IN ('PREFLIGHT','DEPLOYING','VERIFYING','AWAITING_DECISION')"), {"r": h.seed.repository_id})
    assert free.scalar_one() == 0


async def test_a_clear_degradation_ends_the_soak_early_and_asks_a_person(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 10  # worse than the 4 per minute before

    await _look(h, after=GRACE + 6 * 60)  # six minutes of observation: enough, and the soak (ten minutes) is not over

    row = await h.row("staging")
    assert row.status == "AWAITING_DECISION" and row.wake_at is None
    verdict = await _latest(h)
    assert verdict is not None and verdict.verdict == "degraded" and "worse than" in verdict.numbers["reasons"][0]
    events = await h.events()
    assert "remediation.degraded" in events and events[-1] == "remediation.decision_requested"
    assert ("deploy.degraded", "degraded") in await h.audit()


async def test_a_bad_first_minutes_are_not_enough_to_end_the_soak(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 10

    await _look(h, after=GRACE + 2 * 60)  # two minutes of observation could be a restart

    assert (await h.row("staging")).status == "VERIFYING" and await _latest(h) is None


async def test_an_error_that_did_not_change_is_inconclusive_and_asks_a_person(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 4

    await _look(h, after=SOAK)

    row = await h.row("staging")
    verdict = await _latest(h)
    assert row.status == "AWAITING_DECISION" and verdict is not None and verdict.verdict == "inconclusive"


async def test_silence_with_no_traffic_is_inconclusive_never_healthy(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 0
    h.logs.traffic = 3  # the service wrote almost nothing: fixed, or dead?

    await _look(h, after=SOAK)

    verdict = await _latest(h)
    assert verdict is not None and verdict.verdict == "inconclusive" and "log lines" in verdict.numbers["reasons"][0]
    assert (await h.row("staging")).status == "AWAITING_DECISION"


async def test_a_new_error_after_the_deploy_is_degraded_and_named(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 0
    h.logs.others = [{"service_name": "gateway", "fingerprint": "e" * 64, "message_template": "x", "n": 400, "first_ts": h.clock.now, "last_ts": h.clock.now}]

    await _look(h, after=SOAK)

    verdict = await _latest(h)
    assert verdict is not None and verdict.verdict == "degraded" and verdict.numbers["newError"]["fingerprint"] == "e" * 64


async def test_a_log_store_that_fails_while_soaking_is_waited_out(h: Harness) -> None:
    await h.deployed()
    h.logs.error = ClickHouseError("down")

    await _look(h, after=GRACE + 120)

    row = await h.row("staging")
    assert row.status == "VERIFYING" and row.wake_at is not None and await _latest(h) is None  # it looks again


async def test_a_log_store_that_is_down_at_the_end_is_inconclusive(h: Harness) -> None:
    await h.deployed()
    h.logs.error = ClickHouseError("down")

    await _look(h, after=SOAK)

    verdict = await _latest(h)
    assert verdict is not None and verdict.verdict == "inconclusive" and "could not be read" in verdict.numbers["reasons"][0]


# ---------------------------------------------------------------- the person decides


async def _inconclusive(h: Harness) -> None:
    await h.deployed()
    h.logs.errors_per_minute = 4
    await _look(h, after=SOAK)
    assert (await h.row("staging")).status == "AWAITING_DECISION"


async def test_keeping_a_deploy_ends_the_remediation_and_records_who(h: Harness) -> None:
    await _inconclusive(h)

    await h.decide("keep")

    row = await h.row("staging")
    assert row.status == "VERIFIED" and row.finished_at is not None
    async with h.factory() as session:
        answers = (await session.execute(text("SELECT gate, decision, role FROM ai.approval WHERE remediation_id = :r AND gate = 'decision'"), {"r": row.id})).all()
    assert [tuple(a) for a in answers] == [("decision", "keep", "ADMIN")]


async def test_extending_the_soak_watches_again_for_another_window_and_judges_everything_seen(h: Harness) -> None:
    await _inconclusive(h)
    h.logs.errors_per_minute = 0  # it settled after all

    await h.decide("extend")
    row = await h.row("staging")
    assert row.status == "VERIFYING" and row.wake_at is not None  # a new soak window, from now

    await _look(h, after=SOAK)

    assert (await h.row("staging")).status == "VERIFIED"
    verdict = await _latest(h)
    assert verdict is not None and verdict.verdict == "healthy"
    async with h.factory() as session:
        count = (await session.execute(text("SELECT count(*) FROM ai.verification WHERE remediation_id = :r"), {"r": row.id})).scalar_one()
    assert count == 2  # the first (inconclusive) verdict stays on record


async def test_the_second_question_is_answered_by_the_second_answer_not_the_first(h: Harness) -> None:
    await _inconclusive(h)
    await h.decide("extend")
    await _look(h, after=SOAK)  # still 4 per minute: inconclusive again
    assert (await h.row("staging")).status == "AWAITING_DECISION"

    await h.decide("keep")

    assert (await h.row("staging")).status == "VERIFIED"


async def test_a_soak_can_only_be_extended_a_few_times(h: Harness) -> None:
    await _inconclusive(h)
    for _ in range(MAX_EXTENSIONS):
        await h.decide("extend")
        await _look(h, after=SOAK)
        assert (await h.row("staging")).status == "AWAITING_DECISION"

    async with h.factory() as session:
        payload = (await session.execute(text("SELECT payload FROM ai.investigation_event WHERE type = 'remediation.decision_requested' AND investigation_id = :i ORDER BY seq DESC LIMIT 1"), {"i": h.seed.investigation_id})).scalar_one()
    assert payload["canExtend"] is False and payload["round"] == MAX_EXTENSIONS


async def test_asking_to_extend_beyond_the_cap_is_treated_as_keep(h: Harness) -> None:
    await _inconclusive(h)
    for _ in range(MAX_EXTENSIONS):
        await h.decide("extend")
        await _look(h, after=SOAK)
    row = await h.row("staging")
    async with h.factory() as session:  # an answer that the API would have refused, written straight into the table
        await approvals.record(session, tenant_id=h.seed.tenant, remediation_id=row.id, gate="decision", decision="extend", user_id=h.seed.user, role="ADMIN", environment="staging", sha=row.merge_sha)

    await h.run("staging")

    final = (await h.executor._graph.aget_state(thread_config(row.id))).values
    assert final["decision"] == "keep" and final["soak_extensions"] == MAX_EXTENSIONS  # the node did not extend a third time


async def test_the_question_shows_the_verdict_the_numbers_and_the_rollback_target(h: Harness) -> None:
    await _inconclusive(h)

    async with h.factory() as session:
        payload = (await session.execute(text("SELECT payload FROM ai.investigation_event WHERE type = 'remediation.decision_requested' AND investigation_id = :i"), {"i": h.seed.investigation_id})).scalar_one()

    assert payload["verdict"] == "inconclusive" and payload["canExtend"] is True and payload["round"] == 0
    assert payload["rollbackTarget"] == "b" * 40 and payload["approver"] == "ENGINEER" and payload["numbers"]["baselineErrorsPerMinute"] == 4.0
    assert "gate" not in payload
