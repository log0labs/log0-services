"""The runner of deploys: one run per remediation at a time, no lost answers, the sweeper, and a crash that never keeps an environment locked."""

import asyncio
import os
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.deploy.runner import RemediationRunner
from app.runner import SessionFactory
from tests.remediation_support import (  # noqa: F401  (fixture)
    Harness,
    build_harness,
    forget,
    session_factory,
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("SKIP_INTEGRATION_TESTS", "").lower() in ("1", "true", "yes"),
        reason="Postgres integration tests (set SKIP_INTEGRATION_TESTS=1 to skip)",
    ),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def h(session_factory) -> Harness:  # noqa: F811
    harness = await build_harness(session_factory)
    try:
        yield harness
    finally:
        await forget(session_factory, harness.seed.tenant)


class Recorder:
    """An ``execute`` that records its calls; ``gate`` holds the first call open until the test lets it go."""

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.gate = asyncio.Event()
        self.hold = False
        self.running = 0
        self.max_running = 0
        self.error: Exception | None = None

    async def __call__(self, factory: SessionFactory, remediation_id: UUID, tenant_id: UUID) -> None:
        self.calls.append(remediation_id)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.hold and len(self.calls) == 1:
                await self.gate.wait()
            if self.error:
                raise self.error
        finally:
            self.running -= 1


async def test_a_scheduled_run_executes(h: Harness) -> None:
    execute = Recorder()
    runner = RemediationRunner(h.factory, execute, now=h.clock)
    offer = h.seed.offers["staging"]

    runner.schedule(offer, h.seed.tenant)
    await runner.wait_idle(offer)

    assert execute.calls == [offer]


async def test_a_request_while_running_repeats_the_run_once_and_never_overlaps(h: Harness) -> None:
    execute = Recorder()
    execute.hold = True
    runner = RemediationRunner(h.factory, execute, now=h.clock)
    offer = h.seed.offers["staging"]

    runner.schedule(offer, h.seed.tenant)
    await asyncio.sleep(0.05)
    runner.schedule(offer, h.seed.tenant)  # the engineer clicked while a poll was in progress
    runner.schedule(offer, h.seed.tenant)  # and again
    assert runner.is_running(offer) and execute.calls == [offer]
    execute.gate.set()
    await runner.wait_idle(offer)

    assert execute.calls == [offer, offer]  # once more, not twice
    assert execute.max_running == 1


async def test_different_remediations_run_side_by_side(h: Harness) -> None:
    execute = Recorder()
    runner = RemediationRunner(h.factory, execute, now=h.clock)

    runner.schedule(h.seed.offers["staging"], h.seed.tenant)
    runner.schedule(h.seed.offers["production"], h.seed.tenant)
    await runner.wait_idle(h.seed.offers["staging"])
    await runner.wait_idle(h.seed.offers["production"])

    assert sorted(execute.calls) == sorted([h.seed.offers["staging"], h.seed.offers["production"]])


async def test_a_run_that_dies_unexpectedly_fails_the_remediation_so_the_environment_is_free(h: Harness) -> None:
    async with h.factory() as session:
        await session.execute(text("UPDATE ai.remediation SET status = 'PREFLIGHT' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.commit()
    row = await h.row("staging")
    execute = Recorder()
    execute.error = RuntimeError("a bug")
    runner = RemediationRunner(h.factory, execute, now=h.clock)

    runner.schedule(row.id, h.seed.tenant)
    await runner.wait_idle(row.id)

    failed = await h.row("staging")
    assert failed.status == "FAILED" and failed.finished_at is not None and failed.outcome == {"failed": "log0 hit an internal error", "error": "RuntimeError"}
    assert ("remediation.crashed", "failed") in await h.audit()
    assert "a bug" not in str(failed.outcome)  # the message of an unexpected error is not shown or stored


async def test_the_sweeper_schedules_only_the_runs_whose_time_has_come(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")  # parks with a wake-up time 30 seconds away
    execute = Recorder()
    runner = RemediationRunner(h.factory, execute, now=h.clock)

    assert await runner.sweep_once() == 0
    h.clock.advance(seconds=31)
    assert await runner.sweep_once() == 1
    await runner.wait_idle(h.seed.offers["staging"])

    assert execute.calls == [h.seed.offers["staging"]]


async def test_a_run_waiting_for_a_human_has_no_wake_up_time_and_is_never_swept(h: Harness) -> None:
    await h.start("staging")
    execute = Recorder()
    runner = RemediationRunner(h.factory, execute, now=h.clock)

    h.clock.advance(hours=48)

    assert (await h.row("staging")).wake_at is None
    assert await runner.sweep_once() == 0


async def test_runs_that_were_mid_flight_when_the_service_stopped_are_picked_up(h: Harness) -> None:
    async with h.factory() as session:
        await session.execute(text("UPDATE ai.remediation SET status = 'PREFLIGHT' WHERE id = :i"), {"i": h.seed.offers["staging"]})
        await session.execute(text("UPDATE ai.remediation SET status = 'AWAITING_DEPLOY_APPROVAL' WHERE id = :i"), {"i": h.seed.offers["production"]})
        await session.commit()
    execute = Recorder()
    runner = RemediationRunner(h.factory, execute, now=h.clock)

    assert await runner.recover_on_startup() == 1  # not the one waiting for a human
    await runner.wait_idle(h.seed.offers["staging"])

    assert execute.calls == [h.seed.offers["staging"]]


async def test_the_sweeper_loop_wakes_due_runs_by_itself_and_stops_on_shutdown(h: Harness) -> None:
    await h.start("staging")
    await h.answer("approve")
    h.provider.status = "succeeded"
    runner = RemediationRunner(h.factory, h.executor.run, sweep_seconds=0.02, now=h.clock)
    h.clock.advance(seconds=31)

    runner.start_sweeper()
    runner.start_sweeper()  # starting twice does not start two
    for _ in range(100):
        if (await h.row("staging")).status == "VERIFYING":
            break
        await asyncio.sleep(0.05)
    await runner.shutdown()

    assert (await h.row("staging")).status == "VERIFYING"
